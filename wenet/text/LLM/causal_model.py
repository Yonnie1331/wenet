from typing import Dict, List, Optional, Union
import torch
from wenet.text.LLM.decoder import DecoderOnly
from wenet.text.LLM.sampler import sampler
from wenet.transformer.label_smoothing_loss import LabelSmoothingLoss
from wenet.utils.common import IGNORE_ID, add_sos_eos, th_accuracy
from wenet.utils.mask import make_non_pad_mask, subsequent_mask


class CausalLM(torch.nn.Module):

    def __init__(
        self,
        vocab_size: int,
        decoder: DecoderOnly,
        tie_word_embedding: bool = False,
        linear_bias: bool = False,
        special_tokens: Optional[dict] = None,
        ignore_id: int = IGNORE_ID,
        lsm_weight: float = 0.0,
        length_normalized_loss: bool = False,
    ) -> None:
        super().__init__()

        self.embed = torch.nn.Embedding(vocab_size, decoder.hidden_size)
        self.out = torch.nn.Linear(decoder.hidden_size,
                                   vocab_size,
                                   bias=linear_bias)
        if tie_word_embedding:
            self.out.weight = self.embed.weight

        self.decoder = decoder
        self.sos = (vocab_size - 1 if special_tokens is None else
                    special_tokens.get("<sos>", vocab_size - 1))
        self.eos = (vocab_size - 1 if special_tokens is None else
                    special_tokens.get("<eos>", vocab_size - 1))

        self.criterion_att = LabelSmoothingLoss(
            size=vocab_size,
            padding_idx=ignore_id,
            smoothing=lsm_weight,
            normalize_length=length_normalized_loss,
        )

        self.ignore_id = ignore_id

    @torch.jit.ignore(drop=True)
    def forward(
        self,
        batch: dict,
        device: torch.device,
    ) -> Dict[str, Optional[torch.Tensor]]:

        text = batch['text'].to(device)
        text_length = batch['text_lengths'].to(device)

        ys_in_pad, ys_out_pad = add_sos_eos(text, self.sos, self.eos,
                                            self.ignore_id)
        ys_in_lens = text_length + 1

        # TODO: fix maxlength for pading to max
        tgt_mask = make_non_pad_mask(ys_in_lens).unsqueeze(1).to(
            text.device)  # (B, 1, L)
        causal_mask = subsequent_mask(tgt_mask.size(-1),
                                      device=tgt_mask.device).unsqueeze(
                                          0)  # (1,L,L)
        att_mask = causal_mask & tgt_mask  # (B, L, L)

        embeding = self.embed(ys_in_pad)
        decoder_out = self.out(self.decoder(embeding,
                                            att_mask)[0])  # (B, L, vocab_size)

        loss = self.criterion_att(decoder_out, ys_out_pad)
        acc = th_accuracy(decoder_out.view(-1, self.vocab_size),
                          ys_out_pad,
                          ignore_label=self.ignore_id)

        # TODO: ppl
        return {"loss": loss, "ppl": None, "th_accuracy": acc}

    def generate(
        self,
        prompts_tokens: List[List[int]],
        device: torch.device,
        dtype: torch.dtype = torch.float32,
        output_len: int = 100,
        temperature: Union[float, None] = 0.95,
        top_p: float = 1.0,
        top_k: int = 100,
    ) -> List[List[int]]:
        """Generates responses for given prompts using Gemma model."""
        # If a single prompt is provided, treat it as a batch of 1.

        batch_size = len(prompts_tokens)
        min_prompt_len = min(len(p) for p in prompts_tokens)
        max_prompt_len = max(len(p) for p in prompts_tokens)
        # NOTE(Mddct): 1 is sos
        max_seq_len = max_prompt_len + 1 + output_len
        assert max_seq_len <= self.decoder.pos_enc.max_len

        # build KV caches
        kv_caches = []
        for _ in range(self.config.num_hidden_layers):
            size = (batch_size, self.decoder.n_kv_head, min_prompt_len + 1,
                    self.decoder.head_dim)
            k_cache = torch.zeros(size=size, dtype=dtype, device=device)
            v_cache = torch.zeros(size=size, dtype=dtype, device=device)
            # kv_caches.append((k_cache, v_cache))
            # TODO(Mddct): wenet concat k v cache in dims=-1, refactor later to
            #     reduce memory fragment
            kv_caches = torch.cat([k_cache, v_cache], dim=-1)

        # prepare inputs
        token_ids_tensor = torch.full((batch_size, max_prompt_len),
                                      IGNORE_ID,
                                      dtype=torch.int64)
        input_token_ids_tensor = torch.full((batch_size, min_prompt_len),
                                            IGNORE_ID,
                                            dtype=torch.int64)
        # right padding
        for i, p in enumerate(prompts_tokens):
            token_ids_tensor[i, :len(p)] = torch.tensor(p)
            input_token_ids_tensor[i, :min_prompt_len] = torch.tensor(
                p[:min_prompt_len])

        # add sos
        sos = torch.tensor([self.sos] * batch_size).unsqueeze(0)
        token_ids_tensor = torch.cat([sos, token_ids_tensor],
                                     dim=-1).to(device)
        input_token_ids_tensor = torch.cat([sos, token_ids_tensor],
                                           dim=-1).to(device)
        prompt_mask_tensor = token_ids_tensor != IGNORE_ID
        min_prompt_len = min_prompt_len + 1
        max_prompt_len = max_prompt_len + 1
        input_positions_tensor = torch.arange(0,
                                              min_prompt_len,
                                              dtype=torch.int64).to(device)
        mask_tensor = torch.ones((1, 1, max_prompt_len, max_prompt_len),
                                 dtype=torch.bool)
        mask_tensor = torch.triu(mask_tensor, diagonal=1).to(device)
        curr_mask_tensor = mask_tensor.index_select(2, input_positions_tensor)
        att_mask = curr_mask_tensor.squeeze(1)
        output_positions_tensor = torch.LongTensor([min_prompt_len - 1
                                                    ]).to(device)
        temperatures_tensor = None if not temperature else torch.FloatTensor(
            [temperature] * batch_size).to(device)
        top_ps_tensor = torch.FloatTensor([top_p] * batch_size).to(device)
        top_ks_tensor = torch.LongTensor([top_k] * batch_size).to(device)
        output_index = torch.tensor(min_prompt_len,
                                    dtype=torch.int64).to(device)

        input_token_embeding = self.embed(input_token_ids_tensor)
        # Prefill up to min_prompt_len tokens, then treat other prefill as
        # decode and ignore output.
        for i in range(max_seq_len - min_prompt_len):
            decoder_out, kv_caches, = self.decoder(input_token_embeding,
                                                   att_mask,
                                                   input_positions_tensor,
                                                   kv_caches)
            decoder_out = decoder_out.index_select(1, output_positions_tensor)
            next_token_ids = sampler(
                decoder_out,
                temperatures_tensor,
                top_ps_tensor,
                top_ks_tensor,
            )
            curr_prompt_mask = prompt_mask_tensor.index_select(
                1, output_index).squeeze(dim=1)
            curr_token_ids = token_ids_tensor.index_select(
                1, output_index).squeeze(dim=1)
            output_token_ids = torch.where(curr_prompt_mask, curr_token_ids,
                                           next_token_ids).unsqueeze(dim=1)
            token_ids_tensor.index_copy_(1, output_index, output_token_ids)

            input_token_ids_tensor = output_token_ids
            input_positions_tensor = output_index.unsqueeze(dim=-1)
            curr_mask_tensor = mask_tensor.index_select(
                2, input_positions_tensor)
            att_mask = curr_mask_tensor.squeeze(1)
            output_positions_tensor = torch.tensor(
                0, dtype=torch.int64).to(device)
            output_index = output_index + 1

        token_ids = token_ids_tensor.tolist()
        results = []
        for i, tokens in enumerate(token_ids):
            trimmed_output = tokens[len(prompts_tokens[i]
                                        ):len(prompts_tokens[i]) + output_len]
            if self.eos in trimmed_output:
                eos_index = trimmed_output.index(self.eos)
                trimmed_output = trimmed_output[:eos_index]
            results.append(trimmed_output)

        return results

# coding=utf-8
# Copyright 2022 EleutherAI and the HuggingFace Inc. team. All rights reserved.
#
# This code is based on EleutherAI's GPT-NeoX library and the GPT-NeoX
# and OPT implementations in this library. It has been modified from its
# original forms to accommodate minor architectural differences compared
# to GPT-NeoX and OPT used by the Meta AI team that trained the model.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
""" PyTorch Zebra model."""
import math
from typing import List, Optional, Tuple, Union

import torch
import torch.distributions as dist
import torch.nn.functional as F
import torch.utils.checkpoint
from torch import nn
from torch.nn import BCEWithLogitsLoss, CrossEntropyLoss, MSELoss

from ...activations import ACT2FN
from ...modeling_outputs import BaseModelOutputWithPast, CausalLMOutputWithPast, SequenceClassifierOutputWithPast, MaskedLMOutput
from ...modeling_utils import PreTrainedModel
from ...pytorch_utils import ALL_LAYERNORM_LAYERS
from ...utils import (
    add_start_docstrings,
    add_start_docstrings_to_model_forward,
    is_flash_attn_2_available,
    logging,
    replace_return_docstrings,
)
from .configuration_zebra import ZebraConfig
import time
from einops import rearrange
from copy import deepcopy


if is_flash_attn_2_available():
    from flash_attn import flash_attn_func, flash_attn_varlen_func
    from flash_attn.bert_padding import index_first_axis, pad_input, unpad_input  # noqa


logger = logging.get_logger(__name__)

_CONFIG_FOR_DOC = "ZebraConfig"

def _get_unpad_data(padding_mask):
    seqlens_in_batch = padding_mask.sum(dim=-1, dtype=torch.int32)
    indices = torch.nonzero(padding_mask.flatten(), as_tuple=False).flatten()
    max_seqlen_in_batch = seqlens_in_batch.max().item()
    cu_seqlens = F.pad(torch.cumsum(seqlens_in_batch, dim=0, dtype=torch.torch.int32), (1, 0))
    return (
        indices,
        cu_seqlens,
        max_seqlen_in_batch,
    )




def _make_bidirectional_mask(input_ids, pad_token_id):
    # Create a mask of 1s for each token followed by 0s for padding
    attention_mask = (input_ids != pad_token_id).type(torch.float)
    return attention_mask


# Copied from transformers.models.bart.modeling_bart._make_causal_mask
def _make_causal_mask(
    input_ids_shape: torch.Size, dtype: torch.dtype, device: torch.device, past_key_values_length: int = 0
):
    """
    Make causal mask used for bi-directional self-attention.
    """
    bsz, tgt_len = input_ids_shape
    mask = torch.full((tgt_len, tgt_len), torch.finfo(dtype).min, device=device)
    mask_cond = torch.arange(mask.size(-1), device=device)
    mask.masked_fill_(mask_cond < (mask_cond + 1).view(mask.size(-1), 1), 0)
    mask = mask.to(dtype)

    if past_key_values_length > 0:
        mask = torch.cat([torch.zeros(tgt_len, past_key_values_length, dtype=dtype, device=device), mask], dim=-1)
    return mask[None, None, :, :].expand(bsz, 1, tgt_len, tgt_len + past_key_values_length)


# Copied from transformers.models.bart.modeling_bart._expand_mask
def _expand_mask(mask: torch.Tensor, dtype: torch.dtype, tgt_len: Optional[int] = None):
    """
    Expands attention_mask from `[bsz, seq_len]` to `[bsz, 1, tgt_seq_len, src_seq_len]`.
    """
    bsz, src_len = mask.size()
    tgt_len = tgt_len if tgt_len is not None else src_len

    expanded_mask = mask[:, None, None, :].expand(bsz, 1, tgt_len, src_len).to(dtype)

    inverted_mask = 1.0 - expanded_mask

    return inverted_mask.masked_fill(inverted_mask.to(torch.bool), torch.finfo(dtype).min)


class ZebraRMSNorm(nn.Module):
    def __init__(self, hidden_size, eps=1e-6):
        """
        ZebraRMSNorm is equivalent to T5LayerNorm
        """
        super().__init__()
        self.weight = nn.Parameter(torch.ones(hidden_size))
        self.variance_epsilon = eps

    def forward(self, hidden_states):
        input_dtype = hidden_states.dtype
        hidden_states = hidden_states.to(torch.float32)
        variance = hidden_states.pow(2).mean(-1, keepdim=True)
        hidden_states = hidden_states * torch.rsqrt(variance + self.variance_epsilon)
        return self.weight * hidden_states.to(input_dtype)


ALL_LAYERNORM_LAYERS.append(ZebraRMSNorm)


class ZebraRotaryEmbedding(nn.Module):
    def __init__(self, dim, max_position_embeddings=2048, base=10000, device=None):
        super().__init__()

        self.dim = dim
        self.max_position_embeddings = max_position_embeddings
        self.base = base
        inv_freq = 1.0 / (self.base ** (torch.arange(0, self.dim, 2).float().to(device) / self.dim))
        self.register_buffer("inv_freq", inv_freq, persistent=False)

        # Build here to make `torch.jit.trace` work.
        self._set_cos_sin_cache(
            seq_len=max_position_embeddings, device=self.inv_freq.device, dtype=torch.get_default_dtype()
        )

    def _set_cos_sin_cache(self, seq_len, device, dtype):
        self.max_seq_len_cached = seq_len
        t = torch.arange(self.max_seq_len_cached, device=device, dtype=self.inv_freq.dtype)

        freqs = torch.einsum("i,j->ij", t, self.inv_freq)
        # Different from paper, but it uses a different permutation in order to obtain the same calculation
        emb = torch.cat((freqs, freqs), dim=-1)
        self.register_buffer("cos_cached", emb.cos()[None, None, :, :].to(dtype), persistent=False)
        self.register_buffer("sin_cached", emb.sin()[None, None, :, :].to(dtype), persistent=False)

    def forward(self, x, seq_len=None):
        # x: [bs, num_attention_heads, seq_len, head_size]
        if seq_len > self.max_seq_len_cached:
            self._set_cos_sin_cache(seq_len=seq_len, device=x.device, dtype=x.dtype)

        return (
            self.cos_cached[:, :, :seq_len, ...].to(dtype=x.dtype),
            self.sin_cached[:, :, :seq_len, ...].to(dtype=x.dtype),
        )


class ZebraLinearScalingRotaryEmbedding(ZebraRotaryEmbedding):
    """ZebraRotaryEmbedding extended with linear scaling. Credits to the Reddit user /u/kaiokendev"""

    def __init__(self, dim, max_position_embeddings=2048, base=10000, device=None, scaling_factor=1.0):
        self.scaling_factor = scaling_factor
        super().__init__(dim, max_position_embeddings, base, device)

    def _set_cos_sin_cache(self, seq_len, device, dtype):
        self.max_seq_len_cached = seq_len
        t = torch.arange(self.max_seq_len_cached, device=device, dtype=self.inv_freq.dtype)
        t = t / self.scaling_factor

        freqs = torch.einsum("i,j->ij", t, self.inv_freq)
        # Different from paper, but it uses a different permutation in order to obtain the same calculation
        emb = torch.cat((freqs, freqs), dim=-1)
        self.register_buffer("cos_cached", emb.cos()[None, None, :, :].to(dtype), persistent=False)
        self.register_buffer("sin_cached", emb.sin()[None, None, :, :].to(dtype), persistent=False)


class ZebraDynamicNTKScalingRotaryEmbedding(ZebraRotaryEmbedding):
    """ZebraRotaryEmbedding extended with Dynamic NTK scaling. Credits to the Reddit users /u/bloc97 and /u/emozilla"""

    def __init__(self, dim, max_position_embeddings=2048, base=10000, device=None, scaling_factor=1.0):
        self.scaling_factor = scaling_factor
        super().__init__(dim, max_position_embeddings, base, device)

    def _set_cos_sin_cache(self, seq_len, device, dtype):
        self.max_seq_len_cached = seq_len

        if seq_len > self.max_position_embeddings:
            base = self.base * (
                (self.scaling_factor * seq_len / self.max_position_embeddings) - (self.scaling_factor - 1)
            ) ** (self.dim / (self.dim - 2))
            inv_freq = 1.0 / (base ** (torch.arange(0, self.dim, 2).float().to(device) / self.dim))
            self.register_buffer("inv_freq", inv_freq, persistent=False)

        t = torch.arange(self.max_seq_len_cached, device=device, dtype=self.inv_freq.dtype)

        freqs = torch.einsum("i,j->ij", t, self.inv_freq)
        # Different from paper, but it uses a different permutation in order to obtain the same calculation
        emb = torch.cat((freqs, freqs), dim=-1)
        self.register_buffer("cos_cached", emb.cos()[None, None, :, :].to(dtype), persistent=False)
        self.register_buffer("sin_cached", emb.sin()[None, None, :, :].to(dtype), persistent=False)


def rotate_half(x):
    """Rotates half the hidden dims of the input."""
    x1 = x[..., : x.shape[-1] // 2]
    x2 = x[..., x.shape[-1] // 2 :]
    return torch.cat((-x2, x1), dim=-1)


def apply_rotary_pos_emb(q, k, cos, sin, position_ids):
    # The first two dimensions of cos and sin are always 1, so we can `squeeze` them.
    cos = cos.squeeze(1).squeeze(0)  # [seq_len, dim]
    sin = sin.squeeze(1).squeeze(0)  # [seq_len, dim]
    cos = cos[position_ids].unsqueeze(1)  # [bs, 1, seq_len, dim]
    sin = sin[position_ids].unsqueeze(1)  # [bs, 1, seq_len, dim]
    q_embed = (q * cos) + (rotate_half(q) * sin)
    k_embed = (k * cos) + (rotate_half(k) * sin)
    return q_embed, k_embed


class ZebraMLP(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.hidden_size = config.hidden_size
        self.intermediate_size = config.intermediate_size
        self.gate_proj = nn.Linear(self.hidden_size, self.intermediate_size, bias=False)
        self.up_proj = nn.Linear(self.hidden_size, self.intermediate_size, bias=False)
        self.down_proj = nn.Linear(self.intermediate_size, self.hidden_size, bias=False)
        self.act_fn = ACT2FN[config.hidden_act]

    def forward(self, x):
        if self.config.pretraining_tp > 1:
            slice = self.intermediate_size // self.config.pretraining_tp
            gate_proj_slices = self.gate_proj.weight.split(slice, dim=0)
            up_proj_slices = self.up_proj.weight.split(slice, dim=0)
            down_proj_slices = self.down_proj.weight.split(slice, dim=1)

            gate_proj = torch.cat(
                [F.linear(x, gate_proj_slices[i]) for i in range(self.config.pretraining_tp)], dim=-1
            )
            up_proj = torch.cat([F.linear(x, up_proj_slices[i]) for i in range(self.config.pretraining_tp)], dim=-1)

            intermediate_states = (self.act_fn(gate_proj) * up_proj).split(slice, dim=2)
            down_proj = [
                F.linear(intermediate_states[i], down_proj_slices[i]) for i in range(self.config.pretraining_tp)
            ]
            down_proj = sum(down_proj)
        else:
            down_proj = self.down_proj(self.act_fn(self.gate_proj(x)) * self.up_proj(x))

        return down_proj


def repeat_kv(hidden_states: torch.Tensor, n_rep: int) -> torch.Tensor:
    """
    This is the equivalent of torch.repeat_interleave(x, dim=1, repeats=n_rep). The hidden states go from (batch,
    num_key_value_heads, seqlen, head_dim) to (batch, num_attention_heads, seqlen, head_dim)
    """
    batch, num_key_value_heads, slen, head_dim = hidden_states.shape
    if n_rep == 1:
        return hidden_states
    hidden_states = hidden_states[:, :, None, :, :].expand(batch, num_key_value_heads, n_rep, slen, head_dim)
    return hidden_states.reshape(batch, num_key_value_heads * n_rep, slen, head_dim)


class ZebraAttention(nn.Module):
    """Multi-headed attention from 'Attention Is All You Need' paper"""

    def __init__(self, config: ZebraConfig):
        super().__init__()
        self.config = config
        self.hidden_size = config.hidden_size
        self.num_heads = config.num_attention_heads
        self.head_dim = self.hidden_size // self.num_heads
        self.num_key_value_heads = config.num_key_value_heads
        self.num_key_value_groups = self.num_heads // self.num_key_value_heads
        self.max_position_embeddings = config.max_position_embeddings
        self.rope_theta = config.rope_theta

        if (self.head_dim * self.num_heads) != self.hidden_size:
            raise ValueError(
                f"hidden_size must be divisible by num_heads (got `hidden_size`: {self.hidden_size}"
                f" and `num_heads`: {self.num_heads})."
            )
        self.q_proj = nn.Linear(self.hidden_size, self.num_heads * self.head_dim, bias=config.attention_bias)
        self.k_proj = nn.Linear(self.hidden_size, self.num_key_value_heads * self.head_dim, bias=config.attention_bias)
        self.v_proj = nn.Linear(self.hidden_size, self.num_key_value_heads * self.head_dim, bias=config.attention_bias)
        self.o_proj = nn.Linear(self.num_heads * self.head_dim, self.hidden_size, bias=config.attention_bias)
        self._init_rope()

    def _init_rope(self):
        if self.config.rope_scaling is None:
            self.rotary_emb = ZebraRotaryEmbedding(
                self.head_dim,
                max_position_embeddings=self.max_position_embeddings,
                base=self.rope_theta,
            )
        else:
            scaling_type = self.config.rope_scaling["type"]
            scaling_factor = self.config.rope_scaling["factor"]
            if scaling_type == "linear":
                self.rotary_emb = ZebraLinearScalingRotaryEmbedding(
                    self.head_dim,
                    max_position_embeddings=self.max_position_embeddings,
                    scaling_factor=scaling_factor,
                    base=self.rope_theta,
                )
            elif scaling_type == "dynamic":
                self.rotary_emb = ZebraDynamicNTKScalingRotaryEmbedding(
                    self.head_dim,
                    max_position_embeddings=self.max_position_embeddings,
                    scaling_factor=scaling_factor,
                    base=self.rope_theta,
                )
            else:
                raise ValueError(f"Unknown RoPE scaling type {scaling_type}")

    def _shape(self, tensor: torch.Tensor, seq_len: int, bsz: int):
        return tensor.view(bsz, seq_len, self.num_heads, self.head_dim).transpose(1, 2).contiguous()

    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_value: Optional[Tuple[torch.Tensor]] = None,
        output_attentions: bool = False,
        use_cache: bool = False,
        padding_mask: Optional[torch.LongTensor] = None,
    ) -> Tuple[torch.Tensor, Optional[torch.Tensor], Optional[Tuple[torch.Tensor]]]:
        bsz, q_len, _ = hidden_states.size()

        if self.config.pretraining_tp > 1:
            key_value_slicing = (self.num_key_value_heads * self.head_dim) // self.config.pretraining_tp
            query_slices = self.q_proj.weight.split(
                (self.num_heads * self.head_dim) // self.config.pretraining_tp, dim=0
            )
            key_slices = self.k_proj.weight.split(key_value_slicing, dim=0)
            value_slices = self.v_proj.weight.split(key_value_slicing, dim=0)

            query_states = [F.linear(hidden_states, query_slices[i]) for i in range(self.config.pretraining_tp)]
            query_states = torch.cat(query_states, dim=-1)

            key_states = [F.linear(hidden_states, key_slices[i]) for i in range(self.config.pretraining_tp)]
            key_states = torch.cat(key_states, dim=-1)

            value_states = [F.linear(hidden_states, value_slices[i]) for i in range(self.config.pretraining_tp)]
            value_states = torch.cat(value_states, dim=-1)

        else:
            query_states = self.q_proj(hidden_states)
            key_states = self.k_proj(hidden_states)
            value_states = self.v_proj(hidden_states)

        query_states = query_states.view(bsz, q_len, self.num_heads, self.head_dim).transpose(1, 2)
        key_states = key_states.view(bsz, q_len, self.num_key_value_heads, self.head_dim).transpose(1, 2)
        value_states = value_states.view(bsz, q_len, self.num_key_value_heads, self.head_dim).transpose(1, 2)

        kv_seq_len = key_states.shape[-2]
        if past_key_value is not None:
            kv_seq_len += past_key_value[0].shape[-2]
        cos, sin = self.rotary_emb(value_states, seq_len=kv_seq_len)
        query_states, key_states = apply_rotary_pos_emb(query_states, key_states, cos, sin, position_ids)

        if past_key_value is not None:
            # reuse k, v, self_attention
            key_states = torch.cat([past_key_value[0], key_states], dim=2)
            value_states = torch.cat([past_key_value[1], value_states], dim=2)

        past_key_value = (key_states, value_states) if use_cache else None

        key_states = repeat_kv(key_states, self.num_key_value_groups)
        value_states = repeat_kv(value_states, self.num_key_value_groups)

        attn_weights = torch.matmul(query_states, key_states.transpose(2, 3)) / math.sqrt(self.head_dim)

        if attn_weights.size() != (bsz, self.num_heads, q_len, kv_seq_len):
            raise ValueError(
                f"Attention weights should be of size {(bsz, self.num_heads, q_len, kv_seq_len)}, but is"
                f" {attn_weights.size()}"
            )

        if attention_mask is not None:
            if attention_mask.size() != (bsz, 1, q_len, kv_seq_len):
                raise ValueError(
                    f"Attention mask should be of size {(bsz, 1, q_len, kv_seq_len)}, but is {attention_mask.size()}"
                )
            attn_weights = attn_weights + attention_mask

        # upcast attention to fp32
        attn_weights = nn.functional.softmax(attn_weights, dim=-1, dtype=torch.float32).to(query_states.dtype)
        attn_output = torch.matmul(attn_weights, value_states)

        if attn_output.size() != (bsz, self.num_heads, q_len, self.head_dim):
            raise ValueError(
                f"`attn_output` should be of size {(bsz, self.num_heads, q_len, self.head_dim)}, but is"
                f" {attn_output.size()}"
            )

        attn_output = attn_output.transpose(1, 2).contiguous()

        attn_output = attn_output.reshape(bsz, q_len, self.hidden_size)

        if self.config.pretraining_tp > 1:
            attn_output = attn_output.split(self.hidden_size // self.config.pretraining_tp, dim=2)
            o_proj_slices = self.o_proj.weight.split(self.hidden_size // self.config.pretraining_tp, dim=1)
            attn_output = sum([F.linear(attn_output[i], o_proj_slices[i]) for i in range(self.config.pretraining_tp)])
        else:
            attn_output = self.o_proj(attn_output)

        if not output_attentions:
            attn_weights = None

        return attn_output, attn_weights, past_key_value


class ZebraFlashAttention2(ZebraAttention):
    """
    Zebra flash attention module. This module inherits from `ZebraAttention` as the weights of the module stays
    untouched. The only required change would be on the forward pass where it needs to correctly call the public API of
    flash attention and deal with padding tokens in case the input contains any of them.
    """

    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_value: Optional[Tuple[torch.Tensor]] = None,
        output_attentions: bool = False,
        use_cache: bool = False,
        padding_mask: Optional[torch.LongTensor] = None,
    ) -> Tuple[torch.Tensor, Optional[torch.Tensor], Optional[Tuple[torch.Tensor]]]:
        # ZebraFlashAttention2 attention does not support output_attentions
        output_attentions = False

        bsz, q_len, _ = hidden_states.size()

        query_states = self.q_proj(hidden_states)
        key_states = self.k_proj(hidden_states)
        value_states = self.v_proj(hidden_states)

        # Flash attention requires the input to have the shape
        # batch_size x seq_length x head_dime x hidden_dim
        # therefore we just need to keep the original shape
        query_states = query_states.view(bsz, q_len, self.num_heads, self.head_dim).transpose(1, 2)
        key_states = key_states.view(bsz, q_len, self.num_key_value_heads, self.head_dim).transpose(1, 2)
        value_states = value_states.view(bsz, q_len, self.num_key_value_heads, self.head_dim).transpose(1, 2)

        kv_seq_len = key_states.shape[-2]
        if past_key_value is not None:
            kv_seq_len += past_key_value[0].shape[-2]

        cos, sin = self.rotary_emb(value_states, seq_len=kv_seq_len)

        query_states, key_states = apply_rotary_pos_emb(query_states, key_states, cos, sin, position_ids)

        if past_key_value is not None:
            # reuse k, v, self_attention
            key_states = torch.cat([past_key_value[0], key_states], dim=2)
            value_states = torch.cat([past_key_value[1], value_states], dim=2)

        past_key_value = (key_states, value_states) if use_cache else None

        query_states = query_states.transpose(1, 2)
        key_states = key_states.transpose(1, 2)
        value_states = value_states.transpose(1, 2)

        # TODO: zebra does not have dropout in the config??
        # It is recommended to use dropout with FA according to the docs
        # when training.
        dropout_rate = 0.0  # if not self.training else self.attn_dropout

        # In PEFT, usually we cast the layer norms in float32 for training stability reasons
        # therefore the input hidden states gets silently casted in float32. Hence, we need
        # cast them back in float16 just to be sure everything works as expected.
        # This might slowdown training & inference so it is recommended to not cast the LayerNorms
        # in fp32. (ZebraRMSNorm handles it correctly)
        input_dtype = query_states.dtype
        if input_dtype == torch.float32:
            logger.warning_once(
                "The input hidden states seems to be silently casted in float32, this might be related to"
                " the fact you have upcasted embedding or layer norm layers in float32. We will cast back the input in"
                " float16."
            )

            query_states = query_states.to(torch.float16)
            key_states = key_states.to(torch.float16)
            value_states = value_states.to(torch.float16)

        attn_output = self._flash_attention_forward(
            query_states, key_states, value_states, padding_mask, q_len, dropout=dropout_rate
        )

        attn_output = attn_output.reshape(bsz, q_len, self.hidden_size).contiguous()
        attn_output = self.o_proj(attn_output)

        if not output_attentions:
            attn_weights = None

        return attn_output, attn_weights, past_key_value

    def _flash_attention_forward(
        self, query_states, key_states, value_states, padding_mask, query_length, dropout=0.0, softmax_scale=None
    ):
        """
        Calls the forward method of Flash Attention - if the input hidden states contain at least one padding token
        first unpad the input, then computes the attention scores and pad the final attention scores.

        Args:
            query_states (`torch.Tensor`):
                Input query states to be passed to Flash Attention API
            key_states (`torch.Tensor`):
                Input key states to be passed to Flash Attention API
            value_states (`torch.Tensor`):
                Input value states to be passed to Flash Attention API
            padding_mask (`torch.Tensor`):
                The padding mask - corresponds to a tensor of size `(batch_size, seq_len)` where 0 stands for the
                position of padding tokens and 1 for the position of non-padding tokens.
            dropout (`int`, *optional*):
                Attention dropout
            softmax_scale (`float`, *optional*):
                The scaling of QK^T before applying softmax. Default to 1 / sqrt(head_dim)
        """
        # Contains at least one padding token in the sequence
        if padding_mask is not None:
            batch_size = query_states.shape[0]
            query_states, key_states, value_states, indices_q, cu_seq_lens, max_seq_lens = self._upad_input(
                query_states, key_states, value_states, padding_mask, query_length
            )

            cu_seqlens_q, cu_seqlens_k = cu_seq_lens
            max_seqlen_in_batch_q, max_seqlen_in_batch_k = max_seq_lens

            attn_output_unpad = flash_attn_varlen_func(
                query_states,
                key_states,
                value_states,
                cu_seqlens_q=cu_seqlens_q,
                cu_seqlens_k=cu_seqlens_k,
                max_seqlen_q=max_seqlen_in_batch_q,
                max_seqlen_k=max_seqlen_in_batch_k,
                dropout_p=dropout,
                softmax_scale=softmax_scale,
                causal=True,
            )

            attn_output = pad_input(attn_output_unpad, indices_q, batch_size, query_length)
        else:
            attn_output = flash_attn_func(
                query_states, key_states, value_states, dropout, softmax_scale=softmax_scale, causal=True
            )

        return attn_output

    def _upad_input(self, query_layer, key_layer, value_layer, padding_mask, query_length):
        indices_k, cu_seqlens_k, max_seqlen_in_batch_k = _get_unpad_data(padding_mask)
        batch_size, kv_seq_len, num_key_value_heads, head_dim = key_layer.shape

        key_layer = index_first_axis(
            key_layer.reshape(batch_size * kv_seq_len, num_key_value_heads, head_dim), indices_k
        )
        value_layer = index_first_axis(
            value_layer.reshape(batch_size * kv_seq_len, num_key_value_heads, head_dim), indices_k
        )
        if query_length == kv_seq_len:
            query_layer = index_first_axis(
                query_layer.reshape(batch_size * kv_seq_len, self.num_heads, head_dim), indices_k
            )
            cu_seqlens_q = cu_seqlens_k
            max_seqlen_in_batch_q = max_seqlen_in_batch_k
            indices_q = indices_k
        elif query_length == 1:
            max_seqlen_in_batch_q = 1
            cu_seqlens_q = torch.arange(
                batch_size + 1, dtype=torch.int32, device=query_layer.device
            )  # There is a memcpy here, that is very bad.
            indices_q = cu_seqlens_q[:-1]
            query_layer = query_layer.squeeze(1)
        else:
            # The -q_len: slice assumes left padding.
            padding_mask = padding_mask[:, -query_length:]
            query_layer, indices_q, cu_seqlens_q, max_seqlen_in_batch_q = unpad_input(query_layer, padding_mask)

        return (
            query_layer,
            key_layer,
            value_layer,
            indices_q,
            (cu_seqlens_q, cu_seqlens_k),
            (max_seqlen_in_batch_q, max_seqlen_in_batch_k),
        )


class ZebraDecoderLayer(nn.Module):
    def __init__(self, config: ZebraConfig):
        super().__init__()
        self.hidden_size = config.hidden_size
        self.self_attn = (
            ZebraAttention(config=config)
            if not getattr(config, "_flash_attn_2_enabled", False)
            else ZebraFlashAttention2(config=config)
        )
        self.mlp = ZebraMLP(config)
        self.input_layernorm = nn.LayerNorm(config.hidden_size, eps=config.rms_norm_eps) #ZebraRMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.post_attention_layernorm = nn.LayerNorm(config.hidden_size, eps=config.rms_norm_eps) #ZebraRMSNorm(config.hidden_size, eps=config.rms_norm_eps)

    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_value: Optional[Tuple[torch.Tensor]] = None,
        output_attentions: Optional[bool] = False,
        use_cache: Optional[bool] = False,
        padding_mask: Optional[torch.LongTensor] = None,
    ) -> Tuple[torch.FloatTensor, Optional[Tuple[torch.FloatTensor, torch.FloatTensor]]]:
        """
        Args:
            hidden_states (`torch.FloatTensor`): input to the layer of shape `(batch, seq_len, embed_dim)`
            attention_mask (`torch.FloatTensor`, *optional*): attention mask of size
                `(batch, 1, tgt_len, src_len)` where padding elements are indicated by very large negative values.
            output_attentions (`bool`, *optional*):
                Whether or not to return the attentions tensors of all attention layers. See `attentions` under
                returned tensors for more detail.
            use_cache (`bool`, *optional*):
                If set to `True`, `past_key_values` key value states are returned and can be used to speed up decoding
                (see `past_key_values`).
            past_key_value (`Tuple(torch.FloatTensor)`, *optional*): cached past key and value projection states
        """

        residual = hidden_states

        hidden_states = self.input_layernorm(hidden_states)

        # Self Attention
        hidden_states, self_attn_weights, present_key_value = self.self_attn(
            hidden_states=hidden_states,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_value=past_key_value,
            output_attentions=output_attentions,
            use_cache=use_cache,
            padding_mask=padding_mask,
        )
        hidden_states = residual + hidden_states

        # Fully Connected
        residual = hidden_states
        hidden_states = self.post_attention_layernorm(hidden_states)
        hidden_states = self.mlp(hidden_states)
        hidden_states = residual + hidden_states

        outputs = (hidden_states,)

        if output_attentions:
            outputs += (self_attn_weights,)

        if use_cache:
            outputs += (present_key_value,)

        return outputs


def modulate(x, shift, scale):
    return x * (1 + scale) + shift

class ZebraDecoderLayer2(nn.Module):
    def __init__(self, config: ZebraConfig):
        super().__init__()
        self.hidden_size = config.hidden_size
        self.self_attn = (
            ZebraAttention(config=config)
            if not getattr(config, "_flash_attn_2_enabled", False)
            else ZebraFlashAttention2(config=config)
        )
        self.mlp = ZebraMLP(config)
        self.input_layernorm = nn.LayerNorm(config.hidden_size, eps=config.rms_norm_eps) #ZebraRMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.post_attention_layernorm = nn.LayerNorm(config.hidden_size, eps=config.rms_norm_eps) #ZebraRMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.gate_attn = nn.Sequential(nn.ReLU(), nn.Linear(config.hidden_size, config.hidden_size, bias=False))
        self.gate_mlp = nn.Sequential(nn.ReLU(), nn.Linear(config.hidden_size, config.hidden_size, bias=False)) 

        self.shift_mlp = nn.Sequential(nn.ReLU(), nn.Linear(config.hidden_size, config.hidden_size, bias=True))
        self.shift_attn = nn.Sequential(nn.ReLU(), nn.Linear(config.hidden_size, config.hidden_size, bias=True)) 

        self.scale_attn = nn.Sequential(nn.ReLU(), nn.Linear(config.hidden_size, config.hidden_size, bias=True))
        self.scale_mlp = nn.Sequential(nn.ReLU(), nn.Linear(config.hidden_size, config.hidden_size, bias=True))


        nn.init.constant_(self.gate_attn[1].weight, 0)
        #nn.init.constant_(self.gate_attn[1].bias, 0)

        nn.init.constant_(self.gate_mlp[1].weight, 0)
        #nn.init.constant_(self.gate_mlp[1].bias, 0)

        nn.init.constant_(self.shift_mlp[1].weight, 0)
        nn.init.constant_(self.shift_mlp[1].bias, 0)

        nn.init.constant_(self.shift_attn[1].weight, 0)
        nn.init.constant_(self.shift_attn[1].bias, 0)

        nn.init.constant_(self.scale_attn[1].weight, 0)
        nn.init.constant_(self.scale_attn[1].bias, 0)

        nn.init.constant_(self.scale_mlp[1].weight, 0)
        nn.init.constant_(self.scale_mlp[1].bias, 0)


    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_value: Optional[Tuple[torch.Tensor]] = None,
        output_attentions: Optional[bool] = False,
        use_cache: Optional[bool] = False,
        padding_mask: Optional[torch.LongTensor] = None,
    ) -> Tuple[torch.FloatTensor, Optional[Tuple[torch.FloatTensor, torch.FloatTensor]]]:
        """
        Args:
            hidden_states (`torch.FloatTensor`): input to the layer of shape `(batch, seq_len, embed_dim)`
            attention_mask (`torch.FloatTensor`, *optional*): attention mask of size
                `(batch, 1, tgt_len, src_len)` where padding elements are indicated by very large negative values.
            output_attentions (`bool`, *optional*):
                Whether or not to return the attentions tensors of all attention layers. See `attentions` under
                returned tensors for more detail.
            use_cache (`bool`, *optional*):
                If set to `True`, `past_key_values` key value states are returned and can be used to speed up decoding
                (see `past_key_values`).
            past_key_value (`Tuple(torch.FloatTensor)`, *optional*): cached past key and value projection states
        """

        residual = hidden_states

        alpha1 = self.scale_attn(residual)
        beta1 = self.shift_attn(residual)
        gamma1 = self.gate_attn(residual)

        alpha2 = self.scale_mlp(residual)
        beta2 = self.shift_mlp(residual)
        gamma2 = self.gate_mlp(residual)


        hidden_states = modulate(self.input_layernorm(hidden_states), beta1, alpha1)

        # Self Attention
        hidden_states, self_attn_weights, present_key_value = self.self_attn(
            hidden_states=hidden_states,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_value=past_key_value,
            output_attentions=output_attentions,
            use_cache=use_cache,
            padding_mask=padding_mask,
        )
        hidden_states = residual + gamma1 * hidden_states
        #print('add', hidden_states[..., -1, :].std()) 
        #hidden_states = self.input_layernorm(hidden_states)
        #print('layernorm1', hidden_states[..., -1, :].std()) 

        # Fully Connected
        residual = hidden_states
        hidden_states = modulate(self.post_attention_layernorm(hidden_states), beta2, alpha2)
        hidden_states = self.mlp(hidden_states)
        hidden_states = residual + gamma2 * hidden_states
        #print('mlp', hidden_states[..., -1, :].std()) 
        #hidden_states = self.post_attention_layernorm(hidden_states)
        #print('layernorm2', hidden_states[..., -1, :].std()) 

        outputs = (hidden_states,)

        if output_attentions:
            outputs += (self_attn_weights,)

        if use_cache:
            outputs += (present_key_value,)

        return outputs


class ZebraDecoderLayer3(nn.Module):
    def __init__(self, config: ZebraConfig):
        super().__init__()
        self.hidden_size = config.hidden_size
        self.self_attn = (
            ZebraAttention(config=config)
            if not getattr(config, "_flash_attn_2_enabled", False)
            else ZebraFlashAttention2(config=config)
        )
        self.mlp = ZebraMLP(config)
        self.input_layernorm = nn.LayerNorm(config.hidden_size, eps=config.rms_norm_eps) #ZebraRMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.post_attention_layernorm = nn.LayerNorm(config.hidden_size, eps=config.rms_norm_eps) #ZebraRMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.gate_attn = nn.Sequential(nn.ReLU(), nn.Linear(config.hidden_size, config.hidden_size, bias=False))
        self.gate_mlp = nn.Sequential(nn.ReLU(), nn.Linear(config.hidden_size, config.hidden_size, bias=False)) 

        self.shift_mlp = nn.Sequential(nn.ReLU(), nn.Linear(config.hidden_size, config.hidden_size, bias=True))
        self.shift_attn = nn.Sequential(nn.ReLU(), nn.Linear(config.hidden_size, config.hidden_size, bias=True)) 

        self.scale_attn = nn.Sequential(nn.ReLU(), nn.Linear(config.hidden_size, config.hidden_size, bias=True))
        self.scale_mlp = nn.Sequential(nn.ReLU(), nn.Linear(config.hidden_size, config.hidden_size, bias=True))


        nn.init.constant_(self.gate_attn[1].weight, 0)
        #nn.init.constant_(self.gate_attn[1].bias, 0)

        nn.init.constant_(self.gate_mlp[1].weight, 0)
        #nn.init.constant_(self.gate_mlp[1].bias, 0)

        nn.init.constant_(self.shift_mlp[1].weight, 0)
        nn.init.constant_(self.shift_mlp[1].bias, 0)

        nn.init.constant_(self.shift_attn[1].weight, 0)
        nn.init.constant_(self.shift_attn[1].bias, 0)

        nn.init.constant_(self.scale_attn[1].weight, 0)
        nn.init.constant_(self.scale_attn[1].bias, 0)

        nn.init.constant_(self.scale_mlp[1].weight, 0)
        nn.init.constant_(self.scale_mlp[1].bias, 0)


    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_value: Optional[Tuple[torch.Tensor]] = None,
        output_attentions: Optional[bool] = False,
        use_cache: Optional[bool] = False,
        padding_mask: Optional[torch.LongTensor] = None,
        conditioning: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.FloatTensor, Optional[Tuple[torch.FloatTensor, torch.FloatTensor]]]:
        """
        Args:
            hidden_states (`torch.FloatTensor`): input to the layer of shape `(batch, seq_len, embed_dim)`
            attention_mask (`torch.FloatTensor`, *optional*): attention mask of size
                `(batch, 1, tgt_len, src_len)` where padding elements are indicated by very large negative values.
            output_attentions (`bool`, *optional*):
                Whether or not to return the attentions tensors of all attention layers. See `attentions` under
                returned tensors for more detail.
            use_cache (`bool`, *optional*):
                If set to `True`, `past_key_values` key value states are returned and can be used to speed up decoding
                (see `past_key_values`).
            past_key_value (`Tuple(torch.FloatTensor)`, *optional*): cached past key and value projection states
        """

        residual = hidden_states

        alpha1 = self.scale_attn(conditioning)
        beta1 = self.shift_attn(conditioning)
        gamma1 = self.gate_attn(conditioning)

        alpha2 = self.scale_mlp(conditioning)
        beta2 = self.shift_mlp(conditioning)
        gamma2 = self.gate_mlp(conditioning)


        hidden_states = modulate(self.input_layernorm(hidden_states), beta1, alpha1)

        # Self Attention
        hidden_states, self_attn_weights, present_key_value = self.self_attn(
            hidden_states=hidden_states,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_value=past_key_value,
            output_attentions=output_attentions,
            use_cache=use_cache,
            padding_mask=padding_mask,
        )
        hidden_states = residual + gamma1 * hidden_states
        #print('add', hidden_states[..., -1, :].std()) 
        #hidden_states = self.input_layernorm(hidden_states)
        #print('layernorm1', hidden_states[..., -1, :].std()) 

        # Fully Connected
        residual = hidden_states
        hidden_states = modulate(self.post_attention_layernorm(hidden_states), beta2, alpha2)
        hidden_states = self.mlp(hidden_states)
        hidden_states = residual + gamma2 * hidden_states
        #print('mlp', hidden_states[..., -1, :].std()) 
        #hidden_states = self.post_attention_layernorm(hidden_states)
        #print('layernorm2', hidden_states[..., -1, :].std()) 

        outputs = (hidden_states,)

        if output_attentions:
            outputs += (self_attn_weights,)

        if use_cache:
            outputs += (present_key_value,)

        return outputs
    




class ZebraDecoderLayer4(nn.Module):
    def __init__(self, config: ZebraConfig):
        super().__init__()
        self.hidden_size = config.hidden_size
        self.self_attn = (
            ZebraAttention(config=config)
            if not getattr(config, "_flash_attn_2_enabled", False)
            else ZebraFlashAttention2(config=config)
        )
        self.mlp = ZebraMLP(config)
        self.input_layernorm = nn.LayerNorm(config.hidden_size, eps=config.rms_norm_eps) #ZebraRMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.post_attention_layernorm = nn.LayerNorm(config.hidden_size, eps=config.rms_norm_eps) #ZebraRMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.gate_attn = nn.Sequential(nn.ReLU(), nn.Linear(config.hidden_size, config.hidden_size, bias=False))
        self.gate_mlp = nn.Sequential(nn.ReLU(), nn.Linear(config.hidden_size, config.hidden_size, bias=False)) 

        self.shift_mlp = nn.Sequential(nn.ReLU(), nn.Linear(config.hidden_size, config.hidden_size, bias=True))
        self.shift_attn = nn.Sequential(nn.ReLU(), nn.Linear(config.hidden_size, config.hidden_size, bias=True)) 

        self.scale_attn = nn.Sequential(nn.ReLU(), nn.Linear(config.hidden_size, config.hidden_size, bias=True))
        self.scale_mlp = nn.Sequential(nn.ReLU(), nn.Linear(config.hidden_size, config.hidden_size, bias=True))


        nn.init.constant_(self.gate_attn[1].weight, 0)
        #nn.init.constant_(self.gate_attn[1].bias, 0)

        nn.init.constant_(self.gate_mlp[1].weight, 0)
        #nn.init.constant_(self.gate_mlp[1].bias, 0)

        nn.init.constant_(self.shift_mlp[1].weight, 0)
        nn.init.constant_(self.shift_mlp[1].bias, 0)

        nn.init.constant_(self.shift_attn[1].weight, 0)
        nn.init.constant_(self.shift_attn[1].bias, 0)

        nn.init.constant_(self.scale_attn[1].weight, 0)
        nn.init.constant_(self.scale_attn[1].bias, 0)

        nn.init.constant_(self.scale_mlp[1].weight, 0)
        nn.init.constant_(self.scale_mlp[1].bias, 0)


    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_value: Optional[Tuple[torch.Tensor]] = None,
        output_attentions: Optional[bool] = False,
        use_cache: Optional[bool] = False,
        padding_mask: Optional[torch.LongTensor] = None,
        conditioning: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.FloatTensor, Optional[Tuple[torch.FloatTensor, torch.FloatTensor]]]:
        """
        Args:
            hidden_states (`torch.FloatTensor`): input to the layer of shape `(batch, seq_len, embed_dim)`
            attention_mask (`torch.FloatTensor`, *optional*): attention mask of size
                `(batch, 1, tgt_len, src_len)` where padding elements are indicated by very large negative values.
            output_attentions (`bool`, *optional*):
                Whether or not to return the attentions tensors of all attention layers. See `attentions` under
                returned tensors for more detail.
            use_cache (`bool`, *optional*):
                If set to `True`, `past_key_values` key value states are returned and can be used to speed up decoding
                (see `past_key_values`).
            past_key_value (`Tuple(torch.FloatTensor)`, *optional*): cached past key and value projection states
        """

        residual = hidden_states

        alpha1 = self.scale_attn(conditioning)
        beta1 = self.shift_attn(conditioning)
        gamma1 = self.gate_attn(conditioning)

        alpha2 = self.scale_mlp(conditioning)
        beta2 = self.shift_mlp(conditioning)
        gamma2 = self.gate_mlp(conditioning)


        hidden_states = modulate(self.input_layernorm(hidden_states), beta1, alpha1)

        # Self Attention
        hidden_states, self_attn_weights, present_key_value = self.self_attn(
            hidden_states=hidden_states,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_value=past_key_value,
            output_attentions=output_attentions,
            use_cache=use_cache,
            padding_mask=padding_mask,
        )
        hidden_states = gamma1 * (residual + hidden_states)

        # Fully Connected
        residual = hidden_states
        hidden_states = modulate(self.post_attention_layernorm(hidden_states), beta2, alpha2)
        hidden_states = self.mlp(hidden_states)
        hidden_states = gamma2 *(residual + hidden_states)
        #print('mlp', hidden_states[..., -1, :].std()) 
        #hidden_states = self.post_attention_layernorm(hidden_states)
        #print('layernorm2', hidden_states[..., -1, :].std()) 

        outputs = (hidden_states,)

        if output_attentions:
            outputs += (self_attn_weights,)

        if use_cache:
            outputs += (present_key_value,)

        return outputs



class ZebraDecoderLayer5(nn.Module):
    def __init__(self, config: ZebraConfig):
        super().__init__()
        self.hidden_size = config.hidden_size
        self.self_attn = (
            ZebraAttention(config=config)
            if not getattr(config, "_flash_attn_2_enabled", False)
            else ZebraFlashAttention2(config=config)
        )
        self.mlp = ZebraMLP(config)
        self.input_layernorm = nn.LayerNorm(config.hidden_size, eps=config.rms_norm_eps) #ZebraRMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.post_attention_layernorm = nn.LayerNorm(config.hidden_size, eps=config.rms_norm_eps) #ZebraRMSNorm(config.hidden_size, eps=config.rms_norm_eps)

    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_value: Optional[Tuple[torch.Tensor]] = None,
        output_attentions: Optional[bool] = False,
        use_cache: Optional[bool] = False,
        padding_mask: Optional[torch.LongTensor] = None,
    ) -> Tuple[torch.FloatTensor, Optional[Tuple[torch.FloatTensor, torch.FloatTensor]]]:
        """
        Args:
            hidden_states (`torch.FloatTensor`): input to the layer of shape `(batch, seq_len, embed_dim)`
            attention_mask (`torch.FloatTensor`, *optional*): attention mask of size
                `(batch, 1, tgt_len, src_len)` where padding elements are indicated by very large negative values.
            output_attentions (`bool`, *optional*):
                Whether or not to return the attentions tensors of all attention layers. See `attentions` under
                returned tensors for more detail.
            use_cache (`bool`, *optional*):
                If set to `True`, `past_key_values` key value states are returned and can be used to speed up decoding
                (see `past_key_values`).
            past_key_value (`Tuple(torch.FloatTensor)`, *optional*): cached past key and value projection states
        """

        residual = hidden_states

        #hidden_states = self.input_layernorm(hidden_states)

        # Self Attention
        hidden_states, self_attn_weights, present_key_value = self.self_attn(
            hidden_states=hidden_states,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_value=past_key_value,
            output_attentions=output_attentions,
            use_cache=use_cache,
            padding_mask=padding_mask,
        )
        hidden_states = residual + hidden_states

        # Fully Connected
        residual = hidden_states
        #hidden_states = self.post_attention_layernorm(hidden_states)
        hidden_states = self.mlp(hidden_states)
        hidden_states = residual + hidden_states

        outputs = (hidden_states,)

        if output_attentions:
            outputs += (self_attn_weights,)

        if use_cache:
            outputs += (present_key_value,)

        return outputs




ZEBRA_START_DOCSTRING = r"""
    This model inherits from [`PreTrainedModel`]. Check the superclass documentation for the generic methods the
    library implements for all its model (such as downloading or saving, resizing the input embeddings, pruning heads
    etc.)

    This model is also a PyTorch [torch.nn.Module](https://pytorch.org/docs/stable/nn.html#torch.nn.Module) subclass.
    Use it as a regular PyTorch Module and refer to the PyTorch documentation for all matter related to general usage
    and behavior.

    Parameters:
        config ([`ZebraConfig`]):
            Model configuration class with all the parameters of the model. Initializing with a config file does not
            load the weights associated with the model, only the configuration. Check out the
            [`~PreTrainedModel.from_pretrained`] method to load the model weights.
"""


@add_start_docstrings(
    "The bare LLaMA Model outputting raw hidden-states without any specific head on top.",
    ZEBRA_START_DOCSTRING,
)
class ZebraPreTrainedModel(PreTrainedModel):
    config_class = ZebraConfig
    base_model_prefix = "model"
    supports_gradient_checkpointing = True
    _no_split_modules = ["ZebraDecoderLayer"]
    _skip_keys_device_placement = "past_key_values"
    _supports_flash_attn_2 = True

    def _init_weights(self, module):
        std = self.config.initializer_range
        if isinstance(module, nn.Linear):
            module.weight.data.normal_(mean=0.0, std=std)
            if module.bias is not None:
                module.bias.data.zero_()
        elif isinstance(module, nn.Embedding):
            module.weight.data.normal_(mean=0.0, std=std)
            if module.padding_idx is not None:
                module.weight.data[module.padding_idx].zero_()

    def _set_gradient_checkpointing(self, module, value=False):
        if isinstance(module, ZebraModel):
            module.gradient_checkpointing = value


ZEBRA_INPUTS_DOCSTRING = r"""
    Args:
        input_ids (`torch.LongTensor` of shape `(batch_size, sequence_length)`):
            Indices of input sequence tokens in the vocabulary. Padding will be ignored by default should you provide
            it.

            Indices can be obtained using [`AutoTokenizer`]. See [`PreTrainedTokenizer.encode`] and
            [`PreTrainedTokenizer.__call__`] for details.

            [What are input IDs?](../glossary#input-ids)
        attention_mask (`torch.Tensor` of shape `(batch_size, sequence_length)`, *optional*):
            Mask to avoid performing attention on padding token indices. Mask values selected in `[0, 1]`:

            - 1 for tokens that are **not masked**,
            - 0 for tokens that are **masked**.

            [What are attention masks?](../glossary#attention-mask)

            Indices can be obtained using [`AutoTokenizer`]. See [`PreTrainedTokenizer.encode`] and
            [`PreTrainedTokenizer.__call__`] for details.

            If `past_key_values` is used, optionally only the last `input_ids` have to be input (see
            `past_key_values`).

            If you want to change padding behavior, you should read [`modeling_opt._prepare_decoder_attention_mask`]
            and modify to your needs. See diagram 1 in [the paper](https://arxiv.org/abs/1910.13461) for more
            information on the default strategy.

            - 1 indicates the head is **not masked**,
            - 0 indicates the head is **masked**.
        position_ids (`torch.LongTensor` of shape `(batch_size, sequence_length)`, *optional*):
            Indices of positions of each input sequence tokens in the position embeddings. Selected in the range `[0,
            config.n_positions - 1]`.

            [What are position IDs?](../glossary#position-ids)
        past_key_values (`tuple(tuple(torch.FloatTensor))`, *optional*, returned when `use_cache=True` is passed or when `config.use_cache=True`):
            Tuple of `tuple(torch.FloatTensor)` of length `config.n_layers`, with each tuple having 2 tensors of shape
            `(batch_size, num_heads, sequence_length, embed_size_per_head)`) and 2 additional tensors of shape
            `(batch_size, num_heads, encoder_sequence_length, embed_size_per_head)`.

            Contains pre-computed hidden-states (key and values in the self-attention blocks and in the cross-attention
            blocks) that can be used (see `past_key_values` input) to speed up sequential decoding.

            If `past_key_values` are used, the user can optionally input only the last `input_ids` (those that don't
            have their past key value states given to this model) of shape `(batch_size, 1)` instead of all `input_ids`
            of shape `(batch_size, sequence_length)`.
        inputs_embeds (`torch.FloatTensor` of shape `(batch_size, sequence_length, hidden_size)`, *optional*):
            Optionally, instead of passing `input_ids` you can choose to directly pass an embedded representation. This
            is useful if you want more control over how to convert `input_ids` indices into associated vectors than the
            model's internal embedding lookup matrix.
        use_cache (`bool`, *optional*):
            If set to `True`, `past_key_values` key value states are returned and can be used to speed up decoding (see
            `past_key_values`).
        output_attentions (`bool`, *optional*):
            Whether or not to return the attentions tensors of all attention layers. See `attentions` under returned
            tensors for more detail.
        output_hidden_states (`bool`, *optional*):
            Whether or not to return the hidden states of all layers. See `hidden_states` under returned tensors for
            more detail.
        return_dict (`bool`, *optional*):
            Whether or not to return a [`~utils.ModelOutput`] instead of a plain tuple.
"""


@add_start_docstrings(
    "The bare LLaMA Model outputting raw hidden-states without any specific head on top.",
    ZEBRA_START_DOCSTRING,
)
class ZebraModel(ZebraPreTrainedModel):
    """
    Transformer decoder consisting of *config.num_hidden_layers* layers. Each layer is a [`ZebraDecoderLayer`]

    Args:
        config: ZebraConfig
    """

    def __init__(self, config: ZebraConfig):
        super().__init__(config)
        self.padding_idx = config.pad_token_id
        self.vocab_size = config.vocab_size

        self.lift_mu = nn.Linear(1, config.hidden_size)
        self.lift_sigma = nn.Sequential(nn.Linear(1, config.hidden_size),
                                        nn.ReLU(),
                                        nn.Linear(config.hidden_size, config.hidden_size))

        self.embed_tokens = nn.Embedding(config.vocab_size, config.hidden_size, self.padding_idx)
        self.layers = nn.ModuleList([ZebraDecoderLayer(config) for _ in range(config.num_hidden_layers)])
        self.norm = nn.LayerNorm(config.hidden_size, eps=config.rms_norm_eps) #ZebraRMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.bidirectional = config.bidirectional
        self.gradient_checkpointing = False
        # Initialize weights and apply final processing
        self.post_init()

    def get_input_embeddings(self):
        return self.embed_tokens

    def set_input_embeddings(self, value):
        self.embed_tokens = value

    def _prepare_decoder_attention_mask(self, attention_mask, input_shape, inputs_embeds, past_key_values_length):
        if self.bidirectional:
            # For bidirectional models, use a different mask that doesn't enforce causality
            expanded_attn_mask = _expand_mask(attention_mask, inputs_embeds.dtype, tgt_len=input_shape[-1]).to(inputs_embeds.device)
            return expanded_attn_mask
        else:
            # For autoregressive models, use the causal mask
            combined_attention_mask = None
            if input_shape[-1] > 1:
                combined_attention_mask = _make_causal_mask(
                    input_shape,
                    inputs_embeds.dtype,
                    device=inputs_embeds.device,
                    past_key_values_length=past_key_values_length,
                )

            if attention_mask is not None:
                expanded_attn_mask = _expand_mask(attention_mask, inputs_embeds.dtype, tgt_len=input_shape[-1]).to(inputs_embeds.device)
                combined_attention_mask = (
                    expanded_attn_mask if combined_attention_mask is None else expanded_attn_mask + combined_attention_mask
                )
            return combined_attention_mask
    # # Copied from transformers.models.bart.modeling_bart.BartDecoder._prepare_decoder_attention_mask
    # def _prepare_decoder_attention_mask(self, attention_mask, input_shape, inputs_embeds, past_key_values_length):
    #     # create causal mask
    #     # [bsz, seq_len] -> [bsz, 1, tgt_seq_len, src_seq_len]
    #     combined_attention_mask = None
    #     if input_shape[-1] > 1:
    #         combined_attention_mask = _make_causal_mask(
    #             input_shape,
    #             inputs_embeds.dtype,
    #             device=inputs_embeds.device,
    #             past_key_values_length=past_key_values_length,
    #         )

    #     if attention_mask is not None:
    #         # [bsz, seq_len] -> [bsz, 1, tgt_seq_len, src_seq_len]
    #         expanded_attn_mask = _expand_mask(attention_mask, inputs_embeds.dtype, tgt_len=input_shape[-1]).to(
    #             inputs_embeds.device
    #         )
    #         combined_attention_mask = (
    #             expanded_attn_mask if combined_attention_mask is None else expanded_attn_mask + combined_attention_mask
    #         )
    #     return combined_attention_mask

    @add_start_docstrings_to_model_forward(ZEBRA_INPUTS_DOCSTRING)
    def forward(
        self,
        input_ids: torch.LongTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[List[torch.FloatTensor]] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        mu: Optional[torch.Tensor] = None,
        sigma: Optional[torch.Tensor] = None,
    ) -> Union[Tuple, BaseModelOutputWithPast]:
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        use_cache = use_cache if use_cache is not None else self.config.use_cache

        return_dict = return_dict if return_dict is not None else self.config.use_return_dict


        if mu is not None:
            mu_embedding = self.lift_mu(mu[..., None])

        if sigma is not None:
            sigma_embedding = self.lift_sigma(sigma[..., None])

        # retrieve input_ids and inputs_embeds
        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is not None:
            batch_size, seq_length = input_ids.shape
        elif inputs_embeds is not None:
            batch_size, seq_length, _ = inputs_embeds.shape
        else:
            raise ValueError("You have to specify either input_ids or inputs_embeds")

        seq_length_with_past = seq_length
        past_key_values_length = 0

        if past_key_values is not None:
            past_key_values_length = past_key_values[0][0].shape[2]
            seq_length_with_past = seq_length_with_past + past_key_values_length

        if position_ids is None:
            device = input_ids.device if input_ids is not None else inputs_embeds.device
            position_ids = torch.arange(
                past_key_values_length, seq_length + 2 + past_key_values_length, dtype=torch.long, device=device
            )
            position_ids = position_ids.unsqueeze(0).view(-1, seq_length + 2)
        else:
            position_ids = position_ids.view(-1, seq_length + 2).long()

        if inputs_embeds is None:
            inputs_embeds = self.embed_tokens(input_ids)
        # embed positions
        if attention_mask is None:
            attention_mask = _make_causal_mask(torch.Size((batch_size, seq_length + 2)), 
                                               dtype=torch.float32,
                                               device=inputs_embeds.device,
                                               past_key_values_length=past_key_values_length)
            #torch.ones(
            #    (batch_size, seq_length_with_past), dtype=torch.bool, device=inputs_embeds.device
            #)
            padding_mask = None
        else:
            if 0 in attention_mask:
                padding_mask = attention_mask
            else:
                padding_mask = None

        
        if mu is not None:
            inputs_embeds = torch.cat([mu_embedding, inputs_embeds], axis=1)
            #attention_mask = torch.cat([torch.ones(batch_size, 1).bool().to(attention_mask), attention_mask], axis=-1)
            seq_length +=1

        if sigma is not None:
            inputs_embeds = torch.cat([sigma_embedding, inputs_embeds], axis=1)
            #attention_mask = torch.cat([torch.ones(batch_size, 1).bool().to(attention_mask), attention_mask], axis=-1)
            seq_length +=1

        # ask Pierre about this
            
        #attention_mask = self._prepare_decoder_attention_mask(
        #    attention_mask, (batch_size, seq_length), inputs_embeds, past_key_values_length
        #)

        hidden_states = inputs_embeds

        if self.gradient_checkpointing and self.training:
            if use_cache:
                logger.warning_once(
                    "`use_cache=True` is incompatible with gradient checkpointing. Setting `use_cache=False`..."
                )
                use_cache = False

        # decoder layers
        all_hidden_states = () if output_hidden_states else None
        all_self_attns = () if output_attentions else None
        next_decoder_cache = () if use_cache else None

        for idx, decoder_layer in enumerate(self.layers):
            if output_hidden_states:
                all_hidden_states += (hidden_states,)

            past_key_value = past_key_values[idx] if past_key_values is not None else None

            if self.gradient_checkpointing and self.training:

                def create_custom_forward(module):
                    def custom_forward(*inputs):
                        # None for past_key_value
                        return module(*inputs, past_key_value, output_attentions, padding_mask=padding_mask)

                    return custom_forward

                layer_outputs = torch.utils.checkpoint.checkpoint(
                    create_custom_forward(decoder_layer), hidden_states, attention_mask, position_ids
                )
            else:
                layer_outputs = decoder_layer(
                    hidden_states,
                    attention_mask=attention_mask,
                    position_ids=position_ids,
                    past_key_value=past_key_value,
                    output_attentions=output_attentions,
                    use_cache=use_cache,
                    padding_mask=padding_mask,
                )

            hidden_states = layer_outputs[0]

            if use_cache:
                next_decoder_cache += (layer_outputs[2 if output_attentions else 1],)

            if output_attentions:
                all_self_attns += (layer_outputs[1],)

        hidden_states = self.norm(hidden_states)

        # add hidden states from the last decoder layer
        if output_hidden_states:
            all_hidden_states += (hidden_states,)

        next_cache = next_decoder_cache if use_cache else None
        if not return_dict:
            return tuple(v for v in [hidden_states, next_cache, all_hidden_states, all_self_attns] if v is not None)
        return BaseModelOutputWithPast(
            last_hidden_state=hidden_states,
            past_key_values=next_cache,
            hidden_states=all_hidden_states,
            attentions=all_self_attns,
        )


class ZebraForCausalLM(ZebraPreTrainedModel):
    _tied_weights_keys = ["lm_head.weight"]

    def __init__(self, config):
        super().__init__(config)
        self.model = conditioningZebraModel(config)
        self.vocab_size = config.vocab_size
        self.lm_head = nn.Linear(config.hidden_size, config.vocab_size, bias=False)

        # Initialize weights and apply final processing
        self.post_init()

    def get_input_embeddings(self):
        return self.model.embed_tokens

    def set_input_embeddings(self, value):
        self.model.embed_tokens = value

    def get_output_embeddings(self):
        return self.lm_head

    def set_output_embeddings(self, new_embeddings):
        self.lm_head = new_embeddings

    def set_decoder(self, decoder):
        self.model = decoder

    def get_decoder(self):
        return self.model

    @add_start_docstrings_to_model_forward(ZEBRA_INPUTS_DOCSTRING)
    @replace_return_docstrings(output_type=CausalLMOutputWithPast, config_class=_CONFIG_FOR_DOC)
    def forward(
        self,
        input_ids: torch.LongTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[List[torch.FloatTensor]] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        labels: Optional[torch.LongTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        mu: Optional[torch.Tensor] = None,
        sigma: Optional[torch.Tensor] = None,
    ) -> Union[Tuple, CausalLMOutputWithPast]:
        r"""
        Args:
            labels (`torch.LongTensor` of shape `(batch_size, sequence_length)`, *optional*):
                Labels for computing the masked language modeling loss. Indices should either be in `[0, ...,
                config.vocab_size]` or -100 (see `input_ids` docstring). Tokens with indices set to `-100` are ignored
                (masked), the loss is only computed for the tokens with labels in `[0, ..., config.vocab_size]`.

        Returns:

        Example:

        ```python
        >>> from transformers import AutoTokenizer, ZebraForCausalLM

        >>> model = ZebraForCausalLM.from_pretrained(PATH_TO_CONVERTED_WEIGHTS)
        >>> tokenizer = AutoTokenizer.from_pretrained(PATH_TO_CONVERTED_TOKENIZER)

        >>> prompt = "Hey, are you conscious? Can you talk to me?"
        >>> inputs = tokenizer(prompt, return_tensors="pt")

        >>> # Generate
        >>> generate_ids = model.generate(inputs.input_ids, max_length=30)
        >>> tokenizer.batch_decode(generate_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)[0]
        "Hey, are you conscious? Can you talk to me?\nI'm not conscious, but I can talk to you."
        ```"""

        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        #print('mu', mu.shape, sigma.shape, input_ids.shape)

        # decoder outputs consists of (dec_features, layer_state, dec_hidden, dec_attn)
        outputs = self.model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=past_key_values,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            mu = mu,
            sigma = sigma
        )

        hidden_states = outputs[0]
        if mu is not None:
            hidden_states = hidden_states[:, 1:, : ] # we remove the mu token
        if sigma is not None:
            hidden_states = hidden_states[:, 1:, : ] # we remove the sigma token

        if self.config.pretraining_tp > 1:
            lm_head_slices = self.lm_head.weight.split(self.vocab_size // self.config.pretraining_tp, dim=0)
            logits = [F.linear(hidden_states, lm_head_slices[i]) for i in range(self.config.pretraining_tp)]
            logits = torch.cat(logits, dim=-1)
        else:
            logits = self.lm_head(hidden_states)
        logits = logits.float()

        loss = None
        if labels is not None:
            # Shift so that tokens < n predict n
            shift_logits = logits[..., :-1, :].contiguous()
            shift_labels = labels[..., 1:].contiguous()
            # Flatten the tokens
            loss_fct = CrossEntropyLoss()
            shift_logits = shift_logits.view(-1, self.config.vocab_size)
            shift_labels = shift_labels.view(-1)
            # Enable model parallelism
            shift_labels = shift_labels.to(shift_logits.device)
            loss = loss_fct(shift_logits, shift_labels)

        if not return_dict:
            output = (logits,) + outputs[1:]
            return (loss,) + output if loss is not None else output

        return CausalLMOutputWithPast(
            loss=loss,
            logits=logits,
            past_key_values=outputs.past_key_values,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )

    def prepare_inputs_for_generation(
        self, input_ids, past_key_values=None, attention_mask=None, inputs_embeds=None, **kwargs
    ):
        if past_key_values:
            input_ids = input_ids[:, -1:]

        position_ids = kwargs.get("position_ids", None)
        if attention_mask is not None and position_ids is None:
            # create position_ids on the fly for batch generation
            position_ids = attention_mask.long().cumsum(-1) - 1
            position_ids.masked_fill_(attention_mask == 0, 1)
            if past_key_values:
                position_ids = position_ids[:, -1].unsqueeze(-1)

        # if `inputs_embeds` are passed, we only want to use them in the 1st generation step
        if inputs_embeds is not None and past_key_values is None:
            model_inputs = {"inputs_embeds": inputs_embeds}
        else:
            model_inputs = {"input_ids": input_ids}

        model_inputs.update(
            {
                "position_ids": position_ids,
                "past_key_values": past_key_values,
                "use_cache": kwargs.get("use_cache"),
                "attention_mask": attention_mask,
            }
        )
        return model_inputs

    @staticmethod
    def _reorder_cache(past_key_values, beam_idx):
        reordered_past = ()
        for layer_past in past_key_values:
            reordered_past += (
                tuple(past_state.index_select(0, beam_idx.to(past_state.device)) for past_state in layer_past),
            )
        return reordered_past




class ZebraForMaskedLM(ZebraPreTrainedModel):
    _tied_weights_keys = ["lm_head.weight"]

    def __init__(self, config):
        super().__init__(config)
        self.model = ZebraModel(config)
        self.vocab_size = config.vocab_size
        self.lm_head = nn.Linear(config.hidden_size, config.vocab_size, bias=False)

        # Initialize weights and apply final processing
        self.post_init()

    def get_input_embeddings(self):
        return self.model.embed_tokens

    def set_input_embeddings(self, value):
        self.model.embed_tokens = value

    def get_output_embeddings(self):
        return self.lm_head

    def set_output_embeddings(self, new_embeddings):
        self.lm_head = new_embeddings

    def set_decoder(self, decoder):
        self.model = decoder

    def get_decoder(self):
        return self.model

    @add_start_docstrings_to_model_forward(ZEBRA_INPUTS_DOCSTRING)
    @replace_return_docstrings(output_type=CausalLMOutputWithPast, config_class=_CONFIG_FOR_DOC)
    def forward(
        self,
        input_ids: torch.LongTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[List[torch.FloatTensor]] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        labels: Optional[torch.LongTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple, CausalLMOutputWithPast]:
        r"""
        Args:
            labels (`torch.LongTensor` of shape `(batch_size, sequence_length)`, *optional*):
                Labels for computing the masked language modeling loss. Indices should either be in `[0, ...,
                config.vocab_size]` or -100 (see `input_ids` docstring). Tokens with indices set to `-100` are ignored
                (masked), the loss is only computed for the tokens with labels in `[0, ..., config.vocab_size]`.

        Returns:

        Example:

        ```python
        >>> from transformers import AutoTokenizer, ZebraForCausalLM

        >>> model = ZebraForCausalLM.from_pretrained(PATH_TO_CONVERTED_WEIGHTS)
        >>> tokenizer = AutoTokenizer.from_pretrained(PATH_TO_CONVERTED_TOKENIZER)

        >>> prompt = "Hey, are you conscious? Can you talk to me?"
        >>> inputs = tokenizer(prompt, return_tensors="pt")

        >>> # Generate
        >>> generate_ids = model.generate(inputs.input_ids, max_length=30)
        >>> tokenizer.batch_decode(generate_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)[0]
        "Hey, are you conscious? Can you talk to me?\nI'm not conscious, but I can talk to you."
        ```"""

        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        # decoder outputs consists of (dec_features, layer_state, dec_hidden, dec_attn)
        outputs = self.model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=past_key_values,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            
        )

        hidden_states = outputs[0]
        if self.config.pretraining_tp > 1:
            lm_head_slices = self.lm_head.weight.split(self.vocab_size // self.config.pretraining_tp, dim=0)
            logits = [F.linear(hidden_states, lm_head_slices[i]) for i in range(self.config.pretraining_tp)]
            logits = torch.cat(logits, dim=-1)
        else:
            logits = self.lm_head(hidden_states)
        logits = logits.float()

        loss = None

        masked_lm_loss = None
        if labels is not None:
            loss_fct = CrossEntropyLoss()  # -100 index = padding token
            masked_lm_loss = loss_fct(logits.view(-1, self.config.vocab_size), labels.view(-1))

        if not return_dict:
            output = (logits,) + outputs[2:]
            return ((masked_lm_loss,) + output) if masked_lm_loss is not None else output

        return MaskedLMOutput(
            loss=masked_lm_loss,
            logits=logits,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )
        # if labels is not None:
        #     # Shift so that tokens < n predict n
        #     shift_logits = logits[..., :-1, :].contiguous()
        #     shift_labels = labels[..., 1:].contiguous()
        #     # Flatten the tokens
        #     loss_fct = CrossEntropyLoss()
        #     shift_logits = shift_logits.view(-1, self.config.vocab_size)
        #     shift_labels = shift_labels.view(-1)
        #     # Enable model parallelism
        #     shift_labels = shift_labels.to(shift_logits.device)
        #     loss = loss_fct(shift_logits, shift_labels)

        # if not return_dict:
        #     output = (logits,) + outputs[1:]
        #     return (loss,) + output if loss is not None else output

        # return CausalLMOutputWithPast(
        #     loss=loss,
        #     logits=logits,
        #     past_key_values=outputs.past_key_values,
        #     hidden_states=outputs.hidden_states,
        #     attentions=outputs.attentions,
        # )

    def prepare_inputs_for_generation(
        self, input_ids, past_key_values=None, attention_mask=None, inputs_embeds=None, **kwargs
    ):
        if past_key_values:
            input_ids = input_ids[:, -1:]

        position_ids = kwargs.get("position_ids", None)
        if attention_mask is not None and position_ids is None:
            # create position_ids on the fly for batch generation
            position_ids = attention_mask.long().cumsum(-1) - 1
            position_ids.masked_fill_(attention_mask == 0, 1)
            if past_key_values:
                position_ids = position_ids[:, -1].unsqueeze(-1)

        # if `inputs_embeds` are passed, we only want to use them in the 1st generation step
        if inputs_embeds is not None and past_key_values is None:
            model_inputs = {"inputs_embeds": inputs_embeds}
        else:
            model_inputs = {"input_ids": input_ids}

        model_inputs.update(
            {
                "position_ids": position_ids,
                "past_key_values": past_key_values,
                "use_cache": kwargs.get("use_cache"),
                "attention_mask": attention_mask,
            }
        )
        return model_inputs

    @staticmethod
    def _reorder_cache(past_key_values, beam_idx):
        reordered_past = ()
        for layer_past in past_key_values:
            reordered_past += (
                tuple(past_state.index_select(0, beam_idx.to(past_state.device)) for past_state in layer_past),
            )
        return reordered_past


@add_start_docstrings(
    """
    The LLaMa Model transformer with a sequence classification head on top (linear layer).

    [`ZebraForSequenceClassification`] uses the last token in order to do the classification, as other causal models
    (e.g. GPT-2) do.

    Since it does classification on the last token, it requires to know the position of the last token. If a
    `pad_token_id` is defined in the configuration, it finds the last token that is not a padding token in each row. If
    no `pad_token_id` is defined, it simply takes the last value in each row of the batch. Since it cannot guess the
    padding tokens when `inputs_embeds` are passed instead of `input_ids`, it does the same (take the last value in
    each row of the batch).
    """,
    ZEBRA_START_DOCSTRING,
)
class ZebraForSequenceClassification(ZebraPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        self.num_labels = config.num_labels
        self.model = ZebraModel(config)
        self.score = nn.Linear(config.hidden_size, self.num_labels, bias=False)

        # Initialize weights and apply final processing
        self.post_init()

    def get_input_embeddings(self):
        return self.model.embed_tokens

    def set_input_embeddings(self, value):
        self.model.embed_tokens = value

    @add_start_docstrings_to_model_forward(ZEBRA_INPUTS_DOCSTRING)
    def forward(
        self,
        input_ids: torch.LongTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[List[torch.FloatTensor]] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        labels: Optional[torch.LongTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple, SequenceClassifierOutputWithPast]:
        r"""
        labels (`torch.LongTensor` of shape `(batch_size,)`, *optional*):
            Labels for computing the sequence classification/regression loss. Indices should be in `[0, ...,
            config.num_labels - 1]`. If `config.num_labels == 1` a regression loss is computed (Mean-Square loss), If
            `config.num_labels > 1` a classification loss is computed (Cross-Entropy).
        """
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        transformer_outputs = self.model(
            input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=past_key_values,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )
        hidden_states = transformer_outputs[0]
        logits = self.score(hidden_states)

        if input_ids is not None:
            batch_size = input_ids.shape[0]
        else:
            batch_size = inputs_embeds.shape[0]

        if self.config.pad_token_id is None and batch_size != 1:
            raise ValueError("Cannot handle batch sizes > 1 if no padding token is defined.")
        if self.config.pad_token_id is None:
            sequence_lengths = -1
        else:
            if input_ids is not None:
                sequence_lengths = (torch.eq(input_ids, self.config.pad_token_id).long().argmax(-1) - 1).to(
                    logits.device
                )
            else:
                sequence_lengths = -1

        pooled_logits = logits[torch.arange(batch_size, device=logits.device), sequence_lengths]

        loss = None
        if labels is not None:
            labels = labels.to(logits.device)
            if self.config.problem_type is None:
                if self.num_labels == 1:
                    self.config.problem_type = "regression"
                elif self.num_labels > 1 and (labels.dtype == torch.long or labels.dtype == torch.int):
                    self.config.problem_type = "single_label_classification"
                else:
                    self.config.problem_type = "multi_label_classification"

            if self.config.problem_type == "regression":
                loss_fct = MSELoss()
                if self.num_labels == 1:
                    loss = loss_fct(pooled_logits.squeeze(), labels.squeeze())
                else:
                    loss = loss_fct(pooled_logits, labels)
            elif self.config.problem_type == "single_label_classification":
                loss_fct = CrossEntropyLoss()
                loss = loss_fct(pooled_logits.view(-1, self.num_labels), labels.view(-1))
            elif self.config.problem_type == "multi_label_classification":
                loss_fct = BCEWithLogitsLoss()
                loss = loss_fct(pooled_logits, labels)
        if not return_dict:
            output = (pooled_logits,) + transformer_outputs[1:]
            return ((loss,) + output) if loss is not None else output

        return SequenceClassifierOutputWithPast(
            loss=loss,
            logits=pooled_logits,
            past_key_values=transformer_outputs.past_key_values,
            hidden_states=transformer_outputs.hidden_states,
            attentions=transformer_outputs.attentions,
        )




class ZebraInfiniteModel(ZebraPreTrainedModel):
    """
    Transformer decoder consisting of *config.num_hidden_layers* layers. Each layer is a [`ZebraDecoderLayer`]

    Args:
        config: ZebraConfig
    """
    def __init__(self, config: ZebraConfig):
        super().__init__(config)
        self.padding_idx = config.pad_token_id
        self.latent_size = config.latent_size
        self.lift_mu = nn.Sequential(nn.Linear(1, config.hidden_size),
                                     nn.ReLU(),
                                     nn.Linear(config.hidden_size, config.hidden_size))
        self.lift_sigma = nn.Sequential(nn.Linear(1, config.hidden_size),
                                        nn.ReLU(),
                                        nn.Linear(config.hidden_size, config.hidden_size))
        self.embed_tokens = nn.Linear(config.latent_size, config.hidden_size)
        self.layers = nn.ModuleList([ZebraDecoderLayer(config) for _ in range(config.num_hidden_layers)])
        self.norm = nn.LayerNorm(config.hidden_size, eps=config.rms_norm_eps) #ZebraRMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.bidirectional = config.bidirectional
        self.gradient_checkpointing = False
        # Initialize weights and apply final processing
        self.post_init()

    def get_input_embeddings(self):
        return self.embed_tokens

    def set_input_embeddings(self, value):
        self.embed_tokens = value

    def _prepare_decoder_attention_mask(self, attention_mask, input_shape, inputs_embeds, past_key_values_length):
        if self.bidirectional:
            # For bidirectional models, use a different mask that doesn't enforce causality
            expanded_attn_mask = _expand_mask(attention_mask, inputs_embeds.dtype, tgt_len=input_shape[-1]).to(inputs_embeds.device)
            return expanded_attn_mask
        else:
            # For autoregressive models, use the causal mask
            combined_attention_mask = None
            if input_shape[-1] > 1:
                combined_attention_mask = _make_causal_mask(
                    input_shape,
                    inputs_embeds.dtype,
                    device=inputs_embeds.device,
                    past_key_values_length=past_key_values_length,
                )

            if attention_mask is not None:
                expanded_attn_mask = _expand_mask(attention_mask, inputs_embeds.dtype, tgt_len=input_shape[-1]).to(inputs_embeds.device)
                combined_attention_mask = (
                    expanded_attn_mask if combined_attention_mask is None else expanded_attn_mask + combined_attention_mask
                )
            return combined_attention_mask
    # # Copied from transformers.models.bart.modeling_bart.BartDecoder._prepare_decoder_attention_mask
    # def _prepare_decoder_attention_mask(self, attention_mask, input_shape, inputs_embeds, past_key_values_length):
    #     # create causal mask
    #     # [bsz, seq_len] -> [bsz, 1, tgt_seq_len, src_seq_len]
    #     combined_attention_mask = None
    #     if input_shape[-1] > 1:
    #         combined_attention_mask = _make_causal_mask(
    #             input_shape,
    #             inputs_embeds.dtype,
    #             device=inputs_embeds.device,
    #             past_key_values_length=past_key_values_length,
    #         )

    #     if attention_mask is not None:
    #         # [bsz, seq_len] -> [bsz, 1, tgt_seq_len, src_seq_len]
    #         expanded_attn_mask = _expand_mask(attention_mask, inputs_embeds.dtype, tgt_len=input_shape[-1]).to(
    #             inputs_embeds.device
    #         )
    #         combined_attention_mask = (
    #             expanded_attn_mask if combined_attention_mask is None else expanded_attn_mask + combined_attention_mask
    #         )
    #     return combined_attention_mask

    @add_start_docstrings_to_model_forward(ZEBRA_INPUTS_DOCSTRING)
    def forward(
        self,
        input_ids: torch.FloatTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[List[torch.FloatTensor]] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        mu: Optional[torch.Tensor] = None,
        sigma: Optional[torch.Tensor] = None,
    ) -> Union[Tuple, BaseModelOutputWithPast]:
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        use_cache = use_cache if use_cache is not None else self.config.use_cache

        return_dict = return_dict if return_dict is not None else self.config.use_return_dict


        if mu is not None:
            mu_embedding = self.lift_mu(mu[..., None])

        if sigma is not None:
            sigma_embedding = self.lift_sigma(sigma[..., None])

        # retrieve input_ids and inputs_embeds
        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is not None:
            batch_size, seq_length, vocab_dim = input_ids.shape
        elif inputs_embeds is not None:
            batch_size, seq_length, _ = inputs_embeds.shape
        else:
            raise ValueError("You have to specify either input_ids or inputs_embeds")

        seq_length_with_past = seq_length
        past_key_values_length = 0

        if inputs_embeds is None:
            inputs_embeds = self.embed_tokens(input_ids)

        if mu is not None:
            x = torch.cat([mu_embedding, inputs_embeds], axis=1)
            #attention_mask = torch.cat([torch.ones(batch_size, 1).bool().to(attention_mask), attention_mask], axis=-1)
            seq_length +=1

        if sigma is not None:
            x = torch.cat([sigma_embedding, x], axis=1)
            #attention_mask = torch.cat([torch.ones(batch_size, 1).bool().to(attention_mask), attention_mask], axis=-1)
            seq_length +=1

        if past_key_values is not None:
            past_key_values_length = past_key_values[0][0].shape[2]
            seq_length_with_past = seq_length_with_past + past_key_values_length

        if position_ids is None:
            device = input_ids.device if input_ids is not None else inputs_embeds.device
            position_ids = torch.arange(
                past_key_values_length, seq_length + past_key_values_length, dtype=torch.long, device=device
            )
            position_ids = position_ids.unsqueeze(0).view(-1, seq_length)
        else:
            position_ids = position_ids.view(-1, seq_length).long()

        # embed positions
        if attention_mask is None:
            attention_mask = torch.ones(
                (batch_size, seq_length_with_past), dtype=torch.bool, device=inputs_embeds.device
            )
            padding_mask = None
        else:
            if 0 in attention_mask:
                padding_mask = attention_mask
            else:
                padding_mask = None

        attention_mask = self._prepare_decoder_attention_mask(
            attention_mask, (batch_size, seq_length_with_past), inputs_embeds, past_key_values_length
        )

        # first add min to the first row
        attention_mask = torch.cat([torch.full((seq_length_with_past, 2), torch.finfo(torch.float32).min, device=device)[None, None, ...].repeat(batch_size, 1, 1, 1), attention_mask], axis=-1)
        #print('attention_mask', attention_mask.shape)
        attention_mask = torch.cat([torch.zeros(batch_size, 1, 2, seq_length).to(device), attention_mask], axis=-2)

        hidden_states = x

        if self.gradient_checkpointing and self.training:
            if use_cache:
                logger.warning_once(
                    "`use_cache=True` is incompatible with gradient checkpointing. Setting `use_cache=False`..."
                )
                use_cache = False

        # decoder layers
        all_hidden_states = () if output_hidden_states else None
        all_self_attns = () if output_attentions else None
        next_decoder_cache = () if use_cache else None

        for idx, decoder_layer in enumerate(self.layers):
            if output_hidden_states:
                all_hidden_states += (hidden_states,)

            past_key_value = past_key_values[idx] if past_key_values is not None else None

            if self.gradient_checkpointing and self.training:

                def create_custom_forward(module):
                    def custom_forward(*inputs):
                        # None for past_key_value
                        return module(*inputs, past_key_value, output_attentions, padding_mask=padding_mask)

                    return custom_forward

                layer_outputs = torch.utils.checkpoint.checkpoint(
                    create_custom_forward(decoder_layer), hidden_states, attention_mask, position_ids
                )
            else:
                layer_outputs = decoder_layer(
                    hidden_states,
                    attention_mask=attention_mask,
                    position_ids=position_ids,
                    past_key_value=past_key_value,
                    output_attentions=output_attentions,
                    use_cache=use_cache,
                    padding_mask=padding_mask,
                )

            hidden_states = layer_outputs[0]

            if use_cache:
                next_decoder_cache += (layer_outputs[2 if output_attentions else 1],)

            if output_attentions:
                all_self_attns += (layer_outputs[1],)

        hidden_states = self.norm(hidden_states)

        # add hidden states from the last decoder layer
        if output_hidden_states:
            all_hidden_states += (hidden_states,)

        next_cache = next_decoder_cache if use_cache else None
        if not return_dict:
            return tuple(v for v in [hidden_states, next_cache, all_hidden_states, all_self_attns] if v is not None)
        return BaseModelOutputWithPast(
            last_hidden_state=hidden_states,
            past_key_values=next_cache,
            hidden_states=all_hidden_states,
            attentions=all_self_attns,
        )


class ZebraInfiniteForCausalLM(ZebraPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        self.model = ZebraInfiniteModel(config)
        self.latent_size = config.latent_size
        self.num_mixtures = config.num_mixtures
        
        self.mean_head = nn.Linear(config.hidden_size, self.latent_size * self.num_mixtures, bias=False)
        self.variance_head = nn.Linear(config.hidden_size, self.latent_size * self.num_mixtures, bias=False)
        self.mix_prob_head = nn.Linear(config.hidden_size, self.latent_size * self.num_mixtures, bias=False)
        self.softplus = torch.nn.Softplus()
        # Initialize weights and apply final processing
        self.post_init()

    def get_input_embeddings(self):
        return self.model.embed_tokens

    def set_input_embeddings(self, value):
        self.model.embed_tokens = value

    def get_output_embeddings(self):
        return self.lm_head

    def set_output_embeddings(self, new_embeddings):
        self.lm_head = new_embeddings

    def set_decoder(self, decoder):
        self.model = decoder

    def get_decoder(self):
        return self.model

    @add_start_docstrings_to_model_forward(ZEBRA_INPUTS_DOCSTRING)
    @replace_return_docstrings(output_type=CausalLMOutputWithPast, config_class=_CONFIG_FOR_DOC)
    def forward(
        self,
        input_ids: torch.LongTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[List[torch.FloatTensor]] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        labels: Optional[torch.LongTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,

    ) -> Union[Tuple, CausalLMOutputWithPast]:
        r"""
        Args:
            labels (`torch.LongTensor` of shape `(batch_size, sequence_length)`, *optional*):
                Labels for computing the masked language modeling loss. Indices should either be in `[0, ...,
                config.vocab_size]` or -100 (see `input_ids` docstring). Tokens with indices set to `-100` are ignored
                (masked), the loss is only computed for the tokens with labels in `[0, ..., config.vocab_size]`.

        Returns:

        Example:

        ```python
        >>> from transformers import AutoTokenizer, ZebraForCausalLM

        >>> model = ZebraForCausalLM.from_pretrained(PATH_TO_CONVERTED_WEIGHTS)
        >>> tokenizer = AutoTokenizer.from_pretrained(PATH_TO_CONVERTED_TOKENIZER)

        >>> prompt = "Hey, are you conscious? Can you talk to me?"
        >>> inputs = tokenizer(prompt, return_tensors="pt")

        >>> # Generate
        >>> generate_ids = model.generate(inputs.input_ids, max_length=30)
        >>> tokenizer.batch_decode(generate_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)[0]
        "Hey, are you conscious? Can you talk to me?\nI'm not conscious, but I can talk to you."
        ```"""

        start_time = time.time()

        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        # decoder outputs consists of (dec_features, layer_state, dec_hidden, dec_attn)
        outputs = self.model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=past_key_values,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            mu=mu,
            sigma=sigma
        )

        hidden_states = outputs[0]
        if mu is not None:
            hidden_states = hidden_states[:, 1:, : ] # we remove the mu token
        if sigma is not None:
            hidden_states = hidden_states[:, 1:, : ] # we remove the sigma token
        #print('hidden_states', hidden_states.shape)

        batch, seqlen , dim = input_ids.shape
        means = self.mean_head(hidden_states)
        scales =self.variance_head(hidden_states)  # Ensure scales are positive
        mix_probs = self.mix_prob_head(hidden_states) # Ensure mixture probabilities sum to 1

        # Reshape the outputs to [batch_size, seq_length, d, k]
        means = means.view(batch, seqlen, self.latent_size, self.num_mixtures)
        scales =  self.softplus(scales.view(batch, seqlen, self.latent_size, self.num_mixtures))
        mix_probs = F.softmax(mix_probs.view(batch, seqlen, self.latent_size, self.num_mixtures), dim = -1 )

        #print('process tokens', time.time() - start_time)


        start_time = time.time()

        loss = None
        if labels is not None:
            shift_means = means[..., :-1, :, :].contiguous()
            shift_scales = scales[..., :-1, :, :].contiguous()
            shift_mix_probs = mix_probs[..., :-1, :, :].contiguous()
            #     print(shift_means.shape)
            shift_labels = labels[..., 1:,:].contiguous()
            loss = self.gmm_neg_log_likelihood( shift_labels, shift_means, shift_scales, shift_mix_probs )

        if not return_dict:
            output = (means,scales, mix_probs) + outputs[1:]
            return (loss,) + output if loss is not None else output

        #print('compute loss', time.time() - start_time)

        return CausalLMOutputWithPast(
            loss=loss,
            logits=(means,scales, mix_probs),
            past_key_values=outputs.past_key_values,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )
    # def gmm_log_like3(self, x, means, sclaes, mix_probs):
    #     gaussian_nll_loss = nn.GaussianNLLLoss()
    #     likelihood = 0.0
    #     for k in range(means.size(-1)):
    #         likelihood += gaussian_nll_loss(means, x, vars)
    #     return likelihood
    
    # def gmm_log_like2(self, x, means, scales, mix_probs):
    #     # Ensure scales are not too small
    #     scales = torch.maximum(scales, torch.tensor(1e-6))
        
    #     log_likelihood = None
    #     for k in range(means.size(-1)):
    #         log_prob = self.pytorch_normal_log_pdf(x, means[..., k], scales[..., k])
    #         log_mix = torch.log(torch.clamp(mix_probs[..., k], min=1e-6))  # Clipping mix_probs for stability
    #         if log_likelihood is None:
    #             log_likelihood = log_prob + log_mix
    #         else:
    #             log_likelihood = torch.logsumexp(torch.stack([log_likelihood, log_prob + log_mix]), dim=0)
    #     return torch.sum(log_likelihood)  # Remove the negative sign for a positive loss

    # def pytorch_normal_log_pdf(self, x, mean, std):
    #     normal_dist = Normal(mean, std)
    #     return normal_dist.log_prob(x)

    def gmm_neg_log_likelihood(self, x, means, scales, mix_probs):
        """
        Compute the negative log-likelihood of data x under the GMM defined by means, scales, and mix_probs.

        Args:
        x (Tensor): The ground truth data, shape [batch_size, seq_length, vocab]
        means (Tensor): Predicted means, shape [batch_size, seq_length, d, k]
        scales (Tensor): Predicted scales, shape [batch_size, seq_length, d, k]
        mix_probs (Tensor): Predicted mixture probabilities, shape [batch_size, seq_length, d, k]

        Returns:
        Tensor: The negative log-likelihood loss
        """
        batch_size, seq_length, vocab = x.size()
        _, _, d, k = means.size()
        x = x.unsqueeze(-1).expand_as(means)


        # Calculate the Gaussian likelihood for each component
        var = scales ** 2
        gaussian = torch.exp(-0.5 * ((x - means) ** 2) / var) / (scales * math.sqrt(2 * math.pi))

        # Weighted sum of Gaussian likelihoods for each mixture component
        weighted_gaussian = gaussian * mix_probs
        weighted_sum = weighted_gaussian.sum(dim=-1)  # Sum over the mixture components

        # Compute negative log likelihood
        nll = torch.log(weighted_sum + 1e-6)
        loss = -nll.mean()  # Mean over all dimensions

        return loss


    def prepare_inputs_for_generation(
        self, input_ids, past_key_values=None, attention_mask=None, inputs_embeds=None, **kwargs
    ):
        if past_key_values:
            input_ids = input_ids[:, -1:]

        position_ids = kwargs.get("position_ids", None)
        if attention_mask is not None and position_ids is None:
            # create position_ids on the fly for batch generation
            position_ids = attention_mask.long().cumsum(-1) - 1
            position_ids.masked_fill_(attention_mask == 0, 1)
            if past_key_values:
                position_ids = position_ids[:, -1].unsqueeze(-1)

        # if `inputs_embeds` are passed, we only want to use them in the 1st generation step
        if inputs_embeds is not None and past_key_values is None:
            model_inputs = {"inputs_embeds": inputs_embeds}
        else:
            model_inputs = {"input_ids": input_ids}

        model_inputs.update(
            {
                "position_ids": position_ids,
                "past_key_values": past_key_values,
                "use_cache": kwargs.get("use_cache"),
                "attention_mask": attention_mask,
            }
        )
        return model_inputs

    @staticmethod
    def _reorder_cache(past_key_values, beam_idx):
        reordered_past = ()
        for layer_past in past_key_values:
            reordered_past += (
                tuple(past_state.index_select(0, beam_idx.to(past_state.device)) for past_state in layer_past),
            )
        return reordered_past



class ZebraInfiniteForCausalLMv2(ZebraPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        self.model = ZebraInfiniteModelv2(config)
        new_config = deepcopy(config)
        new_config.latent_size = 2
        new_config.num_hidden_layers = 4
        new_config.last_norm = False 
        #self.model_stats = ZebraInfiniteModelv2(new_config)
        self.latent_size = config.latent_size
        self.num_mixtures = config.num_mixtures
        self.hidden_size = config.hidden_size
       
        #self.revin_head = nn.Linear(config.hidden_size, 2*config.hidden_size)
        self.mean_head = nn.Linear(config.hidden_size, self.latent_size * self.num_mixtures, bias=True)
        self.variance_head = nn.Linear(config.hidden_size, self.latent_size * self.num_mixtures, bias=True)
        self.mix_prob_head = nn.Linear(config.hidden_size, self.latent_size * self.num_mixtures, bias=True)
        self.softplus = torch.nn.Softplus()
        # Initialize weights and apply final processing
        self.post_init()
        self.rel_loss = RelativeL2()

    def get_input_embeddings(self):
        return self.model.embed_tokens

    def set_input_embeddings(self, value):
        self.model.embed_tokens = value

    def get_output_embeddings(self):
        return self.lm_head

    def set_output_embeddings(self, new_embeddings):
        self.lm_head = new_embeddings

    def set_decoder(self, decoder):
        self.model = decoder

    def get_decoder(self):
        return self.model

    @add_start_docstrings_to_model_forward(ZEBRA_INPUTS_DOCSTRING)
    @replace_return_docstrings(output_type=CausalLMOutputWithPast, config_class=_CONFIG_FOR_DOC)
    def forward(
        self,
        input_ids: torch.LongTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[List[torch.FloatTensor]] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        labels: Optional[torch.LongTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        mu: torch.Tensor = None,
        sigma: torch.Tensor = None,
        patch_size=10,

    ) -> Union[Tuple, CausalLMOutputWithPast]:
        r"""
        Args:
            labels (`torch.LongTensor` of shape `(batch_size, sequence_length)`, *optional*):
                Labels for computing the masked language modeling loss. Indices should either be in `[0, ...,
                config.vocab_size]` or -100 (see `input_ids` docstring). Tokens with indices set to `-100` are ignored
                (masked), the loss is only computed for the tokens with labels in `[0, ..., config.vocab_size]`.

        Returns:

        Example:

        ```python
        >>> from transformers import AutoTokenizer, ZebraForCausalLM

        >>> model = ZebraForCausalLM.from_pretrained(PATH_TO_CONVERTED_WEIGHTS)
        >>> tokenizer = AutoTokenizer.from_pretrained(PATH_TO_CONVERTED_TOKENIZER)

        >>> prompt = "Hey, are you conscious? Can you talk to me?"
        >>> inputs = tokenizer(prompt, return_tensors="pt")

        >>> # Generate
        >>> generate_ids = model.generate(inputs.input_ids, max_length=30)
        >>> tokenizer.batch_decode(generate_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)[0]
        "Hey, are you conscious? Can you talk to me?\nI'm not conscious, but I can talk to you."
        ```"""

        start_time = time.time()

        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        # decoder outputs consists of (dec_features, layer_state, dec_hidden, dec_attn)
        outputs = self.model(
            input_ids=input_ids,#(input_ids - mu)/(sigma + 1e-8),
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=past_key_values,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        #outputs_revin = self.model_stats(
        #    input_ids=torch.cat([mu, sigma], axis=-1),
        #    attention_mask=attention_mask,
        #    position_ids=position_ids,
        #    past_key_values=past_key_values,
        #    inputs_embeds=inputs_embeds,
        #    use_cache=use_cache,
        #    output_attentions=output_attentions,
        #    output_hidden_states=output_hidden_states,
        #    return_dict=return_dict,
        #)
        #mean_scale = self.revin_head(outputs_revin[0])
        hidden_states = outputs[0]
        #print('hidden_states', hidden_states.shape)
        #print('mean_scale', mean_scale.shape)
        #hidden_states = torch.relu(mean_scale[..., self.hidden_size:])*hidden_states + mean_scale[..., :self.hidden_size]
        #hidden_states = hidden_states*sigma + mu
        #if mu is not None:
        #    hidden_states = hidden_states[:, 1:, : ] # we remove the mu token
        #if sigma is not None:
        #    hidden_states = hidden_states[:, 1:, : ] # we remove the sigma token
        #print('hidden_states', hidden_states.shape)

        batch, seqlen , dim = input_ids.shape
        means = self.mean_head(hidden_states)
        scales = self.variance_head(hidden_states)  # Ensure scales are positive
        mix_probs = self.mix_prob_head(hidden_states) # Ensure mixture probabilities sum to 1

        # Reshape the outputs to [batch_size, seq_length, d, k]
        means = means.view(batch, seqlen, self.latent_size, self.num_mixtures)
        scales =  self.softplus(scales.view(batch, seqlen, self.latent_size, self.num_mixtures))
        mix_probs = F.softmax(mix_probs.view(batch, seqlen, self.latent_size, self.num_mixtures), dim = -1 )

        #print('process tokens', time.time() - start_time)


        start_time = time.time()

        loss = None
        if labels is not None:
            shift_means = means[..., :-1, :, :].contiguous()
            shift_scales = scales[..., :-1, :, :].contiguous()
            shift_mix_probs = mix_probs[..., :-1, :, :].contiguous()
            #     print(shift_means.shape)
            shift_labels = labels[..., 1:,:].contiguous()
            #loss = self.gmm_neg_log_likelihood( shift_labels, shift_means, shift_scales, shift_mix_probs )
            #loss = self.rel_loss(rearrange(shift_means[..., 0], ' b t c -> (b t) c'), rearrange(shift_labels, 'b t c -> (b t) c'))
            #loss = self.rel_loss(shift_means[..., 0], shift_labels)
            use_rel_loss = True
            if use_rel_loss:
                pred = torch.cat([input_ids[:, :1, :], shift_means[..., 0]], axis=1)
                shift_labels = torch.cat([input_ids[:, :1, :], shift_labels], axis=1)
                pred = rearrange(pred, 'b (t l) c -> (b t) (l c)', l=patch_size) 
                shift_labels = rearrange(shift_labels, 'b (t l) c -> (b t) (l c)', l=patch_size) 
                loss = self.rel_loss(pred, shift_labels)
            else: 
                loss = ((shift_means[..., 0] - shift_labels)**2).mean()

        if not return_dict:
            output = (means,scales, mix_probs) + outputs[1:]
            return (loss,) + output if loss is not None else output

        #print('compute loss', time.time() - start_time)

        return CausalLMOutputWithPast(
            loss=loss,
            logits=(means,scales, mix_probs),
            past_key_values=outputs.past_key_values,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )

    def gmm_neg_log_likelihood(self, x, means, scales, mix_probs):
        """
        Compute the negative log-likelihood of data x under the GMM defined by means, scales, and mix_probs.

        Args:
        x (Tensor): The ground truth data, shape [batch_size, seq_length, vocab]
        means (Tensor): Predicted means, shape [batch_size, seq_length, d, k]
        scales (Tensor): Predicted scales, shape [batch_size, seq_length, d, k]
        mix_probs (Tensor): Predicted mixture probabilities, shape [batch_size, seq_length, d, k]

        Returns:
        Tensor: The negative log-likelihood loss
        """
        batch_size, seq_length, vocab = x.size()
        _, _, d, k = means.size()
        x = x.unsqueeze(-1).expand_as(means)


        # Calculate the Gaussian likelihood for each component
        var = scales ** 2
        gaussian = torch.exp(-0.5 * ((x - means) ** 2) / var) / (scales * math.sqrt(2 * math.pi))

        # Weighted sum of Gaussian likelihoods for each mixture component
        weighted_gaussian = gaussian * mix_probs
        weighted_sum = weighted_gaussian.sum(dim=-1)  # Sum over the mixture components

        # Compute negative log likelihood
        nll = torch.log(weighted_sum + 1e-6)
        loss = -nll.mean()  # Mean over all dimensions

        return loss

class ZebraInfiniteModelv2(ZebraPreTrainedModel):
    """
    Transformer decoder consisting of *config.num_hidden_layers* layers. Each layer is a [`ZebraDecoderLayer`]

    Args:
        config: ZebraConfig
    """
    def __init__(self, config: ZebraConfig):
        super().__init__(config)
        self.padding_idx = config.pad_token_id
        self.latent_size = config.latent_size
        self.embed_tokens = nn.Linear(config.latent_size, config.hidden_size, bias=False)
        self.layers = nn.ModuleList([ZebraDecoderLayer2(config) for _ in range(config.num_hidden_layers)])
        self.norm = nn.LayerNorm(config.hidden_size, eps=config.rms_norm_eps) #ZebraRMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.bidirectional = config.bidirectional
        self.gradient_checkpointing = False
        # Initialize weights and apply final processing
        self.post_init()
        self.last_norm = False #config.last_norm

    def get_input_embeddings(self):
        return self.embed_tokens

    def set_input_embeddings(self, value):
        self.embed_tokens = value

    def _prepare_decoder_attention_mask(self, attention_mask, input_shape, inputs_embeds, past_key_values_length):
        if self.bidirectional:
            # For bidirectional models, use a different mask that doesn't enforce causality
            expanded_attn_mask = _expand_mask(attention_mask, inputs_embeds.dtype, tgt_len=input_shape[-1]).to(inputs_embeds.device)
            return expanded_attn_mask
        else:
            # For autoregressive models, use the causal mask
            combined_attention_mask = None
            if input_shape[-1] > 1:
                combined_attention_mask = _make_causal_mask(
                    input_shape,
                    inputs_embeds.dtype,
                    device=inputs_embeds.device,
                    past_key_values_length=past_key_values_length,
                )

            if attention_mask is not None:
                expanded_attn_mask = _expand_mask(attention_mask, inputs_embeds.dtype, tgt_len=input_shape[-1]).to(inputs_embeds.device)
                combined_attention_mask = (
                    expanded_attn_mask if combined_attention_mask is None else expanded_attn_mask + combined_attention_mask
                )
            return combined_attention_mask
    # # Copied from transformers.models.bart.modeling_bart.BartDecoder._prepare_decoder_attention_mask
    # def _prepare_decoder_attention_mask(self, attention_mask, input_shape, inputs_embeds, past_key_values_length):
    #     # create causal mask
    #     # [bsz, seq_len] -> [bsz, 1, tgt_seq_len, src_seq_len]
    #     combined_attention_mask = None
    #     if input_shape[-1] > 1:
    #         combined_attention_mask = _make_causal_mask(
    #             input_shape,
    #             inputs_embeds.dtype,
    #             device=inputs_embeds.device,
    #             past_key_values_length=past_key_values_length,
    #         )

    #     if attention_mask is not None:
    #         # [bsz, seq_len] -> [bsz, 1, tgt_seq_len, src_seq_len]
    #         expanded_attn_mask = _expand_mask(attention_mask, inputs_embeds.dtype, tgt_len=input_shape[-1]).to(
    #             inputs_embeds.device
    #         )
    #         combined_attention_mask = (
    #             expanded_attn_mask if combined_attention_mask is None else expanded_attn_mask + combined_attention_mask
    #         )
    #     return combined_attention_mask

    @add_start_docstrings_to_model_forward(ZEBRA_INPUTS_DOCSTRING)
    def forward(
        self,
        input_ids: torch.FloatTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[List[torch.FloatTensor]] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple, BaseModelOutputWithPast]:
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        use_cache = use_cache if use_cache is not None else self.config.use_cache

        return_dict = return_dict if return_dict is not None else self.config.use_return_dict


        # retrieve input_ids and inputs_embeds
        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is not None:
            batch_size, seq_length, vocab_dim = input_ids.shape
        elif inputs_embeds is not None:
            batch_size, seq_length, _ = inputs_embeds.shape
        else:
            raise ValueError("You have to specify either input_ids or inputs_embeds")

        seq_length_with_past = seq_length
        past_key_values_length = 0

        if inputs_embeds is None:
            inputs_embeds = self.embed_tokens(input_ids)


        if past_key_values is not None:
            past_key_values_length = past_key_values[0][0].shape[2]
            seq_length_with_past = seq_length_with_past + past_key_values_length

        if position_ids is None:
            device = input_ids.device if input_ids is not None else inputs_embeds.device
            position_ids = torch.arange(
                past_key_values_length, seq_length + past_key_values_length, dtype=torch.long, device=device
            )
            position_ids = position_ids.unsqueeze(0).view(-1, seq_length)
        else:
            position_ids = position_ids.view(-1, seq_length).long()

        # embed positions
        if attention_mask is None:
            attention_mask = torch.ones(
                (batch_size, seq_length_with_past), dtype=torch.bool, device=inputs_embeds.device
            )
            padding_mask = None
        else:
            if 0 in attention_mask:
                padding_mask = attention_mask
            else:
                padding_mask = None

        attention_mask = self._prepare_decoder_attention_mask(
            attention_mask, (batch_size, seq_length_with_past), inputs_embeds, past_key_values_length
        )

        # first add min to the first row

        hidden_states = inputs_embeds

        if self.gradient_checkpointing and self.training:
            if use_cache:
                logger.warning_once(
                    "`use_cache=True` is incompatible with gradient checkpointing. Setting `use_cache=False`..."
                )
                use_cache = False

        # decoder layers
        all_hidden_states = () if output_hidden_states else None
        all_self_attns = () if output_attentions else None
        next_decoder_cache = () if use_cache else None

        for idx, decoder_layer in enumerate(self.layers):
            if output_hidden_states:
                all_hidden_states += (hidden_states,)

            past_key_value = past_key_values[idx] if past_key_values is not None else None

            if self.gradient_checkpointing and self.training:

                def create_custom_forward(module):
                    def custom_forward(*inputs):
                        # None for past_key_value
                        return module(*inputs, past_key_value, output_attentions, padding_mask=padding_mask)

                    return custom_forward

                layer_outputs = torch.utils.checkpoint.checkpoint(
                    create_custom_forward(decoder_layer), hidden_states, attention_mask, position_ids
                )
            else:
                layer_outputs = decoder_layer(
                    hidden_states,
                    attention_mask=attention_mask,
                    position_ids=position_ids,
                    past_key_value=past_key_value,
                    output_attentions=output_attentions,
                    use_cache=use_cache,
                    padding_mask=padding_mask,
                )

            hidden_states = layer_outputs[0]

            if use_cache:
                next_decoder_cache += (layer_outputs[2 if output_attentions else 1],)

            if output_attentions:
                all_self_attns += (layer_outputs[1],)
        
        if self.last_norm:
            hidden_states = self.norm(hidden_states)
        else:
            hidden_states = hidden_states

        # add hidden states from the last decoder layer
        if output_hidden_states:
            all_hidden_states += (hidden_states,)

        next_cache = next_decoder_cache if use_cache else None
        if not return_dict:
            return tuple(v for v in [hidden_states, next_cache, all_hidden_states, all_self_attns] if v is not None)
        return BaseModelOutputWithPast(
            last_hidden_state=hidden_states,
            past_key_values=next_cache,
            hidden_states=all_hidden_states,
            attentions=all_self_attns,
        )



class ZebraInfiniteForCausalLMmixed(ZebraPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        self.model = ZebraInfiniteModelmixed(config)
        self.latent_size = config.latent_size
        self.num_mixtures = config.num_mixtures
        self.vocab_size = config.vocab_size
        
        self.mean_head = nn.Linear(config.hidden_size, (self.latent_size) * self.num_mixtures, bias=False) #bias=False
        self.variance_head = nn.Linear(config.hidden_size, (self.latent_size) * self.num_mixtures, bias=False)
        self.mix_prob_head = nn.Linear(config.hidden_size, self.num_mixtures, bias=True)
        self.norm = nn.LayerNorm(config.hidden_size, eps=config.rms_norm_eps) #ZebraRMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        #self.mix_prob_head = nn.Linear(config.hidden_size, (self.latent_size) * self.num_mixtures, bias=True)

        self.softplus = torch.nn.Softplus()
        self.vocab_head = nn.Linear(config.hidden_size, self.vocab_size, bias=True)
        # Initialize weights and apply final processing
        self.post_init()
        self.rel_loss = RelativeL2()

    def get_input_embeddings(self):
        return self.model.embed_tokens

    def set_input_embeddings(self, value):
        self.model.embed_tokens = value

    def get_output_embeddings(self):
        return self.lm_head

    def set_output_embeddings(self, new_embeddings):
        self.lm_head = new_embeddings

    def set_decoder(self, decoder):
        self.model = decoder

    def get_decoder(self):
        return self.model

    @add_start_docstrings_to_model_forward(ZEBRA_INPUTS_DOCSTRING)
    @replace_return_docstrings(output_type=CausalLMOutputWithPast, config_class=_CONFIG_FOR_DOC)
    def forward(
        self,
        input_ids: torch.FloatTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[List[torch.FloatTensor]] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        labels: Optional[torch.LongTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        scale_tokens: torch.Tensor = None,
        mu_tokens: torch.Tensor = None,
        weight_logits=1.0,
        patch_size=10,
        use_variance=False,

    ) -> Union[Tuple, CausalLMOutputWithPast]:
        r"""
        Args:
            labels (`torch.LongTensor` of shape `(batch_size, sequence_length)`, *optional*):
                Labels for computing the masked language modeling loss. Indices should either be in `[0, ...,
                config.vocab_size]` or -100 (see `input_ids` docstring). Tokens with indices set to `-100` are ignored
                (masked), the loss is only computed for the tokens with labels in `[0, ..., config.vocab_size]`.

        Returns:

        Example:

        ```python
        >>> from transformers import AutoTokenizer, ZebraForCausalLM

        >>> model = ZebraForCausalLM.from_pretrained(PATH_TO_CONVERTED_WEIGHTS)
        >>> tokenizer = AutoTokenizer.from_pretrained(PATH_TO_CONVERTED_TOKENIZER)

        >>> prompt = "Hey, are you conscious? Can you talk to me?"
        >>> inputs = tokenizer(prompt, return_tensors="pt")

        >>> # Generate
        >>> generate_ids = model.generate(inputs.input_ids, max_length=30)
        >>> tokenizer.batch_decode(generate_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)[0]
        "Hey, are you conscious? Can you talk to me?\nI'm not conscious, but I can talk to you."
        ```"""

        start_time = time.time()

        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        # decoder outputs consists of (dec_features, layer_state, dec_hidden, dec_attn)
        outputs = self.model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=past_key_values,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            scale_tokens=scale_tokens,
            mu_tokens=mu_tokens
        )

        hidden_states = outputs[0]
        hidden_states = self.norm(hidden_states) # v0
         

        #if mu is not None:
        #    hidden_states = hidden_states[:, 1:, : ] # we remove the mu token
        #if sigma is not None:
        #    hidden_states = hidden_states[:, 1:, : ] # we remove the sigma token
        #print('hidden_states', hidden_states.shape)

        batch, seqlen , dim = input_ids.shape
        means = self.mean_head(hidden_states)
        scales =self.variance_head(hidden_states)  # Ensure scales are positive
        mix_probs = torch.clamp(self.mix_prob_head(hidden_states), -10, 10) # v0
        #mix_probs = torch.clamp(self.mix_prob_head(self.norm(hidden_states)), -10, 10) # v1
        #hidden_states = self.norm(hidden_states) # v0
        logits_vocab = self.vocab_head(hidden_states)

        # Reshape the outputs to [batch_size, seq_length, d, k]
        means = means.view(batch, seqlen, self.latent_size, self.num_mixtures)
        #scales =  self.softplus(scales.view(batch, seqlen, self.latent_size, self.num_mixtures)) + 1e-8
        scales = torch.clamp(self.softplus(scales.view(batch, seqlen, self.latent_size, self.num_mixtures)), 1e-8, 20)
        #mix_probs = F.softmax(mix_probs.view(batch, seqlen, self.latent_size, self.num_mixtures), dim = -1 )
        mix_probs = F.softmax(mix_probs.view(batch, seqlen, self.num_mixtures), dim = -1 )
        logits_vocab = F.softmax(logits_vocab, dim = -1 )

        #print('process tokens', time.time() - start_time)


        start_time = time.time()

        loss = None
        rel_loss = None
        if labels is not None:
            shift_means = means[:, :-1, :, :].contiguous()
            shift_scales = scales[:, :-1, :, :].contiguous()
            shift_mix_probs = mix_probs[:, :-1, :].contiguous()
            #     print(shift_means.shape)
            shift_labels = labels[..., 1:,:].contiguous()

            if use_variance:
                loss = self.gmm_neg_log_likelihood(shift_labels, shift_means, shift_scales, shift_mix_probs.unsqueeze(-2).repeat(1,1,self.latent_size,1))
            else:
                loss = self.weighted_mse(shift_labels, shift_means, shift_scales, shift_mix_probs.unsqueeze(-2).repeat(1,1,self.latent_size,1))

            #mixture_distribution = dist.Categorical(shift_mix_probs.view(-1, self.num_mixtures))
            #component_distribution = dist.Independent(dist.Normal(shift_means.view(-1, self.num_mixtures, self.latent_size), shift_scales.view(-1, self.num_mixtures, self.latent_size)), 1)
        
        # Create the mixture of Gaussians
            #mixture_of_gaussians = dist.MixtureSameFamily(mixture_distribution, component_distribution)
        
        # Compute the log probability of x
            #loss = - mixture_of_gaussians.log_prob(shift_labels.view(-1, self.latent_size)).mean()
            #loss = ((shift_means[..., 0] - shift_labels)**2).mean()
            #loss = self.rel_loss(shift_means[..., 0], shift_labels)
            #loss = self.rel_loss(rearrange(shift_means[..., 0], ' b t c -> (b t) c'), rearrange(shift_labels, 'b t c -> (b t) c'))
            use_rel_loss = False 
            if use_rel_loss:
                index = shift_mix_probs.argmax(-1)[..., None, None].repeat(1, 1, self.latent_size, 1)
                #weights = shift_mix_probs[..., None].repeat(1, 1, self.latent_size)
                #print(index.shape, shift_means.shape)
                pred = torch.cat([input_ids[:, :1, :], torch.gather(shift_means[..., :self.latent_size], -1, index).squeeze(-1) ], axis=1)
                shift_labels = torch.cat([input_ids[:, :1, :], shift_labels], axis=1)
                pred = rearrange(pred, 'b (t l) c -> (b t) (l c)', l=patch_size) 
                shift_labels = rearrange(shift_labels, 'b (t l) c -> (b t) (l c)',l=patch_size) 
                #loss = ((pred - shift_labels)**2).mean()
                rel_loss = self.rel_loss(pred, shift_labels)
                #loss = 1/self.latent_size*loss + rel_loss
                #loss = rel_loss
            else:
                pass 
                #loss = ((shift_means[..., 0] - shift_labels)**2).mean()

            #print(scale_tokens.shape, logits_vocab.shape)
            #shift_logits_vocab = logits_vocab[..., :-1, :].contiguous() 
            #shift_labels_vocab = scale_tokens[..., 1:].contiguous()
            #loss_fct = CrossEntropyLoss()
            #shift_logits = shift_logits.view(-1, self.config.vocab_size)
            #shift_labels = shift_labels.view(-1)
            # Enable model parallelism
            #loss += weight_logits*loss_fct(shift_logits_vocab.view(-1, self.vocab_size), shift_labels_vocab.view(-1)) 
            #loss += self.rel_loss(mu_tokens[..., 1:, :], shift_means[..., -1:, 0])

        if not return_dict:
            output = (means,scales, mix_probs) + outputs[1:]
            return (loss,) + output if loss is not None else output

        #print('compute loss', time.time() - start_time)

        return CausalLMOutputWithPast(
            loss=loss,
            rel_loss=rel_loss,
            logits=(means,scales, mix_probs, logits_vocab),
            past_key_values=outputs.past_key_values,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )

    def gmm_neg_log_likelihood(self, x, means, scales, mix_probs):
        """
        Compute the negative log-likelihood of data x under the GMM defined by means, scales, and mix_probs.

        Args:
        x (Tensor): The ground truth data, shape [batch_size, seq_length, vocab]
        means (Tensor): Predicted means, shape [batch_size, seq_length, d, k]
        scales (Tensor): Predicted scales, shape [batch_size, seq_length, d, k]
        mix_probs (Tensor): Predicted mixture probabilities, shape [batch_size, seq_length, d, k]

        Returns:
        Tensor: The negative log-likelihood loss
        """
        batch_size, seq_length, vocab = x.size()
        _, _, d, k = means.size()
        x = x.unsqueeze(-1).expand_as(means)


        # Calculate the Gaussian likelihood for each component
        var = scales ** 2
        gaussian = torch.exp(-0.5 * ((x - means) ** 2) / var) / (scales * math.sqrt(2 * math.pi))

        # Weighted sum of Gaussian likelihoods for each mixture component
        weighted_gaussian = gaussian * mix_probs
        weighted_sum = weighted_gaussian.sum(dim=-1)  # Sum over the mixture components

        # Compute negative log likelihood
        nll = torch.log(weighted_sum + 1e-6)
        loss = -nll.mean()  # Mean over all dimensions

        return loss
    
    def weighted_mse(self, x, means, scales, mix_probs):
        """
        Compute the negative log-likelihood of data x under the GMM defined by means, scales, and mix_probs.

        Args:
        x (Tensor): The ground truth data, shape [batch_size, seq_length, vocab]
        means (Tensor): Predicted means, shape [batch_size, seq_length, d, k]
        scales (Tensor): Predicted scales, shape [batch_size, seq_length, d, k]
        mix_probs (Tensor): Predicted mixture probabilities, shape [batch_size, seq_length, d, k]

        Returns:
        Tensor: The negative log-likelihood loss
        """
        batch_size, seq_length, vocab = x.size()
        _, _, d, k = means.size()
        x = x.unsqueeze(-1).expand_as(means)


        # Calculate the Gaussian likelihood for each component
        delta = ((x - means) ** 2) 

        # Weighted sum of Gaussian likelihoods for each mixture component
        weighted_gaussian = delta * mix_probs
        weighted_sum = weighted_gaussian.sum(dim=-1)  # Sum over the mixture components

        # Compute negative log likelihood
        loss = weighted_sum.mean()  # Mean over all dimensions

        return loss

    def gmm_neg_log_likelihood_2(self, x, means, scales, mix_probs):
        """
        Compute the negative log-likelihood of data x under the GMM defined by means, scales, and mix_probs.

        Args:
        x (Tensor): The ground truth data, shape [batch_size, seq_length, vocab]
        means (Tensor): Predicted means, shape [batch_size, seq_length, d, k]
        scales (Tensor): Predicted scales, shape [batch_size, seq_length, d, k]
        mix_probs (Tensor): Predicted mixture probabilities, shape [batch_size, seq_length, d, k]

        Returns:
        Tensor: The negative log-likelihood loss
        """
        batch_size, seq_length, vocab = x.size()
        _, _, d, k = means.size()
        x = x.unsqueeze(-1).expand_as(means)


        # Calculate the Gaussian likelihood for each component
        var = scales ** 2
        pre_exp = -0.5 * ((x - means) ** 2) / var #/ (scales * math.sqrt(2 * math.pi)
        gaussian = torch.exp(pre_exp.sum(-2)) / (scales.prod(-2) * math.sqrt(2 * math.pi)**d)

        # Weighted sum of Gaussian likelihoods for each mixture component
        weighted_gaussian = gaussian * mix_probs
        weighted_sum = weighted_gaussian.sum(dim=-1)  # Sum over the mixture components

        # Compute negative log likelihood
        nll = torch.log(weighted_sum + 1e-6)
        loss = -nll.mean()  # Mean over all dimensions

        return loss

class ZebraInfiniteModelmixed(ZebraPreTrainedModel):
    """
    Transformer decoder consisting of *config.num_hidden_layers* layers. Each layer is a [`ZebraDecoderLayer`]

    Args:
        config: ZebraConfig
    """
    def __init__(self, config: ZebraConfig):
        super().__init__(config)
        self.padding_idx = config.pad_token_id
        self.latent_size = config.latent_size
        #self.embed_vocab = nn.Embedding(config.vocab_size, config.hidden_size, self.padding_idx)
        #self.embed_vocab = nn.Linear(1, config.hidden_size, bias=False)
        self.embed_vocab = nn.Linear(config.latent_size, config.hidden_size, bias=False)
        self.embed_tokens = nn.Linear(config.latent_size, config.hidden_size, bias=False)
        self.layers = nn.ModuleList([ZebraDecoderLayer(config) for _ in range(config.num_hidden_layers)])
        self.layers_condition = ZebraDecoderLayer5(config)
        self.layers_proj = ZebraDecoderLayer4(config)
        self.bidirectional = config.bidirectional
        self.lift_mu = nn.Linear(1, config.hidden_size, bias=False)
        self.conditioning_mlp = nn.Sequential(nn.Linear(config.hidden_size, config.hidden_size),
                                              nn.ReLU(),
                                              nn.Linear(config.hidden_size, config.hidden_size))
        self.projection_mlp = nn.ModuleList([nn.Linear(config.hidden_size, config.hidden_size, bias=False) for j in range(config.num_hidden_layers//2)])
        self.gradient_checkpointing = False
        # Initialize weights and apply final processing
        self.post_init()

    def get_input_embeddings(self):
        return self.embed_tokens

    def set_input_embeddings(self, value):
        self.embed_tokens = value

    def _prepare_decoder_attention_mask(self, attention_mask, input_shape, inputs_embeds, past_key_values_length):
        if self.bidirectional:
            # For bidirectional models, use a different mask that doesn't enforce causality
            expanded_attn_mask = _expand_mask(attention_mask, inputs_embeds.dtype, tgt_len=input_shape[-1]).to(inputs_embeds.device)
            return expanded_attn_mask
        else:
            # For autoregressive models, use the causal mask
            combined_attention_mask = None
            if input_shape[-1] > 1:
                combined_attention_mask = _make_causal_mask(
                    input_shape,
                    inputs_embeds.dtype,
                    device=inputs_embeds.device,
                    past_key_values_length=past_key_values_length,
                )

            if attention_mask is not None:
                expanded_attn_mask = _expand_mask(attention_mask, inputs_embeds.dtype, tgt_len=input_shape[-1]).to(inputs_embeds.device)
                combined_attention_mask = (
                    expanded_attn_mask if combined_attention_mask is None else expanded_attn_mask + combined_attention_mask
                )
            return combined_attention_mask
    # # Copied from transformers.models.bart.modeling_bart.BartDecoder._prepare_decoder_attention_mask
    # def _prepare_decoder_attention_mask(self, attention_mask, input_shape, inputs_embeds, past_key_values_length):
    #     # create causal mask
    #     # [bsz, seq_len] -> [bsz, 1, tgt_seq_len, src_seq_len]
    #     combined_attention_mask = None
    #     if input_shape[-1] > 1:
    #         combined_attention_mask = _make_causal_mask(
    #             input_shape,
    #             inputs_embeds.dtype,
    #             device=inputs_embeds.device,
    #             past_key_values_length=past_key_values_length,
    #         )

    #     if attention_mask is not None:
    #         # [bsz, seq_len] -> [bsz, 1, tgt_seq_len, src_seq_len]
    #         expanded_attn_mask = _expand_mask(attention_mask, inputs_embeds.dtype, tgt_len=input_shape[-1]).to(
    #             inputs_embeds.device
    #         )
    #         combined_attention_mask = (
    #             expanded_attn_mask if combined_attention_mask is None else expanded_attn_mask + combined_attention_mask
    #         )
    #     return combined_attention_mask

    @add_start_docstrings_to_model_forward(ZEBRA_INPUTS_DOCSTRING)
    def forward(
        self,
        input_ids: torch.FloatTensor = None,
        scale_tokens: torch.LongTensor=None,
        mu_tokens: torch.FloatTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[List[torch.FloatTensor]] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple, BaseModelOutputWithPast]:
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        use_cache = use_cache if use_cache is not None else self.config.use_cache

        return_dict = return_dict if return_dict is not None else self.config.use_return_dict


        # retrieve input_ids and inputs_embeds
        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is not None:
            batch_size, seq_length, vocab_dim = input_ids.shape
        elif inputs_embeds is not None:
            batch_size, seq_length, _ = inputs_embeds.shape
        else:
            raise ValueError("You have to specify either input_ids or inputs_embeds")

        seq_length_with_past = seq_length
        past_key_values_length = 0

        if inputs_embeds is None:
            inputs_embeds = self.embed_tokens(input_ids)


        if past_key_values is not None:
            past_key_values_length = past_key_values[0][0].shape[2]
            seq_length_with_past = seq_length_with_past + past_key_values_length

        if position_ids is None:
            device = input_ids.device if input_ids is not None else inputs_embeds.device
            position_ids = torch.arange(
                past_key_values_length, seq_length + past_key_values_length, dtype=torch.long, device=device
            )
            position_ids = position_ids.unsqueeze(0).view(-1, seq_length)
        else:
            position_ids = position_ids.view(-1, seq_length).long()

        # embed positions
        if attention_mask is None:
            attention_mask = torch.ones(
                (batch_size, seq_length_with_past), dtype=torch.bool, device=inputs_embeds.device
            )
            padding_mask = None
        else:
            if 0 in attention_mask:
                padding_mask = attention_mask
            else:
                padding_mask = None

        attention_mask = self._prepare_decoder_attention_mask(
            attention_mask, (batch_size, seq_length_with_past), inputs_embeds, past_key_values_length
        )

        # first add min to the first row
        #print(inputs_embeds.shape)
        #print(self.embed_vocab(scale_tokens).shape)
        #print(scale_tokens.shape)
        
        #print('scale_tokens', scale_tokens.shape, mu_tokens.shape)
        #conditioning = self.embed_vocab(scale_tokens) + self.lift_mu(mu_tokens) 
        #conditioning = self.embed_vocab(scale_tokens) + self.lift_mu(mu_tokens) 
        #conditioning = self.conditioning_mlp(conditioning)

        #conditioning = self.embed_vocab(input_ids) # v1
        #layer_outputs = self.layers_condition(
        #            conditioning,
        #            #conditioning=conditioning,
        #            attention_mask=attention_mask,
        #            position_ids=position_ids,
        #            past_key_value=None,
        #            output_attentions=None,
        #            use_cache=use_cache,
        #            padding_mask=padding_mask,
        #        )
        #conditioning = layer_outputs[0] # v1

        #conditioning = self.embed_vocab(inputs_ids)
        hidden_states = inputs_embeds #+ self.embed_vocab(scale_tokens) !!! WARNING

        if self.gradient_checkpointing and self.training:
            if use_cache:
                logger.warning_once(
                    "`use_cache=True` is incompatible with gradient checkpointing. Setting `use_cache=False`..."
                )
                use_cache = False

        # decoder layers
        all_hidden_states = () if output_hidden_states else None
        all_self_attns = () if output_attentions else None
        next_decoder_cache = () if use_cache else None

        for idx, decoder_layer in enumerate(self.layers):
            if output_hidden_states:
                all_hidden_states += (hidden_states,)

            past_key_value = past_key_values[idx] if past_key_values is not None else None

            if self.gradient_checkpointing and self.training:

                def create_custom_forward(module):
                    def custom_forward(*inputs):
                        # None for past_key_value
                        return module(*inputs, past_key_value, output_attentions, padding_mask=padding_mask)

                    return custom_forward

                layer_outputs = torch.utils.checkpoint.checkpoint(
                    create_custom_forward(decoder_layer), hidden_states, attention_mask, position_ids
                )
            else:
                layer_outputs = decoder_layer(
                    hidden_states,
                    #conditioning=conditioning,
                    attention_mask=attention_mask,
                    position_ids=position_ids,
                    past_key_value=past_key_value,
                    output_attentions=output_attentions,
                    use_cache=use_cache,
                    padding_mask=padding_mask,
                )
                #if idx ==0: #2 == 1:
                #    conditioning = self.conditioning_mlp(hidden_states)
                #if idx % 2 == 1:
                    #conditioning = self.conditioning_mlp(self.projection_mlp[idx//2](hidden_states))
            #if idx == 0:
            #    conditioning = layer_outputs[0]
            #else:
            #    hidden_states = layer_outputs[0]
            hidden_states = layer_outputs[0]

            if use_cache:
                next_decoder_cache += (layer_outputs[2 if output_attentions else 1],)

            if output_attentions:
                all_self_attns += (layer_outputs[1],)

        #layer_outputs = self.layers_proj( # v1
        #            hidden_states,
        #            conditioning=conditioning,
        #            attention_mask=attention_mask,
        #            position_ids=position_ids,
        #            past_key_value=past_key_value,
        #            output_attentions=output_attentions,
        #            use_cache=use_cache,
        #            padding_mask=padding_mask,
        #        )
        #
        #hidden_states = layer_outputs[0] # v1
        #hidden_states = inputs_embeds + self.norm(hidden_states)

        # add hidden states from the last decoder layer
        if output_hidden_states:
            all_hidden_states += (hidden_states,)

        next_cache = next_decoder_cache if use_cache else None
        if not return_dict:
            return tuple(v for v in [hidden_states, next_cache, all_hidden_states, all_self_attns] if v is not None)
        return BaseModelOutputWithPast(
            last_hidden_state=hidden_states,
            past_key_values=next_cache,
            hidden_states=all_hidden_states,
            attentions=all_self_attns,
        )


class RelativeL2(torch.nn.Module):
    def forward(self, x, y):
        x = rearrange(x, "b ... -> b (...)")
        y = rearrange(y, "b ... -> b (...)")
        diff_norms = torch.linalg.norm(x - y, ord=2, dim=-1)
        y_norms = torch.linalg.norm(y, ord=2, dim=-1) + 1e-6
        return (diff_norms / y_norms).mean()

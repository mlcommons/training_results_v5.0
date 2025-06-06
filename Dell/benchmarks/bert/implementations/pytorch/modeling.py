# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HugginFace Inc. team.
# Copyright (c) 2018-2025, NVIDIA CORPORATION.  All rights reserved.
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
"""PyTorch BERT model."""

from __future__ import absolute_import, division, print_function, unicode_literals

import copy
import json
import logging
import math
import os
import shutil
import tarfile
import tempfile
import sys
from io import open
from operator import mul
from functools import reduce
from contextlib import nullcontext

import torch
from torch import nn
from torch.nn import CrossEntropyLoss
from torch.utils import checkpoint
from apex.contrib.multihead_attn import SelfMultiheadAttn
from apex import fused_dense
from model.layers.fused import FusedMlp
from file_utils import cached_path
from utils import get_rank

import model.layers.activations
from model.layers.activations import bias_gelu_impl as bias_gelu
from model.layers.activations import ACT2FN

from model.layers.embeddings import BertEmbeddings
from model.layers.layernorm import BertLayerNorm

import mhalib
from mha import *
from fmha import FMHA
from transformer_engine.pytorch import fp8_autocast, TransformerLayer, LayerNormMLP
from transformer_engine.pytorch import CudaRNGStatesTracker as TECudaRNGStatesTracker
from transformer_engine.common import recipe
import thunder

logger = logging.getLogger(__name__)
#torch._C._jit_set_profiling_mode(False)
#torch._C._jit_set_profiling_executor(False)
#torch._C._jit_override_can_fuse_on_cpu(True)
#torch._C._jit_override_can_fuse_on_gpu(True)

## use NV fuser

torch._C._jit_set_nvfuser_enabled(True)
torch._C._jit_set_texpr_fuser_enabled(False)
torch._C._jit_set_profiling_executor(True)
torch._C._jit_set_profiling_mode(True)
torch._C._jit_override_can_fuse_on_cpu(False)
torch._C._jit_override_can_fuse_on_gpu(False)

def remap_attn_names_tf(name):
    if 'attention' in name:
        ind = name.index("attention")
        if 'self' in name and 'query' in name and 'kernel' in name:
            name = name[:(ind+1)] + ['multi_head_attention', 'q_weight']
        if 'self' in name and 'query' in name and 'bias' in name:
            name = name[:(ind+1)] + ['multi_head_attention', 'q_bias']
        if 'self' in name and 'key' in name and 'kernel' in name:
            name = name[:(ind+1)] + ['multi_head_attention', 'k_weight']
        if 'self' in name and 'key' in name and 'bias' in name:
            name = name[:(ind+1)] + ['multi_head_attention', 'k_bias']
        if 'self' in name and 'value' in name and 'kernel' in name:
            name = name[:(ind+1)] + ['multi_head_attention', 'v_weight']
        if 'self' in name and 'value' in name and 'bias' in name:
            name = name[:(ind+1)] + ['multi_head_attention', 'v_bias']
        if 'output' in name and 'dense' in name and 'kernel' in name:
            name = name[:(ind+1)] + ['multi_head_attention', 'out_proj_weight']
        if 'output' in name and 'dense' in name and 'bias' in name:
            name = name[:(ind+1)] + ['multi_head_attention', 'out_proj_bias']
        if 'output' in name and 'LayerNorm' in name:
            name = name[:(ind+1)] + ['layer_norm'] + name[-1:]
    return name

def load_tf_weights_in_bert(model, tf_checkpoint_path, use_fast_mha=False):
    """ Load tf checkpoints in a pytorch model
    """
    try:
        import re
        import numpy as np
        import tensorflow as tf
    except ImportError:
        print("Loading a TensorFlow models in PyTorch, requires TensorFlow to be installed. Please see "
            "https://www.tensorflow.org/install/ for installation instructions.")
        raise
    tf_path = os.path.abspath(tf_checkpoint_path)
    if get_rank() == 0:
        print("Converting TensorFlow checkpoint from {}".format(tf_path))
    # Load weights from TF model
    init_vars = tf.train.list_variables(tf_path)
    names = []
    arrays = []
    for name, shape in init_vars:
        if get_rank() == 0:
            print("Loading TF weight {} with shape {}".format(name, shape))
        array = tf.train.load_variable(tf_path, name)
        names.append(name)
        arrays.append(array)

    # MHA params need to be treated separately
    if use_fast_mha:
        mha_params = ['q_weight', 'q_bias', 'k_weight', 'k_bias', 'v_weight', 'v_bias', 'out_proj_weight', 'out_proj_bias']
    else:
        mha_params = []

    for name, array in zip(names, arrays):
        name = name.split('/')
        # adam_v and adam_m are variables used in AdamWeightDecayOptimizer to calculated m and v
        # which are not required for using pretrained model
        if any(n in ["adam_v", "adam_m", "global_step", "LAMB", "LAMB_1", "beta1_power", "beta2_power"] for n in name):
            if get_rank() == 0:
                print("Skipping {}".format("/".join(name)))
            continue

        if use_fast_mha:
            name = remap_attn_names_tf(name)

        pointer = model
        for m_name in name:
            if re.fullmatch(r'[A-Za-z]+_\d+', m_name):
                l = re.split(r'_(\d+)', m_name)
            else:
                l = [m_name]
            if l[0] in mha_params:
                pointer = getattr(pointer, l[0])
            elif l[0] == 'kernel' or l[0] == 'gamma':
                pointer = getattr(pointer, 'weight')
            elif l[0] == 'output_bias' or l[0] == 'beta':
                pointer = getattr(pointer, 'bias')
            elif l[0] == 'output_weights':
                pointer = getattr(pointer, 'weight')
            else:
                pointer = getattr(pointer, l[0])
            if len(l) >= 2:
                num = int(l[1])
                pointer = pointer[num]
        if m_name[-11:] == '_embeddings':
            pointer = getattr(pointer, 'weight')
        elif m_name == 'kernel' or (m_name in mha_params and 'bias' not in m_name):
            array = np.ascontiguousarray(np.transpose(array))

        try:
            assert pointer.shape == array.shape
        except AssertionError as e:
            # If copying smaller into larger, assume padded and ok
            if reduce(mul, pointer.shape) > reduce(mul, array.shape):
                if get_rank() == 0:
                    print("Initialize padded PyTorch weight {}".format(name))
                pointer.data.zero_()

                def generate_slices():
                    slices = []
                    for i in range(array.ndim):
                        slices.append(slice(0, array.shape[i], 1))
                    return slices
                # pointer.data[generate_slices()] = torch.from_numpy(array)
                pointer.data[generate_slices()] = torch.from_numpy(array)
            else:
                e.args += (pointer.shape, array.shape)
                raise
        else:
            if get_rank() == 0:
                print("Initialize PyTorch weight {}".format(name))
            pointer.data = torch.from_numpy(array)
    return model

@torch.compile
def jit_dropout_add(x, residual, prob):
    # type: (Tensor, Tensor, float) -> Tensor
    #out = F.dropout(x, p=prob)
    out = torch.nn.functional.dropout(x, p=prob, training=True)
    out = residual + out
    return out

@thunder.jit
def jit_bias_dropout_add(x, bias, residual, prob) :
    # type: (Tensor, Tensor, Tensor, float) -> Tensor
    #out = F.dropout(x, p=prob)
    out = torch.nn.functional.dropout(x + bias, p=prob, training=True)
    out = residual + out
    return out

def fused_dropout_add(x, residual, prob, is_training) :
    # type: (Tensor, Tensor, float, bool) -> Tensor
    #out = F.dropout(x, p=prob, training=is_training)
    if is_training:
        out = jit_dropout_add(x, residual, prob)
    else:
        out = torch.nn.functional.dropout(x, p=prob, training=is_training)
        out = residual + out
    return out

def fused_bias_dropout_add(x, bias, residual, prob, is_training) :
    # type: (Tensor, Tensor, Tensor, float, bool) -> Tensor
    #out = F.dropout(x, p=prob, training=is_training)
    if is_training:
        out = jit_bias_dropout_add(x, bias, residual, prob, True)
    else:
        out = torch.nn.functional.dropout(x + bias, p=prob, training=is_training)
        out = residual + out
    return out

class LinearActivation(torch.nn.Linear):
    r"""Fused Linear and activation Module.
    """
    __constants__ = ['bias']

    def __init__(self, in_features, out_features, act='gelu', bias=True):
        super(LinearActivation, self).__init__(in_features, out_features, bias)
        self.act_fn = nn.Identity()                                                         #
        self.biased_act_fn = None                                                           # 
        if isinstance(act, str) or (sys.version_info[0] == 2 and isinstance(act, unicode)): # For TorchScript
            if bias and not 'bias' in act:                                                  # compatibility
                act = 'bias_' + act                                                         #
                self.biased_act_fn = ACT2FN[act]                                            #

            else:
                self.act_fn = ACT2FN[act]
        else:
            self.act_fn = act

    def forward(self, input):
        if not self.bias is None:
            return self.biased_act_fn(self.bias, nn.functional.linear(input, self.weight, None))
        else:
            return self.act_fn(F.linear(input, self.weight, self.bias))

class LinearDropoutAdd(torch.nn.Linear):
    r"""Fused Linear and activation Module.
    """
    __constants__ = ['bias']

    def __init__(self, in_features, out_features, bias=True, p=0.1, fused_bias_fc=False):
        super(LinearDropoutAdd, self).__init__(in_features, out_features, bias)
        self.p = p
        self.fused_bias_fc=fused_bias_fc
    def forward(self, input, residual):
        if self.bias is None or not self.fused_bias_fc:
            linear_out = nn.functional.linear(input, self.weight, None)
        else:
            shape_init = list(input.size())
            if 'fused_dense_function' in dir(fused_dense):
                linear_out = fused_dense.fused_dense_function(input.view(-1, shape_init[-1]), self.weight, self.bias)
            else:
                linear_out = fused_dense.fused_dense._fused_dense(input.view(-1, shape_init[-1]), self.weight, self.bias)
            shape_final = shape_init[:-1] + [list(linear_out.size())[-1]]
            linear_out = linear_out.view(shape_final)

           # linear_out = fused_dense.fused_dense_function(input, self.weight, self.bias)
        if self.bias is None:
            result = fused_dropout_add(linear_out, residual, self.p, is_training=self.training)
        else:
            if not self.fused_bias_fc:
                result = fused_bias_dropout_add(linear_out, self.bias.expand_as(residual), residual, self.p, is_training=self.training)
            else:
                result = fused_dropout_add(linear_out, residual, self.p, is_training=self.training)
        return result

class BertConfig(object):
    """Configuration class to store the configuration of a `BertModel`.
    """
    def __init__(self,
                 vocab_size_or_config_json_file,
                 hidden_size=768,
                 num_hidden_layers=12,
                 num_attention_heads=12,
                 intermediate_size=3072,
                 hidden_act="gelu",
                 hidden_dropout_prob=0.1,
                 attention_probs_dropout_prob=0.1,
                 max_position_embeddings=512,
                 type_vocab_size=2,
                 initializer_range=0.02):
        """Constructs BertConfig.

        Args:
            vocab_size_or_config_json_file: Vocabulary size of `inputs_ids` in `BertModel`.
            hidden_size: Size of the encoder layers and the pooler layer.
            num_hidden_layers: Number of hidden layers in the Transformer encoder.
            num_attention_heads: Number of attention heads for each attention layer in
                the Transformer encoder.
            intermediate_size: The size of the "intermediate" (i.e., feed-forward)
                layer in the Transformer encoder.
            hidden_act: The non-linear activation function (function or string) in the
                encoder and pooler. If string, "gelu", "relu" and "swish" are supported.
            hidden_dropout_prob: The dropout probabilitiy for all fully connected
                layers in the embeddings, encoder, and pooler.
            attention_probs_dropout_prob: The dropout ratio for the attention
                probabilities.
            max_position_embeddings: The maximum sequence length that this model might
                ever be used with. Typically set this to something large just in case
                (e.g., 512 or 1024 or 2048).
            type_vocab_size: The vocabulary size of the `token_type_ids` passed into
                `BertModel`.
            initializer_range: The sttdev of the truncated_normal_initializer for
                initializing all weight matrices.
        """
        if isinstance(vocab_size_or_config_json_file, str) or (sys.version_info[0] == 2
                        and isinstance(vocab_size_or_config_json_file, unicode)):
            with open(vocab_size_or_config_json_file, "r", encoding='utf-8') as reader:
                json_config = json.loads(reader.read())
            for key, value in json_config.items():
                self.__dict__[key] = value
        elif isinstance(vocab_size_or_config_json_file, int):
            self.vocab_size = vocab_size_or_config_json_file
            self.hidden_size = hidden_size
            self.num_hidden_layers = num_hidden_layers
            self.num_attention_heads = num_attention_heads
            self.hidden_act = hidden_act
            self.intermediate_size = intermediate_size
            self.hidden_dropout_prob = hidden_dropout_prob
            self.attention_probs_dropout_prob = attention_probs_dropout_prob
            self.max_position_embeddings = max_position_embeddings
            self.type_vocab_size = type_vocab_size
            self.initializer_range = initializer_range
        else:
            raise ValueError("First argument must be either a vocabulary size (int)"
                             "or the path to a pretrained model config file (str)")

    @classmethod
    def from_dict(cls, json_object):
        """Constructs a `BertConfig` from a Python dictionary of parameters."""
        config = BertConfig(vocab_size_or_config_json_file=-1)
        for key, value in json_object.items():
            config.__dict__[key] = value
        return config

    @classmethod
    def from_json_file(cls, json_file):
        """Constructs a `BertConfig` from a json file of parameters."""
        with open(json_file, "r", encoding='utf-8') as reader:
            text = reader.read()
        return cls.from_dict(json.loads(text))

    def __repr__(self):
        return str(self.to_json_string())

    def to_dict(self):
        """Serializes this instance to a Python dictionary."""
        output = copy.deepcopy(self.__dict__)
        return output

    def to_json_string(self):
        """Serializes this instance to a JSON string."""
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"


class BertSelfAttention(nn.Module):
    def __init__(self, config):
        super(BertSelfAttention, self).__init__()
        if config.hidden_size % config.num_attention_heads != 0:
            raise ValueError(
                "The hidden size (%d) is not a multiple of the number of attention "
                "heads (%d)" % (config.hidden_size, config.num_attention_heads))
        self.num_attention_heads = config.num_attention_heads
        self.attention_head_size = int(config.hidden_size / config.num_attention_heads)
        self.all_head_size = self.num_attention_heads * self.attention_head_size

        self.query = nn.Linear(config.hidden_size, self.all_head_size)
        self.key = nn.Linear(config.hidden_size, self.all_head_size)
        self.value = nn.Linear(config.hidden_size, self.all_head_size)

        self.dropout = nn.Dropout(config.attention_probs_dropout_prob)
        self.softmax = nn.Softmax(dim=-1)

    def transpose_for_scores(self, x):
        new_x_shape = x.size()[:-1] + (self.num_attention_heads, self.attention_head_size)
        x = x.view(*new_x_shape)
        return x.permute(0, 2, 1, 3)

    def transpose_key_for_scores(self, x):
        new_x_shape = x.size()[:-1] + (self.num_attention_heads, self.attention_head_size)
        x = x.view(*new_x_shape)
        return x.permute(0, 2, 3, 1)

    def forward(self, hidden_states, attention_mask):
        mixed_query_layer = self.query(hidden_states)
        mixed_key_layer = self.key(hidden_states)
        mixed_value_layer = self.value(hidden_states)

        query_layer = self.transpose_for_scores(mixed_query_layer)
        key_layer = self.transpose_key_for_scores(mixed_key_layer)
        value_layer = self.transpose_for_scores(mixed_value_layer)

        # Take the dot product between "query" and "key" to get the raw attention scores.
        attention_scores = torch.matmul(query_layer, key_layer)
        attention_scores = attention_scores / math.sqrt(self.attention_head_size)
        # Apply the attention mask is (precomputed for all layers in BertModel forward() function)
        attention_scores = attention_scores + attention_mask.unsqueeze(1).unsqueeze(2)
        #attention_scores = attention_scores - (1.0 - attention_mask.unsqueeze(1).unsqueeze(2).float()) * 10000.0

        # Normalize the attention scores to probabilities.
        attention_probs = self.softmax(attention_scores)

        # This is actually dropping out entire tokens to attend to, which might
        # seem a bit unusual, but is taken from the original Transformer paper.
        attention_probs = self.dropout(attention_probs)

        #context_layer = torch.einsum("bnft,btnh->bfnh", attention_probs, mixed_value_layer.view(1,512,16,64))
        context_layer = torch.matmul(attention_probs, value_layer)
        context_layer = context_layer.permute(0, 2, 1, 3).contiguous()
        new_context_layer_shape = context_layer.size()[:-2] + (self.all_head_size,)
        context_layer = context_layer.view(*new_context_layer_shape)
        return context_layer
        #return context_layer.reshape(context_layer.shape[:2] + (self.all_head_size,))


class BertSelfOutput(nn.Module):
    def __init__(self, config):
        super(BertSelfOutput, self).__init__()
        self.fuse_bias = config.fused_bias_mha
        self.fused_dropout_add = config.fused_dropout_add
        if not config.fused_bias_mha:
            self.dense = nn.Linear(config.hidden_size, config.hidden_size)
        else:
            self.dense = fused_dense.FusedDense(config.hidden_size, config.hidden_size)
        self.LayerNorm = BertLayerNorm(config.hidden_size, eps=1e-12)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.p = config.hidden_dropout_prob

    def forward(self, hidden_states, input_tensor):
        if not self.fuse_bias:
            hidden_states = self.dense(hidden_states)
        else:
            shape_init = list(hidden_states.size())
            hidden_states = self.dense(hidden_states.view(-1, shape_init[-1])).view(shape_init)
        if not self.fused_dropout_add:
            hidden_states = self.dropout(hidden_states)
            hidden_states = hidden_states + input_tensor
        else:
            hidden_states = fused_dropout_add(hidden_states, input_tensor, self.p, self.training)
        hidden_states = self.LayerNorm(hidden_states)
        return hidden_states

# This module uses Apex C++ multihead attention implementation with fusions. 
class FastBertAttention(nn.Module):
    def __init__(self, config):
        super(FastBertAttention, self).__init__()
        self.multi_head_attention = SelfMultiheadAttn(config.hidden_size, config.num_attention_heads, dropout = config.attention_probs_dropout_prob, bias=True, include_norm_add=False, impl='fast', separate_qkv_params=True, mask_additive=True)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.p = config.hidden_dropout_prob
        self.fused_dropout_add = config.fused_dropout_add
        self.layer_norm = BertLayerNorm(config.hidden_size, eps=1e-12)
    def forward(self, input_tensor, attention_mask):
        residual=input_tensor
        multi_head_attention_output,_ = self.multi_head_attention(query = input_tensor, key = input_tensor, value = input_tensor, key_padding_mask=attention_mask, need_weights=True,attn_mask = None, is_training = self.training)
        if self.fused_dropout_add:
            attention_output = fused_dropout_add(multi_head_attention_output, residual, self.p, self.training)
            attention_output = self.layer_norm(attention_output)
        else:
            attention_output = self.dropout(multi_head_attention_output)
            attention_output = self.layer_norm(attention_output + residual)

        return attention_output

class PadFMHABertAttention(nn.Module):
    def __init__(self, config):
        super(PadFMHABertAttention, self).__init__()
        self.self = FMHA(config)
        self.output = BertSelfOutput(config)
        self.max_s = config.max_seq_length
    def forward(self, input_tensor, cu_seqlens):

        # TODO flattening here might not be necessary
        hidden_size = input_tensor.shape[-1]
        input_flat = input_tensor.view(-1, hidden_size)
        self_output = self.self(input_flat, cu_seqlens, self.max_s, is_training=self.training)
        self_output = self_output.view(input_tensor.shape)

        attention_output = self.output(self_output, input_tensor)
        return attention_output

class UnpadFMHABertAttention(nn.Module):
    def __init__(self, config):
        super(UnpadFMHABertAttention, self).__init__()
        self.self = FMHA(config)
        self.output = BertSelfOutput(config)
    def forward(self, input_tensor, cu_seqlens, max_s, batch_size=None):
        self_output = self.self(input_tensor, cu_seqlens, max_s, is_training=self.training)
        attention_output = self.output(self_output, input_tensor)
        return attention_output

class FastUnpadBertAttention(nn.Module):
    def __init__(self, config):
        super(FastUnpadBertAttention, self).__init__()
        self.self = FastUnpadBertSelfAttention(config, enable_stream=config.enable_stream, enable_sync=False, fuse_mask=config.fuse_mask, fuse_scale=config.fuse_scale, fuse_qkv=config.fuse_qkv, fuse_dropout=config.fuse_dropout, apex_softmax=config.apex_softmax, pad=config.pad)
        self.output = BertSelfOutput(config)

    def forward(self, input_tensor, attention_mask, seqlen, batch):
        self_output = self.self(input_tensor, attention_mask, seqlen, batch, is_training = self.training)
        attention_output = self.output(self_output, input_tensor)
        return attention_output

class BertAttention(nn.Module):
    def __init__(self, config):
        super(BertAttention, self).__init__()
        self.self = BertSelfAttention(config)
        self.output = BertSelfOutput(config)

    def forward(self, input_tensor, attention_mask):
        self_output = self.self(input_tensor, attention_mask)
        attention_output = self.output(self_output, input_tensor)
        return attention_output


class BertIntermediate(nn.Module):
    def __init__(self, config):
        super(BertIntermediate, self).__init__()
        self.fused_gelu_bias = config.fused_gelu_bias
        self.fuse_bias = config.fused_bias_fc
        self.fuse_fc_gelu = config.fused_gemm_gelu
        if self.fuse_fc_gelu:
          self.dense = fused_dense.FusedDenseGeluDense(config.hidden_size, config.intermediate_size, config.hidden_size)
        else:
          if config.fused_gelu_bias:
              self.dense = LinearActivation(config.hidden_size, config.intermediate_size, act=config.hidden_act)
          else:
              if not self.fuse_bias:
                  self.dense = nn.Linear(config.hidden_size, config.intermediate_size)
              else:
                  self.dense = fused_dense.FusedDense(config.hidden_size, config.intermediate_size)
              if isinstance(config.hidden_act, str) or (sys.version_info[0] == 2 and isinstance(config.hidden_act, unicode)):
                  self.intermediate_act_fn = ACT2FN[config.hidden_act]
              else:
                  self.intermediate_act_fn = config.hidden_act

    def forward(self, hidden_states):
        if self.fuse_fc_gelu:
            shape_init = list(hidden_states.size())
            hidden_states = self.dense(hidden_states.view(-1, shape_init[-1]))
            shape_final = shape_init[:-1] + [list(hidden_states.size())[-1]]
            hidden_states = hidden_states.view(shape_final)
            #hidden_states = self.dense(hidden_states)
        else:
          if not self.fuse_bias:
              hidden_states = self.dense(hidden_states)
          else:
              shape_init = list(hidden_states.size())
              hidden_states = self.dense(hidden_states.view(-1, shape_init[-1]))
              shape_final = shape_init[:-1] + [list(hidden_states.size())[-1]]
              hidden_states = hidden_states.view(shape_final)
          if not self.fused_gelu_bias:
              hidden_states = self.intermediate_act_fn(hidden_states)
        return hidden_states


class BertOutput(nn.Module):
    def __init__(self, config):
        super(BertOutput, self).__init__()
        self.fuse_bias = config.fused_bias_fc
        self.fuse_fc_gelu = config.fused_gemm_gelu
        if not self.fuse_fc_gelu:
            if not config.fused_dropout_add:
                if not self.fuse_bias:
                    self.dense = nn.Linear(config.intermediate_size, config.hidden_size)
                else:
                    self.dense = fused_dense.FusedDense(config.intermediate_size, config.hidden_size)
            else:
                self.dense = LinearDropoutAdd(config.intermediate_size, config.hidden_size, bias=True, p=config.hidden_dropout_prob, fused_bias_fc=self.fuse_bias)
        self.LayerNorm = BertLayerNorm(config.hidden_size, eps=1e-12)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.p = config.hidden_dropout_prob
        self.fused_dropout_add = config.fused_dropout_add
    def forward(self, hidden_states, input_tensor):
        if self.fused_dropout_add:
            if not self.fuse_fc_gelu:
                hidden_states = self.dense(hidden_states, input_tensor)
            else:
                hidden_states = fused_dropout_add(hidden_states, input_tensor, self.p, is_training=self.training)
            hidden_states = self.LayerNorm(hidden_states)
            return hidden_states
        else:
            if not self.fuse_fc_gelu:
                if not self.fuse_bias:
                    hidden_states = self.dense(hidden_states)
                else:
                    shape_init = list(hidden_states.size())
                    hidden_states = self.dense(hidden_states.view(-1, shape_init[-1]))
                    shape_final = shape_init[:-1] + [list(hidden_states.size())[-1]]
                    hidden_states = hidden_states.view(shape_final)
            hidden_states = self.dropout(hidden_states)
            hidden_states = self.LayerNorm(hidden_states + input_tensor)
            return hidden_states

class BertTransformerLayer(TransformerLayer):
    def __init__(
        self,
        config,
    ) -> None:
        self.unpad = config.unpad
        self.pad_fmha = config.pad_fmha
        # FP8 Init
        self.h = config.num_attention_heads
        self.hidden_size = config.hidden_size
        self.d = self.hidden_size // self.h
        self.max_seq_len = config.max_seq_length
        self.rng_state_tracker = config.te_rng_state_tracker

        print(f"CUDARNGStateTracker {self.rng_state_tracker}")

        transformer_layer_args = {
            "hidden_size": self.hidden_size,
            "ffn_hidden_size": self.hidden_size * 4,
            "num_attention_heads": self.h,
            "layernorm_epsilon": 1e-12,
            "hidden_dropout": config.hidden_dropout_prob,
            "attention_dropout": config.attention_probs_dropout_prob,
            "self_attn_mask_type": "padding",
            "get_rng_state_tracker": lambda: self.rng_state_tracker,
            "apply_residual_connection_post_layernorm": True,
            "output_layernorm": True,
            "fuse_qkv_params": True,
            "qkv_weight_interleaved": False,
            "attn_input_format": "thd",
        }
        def prep_weights(state_dict, prefix, local_metadata, strict, missing_keys, unexpected_keys, error_msgs):
            prefix_end_pos = prefix.find('self_attention')
            assert prefix_end_pos > 0
            prefix = prefix[:prefix_end_pos]

            Wq = state_dict.pop(prefix + 'self_attention.query.weight')
            bq = state_dict.pop(prefix + 'self_attention.query.bias')

            Wk = state_dict.pop(prefix + 'self_attention.key.weight')
            bk = state_dict.pop(prefix + 'self_attention.key.bias')

            Wv = state_dict.pop(prefix + 'self_attention.value.weight')
            bv = state_dict.pop(prefix + 'self_attention.value.bias')

            weight = torch.cat([Wq.view(self.h, self.d, self.hidden_size),
                                Wk.view(self.h, self.d, self.hidden_size),
                                Wv.view(self.h, self.d, self.hidden_size)],
                               dim=0).reshape(config.hidden_size*3,config.hidden_size).contiguous()

            bias = torch.cat([bq.view(self.h, self.d),
                              bk.view(self.h, self.d),
                              bv.view(self.h, self.d)],
                             dim=0).reshape(3*config.hidden_size).contiguous()

            state_dict[prefix + 'self_attention.qkv.weight'] = weight
            state_dict[prefix + 'self_attention.qkv.bias'] = bias

        def remove_extra_states(state_dict, prefix, local_metadata, strict, missing_keys, unexpected_keys, error_msgs):
            prefix_end_pos = prefix.find('layernorm')
            assert prefix_end_pos > 0
            prefix = prefix[:prefix_end_pos]
            missing_keys.remove(prefix + 'self_attention.qkv._extra_state')
            missing_keys.remove(prefix + 'self_attention.proj._extra_state')
            missing_keys.remove(prefix + 'layernorm_mlp._extra_state')

        super().__init__(**transformer_layer_args)
        self.self_attention._register_load_state_dict_pre_hook(prep_weights)
        self.layernorm._register_load_state_dict_pre_hook(remove_extra_states)

    def forward(
        self,
        hidden_states: torch.Tensor,
        cu_seqlens: torch.Tensor,
    ) -> torch.Tensor:
        #ntokens = hidden_states.size(0)
        return super().forward(
                hidden_states,
                cu_seqlens_q=cu_seqlens,
                cu_seqlens_kv=cu_seqlens,
                max_seqlen_q=self.max_seq_len,
                max_seqlen_kv=self.max_seq_len,
            )

class BertTransformerLayer2(nn.Module):

    def __init__(self, config):
        super(BertTransformerLayer2, self).__init__()

        from te_layers import FP8_MHA, LayerNormMLP as LayerNormMLP_custom

        self.unpad = config.unpad
        self.pad_fmha = config.pad_fmha
        # FP8 Init
        self.h = config.num_attention_heads
        self.hidden_size = config.hidden_size
        self.d = self.hidden_size // self.h
        self.max_seq_len = config.max_seq_length

        def prep_weights(state_dict, prefix, local_metadata, strict, missing_keys, unexpected_keys, error_msgs):
            prefix_end_pos = prefix.find('attention')
            assert prefix_end_pos > 0
            prefix = prefix[:prefix_end_pos]

            Wq = state_dict.pop(prefix + 'attention.self.query.weight')
            bq = state_dict.pop(prefix + 'attention.self.query.bias')

            Wk = state_dict.pop(prefix + 'attention.self.key.weight')
            bk = state_dict.pop(prefix + 'attention.self.key.bias')

            Wv = state_dict.pop(prefix + 'attention.self.value.weight')
            bv = state_dict.pop(prefix + 'attention.self.value.bias')

            weight = torch.cat([Wq.view(self.h, self.d, self.hidden_size),
                                Wk.view(self.h, self.d, self.hidden_size),
                                Wv.view(self.h, self.d, self.hidden_size)],
                               dim=0).reshape(config.hidden_size*3,config.hidden_size).contiguous()

            bias = torch.cat([bq.view(self.h, self.d),
                              bk.view(self.h, self.d),
                              bv.view(self.h, self.d)],
                             dim=0).reshape(3*config.hidden_size).contiguous()

            state_dict[prefix + 'attention.qkv_weight'] = weight
            state_dict[prefix + 'attention.qkv_bias'] = bias

        def remove_extra_states(state_dict, prefix, local_metadata, strict, missing_keys, unexpected_keys, error_msgs):
            prefix_end_pos = prefix.find('output')
            assert prefix_end_pos > 0
            prefix = prefix[:prefix_end_pos]
            missing_keys.remove(prefix + 'attention._extra_state')
            missing_keys.remove(prefix + 'layernorm_mlp._extra_state')

        self.attention = FP8_MHA(config)
        self.p = config.hidden_dropout_prob

        self.layernorm_mlp = LayerNormMLP_custom(
                            config,
                            eps = 1e-12,
                            )
        self.attention._register_load_state_dict_pre_hook(prep_weights)
        self.output_LayerNorm = BertLayerNorm(config.hidden_size, eps=1e-12)
        self.output_LayerNorm._register_load_state_dict_pre_hook(remove_extra_states)

    def forward(self, hidden_states, attention_mask):
        attention_output = self.attention(hidden_states, attention_mask, self.max_seq_len)
        intermediate_output = self.layernorm_mlp(attention_output)
        layer_output = self.output_LayerNorm(intermediate_output)
        return layer_output

class BertLayer(nn.Module):
    def __init__(self, config):
        super(BertLayer, self).__init__()
        self.unpad = config.unpad
        if config.pad_fmha:
            self.attention = PadFMHABertAttention(config)
        elif config.unpad_fmha:
            self.attention = UnpadFMHABertAttention(config)
        elif config.fused_mha:
            self.attention = FastBertAttention(config)
        elif config.unpad:
            self.attention = FastUnpadBertAttention(config)
        else:
            self.attention = BertAttention(config)
        self.intermediate = BertIntermediate(config)
        self.output = BertOutput(config)

    # def forward(self, hidden_states, attention_mask, seqlen, batch):
    def forward(self, hidden_states, attention_mask, seqlen=None, batch=None):
        if self.unpad:
            attention_output = self.attention(hidden_states, attention_mask, seqlen, batch)
        else:
            attention_output = self.attention(hidden_states, attention_mask)
        intermediate_output = self.intermediate(attention_output)
        layer_output = self.output(intermediate_output, attention_output)
        return layer_output

class BertEncoder(nn.Module):
    def __init__(self, config):
        super(BertEncoder, self).__init__()
        self.fp8_recipe = None
        self.use_transformer_engine2 = config.use_transformer_engine2
        self.use_transformer_engine = config.use_transformer_engine
        self.ntokens = None
        fp8_format = recipe.Format.HYBRID
        fp8_margin = 0
        fp8_interval = 1
        self.fp8_recipe = recipe.DelayedScaling(
            margin=fp8_margin,
            interval=fp8_interval,
            fp8_format=fp8_format,
            amax_history_len=1,
            amax_compute_algo="most_recent",
            reduce_amax=False,
            fp8_dpa=True,
            fp8_mha=False,
        )
        if config.use_transformer_engine:
            self.layer = nn.ModuleList([BertTransformerLayer(config) for _ in range(config.num_hidden_layers)])
        elif config.use_transformer_engine2:
            layer = BertTransformerLayer2(config)
            self.layer = nn.ModuleList([copy.deepcopy(layer) for _ in range(config.num_hidden_layers)])
        else:
            layer = BertLayer(config)
            self.layer = nn.ModuleList([copy.deepcopy(layer) for _ in range(config.num_hidden_layers)])

        self.num_attention_heads = config.num_attention_heads
        self.fused_mha=config.fused_mha
        self.unpad=config.unpad
        self.unpad_fmha = config.unpad_fmha
        self.pad_fmha = config.pad_fmha
        self.pad = config.pad
        self.fuse_mask = config.fuse_mask
        self.enable_stream= config.enable_stream

    def forward(self, hidden_states, attention_mask, output_all_encoded_layers=False, checkpoint_activations=False):

        # Unpad inputs and mask. It will remove tokens that are padded. Assume ntokens is total number of tokens (padded and non-padded)
        # and ntokens_unpad is total number of non-padded tokens. Then unpadding performs the following compression of the inputs:
        #        hidden_states[ntokens,hidden] -> hidden_states[ntokens_unpad,hidden]
        batch = None
        seqlen = None
        maxseqlen = hidden_states.shape[1]
        hidden_size = hidden_states.shape[2]
        if self.unpad:
            batch = hidden_states.shape[0]
            attention_indices, attention_mask, seqlen, ntokens, cu_seqlens, actual_seqlens, maxseqlen_in_batch = generate_mask(attention_mask, self.num_attention_heads, pad=self.pad, fuse_mask=self.fuse_mask, unpad_fmha=self.unpad_fmha)
            if self.pad == True and self.enable_stream == False:
                hidden_states = hidden_states.view(batch,maxseqlen,hidden_size).permute(1,0,2).contiguous().view(batch*maxseqlen,hidden_size).contiguous()
            if self.pad == True and self.enable_stream == True:
                hidden_states = hidden_states.view(batch*maxseqlen,hidden_size)
            if self.pad == False:
                hidden_states = UnpadInput.apply(hidden_states.view(batch*maxseqlen, hidden_size).contiguous(), attention_indices, batch, maxseqlen, hidden_size, ntokens)
                if self.use_transformer_engine or self.use_transformer_engine2:
                    npad = 256 - ((ntokens-1) % 256) - 1
                    hidden_states = torch.nn.functional.pad(hidden_states, (0,0,0,npad))
        elif seqlen is None and batch is None and (self.use_transformer_engine or self.use_transformer_engine2):
            hidden_states = hidden_states.view(-1,hidden_size)

        all_encoder_layers = []
        def custom(start, end):
            def custom_forward(*inputs):
                layers = self.layer[start:end]
                x_ = inputs[0]
                for layer in layers:
                    x_ = layer(x_, inputs[1])
                return x_
            return custom_forward

        if checkpoint_activations:
            l = 0
            num_layers = len(self.layer)
            chunk_length = math.ceil(math.sqrt(num_layers))
            while l < num_layers:
                hidden_states = checkpoint.checkpoint(custom(l, l+chunk_length), hidden_states, attention_mask*1)
                l += chunk_length
            # decoder layers
        else:
            if self.fused_mha and not self.pad_fmha:
                hidden_states = hidden_states.permute(1,0,2).contiguous()
            if self.use_transformer_engine or self.use_transformer_engine2:
                with nullcontext():
                    for i,layer_module in enumerate(self.layer):
                        if seqlen is None and batch is None:
                            hidden_states = layer_module(hidden_states, attention_mask)
                        else:
                            assert seqlen is not None
                            assert batch is not None
                            if self.unpad_fmha:
                                hidden_states = layer_module(hidden_states, cu_seqlens)
                            else:
                                hidden_states = layer_module(hidden_states, attention_mask)
    
                        if output_all_encoded_layers:
                            if self.fused_mha and not self.pad_fmha:
                                all_encoder_layers.append(hidden_states.permute(1,0,2).contiguous())
                            else:
                                all_encoder_layers.append(hidden_states)
            else:
                for i,layer_module in enumerate(self.layer):
                    if seqlen is None and batch is None:
                        hidden_states = layer_module(hidden_states, attention_mask)
                    else:
                        assert seqlen is not None
                        assert batch is not None
                        if self.unpad_fmha:
                            hidden_states = layer_module(hidden_states, cu_seqlens, maxseqlen_in_batch)
                        else:
                            hidden_states = layer_module(hidden_states, attention_mask, seqlen, batch)
    
                    if output_all_encoded_layers:
                        if self.fused_mha and not self.pad_fmha:
                            all_encoder_layers.append(hidden_states.permute(1,0,2).contiguous())
                        else:
                            all_encoder_layers.append(hidden_states)

        # Pad inputs and mask. It will insert back zero-padded tokens. Assume ntokens is total number of tokens (padded and non-padded)
        # and ntokens_unpad is total number of non-padded tokens. Then padding performs the following de-compression:
        #        hidden_states[ntokens_unpad,hidden] -> hidden_states[ntokens,hidden]
        if self.unpad:
            if self.pad == True and self.enable_stream == False:
                hidden_states = hidden_states.view(maxseqlen,batch,hidden_size).permute(1,0,2).contiguous().view(batch,maxseqlen,hidden_size).contiguous()
            if self.pad == True and self.enable_stream == True:
                hidden_states = hidden_states.view(batch,maxseqlen,hidden_size)
            if self.pad == False:
                if self.use_transformer_engine or self.use_transformer_engine2:
                    hidden_states = hidden_states[:ntokens,:]
                hidden_states = PadInput.apply(hidden_states, attention_indices, batch, maxseqlen, hidden_size, ntokens).view(batch, maxseqlen, hidden_size).contiguous()
        elif seqlen is None and batch is None and (self.use_transformer_engine or self.use_transformer_engine2):
            hidden_states = hidden_states.view(-1,maxseqlen,hidden_size)

        if not output_all_encoded_layers or checkpoint_activations:
            if self.fused_mha and not self.pad_fmha:
                all_encoder_layers.append(hidden_states.permute(1,0,2).contiguous())
            else:
                all_encoder_layers.append(hidden_states)
        return tuple(all_encoder_layers)

class BertPooler(nn.Module):
    def __init__(self, config):
        super(BertPooler, self).__init__()
        self.dense = nn.Linear(config.hidden_size, config.hidden_size)
        self.activation = nn.Tanh()
        self.pad_fmha = config.pad_fmha
        self.hidden_size = config.hidden_size

    def forward(self, hidden_states, cu_seqlens):
        # We "pool" the model by simply taking the hidden state corresponding
        # to the first token.
        if self.pad_fmha:
            first_token_tensor = torch.index_select(hidden_states.view(-1, self.hidden_size), 0, cu_seqlens[:-1])
        else:
            first_token_tensor = hidden_states[:, 0]
        pooled_output = self.dense(first_token_tensor)
        pooled_output = self.activation(pooled_output)
        return pooled_output


class BertPredictionHeadTransform(nn.Module):
    def __init__(self, config):
        super(BertPredictionHeadTransform, self).__init__()
        self.dense = nn.Linear(config.hidden_size, config.hidden_size)
        if isinstance(config.hidden_act, str) or (sys.version_info[0] == 2 and isinstance(config.hidden_act, unicode)):
            self.transform_act_fn = ACT2FN[config.hidden_act]
        else:
            self.transform_act_fn = config.hidden_act
        self.LayerNorm = BertLayerNorm(config.hidden_size, eps=1e-12)

    def forward(self, hidden_states):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.transform_act_fn(hidden_states)
        hidden_states = self.LayerNorm(hidden_states)
        return hidden_states


class BertLMPredictionHead(nn.Module):
    def __init__(self, config, bert_model_embedding_weights):
        super(BertLMPredictionHead, self).__init__()
        self.transform = BertPredictionHeadTransform(config)
        self.fused_fc = config.fused_bias_fc_loss_head
        # The output weights are the same as the input embeddings, but there is
        # an output-only bias for each token.
        if not config.fused_bias_fc_loss_head:
            self.decoder = nn.Linear(bert_model_embedding_weights.size(1),
                                 bert_model_embedding_weights.size(0),
                                 bias=False)
            self.bias = nn.Parameter(torch.zeros(bert_model_embedding_weights.size(0)))
        else:
            self.decoder = FusedMlp(bert_model_embedding_weights.size(1), bert_model_embedding_weights.size(0)) 
        self.decoder.weight = bert_model_embedding_weights

    def forward(self, hidden_states):
        hidden_states = self.transform(hidden_states)
        if not self.fused_fc:
            hidden_states = self.decoder(hidden_states) + self.bias   
        else:
            shape_init = list(hidden_states.size())
            hidden_states = self.decoder(hidden_states.view(-1, shape_init[-1]))
            shape_final = shape_init[:-1] + [list(hidden_states.size())[-1]]
            hidden_states = hidden_states.view(shape_final)
        return hidden_states


class BertOnlyMLMHead(nn.Module):
    def __init__(self, config, bert_model_embedding_weights):
        super(BertOnlyMLMHead, self).__init__()
        self.predictions = BertLMPredictionHead(config, bert_model_embedding_weights)

    def forward(self, sequence_output):
        prediction_scores = self.predictions(sequence_output)
        return prediction_scores


class BertOnlyNSPHead(nn.Module):
    def __init__(self, config):
        super(BertOnlyNSPHead, self).__init__()
        self.seq_relationship = nn.Linear(config.hidden_size, 2)

    def forward(self, pooled_output):
        seq_relationship_score = self.seq_relationship(pooled_output)
        return seq_relationship_score


class BertPreTrainingHeads(nn.Module):
    def __init__(self, config, bert_model_embedding_weights):
        super(BertPreTrainingHeads, self).__init__()
        self.predictions = BertLMPredictionHead(config, bert_model_embedding_weights)
        self.seq_relationship = nn.Linear(config.hidden_size, 2)
        self.dense_seq_output = config.dense_seq_output
        self.eval_cuda_graph = config.eval_cuda_graph

    def forward(self, sequence_output, pooled_output, masked_lm_labels, masked_lm_labels_dense_idx=None):
        skip_dense_for_eval = self.eval_cuda_graph and not self.training
        if self.dense_seq_output and not skip_dense_for_eval:
            masked_lm_labels_dense_idx = torch.nonzero(masked_lm_labels.view(-1) != 0, as_tuple=False).squeeze() \
                if masked_lm_labels_dense_idx is None else masked_lm_labels_dense_idx
            sequence_flattened = torch.index_select(sequence_output.view(-1,sequence_output.shape[-1]), 0, masked_lm_labels_dense_idx)
            sequence_output = sequence_flattened
        prediction_scores = self.predictions(sequence_output)
        seq_relationship_score = self.seq_relationship(pooled_output)

        return prediction_scores, seq_relationship_score


class BertPreTrainedModel(nn.Module):
    """ An abstract class to handle weights initialization and
        a simple interface for dowloading and loading pretrained models.
    """
    def __init__(self, config, *inputs, **kwargs):
        super(BertPreTrainedModel, self).__init__()
        if not isinstance(config, BertConfig):
            raise ValueError(
                "Parameter config in `{}(config)` should be an instance of class `BertConfig`. "
                "To create a model from a Google pretrained model use "
                "`model = {}.from_pretrained(PRETRAINED_MODEL_NAME)`".format(
                    self.__class__.__name__, self.__class__.__name__
                ))
        self.config = config

        # we want to make sure vocab size is padded to % 8 == 0
        if self.config.vocab_size % 8 != 0:
            self.config.vocab_size += 8 - (self.config.vocab_size % 8)
            if get_rank == 0:
                print(f'Padded vocab_size to : {self.config.vocab_size}')

    def init_bert_weights(self, module):
        """ Initialize the weights.
        """
        if isinstance(module, (nn.Linear, nn.Embedding)):
            # Slightly different from the TF version which uses truncated_normal for initialization
            # cf https://github.com/pytorch/pytorch/pull/5617
            module.weight.data.normal_(mean=0.0, std=self.config.initializer_range)
        elif isinstance(module, BertLayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)
        if isinstance(module, nn.Linear) and module.bias is not None:
            module.bias.data.zero_()

    @classmethod
    def from_pretrained(cls, pretrained_checkpoint, state_dict=None, cache_dir=None,
                        from_tf=False, config=None, *inputs, **kwargs):
        """
        Instantiate a BertPreTrainedModel from a pre-trained model file or a pytorch state dict.
        Download and cache the pre-trained model file if needed.

        Params:
            pretrained_model_name_or_path: either:
                - a path or url to a pretrained model archive containing:
                    . `bert_config.json` a configuration file for the model
                    . `pytorch_model.bin` a PyTorch dump of a BertForPretraining instance
                - a path or url to a pretrained model archive containing:
                    . `bert_config.json` a configuration file for the model
                    . `model.chkpt` a TensorFlow checkpoint
            from_tf: should we load the weights from a locally saved TensorFlow checkpoint
            cache_dir: an optional path to a folder in which the pre-trained models will be cached.
            state_dict: an optional state dictionnary (collections.OrderedDict object) to use instead of Google pre-trained models
            *inputs, **kwargs: additional input for the specific Bert class
                (ex: num_labels for BertForSequenceClassification)
        """
        logger.info("loading archive file {}".format(pretrained_checkpoint))
        assert config, "BERT configuration file must be provided to from_pretraining()"
        logger.info("Model config {}".format(config))
        # Instantiate model.
        model = cls(config, *inputs, **kwargs)
        if state_dict is None and not from_tf:
            state_dict = torch.load(pretrained_checkpoint, map_location='cpu' if not torch.cuda.is_available() else None)
        if from_tf:
            # Directly load from a TensorFlow checkpoint
            return load_tf_weights_in_bert(model, pretrained_checkpoint, use_fast_mha=config.fused_mha)
        # Load from a PyTorch state_dict
        old_keys = []
        new_keys = []
        # print(f'loading keys: {state_dict.keys()}')
        for key in state_dict.keys():
            new_key = None
            if 'gamma' in key:
                new_key = key.replace('gamma', 'weight')
            if 'beta' in key:
                new_key = key.replace('beta', 'bias')
            if new_key:
                old_keys.append(key)
                new_keys.append(new_key)
        for old_key, new_key in zip(old_keys, new_keys):
            state_dict[new_key] = state_dict.pop(old_key)

        missing_keys = []
        unexpected_keys = []
        error_msgs = []
        # copy state_dict so _load_from_state_dict can modify it
        metadata = getattr(state_dict, '_metadata', None)
        state_dict = state_dict.copy()
        if metadata is not None:
            state_dict._metadata = metadata

        def load(module, prefix=''):
            local_metadata = {} if metadata is None else metadata.get(prefix[:-1], {})
            module._load_from_state_dict(
                state_dict, prefix, local_metadata, True, missing_keys, unexpected_keys, error_msgs)
            for name, child in module._modules.items():
                if child is not None:
                    load(child, prefix + name + '.')
        start_prefix = ''
        if not hasattr(model, 'bert') and any(s.startswith('bert.') for s in state_dict.keys()):
            start_prefix = 'bert.'
        load(model, prefix=start_prefix)
        if len(missing_keys) > 0:
            logger.info("Weights of {} not initialized from pretrained model: {}".format(
                model.__class__.__name__, missing_keys))
        if len(unexpected_keys) > 0:
            logger.info("Weights from pretrained model not used in {}: {}".format(
                model.__class__.__name__, unexpected_keys))
        if len(error_msgs) > 0:
            raise RuntimeError('Error(s) in loading state_dict for {}:\n\t{}'.format(
                               model.__class__.__name__, "\n\t".join(error_msgs)))
        return model


class BertModel(BertPreTrainedModel):
    """BERT model ("Bidirectional Embedding Representations from a Transformer").

    Params:
        config: a BertConfig class instance with the configuration to build a new model

    Inputs:
        `input_ids`: a torch.LongTensor of shape [batch_size, sequence_length]
            with the word token indices in the vocabulary(see the tokens preprocessing logic in the scripts
            `extract_features.py`, `run_classifier.py` and `run_squad.py`)
        `token_type_ids`: an optional torch.LongTensor of shape [batch_size, sequence_length] with the token
            types indices selected in [0, 1]. Type 0 corresponds to a `sentence A` and type 1 corresponds to
            a `sentence B` token (see BERT paper for more details).
        `attention_mask`: an optional torch.LongTensor of shape [batch_size, sequence_length] with indices
            selected in [0, 1]. It's a mask to be used if the input sequence length is smaller than the max
            input sequence length in the current batch. It's the mask that we typically use for attention when
            a batch has varying length sentences.
        `output_all_encoded_layers`: boolean which controls the content of the `encoded_layers` output as described below. Default: `True`.

    Outputs: Tuple of (encoded_layers, pooled_output)
        `encoded_layers`: controled by `output_all_encoded_layers` argument:
            - `output_all_encoded_layers=True`: outputs a list of the full sequences of encoded-hidden-states at the end
                of each attention block (i.e. 12 full sequences for BERT-base, 24 for BERT-large), each
                encoded-hidden-state is a torch.FloatTensor of size [batch_size, sequence_length, hidden_size],
            - `output_all_encoded_layers=False`: outputs only the full sequence of hidden-states corresponding
                to the last attention block of shape [batch_size, sequence_length, hidden_size],
        `pooled_output`: a torch.FloatTensor of size [batch_size, hidden_size] which is the output of a
            classifier pretrained on top of the hidden state associated to the first character of the
            input (`CLS`) to train on the Next-Sentence task (see BERT's paper).

    Example usage:
    ```python
    # Already been converted into WordPiece token ids
    input_ids = torch.LongTensor([[31, 51, 99], [15, 5, 0]])
    input_mask = torch.LongTensor([[1, 1, 1], [1, 1, 0]])
    token_type_ids = torch.LongTensor([[0, 0, 1], [0, 1, 0]])

    config = modeling.BertConfig(vocab_size_or_config_json_file=32000, hidden_size=768,
        num_hidden_layers=12, num_attention_heads=12, intermediate_size=3072)

    model = modeling.BertModel(config=config)
    all_encoder_layers, pooled_output = model(input_ids, token_type_ids, input_mask)
    ```
    """
    def __init__(self, config):
        super(BertModel, self).__init__(config)
        self.embeddings = BertEmbeddings(config)
        self.encoder = BertEncoder(config)
        self.pooler = BertPooler(config)
        self.apply(self.init_bert_weights)
        self.unpad = config.unpad
        self.pad_fmha = config.pad_fmha

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, position_ids=None, attention_mask_orig=None, output_all_encoded_layers=True, checkpoint_activations=False):
        if attention_mask is None:
            attention_mask = torch.ones_like(input_ids)
        if token_type_ids is None:
            token_type_ids = torch.zeros_like(input_ids)

        # We create a 3D attention mask from a 2D tensor mask.
        # Sizes are [batch_size, 1, 1, to_seq_length]
        # So we can broadcast to [batch_size, num_heads, from_seq_length, to_seq_length]
        # this attention mask is more simple than the triangular masking of causal attention
        # used in OpenAI GPT, we just need to prepare the broadcast dimension here.
        extended_attention_mask = attention_mask#.unsqueeze(1).unsqueeze(2)
        extended_attention_mask_orig = attention_mask_orig

        # Since attention_mask is 1.0 for positions we want to attend and 0.0 for
        # masked positions, this operation will create a tensor which is 0.0 for
        # positions we want to attend and -10000.0 for masked positions.
        # Since we are adding it to the raw scores before the softmax, this is
        # effectively the same as removing these entirely.
        if self.unpad == False and not self.pad_fmha:
            extended_attention_mask = extended_attention_mask.to(dtype=next(self.parameters()).dtype) # fp16 compatibility
            extended_attention_mask = (1.0 - extended_attention_mask) * -10000.0
            if attention_mask_orig is not None:
                extended_attention_mask_orig = extended_attention_mask_orig.to(dtype=next(self.parameters()).dtype) # fp16 compatibility
                extended_attention_mask_orig = (1.0 - extended_attention_mask_orig) * -10000.0

        embedding_output = self.embeddings(input_ids, token_type_ids, position_ids)
        fp8_context = nullcontext()
        if self.encoder.use_transformer_engine or self.encoder.use_transformer_engine2:
            fp8_context = fp8_autocast(enabled=True, fp8_recipe=self.encoder.fp8_recipe)
        with fp8_context:
            encoded_layers = self.encoder(embedding_output,
                                      extended_attention_mask)
        sequence_output = encoded_layers[-1]
        pooled_output = self.pooler(sequence_output, extended_attention_mask if attention_mask_orig is None else extended_attention_mask_orig)
        if not output_all_encoded_layers:
            encoded_layers = encoded_layers[-1]
        return encoded_layers, pooled_output


class BertForPretraining(BertPreTrainedModel):
    """BERT model with pre-training heads.
    This module comprises the BERT model followed by the two pre-training heads:
        - the masked language modeling head, and
        - the next sentence classification head.

    Params:
        config: a BertConfig class instance with the configuration to build a new model.

    Inputs:
        `input_ids`: a torch.LongTensor of shape [batch_size, sequence_length]
            with the word token indices in the vocabulary(see the tokens preprocessing logic in the scripts
            `extract_features.py`, `run_classifier.py` and `run_squad.py`)
        `token_type_ids`: an optional torch.LongTensor of shape [batch_size, sequence_length] with the token
            types indices selected in [0, 1]. Type 0 corresponds to a `sentence A` and type 1 corresponds to
            a `sentence B` token (see BERT paper for more details).
        `attention_mask`: an optional torch.LongTensor of shape [batch_size, sequence_length] with indices
            selected in [0, 1]. It's a mask to be used if the input sequence length is smaller than the max
            input sequence length in the current batch. It's the mask that we typically use for attention when
            a batch has varying length sentences.
        `masked_lm_labels`: optional masked language modeling labels: torch.LongTensor of shape [batch_size, sequence_length]
            with indices selected in [-1, 0, ..., vocab_size]. All labels set to -1 are ignored (masked), the loss
            is only computed for the labels set in [0, ..., vocab_size]
        `next_sentence_label`: optional next sentence classification loss: torch.LongTensor of shape [batch_size]
            with indices selected in [0, 1].
            0 => next sentence is the continuation, 1 => next sentence is a random sentence.

    Outputs:
        if `masked_lm_labels` and `next_sentence_label` are not `None`:
            Outputs the total_loss which is the sum of the masked language modeling loss and the next
            sentence classification loss.
        if `masked_lm_labels` or `next_sentence_label` is `None`:
            Outputs a tuple comprising
            - the masked language modeling logits of shape [batch_size, sequence_length, vocab_size], and
            - the next sentence classification logits of shape [batch_size, 2].

    Example usage:
    ```python
    # Already been converted into WordPiece token ids
    input_ids = torch.LongTensor([[31, 51, 99], [15, 5, 0]])
    input_mask = torch.LongTensor([[1, 1, 1], [1, 1, 0]])
    token_type_ids = torch.LongTensor([[0, 0, 1], [0, 1, 0]])

    config = BertConfig(vocab_size_or_config_json_file=32000, hidden_size=768,
        num_hidden_layers=12, num_attention_heads=12, intermediate_size=3072)

    model = BertForPretraining(config)
    masked_lm_logits_scores, seq_relationship_logits = model(input_ids, token_type_ids, input_mask)
    ```
    """
    def __init__(self, config):
        super(BertForPretraining, self).__init__(config)
        self.bert = BertModel(config)
        self.cls = BertPreTrainingHeads(config, self.bert.embeddings.word_embeddings.weight)
        self.apply(self.init_bert_weights)
        self.dense_seq_output = config.dense_seq_output
        self.eval_cuda_graph = config.eval_cuda_graph
    def forward(self, input_ids, token_type_ids=None, attention_mask=None, masked_lm_labels=None, next_sentence_label=None, checkpoint_activations=False):
        sequence_output, pooled_output = self.bert(input_ids, token_type_ids, attention_mask,
                                                   output_all_encoded_layers=False, checkpoint_activations=checkpoint_activations)
        # if dense_seq_output, prediction scores returned by this function is already masked out with masked_lm_labels, and first dimension is flattened
        prediction_scores, seq_relationship_score = self.cls(sequence_output, pooled_output, masked_lm_labels)

        if masked_lm_labels is not None and next_sentence_label is not None:
            loss_fct = CrossEntropyLoss(ignore_index=0)

            masked_lm_labels_flat = masked_lm_labels.view(-1)
            skip_dense_for_eval = self.eval_cuda_graph and not self.training

            if self.dense_seq_output and not skip_dense_for_eval:
                masked_lm_labels_dense = masked_lm_labels_flat[masked_lm_labels_flat != 0]
                masked_lm_loss = loss_fct(prediction_scores.view(-1, self.config.vocab_size), masked_lm_labels_dense)
            else:
                masked_lm_loss = loss_fct(prediction_scores.view(-1, self.config.vocab_size), masked_lm_labels.view(-1))
            nsp_loss_fct = CrossEntropyLoss(ignore_index=-1)
            next_sentence_loss = nsp_loss_fct(seq_relationship_score.view(-1, 2), next_sentence_label.view(-1))
            total_loss = masked_lm_loss + next_sentence_loss
            total_loss = total_loss.float()

            # Masked Language Model Accuracy
            mlm_labels = masked_lm_labels_flat[masked_lm_labels_flat != 0]
            if not self.dense_seq_output or skip_dense_for_eval:
                prediction_scores_flat = prediction_scores.view(-1, prediction_scores.shape[-1])
                mlm_predictions_scores = prediction_scores_flat[masked_lm_labels_flat != 0]
                mlm_predictions = mlm_predictions_scores.argmax(dim=-1)
            else:
                mlm_predictions = prediction_scores.argmax(dim=-1)

            mlm_acc = (mlm_predictions == mlm_labels).sum(dtype=torch.float) / mlm_labels.numel()

            return total_loss, mlm_acc, mlm_labels.numel()
        else: #TODO: Handle this path for dense sequence output as well
            return prediction_scores, seq_relationship_score

class BertForPreTrainingModelOnly(nn.Module):
    def __init__(self, config):
        super(BertForPreTrainingModelOnly, self).__init__()
        self.bert = BertModel(config)
    def forward(self, input_ids, token_type_ids=None, attention_mask=None, position_ids=None, attention_mask_orig=None, checkpoint_activations=False):
        sequence_output, pooled_output = self.bert(input_ids, token_type_ids, attention_mask, position_ids, attention_mask_orig,
                                                   output_all_encoded_layers=False, checkpoint_activations=checkpoint_activations)
        return sequence_output, pooled_output

class BertForPreTrainingHeadsOnly(nn.Module):
    def __init__(self, config, bert_model_embedding_weights):
        super(BertForPreTrainingHeadsOnly, self).__init__()
        self.cls = BertPreTrainingHeads(config, bert_model_embedding_weights)
        self.dense_seq_output = config.dense_seq_output
        self.eval_cuda_graph = config.eval_cuda_graph
        self.config = config
    def forward(self, sequence_output, pooled_output, masked_lm_labels=None, next_sentence_label=None, masked_lm_labels_dense_idx=None):
        # if dense_seq_output, prediction scores returned by this function is already masked out with masked_lm_labels, and first dimension is flattened
        prediction_scores, seq_relationship_score = self.cls(sequence_output, pooled_output, masked_lm_labels, masked_lm_labels_dense_idx)
        global count
        if masked_lm_labels is not None and next_sentence_label is not None:
            loss_fct = CrossEntropyLoss(ignore_index=0)
            masked_lm_labels_flat = masked_lm_labels.view(-1)

            skip_dense_for_eval = self.eval_cuda_graph and not self.training
            if self.dense_seq_output and not skip_dense_for_eval:
                masked_lm_labels_dense = masked_lm_labels_flat[masked_lm_labels_dense_idx]
                masked_lm_loss = loss_fct(prediction_scores.view(-1, self.config.vocab_size), masked_lm_labels_dense)
            else:
                masked_lm_loss = loss_fct(prediction_scores.view(-1, self.config.vocab_size), masked_lm_labels.view(-1))

            nsp_loss_fct = CrossEntropyLoss(ignore_index=-1) 
            next_sentence_loss = nsp_loss_fct(seq_relationship_score.view(-1, 2), next_sentence_label.view(-1))
            total_loss = (masked_lm_loss + next_sentence_loss).float()
            # Masked Language Model Accuracy
            valid_mask =(masked_lm_labels_flat != 0).int()
            num_valid = valid_mask.sum(dtype=torch.int64)

            if not self.dense_seq_output or skip_dense_for_eval:
                mlm_labels = masked_lm_labels_flat
                prediction_scores_flat = prediction_scores.view(-1, prediction_scores.shape[-1])
                mlm_predictions_scores = prediction_scores_flat
                mlm_predictions = mlm_predictions_scores.argmax(dim=-1)
                mlm_acc = ((mlm_predictions == mlm_labels)*valid_mask).sum(dtype=torch.float) / num_valid

            else:
                mlm_labels = masked_lm_labels_flat[masked_lm_labels_dense_idx]
                mlm_predictions = prediction_scores.argmax(dim=-1).view(-1)
                mlm_acc = (mlm_predictions == mlm_labels).sum(dtype=torch.float) / num_valid
            return total_loss, mlm_acc, num_valid

        else: #TODO: Handle this path for dense sequence output as well
            return prediction_scores, seq_relationship_score

class BertForPreTrainingSegmented(BertPreTrainedModel):
    def __init__(self, config):
        super(BertForPreTrainingSegmented, self).__init__(config)
        self.bert_model_segment = BertForPreTrainingModelOnly(config)
        self.heads_only_segment = BertForPreTrainingHeadsOnly(config, self.bert_model_segment.bert.embeddings.word_embeddings.weight)
        self.dense_seq_output = config.dense_seq_output
        self.eval_cuda_graph = config.eval_cuda_graph
        self.apply(self.init_bert_weights)
    def forward(self, input_ids, token_type_ids=None, attention_mask=None, masked_lm_labels=None, next_sentence_label=None, position_ids=None, attention_mask_orig=None, next_sentence_label_orig=None, checkpoint_activations=False):
        assert checkpoint_activations == False, "cuda-graphs: reattach passing of checkpoint_activations when function.py starts handling non-Tensor inputs"
        masked_lm_labels_dense_idx = None
        skip_dense_for_eval = self.eval_cuda_graph and not self.training
        if self.dense_seq_output and not skip_dense_for_eval:
            masked_lm_labels_dense_idx = torch.nonzero(masked_lm_labels.view(-1) != 0, as_tuple=False).squeeze()
        if position_ids is None:
            sequence_output, pooled_output = self.bert_model_segment(input_ids, token_type_ids, attention_mask, attention_mask_orig)
        else:
            sequence_output, pooled_output = self.bert_model_segment(input_ids, token_type_ids, attention_mask, position_ids, attention_mask_orig)
        return self.heads_only_segment(sequence_output, pooled_output, masked_lm_labels, next_sentence_label if next_sentence_label_orig is None else next_sentence_label_orig, masked_lm_labels_dense_idx)

class BertForMaskedLM(BertPreTrainedModel):
    """BERT model with the masked language modeling head.
    This module comprises the BERT model followed by the masked language modeling head.

    Params:
        config: a BertConfig class instance with the configuration to build a new model.

    Inputs:
        `input_ids`: a torch.LongTensor of shape [batch_size, sequence_length]
            with the word token indices in the vocabulary(see the tokens preprocessing logic in the scripts
            `extract_features.py`, `run_classifier.py` and `run_squad.py`)
        `token_type_ids`: an optional torch.LongTensor of shape [batch_size, sequence_length] with the token
            types indices selected in [0, 1]. Type 0 corresponds to a `sentence A` and type 1 corresponds to
            a `sentence B` token (see BERT paper for more details).
        `attention_mask`: an optional torch.LongTensor of shape [batch_size, sequence_length] with indices
            selected in [0, 1]. It's a mask to be used if the input sequence length is smaller than the max
            input sequence length in the current batch. It's the mask that we typically use for attention when
            a batch has varying length sentences.
        `masked_lm_labels`: masked language modeling labels: torch.LongTensor of shape [batch_size, sequence_length]
            with indices selected in [-1, 0, ..., vocab_size]. All labels set to -1 are ignored (masked), the loss
            is only computed for the labels set in [0, ..., vocab_size]

    Outputs:
        if `masked_lm_labels` is  not `None`:
            Outputs the masked language modeling loss.
        if `masked_lm_labels` is `None`:
            Outputs the masked language modeling logits of shape [batch_size, sequence_length, vocab_size].

    Example usage:
    ```python
    # Already been converted into WordPiece token ids
    input_ids = torch.LongTensor([[31, 51, 99], [15, 5, 0]])
    input_mask = torch.LongTensor([[1, 1, 1], [1, 1, 0]])
    token_type_ids = torch.LongTensor([[0, 0, 1], [0, 1, 0]])

    config = BertConfig(vocab_size_or_config_json_file=32000, hidden_size=768,
        num_hidden_layers=12, num_attention_heads=12, intermediate_size=3072)

    model = BertForMaskedLM(config)
    masked_lm_logits_scores = model(input_ids, token_type_ids, input_mask)
    ```
    """
    def __init__(self, config):
        super(BertForMaskedLM, self).__init__(config)
        self.bert = BertModel(config)
        self.cls = BertOnlyMLMHead(config, self.bert.embeddings.word_embeddings.weight)
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, masked_lm_labels=None, checkpoint_activations=False):
        sequence_output, _ = self.bert(input_ids, token_type_ids, attention_mask,
                                       output_all_encoded_layers=False)
        prediction_scores = self.cls(sequence_output)

        if masked_lm_labels is not None:
            #loss_fct = CrossEntropyLoss(ignore_index=-1)
            loss_fct = CrossEntropyLoss(ignore_index=0)
            masked_lm_loss = loss_fct(prediction_scores.view(-1, self.config.vocab_size), masked_lm_labels.view(-1))
            return masked_lm_loss
        else:
            return prediction_scores


class BertForNextSentencePrediction(BertPreTrainedModel):
    """BERT model with next sentence prediction head.
    This module comprises the BERT model followed by the next sentence classification head.

    Params:
        config: a BertConfig class instance with the configuration to build a new model.

    Inputs:
        `input_ids`: a torch.LongTensor of shape [batch_size, sequence_length]
            with the word token indices in the vocabulary(see the tokens preprocessing logic in the scripts
            `extract_features.py`, `run_classifier.py` and `run_squad.py`)
        `token_type_ids`: an optional torch.LongTensor of shape [batch_size, sequence_length] with the token
            types indices selected in [0, 1]. Type 0 corresponds to a `sentence A` and type 1 corresponds to
            a `sentence B` token (see BERT paper for more details).
        `attention_mask`: an optional torch.LongTensor of shape [batch_size, sequence_length] with indices
            selected in [0, 1]. It's a mask to be used if the input sequence length is smaller than the max
            input sequence length in the current batch. It's the mask that we typically use for attention when
            a batch has varying length sentences.
        `next_sentence_label`: next sentence classification loss: torch.LongTensor of shape [batch_size]
            with indices selected in [0, 1].
            0 => next sentence is the continuation, 1 => next sentence is a random sentence.

    Outputs:
        if `next_sentence_label` is not `None`:
            Outputs the total_loss which is the sum of the masked language modeling loss and the next
            sentence classification loss.
        if `next_sentence_label` is `None`:
            Outputs the next sentence classification logits of shape [batch_size, 2].

    Example usage:
    ```python
    # Already been converted into WordPiece token ids
    input_ids = torch.LongTensor([[31, 51, 99], [15, 5, 0]])
    input_mask = torch.LongTensor([[1, 1, 1], [1, 1, 0]])
    token_type_ids = torch.LongTensor([[0, 0, 1], [0, 1, 0]])

    config = BertConfig(vocab_size_or_config_json_file=32000, hidden_size=768,
        num_hidden_layers=12, num_attention_heads=12, intermediate_size=3072)

    model = BertForNextSentencePrediction(config)
    seq_relationship_logits = model(input_ids, token_type_ids, input_mask)
    ```
    """
    def __init__(self, config):
        super(BertForNextSentencePrediction, self).__init__(config)
        self.bert = BertModel(config)
        self.cls = BertOnlyNSPHead(config)
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, next_sentence_label=None, checkpoint_activations=False):
        _, pooled_output = self.bert(input_ids, token_type_ids, attention_mask,
                                     output_all_encoded_layers=False)
        seq_relationship_score = self.cls( pooled_output)

        if next_sentence_label is not None:
            #loss_fct = CrossEntropyLoss(ignore_index=-1)
            loss_fct = CrossEntropyLoss(ignore_index=0)
            next_sentence_loss = loss_fct(seq_relationship_score.view(-1, 2), next_sentence_label.view(-1))
            return next_sentence_loss
        else:
            return seq_relationship_score


class BertForSequenceClassification(BertPreTrainedModel):
    """BERT model for classification.
    This module is composed of the BERT model with a linear layer on top of
    the pooled output.

    Params:
        `config`: a BertConfig class instance with the configuration to build a new model.
        `num_labels`: the number of classes for the classifier. Default = 2.

    Inputs:
        `input_ids`: a torch.LongTensor of shape [batch_size, sequence_length]
            with the word token indices in the vocabulary(see the tokens preprocessing logic in the scripts
            `extract_features.py`, `run_classifier.py` and `run_squad.py`)
        `token_type_ids`: an optional torch.LongTensor of shape [batch_size, sequence_length] with the token
            types indices selected in [0, 1]. Type 0 corresponds to a `sentence A` and type 1 corresponds to
            a `sentence B` token (see BERT paper for more details).
        `attention_mask`: an optional torch.LongTensor of shape [batch_size, sequence_length] with indices
            selected in [0, 1]. It's a mask to be used if the input sequence length is smaller than the max
            input sequence length in the current batch. It's the mask that we typically use for attention when
            a batch has varying length sentences.
        `labels`: labels for the classification output: torch.LongTensor of shape [batch_size]
            with indices selected in [0, ..., num_labels].

    Outputs:
        if `labels` is not `None`:
            Outputs the CrossEntropy classification loss of the output with the labels.
        if `labels` is `None`:
            Outputs the classification logits of shape [batch_size, num_labels].

    Example usage:
    ```python
    # Already been converted into WordPiece token ids
    input_ids = torch.LongTensor([[31, 51, 99], [15, 5, 0]])
    input_mask = torch.LongTensor([[1, 1, 1], [1, 1, 0]])
    token_type_ids = torch.LongTensor([[0, 0, 1], [0, 1, 0]])

    config = BertConfig(vocab_size_or_config_json_file=32000, hidden_size=768,
        num_hidden_layers=12, num_attention_heads=12, intermediate_size=3072)

    num_labels = 2

    model = BertForSequenceClassification(config, num_labels)
    logits = model(input_ids, token_type_ids, input_mask)
    ```
    """
    def __init__(self, config, num_labels):
        super(BertForSequenceClassification, self).__init__(config)
        self.num_labels = num_labels
        self.bert = BertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, num_labels)
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, labels=None, checkpoint_activations=False):
        _, pooled_output = self.bert(input_ids, token_type_ids, attention_mask, output_all_encoded_layers=False)
        pooled_output = self.dropout(pooled_output)
        logits = self.classifier(pooled_output)

        if labels is not None:
            loss_fct = CrossEntropyLoss()
            loss = loss_fct(logits.view(-1, self.num_labels), labels.view(-1))
            return loss
        else:
            return logits


class BertForMultipleChoice(BertPreTrainedModel):
    """BERT model for multiple choice tasks.
    This module is composed of the BERT model with a linear layer on top of
    the pooled output.

    Params:
        `config`: a BertConfig class instance with the configuration to build a new model.
        `num_choices`: the number of classes for the classifier. Default = 2.

    Inputs:
        `input_ids`: a torch.LongTensor of shape [batch_size, num_choices, sequence_length]
            with the word token indices in the vocabulary(see the tokens preprocessing logic in the scripts
            `extract_features.py`, `run_classifier.py` and `run_squad.py`)
        `token_type_ids`: an optional torch.LongTensor of shape [batch_size, num_choices, sequence_length]
            with the token types indices selected in [0, 1]. Type 0 corresponds to a `sentence A`
            and type 1 corresponds to a `sentence B` token (see BERT paper for more details).
        `attention_mask`: an optional torch.LongTensor of shape [batch_size, num_choices, sequence_length] with indices
            selected in [0, 1]. It's a mask to be used if the input sequence length is smaller than the max
            input sequence length in the current batch. It's the mask that we typically use for attention when
            a batch has varying length sentences.
        `labels`: labels for the classification output: torch.LongTensor of shape [batch_size]
            with indices selected in [0, ..., num_choices].

    Outputs:
        if `labels` is not `None`:
            Outputs the CrossEntropy classification loss of the output with the labels.
        if `labels` is `None`:
            Outputs the classification logits of shape [batch_size, num_labels].

    Example usage:
    ```python
    # Already been converted into WordPiece token ids
    input_ids = torch.LongTensor([[[31, 51, 99], [15, 5, 0]], [[12, 16, 42], [14, 28, 57]]])
    input_mask = torch.LongTensor([[[1, 1, 1], [1, 1, 0]],[[1,1,0], [1, 0, 0]]])
    token_type_ids = torch.LongTensor([[[0, 0, 1], [0, 1, 0]],[[0, 1, 1], [0, 0, 1]]])
    config = BertConfig(vocab_size_or_config_json_file=32000, hidden_size=768,
        num_hidden_layers=12, num_attention_heads=12, intermediate_size=3072)

    num_choices = 2

    model = BertForMultipleChoice(config, num_choices)
    logits = model(input_ids, token_type_ids, input_mask)
    ```
    """
    def __init__(self, config, num_choices):
        super(BertForMultipleChoice, self).__init__(config)
        self.num_choices = num_choices
        self.bert = BertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, 1)
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, labels=None, checkpoint_activations=False):
        flat_input_ids = input_ids.view(-1, input_ids.size(-1))
        flat_token_type_ids = token_type_ids.view(-1, token_type_ids.size(-1))
        flat_attention_mask = attention_mask.view(-1, attention_mask.size(-1))
        _, pooled_output = self.bert(flat_input_ids, flat_token_type_ids, flat_attention_mask, output_all_encoded_layers=False)
        pooled_output = self.dropout(pooled_output)
        logits = self.classifier(pooled_output)
        reshaped_logits = logits.view(-1, self.num_choices)

        if labels is not None:
            loss_fct = CrossEntropyLoss()
            loss = loss_fct(reshaped_logits, labels)
            return loss
        else:
            return reshaped_logits


class BertForTokenClassification(BertPreTrainedModel):
    """BERT model for token-level classification.
    This module is composed of the BERT model with a linear layer on top of
    the full hidden state of the last layer.

    Params:
        `config`: a BertConfig class instance with the configuration to build a new model.
        `num_labels`: the number of classes for the classifier. Default = 2.

    Inputs:
        `input_ids`: a torch.LongTensor of shape [batch_size, sequence_length]
            with the word token indices in the vocabulary(see the tokens preprocessing logic in the scripts
            `extract_features.py`, `run_classifier.py` and `run_squad.py`)
        `token_type_ids`: an optional torch.LongTensor of shape [batch_size, sequence_length] with the token
            types indices selected in [0, 1]. Type 0 corresponds to a `sentence A` and type 1 corresponds to
            a `sentence B` token (see BERT paper for more details).
        `attention_mask`: an optional torch.LongTensor of shape [batch_size, sequence_length] with indices
            selected in [0, 1]. It's a mask to be used if the input sequence length is smaller than the max
            input sequence length in the current batch. It's the mask that we typically use for attention when
            a batch has varying length sentences.
        `labels`: labels for the classification output: torch.LongTensor of shape [batch_size, sequence_length]
            with indices selected in [0, ..., num_labels].

    Outputs:
        if `labels` is not `None`:
            Outputs the CrossEntropy classification loss of the output with the labels.
        if `labels` is `None`:
            Outputs the classification logits of shape [batch_size, sequence_length, num_labels].

    Example usage:
    ```python
    # Already been converted into WordPiece token ids
    input_ids = torch.LongTensor([[31, 51, 99], [15, 5, 0]])
    input_mask = torch.LongTensor([[1, 1, 1], [1, 1, 0]])
    token_type_ids = torch.LongTensor([[0, 0, 1], [0, 1, 0]])

    config = BertConfig(vocab_size_or_config_json_file=32000, hidden_size=768,
        num_hidden_layers=12, num_attention_heads=12, intermediate_size=3072)

    num_labels = 2

    model = BertForTokenClassification(config, num_labels)
    logits = model(input_ids, token_type_ids, input_mask)
    ```
    """
    def __init__(self, config, num_labels):
        super(BertForTokenClassification, self).__init__(config)
        self.num_labels = num_labels
        self.bert = BertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, num_labels)
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, labels=None, checkpoint_activations=False):
        sequence_output, _ = self.bert(input_ids, token_type_ids, attention_mask, output_all_encoded_layers=False)
        sequence_output = self.dropout(sequence_output)
        logits = self.classifier(sequence_output)

        if labels is not None:
            loss_fct = CrossEntropyLoss()
            # Only keep active parts of the loss
            if attention_mask is not None:
                active_loss = attention_mask.view(-1) == 1
                active_logits = logits.view(-1, self.num_labels)[active_loss]
                active_labels = labels.view(-1)[active_loss]
                loss = loss_fct(active_logits, active_labels)
            else:
                loss = loss_fct(logits.view(-1, self.num_labels), labels.view(-1))
            return loss
        else:
            return logits


class BertForQuestionAnswering(BertPreTrainedModel):
    """BERT model for Question Answering (span extraction).
    This module is composed of the BERT model with a linear layer on top of
    the sequence output that computes start_logits and end_logits

    Params:
        `config`: a BertConfig class instance with the configuration to build a new model.

    Inputs:
        `input_ids`: a torch.LongTensor of shape [batch_size, sequence_length]
            with the word token indices in the vocabulary(see the tokens preprocessing logic in the scripts
            `extract_features.py`, `run_classifier.py` and `run_squad.py`)
        `token_type_ids`: an optional torch.LongTensor of shape [batch_size, sequence_length] with the token
            types indices selected in [0, 1]. Type 0 corresponds to a `sentence A` and type 1 corresponds to
            a `sentence B` token (see BERT paper for more details).
        `attention_mask`: an optional torch.LongTensor of shape [batch_size, sequence_length] with indices
            selected in [0, 1]. It's a mask to be used if the input sequence length is smaller than the max
            input sequence length in the current batch. It's the mask that we typically use for attention when
            a batch has varying length sentences.
        `start_positions`: position of the first token for the labeled span: torch.LongTensor of shape [batch_size].
            Positions are clamped to the length of the sequence and position outside of the sequence are not taken
            into account for computing the loss.
        `end_positions`: position of the last token for the labeled span: torch.LongTensor of shape [batch_size].
            Positions are clamped to the length of the sequence and position outside of the sequence are not taken
            into account for computing the loss.

    Outputs:
        if `start_positions` and `end_positions` are not `None`:
            Outputs the total_loss which is the sum of the CrossEntropy loss for the start and end token positions.
        if `start_positions` or `end_positions` is `None`:
            Outputs a tuple of start_logits, end_logits which are the logits respectively for the start and end
            position tokens of shape [batch_size, sequence_length].

    Example usage:
    ```python
    # Already been converted into WordPiece token ids
    input_ids = torch.LongTensor([[31, 51, 99], [15, 5, 0]])
    input_mask = torch.LongTensor([[1, 1, 1], [1, 1, 0]])
    token_type_ids = torch.LongTensor([[0, 0, 1], [0, 1, 0]])

    config = BertConfig(vocab_size_or_config_json_file=32000, hidden_size=768,
        num_hidden_layers=12, num_attention_heads=12, intermediate_size=3072)

    model = BertForQuestionAnswering(config)
    start_logits, end_logits = model(input_ids, token_type_ids, input_mask)
    ```
    """
    def __init__(self, config):
        super(BertForQuestionAnswering, self).__init__(config)
        self.bert = BertModel(config)
        # TODO check with Google if it's normal there is no dropout on the token classifier of SQuAD in the TF version
        # self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.qa_outputs = nn.Linear(config.hidden_size, 2)
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, start_positions=None, end_positions=None, checkpoint_activations=False):
        sequence_output, _ = self.bert(input_ids, token_type_ids, attention_mask, output_all_encoded_layers=False)
        logits = self.qa_outputs(sequence_output)
        start_logits, end_logits = logits.split(1, dim=-1)
        start_logits = start_logits.squeeze(-1)
        end_logits = end_logits.squeeze(-1)

        if start_positions is not None and end_positions is not None:
            # If we are on multi-GPU, split add a dimension
            if len(start_positions.size()) > 1:
                start_positions = start_positions.squeeze(-1)
            if len(end_positions.size()) > 1:
                end_positions = end_positions.squeeze(-1)
            # sometimes the start/end positions are outside our model inputs, we ignore these terms
            ignored_index = start_logits.size(1)
            start_positions.clamp_(0, ignored_index)
            end_positions.clamp_(0, ignored_index)

            loss_fct = CrossEntropyLoss(ignore_index=ignored_index)
            start_loss = loss_fct(start_logits, start_positions)
            end_loss = loss_fct(end_logits, end_positions)
            total_loss = (start_loss + end_loss) / 2
            return total_loss
        else:
            return start_logits, end_logits


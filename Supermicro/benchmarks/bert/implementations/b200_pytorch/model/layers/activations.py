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

import math

import torch
from torch import nn

# Fused GeLU
#torch._C._jit_set_profiling_mode(False)                                                                                    
#torch._C._jit_set_profiling_executor(False)                                                                                
#torch._C._jit_override_can_fuse_on_cpu(True)                                                                               
#torch._C._jit_override_can_fuse_on_gpu(True)

## Use NV Fuser

torch._C._jit_set_profiling_executor(True)
torch._C._jit_set_nvfuser_enabled(True)
torch._C._jit_set_texpr_fuser_enabled(False)
torch._C._jit_set_profiling_mode(True)
torch._C._jit_override_can_fuse_on_cpu(False)
torch._C._jit_override_can_fuse_on_gpu(False)

# 1/sqrt(2*pi)-> 0.3989423
# 1/sqrt(2)   -> 0.70710678
# sqrt(2/pi)  -> 0.79788456

# this function is tanh approximation of gelu
# actual gelu is:
# x * 0.5 * (1.0 + torch.erf(x * 0.70710678))
@torch.jit.script
def bias_gelu(bias, y):
  x = bias + y
  return  x * 0.5 * (1.0 + torch.tanh(0.79788456 * x * (1 + 0.044715 * x * x)))

# gradient of tanh approximation of gelu
# gradient of actual gelu is:
# 0.5 * (1. + torch.erf(x * 0.70710678)) + 0.3989423 * x * torch.exp(-0.5 * x * x)
@torch.jit.script
def bias_gelu_back(g, bias, y):
  x = bias + y
  tanh_out = torch.tanh(0.79788456 * x * (1 + 0.044715 * x * x))
  # sqrt(2/pi) * 3 * 0.044715 -> 0.1070322243
  ff = 0.5 * x * ((1 - tanh_out * tanh_out) * (0.79788456 + 0.1070322243 * x * x)) + 0.5 * (1 + tanh_out)
  return ff*g

class GeLUFunction(torch.autograd.Function):
  @staticmethod
  # bias is an optional argument
  def forward(ctx, input, bias):
    ctx.save_for_backward(input, bias)
    return bias_gelu(bias, input)

  @staticmethod
  def backward(ctx, grad_output):
    input, bias = ctx.saved_tensors
    tmp = bias_gelu_back(grad_output, bias, input)
    return tmp, tmp

bias_gelu_impl = GeLUFunction.apply

# this function is tanh approximation of gelu
# actual gelu is:
# x * 0.5 * (1.0 + torch.erf(x * 0.70710678))
@torch.jit.script
def gelu_fwd(x):
  return  x * 0.5 * (1.0 + torch.tanh(0.79788456 * x * (1 + 0.044715 * x * x)))

# gradient of tanh approximation of gelu
# gradient of actual gelu is:
# 0.5 * (1. + torch.erf(x * 0.70710678)) + 0.3989423 * x * torch.exp(-0.5 * x * x)
@torch.jit.script
def gelu_bwd(g, x):
  tanh_out = torch.tanh(0.79788456 * x * (1 + 0.044715 * x * x))
  # sqrt(2/pi) * 3 * 0.044715 -> 0.1070322243
  ff = 0.5 * x * ((1 - tanh_out * tanh_out) * (0.79788456 + 0.1070322243 * x * x)) + 0.5 * (1 + tanh_out)
  return ff*g

class FastGeLUFunction(torch.autograd.Function):
  @staticmethod
  # bias is an optional argument
  def forward(ctx, input):
    ctx.save_for_backward(input)
    return gelu_fwd(input)

  @staticmethod
  def backward(ctx, grad_output):
    input, = ctx.saved_tensors
    tmp = gelu_bwd(grad_output, input)
    return tmp

fast_gelu_impl = FastGeLUFunction.apply

# Swish
def swish(x):
  return x * torch.sigmoid(x)

# Fast GeLU
def fast_gelu(x):
  pi = 3.1415926535897932
  cdf = 0.5 * (1.0 + torch.tanh((math.sqrt(2 / pi) * (x + 0.044715 * torch.pow(x, 3)))))
  return x * cdf

ACT2FN = {
  "gelu": fast_gelu_impl,
  "bias_gelu": bias_gelu_impl,
  "relu": torch.nn.functional.relu,
  "swish": swish
}


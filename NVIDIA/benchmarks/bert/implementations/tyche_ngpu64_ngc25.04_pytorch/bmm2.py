# Copyright (c) 2019-2025 NVIDIA CORPORATION. All rights reserved.
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

import torch
import mhalib

###########################################################################################

class Bmm2Function(torch.autograd.Function):

    @staticmethod
    def forward(ctx, batch1, batch2, seqlen, batch, maxseqlen, heads, embed, sync, stream):
        ctx.save_for_backward(batch1, batch2, seqlen)
        ctx.batch = batch
        ctx.maxseqlen = maxseqlen
        ctx.heads = heads
        ctx.embed = embed
        ctx.stream = stream
        ctx.sync = sync
        ntokens = seqlen.sum().item()
        ctx.ntokens = ntokens

        output = torch.empty([ntokens,heads,embed], device="cuda", dtype=torch.float16)
        mhalib.FastBmm2Fprop(batch2.flatten().contiguous(), batch1.flatten().contiguous(), output, batch, seqlen, heads, embed, False, False, stream, sync)

        return output[:ntokens]

    @staticmethod
    def backward(ctx, grad_output):

        batch1, batch2, seqlen = ctx.saved_tensors
        batch = ctx.batch
        maxseqlen = ctx.maxseqlen
        heads = ctx.heads
        embed = ctx.embed
        ntokens = ctx.ntokens
        ntokens2 = 0
        for i in range(batch):
            ntokens2 += seqlen[i]*seqlen[i]

        grad_batch1 = torch.empty([ntokens2*heads], device="cuda", dtype=torch.float16)
        grad_batch2 = torch.empty([ntokens,heads*embed], device="cuda", dtype=torch.float16)

        mhalib.FastBmm2Dgrad1(batch2.flatten().contiguous(), grad_output, grad_batch1, batch, seqlen, heads, embed, False, False, ctx.stream, ctx.sync)
        mhalib.FastBmm2Dgrad2(grad_output, batch1, grad_batch2, batch, seqlen, heads, embed, False, False, ctx.stream, ctx.sync)

        return grad_batch1[:ntokens2*heads], grad_batch2[:ntokens], None, None, None, None, None, None, None

class Bmm2(torch.nn.Module):
    def __init__(self, batch, seqlen, heads, embed, stream=True, sync=True):
        super(Bmm2, self).__init__()

        self.heads = heads
        self.embed = embed
        self.maxseqlen = seqlen
        self.stream = stream
        self.sync = sync

    def forward(self, batch1, batch2, batch, seqlen):
        return Bmm2Function.apply(batch1, batch2, seqlen, batch, self.maxseqlen, self.heads, self.embed, self.stream, self.sync)

###########################################################################################

class Bmm2StridedFunction(torch.autograd.Function):

    @staticmethod
    def forward(ctx, batch1, mixed, seqlen, batch, maxseqlen, heads, embed, stream, sync, timers):
        ctx.save_for_backward(batch1, mixed, seqlen)
        ctx.batch = batch
        ctx.maxseqlen = maxseqlen
        ctx.heads = heads
        ctx.embed = embed
        ctx.stream = stream
        ctx.sync = sync
        ctx.timers = timers
        ntokens = seqlen.sum().item()
        ctx.ntokens = ntokens

        output = torch.empty([ntokens,heads,embed], device="cuda", dtype=torch.float16)

        if timers: timers['start_fprop'].record()
        mhalib.FastBmm2Fprop(mixed, batch1, output, batch, seqlen, heads, embed, False, True, stream, sync)
        if timers: timers['stop_fprop'].record()

        return output[:ntokens]

    @staticmethod
    def backward(ctx, grad_output):

        batch1, mixed, seqlen = ctx.saved_tensors
        batch = ctx.batch
        maxseqlen = ctx.maxseqlen
        heads = ctx.heads
        embed = ctx.embed
        ntokens = ctx.ntokens
        ntokens2 = 0
        for i in range(batch):
            ntokens2 += seqlen[i]*seqlen[i]

        grad_batch1 = torch.empty(ntokens2*heads, device="cuda", dtype=torch.float16)
        grad_mixed = torch.empty([ntokens,heads*3*embed], device="cuda", dtype=torch.float16)

        if ctx.timers: ctx.timers['start_dgrad'].record()
        mhalib.FastBmm2Dgrad1(mixed, grad_output, grad_batch1, batch, seqlen, heads, embed, False, True, ctx.stream, ctx.sync)
        if ctx.timers: ctx.timers['stop_dgrad'].record()
        if ctx.timers: ctx.timers['start_wgrad'].record()
        mhalib.FastBmm2Dgrad2(grad_output, batch1, grad_mixed, batch, seqlen, heads, embed, False, True, ctx.stream, ctx.sync)
        if ctx.timers: ctx.timers['stop_wgrad'].record()
        return grad_batch1[:ntokens2*heads], grad_mixed[:ntokens], None, None, None, None, None, None, None, None

class Bmm2Strided(torch.nn.Module):
    def __init__(self, batch, seqlen, heads, embed, stream=True, sync=True, timer=False):
        super(Bmm2Strided, self).__init__()

        self.heads = heads
        self.embed = embed
        self.maxseqlen = seqlen
        self.stream = stream
        self.sync = sync
        if timer:
            self.timers = {'start_fprop':torch.cuda.Event(enable_timing=True),
                           'start_dgrad':torch.cuda.Event(enable_timing=True),
                           'start_wgrad':torch.cuda.Event(enable_timing=True),
                           'stop_fprop':torch.cuda.Event(enable_timing=True),
                           'stop_dgrad':torch.cuda.Event(enable_timing=True),
                           'stop_wgrad':torch.cuda.Event(enable_timing=True)}
        else:
            self.timers = None

    def forward(self, batch1, mixed, batch, seqlen):
        return Bmm2StridedFunction.apply(batch1, mixed, seqlen, batch, self.maxseqlen, self.heads, self.embed, self.stream, self.sync, self.timers)

###########################################################################################

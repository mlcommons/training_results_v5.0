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
from function import graph
from apex import amp
import time
from mlperf_common.scaleoutbridge import ScaleoutBridgeBase as SBridge

def preprocess_batch(args, input_ids, segment_ids, input_mask, labels_mlm, labels_nsp,  packed_seqlens=None, input_only=False, get_ntokens=False, graph_capture_large_batch=False):
    b, s = input_ids.shape
    if args.pad_fmha:
        seqlens = input_mask.sum(-1, dtype=torch.int32)
        cu_seqlens = torch.zeros(b+1, dtype=torch.int32, device=seqlens.device)
        cu_seqlens[1:] = torch.cumsum(seqlens, 0)
        ntokens = cu_seqlens[-1].item()

        position_ids = torch.cat([torch.arange(s, dtype=torch.int64, device=seqlens.device) for _ in range(b)]).view(b,s)

        def compact(t):
            '''Removes per-sequence padding and adds all padding to the end of the batch.
            Thus, the output will still be [batch_size x seq_len].
            '''
            t_compact = torch.zeros_like(t).view(-1)
            for it in range(b):
                si = seqlens[it]
                begin = cu_seqlens[it]
                end = cu_seqlens[it +  1]
                t_compact[begin:end] = t[it, :si]
            return t_compact.view(t.shape)

        iids = compact(input_ids)
        sids = compact(segment_ids)
        pids = compact(position_ids)
        lmlm = compact(labels_mlm)

        # in case we deal with packed sequences we need to update cu_seqlens and pids
        if packed_seqlens != None:
            packed_seqlens = packed_seqlens.view(-1)
            labels_nsp = labels_nsp.view(-1)[torch.nonzero(packed_seqlens, as_tuple=True)[-1]] #pick predictions for non-zero length sequences
            packed_seqlens = packed_seqlens[torch.nonzero(packed_seqlens, as_tuple=True)[-1]]
            pids = torch.cat([torch.arange(l, dtype=torch.int64, device=seqlens.device) for l in packed_seqlens]+
                    [torch.zeros((s*b-packed_seqlens.sum(),), dtype=torch.int64, device=seqlens.device)]).view(b,s)
            cu_seqlens = torch.zeros(packed_seqlens.shape[0]+1, dtype=torch.int32, device=packed_seqlens.device)
            cu_seqlens[1:] = torch.cumsum(packed_seqlens, 0)

            # With packing and CUDA Graph capture, we need to ensure that certain tensors have static shapes
            if graph_capture_large_batch:
                # The originals of the following tensors must be retained
                cu_seqlens_orig = cu_seqlens.detach().clone()
                labels_nsp_orig = labels_nsp.detach().clone()

                # Determine how many elements need to be filled in for the tensors
                fill_count = args.train_batch_size*args.max_pack_factor+1 - cu_seqlens.size(dim=0)
                if fill_count > 0:
                    # Repeat last element of cu_seqlens to produce a static shape for CUDA Graph capture
                    cu_seqlens_cat = torch.full((fill_count,), cu_seqlens[-1], dtype=cu_seqlens.dtype, device=cu_seqlens.device)
                    cu_seqlens = torch.cat((cu_seqlens, cu_seqlens_cat))

                    # Add -1's at the end of label_nsp (will be ignored)
                    labels_nsp_cat = torch.full((fill_count,), -1, dtype=labels_nsp.dtype, device=labels_nsp.device)
                    labels_nsp = torch.cat((labels_nsp, labels_nsp_cat))

                # Need to additionally return original cu_seqlens (for torch.index_select in BertPooler)
                # and labels_nsp (for nsp_loss_fct in BertForPreTrainingHeadsOnly) tensors
                if get_ntokens:
                    return iids, sids, cu_seqlens, lmlm, labels_nsp, pids, cu_seqlens_orig, labels_nsp_orig, ntokens
                else:
                    return iids, sids, cu_seqlens, lmlm, labels_nsp, pids, cu_seqlens_orig, labels_nsp_orig

        if input_only:
            return iids, sids, cu_seqlens, pids

        if get_ntokens:
            return iids, sids, cu_seqlens, lmlm, labels_nsp, pids, ntokens
        return iids, sids, cu_seqlens, lmlm, labels_nsp, pids

    if input_only:
        return input_ids, segment_ids, input_mask
    return input_ids, segment_ids, input_mask, labels_mlm, labels_nsp

class FwdLossBwdTrainer():

    def __init__(self, args, grad_scaler):
        super(FwdLossBwdTrainer, self).__init__()
        self.args = args
        self.grad_scaler = grad_scaler
        self.capture_stream = torch.cuda.Stream()

        self.send_stats_in_parallel = False
        self.stats_stream = torch.cuda.Stream()
        self.loss_cpu = torch.tensor(0.0, dtype=torch.float32, device='cpu').pin_memory()
        self.mlm_acc_cpu = torch.tensor(0.0, dtype=torch.float32, device='cpu').pin_memory()

    def capture_bert_model_segment_graph(self, bert_model, use_cuda_graph, graph_capture_large_batch=False):
        # eval batch depends on the rank, since eval sample count isn't divisible by world size
        rank = torch.distributed.get_rank()
        world_size = torch.distributed.get_world_size()
        eval_batch_min = self.args.num_eval_examples // world_size
        remainder = self.args.num_eval_examples % world_size
        if rank<remainder:
            eval_batch = eval_batch_min + 1
        else:
            eval_batch = eval_batch_min
        eval_batch = min(eval_batch, self.args.eval_batch_size)
        batches_to_graph = [eval_batch, self.args.train_batch_size]
        
        bert_model_segment = bert_model.bert_model_segment
        sample_train = [
                 torch.ones(self.args.train_batch_size, self.args.max_seq_length, dtype=torch.int64, device=self.args.device),
                 torch.ones(self.args.train_batch_size, self.args.max_seq_length, dtype=torch.int64, device=self.args.device),
                 torch.ones(self.args.train_batch_size, self.args.max_seq_length, dtype=torch.int64, device=self.args.device),
                 torch.ones(self.args.train_batch_size, self.args.max_seq_length, dtype=torch.int64, device=self.args.device),
                 torch.ones(self.args.train_batch_size, dtype=torch.int64, device=self.args.device),
                 ]
        sample_eval = [
                 torch.ones(eval_batch, self.args.max_seq_length, dtype=torch.int64, device=self.args.device),
                 torch.ones(eval_batch, self.args.max_seq_length, dtype=torch.int64, device=self.args.device),
                 torch.ones(eval_batch, self.args.max_seq_length, dtype=torch.int64, device=self.args.device),
                 torch.ones(eval_batch, self.args.max_seq_length, dtype=torch.int64, device=self.args.device),
                 torch.ones(eval_batch, dtype=torch.int64, device=self.args.device),
                 ]  
        sample_input_encoder = [
            torch.zeros(self.args.train_batch_size, self.args.max_seq_length, 1024, dtype=torch.float16, device=self.args.device, requires_grad=True),
            torch.ones(self.args.train_batch_size*self.args.max_pack_factor+1, dtype=torch.int32, device=self.args.device, requires_grad=False),
        ]
        if graph_capture_large_batch:
            print ('Enabling make_graphed_callables for encoder!!')

            from transformer_engine.pytorch import make_graphed_callables
            bert_model_segment.bert.encoder = make_graphed_callables(
                bert_model_segment.bert.encoder, 
                tuple(sample_input_encoder),
                fp8_enabled=True,
                fp8_recipe=bert_model_segment.bert.encoder.fp8_recipe)
            return bert_model
        sample_model_train = preprocess_batch(self.args, *sample_train, input_only=True)
        sample_model_eval = preprocess_batch(self.args, *sample_eval, input_only=True)
        bert_model_segment = graph(bert_model_segment,
                                    tuple(t.clone() for t in sample_model_train),
                                    tuple(t.clone() for t in sample_model_eval) if self.args.eval_batch_size * world_size >= self.args.num_eval_examples else None,
                                    self.capture_stream,
                                    warmup_iters=0, #8
                                    warmup_only=(not use_cuda_graph))

        bert_head_segment = bert_model.heads_only_segment
        sample_head_train = [
                torch.ones(self.args.train_batch_size, self.args.max_seq_length, 1024, dtype=torch.float16, device=self.args.device),
                torch.ones(self.args.train_batch_size,                           1024, dtype=torch.float16, device=self.args.device),
                torch.ones(self.args.train_batch_size, self.args.max_seq_length,       dtype=torch.int64, device=self.args.device),
                torch.ones(self.args.train_batch_size,                                 dtype=torch.int64, device=self.args.device),
                ]
        sample_head_eval = [
                torch.ones(eval_batch, self.args.max_seq_length, 1024, dtype=torch.float16, device=self.args.device),
                torch.ones(eval_batch,                           1024, dtype=torch.float16, device=self.args.device),
                torch.ones(eval_batch, self.args.max_seq_length,       dtype=torch.int64, device=self.args.device),
                torch.ones(eval_batch,                                 dtype=torch.int64, device=self.args.device),
                ]
        sample_head_tuple_train = tuple([sample_head_train[0].clone().requires_grad_(), sample_head_train[1].clone().requires_grad_(), sample_head_train[2].clone(), sample_head_train[3].clone()])
        sample_head_tuple_eval = tuple([sample_head_eval[0].clone(), sample_head_eval[1].clone(), sample_head_eval[2].clone(), sample_head_eval[3].clone()])
        bert_head_segment = graph(bert_head_segment,
                                               sample_head_tuple_train,
                                               sample_head_tuple_eval if self.args.eval_batch_size * world_size >= self.args.num_eval_examples else None,
                                               self.capture_stream,
                                               warmup_iters=0, #
                                               warmup_only=(not use_cuda_graph))


        return bert_model

    def eval_step(self, batch, model):
        model.eval()
        loss = None
        mlm_acc = None

        loss, mlm_acc, num_valid = model(*batch)
        return loss, mlm_acc, num_valid

    def step(self, step, batch, model, optimizer, sbridge=SBridge(), ntokens=None, graph_capture_large_batch=False, data_iter=None):
        loss = None
        mlm_acc = None

        sbridge.start_prof(SBridge.FWD_TIME)
        if ntokens is not None:
            #print ('fwd_loss_bwd::step ntokens {}'.format(ntokens))
            model.bert_model_segment.bert.encoder.ntokens = ntokens
        loss, mlm_acc, _ = model(*batch)
        next_batch = None
        if data_iter is not None:
            next_batch = next(data_iter, None)
            if next_batch is not None:
                next_batch = preprocess_batch(self.args, *next_batch, get_ntokens=(ntokens is not None), graph_capture_large_batch=graph_capture_large_batch)

        if self.send_stats_in_parallel:
            self.stats_stream.wait_stream(torch.cuda.current_stream())
            with torch.cuda.stream(self.stats_stream):
                self.loss_cpu.copy_(loss.detach(), non_blocking=True)
                self.mlm_acc_cpu.copy_(mlm_acc.detach(), non_blocking=True)

        sbridge.stop_start_prof(SBridge.FWD_TIME, SBridge.BWD_TIME)

        if self.args.bypass_amp:
            loss.backward()
        elif self.args.distributed_lamb:
            optimizer._lazy_init_stage1()
            self.grad_scaler.scale(loss).backward()
            optimizer._lazy_init_stage2()
        else:
            with amp.scale_loss(loss, optimizer, delay_overflow_check=self.args.allreduce_post_accumulation) as scaled_loss:
                scaled_loss.backward()
        sbridge.stop_prof(SBridge.BWD_TIME)

        if self.send_stats_in_parallel:
            self.stats_stream.synchronize()
            loss = self.loss_cpu
            mlm_acc = self.mlm_acc_cpu

        if data_iter is not None:
            return loss, mlm_acc, sbridge, next_batch
        return loss, mlm_acc, sbridge

// Copyright (c) 2021-2025, NVIDIA CORPORATION. All rights reserved.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//           http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#include <cuda_runtime_api.h>
#include "proposal_matcher.h"

#define CHECK_LAST_CUDA_ERROR() checkLast(__FILE__, __LINE__)
void checkLast(const char* const file, const int line)
{
    cudaError_t err{cudaGetLastError()};
    if (err != cudaSuccess)
    {
        std::cerr << "CUDA Runtime Error at: " << file << ":" << line
                  << std::endl;
        std::cerr << cudaGetErrorString(err) << std::endl;
        // We don't exit when we encounter CUDA errors.
        // std::exit(EXIT_FAILURE);
    }
}

namespace other_ns {


__launch_bounds__(256) static __global__
    void max_along_gt_idx(float *match, unsigned char *pred_forgiven, long *max_gt_idx, long long gt,long long preds,
                          bool include_low_quality, float low_th, float high_th) {
    
    long long tid = blockIdx.x * blockDim.x + threadIdx.x;
    int image_id = blockIdx.y;
    int offset_match_matrix = image_id * preds * gt;
    int offset_preds = image_id * preds;
    if(tid < preds){
        float max_iou = 0.0f;
        int max_idx = 0;
        float iou;
        for(long long i = 0;i < gt; i++){
            iou = match[offset_match_matrix + i * preds + tid]; 
            if (iou > max_iou) {max_iou = iou; max_idx = i;}
        }

        if (max_iou >= high_th) max_gt_idx[offset_preds + tid] = max_idx;
        else if ((pred_forgiven[offset_preds + tid] == 1 && include_low_quality)) max_gt_idx[offset_preds + tid] = max_idx;
        else if (max_iou < low_th) max_gt_idx[offset_preds + tid] = -1;
        else if (max_iou < high_th) max_gt_idx[offset_preds + tid] = -2;
    }
}


__device__ void warpReduce(volatile float* sdata, int tid) {
    sdata[tid] = fmax(sdata[tid],sdata[tid + 32]);
    sdata[tid] = fmax(sdata[tid],sdata[tid + 16]);
    sdata[tid] = fmax(sdata[tid],sdata[tid + 8]);
    sdata[tid] = fmax(sdata[tid],sdata[tid + 4]);
    sdata[tid] = fmax(sdata[tid],sdata[tid + 2]);
    sdata[tid] = fmax(sdata[tid],sdata[tid + 1]);
}


static __global__
    void max_along_preds(float* match, float* inter_gt, long long gt,long long preds) {
    int gt_idx = blockIdx.x;
    int chunk_idx = blockIdx.y;
    int image_id = blockIdx.z;
    int num_chunks = (preds + 2047) / 2048;
    int gt_offset = chunk_idx * 2048;
    int start_idx = image_id * preds * gt + gt_idx * preds + gt_offset;
    int idx = threadIdx.x;
    __shared__ float shbuf[1024]; 
   shbuf[idx] = 0.0f;
    __syncthreads();
    if(gt_offset + idx + 1024 < preds) shbuf[idx] = fmax(match[start_idx + idx], match[start_idx + idx + 1024]);
    else if (gt_offset + idx < preds) shbuf[idx] = match[start_idx + idx];
    __syncthreads();
    if(idx < 512) shbuf[idx] = fmax(shbuf[idx],shbuf[idx + 512]);
    __syncthreads();
    if(idx < 256) shbuf[idx] = fmax(shbuf[idx], shbuf[idx + 256]);
    __syncthreads();
    if(idx < 128) shbuf[idx] = fmax(shbuf[idx], shbuf[idx + 128]);
    __syncthreads();
    if(idx < 64) shbuf[idx] = fmax(shbuf[idx], shbuf[idx + 64]);
    __syncthreads();
    if(idx < 32) warpReduce(shbuf, idx);
    if (idx == 0) inter_gt[image_id * num_chunks * gt +  num_chunks * gt_idx + chunk_idx] = shbuf[idx];
}


__launch_bounds__(256) static __global__
    void max_along_preds_reduced(float *match, float *max_preds, long long gt,long long preds) {
    long long tid = blockIdx.x * blockDim.x + threadIdx.x;
    int image_id = blockIdx.y;
    if (tid < gt){
        float max_iou = 0.0f;
        float iou;
        for(long long i = 0; i < preds; i++){
            iou = match[image_id * gt * preds + tid * preds + i]; 
            if (iou > max_iou) max_iou = iou;
        }
        max_preds[image_id * gt + tid] = max_iou;
    }
}



__launch_bounds__(256) static __global__
    void forgive_preds(float *match_quality_data, float *d_best_pred_per_gt, unsigned char *d_pred_forgiven, 
                       long gt, long preds) {
    long tid = blockIdx.x * blockDim.x + threadIdx.x;
    int image_id = blockIdx.y;
    int offset = image_id * gt * preds;
    if (tid < preds) {
        unsigned char forgiven = 0;
        float iou;
        for(int i = 0; i < gt; i++) {
            iou = match_quality_data[offset + i * preds + tid];
            // do not consider predictions from padded targets (iou = -1)
            if(((iou == d_best_pred_per_gt[image_id * gt + i])) && (iou != -1.0)) {
                forgiven = 1;
                break;
            }            
        }
        d_pred_forgiven[image_id * preds + tid] = forgiven;
    }    
} 


template<>
void proposal_matcher<::dali::GPUBackend>::RunImpl(::dali::Workspace &ws) {
  const auto &input = ws.Input<::dali::GPUBackend>(0);
  const auto &shape = input.shape();
  auto &output = ws.Output<::dali::GPUBackend>(0);

  bool allow_low_quality_matches = true;
  float low_th = 0.4;
  float high_th = 0.5;

  int num_images = 1;  //shape.num_samples();

  for (int sample_idx = 0; sample_idx < shape.num_samples(); sample_idx++) {
      int gt = shape[sample_idx][0];
      long long preds = shape[sample_idx][1];
      float *match_quality_data = (float*) input.raw_tensor(sample_idx);
      int num_chunks = (preds + 2047) / 2048;
      
      // do an intermediate reduction along all predictions for each gt
      dim3 block(1024, 1, 1);
      dim3 grid(gt, num_chunks, num_images);
      
      if (allow_low_quality_matches) max_along_preds<<<grid, block, 0, ws.stream()>>>(
		      (float*) input.raw_tensor(sample_idx),
		      d_intergt,
		      gt,
		      preds);

      // final reduction to find best iou per gt
      int numThreads = 256;
      int numBlocks = (gt + numThreads - 1) / numThreads;
      dim3 grid2(numBlocks, num_images, 1);

      if (allow_low_quality_matches) max_along_preds_reduced<<<grid2, numThreads, 0, ws.stream()>>>(
		      d_intergt,
		      d_best_pred_per_gt,
		      gt,
		      num_chunks);

      numBlocks=(preds + numThreads - 1) / numThreads;
      dim3 grid_preds(numBlocks, num_images, 1);
      // if low_quality_matches are allowed, mark some predictions to keep their best matching gt even though
      // iou < threshold
      if (allow_low_quality_matches) forgive_preds<<<grid_preds, numThreads, 0, ws.stream()>>>(
		      (float*) input.raw_tensor(sample_idx),
		      d_best_pred_per_gt,
		      d_pred_forgiven,
		      gt,
		      preds);

      // compute resulting tensor of indices
      max_along_gt_idx<<<grid_preds, numThreads, 0, ws.stream()>>>(
		      (float*) input.raw_tensor(sample_idx),
		      d_pred_forgiven,
		      (long*) output.raw_mutable_tensor(sample_idx),
		      gt,
		      preds,
		      allow_low_quality_matches,
		      low_th,
		      high_th);
  }
  CHECK_LAST_CUDA_ERROR();
}

}  // namespace other_ns


DALI_REGISTER_OPERATOR(proposal_matcher, ::other_ns::proposal_matcher<::dali::GPUBackend>, ::dali::GPU);


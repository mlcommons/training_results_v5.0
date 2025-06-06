#include "common.h"
#include "reduce_scatter.h"
#if CUDART_VERSION >= 11000
  #if __CUDA_ARCH__ < 0
    DEFINE_ncclDevKernel_nop(ReduceScatter_Sum_bf16_RING_LL, ncclFuncReduceScatter, FuncSum, __nv_bfloat16, NCCL_ALGO_RING, NCCL_PROTO_LL, 606)
  #else
    DEFINE_ncclDevKernel(ReduceScatter_Sum_bf16_RING_LL, ncclFuncReduceScatter, FuncSum, __nv_bfloat16, NCCL_ALGO_RING, NCCL_PROTO_LL, 606)
  #endif
#endif
#if CUDART_VERSION >= 11000 && __CUDA_ARCH__ >= 0
DEFINE_ncclDevFunc(ReduceScatter_Sum_bf16_COLLNET_DIRECT_SIMPLE, ncclFuncReduceScatter, FuncSum, __nv_bfloat16, NCCL_ALGO_COLLNET_DIRECT, NCCL_PROTO_SIMPLE)
#endif
#if CUDART_VERSION >= 12010 && __CUDA_ARCH__ >= 900
DEFINE_ncclDevFunc(ReduceScatter_Sum_bf16_NVLS_SIMPLE, ncclFuncReduceScatter, FuncSum, __nv_bfloat16, NCCL_ALGO_NVLS, NCCL_PROTO_SIMPLE)
#endif
#if CUDART_VERSION >= 11000 && __CUDA_ARCH__ >= 0
DEFINE_ncclDevFunc(ReduceScatter_Sum_bf16_PAT_SIMPLE, ncclFuncReduceScatter, FuncSum, __nv_bfloat16, NCCL_ALGO_PAT, NCCL_PROTO_SIMPLE)
#endif
#if CUDART_VERSION >= 11000 && __CUDA_ARCH__ >= 0
DEFINE_ncclDevFunc(ReduceScatter_Sum_bf16_RING_LL, ncclFuncReduceScatter, FuncSum, __nv_bfloat16, NCCL_ALGO_RING, NCCL_PROTO_LL)
#endif
#if CUDART_VERSION >= 11000 && __CUDA_ARCH__ >= 0
DEFINE_ncclDevFunc(ReduceScatter_Sum_bf16_RING_LL128, ncclFuncReduceScatter, FuncSum, __nv_bfloat16, NCCL_ALGO_RING, NCCL_PROTO_LL128)
#endif
#if CUDART_VERSION >= 11000 && __CUDA_ARCH__ >= 0
DEFINE_ncclDevFunc(ReduceScatter_Sum_bf16_RING_SIMPLE, ncclFuncReduceScatter, FuncSum, __nv_bfloat16, NCCL_ALGO_RING, NCCL_PROTO_SIMPLE)
#endif

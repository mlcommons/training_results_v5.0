#include "common.h"
#include "reduce_scatter.h"
DEFINE_ncclDevKernel(ReduceScatter_Sum_f16_RING_LL, ncclFuncReduceScatter, FuncSum, half, NCCL_ALGO_RING, NCCL_PROTO_LL, 612)
DEFINE_ncclDevFunc(ReduceScatter_Sum_f16_COLLNET_DIRECT_SIMPLE, ncclFuncReduceScatter, FuncSum, half, NCCL_ALGO_COLLNET_DIRECT, NCCL_PROTO_SIMPLE)
#if CUDART_VERSION >= 12010 && __CUDA_ARCH__ >= 900
DEFINE_ncclDevFunc(ReduceScatter_Sum_f16_NVLS_SIMPLE, ncclFuncReduceScatter, FuncSum, half, NCCL_ALGO_NVLS, NCCL_PROTO_SIMPLE)
#endif
DEFINE_ncclDevFunc(ReduceScatter_Sum_f16_PAT_SIMPLE, ncclFuncReduceScatter, FuncSum, half, NCCL_ALGO_PAT, NCCL_PROTO_SIMPLE)
DEFINE_ncclDevFunc(ReduceScatter_Sum_f16_RING_LL, ncclFuncReduceScatter, FuncSum, half, NCCL_ALGO_RING, NCCL_PROTO_LL)
DEFINE_ncclDevFunc(ReduceScatter_Sum_f16_RING_LL128, ncclFuncReduceScatter, FuncSum, half, NCCL_ALGO_RING, NCCL_PROTO_LL128)
DEFINE_ncclDevFunc(ReduceScatter_Sum_f16_RING_SIMPLE, ncclFuncReduceScatter, FuncSum, half, NCCL_ALGO_RING, NCCL_PROTO_SIMPLE)

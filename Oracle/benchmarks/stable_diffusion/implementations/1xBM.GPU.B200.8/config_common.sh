# Copyright (c) 2023-2025, NVIDIA CORPORATION.  All rights reserved.
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

# Those variables must be set specifically for each config
: ${DGXNNODES:?DGXNNODES must be set}
: ${DGXNGPU:?DGXNGPU must be set}
: ${BATCHSIZE:?BATCHSIZE must be set}

# Enable offline CLIP text processing
CLIP_TEXT_ENCODER=${CLIP_TEXT_ENCODER:-offline}
if [ "${CLIP_TEXT_ENCODER}" == "offline" ]; then

    # if OFFLINE CLIP is used we force the use of the appropriate config
    if [ -z "${CONFIG_NAME:-}" ]; then
    export CONFIG_NAME="sd2_mlperf_train_moments_encoded"
    else
    echo "WARNING: CONFIG_NAME is explicitly set and CLIP_TEXT_ENCODER=offline. CLIP_TEXT_ENCODER will be ignored."
    fi
fi

# Training knobs
export EXP_NAME=${EXP_NAME:-stable-diffusion2-train-$(date +%y%m%d%H%M%S%N)}
export RANDOM_SEED=${RANDOM_SEED:-$RANDOM}
export CONFIG_PATH=${CONFIG_PATH:-conf}
export CONFIG_NAME=${CONFIG_NAME:-sd2_mlperf_train_moments}
export CONFIG_MAX_STEPS=${CONFIG_MAX_STEPS:-1000}
export INFER_NUM_IMAGES=${INFER_NUM_IMAGES:-30000}
export INFER_START_STEP=${INFER_START_STEP:-0}
export INFER_BATCH_SIZE=${INFER_BATCH_SIZE:-32}
export BASE_LR=${BASE_LR:-"0.000000125"}
export WARMUP_STEPS=${WARMUP_STEPS:-1000}

# Print the variables
echo "EXP_NAME=${EXP_NAME}"
echo "RANDOM_SEED=${RANDOM_SEED}"
echo "CONFIG_PATH=${CONFIG_PATH}"
echo "CONFIG_NAME=${CONFIG_NAME}"
echo "CONFIG_MAX_STEPS=${CONFIG_MAX_STEPS}"
echo "INFER_NUM_IMAGES=${INFER_NUM_IMAGES}"
echo "INFER_START_STEP=${INFER_START_STEP}"
echo "INFER_BATCH_SIZE=${INFER_BATCH_SIZE}"
echo "BASE_LR=${BASE_LR}"
echo "WARMUP_STEPS=${WARMUP_STEPS}"

GLOBAL_BATCH_SIZE=$(expr $DGXNNODES \* $DGXNGPU \* $BATCHSIZE)
export LEARNING_RATE=$(awk "BEGIN {print $BASE_LR * $GLOBAL_BATCH_SIZE}")

# By default we create a checkpoint every 512000 samples (benchmark requirements)
export CHECKPOINT_STEPS=${CHECKPOINT_STEPS:-$(( 512000 / GLOBAL_BATCH_SIZE ))}
echo "CHECKPOINT_STEPS=${CHECKPOINT_STEPS}"

# Performance knobs
export FLASH_ATTENTION=${FLASH_ATTENTION:-True}
export USE_TE_DPA=${USE_TE_DPA:-False}
export USE_CUDNN_LAYER_NORM=${USE_CUDNN_LAYER_NORM:-True}
export USE_TORCH_SCHED=${USE_TORCH_SCHED:-False}
export USE_DIST_OPTIMIZER=${USE_DIST_OPTIMIZER:-True}
export APEX_GROUP_NORM_BPROP_SM_MARGIN=32

if [[ "${USE_TORCH_SCHED,,}" == "true"  ]]; then
    export CUDA_DEVICE_MAX_CONNECTIONS=128
fi

# Runner knobs
export CHECK_COMPLIANCE=${CHECK_COMPLIANCE:-1}
export WALLTIME=$(( ${NEXP:-1} * ${WALLTIME}))

export HF_HUB_OFFLINE=1 # disable network request for clip checkpoint
export HYDRA_FULL_ERROR=1

echo "DGXSYSTEM=${DGXSYSTEM}"

export NCCL_NVLS_ENABLE=1
export NCCL_GRAPH_REGISTER=0
export NCCL_LOCAL_REGISTER=0

#---------------------- OCI Extras -------------------
set -eux
export PMI_DEBUG=1
export OMPI_MCA_pml=ucx
export OMPI_MCA_btl=^openib
export OMPI_MCA_btl_tcp_if_include="10.224.0.0/12"
export PMIX_MCA_gds="^ds12" \
      NCCL_SOCKET_NTHREADS=16 \
      NCCL_DEBUG=WARN \
      NCCL_CUMEM_ENABLE=0 \
      NCCL_IB_SPLIT_DATA_ON_QPS=0 \
      NCCL_IB_QPS_PER_CONNECTION=1 \
      NCCL_IB_GID_INDEX=3 \
      NCCL_IB_TC=41 \
      NCCL_IB_SL=0 \
      NCCL_IB_TIMEOUT=22 \
      NCCL_NET_PLUGIN=none \
      NCCL_SOCKET_IFNAME=eth0 \
      NCCL_IGNORE_CPU_AFFINITY=1 \
      RX_QUEUE_LEN=8192 \
      IB_RX_QUEUE_LEN=8192 \
      UCX_NET_DEVICES=eth0 \
      UCX_TLS=tcp \
      HCOLL_ENABLE_MCAST_ALL=0 \
      coll_hcoll_enable=0 \
      NCCL_IB_HCA='=mlx5_0,mlx5_3,mlx5_4,mlx5_5,mlx5_6,mlx5_9,mlx5_10,mlx5_11'

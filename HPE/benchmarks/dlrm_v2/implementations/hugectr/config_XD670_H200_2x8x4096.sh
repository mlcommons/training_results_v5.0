## DL params
export RUN_SCRIPT="train.py"
export BATCHSIZE=65536
export BATCHSIZE_EVAL=1048576
export LEARNING_RATE=0.004
export USE_MIXED_PRECISION=true
export SCALER=20480
export SHARDING_PLAN=hier_auto
export MEM_COMM_BW_RATIO=67
export GEN_LOSS_SUMMARY=true
export MINIMUM_TRAINING_TIME=10
export DP_SHARDING_THRESHOLD=0.0125

## System run parms
export DGXNNODES=2
export DGXNGPU=8
export DGXSYSTEM=$(basename $(readlink -f ${BASH_SOURCE[0]}) | sed 's/^config_//' | sed 's/\.sh$//' )
export WALLTIME_RUNANDTIME=10

## Set clocks and walltime for maxQ and minEDP runs
if [[ "${SET_MAXQ_CLK:-0}" == "1" ]]; then
  export MAXQ_CLK=1320
  WALLTIME_RUNANDTIME=$(expr ${WALLTIME_RUNANDTIME} + ${WALLTIME_RUNANDTIME} / 2) # 50% longer walltime
elif [[ "${SET_MINEDP_CLK:-0}" == "1" ]]; then
  export MINEDP_CLK=1665
  WALLTIME_RUNANDTIME=$(expr ${WALLTIME_RUNANDTIME} + ${WALLTIME_RUNANDTIME} / 3) # 33% longer walltime
fi
export WALLTIME=$((5 + ${NEXP:-1} * ($WALLTIME_RUNANDTIME + 5)))

## network flags
#export SBATCH_NETWORK=sharp
#export NCCL_COLLNET_ENABLE=1

#Performance paramenter
export DLRM_BIND="./bindpcie --cpu=bind_cpu_topology.sh --mem=bind_mem_topology.sh --ib=single"
#Mandatory param NCCL_IB_GID_INDEX=3
export NCCL_IB_GID_INDEX=3
#export NCCL_TEST=0

export NCCL_DEBUG=INFO
#export NCCL_DEBUG_SUBSYS=INIT,net,sys


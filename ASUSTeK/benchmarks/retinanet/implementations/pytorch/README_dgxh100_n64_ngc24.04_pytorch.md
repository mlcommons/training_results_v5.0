## Steps to launch training

### NVIDIA DGX H100 (multi node)

Launch configuration and system-specific hyperparameters for the NVIDIA DGX H100
multi node submission are in the `config_DGXH100_064x08x002.sh` script.

Steps required to launch multi node training on NVIDIA DGX H100

1. Build the docker container and push to a docker registry

```
docker build --pull -t <DOCKER_REGISTRY>/mlperf-nvidia:single_stage_detector-pytorch .
docker push <DOCKER_REGISTRY>/mlperf-nvidia:single_stage_detector-pytorch
```

2. Launch the training

```
source config_DGXH100_064x08x002.sh
CONT="<DOCKER_REGISTRY>/mlperf-nvidia:single_stage_detector-pytorch" DATADIR=<path/to/data/dir> LOGDIR=<path/to/output/dir> sbatch -N $DGXNNODES -t $WALLTIME run.sub
```

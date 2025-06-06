# Copyright (c) 2024, NVIDIA CORPORATION.  All rights reserved.
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

import glob
import os
import subprocess
import warnings
from typing import List, Optional

from tensorrt_llm.models import PretrainedConfig

from nemo.export.trt_llm.qnemo.utils import CONFIG_NAME, WEIGHTS_NAME


def qnemo_to_tensorrt_llm(
    nemo_checkpoint_path: str,
    engine_dir: str,
    max_input_len: int,
    max_output_len: int,
    max_seq_len: Optional[int],
    max_batch_size: int,
    max_prompt_embedding_table_size: int,
    tensor_parallel_size: Optional[int] = None,
    pipeline_parallel_size: Optional[int] = None,
    use_parallel_embedding: bool = False,
    paged_kv_cache: bool = True,
    remove_input_padding: bool = True,
    use_lora_plugin: Optional[str] = None,
    lora_target_modules: Optional[List[str]] = None,
    max_lora_rank: int = 64,
    max_num_tokens: Optional[int] = None,
    opt_num_tokens: Optional[int] = None,
    max_beam_width: int = 1,
    multiple_profiles: bool = False,
):
    """Build TensorRT-LLM engine with trtllm-build command in a subprocess."""
    assert not lora_target_modules, f"LoRA is not supported for quantized checkpoints, got {lora_target_modules}"

    warnings.warn(
        "Note that setting tensor_parallel_size, pipeline_parallel_size and use_parallel_embedding "
        " parameters for quantized models is done on calibration step with nemo.export.quantize module."
        " These parameters are ignored when building and running TensorRT-LLM engine below.",
        UserWarning,
        stacklevel=3,
    )

    num_build_workers = len(glob.glob(os.path.join(nemo_checkpoint_path, WEIGHTS_NAME.format("*"))))
    assert num_build_workers, f"No TensorRT-LLM weight files found in {nemo_checkpoint_path}"

    config = PretrainedConfig.from_json_file(os.path.join(nemo_checkpoint_path, CONFIG_NAME))

    log_level = "warning"

    quant_algo = config.quantization.quant_algo

    use_fused_mlp = quant_algo in [
        "FP8",
        None,
    ] and config.hidden_act in ["silu", "swiglu", "fast-swiglu", "gelu", "geglu"]

    use_qdq = quant_algo in ["FP8", "W8A8_SQ_PER_CHANNEL"]

    builder_opt = 4 if "RecurrentGemma" not in config.architecture else 0

    speculative_decoding_mode = "medusa" if "Medusa" in config.architecture else None

    build_cmd = "trtllm-build "
    build_cmd += f"--checkpoint_dir {nemo_checkpoint_path} "
    build_cmd += f"--log_level {log_level} "
    build_cmd += f"--output_dir {engine_dir} "
    build_cmd += f"--workers {num_build_workers} "
    build_cmd += f"--max_batch_size {max_batch_size} "
    build_cmd += f"--max_input_len {max_input_len} "
    build_cmd += f"--max_output_len {max_output_len} "
    build_cmd += f"--max_beam_width {max_beam_width} "
    build_cmd += f"--tp_size {config.mapping.tp_size} "
    build_cmd += f"--pp_size {config.mapping.pp_size} "
    build_cmd += f"--max_prompt_embedding_table_size {max_prompt_embedding_table_size} "
    build_cmd += f"--builder_opt {builder_opt} "
    build_cmd += f"--gpt_attention_plugin {config.dtype} "
    build_cmd += f"--nccl_plugin {config.dtype} "
    build_cmd += f"--paged_kv_cache {'enable' if paged_kv_cache else 'disable'} "
    build_cmd += f"--remove_input_padding {'enable' if remove_input_padding else 'disable'} "
    build_cmd += f"--multiple_profiles {'enable' if multiple_profiles else 'disable'} "

    if use_fused_mlp:
        build_cmd += "--use_fused_mlp " if "RecurrentGemma" not in config.architecture else ""

    if not use_qdq:
        build_cmd += f"--gemm_plugin {config.dtype} "

    if max_seq_len:
        build_cmd += f"--max_seq_len {max_seq_len} "

    if max_num_tokens is not None:
        build_cmd += f"--max_num_tokens {max_num_tokens} "
    else:
        build_cmd += f"--max_num_tokens {max_batch_size * max_input_len} "

    if opt_num_tokens is not None:
        build_cmd += f"--opt_num_tokens {opt_num_tokens} "

    if speculative_decoding_mode:
        build_cmd += f"--speculative_decoding_mode {speculative_decoding_mode} "

    build_cmd = build_cmd.replace("--", "\\\n  --")  # Separate parameters line by line

    print("trtllm-build command:")
    print(build_cmd)

    subprocess.run(build_cmd, shell=True, check=True)

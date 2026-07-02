from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

import psutil


@dataclass(frozen=True)
class GpuInfo:
    name: str
    memory_gb: float


@dataclass(frozen=True)
class HardwareProfile:
    cpu_count: int
    memory_gb: float
    gpu: GpuInfo | None


@dataclass(frozen=True)
class ModelRecommendation:
    whisper_model: str
    whisper_device: str
    whisper_compute_type: str
    ollama_model: str
    reason: str


def detect_hardware() -> HardwareProfile:
    cpu_count = os.cpu_count() or 1
    memory_gb = psutil.virtual_memory().total / 1024**3
    gpu = _detect_nvidia_gpu()
    return HardwareProfile(cpu_count=cpu_count, memory_gb=round(memory_gb, 1), gpu=gpu)


def recommend_models(profile: HardwareProfile | None = None) -> ModelRecommendation:
    profile = profile or detect_hardware()
    forced_whisper = os.getenv("WHISPER_MODEL")
    forced_ollama = os.getenv("OLLAMA_MODEL")

    if profile.gpu and profile.gpu.memory_gb >= 20:
        whisper_model = "large-v3"
        ollama_model = "qwen2.5:14b"
        reason = "偵測到 20GB 以上 VRAM，可使用較高品質模型。"
    elif profile.gpu and profile.gpu.memory_gb >= 10:
        whisper_model = "medium"
        ollama_model = "qwen2.5:7b"
        reason = "偵測到 10GB 以上 VRAM，優先平衡轉錄品質、中文整理能力與延遲。"
    elif profile.gpu and profile.gpu.memory_gb >= 6:
        whisper_model = "small"
        ollama_model = "qwen2.5:7b"
        reason = "偵測到中階 GPU，降低 Whisper 模型大小以保留 LLM 推論空間。"
    elif profile.memory_gb >= 24:
        whisper_model = "small"
        ollama_model = "qwen2.5:3b"
        reason = "未偵測到可用 GPU，但記憶體足夠，以 CPU 模式執行較小模型。"
    else:
        whisper_model = "base"
        ollama_model = "qwen2.5:1.5b"
        reason = "硬體資源有限，使用輕量模型避免處理時間過長。"

    device = "cuda" if profile.gpu else "cpu"
    compute_type = "float16" if profile.gpu else "int8"

    return ModelRecommendation(
        whisper_model=forced_whisper or whisper_model,
        whisper_device=os.getenv("WHISPER_DEVICE") or device,
        whisper_compute_type=os.getenv("WHISPER_COMPUTE_TYPE") or compute_type,
        ollama_model=forced_ollama or ollama_model,
        reason=reason,
    )


def _detect_nvidia_gpu() -> GpuInfo | None:
    if not shutil.which("nvidia-smi"):
        return None

    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return None

    first_line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    if not first_line:
        return None

    name, memory_mb = [part.strip() for part in first_line.rsplit(",", 1)]
    return GpuInfo(name=name, memory_gb=round(float(memory_mb) / 1024, 1))

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import psutil


class ExternalAPIConfigurationError(ValueError):
    pass


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
    chat_base_url: str
    chat_api_key_env: str
    llm_model: str
    reason: str


def detect_hardware() -> HardwareProfile:
    cpu_count = os.cpu_count() or 1
    memory_gb = psutil.virtual_memory().total / 1024**3
    gpu = _detect_nvidia_gpu()
    return HardwareProfile(cpu_count=cpu_count, memory_gb=round(memory_gb, 1), gpu=gpu)


def recommend_models(profile: HardwareProfile | None = None) -> ModelRecommendation:
    profile = profile or detect_hardware()
    forced_whisper = os.getenv("WHISPER_MODEL")
    if profile.gpu and profile.gpu.memory_gb >= 20:
        whisper_model = "large-v3"
        reason = "偵測到 20GB 以上 VRAM，使用較高品質的本機 Whisper；報告 LLM 使用外部 API。"
    elif profile.gpu and profile.gpu.memory_gb >= 10:
        whisper_model = "medium"
        reason = "偵測到 10GB 以上 VRAM，使用 GPU Whisper medium；報告 LLM 使用外部 API。"
    elif profile.gpu and profile.gpu.memory_gb >= 6:
        whisper_model = "small"
        reason = "偵測到中階 GPU，使用 GPU Whisper small；報告 LLM 使用外部 API。"
    elif profile.memory_gb >= 24:
        whisper_model = "small"
        reason = "未偵測到 GPU，Whisper 以 CPU 執行；報告 LLM 仍使用外部 API。"
    else:
        whisper_model = "base"
        reason = "硬體資源有限，Whisper 使用 base；報告 LLM 使用外部 API。"

    chat_base_url = os.getenv(
        "MEETING_CHAT_API_BASE_URL",
        "https://generativelanguage.googleapis.com/v1beta/openai",
    ).strip().rstrip("/")
    chat_api_key_env = os.getenv("MEETING_CHAT_API_KEY_ENV", "MEETING_CHAT_API_KEY").strip()
    llm_model = os.getenv("MEETING_CHAT_MODEL", "gemini-3.5-flash").strip()
    validate_external_configuration(chat_base_url, chat_api_key_env, llm_model)
    return ModelRecommendation(
        whisper_model=forced_whisper or whisper_model,
        whisper_device=os.getenv("WHISPER_DEVICE") or ("cuda" if profile.gpu else "cpu"),
        whisper_compute_type=os.getenv("WHISPER_COMPUTE_TYPE") or ("float16" if profile.gpu else "int8"),
        chat_base_url=chat_base_url,
        chat_api_key_env=chat_api_key_env,
        llm_model=llm_model,
        reason=reason,
    )


def validate_external_configuration(base_url: str, api_key_env: str, model: str) -> None:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ExternalAPIConfigurationError("MEETING_CHAT_API_BASE_URL 必須是完整 HTTP(S) URL。")
    if not api_key_env:
        raise ExternalAPIConfigurationError("MEETING_CHAT_API_KEY_ENV 不可為空白。")
    if not model:
        raise ExternalAPIConfigurationError("MEETING_CHAT_MODEL 不可為空白。")


def api_endpoint(base_url: str, suffix: str) -> str:
    parsed = urlsplit(base_url.rstrip("/"))
    path = f"{parsed.path.rstrip('/')}{suffix}"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _detect_nvidia_gpu() -> GpuInfo | None:
    if not shutil.which("nvidia-smi"):
        return None
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
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

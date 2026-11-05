from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


class ExternalAPIConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class ModelRecommendation:
    google_api_base_url: str
    google_api_key_env: str
    transcription_model: str
    llm_model: str
    whisper_model: str
    whisper_device: str
    whisper_compute_type: str
    reason: str


def recommend_models() -> ModelRecommendation:
    google_api_base_url = os.getenv(
        "GOOGLE_API_BASE_URL",
        "https://generativelanguage.googleapis.com/v1beta",
    ).strip().rstrip("/")
    google_api_key_env = os.getenv("GOOGLE_API_KEY_ENV", "GEMINI_API_KEY").strip()
    transcription_model = os.getenv("GOOGLE_TRANSCRIPTION_MODEL", "gemini-3.1-flash-lite").strip()
    llm_model = os.getenv("GOOGLE_LLM_MODEL", "gemini-3.1-flash-lite").strip()
    whisper_model = os.getenv("WHISPER_MODEL", "base").strip()
    whisper_device = os.getenv("WHISPER_DEVICE", "cpu").strip()
    whisper_compute_type = os.getenv("WHISPER_COMPUTE_TYPE", "int8").strip()
    validate_external_configuration(
        google_api_base_url,
        google_api_key_env,
        transcription_model,
        llm_model,
        whisper_model,
        whisper_device,
        whisper_compute_type,
    )
    return ModelRecommendation(
        google_api_base_url=google_api_base_url,
        google_api_key_env=google_api_key_env,
        transcription_model=transcription_model,
        llm_model=llm_model,
        whisper_model=whisper_model,
        whisper_device=whisper_device,
        whisper_compute_type=whisper_compute_type,
        reason="預設使用本機 fast-whisper 轉錄，網頁可切換成 Google Gemini API 轉錄；會議報告整理使用 Google Gemini API。",
    )


def validate_external_configuration(
    google_api_base_url: str,
    google_api_key_env: str,
    transcription_model: str,
    llm_model: str,
    whisper_model: str,
    whisper_device: str,
    whisper_compute_type: str,
) -> None:
    parsed = urlsplit(google_api_base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ExternalAPIConfigurationError("GOOGLE_API_BASE_URL 必須是完整 HTTP(S) URL。")
    if not google_api_key_env:
        raise ExternalAPIConfigurationError("GOOGLE_API_KEY_ENV 不可為空白。")
    if not transcription_model:
        raise ExternalAPIConfigurationError("GOOGLE_TRANSCRIPTION_MODEL 不可為空白。")
    if not llm_model:
        raise ExternalAPIConfigurationError("GOOGLE_LLM_MODEL 不可為空白。")
    if not whisper_model:
        raise ExternalAPIConfigurationError("WHISPER_MODEL 不可為空白。")
    if not whisper_device:
        raise ExternalAPIConfigurationError("WHISPER_DEVICE 不可為空白。")
    if not whisper_compute_type:
        raise ExternalAPIConfigurationError("WHISPER_COMPUTE_TYPE 不可為空白。")


def api_endpoint(base_url: str, suffix: str) -> str:
    parsed = urlsplit(base_url.rstrip("/"))
    path = f"{parsed.path.rstrip('/')}{suffix}"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))

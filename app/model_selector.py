from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


class ExternalAPIConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class ModelRecommendation:
    base_url: str
    api_key_env: str
    transcription_model: str
    llm_model: str
    reason: str


def recommend_models() -> ModelRecommendation:
    base_url = os.getenv("EXTERNAL_API_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
    api_key_env = os.getenv("EXTERNAL_API_KEY_ENV", "EXTERNAL_API_KEY").strip()
    llm_model = os.getenv("EXTERNAL_LLM_MODEL", "gpt-4o-mini").strip()
    transcription_model = os.getenv("EXTERNAL_TRANSCRIPTION_MODEL", "gpt-4o-mini-transcribe").strip()
    validate_external_configuration(base_url, api_key_env, llm_model, transcription_model)
    return ModelRecommendation(
        base_url=base_url,
        api_key_env=api_key_env,
        transcription_model=transcription_model,
        llm_model=llm_model,
        reason="使用外部 OpenAI-compatible transcription 與 Chat Completions API，不在本機載入 Whisper 或 LLM 模型。",
    )


def validate_external_configuration(
    base_url: str,
    api_key_env: str,
    llm_model: str,
    transcription_model: str,
) -> None:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ExternalAPIConfigurationError("EXTERNAL_API_BASE_URL 必須是完整 HTTP(S) URL。")
    if not api_key_env:
        raise ExternalAPIConfigurationError("EXTERNAL_API_KEY_ENV 不可為空白。")
    if not llm_model:
        raise ExternalAPIConfigurationError("EXTERNAL_LLM_MODEL 不可為空白。")
    if not transcription_model:
        raise ExternalAPIConfigurationError("EXTERNAL_TRANSCRIPTION_MODEL 不可為空白。")


def api_endpoint(base_url: str, suffix: str) -> str:
    parsed = urlsplit(base_url.rstrip("/"))
    path = f"{parsed.path.rstrip('/')}{suffix}"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))

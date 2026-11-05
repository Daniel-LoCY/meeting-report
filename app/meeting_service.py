from __future__ import annotations

import json
import os
import re
import textwrap
import time
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

import requests
from google import genai

from app.model_selector import ModelRecommendation, api_endpoint


class MeetingProcessingError(RuntimeError):
    pass


@lru_cache(maxsize=4)
def get_whisper_model(model_size: str, device: str, compute_type: str):
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise MeetingProcessingError("本機 fast-whisper 尚未安裝，無法使用本地轉錄模式。") from exc
    return WhisperModel(
        model_size,
        device=device,
        compute_type=compute_type,
        download_root=os.getenv("WHISPER_CACHE_DIR", "/models/whisper"),
    )


TRANSCRIPT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "language": {"type": "string"},
        "duration_seconds": {"type": "number"},
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_seconds": {"type": "number"},
                    "end_seconds": {"type": "number"},
                    "speaker": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["start_seconds", "end_seconds", "text"],
            },
        },
    },
    "required": ["language", "segments"],
}


def transcribe_audio(
    file_path: Path,
    recommendation: ModelRecommendation,
    language: str | None,
    progress,
    preview: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    progress("uploading_audio", "上傳音訊或影片至 Google Gemini")
    client = _google_client(recommendation)

    try:
        uploaded_file = client.files.upload(file=str(file_path))
    except Exception as exc:  # noqa: BLE001
        raise MeetingProcessingError(f"Google Gemini 檔案上傳失敗：{_safe_api_error(exc)}") from exc

    progress("transcribing", "Google Gemini 音訊或影片轉文字")
    prompt = build_transcription_prompt(language)
    try:
        response = client.models.generate_content(
            model=recommendation.transcription_model,
            contents=[uploaded_file, prompt],
            config={
                "temperature": 0.0,
                "response_mime_type": "application/json",
                "response_schema": TRANSCRIPT_SCHEMA,
            },
        )
    except Exception as exc:  # noqa: BLE001
        raise MeetingProcessingError(f"Google Gemini 轉錄失敗：{_safe_api_error(exc)}") from exc

    raw_text = _response_text(response)
    payload = _parse_json_response(raw_text, "Google Gemini 轉錄", strict=False)
    if payload is None:
        rows = []
        formatted = raw_text.strip()
        language_value = language or "unknown"
        duration_value = None
    else:
        rows = _transcript_segments(payload)
        fallback_text = str(payload.get("text") or "").strip()
        formatted = _format_transcript(rows, fallback_text)
        language_value = str(payload.get("language") or language or "unknown")
        duration_value = _transcript_duration(rows, payload.get("duration_seconds"))
    if not formatted:
        raise MeetingProcessingError("Google Gemini 回傳空白逐字稿。")
    if preview:
        preview(formatted)

    return {
        "provider": "google_gemini_api",
        "language": language_value,
        "language_probability": None,
        "duration": duration_value,
        "segments": rows,
        "transcript": formatted,
    }


def transcribe_audio_local_whisper(
    file_path: Path,
    recommendation: ModelRecommendation,
    language: str | None,
    progress,
    preview: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    progress("loading_whisper", "載入本機 fast-whisper 模型")
    model = get_whisper_model(
        recommendation.whisper_model,
        recommendation.whisper_device,
        recommendation.whisper_compute_type,
    )
    progress("transcribing", "本機 fast-whisper 音訊轉文字")
    segments, info = model.transcribe(
        str(file_path),
        language=language or None,
        vad_filter=True,
        beam_size=5,
    )
    rows: list[dict[str, Any]] = []
    transcript_parts: list[str] = []
    for segment in segments:
        text = str(segment.text).strip()
        if not text:
            continue
        start = round(float(segment.start), 2)
        end = round(float(segment.end), 2)
        rows.append({"start": start, "end": end, "speaker": "", "text": text})
        transcript_parts.append(f"[{_fmt_time(start)} - {_fmt_time(end)}] {text}")
        if preview:
            preview("\n".join(transcript_parts))
    transcript = "\n".join(transcript_parts)
    if not transcript:
        raise MeetingProcessingError("本機 fast-whisper 回傳空白逐字稿。")
    return {
        "provider": "local_fast_whisper",
        "language": getattr(info, "language", language or "unknown"),
        "language_probability": round(float(getattr(info, "language_probability", 0.0)), 4),
        "duration": round(float(getattr(info, "duration", 0.0)), 2),
        "segments": rows,
        "transcript": transcript,
    }


def generate_report(
    transcript: str,
    meeting_title: str,
    recommendation: ModelRecommendation,
    output_language: str,
    report_mode: str,
    progress,
    preview: Callable[[str], None] | None = None,
) -> str:
    if not transcript.strip():
        raise MeetingProcessingError("轉錄結果為空，無法產生會議報告。")

    progress("checking_llm", "確認 Google Gemini API")
    chunks = split_text(transcript, max_chars=9000)
    if len(chunks) == 1:
        progress("summarizing", "由 Google Gemini 整理會議報告")
        report = call_external_llm(
            recommendation,
            build_final_prompt(meeting_title, transcript, output_language, report_mode),
        )
        if preview:
            preview(report)
        return report

    partials: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        progress("summarizing", f"Google Gemini 整理分段摘要 {index}/{len(chunks)}")
        partials.append(
            call_external_llm(
                recommendation,
                build_chunk_prompt(meeting_title, chunk, output_language, index, len(chunks)),
            )
        )
        if preview:
            preview("\n\n".join(partials))

    progress("summarizing", "Google Gemini 合併分段摘要")
    report = call_external_llm(
        recommendation,
        build_final_prompt(meeting_title, "\n\n".join(partials), output_language, report_mode),
    )
    if preview:
        preview(report)
    return report


def call_external_llm(recommendation: ModelRecommendation, prompt: str) -> str:
    client = _google_client(recommendation)
    try:
        response = client.models.generate_content(
            model=recommendation.llm_model,
            contents=prompt,
            config={
                "system_instruction": "你是嚴謹的會議記錄整理助理。",
                "temperature": 0.2,
            },
        )
    except Exception as exc:  # noqa: BLE001
        raise MeetingProcessingError(f"Google Gemini 產生報告失敗：{_safe_api_error(exc)}") from exc

    text = _response_text(response).strip()
    if not text:
        raise MeetingProcessingError("Google Gemini 回傳空白結果。")
    return text


def external_api_status(recommendation: ModelRecommendation, include_transcription: bool = True) -> dict[str, Any]:
    try:
        key = _api_key(recommendation)
        response = requests.get(
            api_endpoint(recommendation.google_api_base_url, "/models"),
            headers={"x-goog-api-key": key},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
    except (MeetingProcessingError, ValueError, requests.RequestException) as exc:
        return {"available": False, "detail": _safe_api_error(exc)}

    models = payload.get("models") if isinstance(payload, dict) else []
    model_ids = _model_ids(models)
    required = {recommendation.llm_model}
    if include_transcription:
        required.add(recommendation.transcription_model)
    missing = sorted(model for model in required if model not in model_ids)
    if missing:
        return {"available": False, "detail": f"Google Gemini API 找不到設定模型：{', '.join(missing)}"}
    return {"available": True, "model_check": "passed", "detail": "Google Gemini API key 與模型設定可用。"}


def local_whisper_status(recommendation: ModelRecommendation) -> dict[str, Any]:
    try:
        from faster_whisper import WhisperModel  # noqa: F401
    except ImportError:
        return {"available": False, "detail": "容器內未安裝 faster-whisper。"}
    return {
        "available": True,
        "detail": "本機 fast-whisper 已安裝；首次使用模型時會下載或載入快取。",
        "model": recommendation.whisper_model,
        "device": recommendation.whisper_device,
        "compute_type": recommendation.whisper_compute_type,
    }


def build_transcription_prompt(language: str | None) -> str:
    language_hint = language or "auto"
    return textwrap.dedent(
        f"""
        請將音訊或影片中的語音完整轉成逐字稿。語言提示：{language_hint}。
        請保留原語言，不要摘要、不要翻譯、不要省略內容。
        若可辨識不同講者，請填入 speaker；無法辨識時可省略或使用「未指定」。
        請以 JSON schema 回傳，每個 segment 都要包含 start_seconds、end_seconds、text。
        """
    ).strip()


def build_chunk_prompt(
    meeting_title: str,
    transcript: str,
    output_language: str,
    index: int,
    total: int,
) -> str:
    return textwrap.dedent(
        f"""
        你是專業會議記錄整理助理。請用{output_language}整理第 {index}/{total} 段逐字稿。

        會議名稱：{meeting_title or "未命名會議"}

        請輸出 Markdown，包含：
        - 本段重點
        - 決議
        - 待辦事項：負責人、任務、期限；未知請寫「未指定」
        - 風險或待確認問題

        逐字稿：
        {transcript}
        """
    ).strip()


def build_final_prompt(
    meeting_title: str,
    transcript_or_notes: str,
    output_language: str,
    report_mode: str,
) -> str:
    mode_hint = {
        "executive": "偏主管摘要，先講結論與決策，細節保持精簡。",
        "action": "偏行動追蹤，強化待辦事項、負責人、期限、阻塞點。",
        "standard": "平衡摘要、決議、待辦事項與逐字稿依據。",
    }.get(report_mode, "平衡摘要、決議、待辦事項與逐字稿依據。")

    return textwrap.dedent(
        f"""
        你是專業會議記錄整理助理。請根據以下內容產生可直接交付的會議記錄整理報告。
        請使用{output_language}，輸出 Markdown，不要編造逐字稿中沒有的事實；若資訊不足，請明確標示「未指定」或「待確認」。

        會議名稱：{meeting_title or "未命名會議"}
        報告風格：{mode_hint}

        報告必須包含以下章節：
        # {meeting_title or "會議記錄整理報告"}
        ## 1. 結論摘要
        ## 2. 主要討論重點
        ## 3. 決議事項
        ## 4. 待辦事項
        使用表格欄位：項目、負責人、期限、狀態、依據
        ## 5. 風險與待確認問題
        ## 6. 後續建議

        內容來源：
        {transcript_or_notes}
        """
    ).strip()


def split_text(text: str, max_chars: int) -> list[str]:
    paragraphs = text.splitlines()
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for paragraph in paragraphs:
        paragraph_len = len(paragraph) + 1
        if current and current_len + paragraph_len > max_chars:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(paragraph)
        current_len += paragraph_len
    if current:
        chunks.append("\n".join(current))
    return chunks


def decorate_report(
    report: str,
    transcript_meta: dict[str, Any],
    recommendation: ModelRecommendation,
    source_name: str,
) -> str:
    generated_at = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    metadata = textwrap.dedent(
        f"""
        ---
        report_type: meeting_minutes
        generated_at: "{generated_at}"
        source_file: "{source_name}"
        detected_language: "{transcript_meta.get("language")}"
        audio_duration_seconds: {transcript_meta.get("duration")}
        transcription_model: "{recommendation.transcription_model}"
        llm_model: "{recommendation.llm_model}"
        whisper_model: "{recommendation.whisper_model}"
        whisper_device: "{recommendation.whisper_device}"
        whisper_compute_type: "{recommendation.whisper_compute_type}"
        inference_provider: "{transcript_meta.get("provider", "google_gemini_api")}"
        known_limits: "AI 自動轉錄與整理可能誤判講者、期限與專有名詞；正式發布前需人工覆核。"
        ---

        """
    ).lstrip()
    return metadata + report.strip() + "\n"


def _google_client(recommendation: ModelRecommendation):
    return genai.Client(api_key=_api_key(recommendation))


def _api_key(recommendation: ModelRecommendation) -> str:
    key = os.getenv(recommendation.google_api_key_env, "").strip()
    if not key:
        raise MeetingProcessingError(f"尚未設定 Google Gemini API key 環境變數：{recommendation.google_api_key_env}")
    return key


def _response_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if output_text is not None:
        return str(output_text)
    text = getattr(response, "text", None)
    if text is not None:
        return str(text)
    return str(response)


def _parse_json_response(text: str, label: str, strict: bool = True) -> dict[str, Any] | None:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    candidates = [cleaned]
    extracted = _extract_json_object(cleaned)
    if extracted and extracted != cleaned:
        candidates.append(extracted)
    last_error: json.JSONDecodeError | None = None
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if not isinstance(payload, dict):
            if strict:
                raise MeetingProcessingError(f"{label} 回傳格式無效。")
            return None
        return payload
    if strict:
        raise MeetingProcessingError(f"{label} 回傳格式不是有效 JSON。") from last_error
    return None


def _extract_json_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(text[start:], start=start):
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_string:
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _transcript_segments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list):
        return []
    rows: list[dict[str, Any]] = []
    for segment in raw_segments:
        if not isinstance(segment, dict):
            continue
        text = str(segment.get("text") or segment.get("content") or "").strip()
        if not text:
            continue
        try:
            start = round(float(segment.get("start_seconds", segment.get("start", 0))), 2)
            end = round(float(segment.get("end_seconds", segment.get("end", start))), 2)
        except (TypeError, ValueError):
            continue
        speaker = str(segment.get("speaker") or "").strip()
        rows.append({"start": start, "end": max(start, end), "speaker": speaker, "text": text})
    return rows


def _format_transcript(rows: list[dict[str, Any]], fallback: str) -> str:
    if not rows:
        return fallback
    lines = []
    for row in rows:
        speaker = f"{row['speaker']}: " if row.get("speaker") else ""
        lines.append(f"[{_fmt_time(row['start'])} - {_fmt_time(row['end'])}] {speaker}{row['text']}")
    return "\n".join(lines)


def _transcript_duration(rows: list[dict[str, Any]], fallback: Any) -> float | None:
    if rows:
        return max(float(row["end"]) for row in rows)
    try:
        return round(float(fallback), 2)
    except (TypeError, ValueError):
        return None


def _model_ids(models: Any) -> set[str]:
    if not isinstance(models, list):
        return set()
    ids: set[str] = set()
    for model in models:
        if not isinstance(model, dict):
            continue
        name = str(model.get("name") or "")
        if not name:
            continue
        ids.add(name)
        ids.add(name.removeprefix("models/"))
    return ids


def _safe_api_error(exc: Exception) -> str:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"HTTP {exc.response.status_code}"
    return str(exc)


def _fmt_time(seconds: float) -> str:
    total = int(seconds)
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"

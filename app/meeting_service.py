from __future__ import annotations

import mimetypes
import os
import textwrap
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import requests

from app.model_selector import ModelRecommendation, api_endpoint


class MeetingProcessingError(RuntimeError):
    pass


def transcribe_audio(
    file_path: Path,
    recommendation: ModelRecommendation,
    language: str | None,
    progress,
    preview: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    progress("uploading_audio", "上傳音訊至外部轉錄 API")
    key = _api_key(recommendation)
    mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    fields: dict[str, str] = {
        "model": recommendation.transcription_model,
        "response_format": os.getenv("EXTERNAL_TRANSCRIPTION_RESPONSE_FORMAT", "verbose_json"),
    }
    if language:
        fields["language"] = language

    try:
        with file_path.open("rb") as audio_file:
            response = requests.post(
                api_endpoint(recommendation.base_url, "/audio/transcriptions"),
                headers={"Authorization": f"Bearer {key}"},
                data=fields,
                files={"file": (file_path.name, audio_file, mime_type)},
                timeout=(30, 1800),
            )
        response.raise_for_status()
        payload = response.json()
    except (OSError, ValueError, requests.RequestException) as exc:
        raise MeetingProcessingError(f"外部語音轉錄 API 失敗：{_safe_api_error(exc)}") from exc

    if not isinstance(payload, dict):
        raise MeetingProcessingError("外部語音轉錄 API 回傳格式無效。")
    transcript = str(payload.get("text") or "").strip()
    if not transcript:
        raise MeetingProcessingError("外部語音轉錄 API 回傳空白逐字稿。")
    rows = _transcript_segments(payload)
    formatted = _format_transcript(rows, transcript)
    if preview:
        preview(formatted)
    progress("transcribing", "外部語音轉錄完成")
    return {
        "language": str(payload.get("language") or language or "unknown"),
        "language_probability": None,
        "duration": _transcript_duration(rows),
        "segments": rows,
        "transcript": formatted,
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

    progress("checking_llm", "確認外部 LLM API")
    chunks = split_text(transcript, max_chars=9000)
    if len(chunks) == 1:
        progress("summarizing", "由外部 LLM 整理會議報告")
        report = call_external_llm(
            recommendation,
            build_final_prompt(meeting_title, transcript, output_language, report_mode),
        )
        if preview:
            preview(report)
        return report

    partials: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        progress("summarizing", f"外部 LLM 整理分段摘要 {index}/{len(chunks)}")
        partials.append(
            call_external_llm(
                recommendation,
                build_chunk_prompt(meeting_title, chunk, output_language, index, len(chunks)),
            )
        )
        if preview:
            preview("\n\n".join(partials))

    progress("summarizing", "外部 LLM 合併分段摘要")
    report = call_external_llm(
        recommendation,
        build_final_prompt(meeting_title, "\n\n".join(partials), output_language, report_mode),
    )
    if preview:
        preview(report)
    return report


def call_external_llm(recommendation: ModelRecommendation, prompt: str) -> str:
    payload = {
        "model": recommendation.llm_model,
        "messages": [
            {"role": "system", "content": "你是嚴謹的會議記錄整理助理。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }
    try:
        response = requests.post(
            api_endpoint(recommendation.base_url, "/chat/completions"),
            headers={"Authorization": f"Bearer {_api_key(recommendation)}"},
            json=payload,
            timeout=(30, 1200),
        )
        response.raise_for_status()
        data = response.json()
    except (ValueError, requests.RequestException) as exc:
        raise MeetingProcessingError(f"外部 LLM API 產生報告失敗：{_safe_api_error(exc)}") from exc

    try:
        choices = data["choices"]
        message = choices[0]["message"]
        text = str(message["content"]).strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise MeetingProcessingError("外部 LLM API 回傳格式無效。") from exc
    if not text:
        raise MeetingProcessingError("外部 LLM API 回傳空白結果。")
    return text


def external_api_status(recommendation: ModelRecommendation) -> dict[str, Any]:
    try:
        key = _api_key(recommendation)
        response = requests.get(
            api_endpoint(recommendation.base_url, "/models"),
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
        if response.status_code == 404:
            return {
                "available": True,
                "model_check": "unsupported",
                "detail": "外部 API 未提供 GET /models；會在第一個工作時驗證。",
            }
        response.raise_for_status()
        payload = response.json()
    except (MeetingProcessingError, ValueError, requests.RequestException) as exc:
        return {"available": False, "detail": _safe_api_error(exc)}
    models = payload.get("data") if isinstance(payload, dict) else []
    model_ids = {str(item.get("id") or "") for item in models if isinstance(item, dict)}
    required = {recommendation.llm_model, recommendation.transcription_model}
    missing = sorted(required.difference(model_ids)) if model_ids else []
    if missing:
        return {"available": False, "detail": f"外部 API 找不到設定模型：{', '.join(missing)}"}
    return {"available": True, "model_check": "passed", "detail": "外部 API 與設定模型均可用。"}


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
        inference_provider: "external_openai_compatible_api"
        known_limits: "AI 自動整理可能誤判講者、期限與專有名詞；正式發布前需人工覆核。"
        ---

        """
    ).lstrip()
    return metadata + report.strip() + "\n"


def _api_key(recommendation: ModelRecommendation) -> str:
    key = os.getenv(recommendation.api_key_env, "").strip()
    if not key:
        raise MeetingProcessingError(f"尚未設定外部 API key 環境變數：{recommendation.api_key_env}")
    return key


def _transcript_segments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list):
        return []
    rows: list[dict[str, Any]] = []
    for segment in raw_segments:
        if not isinstance(segment, dict):
            continue
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        try:
            start = round(float(segment.get("start", 0)), 2)
            end = round(float(segment.get("end", start)), 2)
        except (TypeError, ValueError):
            continue
        rows.append({"start": start, "end": max(start, end), "text": text})
    return rows


def _format_transcript(rows: list[dict[str, Any]], fallback: str) -> str:
    if not rows:
        return fallback
    return "\n".join(f"[{_fmt_time(row['start'])} - {_fmt_time(row['end'])}] {row['text']}" for row in rows)


def _transcript_duration(rows: list[dict[str, Any]]) -> float | None:
    if not rows:
        return None
    return max(float(row["end"]) for row in rows)


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

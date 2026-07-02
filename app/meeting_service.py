from __future__ import annotations

import os
import textwrap
import time
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

import requests
from faster_whisper import WhisperModel

from app.model_selector import ModelRecommendation


class MeetingProcessingError(RuntimeError):
    pass


@lru_cache(maxsize=4)
def get_whisper_model(model_size: str, device: str, compute_type: str) -> WhisperModel:
    return WhisperModel(
        model_size,
        device=device,
        compute_type=compute_type,
        download_root=os.getenv("WHISPER_CACHE_DIR", "/models/whisper"),
    )


def transcribe_audio(
    file_path: Path,
    recommendation: ModelRecommendation,
    language: str | None,
    progress,
    preview: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    progress("loading_whisper", "載入 Whisper 模型")
    model = get_whisper_model(
        recommendation.whisper_model,
        recommendation.whisper_device,
        recommendation.whisper_compute_type,
    )

    progress("transcribing", "音訊轉文字")
    segments, info = model.transcribe(
        str(file_path),
        language=language or None,
        vad_filter=True,
        beam_size=5,
    )

    rows: list[dict[str, Any]] = []
    transcript_parts: list[str] = []
    for segment in segments:
        text = segment.text.strip()
        if not text:
            continue
        rows.append(
            {
                "start": round(segment.start, 2),
                "end": round(segment.end, 2),
                "text": text,
            }
        )
        transcript_parts.append(f"[{_fmt_time(segment.start)} - {_fmt_time(segment.end)}] {text}")
        if preview:
            preview("\n".join(transcript_parts))

    return {
        "language": info.language,
        "language_probability": round(info.language_probability, 4),
        "duration": round(info.duration, 2),
        "segments": rows,
        "transcript": "\n".join(transcript_parts),
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

    progress("checking_llm", "確認 Ollama 模型")
    ensure_ollama_model(recommendation.ollama_model)

    chunks = split_text(transcript, max_chars=9000)
    if len(chunks) == 1:
        progress("summarizing", "整理會議報告")
        report = call_ollama(
            recommendation.ollama_model,
            build_final_prompt(meeting_title, transcript, output_language, report_mode),
        )
        if preview:
            preview(report)
        return report

    partials: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        progress("summarizing", f"整理分段摘要 {index}/{len(chunks)}")
        partials.append(
            call_ollama(
                recommendation.ollama_model,
                build_chunk_prompt(meeting_title, chunk, output_language, index, len(chunks)),
            )
        )
        if preview:
            preview("\n\n".join(partials))

    progress("summarizing", "合併分段摘要")
    report = call_ollama(
        recommendation.ollama_model,
        build_final_prompt(meeting_title, "\n\n".join(partials), output_language, report_mode),
    )
    if preview:
        preview(report)
    return report


def ensure_ollama_model(model: str) -> None:
    base_url = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434").rstrip("/")
    try:
        show = requests.post(f"{base_url}/api/show", json={"model": model}, timeout=20)
        if show.ok:
            return

        with requests.post(
            f"{base_url}/api/pull",
            json={"model": model, "stream": True},
            stream=True,
            timeout=(20, 1800),
        ) as response:
            if not response.ok:
                raise MeetingProcessingError(f"Ollama 無法下載模型 {model}: {response.text[:300]}")
            for _ in response.iter_lines():
                pass
    except requests.RequestException as exc:
        raise MeetingProcessingError(f"無法連線到 Ollama：{exc}") from exc


def call_ollama(model: str, prompt: str) -> str:
    base_url = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434").rstrip("/")
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.2,
            "top_p": 0.9,
            "num_ctx": 16384,
        },
    }

    try:
        response = requests.post(f"{base_url}/api/generate", json=payload, timeout=(20, 1200))
        response.raise_for_status()
    except requests.RequestException as exc:
        raise MeetingProcessingError(f"Ollama 產生報告失敗：{exc}") from exc

    data = response.json()
    text = data.get("response", "").strip()
    if not text:
        raise MeetingProcessingError("Ollama 回傳空白結果。")
    return text


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
        whisper_model: "{recommendation.whisper_model}"
        whisper_device: "{recommendation.whisper_device}"
        whisper_compute_type: "{recommendation.whisper_compute_type}"
        ollama_model: "{recommendation.ollama_model}"
        known_limits: "AI 自動整理可能誤判講者、期限與專有名詞；正式發布前需人工覆核。"
        ---

        """
    ).lstrip()
    return metadata + report.strip() + "\n"


def _fmt_time(seconds: float) -> str:
    total = int(seconds)
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"

from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.meeting_service import (
    decorate_report,
    external_api_status,
    generate_report,
    local_whisper_status,
    transcribe_audio,
    transcribe_audio_local_whisper,
)
from app.model_selector import ExternalAPIConfigurationError, recommend_models

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(__import__("os").getenv("APP_DATA_DIR", "/data"))
UPLOAD_DIR = DATA_DIR / "uploads"
REPORT_DIR = DATA_DIR / "reports"
MAX_UPLOAD_MB = int(__import__("os").getenv("MAX_UPLOAD_MB", "800"))
PREVIEW_LIMIT = int(__import__("os").getenv("PREVIEW_LIMIT", "12000"))

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
REPORT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Meeting Report Tool")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")

jobs: dict[str, dict[str, Any]] = {}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(APP_DIR / "static" / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    try:
        recommendation = recommend_models()
    except ExternalAPIConfigurationError as exc:
        return {"status": "not_ready", "external_api": {"available": False, "detail": str(exc)}}
    api_status = external_api_status(recommendation)
    whisper_status = local_whisper_status(recommendation)
    return {
        "status": "ok" if api_status["available"] else "not_ready",
        "recommendation": recommendation.__dict__,
        "external_api": api_status,
        "local_whisper": whisper_status,
    }


@app.post("/api/jobs")
def create_job(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    meeting_title: str = Form(""),
    language: str = Form("zh"),
    output_language: str = Form("繁體中文"),
    report_mode: str = Form("standard"),
    transcription_provider: str = Form("local_fast_whisper"),
) -> dict[str, str]:
    if not file.filename:
        raise HTTPException(status_code=400, detail="缺少檔名。")

    safe_name = _safe_filename(file.filename)
    job_id = uuid.uuid4().hex
    upload_path = UPLOAD_DIR / f"{job_id}_{safe_name}"

    size = 0
    with upload_path.open("wb") as buffer:
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_MB * 1024 * 1024:
                upload_path.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail=f"檔案超過 {MAX_UPLOAD_MB} MB。")
            buffer.write(chunk)

    try:
        recommendation = recommend_models()
    except ExternalAPIConfigurationError as exc:
        upload_path.unlink(missing_ok=True)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if transcription_provider not in {"google_gemini", "local_fast_whisper"}:
        upload_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="轉錄模式無效。")

    readiness = external_api_status(recommendation, include_transcription=transcription_provider == "google_gemini")
    if not readiness["available"]:
        upload_path.unlink(missing_ok=True)
        raise HTTPException(status_code=503, detail=str(readiness.get("detail") or "外部 API 未就緒。"))
    if transcription_provider == "local_fast_whisper":
        whisper_readiness = local_whisper_status(recommendation)
        if not whisper_readiness["available"]:
            upload_path.unlink(missing_ok=True)
            raise HTTPException(status_code=503, detail=str(whisper_readiness.get("detail") or "本機 fast-whisper 未就緒。"))
    report_path = REPORT_DIR / f"{job_id}_meeting_report.md"
    transcript_path = REPORT_DIR / f"{job_id}_transcript.md"

    jobs[job_id] = {
        "id": job_id,
        "status": "queued",
        "stage": "queued",
        "message": "等待處理",
        "progress": 0,
        "source_name": file.filename,
        "upload_path": str(upload_path),
        "report_path": str(report_path),
        "transcript_path": str(transcript_path),
        "recommendation": recommendation.__dict__,
        "transcription_provider": transcription_provider,
        "transcript_preview": "",
        "report_preview": "",
        "preview_updated_at": None,
        "error": None,
    }

    background_tasks.add_task(
        _process_job,
        job_id,
        upload_path,
        report_path,
        transcript_path,
        meeting_title,
        language,
        output_language,
        report_mode,
        transcription_provider,
        file.filename,
        recommendation,
    )
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="找不到工作。")
    return _public_job(job)


@app.get("/api/jobs/{job_id}/report")
def download_report(job_id: str) -> FileResponse:
    job = jobs.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(status_code=404, detail="報告尚未完成或不存在。")
    return FileResponse(job["report_path"], filename="meeting_report.md", media_type="text/markdown")


@app.get("/api/jobs/{job_id}/transcript")
def download_transcript(job_id: str) -> FileResponse:
    job = jobs.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(status_code=404, detail="逐字稿尚未完成或不存在。")
    return FileResponse(job["transcript_path"], filename="meeting_transcript.md", media_type="text/markdown")


def _process_job(
    job_id: str,
    upload_path: Path,
    report_path: Path,
    transcript_path: Path,
    meeting_title: str,
    language: str,
    output_language: str,
    report_mode: str,
    transcription_provider: str,
    source_name: str,
    recommendation,
) -> None:
    def progress(stage: str, message: str) -> None:
        progress_map = {
            "uploading_audio": 15,
            "loading_whisper": 15,
            "transcribing": 40,
            "checking_llm": 65,
            "summarizing": 82,
            "saving": 95,
        }
        jobs[job_id].update(
            {
                "status": "running",
                "stage": stage,
                "message": message,
                "progress": progress_map.get(stage, jobs[job_id].get("progress", 0)),
            }
        )

    def update_preview(kind: str, text: str) -> None:
        jobs[job_id].update(
            {
                f"{kind}_preview": _preview_text(text),
                "preview_updated_at": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
            }
        )

    try:
        if transcription_provider == "local_fast_whisper":
            progress("loading_whisper", "準備本機 fast-whisper")
            transcript_meta = transcribe_audio_local_whisper(
                upload_path,
                recommendation,
                language,
                progress,
                preview=lambda text: update_preview("transcript", text),
            )
        else:
            progress("uploading_audio", "準備上傳至 Google Gemini")
            transcript_meta = transcribe_audio(
                upload_path,
                recommendation,
                language,
                progress,
                preview=lambda text: update_preview("transcript", text),
            )
        update_preview("transcript", transcript_meta["transcript"])
        transcript_path.write_text(transcript_meta["transcript"], encoding="utf-8")

        report = generate_report(
            transcript_meta["transcript"],
            meeting_title,
            recommendation,
            output_language,
            report_mode,
            progress,
            preview=lambda text: update_preview("report", text),
        )
        update_preview("report", report)
        progress("saving", "寫入報告")
        report_path.write_text(
            decorate_report(report, transcript_meta, recommendation, source_name),
            encoding="utf-8",
        )
        jobs[job_id].update(
            {
                "status": "done",
                "stage": "done",
                "message": "完成",
                "progress": 100,
            }
        )
    except Exception as exc:  # noqa: BLE001
        jobs[job_id].update(
            {
                "status": "failed",
                "stage": "failed",
                "message": "處理失敗",
                "error": str(exc),
            }
        )


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    public_keys = {
        "id",
        "status",
        "stage",
        "message",
        "progress",
        "source_name",
        "recommendation",
        "transcription_provider",
        "transcript_preview",
        "report_preview",
        "preview_updated_at",
        "error",
    }
    data = {key: value for key, value in job.items() if key in public_keys}
    if job.get("status") == "done":
        data["report_url"] = f"/api/jobs/{job['id']}/report"
        data["transcript_url"] = f"/api/jobs/{job['id']}/transcript"
    return data


def _safe_filename(name: str) -> str:
    normalized = Path(name).name.replace(" ", "_")
    return "".join(char for char in normalized if char.isalnum() or char in "._-") or "upload"


def _preview_text(text: str) -> str:
    cleaned = text.strip()
    if len(cleaned) <= PREVIEW_LIMIT:
        return cleaned
    return f"...\n{cleaned[-PREVIEW_LIMIT:]}"

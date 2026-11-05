from __future__ import annotations

import os
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.meeting_service import call_external_llm, external_api_status, transcribe_audio, transcribe_audio_local_whisper
from app.main import create_job
from app.model_selector import ModelRecommendation, recommend_models


class FakeResponse:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            error = requests.HTTPError(f"HTTP {self.status_code}")
            error.response = self
            raise error


class FakeUploadedFile:
    uri = "files/test-audio"
    mime_type = "audio/wav"


class FakeFiles:
    def __init__(self) -> None:
        self.uploaded: list[str] = []

    def upload(self, file: str) -> FakeUploadedFile:
        self.uploaded.append(file)
        return FakeUploadedFile()


class FakeModels:
    def __init__(self, text: str) -> None:
        self.calls: list[dict[str, object]] = []
        self.text = text

    def generate_content(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return type("GenerateContentResponse", (), {"text": self.text})()


class FakeClient:
    last: "FakeClient | None" = None

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.files = FakeFiles()
        self.models = FakeModels(
            '{"language":"zh","duration_seconds":3.0,"segments":['
            '{"start_seconds":0,"end_seconds":1.5,"speaker":"A","text":"第一段"},'
            '{"start_seconds":1.5,"end_seconds":3.0,"speaker":"B","text":"第二段"}]}'
        )
        FakeClient.last = self


class FakePlainTextClient(FakeClient):
    last: "FakePlainTextClient | None" = None

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.files = FakeFiles()
        self.models = FakeModels("這是一段沒有 JSON 包裝的逐字稿。")
        FakePlainTextClient.last = self


class FakeWrappedJsonClient(FakeClient):
    last: "FakeWrappedJsonClient | None" = None

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.files = FakeFiles()
        self.models = FakeModels(
            '以下是逐字稿 JSON：\n{"language":"zh","segments":[{"start_seconds":0,'
            '"end_seconds":2,"text":"包在文字中的 JSON"}]}\n完成。'
        )
        FakeWrappedJsonClient.last = self


class FakeReportClient(FakeClient):
    last: "FakeReportClient | None" = None

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.files = FakeFiles()
        self.models = FakeModels("# 報告")
        FakeReportClient.last = self


class FakeSegment:
    def __init__(self, start: float, end: float, text: str) -> None:
        self.start = start
        self.end = end
        self.text = text


class FakeWhisperModel:
    def transcribe(self, *args: object, **kwargs: object) -> tuple[list[FakeSegment], object]:
        del args, kwargs
        return [FakeSegment(0, 1.5, "第一段"), FakeSegment(1.5, 3, "第二段")], type(
            "Info", (), {"language": "zh", "language_probability": 0.98, "duration": 3.0}
        )()


def recommendation() -> ModelRecommendation:
    return ModelRecommendation(
        google_api_base_url="https://generativelanguage.googleapis.com/v1beta",
        google_api_key_env="GEMINI_API_KEY",
        transcription_model="gemini-transcribe-test",
        llm_model="gemini-report-test",
        whisper_model="base",
        whisper_device="cpu",
        whisper_compute_type="int8",
        reason="test",
    )


class GoogleAPITest(unittest.TestCase):
    def test_recommendation_uses_google_models_without_local_inference(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GOOGLE_API_BASE_URL": "https://generativelanguage.googleapis.com/v1beta",
                "GOOGLE_API_KEY_ENV": "GEMINI_API_KEY",
                "GOOGLE_TRANSCRIPTION_MODEL": "gemini-transcribe",
                "GOOGLE_LLM_MODEL": "gemini-report",
            },
            clear=True,
        ):
            value = recommend_models()

        self.assertEqual(value.google_api_base_url, "https://generativelanguage.googleapis.com/v1beta")
        self.assertEqual(value.google_api_key_env, "GEMINI_API_KEY")
        self.assertEqual(value.transcription_model, "gemini-transcribe")
        self.assertEqual(value.llm_model, "gemini-report")
        self.assertEqual(value.whisper_model, "base")
        self.assertEqual(value.whisper_device, "cpu")
        self.assertEqual(value.whisper_compute_type, "int8")
        self.assertIn("切換", value.reason)

    def test_create_job_defaults_to_local_fast_whisper(self) -> None:
        parameter = inspect.signature(create_job).parameters["transcription_provider"]
        self.assertEqual(parameter.default.default, "local_fast_whisper")

    def test_transcription_uploads_audio_to_google_and_preserves_segments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            audio = Path(temporary_directory) / "meeting.wav"
            audio.write_bytes(b"audio")
            events: list[tuple[str, str]] = []
            with (
                patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}, clear=True),
                patch("app.meeting_service.genai.Client", FakeClient),
            ):
                result = transcribe_audio(audio, recommendation(), "zh", lambda stage, text: events.append((stage, text)))

        self.assertEqual(result["duration"], 3.0)
        self.assertIn("[00:00:00 - 00:00:01] A: 第一段", result["transcript"])
        self.assertEqual(events[0][0], "uploading_audio")
        self.assertEqual(events[-1][0], "transcribing")
        self.assertEqual(FakeClient.last.api_key, "test-key")
        self.assertEqual(FakeClient.last.models.calls[0]["model"], "gemini-transcribe-test")
        self.assertEqual(FakeClient.last.models.calls[0]["contents"][0].uri, "files/test-audio")

    def test_transcription_falls_back_when_google_returns_plain_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            audio = Path(temporary_directory) / "meeting.wav"
            audio.write_bytes(b"audio")
            with (
                patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}, clear=True),
                patch("app.meeting_service.genai.Client", FakePlainTextClient),
            ):
                result = transcribe_audio(audio, recommendation(), "zh", lambda *_: None)

        self.assertEqual(result["language"], "zh")
        self.assertIsNone(result["duration"])
        self.assertEqual(result["segments"], [])
        self.assertEqual(result["transcript"], "這是一段沒有 JSON 包裝的逐字稿。")

    def test_transcription_extracts_json_embedded_in_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            audio = Path(temporary_directory) / "meeting.wav"
            audio.write_bytes(b"audio")
            with (
                patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}, clear=True),
                patch("app.meeting_service.genai.Client", FakeWrappedJsonClient),
            ):
                result = transcribe_audio(audio, recommendation(), "zh", lambda *_: None)

        self.assertEqual(result["duration"], 2.0)
        self.assertEqual(result["transcript"], "[00:00:00 - 00:00:02] 包在文字中的 JSON")

    def test_report_generation_uses_google_generate_content(self) -> None:
        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}, clear=True),
            patch("app.meeting_service.genai.Client", FakeReportClient),
        ):
            report = call_external_llm(recommendation(), "prompt")

        self.assertEqual(report, "# 報告")
        self.assertEqual(FakeReportClient.last.models.calls[0]["model"], "gemini-report-test")
        self.assertEqual(FakeReportClient.last.models.calls[0]["contents"], "prompt")

    def test_local_fast_whisper_transcription_preserves_segments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            audio = Path(temporary_directory) / "meeting.wav"
            audio.write_bytes(b"audio")
            events: list[tuple[str, str]] = []
            with patch("app.meeting_service.get_whisper_model", return_value=FakeWhisperModel()) as get_model:
                result = transcribe_audio_local_whisper(
                    audio,
                    recommendation(),
                    "zh",
                    lambda stage, text: events.append((stage, text)),
                )

        self.assertEqual(result["provider"], "local_fast_whisper")
        self.assertEqual(result["duration"], 3.0)
        self.assertIn("[00:00:00 - 00:00:01] 第一段", result["transcript"])
        self.assertEqual(events[0][0], "loading_whisper")
        self.assertEqual(events[-1][0], "transcribing")
        self.assertEqual(get_model.call_args.args, ("base", "cpu", "int8"))

    def test_health_status_requires_key_and_checks_google_models(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            missing = external_api_status(recommendation())
        self.assertFalse(missing["available"])
        self.assertIn("GEMINI_API_KEY", str(missing["detail"]))

        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}, clear=True),
            patch(
                "app.meeting_service.requests.get",
                return_value=FakeResponse(
                    {
                        "models": [
                            {"name": "models/gemini-transcribe-test"},
                            {"name": "models/gemini-report-test"},
                        ]
                    }
                ),
            ),
        ):
            ready = external_api_status(recommendation())
        self.assertTrue(ready["available"])


if __name__ == "__main__":
    unittest.main()

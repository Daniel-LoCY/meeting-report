from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.meeting_service import call_external_llm, external_api_status, transcribe_audio
from app.model_selector import GpuInfo, HardwareProfile, ModelRecommendation, recommend_models


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
        whisper_model="medium",
        whisper_device="cuda",
        whisper_compute_type="float16",
        chat_base_url="https://api.example.test/v1",
        chat_api_key_env="MEETING_CHAT_API_KEY",
        llm_model="chat-test",
        reason="test",
    )


class ExternalAPITest(unittest.TestCase):
    def test_recommendation_keeps_gpu_whisper_and_uses_external_chat(self) -> None:
        profile = HardwareProfile(cpu_count=8, memory_gb=32, gpu=GpuInfo("Test GPU", 12))
        with patch.dict(
            os.environ,
            {
                "MEETING_CHAT_API_BASE_URL": "https://provider.example/v1",
                "MEETING_CHAT_API_KEY_ENV": "MEETING_CHAT_API_KEY",
                "MEETING_CHAT_MODEL": "chat-model",
            },
            clear=True,
        ):
            value = recommend_models(profile)

        self.assertEqual(value.whisper_model, "medium")
        self.assertEqual(value.whisper_device, "cuda")
        self.assertEqual(value.chat_base_url, "https://provider.example/v1")
        self.assertEqual(value.llm_model, "chat-model")

    def test_transcription_uses_local_whisper_and_preserves_segments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            audio = Path(temporary_directory) / "meeting.wav"
            audio.write_bytes(b"audio")
            events: list[tuple[str, str]] = []
            with patch("app.meeting_service.get_whisper_model", return_value=FakeWhisperModel()) as get_model:
                result = transcribe_audio(audio, recommendation(), "zh", lambda stage, text: events.append((stage, text)))

        self.assertEqual(result["duration"], 3.0)
        self.assertIn("[00:00:00 - 00:00:01] 第一段", result["transcript"])
        self.assertEqual(events[0][0], "loading_whisper")
        self.assertEqual(events[-1][0], "transcribing")
        self.assertEqual(get_model.call_args.args, ("medium", "cuda", "float16"))

    def test_report_generation_uses_external_chat_completions_with_bearer_auth(self) -> None:
        payload = {"choices": [{"message": {"content": "# 報告"}}]}
        with (
            patch.dict(os.environ, {"MEETING_CHAT_API_KEY": "test-key"}, clear=True),
            patch("app.meeting_service.requests.post", return_value=FakeResponse(payload)) as post,
        ):
            report = call_external_llm(recommendation(), "prompt")

        self.assertEqual(report, "# 報告")
        self.assertEqual(post.call_args.args[0], "https://api.example.test/v1/chat/completions")
        self.assertEqual(post.call_args.kwargs["json"]["model"], "chat-test")
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer test-key")

    def test_health_status_requires_chat_key_and_checks_chat_model(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            missing = external_api_status(recommendation())
        self.assertFalse(missing["available"])
        self.assertIn("MEETING_CHAT_API_KEY", str(missing["detail"]))

        with (
            patch.dict(os.environ, {"MEETING_CHAT_API_KEY": "test-key"}, clear=True),
            patch("app.meeting_service.requests.get", return_value=FakeResponse({"data": [{"id": "chat-test"}]})),
        ):
            ready = external_api_status(recommendation())
        self.assertTrue(ready["available"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.meeting_service import call_external_llm, external_api_status, transcribe_audio
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


def recommendation() -> ModelRecommendation:
    return ModelRecommendation(
        base_url="https://api.example.test/v1",
        api_key_env="EXTERNAL_API_KEY",
        transcription_model="transcribe-test",
        llm_model="chat-test",
        reason="test",
    )


class ExternalAPITest(unittest.TestCase):
    def test_recommendation_uses_external_models_without_hardware_selection(self) -> None:
        with patch.dict(
            os.environ,
            {
                "EXTERNAL_API_BASE_URL": "https://provider.example/v1",
                "EXTERNAL_API_KEY_ENV": "EXTERNAL_API_KEY",
                "EXTERNAL_LLM_MODEL": "chat-model",
                "EXTERNAL_TRANSCRIPTION_MODEL": "transcribe-model",
            },
            clear=True,
        ):
            value = recommend_models()

        self.assertEqual(value.base_url, "https://provider.example/v1")
        self.assertEqual(value.llm_model, "chat-model")
        self.assertEqual(value.transcription_model, "transcribe-model")
        self.assertNotIn("GPU", value.reason)

    def test_transcription_posts_multipart_to_external_api_and_formats_segments(self) -> None:
        payload = {
            "language": "zh",
            "text": "第一段 第二段",
            "segments": [
                {"start": 0, "end": 1.5, "text": "第一段"},
                {"start": 1.5, "end": 3, "text": "第二段"},
            ],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            audio = Path(temporary_directory) / "meeting.wav"
            audio.write_bytes(b"audio")
            events: list[tuple[str, str]] = []
            with (
                patch.dict(os.environ, {"EXTERNAL_API_KEY": "test-key"}, clear=True),
                patch("app.meeting_service.requests.post", return_value=FakeResponse(payload)) as post,
            ):
                result = transcribe_audio(audio, recommendation(), "zh", lambda stage, text: events.append((stage, text)))

        self.assertEqual(result["duration"], 3.0)
        self.assertIn("[00:00:00 - 00:00:01] 第一段", result["transcript"])
        self.assertEqual(events[0][0], "uploading_audio")
        self.assertEqual(events[-1][0], "transcribing")
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(post.call_args.args[0], "https://api.example.test/v1/audio/transcriptions")

    def test_report_generation_uses_chat_completions_with_bearer_auth(self) -> None:
        payload = {"choices": [{"message": {"content": "# 報告"}}]}
        with (
            patch.dict(os.environ, {"EXTERNAL_API_KEY": "test-key"}, clear=True),
            patch("app.meeting_service.requests.post", return_value=FakeResponse(payload)) as post,
        ):
            report = call_external_llm(recommendation(), "prompt")

        self.assertEqual(report, "# 報告")
        self.assertEqual(post.call_args.args[0], "https://api.example.test/v1/chat/completions")
        self.assertEqual(post.call_args.kwargs["json"]["model"], "chat-test")
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer test-key")

    def test_health_status_requires_external_key_and_checks_both_models(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            missing = external_api_status(recommendation())
        self.assertFalse(missing["available"])
        self.assertIn("EXTERNAL_API_KEY", str(missing["detail"]))

        with (
            patch.dict(os.environ, {"EXTERNAL_API_KEY": "test-key"}, clear=True),
            patch(
                "app.meeting_service.requests.get",
                return_value=FakeResponse({"data": [{"id": "chat-test"}, {"id": "transcribe-test"}]}),
            ),
        ):
            ready = external_api_status(recommendation())
        self.assertTrue(ready["available"])


if __name__ == "__main__":
    unittest.main()

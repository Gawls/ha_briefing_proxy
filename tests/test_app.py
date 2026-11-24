"""Endpoint regression tests; all data is temporary and all API calls are mocked.

Run from the add-on directory with:
    python -m unittest discover -s tests -v
"""

import asyncio
from contextlib import ExitStack
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
from openai import APIError, APITimeoutError, RateLimitError


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
PARAGRAPH = "Good morning, family. St. Albert is chilly — dress warmly."
ENDING = "The snowman called in sick with a meltdown."
WEATHER = "St. Albert: 4 cm of snow, gusting to 50 km/h."
CALENDAR_NOTE = "Quick note: Lucas's calendar is unavailable, so events may be missing."
OILERS_RECAP = "Last night, the Oilers beat the Flames 4-2 at home."
OLD_TIMESTAMP = "2025-01-01T07:00:00+00:00"
OLD_TEXT = "This is yesterday's briefing and must not be replayed after generation."


def completion(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


class BriefingEndpointTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.data_dir = Path(self.stack.enter_context(
            tempfile.TemporaryDirectory(prefix="briefing-proxy-tests-")
        ))
        self.stack.enter_context(patch.dict(os.environ, {
            "DATA_DIR": str(self.data_dir),
            "OPENAI_API_KEY": "",
            "MODEL_NAME": "existing-configured-model",
            "HA_WEBHOOK_URL": "http://homeassistant:8123/api/webhook/legacy-existing",
        }))
        # Exercise the real default when an older installation lacks this option.
        os.environ.pop("SEND_WEBHOOK", None)

        # A regression must fail locally instead of making a real HTTP/DNS call.
        self.network_request = self.stack.enter_context(patch(
            "httpx.AsyncClient.request",
            new_callable=AsyncMock,
            side_effect=AssertionError("Live HTTP requests are forbidden in tests"),
        ))
        self.stack.enter_context(patch(
            "socket.getaddrinfo",
            side_effect=AssertionError("Live DNS requests are forbidden in tests"),
        ))
        self.stack.enter_context(patch(
            "socket.socket.connect",
            side_effect=AssertionError("Live socket connections are forbidden in tests"),
        ))

        module_name = f"briefing_proxy_test_{id(self)}"
        spec = importlib.util.spec_from_file_location(module_name, APP_PATH)
        self.module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = self.module
        self.stack.callback(sys.modules.pop, module_name, None)
        spec.loader.exec_module(self.module)

        self.model_call = AsyncMock(return_value=completion(
            f"{PARAGRAPH}\nFUN_ENDING: {ENDING}"
        ))
        mocked_client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=self.model_call)
        ))
        self.stack.enter_context(patch.object(self.module, "client", mocked_client))
        self.oilers_call = self.stack.enter_context(patch.object(
            self.module, "fetch_oilers_recap", new_callable=AsyncMock,
            return_value=OILERS_RECAP,
        ))
        self.webhook_call = self.stack.enter_context(patch.object(
            self.module, "post_to_ha", new_callable=AsyncMock,
        ))
        self.http = self.stack.enter_context(TestClient(self.module.app))

    def tearDown(self):
        self.network_request.assert_not_awaited()

    def post_briefing(self):
        return self.http.post("/briefing", json={
            "weather": WEATHER,
            "events": {
                "gawley": [{"summary": "School concert"}],
                "meta": {
                    "calendar_note": CALENDAR_NOTE,
                    "calendar_failed_list": ["Lucas"],
                    "calendar_ok_all": False,
                },
            },
        })

    def seed_previous_briefing(self):
        path = self.data_dir / "latest_briefing.json"
        path.write_text(json.dumps({
            "text": OLD_TEXT,
            "audio_url": "https://old.example/local/briefing.mp3",
            "updated_at": OLD_TIMESTAMP,
        }), encoding="utf-8")
        return path

    def assert_saved_response(self, body):
        self.assertEqual(body["status"], "ok")
        self.assertIsNone(body["audio_url"])
        self.assertEqual(body["character_count"], len(body["text"]))
        self.assertTrue(body["text"].strip())
        self.assertIsNotNone(datetime.fromisoformat(body["updated_at"]).tzinfo)
        payload = {key: value for key, value in body.items() if key != "status"}
        saved = json.loads((self.data_dir / "latest_briefing.json").read_text(
            encoding="utf-8"
        ))
        self.assertEqual(payload, saved)
        latest = self.http.get("/briefing/latest")
        self.assertEqual(latest.status_code, 200)
        self.assertEqual(payload, latest.json())

    def assert_fresh_fallback(self, expected_warning):
        self.seed_previous_briefing()
        started = datetime.now(timezone.utc)
        response = self.post_briefing()
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assert_saved_response(body)
        self.assertEqual(body["generation_mode"], "fallback")
        self.assertEqual(body["warning"], expected_warning)
        self.assertIn(WEATHER, body["text"])
        self.assertIn(CALENDAR_NOTE, body["text"])
        self.assertNotIn(OLD_TEXT, body["text"])
        self.assertNotEqual(body["updated_at"], OLD_TIMESTAMP)
        self.assertGreaterEqual(datetime.fromisoformat(body["updated_at"]), started)

    def test_success_returns_final_text_and_exact_saved_record(self):
        self.seed_previous_briefing()
        response = self.post_briefing()
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["text"], f"{PARAGRAPH} {ENDING}")
        self.assertNotIn("FUN_ENDING:", body["text"])
        self.assertEqual(body["generation_mode"], "ai")
        self.assertIsNone(body["warning"])
        self.assertNotEqual(body["updated_at"], OLD_TIMESTAMP)
        self.assert_saved_response(body)
        self.model_call.assert_awaited_once()
        self.oilers_call.assert_awaited_once()

    def test_existing_webhook_url_does_not_enable_delivery_by_default(self):
        self.assertTrue(os.environ["HA_WEBHOOK_URL"])
        self.assertNotIn("SEND_WEBHOOK", os.environ)
        self.assertFalse(self.http.get("/health").json()["send_webhook"])
        response = self.post_briefing()
        self.assertEqual(response.status_code, 200, response.text)
        self.webhook_call.assert_not_called()

    def test_legacy_cached_audio_is_hidden_and_audio_endpoint_is_retired(self):
        self.seed_previous_briefing()
        response = self.http.get("/briefing/latest")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["text"], OLD_TEXT)
        self.assertEqual(body["updated_at"], OLD_TIMESTAMP)
        self.assertEqual(body["character_count"], len(OLD_TEXT))
        self.assertIsNone(body["audio_url"])
        self.assertEqual(self.http.get("/briefing_audio").status_code, 410)
        self.model_call.assert_not_awaited()

    def test_missing_key_returns_503_without_replacing_previous_briefing(self):
        path = self.seed_previous_briefing()
        previous = path.read_bytes()
        with patch.object(self.module, "client", None):
            response = self.post_briefing()
        self.assertEqual(response.status_code, 503)
        self.assertIn("key", response.json()["detail"].lower())
        self.assertEqual(path.read_bytes(), previous)
        self.model_call.assert_not_awaited()
        self.oilers_call.assert_not_awaited()
        self.webhook_call.assert_not_called()

    def test_rate_limit_returns_fresh_weather_fallback(self):
        request = httpx.Request("POST", "https://example.invalid/chat/completions")
        self.model_call.side_effect = RateLimitError(
            "Rate limited", response=httpx.Response(429, request=request), body=None
        )
        self.assert_fresh_fallback("rate_limit")

    def test_api_error_returns_fresh_weather_fallback(self):
        self.model_call.side_effect = APIError(
            "Service unavailable",
            request=httpx.Request("POST", "https://example.invalid/chat/completions"),
            body=None,
        )
        self.assert_fresh_fallback("api_error")

    def test_empty_or_ending_only_output_returns_fresh_weather_fallback(self):
        for content in ("", " \n ", None, "FUN_ENDING: A joke without a briefing."):
            with self.subTest(content=content):
                self.model_call.return_value = completion(content)
                self.assert_fresh_fallback("generation_error")

    def test_generation_timeout_returns_fresh_weather_fallback(self):
        self.model_call.side_effect = asyncio.TimeoutError()
        self.assert_fresh_fallback("timeout")

    def test_sdk_timeout_returns_fresh_weather_fallback(self):
        self.model_call.side_effect = APITimeoutError(
            request=httpx.Request("POST", "https://example.invalid/chat/completions")
        )
        # The SDK timeout is an APIError; the outer deadline has its own warning.
        self.assert_fresh_fallback("api_error")

    def test_unexpected_generation_error_returns_fresh_weather_fallback(self):
        self.model_call.side_effect = RuntimeError("Unexpected mocked model failure")
        self.assert_fresh_fallback("generation_error")

    def test_atomic_save_failure_preserves_previous_complete_file(self):
        path = self.seed_previous_briefing()
        previous = path.read_bytes()
        # Avoid a history write so the injected failure targets latest publication.
        self.model_call.return_value = completion(PARAGRAPH)
        with patch.object(self.module.os, "replace", side_effect=OSError("Disk full")):
            response = self.post_briefing()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual(self.http.get("/briefing/latest").json()["text"], OLD_TEXT)
        self.webhook_call.assert_not_called()

    def test_existing_history_and_briefing_inputs_are_retained(self):
        history = ["A previously used mom joke.", "A previously used dad joke."]
        path = self.data_dir / "briefing_endings.json"
        path.write_text(json.dumps(history), encoding="utf-8")
        response = self.post_briefing()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), history + [ENDING])
        arguments = self.model_call.await_args.kwargs
        self.assertEqual(arguments["model"], "existing-configured-model")
        messages = "\n".join(message["content"] for message in arguments["messages"])
        for value in history + [WEATHER, OILERS_RECAP, CALENDAR_NOTE, "School concert"]:
            self.assertIn(value, messages)


if __name__ == "__main__":
    unittest.main()

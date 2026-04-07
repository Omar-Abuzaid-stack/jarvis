import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

import server


class _FakeChoice:
    def __init__(self, content: str):
        self.message = type("Message", (), {"content": content})()


class _FakeResponse:
    def __init__(self, content: str):
        self.choices = [_FakeChoice(content)]


class JarvisSystemAssistantApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)

    def test_apply_speech_corrections_handles_common_jarvis_misfires(self):
        corrected = server.apply_speech_corrections("hey services open cloud code for me")
        self.assertEqual(corrected, "hey JARVIS open Claude Code for me")

    def test_apply_speech_corrections_collapses_duplicate_names(self):
        corrected = server.apply_speech_corrections("jarvis jarvis open clawd code")
        self.assertEqual(corrected, "jarvis open Claude Code")

    def test_phone_page_serves_mobile_client(self):
        response = self.client.get("/phone")
        self.assertEqual(response.status_code, 200)
        self.assertIn("JARVIS Mobile", response.text)
        self.assertIn("/api/assistant/turn", response.text)

    def test_phone_link_returns_shape(self):
        response = self.client.get("/api/phone-link")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("port", payload)
        self.assertEqual(payload["port"], 8340)

    def test_wake_endpoint_uses_source_and_page_route(self):
        with patch.object(server, "_focus_or_open_jarvis_page", AsyncMock(return_value={"action": "focused_existing", "detail": "Focused existing tab"})), \
             patch.object(server, "synthesize_speech", AsyncMock(return_value=b"audio-bytes")), \
             patch.object(server, "_push_wake_audio", AsyncMock()):
            response = self.client.post("/api/wake", json={"source": "phone"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["page_action"], "focused_existing")
        self.assertEqual(payload["greeting"], "At your services Mr Omar")
        self.assertTrue(payload["audio"])

    def test_assistant_turn_endpoint_returns_audio_payload(self):
        with patch.object(server, "_ensure_project_cache", AsyncMock()), \
             patch.object(server, "detect_action_fast", return_value={"action": "open_terminal"}), \
             patch.object(server, "handle_open_terminal", AsyncMock(return_value="Terminal is open, sir.")), \
             patch.object(server, "synthesize_speech", AsyncMock(return_value=b"audio-bytes")):
            response = self.client.post(
                "/api/assistant/turn",
                json={"text": "open terminal", "session_id": "abc", "source": "mac"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["source"], "mac")
        self.assertEqual(payload["session_id"], "abc")
        self.assertEqual(payload["text"], "Terminal is open, sir.")
        self.assertTrue(payload["audio"])

    def test_assistant_turn_endpoint_dedupes_rapid_duplicate_transcript(self):
        with patch.object(server, "_ensure_project_cache", AsyncMock()), \
             patch.object(server, "detect_action_fast", return_value={"action": "open_terminal"}), \
             patch.object(server, "handle_open_terminal", AsyncMock(return_value="Terminal is open, sir.")), \
             patch.object(server, "synthesize_speech", AsyncMock(return_value=b"audio-bytes")):
            first = self.client.post(
                "/api/assistant/turn",
                json={"text": "open terminal", "session_id": "dup", "source": "mac"},
            )
            second = self.client.post(
                "/api/assistant/turn",
                json={"text": "open terminal", "session_id": "dup", "source": "mac"},
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["status"], "ok")
        self.assertEqual(second.json()["status"], "deduped")
        self.assertEqual(second.json()["text"], "Terminal is open, sir.")
        self.assertIsNone(second.json()["audio"])

    def test_simple_wake_phrase_returns_direct_response_without_llm(self):
        with patch.object(server, "_ensure_project_cache", AsyncMock()) as ensure_cache, \
             patch.object(server, "generate_response", AsyncMock()) as generate_response, \
             patch.object(server, "synthesize_speech", AsyncMock(return_value=b"audio-bytes")):
            response = self.client.post(
                "/api/assistant/turn",
                json={"text": "hey Jarvis", "session_id": "wake", "source": "browser"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["text"], "At your services, sir.")
        self.assertTrue(payload["audio"])
        ensure_cache.assert_not_called()
        generate_response.assert_not_called()

    def test_repeat_wake_phrase_uses_short_acknowledgement(self):
        with patch.object(server, "_ensure_project_cache", AsyncMock()) as ensure_cache, \
             patch.object(server, "generate_response", AsyncMock()) as generate_response, \
             patch.object(server, "synthesize_speech", AsyncMock(return_value=b"audio-bytes")):
            first = self.client.post(
                "/api/assistant/turn",
                json={"text": "hey Jarvis", "session_id": "wake-twice", "source": "browser"},
            )
            second = self.client.post(
                "/api/assistant/turn",
                json={"text": "Jarvis", "session_id": "wake-twice", "source": "browser"},
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["text"], "At your services, sir.")
        self.assertEqual(second.json()["text"], "Yes, sir.")
        ensure_cache.assert_not_called()
        generate_response.assert_not_called()

    def test_unclear_short_transcript_requests_clarification(self):
        with patch.object(server, "_ensure_project_cache", AsyncMock()) as ensure_cache, \
             patch.object(server, "generate_response", AsyncMock()) as generate_response, \
             patch.object(server, "synthesize_speech", AsyncMock(return_value=b"audio-bytes")):
            response = self.client.post(
                "/api/assistant/turn",
                json={"text": "morning", "session_id": "unclear", "source": "browser"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertIn("I didn't quite catch that, sir.", payload["text"])
        self.assertIn("Could you continue?", payload["text"])
        self.assertTrue(payload["audio"])
        ensure_cache.assert_not_called()
        generate_response.assert_not_called()

    def test_repair_phrase_requests_clarification_before_action(self):
        with patch.object(server, "_ensure_project_cache", AsyncMock()) as ensure_cache, \
             patch.object(server, "detect_action_fast", return_value={"action": "create_note"}), \
             patch.object(server, "generate_response", AsyncMock()) as generate_response, \
             patch.object(server, "synthesize_speech", AsyncMock(return_value=b"audio-bytes")):
            response = self.client.post(
                "/api/assistant/turn",
                json={
                    "text": "I can't really hear you, I am saying can you type something on the note",
                    "session_id": "repair",
                    "source": "browser",
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertIn("I didn't quite catch that, sir.", payload["text"])
        self.assertTrue(payload["audio"])
        ensure_cache.assert_not_called()
        generate_response.assert_not_called()

    def test_repeated_llm_reply_is_replaced_with_clarification(self):
        with patch.object(server, "_ensure_project_cache", AsyncMock()), \
             patch.object(server, "mistral_client", object()), \
             patch.object(server, "detect_action_fast", return_value=None), \
             patch.object(server, "generate_response", AsyncMock(side_effect=[
                 "The note is open, sir.",
                 "The note is open, sir.",
             ])), \
             patch.object(server, "synthesize_speech", AsyncMock(return_value=b"audio-bytes")):
            first = self.client.post(
                "/api/assistant/turn",
                json={"text": "open the note", "session_id": "repeat-reply", "source": "browser"},
            )
            second = self.client.post(
                "/api/assistant/turn",
                json={"text": "note?", "session_id": "repeat-reply", "source": "browser"},
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["text"], "The note is open, sir.")
        self.assertEqual(second.json()["text"], "I may have misheard that, sir. Could you say it once more?")

    def test_generate_response_uses_recovery_path_after_primary_failure(self):
        async def _run():
            with patch.object(server, "_mistral_chat", AsyncMock(side_effect=[
                TimeoutError("slow"),
                _FakeResponse("Recovered cleanly, sir."),
            ])):
                return await server.generate_response(
                    text="status update",
                    client=object(),
                    task_mgr=server.task_manager,
                    projects=[],
                    conversation_history=[],
                    last_response="Previous reply",
                    session_summary="",
                )

        result = asyncio.run(_run())
        self.assertEqual(result, "Recovered cleanly, sir.")

    def test_generate_response_returns_calm_fallback_after_recovery_failure(self):
        async def _run():
            with patch.object(server, "_mistral_chat", AsyncMock(side_effect=[
                TimeoutError("slow"),
                RuntimeError("still failing"),
            ])):
                return await server.generate_response(
                    text="status update",
                    client=object(),
                    task_mgr=server.task_manager,
                    projects=[],
                    conversation_history=[],
                    last_response="Previous reply",
                    session_summary="",
                )

        result = asyncio.run(_run())
        self.assertEqual(result, "Slight signal drop, sir. Say that once more.")


if __name__ == "__main__":
    unittest.main()

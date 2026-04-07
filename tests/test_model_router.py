import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from model_router import CODE_MODEL, CHAT_MODEL, MODEL_ROUTER


class ModelRouterTests(unittest.TestCase):
    def test_chat_tasks_route_to_chat_model(self):
        decision = MODEL_ROUTER.route("conversation", "primary response")
        self.assertEqual(decision.family, "chat")
        self.assertEqual(decision.primary_model, CHAT_MODEL)

    def test_planning_tasks_route_to_chat_model(self):
        decision = MODEL_ROUTER.route("planning", "planning mode detection")
        self.assertEqual(decision.family, "chat")
        self.assertEqual(decision.primary_model, CHAT_MODEL)

    def test_coding_tasks_route_to_code_model(self):
        decision = MODEL_ROUTER.route("coding", "apply bug fix")
        self.assertEqual(decision.family, "code")
        self.assertEqual(decision.primary_model, CODE_MODEL)

    def test_dev_tasks_route_to_code_model(self):
        decision = MODEL_ROUTER.route("dev", "small implementation task")
        self.assertEqual(decision.family, "code")
        self.assertEqual(decision.primary_model, CODE_MODEL)

    def test_code_purpose_hints_route_to_code_model(self):
        decision = MODEL_ROUTER.route("other", "code fix summary")
        self.assertEqual(decision.family, "code")
        self.assertEqual(decision.primary_model, CODE_MODEL)


class ModelRouterVerifyAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_verify_access_falls_back_to_chat_client_for_code_check(self):
        chat_client = SimpleNamespace(
            chat=SimpleNamespace(
                complete_async=AsyncMock(return_value=SimpleNamespace(choices=[]))
            )
        )
        code_client = SimpleNamespace(
            chat=SimpleNamespace(
                complete_async=AsyncMock(side_effect=RuntimeError("401 Unauthorized"))
            )
        )

        checks = await MODEL_ROUTER.verify_access(chat_client, code_client)

        self.assertTrue(checks["chat"]["ok"])
        self.assertTrue(checks["code"]["ok"])
        self.assertTrue(checks["code"]["fallback_used"])


if __name__ == "__main__":
    unittest.main()

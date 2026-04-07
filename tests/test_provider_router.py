import unittest

from provider_router import ProviderExecutionResult, ProviderRouter


class ProviderRouterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.router = ProviderRouter()

    def test_light_classification(self):
        self.assertEqual(self.router.classify_task("open terminal"), "light")

    def test_heavy_classification(self):
        self.assertEqual(self.router.classify_task("build and debug this project end to end"), "heavy")

    def test_cooldown_expires(self):
        self.router._cooldowns["claude"] = (0, "old failure")
        self.assertIsNone(self.router._get_cooldown_reason("claude"))

    async def test_provider_aliases_resolve(self):
        async def fake_claude():
            return type("Status", (), {"name": "claude", "status": "working", "reason": "ok", "automated": True, "available": True, "details": {}})()

        async def fake_opencode():
            return type("Status", (), {"name": "opencode", "status": "working_direct", "reason": "ok", "automated": True, "available": True, "details": {}})()

        self.router._probe_claude = fake_claude  # type: ignore[method-assign]
        self.router._probe_opencode = fake_opencode  # type: ignore[method-assign]
        claude_status = await self.router.get_provider_status("cloudcode")
        opencode_status = await self.router.get_provider_status("oc")
        self.assertEqual(claude_status.details.get("alias_for"), "claude")
        self.assertEqual(opencode_status.details.get("alias_for"), "opencode")

    def test_opencode_config_detection(self):
        self.assertTrue(self.router._opencode_uses_local_ollama({"model": "ollama/minimax-m2.5:cloud"}))
        self.assertFalse(self.router._opencode_uses_local_ollama({"model": "google/gemini"}))

    def test_extract_opencode_output_reads_json_events(self):
        stdout = '{"type":"message","text":"OK"}\n'
        self.assertEqual(self.router._extract_opencode_output(stdout, ""), "OK")

    def test_extract_localai_models_reads_openai_payload(self):
        payload = '{"data":[{"id":"qwen-local"},{"id":"coder-local"}]}'
        self.assertEqual(self.router._extract_localai_models(payload), ["qwen-local", "coder-local"])

    def test_extract_localai_output_reads_chat_response(self):
        payload = '{"choices":[{"message":{"role":"assistant","content":"Done."}}]}'
        self.assertEqual(self.router._extract_localai_output(payload), "Done.")

    def test_opencode_quota_failure_maps_to_quota_blocked(self):
        text = 'responseBody":"{\\"error\\":\\"you have reached your weekly usage limit\\"}" statusCode":429'
        self.assertEqual(self.router._opencode_status_from_failure(text, "misconfigured"), "quota_blocked")

    async def test_run_heavy_task_uses_required_fallback_order(self):
        async def fake_status(provider: str):
            if provider == "claude":
                return type("Status", (), {"automated": True, "available": True, "reason": "ready"})()
            if provider == "ct":
                return type("Status", (), {"automated": True, "available": True, "reason": "ready"})()
            if provider == "localai":
                return type("Status", (), {"automated": True, "available": False, "reason": "offline"})()
            return type("Status", (), {"automated": False, "available": False, "reason": "skip"})()

        calls: list[str] = []

        async def fake_run(provider: str, prompt: str, working_dir: str):
            calls.append(provider)
            if provider == "claude":
                return ProviderExecutionResult("claude", False, "", "rate_limited", "quota hit")
            return ProviderExecutionResult("ct", True, "done", "working", "completed")

        self.router.get_provider_status = fake_status  # type: ignore[method-assign]
        self.router._run_provider = fake_run  # type: ignore[method-assign]

        result = await self.router.run_heavy_task("build the repo", ".")
        self.assertTrue(result.ok)
        self.assertEqual(result.provider, "ct")
        self.assertTrue(result.fallback_used)
        self.assertEqual(calls, ["claude", "ct"])
        self.assertIn("claude", self.router._cooldowns)


if __name__ == "__main__":
    unittest.main()

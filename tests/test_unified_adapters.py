import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace


class AdapterTests(unittest.TestCase):
    def test_probe_accepts_help_written_to_stderr(self):
        from fusion_pipeline.adapters import probe
        with patch("fusion_pipeline.adapters.executable", return_value="native.exe"), patch("fusion_pipeline.adapters.subprocess.run",
                   side_effect=[SimpleNamespace(returncode=0, stdout=b"1.19.3", stderr=b""),
                                SimpleNamespace(returncode=0, stdout=b"", stderr=b"--format --model")]):
            self.assertTrue(probe("opencode")["available"])

    def test_every_provider_uses_stdin_and_fresh_no_resume(self):
        from fusion_pipeline.adapters import build_invocation
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for provider in ("opencode", "codex", "claude"):
                with self.subTest(provider=provider):
                    audit = root / provider
                    audit.mkdir()
                    spec = {"provider": provider, "executable": "cli", "model": "chosen-model", "workspace": str(root),
                            "audit_dir": str(audit), "prompt": "第一行\nLAST_LINE", "worker_command": ["python", "gateway"], "env": {},
                            "credential_settings": str(root / "not-present")}
                    invocation = build_invocation(spec)
                    self.assertEqual(invocation["input"], spec["prompt"].encode("utf-8"))
                    self.assertNotIn(spec["prompt"], invocation["args"])
                    self.assertFalse(set(invocation["args"]) & {"resume", "--resume", "--continue", "--fork"})
                    self.assertIn("chosen-model", invocation["args"])
                    self.assertEqual(invocation["budget_scope"], "mcp_tools_scope")
                    if provider == "claude":
                        self.assertIn("--strict-mcp-config", invocation["args"])
                        self.assertEqual(invocation["args"][invocation["args"].index("--tools") + 1], "")
                    if provider == "codex":
                        self.assertIn("--ignore-user-config", invocation["args"])
                        self.assertIn("--ephemeral", invocation["args"])
                        self.assertIn("features.shell_tool=false", invocation["args"])

    def test_strict_codex_isolation_fails_before_launch(self):
        from fusion_pipeline.adapters import build_invocation
        with self.assertRaises(ValueError):
            build_invocation({"provider": "codex", "strict_tool_isolation": True})

    def test_credentials_inherit_only_selected_provider_and_claude_env(self):
        from fusion_pipeline.adapters import credential_settings
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            codex = root / "config.toml"
            codex.write_text('model_provider="testprovider"\n[model_providers.testprovider]\nname="Test"\nbase_url="https://example.invalid/v1"\nenv_key="BENCHMARK_TEST_KEY"\nwire_api="responses"\n[mcp_servers.secret]\ncommand="do-not-inherit"\n')
            env, extra, metadata = credential_settings("codex", {"BENCHMARK_TEST_KEY": "hidden-value"}, codex)
            self.assertIn('model_provider="testprovider"', extra)
            self.assertNotIn("hidden-value", json.dumps(extra))
            self.assertNotIn("do-not-inherit", json.dumps(extra))
            claude = root / "settings.json"
            claude.write_text(json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "hidden-value", "ANTHROPIC_DEFAULT_SONNET_MODEL": "chosen-model", "UNRELATED_SECRET": "no"}, "hooks": {"bad": True}}))
            env, extra, metadata = credential_settings("claude", {}, claude)
            self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "hidden-value")
            self.assertNotIn("UNRELATED_SECRET", env)
            self.assertNotIn("hidden-value", json.dumps(metadata))

    def test_codex_error_cannot_be_success_after_turn_completed(self):
        from fusion_pipeline.adapters import inspect_events
        events = [{"type": "thread.started", "thread_id": "fresh"}, {"type": "turn.completed"}, {"type": "error", "message": "no"}]
        self.assertFalse(inspect_events("codex", events, "model")["verified"])

    def test_claude_requires_matching_session_and_successful_terminal(self):
        from fusion_pipeline.adapters import inspect_events
        events = [{"type": "system", "subtype": "init", "session_id": "new", "model": "claude-model"},
                  {"type": "result", "subtype": "success", "is_error": False, "session_id": "old"}]
        self.assertFalse(inspect_events("claude", events, "claude-model")["verified"])

    def test_claude_final_error_cannot_be_erased_by_earlier_success(self):
        from fusion_pipeline.adapters import inspect_events
        events = [{"type": "system", "subtype": "init", "session_id": "new", "model": "claude-model"},
                  {"type": "result", "subtype": "error_during_execution", "is_error": False, "session_id": "new"}]
        self.assertFalse(inspect_events("claude", events, "claude-model")["verified"])

    def test_claude_silent_model_switch_is_rejected(self):
        from fusion_pipeline.adapters import inspect_events
        events = [{"type": "system", "subtype": "init", "session_id": "new", "model": "other-model"},
                  {"type": "result", "subtype": "success", "is_error": False, "session_id": "new"}]
        self.assertFalse(inspect_events("claude", events, "expected-model")["verified"])

    def test_claude_alias_uses_configured_exact_model(self):
        from fusion_pipeline.adapters import build_invocation
        with tempfile.TemporaryDirectory() as folder:
            invocation = build_invocation({"provider": "claude", "executable": "cli", "model": "sonnet", "workspace": folder,
                "audit_dir": folder, "prompt": "p", "worker_command": ["gateway"], "credential_settings": str(Path(folder) / "absent"),
                "env": {"ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-5-turbo"}})
            self.assertEqual(invocation["expected_actual_model"], "glm-5-turbo")


if __name__ == "__main__":
    unittest.main()

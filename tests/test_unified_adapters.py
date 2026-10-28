import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace


class AdapterTests(unittest.TestCase):
    def test_opencode_preserves_selected_connection_only_without_logging_credentials(self):
        from fusion_pipeline.adapters import build_invocation
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            connection = root / 'opencode.json'
            connection.write_text(json.dumps({'plugin': ['POLLUTED'], 'mcp': {'secret': {'command': ['POLLUTED']}},
                'instructions': ['POLLUTED'], 'provider': {'custom': {'npm': '@ai-sdk/openai-compatible',
                    'options': {'baseURL': 'https://example.invalid/v1', 'apiKey': 'fixture-credential',
                                'reasoningEffort': 'POLLUTED', 'temperature': 0.8},
                    'models': {'model': {'id': 'backend-model-id', 'name': 'Fixture',
                        'options': {'reasoningEffort': 'POLLUTED'},
                        'variants': {'high': {'reasoningEffort': 'POLLUTED'}}}},
                    'permission': {'*': 'POLLUTED'}}, 'unselected': {'npm': 'POLLUTED'}}}))
            spec = {'provider': 'opencode', 'executable': 'cli', 'model': 'custom/model', 'workspace': folder,
                    'audit_dir': folder, 'prompt': 'p', 'worker_command': ['gateway'],
                    'credential_settings': str(connection), 'env': {}}
            invocation = build_invocation(spec)
            overlay = json.loads(invocation['env']['OPENCODE_CONFIG_CONTENT'])
            selected = overlay.get('provider', {}).get('custom', {})
            self.assertEqual(selected.get('npm'), '@ai-sdk/openai-compatible')
            self.assertEqual(selected['options']['baseURL'], 'https://example.invalid/v1')
            self.assertEqual(selected['models']['model']['id'], 'backend-model-id')
            self.assertNotIn('POLLUTED', json.dumps(overlay))
            self.assertNotIn('fixture-credential', json.dumps(overlay))
            self.assertNotIn('fixture-credential', json.dumps(invocation['auth_metadata']))
            self.assertNotIn('fixture-credential', json.dumps(invocation['args']))
            self.assertIn('fixture-credential', invocation['env'].values())

    def test_opencode_legacy_config_connection_and_explicit_variant_coexist(self):
        from fusion_pipeline.adapters import build_invocation
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            global_config = root / 'opencode'
            global_config.mkdir()
            (global_config / 'opencode.json').write_text(json.dumps({'provider': {'custom': {
                'npm': '@ai-sdk/openai-compatible', 'options': {'baseURL': 'https://example.invalid/v1'},
                'models': {'model': {'id': 'native-id', 'variants': {'high': {'reasoningEffort': 'POLLUTED'}}}}}}}))
            spec = {'provider': 'opencode', 'executable': 'cli', 'model': 'custom/model', 'workspace': folder,
                    'audit_dir': folder, 'prompt': 'p', 'worker_command': ['gateway'], 'env': {'XDG_CONFIG_HOME': folder},
                    'reasoning': {'mode': 'explicit', 'control': 'variant', 'value': 'high'},
                    'reasoning_support': {'provider': 'opencode', 'model': 'custom/model', 'cli_version': 'fixture-1',
                        'status': 'verified', 'controls': {'variant': ['high']}, 'evidence_sha256': '0' * 64,
                        'variant_options': {'high': {'reasoningEffort': 'high'}}}}
            invocation = build_invocation(spec)
            overlay = json.loads(invocation['env']['OPENCODE_CONFIG_CONTENT'])
            provider = overlay.get('provider', {}).get('custom', {})
            self.assertEqual(provider.get('npm'), '@ai-sdk/openai-compatible')
            self.assertEqual(provider['models']['model']['variants']['high'], {'reasoningEffort': 'high'})
            self.assertNotIn('POLLUTED', json.dumps(overlay))

    def test_opencode_jsonc_connection_env_references_and_endpoint_validation(self):
        from fusion_pipeline.adapters import build_invocation
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'connection.jsonc'
            path.write_text('''{
                // connection-only JSONC fixture
                "provider": {"custom": {"npm": "@ai-sdk/openai-compatible",
                "options": {"baseURL": "https://example.invalid/v1", "apiKey": "{env:FIXTURE_KEY}",
                            "headers": {"Authorization": "Bearer fixture-token"},},
                "models": {"model": {"id": "native-model",}},}},
            }''')
            spec = {'provider': 'opencode', 'executable': 'cli', 'model': 'custom/model', 'workspace': folder,
                    'audit_dir': folder, 'prompt': 'p', 'worker_command': ['gateway'],
                    'credential_settings': str(path), 'env': {'FIXTURE_KEY': 'fixture-value'}}
            invocation = build_invocation(spec)
            options = json.loads(invocation['env']['OPENCODE_CONFIG_CONTENT'])['provider']['custom']['options']
            self.assertEqual(options['apiKey'], '{env:FIXTURE_KEY}')
            self.assertEqual(invocation['env']['FIXTURE_KEY'], 'fixture-value')
            self.assertNotIn('fixture-token', json.dumps(options))
            self.assertNotIn('fixture-token', json.dumps(invocation['auth_metadata']))
            path.write_text(json.dumps({'provider': {'custom': {'options': {
                'baseURL': 'https://user:password@example.invalid/v1'}}}}))
            with self.assertRaisesRegex(ValueError, 'opencode_connection_endpoint_invalid'):
                build_invocation(spec)
            path.write_text(json.dumps({'provider': {'custom': {'options': {'unreviewedConnectionProxy': 'unknown'}}}}))
            with self.assertRaisesRegex(ValueError, 'opencode_connection_settings_unsupported'):
                build_invocation(spec)

    def test_opencode_legacy_inline_connection_layers_over_global_but_not_explicit_source(self):
        from fusion_pipeline.adapters import build_invocation
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config_root = root / 'opencode'
            config_root.mkdir()
            global_file = config_root / 'opencode.json'
            inline = {'mcp': {'pollution': {'enabled': True}}, 'provider': {'custom': {
                'options': {'baseURL': 'https://inline.invalid/v1', 'apiKey': '{env:FIXTURE_KEY}'},
                'models': {'model': {'id': 'inline-id'}}}}}
            spec = {'provider': 'opencode', 'executable': 'cli', 'model': 'custom/model', 'workspace': folder,
                    'audit_dir': folder, 'prompt': 'p', 'worker_command': ['gateway'],
                    'env': {'XDG_CONFIG_HOME': folder, 'OPENCODE_CONFIG_CONTENT': json.dumps(inline),
                            'FIXTURE_KEY': 'fixture-secret'}}
            global_file.write_text(json.dumps({'mcp': {'pollution': {'enabled': True}}}))
            invocation = build_invocation(spec)
            overlay = json.loads(invocation['env']['OPENCODE_CONFIG_CONTENT'])
            self.assertEqual(overlay.get('provider', {}).get('custom', {}).get('models', {}).get('model', {}).get('id'), 'inline-id')
            global_file.write_text(json.dumps({'provider': {'custom': {'npm': '@ai-sdk/openai-compatible',
                'options': {'baseURL': 'https://global.invalid/v1'},
                'models': {'model': {'name': 'global-name', 'id': 'global-id'}}}}}))
            invocation = build_invocation(spec)
            selected = json.loads(invocation['env']['OPENCODE_CONFIG_CONTENT'])['provider']['custom']
            self.assertEqual(selected['npm'], '@ai-sdk/openai-compatible')
            self.assertEqual(selected['options']['baseURL'], 'https://inline.invalid/v1')
            self.assertEqual(selected['models']['model'], {'id': 'inline-id', 'name': 'global-name'})
            self.assertNotIn('pollution', json.dumps(selected))
            self.assertNotIn('fixture-secret', json.dumps(invocation['auth_metadata']))
            spec['credential_settings'] = str(global_file)
            invocation = build_invocation(spec)
            selected = json.loads(invocation['env']['OPENCODE_CONFIG_CONTENT'])['provider']['custom']
            self.assertEqual(selected['options']['baseURL'], 'https://global.invalid/v1')
            self.assertNotIn('apiKey', selected['options'])
            self.assertEqual(selected['models']['model']['id'], 'global-id')

    def test_explicit_reasoning_emitted_once_and_environment_pollution_removed(self):
        from fusion_pipeline.adapters import build_invocation
        with tempfile.TemporaryDirectory() as folder:
            for provider, control, native in [('codex', 'effort', 'model_reasoning_effort="high"'),
                                               ('claude', 'effort', '--effort'),
                                               ('opencode', 'variant', '--variant')]:
                model = 'fixture/model' if provider == 'opencode' else 'fixture-model'
                support = {'provider': provider, 'model': model, 'cli_version': 'fixture-1', 'status': 'verified',
                           'controls': {control: ['high']}, 'evidence_sha256': '0' * 64,
                           'variant_options': {'high': {'reasoningEffort': 'high'}}}
                spec = {'provider': provider, 'executable': 'cli', 'model': model, 'workspace': folder,
                        'audit_dir': folder, 'prompt': 'p', 'worker_command': ['gateway'],
                        'credential_settings': str(Path(folder) / 'absent'), 'cli_version': 'fixture-1',
                        'reasoning': {'mode': 'explicit', 'control': control, 'value': 'high'}, 'reasoning_support': support,
                        'env': {'CLAUDE_CODE_EFFORT_LEVEL': 'SENTINEL', 'MAX_THINKING_TOKENS': 'SENTINEL',
                                'CODEX_MODEL_REASONING_EFFORT': 'SENTINEL', 'OPENCODE_CONFIG_CONTENT': 'SENTINEL',
                                'OPENCODE_CONFIG': 'SENTINEL', 'OPENCODE_CONFIG_DIR': 'SENTINEL', 'AUTH_FIXTURE': 'kept'}}
                with self.subTest(provider=provider):
                    invocation = build_invocation(spec)
                    self.assertEqual(invocation['args'].count(native), 1)
                    self.assertNotIn('SENTINEL', json.dumps(invocation['env']))
                    self.assertEqual(invocation['env']['AUTH_FIXTURE'], 'kept')
                    self.assertIsNone(invocation['reasoning']['observed_reasoning'])
                    if provider == 'codex':
                        self.assertEqual(invocation['args'][invocation['args'].index(native) - 1], '-c')
                    else:
                        self.assertEqual(invocation['args'][invocation['args'].index(native) + 1], 'high')
                    if provider == 'opencode':
                        overlay = json.loads(invocation['env']['OPENCODE_CONFIG_CONTENT'])
                        self.assertEqual(overlay['provider']['fixture']['models']['model']['variants']['high'],
                                         {'reasoningEffort': 'high'})

    def test_default_reasoning_clears_inherited_overrides_without_emitting_control(self):
        from fusion_pipeline.adapters import build_invocation
        with tempfile.TemporaryDirectory() as folder:
            for provider in ('codex', 'claude', 'opencode'):
                spec = {'provider': provider, 'executable': 'cli', 'model': 'fixture-model', 'workspace': folder,
                        'audit_dir': folder, 'prompt': 'p', 'worker_command': ['gateway'],
                        'credential_settings': str(Path(folder) / 'absent'), 'reasoning': {'mode': 'cli_default'},
                        'env': {'CLAUDE_CODE_EFFORT_LEVEL': 'SENTINEL', 'CODEX_MODEL_REASONING_EFFORT': 'SENTINEL',
                                'OPENCODE_CONFIG_CONTENT': '{"options":{"reasoningEffort":"SENTINEL"}}'}}
                invocation = build_invocation(spec)
                self.assertFalse(set(invocation['args']) & {'--effort', '--variant', 'model_reasoning_effort="high"'})
                self.assertNotIn('SENTINEL', json.dumps(invocation['env']))

    def test_explicit_expected_actual_model_takes_precedence_over_alias(self):
        from fusion_pipeline.adapters import build_invocation
        with tempfile.TemporaryDirectory() as folder:
            invocation = build_invocation({'provider': 'claude', 'executable': 'cli', 'model': 'sonnet', 'workspace': folder,
                'audit_dir': folder, 'prompt': 'p', 'worker_command': ['gateway'], 'credential_settings': str(Path(folder) / 'absent'),
                'expected_actual_model': 'explicit-actual', 'env': {'ANTHROPIC_DEFAULT_SONNET_MODEL': 'old-alias'}})
            self.assertEqual(invocation['expected_actual_model'], 'explicit-actual')

    def test_reasoning_metadata_mismatch_is_not_copied_from_request(self):
        from fusion_pipeline.adapters import inspect_events
        from fusion_pipeline.reasoning import resolve_reasoning
        resolved = resolve_reasoning({'provider': 'claude', 'model': 'fixture-model',
            'reasoning': {'mode': 'explicit', 'control': 'effort', 'value': 'high'}},
            {'provider': 'claude', 'model': 'fixture-model', 'cli_version': 'fixture-1', 'status': 'verified',
             'controls': {'effort': ['high']}, 'evidence_sha256': '0' * 64})
        events = [{'type': 'system', 'subtype': 'init', 'session_id': 'new', 'model': 'fixture-model',
                   'reasoning': {'control': 'effort', 'value': 'low'}},
                  {'type': 'result', 'subtype': 'success', 'session_id': 'new'}]
        verified = inspect_events('claude', events, 'fixture-model', reasoning=resolved)
        self.assertFalse(verified['verified'])
        self.assertEqual(verified['observed_reasoning']['value'], 'low')
        del events[0]['reasoning']
        absent = inspect_events('claude', events, 'fixture-model', reasoning=resolved)
        self.assertIsNone(absent['observed_reasoning'])
        self.assertEqual(absent['reasoning_verification_status'], 'parameter_emitted_only')

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

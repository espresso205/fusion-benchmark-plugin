"""No models or Fusion: exercise the public stdio protocol and registration."""
import base64
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


class CandidateEntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = self.root / 'registry.json'
        self.registry.write_text(json.dumps({'schema': 'candidate-capability-registry/1',
            'skills': {}, 'mcp_servers': {'cad_native': {'kind': 'builtin_cad_adapter'}}}))

    def selection(self, **updates):
        return dict(schema='candidate-capabilities/1', study_kind='capability_ablation',
                    skill_delivery='gateway_read', exposure_mode='gateway_proxy',
                    skills=[], mcp_servers=['cad_native'], **updates)

    def config(self):
        return dict(provider='claude', model='fixture', cli_executable=sys.executable,
                    capability_registry=str(self.registry), fusion_skill='PRIVATE_LEGACY',
                    runs_root=str(self.root / 'runs'), private_runs_root=str(self.root / 'private'))

    def test_explicit_management_override_removes_legacy_without_mutating_and_is_idempotent(self):
        from fusion_pipeline.candidate_config import resolve_candidate_configuration
        config = self.config()
        derived = resolve_candidate_configuration(config, {'candidate_capabilities': self.selection()})
        self.assertNotIn('fusion_skill', derived)
        self.assertEqual(derived['candidate_capability_selection']['skills'], [])
        self.assertEqual(derived['legacy_skill_fields_removed_by_explicit_override'], ['fusion_skill'])
        self.assertEqual(config['fusion_skill'], 'PRIVATE_LEGACY')
        self.assertEqual(resolve_candidate_configuration(derived), derived)
        with self.assertRaisesRegex(ValueError, 'legacy_skill_selection_conflict'):
            resolve_candidate_configuration(dict(config, candidate_capabilities=self.selection()))

    def test_profile_support_and_saved_reasoning_override_are_bound_and_idempotent(self):
        from fusion_pipeline.candidate_config import resolve_candidate_configuration
        profile = dict(provider='claude', model='profile-model', cli_executable=sys.executable,
                       strict_tool_isolation=True, reasoning={'mode': 'cli_default'})
        support = dict(provider='claude', model='profile-model', cli_version='fixture-1',
                       status='verified', controls={'effort': ['high']}, evidence_sha256='0'*64)
        config = dict(self.config(), candidate_profiles={'chosen': profile},
                      candidate_reasoning_support={'chosen': support}, reasoning_support={'provider': 'other'})
        derived = resolve_candidate_configuration(config, {'candidate_profile': 'chosen',
                    'reasoning': {'mode': 'explicit', 'control': 'effort', 'value': 'high'}})
        self.assertEqual(derived['reasoning_support'], support)
        self.assertEqual(derived['resolved_reasoning']['native_parameters'], ['--effort', 'high'])
        self.assertEqual(resolve_candidate_configuration(derived), derived)
        self.assertIsNone(derived['candidate_selection']['observed_model_evidence'])
        self.assertNotIn('PRIVATE_LEGACY', json.dumps(derived['candidate_selection']))

    def test_invalid_selection_never_starts_process(self):
        from fusion_pipeline.service import ManagementService
        path = self.root / 'config.json'
        path.write_text(json.dumps(self.config()))
        with patch('subprocess.Popen') as launch:
            with self.assertRaisesRegex(ValueError, 'cad_capability_required'):
                ManagementService(path).call('start_run', {'candidate_capabilities':
                    dict(self.selection(), mcp_servers=[])})
            launch.assert_not_called()

    def test_tools_list_uses_observation_hook_and_serve_closes_on_eof_and_failure(self):
        from fusion_pipeline.service import dispatch, serve
        class Service:
            tools = []
            closed = False
            def list_tools(self):
                return {'tools': [{'name': 'observed'}]}
            def close(self):
                self.closed = True
        service = Service()
        response = dispatch(service, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        self.assertEqual(response['result'], {'tools': [{'name': 'observed'}]})
        serve(service, io.BytesIO(), io.BytesIO())
        self.assertTrue(service.closed)
        service.closed = False
        class Broken:
            def __iter__(self):
                raise RuntimeError('transport failure')
        with self.assertRaisesRegex(RuntimeError, 'transport failure'):
            serve(service, Broken(), io.BytesIO())
        self.assertTrue(service.closed)

    def test_preflight_only_probes_selected_profile_executable_and_strips_private_paths(self):
        from fusion_pipeline.service import ManagementService
        config = dict(self.config(), candidate_profile='chosen', candidate_profiles={'chosen': {
            'provider': 'claude', 'model': 'chosen', 'cli_executable': sys.executable,
            'strict_tool_isolation': True, 'reasoning': {'mode': 'cli_default'}}})
        path = self.root / 'config.json'
        path.write_text(json.dumps(config))
        with patch('fusion_pipeline.adapters.probe', return_value={
                'provider': 'claude', 'available': True, 'executable': 'PRIVATE_PATH', 'version': 'fixture'}) as probe:
            result = ManagementService(path).call('preflight', {})
        probe.assert_called_once_with('claude', str(Path(sys.executable).resolve()))
        self.assertNotIn('PRIVATE_PATH', json.dumps(result))
        self.assertEqual(result['candidate_selection']['requested_model'], 'chosen')

    def test_switching_profiles_cannot_reuse_previous_reasoning_support_or_credentials(self):
        from fusion_pipeline.candidate_config import resolve_candidate_configuration
        profiles = {provider: dict(provider=provider, model=provider + '-model', cli_executable=sys.executable,
            strict_tool_isolation=provider != 'codex', reasoning={'mode': 'cli_default'},
            **({'native_tools_authorization': {'source': 'fixture', 'scope': 'fixture'}} if provider == 'codex' else {}))
            for provider in ('claude', 'codex', 'opencode')}
        profiles['claude']['credential_settings'] = str(self.root / 'never-read-auth.json')
        config = dict(self.config(), candidate_profiles=profiles, candidate_reasoning_support={
            provider: {'provider': provider, 'model': provider+'-model'} for provider in profiles})
        for provider in profiles:
            config = resolve_candidate_configuration(config, {'candidate_profile': provider})
            self.assertEqual(config['provider'], provider)
            self.assertEqual(config['reasoning_support']['provider'], provider)
            self.assertEqual(config.get('credential_settings'), profiles[provider].get('credential_settings'))
            self.assertEqual(resolve_candidate_configuration(config), config)

    def test_unsupported_reasoning_rejected_without_probe_or_process(self):
        from fusion_pipeline.service import ManagementService
        config = dict(self.config(), reasoning_support=dict(provider='claude', model='fixture',
            cli_version='fixture', status='verified', controls={'effort': ['low']}, evidence_sha256='0'*64))
        path = self.root / 'config.json'
        path.write_text(json.dumps(config))
        with patch('subprocess.Popen') as launch, patch('fusion_pipeline.adapters.probe') as probe:
            with self.assertRaisesRegex(ValueError, 'reasoning_unsupported'):
                ManagementService(path).call('start_run', {'reasoning': {
                    'mode': 'explicit', 'control': 'effort', 'value': 'high'}})
        launch.assert_not_called()
        probe.assert_not_called()

    def test_valid_selection_is_saved_with_job_and_returned_by_status(self):
        from fusion_pipeline.service import ManagementService
        path = self.root / 'config.json'
        path.write_text(json.dumps(self.config()))
        service = ManagementService(path)
        with patch('subprocess.Popen') as launch:
            result = service.call('start_run', {'candidate_capabilities': self.selection()})
        launch.assert_called_once()
        derived = json.loads((self.root / 'runs/.service' / result['run_id'] / 'config.json').read_text())
        self.assertNotIn('fusion_skill', derived)
        self.assertEqual(result['candidate_selection']['capability_selection']['skills'], [])
        status = service.call('status', {'run_id': result['run_id']})
        self.assertEqual(status['candidate_selection'], result['candidate_selection'])
        self.assertEqual(json.loads(path.read_text())['fusion_skill'], 'PRIVATE_LEGACY')

    def test_capabilities_reports_static_selection_without_any_probe_or_process(self):
        from fusion_pipeline.service import ManagementService
        path = self.root / 'config.json'
        config = self.config()
        config.pop('fusion_skill')
        config['candidate_capabilities'] = self.selection()
        path.write_text(json.dumps(config))
        with patch('subprocess.Popen') as launch, patch('fusion_pipeline.adapters.probe') as probe:
            result = ManagementService(path).call('capabilities', {})
        self.assertEqual(result['configuration_validation']['status'], 'passed')
        self.assertEqual(result['candidate_selection']['capability_selection']['skills'], [])
        launch.assert_not_called()
        probe.assert_not_called()

    def test_preflight_invalidates_reasoning_support_when_observed_cli_version_changes(self):
        from fusion_pipeline.service import ManagementService
        config = dict(self.config(), reasoning={'mode': 'explicit', 'control': 'effort', 'value': 'high'},
            reasoning_support=dict(provider='claude', model='fixture', cli_version='old-version',
                status='verified', controls={'effort': ['high']}, evidence_sha256='0'*64))
        path = self.root / 'config.json'
        path.write_text(json.dumps(config))
        with patch('fusion_pipeline.adapters.probe', return_value={
                'provider': 'claude', 'available': True, 'version': 'new-version'}):
            result = ManagementService(path).call('preflight', {})
        self.assertEqual(result['configuration_validation'],
                         {'status': 'failed', 'reason': 'reasoning_support_unverified'})
        self.assertEqual(result['candidate_selection']['requested_reasoning']['value'], 'high')
        self.assertEqual(result['candidate_selection']['support_status'], 'not_verified')
        self.assertIsNone(result['candidate_selection']['resolved_reasoning'])

    def test_unverified_reasoning_remains_visible_statically_but_never_launches(self):
        from fusion_pipeline.candidate_config import resolve_candidate_configuration
        from fusion_pipeline.service import ManagementService
        config = dict(self.config(), reasoning={'mode': 'explicit', 'control': 'effort', 'value': 'high'})
        path = self.root / 'config.json'
        path.write_text(json.dumps(config))
        service = ManagementService(path)
        with patch('subprocess.Popen') as launch:
            capabilities = service.call('capabilities', {})
            self.assertEqual(capabilities['candidate_selection']['support_status'], 'not_verified')
            with self.assertRaisesRegex(ValueError, 'reasoning_support_unverified'):
                service.call('start_run', {})
        launch.assert_not_called()
        self.assertFalse((self.root / 'runs').exists())
        draft = resolve_candidate_configuration(config)
        self.assertEqual(resolve_candidate_configuration(draft), draft)
        with self.assertRaisesRegex(ValueError, 'reasoning_invalid'):
            resolve_candidate_configuration(config, {'reasoning': {'mode': 'explicit'}})
        with self.assertRaisesRegex(ValueError, 'reasoning_unsupported'):
            resolve_candidate_configuration(config, {'reasoning': {
                'mode': 'explicit', 'control': 'variant', 'value': 'high'}})
        for malformed in ([], {'controls': []}, {'controls': {'effort': {'high': True}}}, {'model': []}):
            with self.subTest(support=malformed), self.assertRaisesRegex(ValueError, 'reasoning_support_invalid'):
                resolve_candidate_configuration(dict(config, reasoning_support=malformed))

    def test_status_projects_runtime_candidate_evidence_without_private_fields(self):
        from fusion_pipeline.service import ManagementService
        path = self.root / 'config.json'
        path.write_text(json.dumps(self.config()))
        identifier = 'a'*32
        job = self.root / 'runs/.service' / identifier
        job.mkdir(parents=True)
        run = self.root / 'runs/new-run'
        run.mkdir()
        (job / 'job.json').write_text(json.dumps(dict(run_id=identifier, state='running', run_path=str(run))))
        (run / 'pipeline-status.json').write_text(json.dumps(dict(state='completed', cases=[], candidate_selection={
            'candidate_profile_id': 'fixture', 'provider': 'claude',
            'observed_model_evidence': {'model': 'observed-model', 'verified': True, 'credential_settings': 'PRIVATE_PATH'},
            'observed_reasoning': {'control': 'effort', 'value': 'high', 'private_path': 'PRIVATE_PATH'},
            'reasoning_verification_status': 'observed', 'cli_executable': 'PRIVATE_PATH',
            'cli_version': 'fixture-1', 'exposure_verification_status': 'tools_list_recorded',
            'observed_tool_surface': [{'name': 'read_skill', 'description': 'PRIVATE_PATH'}, 'aux_lookup__find'],
            'actual_skill_reads': [{'id': 'guide', 'resource_id': None, 'sha256': '1'*64, 'path': 'PRIVATE_PATH'}],
            'per_server_tool_calls': {'lookup': 2, 'PRIVATE_PATH': {'path': 'PRIVATE_PATH'}},
            'cleanup_verified': True, 'emitted_reasoning_parameters': ['--effort', 'high'],
            'reasoning_support_evidence': {'status': 'verified', 'source': 'PRIVATE_PATH', 'evidence_sha256': '0'*64}})))
        result = ManagementService(path).call('status', {'run_id': identifier})
        self.assertEqual(result['candidate_selection']['observed_model_evidence']['model'], 'observed-model')
        self.assertEqual(result['candidate_selection']['observed_reasoning']['value'], 'high')
        self.assertEqual(result['candidate_selection']['observed_tool_surface'], ['read_skill', 'aux_lookup__find'])
        self.assertEqual(result['candidate_selection']['actual_skill_reads'][0]['id'], 'guide')
        self.assertEqual(result['candidate_selection']['per_server_tool_calls'], {'lookup': 2})
        self.assertEqual(result['candidate_selection']['emitted_reasoning_parameters'], ['--effort', 'high'])
        self.assertTrue(result['candidate_selection']['cleanup_verified'])
        self.assertNotIn('PRIVATE_PATH', json.dumps(result))


class UnifiedServiceTests(unittest.TestCase):
    def test_worker_stdio_returns_decodable_native_image_content(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workspace, audit = root / "case", root / "audit"
            workspace.mkdir()
            audit.mkdir()
            (workspace / "TASK.md").write_text("Use the annotated drawing.", encoding="utf-8")
            Image.new("RGB", (13, 9), (11, 22, 33)).save(workspace / "drawing.png")

            def asset(identifier, role, filename, mime):
                data = (workspace / filename).read_bytes()
                return {"id": identifier, "role": role, "path": filename, "mime": mime,
                        "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "display_name": filename}

            manifest = {"schema": "fusion-public-inputs/1", "case_id": "Q01",
                        "task": asset("task", "task", "TASK.md", "text/markdown"),
                        "attachments": [asset("drawing", "drawing", "drawing.png", "image/png")]}
            contract = audit / "worker-contract.json"
            contract.write_text(json.dumps({"schema": "fusion-worker/1", "case_id": "Q01", "workspace": str(workspace),
                "audit_dir": str(audit), "max_tool_calls": 4, "deadline": time.time() + 60,
                "model": "synthetic", "budget_scope": "mcp_tools_scope", "input_manifest": manifest}), encoding="utf-8")
            requests = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "read_image", "arguments": {"id": "drawing"}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "read_input", "arguments": {"name": "drawing.png"}}},
            ]
            result = subprocess.run([sys.executable, "-B", "-m", "fusion_pipeline.cli", "worker", "--contract", str(contract)],
                                    input="\n".join(json.dumps(r) for r in requests).encode(), capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            responses = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual([response["id"] for response in responses], [1, 2, 3, 4])
            image_result = responses[2]["result"]
            self.assertFalse(image_result.get("isError"), image_result)
            content = next(item for item in image_result["content"] if item["type"] == "image")
            data = base64.b64decode(content["data"], validate=True)
            with Image.open(io.BytesIO(data)) as decoded:
                self.assertEqual(decoded.size, (13, 9))
                self.assertEqual(decoded.getpixel((0, 0)), (11, 22, 33))
            self.assertEqual(image_result["structuredContent"]["returned_sha256"], hashlib.sha256(data).hexdigest())
            self.assertTrue(responses[3]["result"]["isError"])
            self.assertEqual(responses[3]["result"]["content"][0]["text"], "image_requires_read_image")

    def test_stdio_handshake_discovery_and_notification_have_only_protocol_output(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "capabilities", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "start_run", "arguments": {}}},
        ]
        result = subprocess.run([sys.executable, "-m", "fusion_pipeline.cli", "serve"],
                                input="\n".join(json.dumps(r) for r in requests).encode(), capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([r["id"] for r in responses], [1, 2, 3, 4])
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual({t["name"] for t in responses[1]["result"]["tools"]}, {"capabilities", "preflight", "start_run", "status", "cancel", "results"})
        self.assertFalse(responses[2]["result"].get("isError", False))
        self.assertTrue(responses[3]["result"]["isError"])

    def test_registration_preserves_existing_keys_and_is_idempotent(self):
        from fusion_pipeline.discovery import register
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "opencode.json"
            original = {"model": "user/model", "mcp": {"existing": {"type": "remote", "url": "https://example.invalid"}}}
            target.write_text(json.dumps(original), encoding="utf-8")
            first = register("opencode", target, [sys.executable, "-m", "fusion_pipeline.cli", "serve"])
            self.assertTrue(first["changed"])
            merged = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(merged["model"], original["model"])
            self.assertEqual(merged["mcp"]["existing"], original["mcp"]["existing"])
            self.assertTrue(Path(first["backup"]).is_file())
            self.assertFalse(register("opencode", target, [sys.executable, "-m", "fusion_pipeline.cli", "serve"])["changed"])

    def test_codex_registration_keeps_unrelated_toml_and_rejects_conflict(self):
        from fusion_pipeline.discovery import register
        import tomllib
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "config.toml"
            original = '# personal comment\nmodel = "user-model"\n[mcp_servers.other]\ncommand = "other"\n'
            target.write_text(original, encoding="utf-8")
            register("codex", target, [sys.executable, "-m", "fusion_pipeline.cli", "serve"])
            raw = target.read_text(encoding="utf-8")
            self.assertTrue(raw.startswith(original))
            self.assertIn("fusion_benchmark", tomllib.loads(raw)["mcp_servers"])
            with self.assertRaises(ValueError):
                register("codex", target, ["different"])

    def test_management_rejects_unknown_and_path_injection_arguments(self):
        from fusion_pipeline.service import ManagementService, dispatch
        service = ManagementService()
        for arguments in ({"config": "secret.json"}, {"run_id": "../../private"}):
            response = dispatch(service, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "start_run", "arguments": arguments}})
            self.assertTrue(response["result"]["isError"])

    def test_results_return_scores_and_coverage_without_private_paths(self):
        from fusion_pipeline.service import ManagementService
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "config.json"
            config.write_text(json.dumps({"runs_root": str(root / "runs"), "private_runs_root": str(root / "private")}))
            identifier = "a" * 32
            job = root / "runs/.service" / identifier
            job.mkdir(parents=True)
            run = root / "runs/fusion-example"
            run.mkdir()
            (job / "job.json").write_text(json.dumps({"run_id": identifier, "state": "completed", "run_path": str(run)}))
            (run / "pipeline-status.json").write_text(json.dumps({"state": "completed", "cases": [{"case_id": "Q01", "final_score": 0.8}]}))
            evaluation = root / "private/fusion-example/evaluation"
            evaluation.mkdir(parents=True)
            (evaluation / "summary.json").write_text(json.dumps({"coordinate_mode":"geometry", "cases": [{"case": "Q01", "final_score": 0.8,
                "coordinate_mode":"geometry", "pose":{"status":"ok","raw":{"R":[[1,0,0],[0,1,0],[0,0,1]],"t":[1,2,3],
                    "aligned_candidate_path":"PRIVATE_PATH", "translation_norm_mm":3.74}},
                "metrics": {"shape": {"score": 0.7,"metric_role":"scored"}}, "measurement_coverage": 0.9, "gt": "PRIVATE_PATH"}], "batch_mean": 0.8}))
            result = ManagementService(config).call("results", {"run_id": identifier})
            self.assertEqual(result["scores"][0]["metrics"]["shape"]["score"], 0.7)
            self.assertEqual(result["scores"][0]["measurement_coverage"], 0.9)
            self.assertNotIn("PRIVATE_PATH", json.dumps(result))
            self.assertEqual(result['coordinate_mode'],'geometry')
            self.assertEqual(result['scores'][0]['metrics']['shape']['metric_role'],'scored')
            self.assertEqual(result['scores'][0]['pose']['raw']['t'],[1,2,3])

    def test_job_failure_is_not_overwritten_by_stale_completed_pipeline(self):
        from fusion_pipeline.service import ManagementService
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "config.json"
            config.write_text(json.dumps({"runs_root": str(root)}))
            identifier = "f" * 32
            job = root / ".service" / identifier
            job.mkdir(parents=True)
            run = root / "fusion-example"
            run.mkdir()
            (job / "job.json").write_text(json.dumps({"run_id": identifier, "state": "failed", "returncode": 2, "run_path": str(run)}))
            (run / "pipeline-status.json").write_text(json.dumps({"state": "completed", "batch_complete": True, "cases": []}))
            result = ManagementService(config).call("status", {"run_id": identifier})
            self.assertEqual(result["state"], "failed")
            self.assertFalse(result["batch_complete"])

    def test_management_advertises_v4_and_rejects_v3_geometry_before_launch(self):
        from fusion_pipeline.service import ManagementService
        capabilities=ManagementService().call('capabilities',{})
        self.assertEqual(capabilities['default_scoring_protocol'],'fusion-step-adapted-v4.0')
        definition=next(t for t in ManagementService.tools if t['name']=='start_run')['inputSchema']['properties']
        self.assertEqual(set(definition['coordinate_mode']['enum']),{'geometry','strict'})
        with tempfile.TemporaryDirectory() as d:
            config=Path(d)/'config.json'
            config.write_text(json.dumps(dict(provider='opencode',model='test/model')),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'coordinate'):
                ManagementService(config).call('start_run',dict(protocol='fusion-step-adapted-v3.0',coordinate_mode='geometry'))


if __name__ == "__main__":
    unittest.main()

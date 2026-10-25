"""No models or Fusion: exercise the public stdio protocol and registration."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class UnifiedServiceTests(unittest.TestCase):
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

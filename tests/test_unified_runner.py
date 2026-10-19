import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class UnifiedRunnerTests(unittest.TestCase):
    def test_shared_desktop_lease_blocks_different_run_roots(self):
        from fusion_pipeline import runner
        from tests.test_fusion_runner_safety import RunnerSafetyTests
        fixture = RunnerSafetyTests()
        fixture.setUp()
        try:
            t = fixture.trial
            t.unified_gateway = True
            common = fixture.root/'desktop'
            common.mkdir()
            lease = common/'fusion-benchmark-desktop.lock'
            lease.write_text('existing different project owner')
            t.prepare = lambda: self.fail('must not access Fusion with an occupied desktop lease')
            with patch.object(runner.tempfile, 'gettempdir', return_value=str(common)):
                self.assertEqual(t.run_all(preflight_only=True), 2)
            self.assertEqual(lease.read_text(), 'existing different project owner')
        finally:
            fixture.doCleanups()

    def test_smoke_rejects_only_failed_fusion_reads(self):
        from fusion_pipeline.runner import Trial
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            trial = Trial.__new__(Trial)
            trial.run, trial.unified_gateway = root, True
            trial.probe = lambda label: self.fail("failed smoke cannot pass to post-probe")
            def launch(workspace, audit, prompt, smoke):
                audit.mkdir(parents=True)
                (audit / "gateway-events.jsonl").write_text(json.dumps({"type": "fusion_result", "is_error": True}) + "\n")
                return {"returncode": 0, "timed_out": False, "guard_classification": "clear", "fusion_tool_calls": 1}
            trial.launch = launch
            with self.assertRaisesRegex(RuntimeError, "successful Fusion"):
                trial.smoke()

    def test_provider_model_and_case_subset_are_preserved_without_model_contact(self):
        from fusion_pipeline.runner import Trial
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = {name: str(root / name) for name in ("python", "questions_root", "private_cases", "runs_root", "private_runs_root", "fusion_skill")}
            config.update(provider="claude", model="claude-exact-model", execution_backend="unified_gateway", case_ids=["Q03"],
                          fusion_connection={"url": "http://127.0.0.1:9000/mcp"}, evaluator_directory=str(root))
            file = root / "config.json"
            file.write_text(json.dumps(config))
            trial = Trial(file)
            self.assertEqual(trial.state["model"], "claude-exact-model")
            self.assertEqual([r["case_id"] for r in trial.state["cases"]], ["Q03"])
            self.assertEqual(trial.state["budget_scope"], "mcp_tools_scope")

    def test_frozen_evaluator_directory_excludes_private_runs(self):
        from fusion_pipeline.runner import snapshot_evaluator, verify_package
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            for name in ("evaluate.py", "worker.py", "weights.json"):
                (source / name).write_text("{}" if name.endswith("json") else "# fixture")
            (source / "private").mkdir()
            (source / "private" / "secret.txt").write_text("never copy")
            out = snapshot_evaluator(source, root / "snapshot")
            self.assertFalse((out / "private").exists())
            from src.io_utils import sha
            verify_package(out, sha(out / "PACKAGE_MANIFEST.json"))


if __name__ == "__main__":
    unittest.main()

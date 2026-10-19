"""Synthetic byte fixtures exercise file contracts, never CAD health."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from scheduler import InvalidTransition, RetryLimitExceeded, Scheduler

BASE = Path(__file__).resolve().parents[1]

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")

class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task = self.root / "TASK.md"
        self.task.write_text("Synthetic test only", encoding="utf-8")
        self.drawing = self.root / "drawing.dxf"
        self.drawing.write_bytes(b"synthetic drawing")
        self.scheduler = Scheduler(self.root / "runs")
        self.scheduler.create_run("run-1")

    def register(self, case_id="Q01", **kwargs):
        self.scheduler.register_case(case_id, task=self.task, drawing=self.drawing, **kwargs)

    def stage(self, case_id="Q01", raw=False, dependency=False):
        self.register(case_id)
        run = self.scheduler.run_dir
        candidate = run / "candidate" / case_id
        candidate.mkdir(parents=True)
        (candidate / "submission.f3d").write_bytes(b"synthetic f3d")
        if raw:
            (candidate / "submission.step").write_bytes(b"synthetic step")
        deps = []
        if dependency:
            dep = candidate / "dependencies" / "part.f3d"
            dep.parent.mkdir()
            dep.write_bytes(b"synthetic dependency")
            deps.append({"path": dep.relative_to(run).as_posix(), "sha256": digest(dep)})
        write_json(candidate / "dependency_manifest.json", {
            "schema_version": 1, "case_id": case_id, "attempt_id": "attempt-1",
            "inspection_method": "synthetic fixture", "reviewer": "unittest", "dependencies": deps,
        })
        for stage in ("case_queued", "modeling", "artifact_staged"):
            self.scheduler.transition(case_id, stage)
        return candidate

    def freeze(self, case_id="Q01", **kwargs):
        self.stage(case_id, **kwargs)
        return self.scheduler.freeze_candidate(case_id)

    def reload(self):
        return Scheduler.load(self.root / "runs", "run-1")

    def test_unique_run_and_path_validation(self):
        self.assertRaises(FileExistsError, self.scheduler.create_run, "run-1")
        for name in ("../other", "CON", "q:stream", "", "Q.1"):
            with self.subTest(name=name):
                self.assertRaises(ValueError, Scheduler(self.root / "runs").create_run, name)

    def test_registration_snapshots_and_binds_inputs(self):
        self.register(protocol_id="proto-x", attempt_id="attempt-7", trusted_step_required=False)
        rec = self.reload().status()["cases"]["Q01"]
        self.assertEqual(rec["contract"]["protocol_id"], "proto-x")
        self.assertEqual(rec["contract"]["attempt_id"], "attempt-7")
        self.assertEqual(rec["contract"]["inputs"]["task"]["sha256"], digest(self.task))
        self.task.write_text("changed original", encoding="utf-8")
        self.assertEqual((self.scheduler.run_dir / "public/Q01/TASK.md").read_text(), "Synthetic test only")

    def test_missing_inputs_and_nonboolean_contract_rejected(self):
        self.assertRaises(ValueError, self.scheduler.register_case, "Q01")
        self.assertRaises(ValueError, self.register, trusted_step_required="false")

    def test_source_freeze_does_not_require_future_export(self):
        manifest = self.freeze()
        self.assertTrue(self.scheduler.verify_artifact_manifest(manifest))
        status = self.reload().status()
        self.assertEqual(status["cases"]["Q01"]["status"], "candidate_frozen")
        self.assertTrue(status["all_submissions_verified"])
        for field in ("artifact_frozen", "evaluation_complete"):
            self.assertFalse(status["cases"]["Q01"][field])
        self.assertFalse(status["batch_complete"])
        self.assertFalse(status["rank_eligible"])

    def test_cannot_bypass_freeze_or_claim_live_completion(self):
        self.stage()
        self.assertRaises(InvalidTransition, self.scheduler.transition, "Q01", "candidate_frozen")
        self.scheduler.freeze_candidate("Q01")
        for state in ("f3d_reopened", "trusted_step_exported", "scored", "reported"):
            self.assertRaises(InvalidTransition, self.scheduler.transition, "Q01", state)
        self.assertRaises(InvalidTransition, self.scheduler.set_evaluation_complete, "Q01")

    def test_dependency_manifest_requires_real_review_fields(self):
        candidate = self.stage()
        write_json(candidate / "dependency_manifest.json", {})
        self.assertRaises(ValueError, self.scheduler.freeze_candidate, "Q01")
        self.assertFalse(self.reload().status()["all_submissions_verified"])

    def test_optional_present_and_dependency_tamper_invalidate(self):
        self.freeze(raw=True, dependency=True)
        run = self.scheduler.run_dir
        for rel in ("candidate/Q01/submission.step", "candidate/Q01/dependencies/part.f3d"):
            path = run / rel
            original = path.read_bytes()
            path.write_bytes(b"tampered")
            self.assertFalse(self.reload().status()["cases"]["Q01"]["submission_verified"])
            path.write_bytes(original)

    def test_input_tamper_invalidates_frozen_submission(self):
        self.freeze()
        (self.scheduler.run_dir / "public/Q01/TASK.md").write_text("changed")
        self.assertFalse(self.reload().status()["all_submissions_verified"])

    def test_input_tamper_before_freeze_is_reported(self):
        self.register()
        (self.scheduler.run_dir / "public/Q01/TASK.md").write_text("changed")
        status = self.reload().status()
        self.assertFalse(status["cases"]["Q01"]["inputs_verified"])
        self.assertIn("verification_error", status["cases"]["Q01"])
        self.assertEqual(self.cli("status", self.scheduler.run_dir).returncode, 2)

    def test_rejected_operation_still_persists_discovered_tamper(self):
        self.freeze()
        (self.scheduler.run_dir / "candidate/Q01/submission.f3d").write_bytes(b"changed")
        self.assertRaises(InvalidTransition, self.scheduler.transition, "Q01", "scored")
        data = json.loads((self.scheduler.run_dir / "checkpoint.json").read_text())
        self.assertFalse(data["cases"]["Q01"]["submission_verified"])
        self.assertEqual(data["events"][-1]["event"], "submission_invalidated")

    def test_tamper_and_missing_file_revalidate_only_relevant_case(self):
        self.freeze()
        self.freeze("Q02")
        (self.scheduler.run_dir / "candidate/Q01/submission.f3d").unlink()
        status = self.reload().status()
        self.assertFalse(status["cases"]["Q01"]["submission_verified"])
        self.assertTrue(status["cases"]["Q02"]["submission_verified"])
        self.assertIn("verification_error", status["cases"]["Q01"])

    def test_rewriting_manifest_cannot_replace_frozen_digest(self):
        manifest = self.freeze()
        data = json.loads(manifest.read_text())
        f3d = self.scheduler.run_dir / "candidate/Q01/submission.f3d"
        f3d.write_bytes(b"new candidate")
        data["artifacts"]["f3d"]["sha256"] = digest(f3d)
        write_json(manifest, data)
        self.assertFalse(self.scheduler.verify_artifact_manifest(manifest))

    def test_external_manifest_does_not_invalidate_real_case(self):
        manifest = self.freeze()
        external = self.root / "outside/Q01/artifact_manifest.json"
        external.parent.mkdir(parents=True)
        external.write_bytes(manifest.read_bytes())
        self.assertFalse(self.scheduler.verify_artifact_manifest(external))
        self.assertTrue(self.reload().status()["all_submissions_verified"])

    def test_dependency_paths_reject_aliases_cross_case_and_ads(self):
        candidate = self.stage(dependency=True)
        path = candidate / "dependency_manifest.json"
        original = json.loads(path.read_text())
        for bad in ("candidate/Q02/dependencies/part.f3d", "candidate/Q01/../Q01/dependencies/part.f3d",
                    "candidate/Q01/dependencies/part.f3d:stream", str(candidate / "dependencies/part.f3d"),
                    "candidate\\Q01\\dependencies\\part.f3d", "candidate/Q01/dependencies/CON"):
            with self.subTest(path=bad):
                data = json.loads(json.dumps(original))
                data["dependencies"][0]["path"] = bad
                write_json(path, data)
                self.assertRaises(ValueError, self.scheduler.freeze_candidate, "Q01")

    def test_duplicate_dependency_paths_rejected(self):
        candidate = self.stage(dependency=True)
        path = candidate / "dependency_manifest.json"
        data = json.loads(path.read_text())
        data["dependencies"].append(data["dependencies"][0])
        write_json(path, data)
        self.assertRaises(ValueError, self.scheduler.freeze_candidate, "Q01")

    def test_freeze_refuses_overwrite(self):
        self.freeze()
        self.assertRaises(InvalidTransition, self.scheduler.freeze_candidate, "Q01")

    def test_summary_is_rebuilt_from_checkpoint(self):
        self.freeze()
        (self.scheduler.run_dir / "summary.json").write_text('{"batch_complete":true}')
        (self.scheduler.run_dir / "events.jsonl").write_text("broken")
        self.assertFalse(self.reload().status()["batch_complete"])
        events = (self.scheduler.run_dir / "events.jsonl").read_text().splitlines()
        last = json.loads(events[-1])
        for key in ("run_id", "case_id", "attempt_id", "stage", "request_id", "timestamp_utc",
                    "monotonic_s", "responsibility", "event", "sequence"):
            self.assertIn(key, last)

    def test_checkpoint_identity_and_schema_are_validated(self):
        self.register()
        path = self.scheduler.run_dir / "checkpoint.json"
        original = path.read_text()
        mutations = (lambda d: d.update(run_id="wrong"),
                     lambda d: d["cases"]["Q01"].update(status="scored"),
                     lambda d: d["cases"]["Q01"].update(retries=True),
                     lambda d: d["cases"]["Q01"].update(retries=-1),
                     lambda d: d["cases"]["Q01"].update(artifact_frozen=True),
                     lambda d: d["events"][0].update(sequence=True),
                     lambda d: d["events"][0].update(stage="scored"))
        for mutate in mutations:
            data = json.loads(original)
            mutate(data)
            write_json(path, data)
            self.assertRaises(ValueError, self.reload)
        path.write_text(original, encoding="utf-8")
        self.assertRaises(ValueError, Scheduler.load, self.root / "runs", "../escape")

    def test_strict_json_rejects_duplicate_keys_nonfinite_and_truncation(self):
        path = self.scheduler.run_dir / "checkpoint.json"
        for text in ('{"run_id":"run-1","run_id":"run-1"}', '{"x":NaN}', '{"x":1e999}', '{'):
            path.write_text(text, encoding="utf-8")
            self.assertRaises(ValueError, self.reload)

    def test_rejected_operation_discards_partial_changes_but_keeps_input_revalidation(self):
        self.register()
        original = (self.scheduler.run_dir / "public/Q01/TASK.md").read_bytes()
        (self.scheduler.run_dir / "public/Q01/TASK.md").write_bytes(b"changed")
        self.assertFalse(self.reload().status()["cases"]["Q01"]["inputs_verified"])
        (self.scheduler.run_dir / "public/Q01/TASK.md").write_bytes(original)
        real_event = Scheduler._event
        def fail_transition(scheduler, case_id, event, **kwargs):
            if event == "case_queued":
                raise ValueError("injected event failure")
            return real_event(scheduler, case_id, event, **kwargs)
        with patch.object(Scheduler, "_event", fail_transition):
            self.assertRaises(ValueError, self.scheduler.transition, "Q01", "case_queued")
        record = self.reload().status()["cases"]["Q01"]
        self.assertTrue(record["inputs_verified"])
        self.assertEqual(record["status"], "preflight")

    def test_lock_prevents_parallel_writer(self):
        lock = self.scheduler.run_dir / ".scheduler.lock"
        lock.write_text("owned")
        self.assertRaises(RuntimeError, self.register)
        self.assertEqual(lock.read_text(), "owned")

    def test_two_instances_reload_under_lock_without_lost_updates(self):
        other = self.reload()
        self.register()
        other.register_case("Q02", task=self.task, drawing=self.drawing)
        self.assertEqual(set(self.reload().status()["cases"]), {"Q01", "Q02"})

    def test_checkpoint_commit_recovers_after_derived_view_failure(self):
        self.register()
        with patch.object(Scheduler, "_write_views", side_effect=OSError("injected failure")):
            self.assertRaises(OSError, self.scheduler.transition, "Q01", "case_queued")
        restored = self.reload().status()
        self.assertEqual(restored["cases"]["Q01"]["status"], "case_queued")
        events = (self.scheduler.run_dir / "events.jsonl").read_text().splitlines()
        self.assertEqual(sum(json.loads(row)["event"] == "case_queued" for row in events), 1)

    def test_retry_requires_confirmed_safe_infrastructure_failure(self):
        self.register()
        for code in ("fusion_task_unknown", "candidate_timeout", "export_failed", "candidate_invalid"):
            self.assertRaises(RetryLimitExceeded, self.scheduler.retry, "Q01", code,
                              retryable=True, no_side_effects_confirmed=True)
        self.assertRaises(RetryLimitExceeded, self.scheduler.retry, "Q01", "plugin_unavailable", retryable="true")
        self.assertRaises(RetryLimitExceeded, self.scheduler.retry, "Q01", "plugin_unavailable", retryable=True)
        self.scheduler.retry("Q01", "plugin_unavailable", retryable=True, no_side_effects_confirmed=True)
        self.assertRaises(RetryLimitExceeded, self.reload().retry, "Q01", "plugin_unavailable",
                          retryable=True, no_side_effects_confirmed=True)
        self.assertEqual(self.reload().status()["cases"]["Q01"]["contract"]["attempt_id"], "attempt-1")

    def test_retry_after_modeling_is_blocked(self):
        self.stage()
        self.assertRaises(RetryLimitExceeded, self.scheduler.retry, "Q01", "plugin_unavailable",
                          retryable=True, no_side_effects_confirmed=True)

    def test_symlink_candidate_rejected(self):
        candidate = self.stage()
        target = self.root / "outside.f3d"
        target.write_bytes(b"synthetic f3d")
        f3d = candidate / "submission.f3d"
        f3d.unlink()
        try:
            f3d.symlink_to(target)
        except OSError as error:
            self.skipTest(f"symlink privilege unavailable: {error}")
        self.assertRaises(ValueError, self.scheduler.freeze_candidate, "Q01")

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_junction_manifest_directory_rejected(self):
        manifest = self.freeze()
        directory = manifest.parent
        outside = self.root / "outside-frozen"
        directory.rename(outside)
        command = f'New-Item -ItemType Junction -Path "{directory}" -Target "{outside}" | Out-Null'
        result = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        try:
            self.assertFalse(self.scheduler.verify_artifact_manifest(manifest))
            self.assertFalse(self.reload().status()["all_submissions_verified"])
        finally:
            os.rmdir(directory)

    def cli(self, *args, script=False):
        return subprocess.run([sys.executable, *(["scheduler.py"] if script else ["-m", "scheduler"]),
                               *map(str, args)], cwd=BASE, capture_output=True, text=True, encoding="utf-8")

    def test_cli_both_entrypoints_status_verify_and_errors(self):
        manifest = self.freeze()
        for script in (False, True):
            result = self.cli("status", self.scheduler.run_dir, script=script)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)["all_submissions_verified"])
            valid = self.cli("verify", self.root / "runs", "run-1", manifest, script=script)
            self.assertEqual(valid.returncode, 0, valid.stderr)
            invalid = self.cli("verify", self.root / "runs", "run-1", self.root / "missing.json", script=script)
            self.assertEqual(invalid.returncode, 2, invalid.stdout)
            self.assertFalse(json.loads(invalid.stdout)["valid"])
            bad_id = self.cli("verify", self.root / "runs", "../escape", manifest, script=script)
            self.assertEqual(bad_id.returncode, 2)
            self.assertNotIn("Traceback", bad_id.stderr)

    def test_cli_register_transition_and_freeze_workflow(self):
        run = self.scheduler.run_dir
        result = self.cli("register-case", run, "Q01", "--task", self.task, "--drawing", self.drawing)
        self.assertEqual(result.returncode, 0, result.stderr)
        for stage in ("case_queued", "modeling", "artifact_staged"):
            result = self.cli("transition", run, "Q01", stage)
            self.assertEqual(result.returncode, 0, result.stderr)
        missing = self.cli("freeze-candidate", run, "Q01")
        self.assertEqual(missing.returncode, 2)

if __name__ == "__main__":
    unittest.main()

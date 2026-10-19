"""Failure injection for the coordinator; no model, network or Fusion calls."""
import csv
import contextlib
import base64
import hashlib
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from fusion_pipeline import runner


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class RunnerSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.trial = runner.Trial.__new__(runner.Trial)
        t = self.trial
        t.run = self.root / "run"
        t.run.mkdir()
        t.private = self.root / "private"
        t.private.mkdir()
        t.evaluator = self.root / "evaluator"
        t.evaluator.mkdir()
        files = {}
        for name, content in {"evaluate.py": b"# packaged evaluator", "worker.py": b"# worker", "weights.json": b'{"weights":{}}'}.items():
            (t.evaluator / name).write_bytes(content)
            files[name] = {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        write_json(t.evaluator / "PACKAGE_MANIFEST.json", {"files": files})
        t.state = {"state": "preparing", "run_id": "synthetic", "cases": [],
                   "batch_complete": False, "evaluator_manifest_sha256": runner.sha(t.evaluator / "PACKAGE_MANIFEST.json")}
        t.config = {"runs_root": str(self.root), "python": "NEVER_EXECUTE", "opencode": "NEVER_EXECUTE"}
        t.cli_config = {}
        t._processes = []
        t.ledger = self.root / "ledger"
        t.references = {}
        t.scheduler = lambda *args: ""

    def case(self):
        t = self.trial
        workspace = t.ledger / "candidate/Q01"
        workspace.mkdir(parents=True)
        for name in ("TASK.md", "drawing.dxf"):
            public = t.ledger / "public/Q01" / name
            public.parent.mkdir(parents=True, exist_ok=True)
            public.write_bytes(b"synthetic public input")
            (workspace / name).write_bytes(public.read_bytes())
        row = {"case_id": "Q01", "workspace": str(workspace), "state": "trusted_exported",
               "trusted_step": str(self.root / "synthetic.step"), "trusted_step_sha256": "a" * 64}
        for role in ("gt", "annotations"):
            (self.root / role).write_bytes(b"synthetic private fixture, not CAD")
        t.references["Q01"] = {role: str(self.root / role) for role in ("gt", "annotations")}
        t.references["Q01"]["expected_hashes"] = {role: runner.sha(self.root / role) for role in ("gt", "annotations")}
        t.state["cases"] = [row]
        return row

    def evaluation_outputs(self, cases, *, blocked=False):
        t = self.trial
        out = t.private / "evaluation"
        out.mkdir()
        manifest = {"cases": cases, "weights_sha256": runner.sha(t.evaluator / "weights.json"),
                    "cases_sha256": runner.sha(t.private / "evaluation_cases.json"), "protocol_id": "fusion-step-adapted-v2.0"}
        write_json(out / "manifest.json", manifest)
        final = {"case": "Q01", "status": "reference_invalid" if blocked else "ok", "final_score": None if blocked else 1.0,
                 "run_manifest_sha256": runner.sha(out / "manifest.json"),
                 "metrics": {name: {"score": None if blocked else 1.0} for name in
                             ("validity", "shape", "dimension", "distance", "mass", "face_match", "section")}}
        write_json(out / "Q01/aggregate.json", final)
        summary = {"cases": [final], "batch_mean": None if blocked else 1.0,
                   "run_manifest_sha256": runner.sha(out / "manifest.json"), "code_unchanged": True,
                   "original_inputs_unchanged": True, "all_cases_aggregatable": not blocked, "ranking_eligible": False}
        write_json(out / "summary.json", summary)
        score = "不聚合" if blocked else "100.000"
        (out / "report.md").write_text(f"|Q01|—|completed|valid/valid|ok|7/7|{score}|—|\n", encoding="utf-8")
        with (out / "comparison.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["case", "module", "new_score", "aggregate_status"])
            writer.writeheader()
            for name in final["metrics"]:
                writer.writerow({"case": "Q01", "module": name, "new_score": "" if blocked else "1.0", "aggregate_status": final["status"]})
        return out

    def evaluate_with(self, mutate=None, returncode=0, blocked=False):
        t = self.trial
        def process(*args, **kwargs):
            cases = json.loads((t.private / "evaluation_cases.json").read_text())["cases"]
            out = self.evaluation_outputs(cases, blocked=blocked)
            if mutate:
                mutate(out)
            return NS(returncode=returncode)
        with patch.object(runner.subprocess, "run", side_effect=process):
            t.evaluate()

    def test_modified_packaged_evaluator_blocks_scoring_before_process(self):
        self.case()
        (self.trial.evaluator / "evaluate.py").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "package|payload"):
            self.evaluate_with()
        self.assertFalse((self.trial.private / "evaluation").exists())

    def test_modified_packaged_weights_blocks_scoring(self):
        self.case()
        (self.trial.evaluator / "weights.json").write_bytes(b"{}")
        with self.assertRaisesRegex(ValueError, "package|payload"):
            self.evaluate_with()

    def test_rewritten_package_manifest_cannot_refreeze_changed_code(self):
        self.case()
        write_json(self.trial.evaluator / "PACKAGE_MANIFEST.json", {"files": {}})
        with self.assertRaisesRegex(ValueError, "manifest"):
            self.evaluate_with()

    def test_changed_working_task_blocks_evaluation(self):
        row = self.case()
        (Path(row["workspace"]) / "TASK.md").write_text("changed task")
        with self.assertRaisesRegex(ValueError, "public input"):
            self.evaluate_with()

    def test_changed_working_drawing_cannot_be_frozen_after_modeling(self):
        row = self.case()
        (Path(row["workspace"]) / "submission.f3d").write_bytes(b"synthetic candidate")
        self.trial.probe = lambda _: None
        def launch(*args, **kwargs):
            (Path(row["workspace"]) / "drawing.dxf").write_text("changed drawing")
            return {"returncode": 0, "timed_out": False, "guard_classification": "clear"}
        self.trial.launch = launch
        with self.assertRaisesRegex(ValueError, "public input"):
            self.trial.model(row)
        self.assertNotEqual(row["state"], "candidate_frozen")

    def test_failed_evaluator_with_summary_does_not_complete_case(self):
        row = self.case()
        with self.assertRaisesRegex(RuntimeError, "exit|return"):
            self.evaluate_with(returncode=1)
        self.assertNotEqual(row["state"], "evaluation_complete")

    def test_missing_report_or_comparison_blocks_completion(self):
        self.case()
        with self.assertRaises((ValueError, FileNotFoundError)):
            self.evaluate_with(lambda out: (out / "report.md").unlink())

    def test_missing_comparison_blocks_completion(self):
        self.case()
        with self.assertRaisesRegex(ValueError, "publication"):
            self.evaluate_with(lambda out: (out / "comparison.csv").unlink())

    def test_report_score_must_match_summary(self):
        self.case()
        with self.assertRaisesRegex(ValueError, "report"):
            self.evaluate_with(lambda out: (out / "report.md").write_text("|Q01|—|completed|valid/valid|ok|7/7|0.000|—|", encoding="utf-8"))

    def test_code_mutation_during_evaluation_blocks_completion(self):
        self.case()
        with self.assertRaisesRegex(ValueError, "package|payload"):
            self.evaluate_with(lambda _: (self.trial.evaluator / "worker.py").write_text("changed worker"))

    def test_mismatched_aggregate_blocks_completion(self):
        self.case()
        with self.assertRaisesRegex(ValueError, "aggregate"):
            self.evaluate_with(lambda out: write_json(out / "Q01/aggregate.json", {"case": "Q01", "final_score": 0}))

    def test_comparison_score_must_match_aggregate(self):
        self.case()
        def mutate(out):
            p = out / "comparison.csv"
            p.write_text(p.read_text(encoding="utf-8-sig").replace(",1.0,", ",0.0,"), encoding="utf-8-sig")
        with self.assertRaisesRegex(ValueError, "comparison"):
            self.evaluate_with(mutate)

    def test_complete_blocked_evaluation_is_terminal_without_score(self):
        row = self.case()
        self.evaluate_with(returncode=2, blocked=True)
        self.assertTrue(row["evaluation_complete"])
        self.assertIsNone(row["final_score"])
        self.assertIsNone(self.trial.state["batch_mean"])

    def test_actual_synthetic_pipeline_publication_validates_without_fusion(self):
        from test_pipeline import PipelineTests
        case = PipelineTests("test_complete_analytic_box_scores_one_and_does_not_write_beside_inputs")
        case.setUp()
        try:
            result = case.run_cli()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            summary = runner.validate_evaluation(case.output, case.cases, result.returncode, runner.sha(runner.BASE / "weights.json"))
            self.assertAlmostEqual(summary["batch_mean"], 1.0)
        finally:
            case.doCleanups()

    def test_keyboard_interrupt_marks_blocked_preserves_lock_and_reraises(self):
        t = self.trial
        t.prepare = lambda: None
        t.smoke = lambda: None
        t.state["cases"] = [{"case_id": "Q01", "state": "queued"}]
        def model(_):
            raise KeyboardInterrupt("synthetic interrupt")
        t.model = model
        with self.assertRaises(KeyboardInterrupt):
            t.run_all()
        self.assertEqual(t.state["state"], "blocked")
        self.assertTrue((self.root / ".fusion-benchmark.lock").exists())
        self.assertEqual(json.loads((t.run / "pipeline-status.json").read_text())["state"], "blocked")

    def test_existing_global_lock_is_preserved_and_reported(self):
        lock = self.root / ".fusion-benchmark.lock"
        lock.write_bytes(b"existing owner")
        self.assertEqual(self.trial.run_all(), 2)
        self.assertEqual(lock.read_bytes(), b"existing owner")
        self.assertEqual(self.trial.state["state"], "blocked")

    def test_interrupt_during_cleanup_is_not_swallowed(self):
        t = self.trial
        t.prepare = lambda: None
        t.smoke = lambda: None
        t._processes = [NS(pid=1)]
        with patch.object(runner, "stop_tree", side_effect=KeyboardInterrupt("cleanup interrupted")):
            with self.assertRaises(KeyboardInterrupt):
                t.run_all(preflight_only=True)
        self.assertEqual(t.state["state"], "blocked")
        self.assertTrue((self.root / ".fusion-benchmark.lock").exists())

    def test_invalid_guard_cannot_be_clear(self):
        for state in ({}, {"version": 1, "pendingFusion": 0}, {"version": 1, "pendingFusion": 0, "model_verified": False, "sessionID": "s"}):
            with self.subTest(state=state):
                self.assertNotEqual(runner.classify_guard(state), "clear")

    def test_expired_guard_cannot_be_clear(self):
        state = {"version": 1, "pendingFusion": 0, "model_verified": True, "sessionID": "s", "deadline": 1}
        self.assertNotEqual(runner.classify_guard(state), "clear")

    def test_guard_rejects_unfinished_local_calls_and_inconsistent_counts(self):
        state = {"version": 1, "pendingFusion": 0, "model_verified": True, "sessionID": "s",
                 "deadline": time.time() * 1000 + 60000, "total_tool_calls": 1, "fusion_tool_calls": 0,
                 "calls": {"call1": {"tool": "read", "sessionID": "s", "fusion": False, "status": "pending", "permitted": True}}}
        self.assertNotEqual(runner.classify_guard(state), "clear")
        state["calls"]["call1"].update(status="completed", terminal_seen=True)
        state["total_tool_calls"] = 2
        self.assertNotEqual(runner.classify_guard(state), "clear")
        state["total_tool_calls"] = 1
        state["calls"]["call1"]["sessionID"] = "other-session"
        self.assertNotEqual(runner.classify_guard(state), "clear")

    def test_source_inventory_includes_imported_io_helper(self):
        with patch.object(runner, "BASE", self.root):
            (self.root / "src").mkdir()
            (self.root / "src/io_utils.py").write_bytes(b"original helper")
            before = runner.source_hashes()
            (self.root / "src/io_utils.py").write_bytes(b"modified helper")
            self.assertNotEqual(before, runner.source_hashes())

    def test_duplicate_private_case_is_rejected_before_scheduler_dispatch(self):
        t = self.trial
        row = self.case()
        reference = {"case": "Q01", **t.references["Q01"]}
        write_json(Path(reference["annotations"]), {"gt_sha256": reference["expected_hashes"]["gt"]})
        reference["expected_hashes"]["annotations"] = runner.sha(reference["annotations"])
        registry = self.root / "private-cases.json"
        write_json(registry, {"cases": [reference, reference]})
        archive = self.root / "archive.zip"
        archive.write_bytes(b"synthetic archive boundary")
        t.config.update(evaluator_zip=str(archive), private_cases=str(registry), questions_root=str(self.root))
        def unexpected(*args):
            self.fail("duplicate registry reached scheduler dispatch")
        t.scheduler = unexpected
        with patch.object(runner, "extract_evaluator", return_value=t.evaluator), \
                patch.object(runner.subprocess, "run", return_value=NS(returncode=0, stdout=runner.MODEL)):
            with self.assertRaisesRegex(ValueError, "duplicate"):
                t.prepare()

    def test_changed_runner_source_blocks_final_completion(self):
        t = self.trial
        t.state["runner_code_sha256"] = {"fusion_pipeline/runner.py": "0" * 64}
        t.prepare = lambda: None
        t.smoke = lambda: None
        self.assertEqual(t.run_all(preflight_only=True), 2)
        self.assertEqual(t.state["state"], "blocked")
        self.assertTrue((self.root / ".fusion-benchmark.lock").exists())

    def mock_launch(self, *, interrupt=False, prompt="synthetic prompt", mutate_messages=None):
        t = self.trial
        workspace = t.run / "workspace"
        workspace.mkdir()
        audit = t.run / "audit"
        contract = {"run_id": "r", "case_id": "workspace", "attempt_id": "attempt-1"}
        expected = hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        loaded = {"version": 1, "contract_sha256": expected, "total_tool_calls": 0,
                  "fusion_tool_calls": 0, "pendingFusion": 0, "model_verified": False,
                  "sessionID": None, "deadline": time.time() * 1000 + 180000}
        captures = {"environments": [], "requests": [], "argv": [], "popen_options": [], "stdin": []}
        messages = [
            {"info": {"id": "u1", "sessionID": "s", "role": "user", "time": {"created": 1},
                      "model": {"providerID": "lanyi", "modelID": "gpt-6.1-sol"}},
             "parts": [{"type": "text", "text": prompt}]},
            {"info": {"id": "a1", "sessionID": "s", "role": "assistant", "parentID": "u1",
                      "time": {"created": 2, "completed": 3}, "finish": "stop",
                      "providerID": "lanyi", "modelID": "gpt-6.1-sol"},
             "parts": [{"type": "text", "text": "SMOKE_OK"}, {"type": "step-finish", "reason": "stop"}]},
        ]
        if mutate_messages:
            mutate_messages(messages)
        def environment(*args, **kwargs):
            audit.mkdir()
            write_json(audit / "contract.json", contract)
            write_json(audit / "guard-state.json", loaded)
            return {}
        class Process:
            def __init__(self, client):
                self.client = client
                self.stopped = False
                self.pid = 43211 if client else 43210
                self.returncode = None
            def poll(self):
                return self.returncode
            def wait(self, timeout):
                if self.client and interrupt:
                    raise KeyboardInterrupt("synthetic client interrupt")
                terminal = {**loaded, "model_verified": True, "sessionID": "s", "total_tool_calls": 2, "fusion_tool_calls": 1,
                            "calls": {"c1": {"tool": "read", "sessionID": "s", "fusion": False, "status": "completed", "permitted": True, "terminal_seen": True},
                                      "c2": {"tool": "autodeskfusion_fusion_mcp_read", "sessionID": "s", "fusion": True, "status": "completed", "permitted": True, "terminal_seen": True}}}
                write_json(audit / "guard-state.json", terminal)
                self.returncode = 0
                return 0
            def communicate(self, input, timeout):
                captures["stdin"].append(input)
                self.wait(timeout)
                return None, None
        processes = []
        def popen(args, **kwargs):
            captures["environments"].append(dict(kwargs["env"]))
            captures["argv"].append(args)
            captures["popen_options"].append(kwargs)
            process = Process("run" in args)
            processes.append(process)
            return process
        def stop(process):
            process.stopped = True
            process.returncode = -1
        @contextlib.contextmanager
        def response(request, **kwargs):
            captures["requests"].append(request)
            if "/session/" in request.full_url:
                self.assertFalse(processes[0].stopped, "session must be verified before server cleanup")
                payload = json.dumps(messages).encode("utf-8")
            else:
                payload = b"{}"
            yield NS(status=200, read=lambda: payload)
        @contextlib.contextmanager
        def port():
            yield NS(bind=lambda _: None, getsockname=lambda: ("127.0.0.1", 45000))
        with patch.object(t, "child_environment", side_effect=environment), patch.object(runner.subprocess, "Popen", side_effect=popen), \
                patch.object(runner.urllib.request, "urlopen", side_effect=response), patch.object(runner.socket, "socket", side_effect=port), \
                patch.object(runner, "stop_tree", side_effect=stop):
            if interrupt:
                with self.assertRaises(KeyboardInterrupt):
                    t.launch(workspace, audit, prompt, smoke=True)
                self.assertEqual([p.stopped for p in processes], [True, True])
                return
            result = t.launch(workspace, audit, prompt, smoke=True)
        return result, captures

    def test_interrupt_cleans_client_and_server_processes(self):
        self.mock_launch(interrupt=True)

    def test_local_server_uses_ephemeral_auth_and_returns_verified_counts(self):
        result, captures = self.mock_launch()
        env = captures["environments"][0]
        self.assertTrue(env.get("OPENCODE_SERVER_PASSWORD"))
        self.assertEqual(captures["environments"][1]["OPENCODE_SERVER_PASSWORD"], env["OPENCODE_SERVER_PASSWORD"])
        expected = "Basic " + base64.b64encode((env["OPENCODE_SERVER_USERNAME"] + ":" + env["OPENCODE_SERVER_PASSWORD"]).encode()).decode()
        self.assertTrue(all(request.get_header("Authorization") == expected for request in captures["requests"]))
        self.assertEqual(result["tool_calls"], 2)
        self.assertEqual(result["fusion_tool_calls"], 1)
        self.assertTrue(result["model_verified"])

    def test_multiline_unicode_prompt_is_complete_utf8_stdin_and_not_an_argument(self):
        prompt = '第一行 "quoted" & %PATH%\n第二行 $literal\n尾行验证 END_中文'
        result, captures = self.mock_launch(prompt=prompt)
        self.assertEqual(captures["stdin"], [prompt.encode("utf-8")])
        self.assertNotIn(prompt, captures["argv"][1])
        self.assertEqual(captures["popen_options"][1].get("stdin"), runner.subprocess.PIPE)
        self.assertFalse(captures["popen_options"][1].get("text", False))
        self.assertEqual((self.trial.run / "audit/prompt.txt").read_bytes(), prompt.encode("utf-8"))
        self.assertTrue(result["session_verification"]["verified"])
        saved = json.loads((self.trial.run / "audit/session-verification.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["expected_prompt_sha256"], hashlib.sha256(prompt.encode("utf-8")).hexdigest())
        self.assertEqual(saved["received_prompt_sha256"], saved["expected_prompt_sha256"])
        self.assertNotIn(prompt, json.dumps(saved, ensure_ascii=False))
        self.assertTrue(any("/session/s/message?directory=" in request.full_url for request in captures["requests"]))

    def test_truncated_prompt_is_rejected_even_with_successful_exit(self):
        with self.assertRaisesRegex(RuntimeError, "session verification"):
            self.mock_launch(prompt="first line\nlast line", mutate_messages=lambda messages: messages[0]["parts"][0].update(text="first line"))
        saved = json.loads((self.trial.run / "audit/session-verification.json").read_text(encoding="utf-8"))
        self.assertFalse(saved["verified"])
        self.assertIn("prompt_mismatch", saved["errors"])

    def test_incomplete_assistant_is_rejected_even_with_successful_exit(self):
        with self.assertRaisesRegex(RuntimeError, "session verification"):
            self.mock_launch(mutate_messages=lambda messages: messages[-1]["info"]["time"].pop("completed"))

    def test_assistant_error_is_rejected_even_with_successful_exit(self):
        with self.assertRaisesRegex(RuntimeError, "session verification"):
            self.mock_launch(mutate_messages=lambda messages: messages[-1]["info"].update(error={"name": "APIError", "data": {"message": "synthetic"}}))

    def test_nonstop_assistant_is_rejected_even_with_successful_exit(self):
        with self.assertRaisesRegex(RuntimeError, "session verification"):
            self.mock_launch(mutate_messages=lambda messages: messages[-1]["info"].update(finish="length"))

    def test_wrong_assistant_model_is_rejected_even_with_successful_exit(self):
        with self.assertRaisesRegex(RuntimeError, "session verification"):
            self.mock_launch(mutate_messages=lambda messages: messages[-1]["info"].update(modelID="other-model"))

    def test_duplicate_user_is_rejected_even_with_successful_exit(self):
        with self.assertRaisesRegex(RuntimeError, "session verification"):
            self.mock_launch(mutate_messages=lambda messages: messages.insert(1, messages[0]))

    def test_running_stored_tool_is_rejected_even_when_assistant_claims_stop(self):
        with self.assertRaisesRegex(RuntimeError, "session verification"):
            self.mock_launch(mutate_messages=lambda messages: messages[-1]["parts"].append(
                {"type": "tool", "callID": "unfinished", "state": {"status": "running"}}))


if __name__ == "__main__":
    unittest.main()

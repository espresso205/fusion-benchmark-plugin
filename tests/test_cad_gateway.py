"""Gateway boundary tests use a deterministic external CAD process double."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch


def descriptor(name, properties=None, required=()):
    return {"name": name, "inputSchema": {"type": "object", "properties": properties or {},
            "required": list(required), "additionalProperties": False}}


def result(value):
    return {"content": [{"type": "text", "text": json.dumps(value)}], "isError": False}


class NativeProcess:
    backend = "solidworks"

    def __init__(self):
        token = {"parameter_plan_token": {"type": "string", "minLength": 1}}
        definitions = [descriptor("create_new_part", token, token),
            descriptor("save_document", {**token, "filepath": {"type": "string"},
                       "confirm_overwrite": {"type": "boolean"}}, token),
            descriptor("close_document", {"save": {"type": "boolean"}, "document_title": {"type": "string"},
                                          "expected_path": {"type": "string"}}),
            descriptor("get_document_info"), descriptor("list_open_documents"),
            descriptor("validate_parameter_plan", {"plan": {"type": "object"}, "ttl_seconds": {
                "type": "integer", "minimum": 60, "maximum": 604800}}, ["plan"]),
            descriptor("draw_circle", {**token, "radius": {"type": "number", "exclusiveMinimum": 0}},
                       ["radius", "parameter_plan_token"]),
            descriptor("extrude_sketch", {**token, "depth": {"type": "number", "exclusiveMinimum": 0},
                "sketch_name": {"type": "string"}, "unit": {"type": "string"}},
                ["depth", "parameter_plan_token"]),
            descriptor("execute_python"), descriptor("create_new_assembly"), descriptor("open_document"),
            descriptor("fusion_mcp_read")]
        self.tools = {item["name"]: item for item in definitions}
        self.documents = []
        self.identity = {"pid": 100, "session_id": 1, "version": "34.4"}
        self.reply = None
        self.native_bytes = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"native-part"
        self.save_warning_code = 0
        self.plan_token = "issued-native-token"
        self.calls = []
        self.config = {"executor_timeout_s": 180}
        self.deadline = None

    def list_tools(self):
        return self.tools

    def probe_workspace(self):
        return {"safe": True, "identity": dict(self.identity), "documents": list(self.documents),
                "active_document": self.documents[0] if self.documents else None}

    def require_call_budget(self, name, arguments, *, post_probe=True):
        from fusion_pipeline.solidworks import SolidWorksAdapter
        return SolidWorksAdapter.require_call_budget(self, name, arguments, post_probe=post_probe)

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.reply is not None:
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply
        if name == "validate_parameter_plan":
            # Mirrors tools._validate_parameter_plan_tool in bridge 0.2.1rc2.
            return result({"ok": True, "success": True, "status": "success", "tool": name,
                "message": "Parameter plan is complete and ready for SolidWorks execution",
                "data": {"ready": True, "parameter_plan_token": self.plan_token,
                         "plan_digest": "b" * 64, "expires_at": "2099-01-01T00:00:00+00:00",
                         "normalized_plan": arguments["plan"], "default_unit_bindings": [], "warnings": []}})
        if name == "create_new_part":
            self.documents = [{"title": "Part1", "path": "", "document_type": "Part", "dirty": False}]
        if name == "save_document":
            path = Path(arguments.get("filepath") or self.documents[0]["path"])
            prior_path = self.documents[0]["path"]
            path.write_bytes(self.native_bytes)
            self.documents = [{"title": path.name, "path": str(path), "document_type": "Part", "dirty": False}]
            # Native save schema captured from synthetic02 on SolidWorks 34.4.1.
            return result({"ok": True, "success": True, "status": "success", "tool": name,
                "data": {"path": str(path), "document_path_before": prior_path, "document_path_after": str(path),
                    "file_verified": True, "operation_started": True, "automatic_retry_allowed": False,
                    "same_path": prior_path == str(path), "protected_references": [], "embedded_references": [],
                    "api_result": True, "save_error_code": 0, "save_warning_code": self.save_warning_code,
                    "method": "Extension.SaveAs",
                    "com_invocation": "InvokeTypes", "save_options": 1, "changed_references": [],
                    "size_bytes": path.stat().st_size, "output_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "completion_state": "completed"},
                "document_context": {"path": str(path), "title": path.name, "type": "Part", "configuration": "Default"},
                "benchmark_instance": dict(self.identity)})
        if name == "close_document":
            self.documents = []
        return result({"success": True})


class CadGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace, self.audit = self.root / "公开 空间", self.root / "private"
        self.workspace.mkdir()
        self.audit.mkdir()
        (self.workspace / "TASK.md").write_text("Create one part, radius 3 mm.", encoding="utf-8")
        task = (self.workspace / "TASK.md").read_bytes()
        self.task_hash = hashlib.sha256(task).hexdigest()
        manifest = {"schema": "fusion-public-inputs/1", "case_id": "Q01", "attachments": [],
            "task": {"id": "task", "role": "task", "path": "TASK.md", "mime": "text/markdown",
                     "bytes": len(task), "sha256": self.task_hash, "display_name": "Task"}}
        self.contract = {"schema": "cad-worker/1", "run_id": "r", "case_id": "Q01", "attempt_id": "attempt-1",
            "protocol_id": "fusion-step-adapted-v4.0", "cad_backend": "solidworks", "cad_config_sha256": "a" * 64,
            "workspace": str(self.workspace), "audit_dir": str(self.audit), "max_tool_calls": 80,
            "deadline": time.time() + 600, "budget_scope": "mcp_tools_scope", "public_files": ["TASK.md"],
            "input_manifest": manifest,
            "modeling_authorization": {"delegated_assumptions": True, "source": "benchmark:delegated",
                                       "task_sha256": self.task_hash}}
        self.path = self.audit / "contract.json"
        self.native = NativeProcess()

    def gateway(self):
        from fusion_pipeline.worker_gateway import WorkerGateway
        if not self.path.exists():
            self.path.write_text(json.dumps(self.contract), encoding="utf-8")
        self.native.deadline = self.contract["deadline"]
        return WorkerGateway(self.path, upstream=self.native)

    def call(self, name, arguments=None):
        return self.gateway().call("cad_call", {"tool": name, "arguments": arguments or {}})

    def plan(self, operations):
        return {"objective": "Create the public task part", "parameters": [{"name": "radius", "value": 3,
                "unit": "mm", "role": "user_input", "decision": "user_specified",
                "basis": "Explicit task radius in mm; SHA256=" + self.task_hash,
                "source_ref": "public:task#radius"}], "assumptions": [], "output_requirements": [],
                "control_operations": [{"tool": name, "arguments": args} for name, args in operations]}

    def authorize(self, operations):
        self.call("validate_parameter_plan", {"plan": self.plan(operations), "ttl_seconds": 60})

    def create(self):
        self.authorize([("create_new_part", {})])
        self.call("create_new_part", {"parameter_plan_token": "issued-native-token"})

    def test_catalog_intersects_native_discovery_and_maintained_backend_policy(self):
        gateway = self.gateway()
        names = {entry["name"] for entry in gateway.call("cad_tools", {})["tools"]}
        self.assertEqual(names, {"create_new_part", "save_document", "close_document", "get_document_info",
                                "list_open_documents", "validate_parameter_plan", "draw_circle", "extrude_sketch"})
        self.assertIn("cad_call", {item["name"] for item in gateway.tools})
        self.assertNotIn("fusion_call", {item["name"] for item in gateway.tools})
        for forbidden in ("execute_python", "create_new_assembly", "open_document", "fusion_mcp_read", "invented"):
            with self.subTest(forbidden=forbidden), self.assertRaisesRegex(RuntimeError, "cad_tool_denied"):
                self.call(forbidden)
        self.assertEqual(self.native.calls, [])

    def test_real_schema_rejects_types_bounds_missing_and_extra_fields(self):
        for args in ({"radius": "3", "parameter_plan_token": "x"}, {"radius": 0, "parameter_plan_token": "x"},
                     {"radius": 3}, {"radius": 3, "parameter_plan_token": "x", "evil": True}):
            with self.subTest(args=args), self.assertRaisesRegex(RuntimeError, "cad_arguments_invalid"):
                self.call("draw_circle", args)
        self.assertEqual(self.native.calls, [])

    def test_paths_never_escape_or_replace_public_or_existing_files(self):
        self.create()
        for filename in (str(self.root / "secret.SLDPRT"), "../secret.SLDPRT", str(self.workspace / "TASK.md"),
                         str(self.workspace / "other.SLDPRT"), str(self.workspace / "submission.SLDPRT") + ":hidden"):
            with self.subTest(filename=filename), self.assertRaisesRegex(RuntimeError, "cad_path_denied"):
                self.call("save_document", {"filepath": filename, "parameter_plan_token": "issued-native-token"})
        target = self.workspace / "submission.SLDPRT"
        target.write_bytes(b"existing-user-file")
        with self.assertRaisesRegex(RuntimeError, "cad_path_exists"):
            self.call("save_document", {"filepath": str(target), "confirm_overwrite": True,
                                       "parameter_plan_token": "issued-native-token"})
        self.assertEqual(target.read_bytes(), b"existing-user-file")

    def test_existing_user_document_cannot_be_claimed_by_matching_filename(self):
        self.native.documents = [{"title": "submission.SLDPRT", "path": str(self.workspace / "submission.SLDPRT"),
                                  "document_type": "Part", "dirty": True}]
        with self.assertRaisesRegex(RuntimeError, "cad_document_not_owned"):
            self.call("get_document_info")
        self.assertEqual(self.native.calls, [])

    def test_instance_drift_preserves_pending_and_prevents_replay_after_restart(self):
        self.call("list_open_documents")
        self.native.identity["pid"] = 101
        with self.assertRaisesRegex(RuntimeError, "cad_task_unknown"):
            self.call("list_open_documents")
        with self.assertRaisesRegex(RuntimeError, "cad_task_unknown"):
            self.call("list_open_documents")
        state = json.loads((self.audit / "gateway-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["pending_cad"], 1)
        self.assertEqual(len(self.native.calls), 1)

    def test_nested_business_unknown_is_durable_and_audited_with_full_identity(self):
        self.native.reply = result({"ok": False, "data": {"status": "unknown", "partial_mutation_possible": True}})
        with self.assertRaisesRegex(RuntimeError, "cad_task_unknown"):
            self.call("list_open_documents")
        with self.assertRaisesRegex(RuntimeError, "cad_task_unknown"):
            self.call("list_open_documents")
        events = [json.loads(line) for line in (self.audit / "gateway-events.jsonl").read_text(encoding="utf-8").splitlines()]
        for event in events:
            for key in ("run_id", "case_id", "attempt_id", "protocol_id", "cad_backend", "cad_config_sha256"):
                self.assertEqual(event[key], self.contract[key])
        unknown = next(event for event in events if event["type"] == "cad_result")
        self.assertEqual(unknown["outcome"], "unknown")
        self.assertEqual(unknown["native_result"], self.native.reply)
        self.assertTrue(unknown["request_id"])
        self.assertEqual(len(unknown["args_sha256"]), 64)
        self.assertEqual(len(self.native.calls), 1)

    def test_known_business_failure_does_not_block_later_read(self):
        self.native.reply = result({"ok": False, "error": {"code": "invalid_selection"}})
        reply = self.call("list_open_documents")
        self.assertEqual(reply["content"], self.native.reply["content"])
        self.native.reply = None
        self.call("list_open_documents")
        self.assertEqual(json.loads((self.audit / "gateway-state.json").read_text(encoding="utf-8"))["pending_cad"], 0)

    def test_contradictory_business_receipt_preserves_pending(self):
        self.native.reply = result({"ok": True, "success": False})
        with self.assertRaisesRegex(RuntimeError, "cad_task_unknown"):
            self.call("list_open_documents")
        self.assertEqual(json.loads((self.audit / "gateway-state.json").read_text(encoding="utf-8"))["pending_cad"], 1)

    def test_unknown_completion_state_preserves_pending(self):
        self.native.reply = result({"ok": False, "data": {"completion_state": "unknown"}})
        with self.assertRaisesRegex(RuntimeError, "cad_task_unknown"):
            self.call("list_open_documents")

    def test_native_budget_is_persistent_and_separate_from_catalog(self):
        self.contract["max_cad_tool_calls"] = 1
        self.gateway().call("cad_tools", {})
        self.call("list_open_documents")
        with self.assertRaisesRegex(RuntimeError, "cad_tool_budget_exceeded"):
            self.call("list_open_documents")
        self.assertEqual(len(self.native.calls), 1)

    def test_changing_state_alone_cannot_reset_native_budget(self):
        self.contract["max_cad_tool_calls"] = 1
        self.call("list_open_documents")
        path = self.audit / "gateway-state.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        state["cad_calls"] = 0
        path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "audit_state_mismatch"):
            self.call("list_open_documents")
        self.assertEqual(len(self.native.calls), 1)

    def test_vendor_schema_cannot_remove_required_parameter_gate(self):
        self.native.tools["draw_circle"]["inputSchema"]["required"] = ["radius"]
        with self.assertRaisesRegex(RuntimeError, "cad_parameter_gate_missing"):
            self.call("draw_circle", {"radius": 3})
        self.assertEqual(self.native.calls, [])

    def test_external_json_schema_references_are_not_fetched(self):
        self.native.tools["get_document_info"]["inputSchema"] = {"$ref": "https://invalid.example/schema"}
        with self.assertRaisesRegex(RuntimeError, "cad_arguments_invalid"):
            self.call("get_document_info")

    def test_deadline_and_cancellation_prevent_native_calls(self):
        self.contract["deadline"] = time.time() - 1
        with self.assertRaisesRegex(RuntimeError, "time_budget_exceeded"):
            self.call("list_open_documents")
        self.assertEqual(self.native.calls, [])

    def test_timeout_transport_never_retries_native_call(self):
        self.native.reply = TimeoutError("native timeout")
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "cad_task_unknown"):
                self.call("list_open_documents")
        self.assertEqual(len(self.native.calls), 1)

    def test_insufficient_post_probe_budget_refuses_native_dispatch_without_pending(self):
        self.contract["deadline"] = time.time() + 60
        with self.assertRaisesRegex(RuntimeError, "cad_insufficient_post_probe_budget"):
            self.call("list_open_documents")
        self.assertEqual(self.native.calls, [])
        state = json.loads((self.audit / "gateway-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["pending_cad"], 0)
        self.assertEqual(state["cad_calls"], 0)
        self.assertEqual(state["maintenance_probe_calls"], 1)
        events = [json.loads(line) for line in (self.audit / "gateway-events.jsonl").read_text(encoding="utf-8").splitlines()]
        event = next(row for row in events if row["type"] == "cad_budget_rejected")
        self.assertFalse(event["native_dispatched"])
        self.assertEqual(event["error_type"], "TimeoutError")
        self.assertTrue(event["error_message"])
        self.assertFalse(any(row["type"] == "cad_started" for row in events))

    def test_budget_rejection_preserves_mutation_plan_position(self):
        self.create()
        self.call("validate_parameter_plan", {"plan": self.plan([("draw_circle", {"radius": 3})]),
                                              "ttl_seconds": 604800})
        count = len(self.native.calls)
        with patch("fusion_pipeline.worker_gateway.time.time", return_value=self.contract["deadline"] - 200):
            with self.assertRaisesRegex(RuntimeError, "cad_insufficient_post_probe_budget"):
                self.call("draw_circle", {"radius": 3, "parameter_plan_token": "issued-native-token"})
        state = json.loads((self.audit / "gateway-state.json").read_text(encoding="utf-8"))
        self.assertEqual(len(self.native.calls), count)
        self.assertEqual(state["pending_cad"], 0)
        self.assertEqual(state["parameter_plan"]["next_index"], 0)

    def test_post_probe_failure_records_reason_and_remains_unknown_without_retry(self):
        before = self.native.probe_workspace()
        with patch.object(self.native, "probe_workspace", side_effect=[before, TimeoutError("post-probe deadline exhausted")]):
            with self.assertRaisesRegex(RuntimeError, "cad_task_unknown"):
                self.call("list_open_documents")
        state = json.loads((self.audit / "gateway-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["pending_cad"], 1)
        self.assertEqual(len(self.native.calls), 1)
        events = [json.loads(line) for line in (self.audit / "gateway-events.jsonl").read_text(encoding="utf-8").splitlines()]
        event = next(row for row in events if row["type"] == "cad_probe_failed")
        self.assertEqual(event["phase"], "after_call")
        self.assertEqual(event["error_type"], "TimeoutError")
        self.assertEqual(event["error_message"], "post-probe deadline exhausted")
        outcome = next(row for row in events if row["type"] == "cad_result")
        self.assertEqual(outcome["outcome"], "unknown")
        self.assertEqual(outcome["failure_stage"], "post_call_probe")
        with self.assertRaisesRegex(RuntimeError, "cad_task_unknown"):
            self.call("list_open_documents")
        self.assertEqual(len(self.native.calls), 1)

    def test_vendor_schema_cannot_admit_undeclared_native_arguments(self):
        self.native.tools["list_open_documents"]["inputSchema"].pop("additionalProperties")
        with self.assertRaisesRegex(RuntimeError, "cad_arguments_invalid"):
            self.call("list_open_documents", {"arbitrary_path": str(self.root)})
        self.assertEqual(self.native.calls, [])

    def test_saved_native_hardlink_cannot_overwrite_another_file(self):
        self.create()
        target = self.workspace / "submission.SLDPRT"
        self.authorize([("save_document", {"filepath": str(target)})])
        self.call("save_document", {"filepath": str(target), "parameter_plan_token": "issued-native-token"})
        target.unlink()
        outside = self.root / "user-file.SLDPRT"
        outside.write_bytes(b"user-original")
        os.link(outside, target)
        with self.assertRaisesRegex(RuntimeError, "cad_path_denied"):
            self.call("save_document", {"filepath": str(target), "parameter_plan_token": "issued-native-token"})
        self.assertEqual(outside.read_bytes(), b"user-original")

    def test_report_rejects_hardlinked_evidence(self):
        original = self.root / "private-report.json"
        original.write_text('{"private":true}', encoding="utf-8")
        os.link(original, self.workspace / "worker_report.json")
        with self.assertRaisesRegex(RuntimeError, "cad_path_denied"):
            self.gateway().call("write_report", {"report": {}})
        self.assertEqual(original.read_text(encoding="utf-8"), '{"private":true}')

    def test_replaced_workspace_alias_is_rejected_before_report_write(self):
        gateway = self.gateway()
        moved = self.root / "original-workspace"
        outside = self.root / "outside"
        outside.mkdir()
        self.workspace.rename(moved)
        try:
            self.workspace.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            moved.rename(self.workspace)
            self.skipTest(f"directory symlink not available: {error}")
        self.addCleanup(lambda: self.workspace.unlink() if self.workspace.is_symlink() else None)
        with self.assertRaisesRegex(RuntimeError, "cad_path_denied"):
            gateway.call("write_report", {"report": {}})
        self.assertFalse((outside / "worker_report.json").exists())

    def test_maintenance_probes_are_counted_separately_from_worker_calls(self):
        self.call("list_open_documents")
        state = json.loads((self.audit / "gateway-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state.get("maintenance_probe_calls"), 2)
        self.assertEqual(state["cad_calls"], 1)

    def test_plan_rejects_private_provenance_and_false_user_acceptance(self):
        for source, role, decision in (("private:answer", "user_input", "user_specified"),
                                       ("benchmark:delegated", "agent_suggestion", "user_accepted")):
            plan = self.plan([("create_new_part", {})])
            plan["parameters"][0].update(source_ref=source, role=role, decision=decision)
            with self.subTest(source=source), self.assertRaisesRegex(RuntimeError, "parameter_source_denied"):
                self.call("validate_parameter_plan", {"plan": plan})
        self.assertEqual(self.native.calls, [])

    def test_public_plan_requires_matching_frozen_hash_and_annotation_location(self):
        valid_basis = "Explicit radius in mm; SHA256=" + self.task_hash
        for source, basis in (("public:task#radius", "Explicit task dimension"),
                              ("public:task#radius", "SHA256=" + "0" * 64),
                              ("public:task#radius", "SHA256=" + self.task_hash + "0"),
                              ("public:task", valid_basis), ("public:task#", valid_basis),
                              ("public:task#   ", valid_basis)):
            plan = self.plan([("create_new_part", {})])
            plan["parameters"][0].update(source_ref=source, basis=basis)
            with self.subTest(source=source, basis=basis), self.assertRaisesRegex(RuntimeError, "parameter_source_denied"):
                self.call("validate_parameter_plan", {"plan": plan})
        self.assertEqual(self.native.calls, [])

    def test_public_attachment_hash_cannot_be_replaced_by_task_hash(self):
        from PIL import Image
        image = self.workspace / "front.png"
        Image.new("RGB", (5, 5), "white").save(image)
        data = image.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        self.contract["input_manifest"]["attachments"].append({"id": "front", "role": "drawing",
            "path": "front.png", "mime": "image/png", "bytes": len(data), "sha256": digest, "display_name": "Front"})
        self.contract["public_files"].append("front.png")
        plan = self.plan([("create_new_part", {})])
        plan["parameters"][0]["source_ref"] = "public:front#top-left radius 3 mm"
        with self.assertRaisesRegex(RuntimeError, "parameter_source_denied"):
            self.call("validate_parameter_plan", {"plan": plan})
        self.assertEqual(self.native.calls, [])
        plan["parameters"][0]["basis"] = "Radius annotation in mm; SHA256=" + digest
        self.call("validate_parameter_plan", {"plan": plan})
        self.assertEqual(len(self.native.calls), 1)

    def test_delegated_parameters_and_assumptions_require_task_or_contract_hash(self):
        for field in ("parameters", "assumptions"):
            for basis in ("Implementation choice", "SHA256=" + "0" * 64):
                plan = self.plan([("create_new_part", {})])
                item = {"name": "plane", "value": "front", "unit": "name", "role": "agent_suggestion",
                        "decision": "delegated", "source_ref": "benchmark:delegated", "basis": basis}
                plan[field] = [item]
                with self.subTest(field=field, basis=basis), self.assertRaisesRegex(RuntimeError, "parameter_source_denied"):
                    self.call("validate_parameter_plan", {"plan": plan})
        self.assertEqual(self.native.calls, [])
        contract = self.gateway().call("get_contract", {})
        self.assertEqual(contract["contract_sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        for digest in (self.task_hash, contract["contract_sha256"]):
            plan = self.plan([("create_new_part", {})])
            plan["assumptions"] = [{"name": "plane", "decision": "delegated", "source_ref": "benchmark:delegated",
                                    "basis": "Use front plane as an implementation choice; SHA256=" + digest}]
            self.call("validate_parameter_plan", {"plan": plan})
        self.assertEqual(len(self.native.calls), 2)

    def test_public_plan_without_frozen_input_manifest_is_denied(self):
        self.contract.pop("input_manifest")
        with self.assertRaisesRegex(RuntimeError, "parameter_source_denied"):
            self.authorize([("create_new_part", {})])
        self.assertEqual(self.native.calls, [])

    def test_plan_token_is_bound_to_exact_arguments_order_and_expiry(self):
        self.create()
        self.authorize([("draw_circle", {"radius": 3})])
        for args in ({"radius": 3, "parameter_plan_token": "forged"},
                     {"radius": 4, "parameter_plan_token": "issued-native-token"}):
            with self.subTest(args=args), self.assertRaisesRegex(RuntimeError, "parameter_plan_mismatch"):
                self.call("draw_circle", args)
        with patch("fusion_pipeline.worker_gateway.time.time", return_value=time.time() + 61):
            with self.assertRaisesRegex(RuntimeError, "parameter_plan_expired"):
                self.call("draw_circle", {"radius": 3, "parameter_plan_token": "issued-native-token"})
        self.call("draw_circle", {"radius": 3, "parameter_plan_token": "issued-native-token"})
        with self.assertRaisesRegex(RuntimeError, "parameter_plan_mismatch"):
            self.call("draw_circle", {"radius": 3, "parameter_plan_token": "issued-native-token"})

    def test_failed_native_mutation_does_not_advance_vendor_plan_position(self):
        self.create()
        self.authorize([("draw_circle", {"radius": 3})])
        self.native.reply = result({"ok": False, "success": False, "status": "error", "error_code": "no_active_sketch"})
        self.call("draw_circle", {"radius": 3, "parameter_plan_token": "issued-native-token"})
        self.native.reply = None
        self.call("draw_circle", {"radius": 3, "parameter_plan_token": "issued-native-token"})
        state = json.loads((self.audit / "gateway-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["parameter_plan"]["next_index"], 1)

    def selection_failure(self):
        # Shape captured from PNG02; the adapter separately verifies worker/instance evidence.
        return {"ok": False, "success": False, "status": "error", "tool": "extrude_sketch",
            "error_code": "selection_operation_failed", "vendor_error_code": 104,
            "vendor_error_name": "swSelectionError", "message": "Requested sketch was not found",
            "data": {"mutation_started": False, "automatic_retry_allowed": False},
            "benchmark_instance": dict(self.native.identity),
            "executor": {"serial": True, "dedicated_worker": True, "timeout_enforced": True,
                         "soft_timeout_exceeded": False, "timeout_exceeded": False},
            "process_isolation": {"enabled": True, "worker_pid": 123,
                                  "returncode": 0, "same_worker_instance_guard": True}}

    def test_known_selection_failure_keeps_plan_position_until_explicit_revised_plan(self):
        self.create()
        original = {"depth": 3, "sketch_name": "Sketch1", "unit": "mm"}
        corrected = {**original, "sketch_name": "草图1"}
        self.authorize([("extrude_sketch", original)])
        self.native.reply = result(self.selection_failure())
        before_calls = len(self.native.calls)
        self.call("extrude_sketch", {**original, "parameter_plan_token": "issued-native-token"})
        self.assertEqual(len(self.native.calls), before_calls + 1)
        state = json.loads((self.audit / "gateway-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["pending_cad"], 0)
        self.assertEqual(state["parameter_plan"]["next_index"], 0)
        events = [json.loads(line) for line in (self.audit / "gateway-events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row for row in events if row["type"] == "cad_result"][-1]["outcome"], "completed_failure")
        with self.assertRaisesRegex(RuntimeError, "parameter_plan_mismatch"):
            self.call("extrude_sketch", {**corrected, "parameter_plan_token": "issued-native-token"})
        self.assertEqual(len(self.native.calls), before_calls + 1)
        self.native.reply = None
        self.native.plan_token = "revised-native-token"
        self.authorize([("extrude_sketch", corrected)])
        with self.assertRaisesRegex(RuntimeError, "parameter_plan_mismatch"):
            self.call("extrude_sketch", {**corrected, "parameter_plan_token": "issued-native-token"})
        self.call("extrude_sketch", {**corrected, "parameter_plan_token": "revised-native-token"})
        state = json.loads((self.audit / "gateway-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["parameter_plan"]["next_index"], 1)
        self.assertEqual(state["parameter_plan"]["operations"], [{"tool": "extrude_sketch", "arguments": corrected}])

    def test_uncertain_selection_failure_blocks_replanning_and_never_advances(self):
        for field, value in (("partial_mutation_possible", True), ("partial_change_possible", True),
                             ("completion_state", "unknown"), ("reconciliation_required", True)):
            with self.subTest(field=field):
                self.setUp()
                self.create()
                arguments = {"depth": 3, "sketch_name": "Sketch1", "unit": "mm"}
                self.authorize([("extrude_sketch", arguments)])
                payload = self.selection_failure()
                payload["data"][field] = value
                self.native.reply = result(payload)
                with self.assertRaisesRegex(RuntimeError, "cad_task_unknown"):
                    self.call("extrude_sketch", {**arguments, "parameter_plan_token": "issued-native-token"})
                state = json.loads((self.audit / "gateway-state.json").read_text(encoding="utf-8"))
                self.assertEqual(state["pending_cad"], 1)
                self.assertEqual(state["parameter_plan"]["next_index"], 0)
                calls = len(self.native.calls)
                self.native.reply = None
                with self.assertRaisesRegex(RuntimeError, "cad_task_unknown"):
                    self.authorize([("extrude_sketch", {**arguments, "sketch_name": "草图1"})])
                self.assertEqual(len(self.native.calls), calls)

    def test_submit_freezes_native_configuration_unverified_dependencies_and_revokes(self):
        self.create()
        native = self.workspace / "submission.SLDPRT"
        self.authorize([("save_document", {"filepath": str(native)})])
        self.call("save_document", {"filepath": str(native), "parameter_plan_token": "issued-native-token"})
        self.call("close_document", {"document_title": native.name, "expected_path": str(native), "save": False})
        self.gateway().call("write_report", {"report": {"status": "declared", "dependencies": []}})
        (self.workspace / "dependency_manifest.json").write_text('{"candidate":"not trusted"}')
        receipt = self.gateway().call("submit", {"configuration": "Default"})
        self.assertEqual(receipt["schema"], "cad-submission/1")
        self.assertEqual(receipt["native_document"]["configuration"], "Default")
        self.assertEqual(receipt["native_document"]["path"], "submission.SLDPRT")
        self.assertEqual(receipt["native_document"]["sha256"], hashlib.sha256(native.read_bytes()).hexdigest())
        dependency = self.workspace / "dependency_manifest.json"
        self.assertEqual(receipt["native_document"]["dependency_sha256"], hashlib.sha256(dependency.read_bytes()).hexdigest())
        self.assertIn("unverified", json.loads(dependency.read_text(encoding="utf-8"))["inspection_method"])
        self.assertTrue(list(self.audit.glob("candidate-dependency-*.json")))
        self.assertEqual(json.loads((self.audit / "gateway-state.json").read_text(encoding="utf-8"))["schema"], "cad-gateway/1")
        with self.assertRaisesRegex(RuntimeError, "attempt_submitted"):
            self.gateway().call("get_contract", {})

    def test_submit_requires_configuration_and_rejects_bytes_without_native_save(self):
        (self.workspace / "submission.SLDPRT").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1forged-native")
        self.gateway().call("write_report", {"report": {}})
        with self.assertRaisesRegex(RuntimeError, "configuration_required"):
            self.gateway().call("submit", {})
        with self.assertRaisesRegex(RuntimeError, "native_save_unverified"):
            self.gateway().call("submit", {"configuration": "Default"})
        self.assertFalse((self.audit / "submission-receipt.json").exists())

    def test_submit_rejects_changed_saved_bytes_and_configuration(self):
        self.create()
        native = self.workspace / "submission.SLDPRT"
        self.authorize([("save_document", {"filepath": str(native)})])
        self.call("save_document", {"filepath": str(native), "parameter_plan_token": "issued-native-token"})
        self.call("close_document", {"document_title": native.name, "expected_path": str(native), "save": False})
        self.gateway().call("write_report", {"report": {}})
        saved = native.read_bytes()
        native.write_bytes(saved[:-1] + bytes([saved[-1] ^ 1]))
        with self.assertRaisesRegex(RuntimeError, "native_save_mismatch"):
            self.gateway().call("submit", {"configuration": "Default"})
        native.write_bytes(saved)
        with self.assertRaisesRegex(RuntimeError, "native_save_mismatch"):
            self.gateway().call("submit", {"configuration": "NotTheSavedConfiguration"})
        self.assertFalse((self.audit / "submission-receipt.json").exists())
        receipt = self.gateway().call("submit", {"configuration": "Default"})
        self.assertEqual(receipt["native_document"]["sha256"], hashlib.sha256(saved).hexdigest())

    def test_successful_save_with_inconsistent_native_evidence_remains_unknown(self):
        self.create()
        native = self.workspace / "submission.SLDPRT"
        self.authorize([("save_document", {"filepath": str(native)})])
        original = self.native.call_tool
        def corrupt_save(name, arguments):
            reply = original(name, arguments)
            payload = json.loads(reply["content"][0]["text"])
            payload["data"]["output_sha256"] = "0" * 64
            return result(payload)
        with patch.object(self.native, "call_tool", side_effect=corrupt_save):
            with self.assertRaisesRegex(RuntimeError, "cad_task_unknown"):
                self.call("save_document", {"filepath": str(native), "parameter_plan_token": "issued-native-token"})
        state = json.loads((self.audit / "gateway-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["pending_cad"], 1)
        self.assertFalse(state.get("native_save_evidence"))

    def test_display_save_warnings_are_recorded_and_allow_submission(self):
        self.native.save_warning_code = 64 | 128 | 256
        self.create()
        native = self.workspace / "submission.SLDPRT"
        self.authorize([("save_document", {"filepath": str(native)})])
        self.call("save_document", {"filepath": str(native), "parameter_plan_token": "issued-native-token"})
        state = json.loads((self.audit / "gateway-state.json").read_text(encoding="utf-8"))
        warnings = state["native_save_evidence"]["save_warnings"]
        self.assertEqual(warnings["status"], "allowed")
        self.assertEqual(warnings["raw"], 448)
        self.assertEqual({flag["bit"] for flag in warnings["flags"]}, {64, 128, 256})
        self.assertFalse(warnings["geometry_verified_by_warning_policy"])
        self.assertEqual(state["pending_cad"], 0)
        self.assertEqual(state["parameter_plan"]["next_index"], 1)
        self.call("close_document", {"document_title": native.name, "expected_path": str(native), "save": False})
        self.gateway().call("write_report", {"report": {}})
        self.assertEqual(self.gateway().call("submit", {"configuration": "Default"})["status"], "submitted")

    def test_rebuild_save_warning_completes_native_operation_but_blocks_submission(self):
        self.native.save_warning_code = 1 | 64
        self.create()
        native = self.workspace / "submission.SLDPRT"
        self.authorize([("save_document", {"filepath": str(native)})])
        self.call("save_document", {"filepath": str(native), "parameter_plan_token": "issued-native-token"})
        state = json.loads((self.audit / "gateway-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["native_save_evidence"]["save_warnings"]["status"], "blocked")
        self.assertEqual(state["pending_cad"], 0)
        self.assertEqual(state["parameter_plan"]["next_index"], 1)
        events = [json.loads(line) for line in (self.audit / "gateway-events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row for row in events if row["type"] == "cad_result"][-1]["outcome"], "completed_success")
        self.call("close_document", {"document_title": native.name, "expected_path": str(native), "save": False})
        self.gateway().call("write_report", {"report": {}})
        with self.assertRaisesRegex(RuntimeError, "native_save_warning_blocked"):
            self.gateway().call("submit", {"configuration": "Default"})
        self.assertFalse((self.audit / "submission-receipt.json").exists())

    def test_corrected_resave_can_clear_known_save_warning(self):
        self.native.save_warning_code = 2
        self.create()
        native = self.workspace / "submission.SLDPRT"
        self.authorize([("save_document", {"filepath": str(native)}), ("draw_circle", {"radius": 3}),
                        ("save_document", {"filepath": str(native)})])
        save = {"filepath": str(native), "parameter_plan_token": "issued-native-token"}
        self.call("save_document", save)
        self.call("draw_circle", {"radius": 3, "parameter_plan_token": "issued-native-token"})
        self.native.save_warning_code = 0
        self.call("save_document", {**save, "confirm_overwrite": True})
        state = json.loads((self.audit / "gateway-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["native_save_evidence"]["save_warnings"]["status"], "clear")
        self.assertEqual(state["parameter_plan"]["next_index"], 3)
        self.call("close_document", {"document_title": native.name, "expected_path": str(native), "save": False})
        self.gateway().call("write_report", {"report": {}})
        self.assertEqual(self.gateway().call("submit", {"configuration": "Default"})["status"], "submitted")

    def test_unknown_save_warnings_preserve_pending_and_deny_restart_retry(self):
        # Context-dependent warning, unrecognized bit, and malformed native code.
        for code in (4, 4096, None, True, "64", -1):
            with self.subTest(code=code):
                self.setUp()
                self.create()
                native = self.workspace / "submission.SLDPRT"
                self.authorize([("save_document", {"filepath": str(native)})])
                self.native.save_warning_code = code
                with self.assertRaisesRegex(RuntimeError, "cad_task_unknown"):
                    self.call("save_document", {"filepath": str(native), "parameter_plan_token": "issued-native-token"})
                state = json.loads((self.audit / "gateway-state.json").read_text(encoding="utf-8"))
                self.assertEqual(state["pending_cad"], 1)
                self.assertEqual(state["native_save_evidence"]["save_warnings"]["status"], "unknown")
                self.assertEqual(state["native_save_evidence"]["save_warnings"]["raw"], code)
                self.assertEqual(state["parameter_plan"]["next_index"], 0)
                calls = len(self.native.calls)
                with self.assertRaisesRegex(RuntimeError, "cad_task_unknown"):
                    self.call("save_document", {"filepath": str(native), "parameter_plan_token": "issued-native-token"})
                with self.assertRaisesRegex(RuntimeError, "cad_task_unknown"):
                    self.gateway().call("submit", {"configuration": "Default"})
                self.assertEqual(len(self.native.calls), calls)

    def test_native_save_evidence_cannot_be_injected_into_state(self):
        self.gateway().call("write_report", {"report": {}})
        path = self.audit / "gateway-state.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        state["native_save_evidence"] = {"path": str(self.workspace / "submission.SLDPRT"), "sha256": "0" * 64}
        path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "audit_state_mismatch"):
            self.gateway().call("submit", {"configuration": "Default"})

    def test_saved_part_admission_does_not_guess_container_signature(self):
        self.native.native_bytes = b"\xa1\x02\x03\x04variable-native-container"
        self.create()
        native = self.workspace / "submission.SLDPRT"
        self.authorize([("save_document", {"filepath": str(native)})])
        self.call("save_document", {"filepath": str(native), "parameter_plan_token": "issued-native-token"})
        self.call("close_document", {"document_title": native.name, "expected_path": str(native), "save": False})
        self.gateway().call("write_report", {"report": {}})
        receipt = self.gateway().call("submit", {"configuration": "Default"})
        self.assertEqual(receipt["native_document"]["sha256"], hashlib.sha256(self.native.native_bytes).hexdigest())

    def test_new_fusion_contract_accepts_native_read_and_configuration_not_applicable(self):
        import zipfile
        self.contract["cad_backend"] = "fusion"
        self.native.backend = "fusion"
        self.native.tools = {"fusion_mcp_read": descriptor("fusion_mcp_read", {"code": {"type": "string"}}, ["code"])}
        self.native.reply = result({"activeCommand": ""})
        self.call("fusion_mcp_read", {"code": "read native state"})
        events = [json.loads(line) for line in (self.audit / "gateway-events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(next(row for row in events if row["type"] == "cad_result")["outcome"], "completed_success")
        with zipfile.ZipFile(self.workspace / "submission.f3d", "w") as archive:
            archive.writestr("synthetic", "fixture")
        self.gateway().call("write_report", {"report": {"status": "declared"}})
        receipt = self.gateway().call("submit", {})
        self.assertEqual(receipt["native_document"]["format"], "f3d")
        self.assertIsNone(receipt["native_document"]["configuration"])


if __name__ == "__main__":
    unittest.main()

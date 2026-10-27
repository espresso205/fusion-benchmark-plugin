import base64
import hashlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "case"
        self.workspace.mkdir()
        (self.workspace / "TASK.md").write_text("public task", encoding="utf-8")
        self.audit = self.root / "audit"
        self.audit.mkdir()
        self.contract = self.audit / "worker-contract.json"
        self.contract.write_text(json.dumps({"schema": "fusion-worker/1", "run_id": "r", "case_id": "Q01", "attempt_id": "a",
            "workspace": str(self.workspace), "audit_dir": str(self.audit), "max_tool_calls": 3, "deadline": time.time() + 60,
            "model": "m", "budget_scope": "mcp_tools_scope", "public_files": ["TASK.md"]}), encoding="utf-8")

    def gateway(self, upstream=None):
        from fusion_pipeline.worker_gateway import WorkerGateway
        return WorkerGateway(self.contract, upstream=upstream)

    def image_contract(self, max_tool_calls=30, drawings=0):
        from PIL import Image
        for filename, color in (("second.png", "blue"), ("first.png", "red")):
            Image.new("RGB", (7, 5), color).save(self.workspace / filename)

        def asset(identifier, role, filename, mime):
            data = (self.workspace / filename).read_bytes()
            return {"id": identifier, "role": role, "path": filename, "mime": mime,
                    "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "display_name": filename}

        manifest = {"schema": "fusion-public-inputs/1", "case_id": "Q01",
                    "task": asset("task", "task", "TASK.md", "text/markdown"),
                    "attachments": [asset("side", "view", "second.png", "image/png"),
                                    asset("front", "drawing", "first.png", "image/png")]}
        if drawings:
            import ezdxf
            for index in range(drawings):
                document = ezdxf.new()
                document.units = 4
                document.modelspace().add_line((0, 0), (index + 10, 0))
                filename = f"outline-{index}.dxf"
                document.saveas(self.workspace / filename)
                manifest["attachments"].append(asset(f"outline-{index}", "drawing", filename, "application/dxf"))
        contract = json.loads(self.contract.read_text())
        contract.update(input_manifest=manifest, max_tool_calls=max_tool_calls)
        self.contract.write_text(json.dumps(contract), encoding="utf-8")
        return manifest

    def test_manifest_contract_and_listing_preserve_attachment_order(self):
        manifest = self.image_contract()
        gateway = self.gateway()
        contract = gateway.call("get_contract", {})
        self.assertEqual(contract.get("input_manifest"), manifest)
        listing = gateway.call("list_inputs", {})
        self.assertEqual([item["id"] for item in listing["inputs"]], ["task", "side", "front"])
        self.assertEqual(gateway.call("read_input", {"name": "task"})["text"], "public task")
        self.assertEqual(gateway.call("read_input", {"name": "TASK.md"})["text"], "public task")

    def test_image_is_native_and_audit_has_identity_without_image_bytes(self):
        from PIL import Image
        from fusion_pipeline.service import RawToolResult
        manifest = self.image_contract()
        result = self.gateway().call("read_image", {"id": "front"})
        self.assertIsInstance(result, RawToolResult)
        image = next(item for item in result["content"] if item["type"] == "image")
        data = base64.b64decode(image["data"], validate=True)
        self.assertEqual(image["mimeType"], "image/png")
        with Image.open(io.BytesIO(data)) as decoded:
            self.assertEqual(decoded.size, (7, 5))
            self.assertEqual(decoded.getpixel((0, 0)), (255, 0, 0))
        raw = (self.audit / "gateway-events.jsonl").read_text()
        completed = [json.loads(line) for line in raw.splitlines() if json.loads(line)["type"] == "completed"][-1]
        self.assertEqual(completed["image"]["source_sha256"], manifest["attachments"][1]["sha256"])
        self.assertEqual(completed["image"]["returned_sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual((completed["image"]["id"], completed["image"]["index"]), ("front", 1))
        self.assertEqual((completed["image"]["width"], completed["image"]["height"], completed["image"]["transform"]), (7, 5, "none"))
        self.assertNotIn(image["data"], raw)

    def test_read_input_refuses_image_ids_and_paths(self):
        self.image_contract()
        gateway = self.gateway()
        for name in ("front", "first.png"):
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "image_requires_read_image"):
                gateway.call("read_input", {"name": name})

    def test_manifest_reads_recheck_all_assets_after_gateway_restart(self):
        self.image_contract()
        self.gateway().call("read_image", {"id": "front"})
        (self.workspace / "second.png").write_bytes(b"changed")
        for tool, arguments in (("get_contract", {}), ("list_inputs", {}), ("read_image", {"id": "front"}),
                                ("read_input", {"name": "TASK.md"})):
            with self.subTest(tool=tool), self.assertRaisesRegex(RuntimeError, "input_integrity_failed"):
                self.gateway().call(tool, arguments)

    def test_submit_rechecks_inputs_before_receipt_or_revocation(self):
        self.image_contract()
        gateway = self.gateway()
        (self.workspace / "submission.f3d").write_bytes(b"synthetic-f3d")
        gateway.call("write_report", {"report": {"status": "declared"}})
        (self.workspace / "first.png").write_bytes(b"changed")
        with self.assertRaisesRegex(RuntimeError, "input_integrity_failed"):
            gateway.call("submit", {})
        self.assertFalse((self.audit / "submission-receipt.json").exists())
        self.assertFalse(json.loads((self.audit / "gateway-state.json").read_text())["submitted"])

    def test_image_reads_obey_persistent_budget(self):
        self.image_contract(max_tool_calls=1)
        self.gateway().call("read_image", {"id": "front"})
        with self.assertRaisesRegex(RuntimeError, "tool_budget_exceeded"):
            self.gateway().call("read_image", {"id": "side"})

    def test_image_reads_obey_cancellation(self):
        self.image_contract()
        contract = json.loads(self.contract.read_text())
        cancel = self.audit / "cancel.request"
        contract["cancel_file"] = str(cancel)
        self.contract.write_text(json.dumps(contract))
        gateway = self.gateway()
        cancel.touch()
        with self.assertRaisesRegex(RuntimeError, "attempt_cancelled"):
            gateway.call("read_image", {"id": "front"})

    def test_images_are_revoked_after_submit(self):
        self.image_contract()
        gateway = self.gateway()
        (self.workspace / "submission.f3d").write_bytes(b"synthetic-f3d")
        gateway.call("write_report", {"report": {"status": "declared"}})
        gateway.call("submit", {})
        for tool in ("read_image", "list_inputs"):
            with self.subTest(tool=tool), self.assertRaisesRegex(RuntimeError, "attempt_submitted"):
                self.gateway().call(tool, {"id": "front"} if tool == "read_image" else {})

    def test_parse_dxf_selects_declared_id_and_requires_it_when_ambiguous(self):
        self.image_contract(drawings=2)
        gateway = self.gateway()
        with self.assertRaisesRegex(RuntimeError, "dxf_id_required"):
            gateway.call("parse_dxf", {})
        parsed = gateway.call("parse_dxf", {"id": "outline-1"})
        self.assertEqual(parsed["units"], 4)
        self.assertEqual(parsed["entities"][0]["attributes"]["end"], "(11.0, 0.0, 0.0)")
        with self.assertRaisesRegex(RuntimeError, "input_not_dxf"):
            gateway.call("parse_dxf", {"id": "front"})

    def test_parse_dxf_uses_single_declared_drawing_without_id(self):
        self.image_contract(drawings=1)
        parsed = self.gateway().call("parse_dxf", {})
        self.assertEqual(parsed["entities"][0]["type"], "LINE")

    def test_vision_check_refuses_fusion_and_writes_at_call_boundary(self):
        contract = json.loads(self.contract.read_text())
        contract.update(vision_only=True, max_tool_calls=10)
        self.contract.write_text(json.dumps(contract))
        gateway = self.gateway()
        for tool, arguments in (("fusion_tools", {}), ("fusion_call", {"tool": "fusion_mcp_read", "arguments": {}}),
                                ("write_report", {"report": {"status": "declared"}}), ("submit", {})):
            with self.subTest(tool=tool), self.assertRaisesRegex(RuntimeError, "vision_check_read_only"):
                gateway.call(tool, arguments)
        self.assertEqual(gateway.call("read_input", {"name": "TASK.md"})["text"], "public task")
        self.assertFalse((self.workspace / "worker_report.json").exists())
        state = json.loads((self.audit / "gateway-state.json").read_text())
        self.assertEqual(state["fusion_calls"], 0)
        self.assertFalse(state["submitted"])

    def test_restart_cannot_reset_budget(self):
        gateway = self.gateway()
        gateway.call("get_contract", {})
        gateway.call("get_contract", {})
        gateway = self.gateway()
        gateway.call("get_contract", {})
        with self.assertRaises(RuntimeError):
            gateway.call("get_contract", {})

    def test_missing_state_cannot_reset_existing_journal(self):
        gateway = self.gateway()
        gateway.call("get_contract", {})
        (self.audit / "gateway-state.json").unlink()
        with self.assertRaises(RuntimeError):
            self.gateway().call("get_contract", {})

    def test_unknown_fusion_reply_blocks_next_request_after_restart(self):
        class Upstream:
            tools = {"fusion_mcp_read": {}}
            def rpc(self, *args, **kwargs):
                raise RuntimeError("transport unknown")
        gateway = self.gateway(Upstream())
        with self.assertRaises(RuntimeError):
            gateway.call("fusion_call", {"tool": "fusion_mcp_read", "arguments": {}})
        self.assertEqual(json.loads((self.audit / "gateway-state.json").read_text())["pending_fusion"], 1)
        with self.assertRaises(RuntimeError):
            self.gateway(Upstream()).call("fusion_call", {"tool": "fusion_mcp_read", "arguments": {}})

    def test_canceled_or_unknown_error_never_releases_pending_fusion(self):
        messages = ["Request canceled while executing", "Request CANCELLED", "TIMED OUT", "timeout", "Unknown status", "unknown execution"]
        for index, message in enumerate(messages):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                audit = Path(directory)
                contract = json.loads(self.contract.read_text())
                contract["audit_dir"] = str(audit)
                path = audit / "contract.json"
                path.write_text(json.dumps(contract))
                class Upstream:
                    tools = {"fusion_mcp_execute": {}}
                    count = 0
                    def rpc(self, *args, **kwargs):
                        self.count += 1
                        return {"isError": True, "content": [{"type": "text", "text": message}]}
                from fusion_pipeline.worker_gateway import WorkerGateway
                upstream = Upstream()
                gateway = WorkerGateway(path, upstream=upstream)
                with self.assertRaises(RuntimeError):
                    gateway.call("fusion_call", {"tool": "fusion_mcp_execute", "arguments": {}})
                with self.assertRaises(RuntimeError):
                    WorkerGateway(path, upstream=upstream).call("fusion_call", {"tool": "fusion_mcp_execute", "arguments": {}})
                self.assertEqual(upstream.count, 1)
                self.assertEqual(json.loads((audit / "gateway-state.json").read_text())["pending_fusion"], 1)

    def test_read_input_cannot_read_private_or_escape(self):
        gateway = self.gateway()
        (self.root / "secret.txt").write_text("PRIVATE")
        for filename in ("../secret.txt", str(self.root / "secret.txt")):
            with self.assertRaises(RuntimeError):
                gateway.call("read_input", {"name": filename})

    def test_submit_freezes_hash_and_revokes_calls(self):
        from fusion_pipeline.worker_gateway import WorkerGateway
        contract = json.loads(self.contract.read_text())
        contract["max_tool_calls"] = 10
        self.contract.write_text(json.dumps(contract))
        gateway = self.gateway()
        (self.workspace / "submission.f3d").write_bytes(b"synthetic-f3d")
        gateway.call("write_report", {"report": {"status": "declared"}})
        receipt = gateway.call("submit", {})
        self.assertEqual(receipt["status"], "submitted")
        self.assertEqual(len(receipt["f3d_sha256"]), 64)
        with self.assertRaises(RuntimeError):
            gateway.call("read_input", {"name": "TASK.md"})


if __name__ == "__main__":
    unittest.main()

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

"""Attempt-bound MCP tools with durable budgets and fail-closed Fusion receipts.

This is a trusted same-account boundary, not an OS sandbox. The coordinator's
batch lock owns the single Fusion desktop for the entire attempt and export.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

from .service import tool, RawToolResult

FUSION_TOOLS = {"fusion_mcp_read", "fusion_mcp_execute", "fusion_mcp_update", "fusion_mcp_electronics_read"}


class GatewayError(RuntimeError):
    def __init__(self, code):
        self.public_code = code
        super().__init__(code)


class WorkerGateway:
    name = "fusion-benchmark-worker"
    instructions = ("Only this attempt's public TASK and DXF are available. Call get_contract then read_input/parse_dxf. "
                    "Use fusion_tools to inspect Fusion tool schemas and fusion_call to operate your own documents. "
                    "Export submission.f3d, close your own documents, write_report and submit. No reference or scoring access.")
    tools = [
        tool("get_contract", "Read this attempt's public contract and remaining budget.", readonly=True),
        tool("read_input", "Read an allowed public input or Fusion skill text.", {"name": {"type": "string"}}, ("name",), True),
        tool("parse_dxf", "Parse public drawing.dxf with ezdxf; no shell or arbitrary code.", readonly=True),
        tool("fusion_tools", "Read the four allowed Fusion tools' argument schemas.", readonly=True),
        tool("fusion_call", "Execute an allowed Fusion MCP tool; uncertain results block further Fusion calls.",
             {"tool": {"type": "string", "enum": sorted(FUSION_TOOLS)}, "arguments": {"type": "object"}}, ("tool", "arguments")),
        tool("write_report", "Write this attempt's worker_report.json.", {"report": {"type": "object"}}, ("report",)),
        tool("submit", "Freeze submission.f3d and report; revoke all further worker tools."),
    ]

    def __init__(self, contract_path, upstream=None):
        self.path = Path(contract_path).resolve()
        raw = self.path.read_bytes()
        self.contract = json.loads(raw)
        self.contract_hash = hashlib.sha256(raw).hexdigest()
        self.workspace = Path(self.contract["workspace"]).resolve()
        self.audit = Path(self.contract["audit_dir"]).resolve()
        self.state_path = self.audit / "gateway-state.json"
        self.lock_path = self.audit / "gateway.lock"
        self.upstream = upstream
        if self.contract.get("budget_scope") != "mcp_tools_scope":
            raise GatewayError("invalid_budget_scope")

    def _upstream(self):
        if self.upstream is None:
            from .mcp import FusionMCP
            connection = json.loads(Path(self.contract["connection_file"]).read_text(encoding="utf-8"))
            self.upstream = FusionMCP(connection, timeout=min(180, max(1, self.contract["deadline"] - time.time())))
        return self.upstream

    def _state(self):
        journal = self.audit / "gateway-events.jsonl"
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if state.get("contract_sha256") != self.contract_hash:
                raise GatewayError("contract_changed")
            if not journal.exists():
                raise GatewayError("audit_missing")
            previous, sequence, calls = None, 0, 0
            raw = journal.read_bytes()
            if raw and not raw.endswith(b"\n"):
                raise GatewayError("audit_corrupt")
            for line in raw.splitlines():
                event = json.loads(line)
                digest = event.pop("sha256")
                sequence += 1
                if event.get("sequence") != sequence or event.get("previous_sha256") != previous or digest != hashlib.sha256(
                        json.dumps(event, sort_keys=True, separators=(",", ":")).encode()).hexdigest():
                    raise GatewayError("audit_corrupt")
                previous = digest
                calls += event.get("type") == "request"
            if state.get("sequence") != sequence or state.get("last_hash") != previous or state.get("calls") != calls:
                raise GatewayError("audit_state_mismatch")
            return state
        if journal.exists():
            raise GatewayError("audit_state_missing")
        return {"schema": "fusion-gateway/1", "contract_sha256": self.contract_hash, "calls": 0, "fusion_calls": 0,
                "pending_fusion": 0, "submitted": False, "last_hash": None, "sequence": 0}

    def _save(self, state, event):
        from .runner import atomic
        event = {"sequence": state["sequence"] + 1, "previous_sha256": state["last_hash"], "at": time.time(), **event}
        event_hash = hashlib.sha256(json.dumps(event, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with (self.audit / "gateway-events.jsonl").open("ab") as stream:
            stream.write((json.dumps({**event, "sha256": event_hash}) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        state.update(sequence=event["sequence"], last_hash=event_hash)
        atomic(self.state_path, state)

    def call(self, name, arguments):
        try:
            descriptor = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise GatewayError("gateway_busy_or_unreconciled") from None
        try:
            state = self._state()
            if state["submitted"]:
                raise GatewayError("attempt_submitted")
            if self.contract.get("cancel_file") and Path(self.contract["cancel_file"]).exists():
                raise GatewayError("attempt_cancelled")
            if time.time() >= self.contract["deadline"]:
                raise GatewayError("time_budget_exceeded")
            state["calls"] += 1
            self._save(state, {"type": "request", "tool": name,
                               "args_sha256": hashlib.sha256(json.dumps(arguments, sort_keys=True).encode()).hexdigest()})
            if state["calls"] > self.contract["max_tool_calls"]:
                raise GatewayError("tool_budget_exceeded")
            if name == "fusion_call":
                if state["pending_fusion"]:
                    raise GatewayError("fusion_task_unknown")
                if arguments.get("tool") not in FUSION_TOOLS or not isinstance(arguments.get("arguments"), dict):
                    raise GatewayError("fusion_tool_denied")
                if self.contract.get("smoke") and arguments["tool"] != "fusion_mcp_read":
                    raise GatewayError("smoke_read_only")
                state["fusion_calls"] += 1
                state["pending_fusion"] = 1
                self._save(state, {"type": "fusion_started"})
                try:
                    result = self._upstream().rpc("tools/call", {"name": arguments["tool"], "arguments": arguments["arguments"]})
                except Exception:
                    self._save(state, {"type": "fusion_unknown"})
                    raise GatewayError("fusion_task_unknown") from None
                text = json.dumps(result, ensure_ascii=False)
                if not isinstance(result, dict) or not isinstance(result.get("content"), list) or (
                        result.get("isError") and re.search(r"timed?\s*out|timeout|cancelled|canceled|unknown(?:\s+\w+){0,2}\s+(?:execution|status|state)|unknown execution", text, re.IGNORECASE)):
                    self._save(state, {"type": "fusion_unknown"})
                    raise GatewayError("fusion_task_unknown")
                state["pending_fusion"] = 0
                self._save(state, {"type": "fusion_result", "is_error": result.get("isError", False),
                                   "result_sha256": hashlib.sha256(text.encode()).hexdigest()})
                return RawToolResult(result)
            result = self._local(name, arguments, state)
            self._save(state, {"type": "completed", "tool": name})
            return result
        finally:
            os.close(descriptor)
            self.lock_path.unlink()

    def _public(self, name):
        if name == "fusion_skill" and self.contract.get("fusion_skill"):
            return Path(self.contract["fusion_skill"])
        if name not in self.contract.get("public_files", []):
            raise GatewayError("input_denied")
        path = (self.workspace / name).resolve()
        if not path.is_relative_to(self.workspace) or path.is_symlink():
            raise GatewayError("input_denied")
        return path

    def _local(self, name, arguments, state):
        if name == "get_contract":
            return {key: self.contract[key] for key in ("run_id", "case_id", "attempt_id", "workspace", "model", "deadline", "budget_scope", "max_tool_calls")
                    if key in self.contract} | {"remaining_calls": max(0, self.contract["max_tool_calls"] - state["calls"]),
                                               "public_files": self.contract.get("public_files", []), "isolation": "trusted_same_account"}
        if name == "read_input":
            path = self._public(arguments["name"])
            if path.stat().st_size > 8 * 1024 * 1024:
                raise GatewayError("input_too_large_use_parse_dxf")
            return {"name": arguments["name"], "text": path.read_text(encoding="utf-8", errors="replace"),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        if name == "parse_dxf":
            import ezdxf
            document = ezdxf.readfile(self._public("drawing.dxf"))
            entities = []
            for entity in document.modelspace():
                record = {"type": entity.dxftype(), "attributes": entity.dxf.all_existing_dxf_attribs()}
                if entity.dxftype() == "LWPOLYLINE":
                    record["points"] = list(entity.get_points())
                entities.append(record)
                if len(entities) >= 10000:
                    break
            return json.loads(json.dumps({"units": document.units, "entities": entities, "truncated": len(entities) >= 10000}, default=str))
        if name == "fusion_tools":
            return {"tools": [definition for name, definition in self._upstream().tools.items() if name in FUSION_TOOLS]}
        if name == "write_report":
            from .runner import atomic
            if not isinstance(arguments.get("report"), dict):
                raise GatewayError("invalid_report")
            atomic(self.workspace / "worker_report.json", arguments["report"])
            return {"written": "worker_report.json"}
        if name == "submit":
            if state["pending_fusion"]:
                raise GatewayError("fusion_task_unknown")
            from .runner import atomic
            f3d, report = self.workspace / "submission.f3d", self.workspace / "worker_report.json"
            if any(p.is_symlink() or not p.is_file() or not p.stat().st_size for p in (f3d, report)):
                raise GatewayError("submission_missing")
            receipt = {"status": "submitted", "f3d_sha256": hashlib.sha256(f3d.read_bytes()).hexdigest(),
                       "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(), "case_id": self.contract["case_id"]}
            atomic(self.audit / "submission-receipt.json", receipt)
            state["submitted"] = True
            return receipt
        raise GatewayError("unknown_worker_tool")

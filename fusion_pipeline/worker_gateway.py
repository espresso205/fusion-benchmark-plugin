"""Attempt-bound MCP tools with durable budgets and fail-closed Fusion receipts.

This is a trusted same-account boundary, not an OS sandbox. The coordinator's
batch lock owns the single Fusion desktop for the entire attempt and export.
"""
import hashlib
import io
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
    instructions = ("Only this attempt's declared public inputs are available. Call get_contract and list_inputs, "
                    "then read_input for text, read_image for each image ID, and parse_dxf for DXF attachments. "
                    "Read attachments in their declared order. Engineering annotations determine dimensions; pixels are not millimeters. "
                    "Use fusion_tools to inspect Fusion tool schemas and fusion_call to operate your own documents. "
                    "Export submission.f3d, close your own documents, write_report and submit. No reference or scoring access.")
    tools = [
        tool("get_contract", "Read this attempt's public contract and remaining budget.", readonly=True),
        tool("list_inputs", "List the task and public attachments in their declared order.", readonly=True),
        tool("read_input", "Read public text by input ID or declared relative path, or fusion_skill. Images require read_image.",
             {"name": {"type": "string"}}, ("name",), True),
        tool("read_image", "Read a declared image by input ID as native MCP image content.",
             {"id": {"type": "string"}}, ("id",), True),
        tool("parse_dxf", "Parse a declared DXF by input ID. ID is optional when there is one DXF. No shell or arbitrary code.",
             {"id": {"type": "string"}}, readonly=True),
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
        self.input_root = Path(self.contract["workspace"])
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
            if self.contract.get("vision_only") and name not in {
                    "get_contract", "list_inputs", "read_input", "read_image", "parse_dxf"}:
                raise GatewayError("vision_check_read_only")
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
            event = {"type": "completed", "tool": name}
            if name == "read_image":
                metadata = result["structuredContent"]
                event["image"] = {key: metadata[key] for key in (
                    "id", "source_sha256", "returned_sha256", "width", "height", "transform", "mime", "source_mime", "index")}
            self._save(state, event)
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

    def _verify_inputs(self):
        if "input_manifest" in self.contract:
            from .input_assets import validate_manifest, verify_manifest
            try:
                validate_manifest(self.contract["input_manifest"], case_id=self.contract["case_id"])
                verify_manifest(self.contract["input_manifest"], self.input_root)
            except (ValueError, OSError):
                raise GatewayError("input_integrity_failed") from None

    def _input_descriptor(self, identifier, allow_path=False):
        from .input_assets import iter_assets
        for asset in iter_assets(self.contract["input_manifest"]):
            if asset["id"] == identifier or allow_path and asset["path"] == identifier:
                return asset
        raise GatewayError("input_denied")

    def _read_asset(self, identifier):
        from .input_assets import read_asset
        try:
            return read_asset(self.contract["input_manifest"], self.input_root, identifier)
        except (ValueError, OSError):
            raise GatewayError("input_integrity_failed") from None

    def _local(self, name, arguments, state):
        if name in {"get_contract", "list_inputs", "read_input", "read_image", "parse_dxf", "submit"}:
            self._verify_inputs()
        if name == "get_contract":
            return {key: self.contract[key] for key in ("run_id", "case_id", "attempt_id", "workspace", "model", "deadline", "budget_scope", "max_tool_calls", "input_manifest")
                    if key in self.contract} | {"remaining_calls": max(0, self.contract["max_tool_calls"] - state["calls"]),
                                               "public_files": self.contract.get("public_files", []), "isolation": "trusted_same_account"}
        if name == "list_inputs":
            if "input_manifest" in self.contract:
                from .input_assets import iter_assets
                manifest = self.contract["input_manifest"]
                return {"schema": manifest["schema"], "case_id": manifest["case_id"], "inputs": list(iter_assets(manifest))}
            inputs = []
            for filename in self.contract.get("public_files", []):
                data = self._public(filename).read_bytes()
                inputs.append({"id": "task" if filename == "TASK.md" else filename, "path": filename,
                               "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
            return {"case_id": self.contract["case_id"], "inputs": inputs}
        if name == "read_input":
            if "input_manifest" in self.contract and arguments["name"] != "fusion_skill":
                asset = self._input_descriptor(arguments["name"], allow_path=True)
                if asset["mime"].startswith("image/"):
                    raise GatewayError("image_requires_read_image")
                _, data = self._read_asset(asset["id"])
            else:
                path = self._public(arguments["name"])
                if path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
                    raise GatewayError("image_requires_read_image")
                if path.stat().st_size > 8 * 1024 * 1024:
                    raise GatewayError("input_too_large_use_parse_dxf")
                data = path.read_bytes()
            if len(data) > 8 * 1024 * 1024:
                raise GatewayError("input_too_large_use_parse_dxf")
            return {"name": arguments["name"], "text": data.decode("utf-8", errors="replace"),
                    "sha256": hashlib.sha256(data).hexdigest()}
        if name == "read_image":
            if "input_manifest" not in self.contract:
                raise GatewayError("input_denied")
            asset = self._input_descriptor(arguments["id"])
            if not asset["mime"].startswith("image/"):
                raise GatewayError("input_not_image")
            from .input_assets import image_result
            try:
                return RawToolResult(image_result(self.contract["input_manifest"], self.input_root, asset["id"]))
            except (ValueError, OSError):
                raise GatewayError("input_integrity_failed") from None
        if name == "parse_dxf":
            import ezdxf
            if "input_manifest" in self.contract:
                if "id" in arguments:
                    asset = self._input_descriptor(arguments["id"])
                else:
                    from .input_assets import iter_assets
                    drawings = [asset for asset in iter_assets(self.contract["input_manifest"]) if asset["mime"] == "application/dxf"]
                    if len(drawings) != 1:
                        raise GatewayError("dxf_id_required" if drawings else "input_not_dxf")
                    asset = drawings[0]
                if asset["mime"] != "application/dxf":
                    raise GatewayError("input_not_dxf")
                _, data = self._read_asset(asset["id"])
                if data.startswith(b"AutoCAD Binary DXF\r\n\x1a\x00"):
                    from ezdxf.document import Drawing
                    from ezdxf.lldxf.tagger import binary_tags_loader
                    document = Drawing.load(binary_tags_loader(data))
                else:
                    from ezdxf.filemanagement import dxf_stream_info
                    info = dxf_stream_info(io.StringIO(data.decode("utf-8", errors="ignore"), newline=None))
                    document = ezdxf.read(io.StringIO(data.decode(info.encoding, errors="surrogateescape"), newline=None))
            else:
                if arguments.get("id") not in (None, "drawing", "drawing.dxf"):
                    raise GatewayError("input_denied")
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

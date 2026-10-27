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
import stat
import time
import uuid

from .service import tool, RawToolResult

FUSION_TOOLS = {"fusion_mcp_read", "fusion_mcp_execute", "fusion_mcp_update", "fusion_mcp_electronics_read"}
IDENTITY_KEYS = ("run_id", "case_id", "attempt_id", "protocol_id", "cad_backend", "cad_config_sha256")
# This is a maintained policy, not a name-prefix filter. Discovery can only
# reduce this surface. Imports, assemblies, arbitrary code, workflows, global
# preferences and candidate STEP exports are intentionally absent.
SOLIDWORKS_TOOLS = frozenset("""
create_new_part new_part create_sketch create_sketch_on_plane create_sketch_on_face
close_sketch draw_line draw_centerline draw_rectangle draw_circle draw_arc draw_polygon
draw_spline draw_polycurve create_center_rectangle create_circle add_dimension add_relation
extrude_sketch extrude_boss cut_extrude revolve sweep loft fillet_edges chamfer_edges
shell draft linear_pattern mirror create_axis create_reference_plane create_thread
surface_loft offset_surface knit_surfaces thicken_surface cut_with_surface
select_by_name select_by_ray get_selection find_entities_near_point
get_document_info list_open_documents get_solidworks_info solidworks_status active_document
get_feature_tree get_feature_definition list_features get_sketch_status inspect_sketch_profile
get_bounding_box get_mass_properties measure_distance measure_wall_thickness
inspect_model_geometry inspect_active_part inspect_body_continuity inspect_loft_profiles
inspect_sheet_edges inspect_sheet_faces list_configurations list_dimensions list_editable_parameters
list_features_with_dimensions find_dimension_by_hint get_dimension_value set_dimension_value
rebuild_active_document review_model_health diagnose_active_document_health validate_geometry_contract
validate_parameter_plan save_document close_document take_screenshot
""".split())
CREATE_TOOLS = {"create_new_part", "new_part"}
GENERAL_READ_TOOLS = {"list_open_documents", "get_solidworks_info", "solidworks_status", "active_document"}
PARAMETER_GATED_TOOLS = frozenset("""
create_new_part new_part create_sketch create_sketch_on_plane create_sketch_on_face close_sketch
draw_line draw_centerline draw_rectangle draw_circle draw_arc draw_polygon draw_spline draw_polycurve
create_center_rectangle create_circle add_dimension add_relation extrude_sketch extrude_boss cut_extrude
revolve sweep loft fillet_edges chamfer_edges shell draft linear_pattern mirror create_axis
create_reference_plane create_thread surface_loft offset_surface knit_surfaces thicken_surface
cut_with_surface set_dimension_value rebuild_active_document save_document
""".split())


def _json_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _business_objects(result):
    """Read structured business payloads, including JSON in native MCP text."""
    if isinstance(result, dict):
        yield result
        for key, value in result.items():
            if key == "text" and isinstance(value, str):
                try:
                    value = json.loads(value)
                except (ValueError, TypeError):
                    continue
            if isinstance(value, (dict, list)):
                yield from _business_objects(value)
    elif isinstance(result, list):
        for value in result:
            yield from _business_objects(value)


def _outcome(result, backend):
    if not isinstance(result, dict) or not isinstance(result.get("content"), list):
        return "unknown"
    objects = list(_business_objects(result))
    statuses = {str(item.get("status", "")).lower() for item in objects}
    uncertain = {"unknown", "timeout", "timed_out", "cancelled", "canceled", "running", "pending", "partial"}
    if statuses & uncertain or any(item.get("partial_mutation_possible") is True or
            item.get("execution_status") == "unknown" or item.get("outcome") == "unknown" for item in objects):
        return "unknown"
    text = json.dumps(result, ensure_ascii=False)
    if result.get("isError") and re.search(r"timed?\s*out|timeout|cancelled|canceled|unknown", text, re.I):
        return "unknown"
    if backend == "solidworks":
        # The adapter owns the vendor's completion and contradictory-envelope
        # semantics. Apply it here too for injected transports and future callers.
        from .solidworks import classify_tool_result
        return classify_tool_result(result)
    failures = result.get("isError") is True or any(item.get("ok") is False or item.get("success") is False
            or item.get("ready") is False for item in objects) or bool(statuses & {"failed", "failure", "error"})
    if failures:
        return "completed_failure"
    if backend == "fusion" or any(item.get("ok") is True or item.get("success") is True for item in objects):
        return "completed_success"
    return "unknown"


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
        self.generic = self.contract.get("schema") == "cad-worker/1"
        self.backend = self.contract.get("cad_backend", "fusion")
        if self.contract.get("schema") not in {"fusion-worker/1", "cad-worker/1"}:
            raise GatewayError("worker_schema_invalid")
        if self.generic:
            if self.backend not in {"fusion", "solidworks"} or any(not isinstance(self.contract.get(key), str)
                    or not self.contract[key] for key in IDENTITY_KEYS):
                raise GatewayError("cad_identity_invalid")
            if not re.fullmatch(r"[0-9a-f]{64}", self.contract["cad_config_sha256"]):
                raise GatewayError("cad_identity_invalid")
            self.tools = [entry for entry in self.tools if entry["name"] not in {"fusion_tools", "fusion_call", "submit"}]
            self.tools += [tool("cad_tools", "Discover this CAD backend's allowed native tool schemas.", readonly=True),
                          tool("cad_call", "Call one discovered native CAD tool. Unknown execution blocks further CAD operations.",
                               {"tool": {"type": "string"}, "arguments": {"type": "object"}}, ("tool", "arguments")),
                          tool("submit", "Freeze the native document and report, then revoke worker tools.",
                               {"configuration": {"type": "string", "minLength": 1}} if self.backend == "solidworks" else {})]
            self.instructions = ("Read get_contract and list_inputs, then all public attachments in order and cad_skill. "
                "Use cad_tools and cad_call exclusively for native modeling. Work only on this attempt's own Part. "
                "Save the declared native filename, close your own document, write_report, then submit the explicit "
                "SolidWorks configuration. Unknown native execution must not be retried. No reference or scoring access.")
        if self.contract.get("budget_scope") != "mcp_tools_scope":
            raise GatewayError("invalid_budget_scope")

    def _upstream(self):
        if self.upstream is None:
            if self.generic:
                from .cad import create_adapter, normalize_cad_config
                config = json.loads(Path(self.contract["connection_file"]).read_text(encoding="utf-8"))
                normalized = normalize_cad_config(config)
                if normalized["cad_backend"] != self.backend or normalized["cad_config_sha256"] != self.contract["cad_config_sha256"]:
                    raise GatewayError("cad_config_changed")
                self.upstream = create_adapter(config, workspace=self.workspace, audit_dir=self.audit / "cad",
                                               deadline=self.contract["deadline"])
                self.upstream.connect(launch_if_needed=False)
                return self.upstream
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
            if self.generic and (not sequence or event.get("state_sha256") != _json_hash({
                    key: value for key, value in state.items() if key not in {"sequence", "last_hash"}})):
                raise GatewayError("audit_state_mismatch")
            return state
        if journal.exists():
            raise GatewayError("audit_state_missing")
        if self.generic:
            return {"schema": "cad-gateway/1", "contract_sha256": self.contract_hash, "calls": 0, "cad_calls": 0,
                    "maintenance_probe_calls": 0,
                    "pending_cad": 0, "submitted": False, "last_hash": None, "sequence": 0,
                    "owned_documents": [], "saved_paths": [], "native_save_evidence": None, "cad_instance": None}
        return {"schema": "fusion-gateway/1", "contract_sha256": self.contract_hash, "calls": 0, "fusion_calls": 0,
                "pending_fusion": 0, "submitted": False, "last_hash": None, "sequence": 0}

    def _save(self, state, event):
        from .runner import atomic
        if self.generic:
            event = {**event, **{key: self.contract[key] for key in IDENTITY_KEYS},
                     "state_sha256": _json_hash({key: value for key, value in state.items()
                                                if key not in {"sequence", "last_hash"}})}
        event = {"sequence": state["sequence"] + 1, "previous_sha256": state["last_hash"], "at": time.time(), **event}
        event_hash = hashlib.sha256(json.dumps(event, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with (self.audit / "gateway-events.jsonl").open("ab") as stream:
            stream.write((json.dumps({**event, "sha256": event_hash}) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        state.update(sequence=event["sequence"], last_hash=event_hash)
        atomic(self.state_path, state)

    def call(self, name, arguments):
        if self.generic:
            return self._call_cad_contract(name, arguments)
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

    def _call_cad_contract(self, name, arguments):
        try:
            descriptor = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise GatewayError("gateway_busy_or_unreconciled") from None
        state, request = None, None
        try:
            state = self._state()
            if state["submitted"]:
                raise GatewayError("attempt_submitted")
            if self.contract.get("cancel_file") and Path(self.contract["cancel_file"]).exists():
                raise GatewayError("attempt_cancelled")
            if time.time() >= self.contract["deadline"]:
                raise GatewayError("time_budget_exceeded")
            try:
                digest = _json_hash(arguments)
            except (TypeError, ValueError):
                raise GatewayError("cad_arguments_invalid") from None
            request = {"tool": name, "request_id": uuid.uuid4().hex, "args_sha256": digest}
            state["calls"] += 1
            self._save(state, {"type": "request", **request})
            if state["calls"] > self.contract["max_tool_calls"]:
                raise GatewayError("tool_budget_exceeded")
            definition = next((entry for entry in self.tools if entry["name"] == name), None)
            if definition is None:
                raise GatewayError("unknown_worker_tool")
            self._validate_arguments(arguments, definition["inputSchema"])
            if self.contract.get("vision_only") and name not in {
                    "get_contract", "list_inputs", "read_input", "read_image", "parse_dxf"}:
                raise GatewayError("vision_check_read_only")
            if name == "cad_call":
                return self._cad_call(arguments, state, request)
            if name == "cad_tools":
                result = {"cad_backend": self.backend, "tools": list(self._catalog().values())}
            elif name == "submit":
                self._verify_inputs()
                result = self._submit_native(arguments, state)
            else:
                if name == "write_report":
                    self._guard_workspace_file(self.workspace / "worker_report.json")
                result = self._local(name, arguments, state)
                if name == "get_contract":
                    from .native_contract import submission_spec
                    result.update({key: self.contract[key] for key in IDENTITY_KEYS})
                    result.update(native_submission=submission_spec(self.backend), contract_sha256=self.contract_hash,
                                  modeling_authorization=self.contract.get("modeling_authorization", {}),
                                  remaining_cad_calls=max(0, self.contract.get("max_cad_tool_calls", self.contract["max_tool_calls"]) - state["cad_calls"]))
            event = {"type": "completed", **request}
            if name == "read_image":
                metadata = result["structuredContent"]
                event["image"] = {key: metadata[key] for key in (
                    "id", "source_sha256", "returned_sha256", "width", "height", "transform", "mime", "source_mime", "index")}
            self._save(state, event)
            return result
        except GatewayError as error:
            if state is not None and request is not None:
                self._save(state, {"type": "rejected", **request, "code": error.public_code})
            raise
        finally:
            os.close(descriptor)
            self.lock_path.unlink()

    @staticmethod
    def _validate_arguments(arguments, schema):
        from jsonschema import Draft202012Validator
        try:
            if isinstance(arguments, dict) and set(arguments) - set(schema.get("properties", {})):
                raise ValueError("unknown arguments")
            # Never permit remote schema resolution, including vendor drift.
            for item in _business_objects(schema):
                if any(key in item and (not isinstance(item[key], str) or not item[key].startswith("#"))
                       for key in ("$ref", "$dynamicRef")):
                    raise ValueError("external schema reference")
            Draft202012Validator.check_schema(schema)
            Draft202012Validator(schema).validate(arguments)
            _json_hash(arguments)
        except Exception:
            raise GatewayError("cad_arguments_invalid") from None

    def _catalog(self):
        upstream = self._upstream()
        if getattr(upstream, "backend", self.backend) != self.backend:
            raise GatewayError("cad_backend_mismatch")
        discovered = upstream.list_tools()
        allowed = SOLIDWORKS_TOOLS if self.backend == "solidworks" else FUSION_TOOLS
        if not isinstance(discovered, dict):
            raise GatewayError("cad_tool_discovery_invalid")
        selected = {}
        for name, definition in discovered.items():
            if name not in allowed:
                continue
            if not isinstance(definition, dict) or definition.get("name") != name or not isinstance(definition.get("inputSchema"), dict):
                raise GatewayError("cad_tool_discovery_invalid")
            if self.backend == "solidworks" and name in PARAMETER_GATED_TOOLS and "parameter_plan_token" not in definition["inputSchema"].get("required", []):
                raise GatewayError("cad_parameter_gate_missing")
            selected[name] = definition
        return selected

    def _path_guard(self, name, arguments, state):
        """Constrain native outputs before validating any parameter token."""
        if name == "save_document":
            value = arguments.get("filepath")
            if not value:
                owned = state.get("owned_documents", [])
                if len(owned) != 1 or not owned[0].get("path"):
                    raise GatewayError("cad_path_required")
                value = owned[0]["path"]
            target = self._output_path(value, ".sldprt")
            if target != (self.workspace / "submission.SLDPRT").resolve():
                raise GatewayError("cad_path_denied")
            if target.exists() and str(target) not in state.get("saved_paths", []):
                raise GatewayError("cad_path_exists")
            if arguments.get("resolve_lightweight") or arguments.get("save_imported_references"):
                raise GatewayError("cad_tool_denied")
            return target
        if name == "take_screenshot":
            if not arguments.get("save_path"):
                raise GatewayError("cad_path_required")
            target = self._output_path(arguments["save_path"], ".png")
            if target.exists():
                raise GatewayError("cad_path_exists")
            return target
        if name == "close_document" and arguments.get("save", False):
            raise GatewayError("cad_document_not_owned")
        return None

    def _output_path(self, value, extension):
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise GatewayError("cad_path_denied")
        raw = Path(value)
        if any(part in {"..", "."} or ":" in part or part.rstrip(" .") != part for part in raw.parts[1:]):
            raise GatewayError("cad_path_denied")
        # Require an absolute path to avoid vendor cwd/relative path ambiguity.
        if not raw.is_absolute():
            raise GatewayError("cad_path_denied")
        self._guard_workspace_file(raw)
        path = raw.resolve()
        if path.suffix.lower() != extension or not path.is_relative_to(self.workspace):
            raise GatewayError("cad_path_denied")
        current = raw
        while current != self.workspace and current != current.parent:
            if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
                raise GatewayError("cad_path_denied")
            current = current.parent
        if path == self.workspace or not path.parent.is_dir():
            raise GatewayError("cad_path_denied")
        return path

    def _guard_workspace_file(self, path):
        """Check aliases before resolution, including the workspace itself."""
        path = Path(path).absolute()
        workspace = self.input_root.absolute()
        try:
            if not path.is_relative_to(workspace):
                raise ValueError("outside workspace")
            for entry in (*reversed(path.parents), path):
                try:
                    info = entry.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError("alias")
                if entry == path and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
                    raise ValueError("not an exclusive regular file")
            if workspace.resolve() != self.workspace:
                raise ValueError("workspace changed")
        except (ValueError, OSError):
            raise GatewayError("cad_path_denied") from None

    @staticmethod
    def _document_key(document):
        path = document.get("path")
        title = document.get("title")
        if not isinstance(title, str) or not title:
            raise GatewayError("cad_task_unknown")
        if path and path != "Not saved":
            if not isinstance(path, str) or not Path(path).is_absolute():
                raise GatewayError("cad_task_unknown")
            return ("path", str(Path(path).resolve()).casefold())
        return ("unsaved", title)

    def _probe(self, state, *, check_owned=True, phase="before_call"):
        state["maintenance_probe_calls"] += 1
        try:
            probe = self._upstream().probe_workspace()
            instance = probe.get("identity", probe.get("instance"))
            documents = probe.get("documents")
            if probe.get("safe") is not True or not isinstance(instance, dict) or not isinstance(documents, list):
                raise ValueError("unknown workspace")
            stable = {key: instance[key] for key in ("pid", "session_id", "version")}
            if any(value is None or value == "" for value in stable.values()):
                raise ValueError("unknown instance")
            if state["cad_instance"] is not None and stable != state["cad_instance"]:
                raise ValueError("instance drift")
            state["cad_instance"] = stable
            keys = [self._document_key(document) for document in documents]
            if len(set(keys)) != len(keys) or any(document.get("document_type", document.get("type")) not in {"Part", "part", 1}
                                                for document in documents):
                raise ValueError("ambiguous documents")
        except Exception as error:
            state["pending_cad"] = 1
            self._save(state, {"type": "cad_probe_failed", "phase": phase,
                               "error_type": type(error).__name__, "error_message": str(error)[:1024]})
            raise GatewayError("cad_task_unknown") from None
        owned = {self._document_key(document) for document in state["owned_documents"]}
        if check_owned and set(keys) != owned:
            raise GatewayError("cad_document_not_owned")
        return probe

    def _check_plan_source(self, plan):
        if not isinstance(plan, dict):
            raise GatewayError("parameter_source_denied")
        public = {}
        if "input_manifest" in self.contract:
            from .input_assets import iter_assets
            public = {asset["id"]: asset["sha256"] for asset in iter_assets(self.contract["input_manifest"])}
        authorization = self.contract.get("modeling_authorization", {})
        authorization_hashes = {self.contract_hash}
        if "task" in public:
            authorization_hashes.add(public["task"])
        parameters = plan.get("parameters", [])
        assumptions = plan.get("assumptions", [])
        if not isinstance(parameters, list) or not isinstance(assumptions, list):
            raise GatewayError("parameter_source_denied")
        ready = set()
        for item in parameters + assumptions:
            if not isinstance(item, dict):
                raise GatewayError("parameter_source_denied")
            source, decision, role = item.get("source_ref", ""), item.get("decision"), item.get("role")
            basis = item.get("basis")
            if not isinstance(source, str) or not isinstance(basis, str) or not basis.strip():
                raise GatewayError("parameter_source_denied")
            # Match complete hashes, never a prefix embedded in a longer hex value.
            hashes = {value.lower() for value in re.findall(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])", basis)}
            identifier, separator, location = source[7:].partition("#") if source.startswith("public:") else ("", "", "")
            public_source = (bool(separator) and bool(location.strip()) and
                             not any(ord(char) < 32 or ord(char) == 127 for char in location) and
                             public.get(identifier) in hashes)
            delegated = (source == "benchmark:delegated" and decision == "delegated" and
                         authorization.get("delegated_assumptions") is True and
                         authorization.get("source") == "benchmark:delegated" and
                         bool(authorization_hashes & hashes))
            derived = (role == "derived_constraint" and decision == "derived_from_ready" and
                       isinstance(item.get("depends_on"), list) and bool(item["depends_on"]) and
                       set(item["depends_on"]).issubset(ready) and source.startswith("derived:"))
            if not ((public_source and decision == "user_specified" and role in {None, "user_input"}) or
                    (delegated and role in {None, "agent_suggestion"}) or derived):
                raise GatewayError("parameter_source_denied")
            if item.get("name"):
                ready.add(item["name"])

    def _check_plan_call(self, name, arguments, state):
        plan = state.get("parameter_plan")
        if not plan or _json_hash(arguments.get("parameter_plan_token")) != plan["token_sha256"]:
            raise GatewayError("parameter_plan_mismatch")
        if time.time() >= plan["expires_at"]:
            raise GatewayError("parameter_plan_expired")
        operations, index = plan["operations"], plan["next_index"]
        bound = {key: value for key, value in arguments.items() if key not in {"parameter_plan_token", "confirm_overwrite"}}
        if index >= len(operations) or operations[index] != {"tool": name, "arguments": bound}:
            raise GatewayError("parameter_plan_mismatch")

    def _capture_native_save(self, result, target, request_id, state):
        from .native_contract import file_hash, plain_file
        from .solidworks import parse_tool_result
        from .solidworks_helper import classify_native_warnings
        payload = parse_tool_result(result)
        data, context = payload.get("data"), payload.get("document_context")
        if (not isinstance(data, dict) or not isinstance(context, dict) or context.get("type") != "Part"
                or data.get("file_verified") is not True or data.get("completion_state") != "completed"
                or type(data.get("save_error_code")) is not int or data["save_error_code"] != 0):
            raise ValueError("Native save did not verify an unambiguous Part")
        for path in (data.get("path"), context.get("path")):
            if self._output_path(path, ".sldprt") != target:
                raise ValueError("Native save path differs from the owned output")
        configuration = context.get("configuration")
        if (not isinstance(configuration, str) or not configuration.strip() or
                len(configuration) > 1024 or any(ord(char) < 32 for char in configuration)):
            raise ValueError("Native save configuration is unavailable")
        target = plain_file(target)
        digest, size = file_hash(target), target.stat().st_size
        if type(data.get("size_bytes")) is not int or data["size_bytes"] != size or data.get("output_sha256") != digest:
            raise ValueError("Native saved bytes differ from the adapter receipt")
        return {"schema": "cad-native-save/1", **{key: self.contract[key] for key in IDENTITY_KEYS},
                "path": str(target), "bytes": size, "sha256": digest, "document_type": "Part",
                "configuration": configuration, "request_id": request_id, "result_sha256": _json_hash(result),
                "instance": state["cad_instance"],
                "save_warnings": classify_native_warnings("save", data.get("save_warning_code"))}

    def _cad_call(self, arguments, state, request):
        if state["pending_cad"]:
            raise GatewayError("cad_task_unknown")
        name, native_args = arguments["tool"], arguments["arguments"]
        catalog = self._catalog()
        if name not in catalog:
            raise GatewayError("cad_tool_denied")
        schema = catalog[name]["inputSchema"]
        self._validate_arguments(native_args, schema)
        if self.contract.get("smoke") and name not in GENERAL_READ_TOOLS | {"fusion_mcp_read"}:
            raise GatewayError("smoke_read_only")
        if state["cad_calls"] >= self.contract.get("max_cad_tool_calls", self.contract["max_tool_calls"]):
            raise GatewayError("cad_tool_budget_exceeded")
        target, before, planned = None, None, False
        if self.backend == "solidworks":
            target = self._path_guard(name, native_args, state)
            if name == "validate_parameter_plan":
                self._verify_inputs()
                self._check_plan_source(native_args["plan"])
                for operation in native_args["plan"].get("control_operations", []):
                    if not isinstance(operation, dict) or operation.get("tool", operation.get("name")) not in catalog:
                        raise GatewayError("cad_tool_denied")
            planned = "parameter_plan_token" in schema.get("required", [])
            if planned:
                self._check_plan_call(name, native_args, state)
            before = self._probe(state)
            docs = before["documents"]
            if name in CREATE_TOOLS and docs:
                raise GatewayError("cad_document_not_owned")
            if name not in CREATE_TOOLS | GENERAL_READ_TOOLS | {"validate_parameter_plan"}:
                active = before.get("active_document")
                if len(docs) != 1 or not isinstance(active, dict) or self._document_key(active) != self._document_key(docs[0]):
                    raise GatewayError("cad_document_not_owned")
            if name == "close_document":
                if native_args.get("document_title") != docs[0]["title"] or self._document_key({
                        "title": native_args.get("document_title"), "path": native_args.get("expected_path")}) != self._document_key(docs[0]):
                    raise GatewayError("cad_document_not_owned")
            try:
                self._upstream().require_call_budget(name, native_args, post_probe=True)
            except TimeoutError as error:
                self._save(state, {"type": "cad_budget_rejected", **request, "native_tool": name,
                    "native_dispatched": False, "error_type": type(error).__name__,
                    "error_message": str(error)[:1024]})
                raise GatewayError("cad_insufficient_post_probe_budget") from None
        state["cad_calls"] += 1
        state["pending_cad"] = 1
        detail = {**request, "native_tool": name, "args_sha256": _json_hash(native_args)}
        self._save(state, {"type": "cad_started", **detail})
        call_stage = "native_call"
        try:
            native_result = self._upstream().call_tool(name, native_args)
            call_stage = "native_classification"
            outcome = _outcome(native_result, self.backend)
        except Exception as error:
            self._save(state, {"type": "cad_result", **detail, "outcome": "unknown",
                "failure_stage": call_stage, "error_type": type(error).__name__, "error_message": str(error)[:1024]})
            raise GatewayError("cad_task_unknown") from None
        outcome_detail = {"failure_stage": "native_outcome"} if outcome == "unknown" else {}
        if outcome != "unknown" and self.backend == "solidworks":
            post_stage = "post_call_probe"
            try:
                after = self._probe(state, check_owned=False, phase="after_call")
                post_stage = "postconditions"
                after_docs = after["documents"]
                if outcome == "completed_success" and name in CREATE_TOOLS:
                    if len(after_docs) != 1:
                        raise ValueError("creation postcondition")
                    state["owned_documents"] = after_docs
                elif outcome == "completed_success" and name == "save_document":
                    if len(after_docs) != 1 or self._document_key(after_docs[0]) != ("path", str(target).casefold()):
                        raise ValueError("save postcondition")
                    evidence = self._capture_native_save(native_result, target, request["request_id"], state)
                    state["owned_documents"] = after_docs
                    state["saved_paths"] = sorted(set(state["saved_paths"] + [str(target)]))
                    state["native_save_evidence"] = evidence
                    if evidence["save_warnings"]["status"] == "unknown":
                        raise ValueError("Native save warning requires reconciliation")
                    # A known blocked warning prevents submission, but the vendor
                    # completed this operation and consumed its plan position.
                elif outcome == "completed_success" and name == "close_document":
                    if after_docs:
                        raise ValueError("close postcondition")
                    state["owned_documents"] = []
                elif {self._document_key(doc) for doc in after_docs} != {self._document_key(doc) for doc in before["documents"]}:
                    raise ValueError("document identity changed")
            except Exception as error:
                outcome = "unknown"
                outcome_detail = {"failure_stage": post_stage, "error_type": type(error).__name__,
                                  "error_message": str(error)[:1024]}
            if outcome == "completed_success" and name == "validate_parameter_plan":
                tokens = {item[key] for item in _business_objects(native_result)
                          for key in ("parameter_plan_token", "token")
                          if isinstance(item.get(key), str) and item[key]}
                operations = native_args["plan"].get("control_operations", [])
                if len(tokens) != 1 or not isinstance(operations, list):
                    outcome = "unknown"
                else:
                    state["parameter_plan"] = {"token_sha256": _json_hash(next(iter(tokens))),
                        "expires_at": min(self.contract["deadline"], time.time() + native_args.get("ttl_seconds", 86400)),
                        "operations": [{"tool": operation.get("tool", operation.get("name")), "arguments": {
                            key: value for key, value in operation.get("arguments", operation.get("args", {})).items()
                            if key not in {"parameter_plan_token", "confirm_overwrite"}}} for operation in operations], "next_index": 0}
            elif planned and outcome == "completed_success":
                state["parameter_plan"]["next_index"] += 1
        if outcome != "unknown":
            state["pending_cad"] = 0
        self._save(state, {"type": "cad_result", **detail, **outcome_detail, "outcome": outcome, "native_result": native_result,
                           "result_sha256": _json_hash(native_result)})
        if outcome == "unknown":
            raise GatewayError("cad_task_unknown")
        return RawToolResult(native_result)

    def _submit_native(self, arguments, state):
        from .runner import atomic
        from .native_contract import make_native_descriptor, submission_spec
        if state["pending_cad"]:
            raise GatewayError("cad_task_unknown")
        configuration = arguments.get("configuration")
        if self.backend == "solidworks" and (not isinstance(configuration, str) or not configuration.strip()):
            raise GatewayError("configuration_required")
        if self.backend == "solidworks":
            if self._probe(state, phase="submit")["documents"]:
                raise GatewayError("cad_documents_still_open")
        native = self.workspace / submission_spec(self.backend)["filename"]
        report = self.workspace / "worker_report.json"
        for path in (native, report, self.workspace / "dependency_manifest.json"):
            self._guard_workspace_file(path)
        if any(path.is_symlink() or not path.is_file() or not path.stat().st_size for path in (native, report)):
            raise GatewayError("submission_missing")
        identity = {key: self.contract[key] for key in IDENTITY_KEYS}
        saved = state.get("native_save_evidence")
        if self.backend == "solidworks":
            if not isinstance(saved, dict) or saved.get("schema") != "cad-native-save/1":
                raise GatewayError("native_save_unverified")
            from .solidworks_helper import classify_native_warnings
            warnings = saved.get("save_warnings")
            if (not isinstance(warnings, dict) or warnings != classify_native_warnings("save", warnings.get("raw"))
                    or warnings.get("status") not in {"clear", "allowed"}):
                raise GatewayError("native_save_warning_blocked")
            if (saved.get("path") != str(native) or saved.get("configuration") != configuration or
                    saved.get("document_type") != "Part" or any(saved.get(key) != value for key, value in identity.items())):
                raise GatewayError("native_save_mismatch")
        dependency = self.workspace / "dependency_manifest.json"
        declaration = {"schema_version": 1, "case_id": identity["case_id"], "attempt_id": identity["attempt_id"],
            "inspection_method": "Source contract declaration only; external references unverified until independent trusted reopen",
            "reviewer": "cad-gateway/source-contract", "dependencies": []}
        dependency_bytes = (json.dumps(declaration, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        try:
            descriptor = make_native_descriptor(native, identity=identity, configuration=configuration,
                                                dependency_sha256=hashlib.sha256(dependency_bytes).hexdigest())
        except (ValueError, OSError):
            raise GatewayError("native_submission_invalid") from None
        if self.backend == "solidworks" and any(descriptor[key] != saved.get(key) for key in
                                                ("bytes", "sha256", "document_type", "configuration")):
            raise GatewayError("native_save_mismatch")
        if dependency.exists() or dependency.is_symlink():
            if dependency.is_symlink() or not dependency.is_file():
                raise GatewayError("cad_path_denied")
            retained = self.audit / ("candidate-dependency-" + uuid.uuid4().hex + ".json")
            with retained.open("xb") as stream:
                stream.write(dependency.read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
        # The exact serialized declaration is what the native descriptor binds.
        temporary = self.workspace / (".dependency-" + uuid.uuid4().hex + ".tmp")
        with temporary.open("xb") as stream:
            stream.write(dependency_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, dependency)
        receipt = {"schema": "cad-submission/1", "status": "submitted", **identity, "native_document": descriptor,
                   "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest()}
        atomic(self.audit / "submission-receipt.json", receipt)
        state["submitted"] = True
        return receipt

    def _public(self, name):
        if name == "cad_skill" and self.generic and self.contract.get("cad_skill"):
            return Path(self.contract["cad_skill"])
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
            if "input_manifest" in self.contract and arguments["name"] not in {"fusion_skill", "cad_skill"}:
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

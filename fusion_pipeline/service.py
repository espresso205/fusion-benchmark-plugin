"""Standard newline-delimited MCP stdio server; stdout is protocol-only."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")


class RawToolResult(dict):
    """Forward native MCP text/images/errors without stringifying binary content."""


def tool(name, description, properties=None, required=(), readonly=False):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties or {}, "required": list(required), "additionalProperties": False},
            "annotations": {"readOnlyHint": readonly, "destructiveHint": name == "cancel", "openWorldHint": False}}


def dispatch(service, message):
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    identifier, method = message.get("id"), message["method"]
    if "id" not in message:
        return None
    response = {"jsonrpc": "2.0", "id": identifier}
    params = message.get("params", {})
    if not isinstance(params, dict):
        return {**response, "error": {"code": -32602, "message": "Invalid params"}}
    if method == "initialize":
        requested = params.get("protocolVersion")
        result = {"protocolVersion": requested if requested in PROTOCOLS else PROTOCOLS[0],
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": service.name, "version": "3.0.0"},
                  "instructions": service.instructions}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": service.tools}
    elif method == "tools/call":
        try:
            name, arguments = params.get("name"), params.get("arguments", {})
            definition = next((t for t in service.tools if t["name"] == name), None)
            if definition is None or not isinstance(arguments, dict):
                raise ValueError("unknown tool or invalid arguments")
            schema = definition["inputSchema"]
            if set(arguments) - set(schema["properties"]) or set(schema.get("required", [])) - set(arguments):
                raise ValueError("unknown or missing arguments")
            value = service.call(name, arguments)
            if isinstance(value, RawToolResult):
                result = value
            else:
                result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, allow_nan=False)}],
                          "structuredContent": value if isinstance(value, dict) else {"result": value}, "isError": False}
        except Exception as error:
            # Do not echo transport errors, command lines, secrets or remote payloads.
            code = getattr(error, "public_code", "tool_failed")
            result = {"content": [{"type": "text", "text": code}], "isError": True}
    else:
        return {**response, "error": {"code": -32601, "message": "Method not found"}}
    return {**response, "result": result}


def serve(service, input_stream=None, output_stream=None):
    source = input_stream or sys.stdin.buffer
    target = output_stream or sys.stdout.buffer
    for line in source:
        try:
            if len(line) > 16 * 1024 * 1024:
                raise ValueError("request too large")
            message = json.loads(line)
            response = dispatch(service, message)
        except (ValueError, UnicodeError):
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if response is not None:
            target.write((json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"))
            target.flush()


class ManagementService:
    name = "fusion-benchmark"
    instructions = ("Manage an authorized Fusion benchmark. Read capabilities then preflight. "
                    "start_run launches fresh worker sessions and spends model budget. status polls; cancel requests a safe stop. "
                    "This management connection is for trusted maintainers, never modeling workers.")
    tools = [
        tool("capabilities", "Describe supported CLIs, budget scope and isolation limits.", readonly=True),
        tool("preflight", "Read-only local configuration and CLI checks; no model or Fusion calls.", readonly=True),
        tool("start_run", "Start an authorized new benchmark in background. Never resumes an old attempt.",
             {"provider": {"type": "string", "enum": ["opencode", "codex", "claude"]}, "model": {"type": "string"},
              "case_ids": {"type": "array", "items": {"type": "string"}}, "preflight_only": {"type": "boolean"}}),
        tool("status", "Read a managed job's state.", {"run_id": {"type": "string"}}, ("run_id",), True),
        tool("cancel", "Request cancellation. Unknown Fusion operations retain the lease for reconciliation.", {"run_id": {"type": "string"}}, ("run_id",)),
        tool("results", "Read scores and artifact references for a managed job.", {"run_id": {"type": "string"}}, ("run_id",), True),
    ]

    def __init__(self, config_path=None):
        self.config_path = Path(config_path).resolve() if config_path else None
        self.config = json.loads(self.config_path.read_text(encoding="utf-8")) if config_path else None

    def _job(self, identifier):
        if not self.config or not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ValueError("invalid job")
        folder = Path(self.config["runs_root"]) / ".service" / identifier
        if not folder.is_dir():
            raise ValueError("unknown job")
        return folder

    def call(self, name, arguments):
        from .adapters import capabilities, probe
        if name == "capabilities":
            return {"providers": capabilities(), "budget_scope": "mcp_tools_scope", "fusion_lease": "whole_attempt_serial",
                    "isolation": "trusted_internal_same_account", "configured": self.config is not None,
                    "credentials_returned": False, "preflight_calls_models_or_fusion": False}
        if name == "preflight":
            return {"configured": self.config is not None,
                    "providers": [probe(p, (self.config or {}).get(p)) for p in ("opencode", "codex", "claude")],
                    "model_and_fusion_connectivity": "not_tested", "strict_gt_isolation": False}
        if name == "start_run":
            if self.config is None:
                raise ValueError("server requires maintainer config")
            from .runner import atomic
            config = dict(self.config)
            provider = arguments.get("provider", config.get("provider", "opencode"))
            if provider not in ("opencode", "codex", "claude"):
                raise ValueError("invalid provider")
            model = arguments.get("model", config.get("models", {}).get(provider, config.get("model") if provider == config.get("provider", "opencode") else None))
            if not isinstance(model, str) or not model.strip():
                raise ValueError("explicit provider model required")
            cases = arguments.get("case_ids", config.get("case_ids"))
            if cases is not None and (not isinstance(cases, list) or not cases or len(set(cases)) != len(cases)
                                      or any(not isinstance(c, str) or not re.fullmatch(r"Q\d{2}", c) for c in cases)):
                raise ValueError("invalid case list")
            preflight_only = arguments.get("preflight_only", False)
            if type(preflight_only) is not bool:
                raise ValueError("invalid preflight flag")
            config.update(provider=provider, model=model, execution_backend="unified_gateway")
            if cases is not None:
                config["case_ids"] = cases
            identifier = uuid.uuid4().hex
            folder = Path(config["runs_root"]) / ".service" / identifier
            folder.mkdir(parents=True, exist_ok=False)
            config["cancel_file"] = str(folder / "cancel.request")
            atomic(folder / "config.json", config)
            atomic(folder / "job.json", {"run_id": identifier, "state": "starting", "provider": provider, "model": model})
            command = [config.get("python", sys.executable), "-B", "-m", "fusion_pipeline.cli", "_run-job", "--job", str(folder)]
            if preflight_only:
                command.append("--preflight-only")
            options = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
            with (folder / "coordinator.log").open("xb") as log:
                subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT, **options)
            return {"run_id": identifier, "state": "starting", "provider": provider, "model": model}
        folder = self._job(arguments["run_id"])
        if name == "cancel":
            (folder / "cancel.request").touch(exist_ok=True)
            return {"run_id": arguments["run_id"], "state": "cancellation_requested", "lease_released": False}
        status = json.loads((folder / "job.json").read_text(encoding="utf-8"))
        if status.get("run_path"):
            run_path = Path(status["run_path"])
            file = run_path / "pipeline-status.json"
            if file.exists():
                current = json.loads(file.read_text(encoding="utf-8"))
                terminal_failure = status.get("state") in ("failed", "blocked")
                status.update(state=status["state"] if terminal_failure else current["state"],
                              batch_complete=False if terminal_failure else current.get("batch_complete", False),
                              cases=[{k: r[k] for k in ("case_id", "state", "final_score") if k in r} for r in current.get("cases", [])])
            if name == "results":
                status["report"] = str(run_path / "report.md")
                summary_path = Path(self.config["private_runs_root"]) / run_path.name / "evaluation/summary.json"
                if summary_path.is_file():
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                    status.update(batch_mean=summary.get("batch_mean"), protocol=summary.get("protocol_id"),
                                  measurement_coverage=summary.get("measurement_coverage"))
                    allowed = {"case", "status", "final_score", "measurement_coverage", "measurement_weight_coverage",
                               "validation_status", "validation_passed", "participates_in_scoring", "score_kind", "score_interval",
                               "unavailable_metrics", "score_status", "aggregate_status", "scored_modules", "unmeasured_modules"}
                    metric_allowed = {"score", "status", "measured_score", "awarded_score", "score_source", "quality", "backend",
                                      "uncertainty", "reason_codes", "method", "method_id", "measurement_status", "measured", "fallback_used"}
                    status["scores"] = [{**{key: row[key] for key in allowed if key in row},
                                         "metrics": {module: {key: value[key] for key in metric_allowed if key in value}
                                                     for module, value in row.get("metrics", {}).items() if isinstance(value, dict)}}
                                        for row in summary.get("cases", [])]
        return status


def run_job(folder, preflight_only=False):
    from .runner import Trial, atomic
    folder = Path(folder)
    job = json.loads((folder / "job.json").read_text(encoding="utf-8"))
    try:
        trial = Trial(folder / "config.json")
        job.update(state="running", run_path=str(trial.run))
        atomic(folder / "job.json", job)
        code = trial.run_all(preflight_only=preflight_only)
        job.update(state=trial.state["state"], returncode=code)
    except BaseException as error:
        job.update(state="failed", error_type=type(error).__name__)
        code = 2
    atomic(folder / "job.json", job)
    return code

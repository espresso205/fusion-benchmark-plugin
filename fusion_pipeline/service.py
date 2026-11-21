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
                  "serverInfo": {"name": service.name, "version": "4.0.0"},
                  "instructions": service.instructions}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = service.list_tools() if callable(getattr(service, 'list_tools', None)) else {"tools": service.tools}
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
    try:
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
    finally:
        if callable(getattr(service, 'close', None)):
            service.close()


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
              "candidate_profile": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,47}$"},
              "reasoning": {"type": "object", "additionalProperties": False,
                  "properties": {"mode": {"enum": ["cli_default", "explicit"]},
                                 "control": {"enum": ["effort", "variant"]}, "value": {"type": "string"}},
                  "required": ["mode"]},
              "candidate_capabilities": {"type": "object", "additionalProperties": False,
                  "properties": {"schema": {"const": "candidate-capabilities/1"},
                      "study_kind": {"enum": ["capability_ablation", "negative_control_no_cad"]},
                      "skill_delivery": {"const": "gateway_read"}, "exposure_mode": {"const": "gateway_proxy"},
                      "skills": {"type": "array", "items": {"type": "string"}},
                      "mcp_servers": {"type": "array", "items": {"type": "string"}}},
                  "required": ["schema", "study_kind", "skill_delivery", "exposure_mode", "skills", "mcp_servers"]},
              "protocol":{"type":"string","enum":["fusion-step-adapted-v3.0","fusion-step-adapted-v4.0","fusion-step-adapted-v5.0"]},
              "coordinate_mode":{"type":"string","enum":["geometry","strict"]},
              "cad_backend":{"type":"string","enum":["fusion","solidworks"]},
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
            from .cad import cad_capabilities
            selected = None
            validation = {'status': 'not_configured'}
            if self.config is not None:
                from .candidate_config import resolve_candidate_configuration
                try:
                    selected = resolve_candidate_configuration(self.config)
                    validation = {'status': 'passed'}
                except (ValueError, OSError) as error:
                    validation = {'status': 'failed', 'reason': str(error).split(':', 1)[0] if isinstance(error, ValueError)
                                  else 'candidate_source_unavailable'}
            return {"providers": capabilities(), "budget_scope": "mcp_tools_scope", "fusion_lease": "whole_attempt_serial",
                    "cad": cad_capabilities(self.config or {}),
                    "isolation": "trusted_internal_same_account", "configured": self.config is not None,
                    "default_scoring_protocol":"fusion-step-adapted-v4.0", "default_coordinate_mode":"geometry",
                    "configured_scoring_protocol":(self.config or {}).get('protocol'),
                    "configured_coordinate_mode":(self.config or {}).get('coordinate_mode'),
                    "supported_scoring_protocols":["fusion-step-adapted-v3.0","fusion-step-adapted-v4.0","fusion-step-adapted-v5.0"],
                    "candidate_profiles": sorted((self.config or {}).get('candidate_profiles', {})),
                    "candidate_selection_modes": ['legacy_unprofiled', 'candidate_profile'],
                    "capability_selection": {'skill_delivery': ['gateway_read'], 'exposure_mode': ['gateway_proxy'],
                        'explicit_empty_skills': True, 'empty_mcp_requires': 'negative_control_no_cad'},
                    "reasoning_controls": {'codex': 'effort', 'claude': 'effort', 'opencode': 'variant'},
                    "candidate_selection": selected.get('candidate_selection') if selected else None,
                    "configuration_validation": validation,
                    "credentials_returned": False, "preflight_calls_models_or_fusion": False}
        if name == "preflight":
            from .cad import cad_capabilities
            from .runner import validate_run_configuration
            validation = {'status': 'not_configured'}
            selected = None
            probes = []
            if self.config is not None:
                try:
                    from .candidate_config import resolve_candidate_configuration
                    selected = resolve_candidate_configuration(self.config)
                    validate_run_configuration(selected)
                    validation = {'status': 'passed'}
                except (ValueError, OSError) as error:
                    validation = {'status': 'failed', 'reason': str(error).split(':', 1)[0] if isinstance(error, ValueError)
                                  else 'candidate_source_unavailable'}
                if validation['status'] == 'passed':
                    observed = probe(selected['provider'], selected.get('cli_executable'))
                    probes = [{key: value for key, value in observed.items() if key != 'executable'}]
                    if selected['reasoning']['mode'] == 'explicit':
                        from .reasoning import resolve_reasoning
                        try:
                            if not observed.get('available') or not observed.get('version'):
                                raise ValueError('reasoning_support_unverified')
                            resolve_reasoning(dict(selected, cli_version=observed['version']), selected['reasoning_support'])
                        except ValueError as error:
                            validation = {'status': 'failed', 'reason': str(error).split(':', 1)[0]}
                            if str(error) == 'reasoning_support_unverified':
                                from .candidate_config import unverified_reasoning_request, public_candidate_selection
                                selected['resolved_reasoning'] = unverified_reasoning_request(selected)
                                selected['candidate_selection'] = public_candidate_selection(selected)
            return {"configured": self.config is not None,
                    "cad": cad_capabilities(self.config or {}),
                    'configuration_validation': validation,
                    "providers": probes,
                    "candidate_selection": selected.get('candidate_selection') if selected else None,
                    "model_and_fusion_connectivity": "not_tested", "strict_gt_isolation": False}
        if name == "start_run":
            if self.config is None:
                raise ValueError("server requires maintainer config")
            from .runner import atomic
            config = dict(self.config)
            if 'protocol' in arguments:
                if arguments['protocol'] not in ('fusion-step-adapted-v3.0','fusion-step-adapted-v4.0','fusion-step-adapted-v5.0'):
                    raise ValueError('unsupported scoring protocol')
                if arguments['protocol'] != config.get('protocol'):
                    config.pop('coordinate_mode',None)
                config['protocol']=arguments['protocol']
            if 'coordinate_mode' in arguments:
                config['coordinate_mode']=arguments['coordinate_mode']
            selected_protocol=config.get('protocol','fusion-step-adapted-v4.0')
            selected_mode=config.get('coordinate_mode','geometry' if selected_protocol in ('fusion-step-adapted-v4.0','fusion-step-adapted-v5.0') else 'strict')
            if selected_mode not in ('geometry','strict') or selected_protocol not in ('fusion-step-adapted-v4.0','fusion-step-adapted-v5.0') and selected_mode!='strict':
                raise ValueError('coordinate_mode must be geometry/strict; historical protocols require strict')
            from .candidate_config import resolve_candidate_configuration
            config = resolve_candidate_configuration(config, arguments, require_reasoning_verified=True)
            provider = config['provider']
            if provider not in ("opencode", "codex", "claude"):
                raise ValueError("invalid provider")
            model = config['model']
            if not isinstance(model, str) or not model.strip():
                raise ValueError("explicit provider model required")
            cases = arguments.get("case_ids", config.get("case_ids"))
            if cases is not None and (not isinstance(cases, list) or not cases or len(set(cases)) != len(cases)
                                      or any(not isinstance(c, str) or not re.fullmatch(r"Q\d{2}", c) for c in cases)):
                raise ValueError("invalid case list")
            preflight_only = arguments.get("preflight_only", False)
            if type(preflight_only) is not bool:
                raise ValueError("invalid preflight flag")
            if arguments.get('cad_backend', config.get('cad_backend', 'fusion')) == 'solidworks' and config.get('execution_backend', 'unified_gateway') != 'unified_gateway':
                raise ValueError('SolidWorks requires execution_backend=unified_gateway')
            config.update(provider=provider, model=model, execution_backend="unified_gateway")
            if 'cad_backend' in arguments:
                config['cad_backend'] = arguments['cad_backend']
            from .runner import validate_run_configuration
            normalized, _ = validate_run_configuration(config)
            config.update(normalized)
            if cases is not None:
                config["case_ids"] = cases
            identifier = uuid.uuid4().hex
            folder = Path(config["runs_root"]) / ".service" / identifier
            folder.mkdir(parents=True, exist_ok=False)
            config["cancel_file"] = str(folder / "cancel.request")
            atomic(folder / "config.json", config)
            atomic(folder / "job.json", {"run_id": identifier, "state": "starting", "provider": provider, "model": model,
                                        'candidate_selection': config['candidate_selection'],
                                        'cad_backend': config['cad_backend'], 'cad_config_sha256': config['cad_config_sha256']})
            command = [config.get("python", sys.executable), "-B", "-m", "fusion_pipeline.cli", "_run-job", "--job", str(folder)]
            if preflight_only:
                command.append("--preflight-only")
            options = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
            with (folder / "coordinator.log").open("xb") as log:
                subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT, **options)
            return {"run_id": identifier, "state": "starting", "provider": provider, "model": model,
                    'candidate_selection': config['candidate_selection'],
                    'cad_backend': config['cad_backend'], 'cad_config_sha256': config['cad_config_sha256']}
        folder = self._job(arguments["run_id"])
        if name == "cancel":
            (folder / "cancel.request").touch(exist_ok=True)
            return {"run_id": arguments["run_id"], "state": "cancellation_requested", "lease_released": False}
        status = json.loads((folder / "job.json").read_text(encoding="utf-8"))
        from .candidate_config import project_candidate_evidence
        if 'candidate_selection' in status:
            status['candidate_selection'] = project_candidate_evidence(status['candidate_selection'])
        if status.get("run_path"):
            run_path = Path(status["run_path"])
            file = run_path / "pipeline-status.json"
            if file.exists():
                current = json.loads(file.read_text(encoding="utf-8"))
                from src.report_data import project_cad_provenance
                status.update(project_cad_provenance(current))
                if 'candidate_selection' in current:
                    status['candidate_selection'] = project_candidate_evidence(current['candidate_selection'])
                terminal_failure = status.get("state") in ("failed", "blocked")
                status.update(state=status["state"] if terminal_failure else current["state"],
                              batch_complete=False if terminal_failure else current.get("batch_complete", False),
                              cases=[{**{k: r[k] for k in ("case_id", "state", "final_score") if k in r},
                                      **({'candidate_selection': project_candidate_evidence(r['candidate_selection'])}
                                         if 'candidate_selection' in r else {}),
                                      **project_cad_provenance(r)} for r in current.get("cases", [])])
            if name == "results":
                if (run_path / "report.md").is_file():
                    status["report"] = str(run_path / "report.md")
                from .reporting import report_references
                status.update(report_references(Path(self.config["private_runs_root"]) / run_path.name))
                summary_path = Path(self.config["private_runs_root"]) / run_path.name / "evaluation/summary.json"
                if summary_path.is_file():
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                    status.update(batch_mean=summary.get("batch_mean"), protocol=summary.get("protocol_id"),
                                  measurement_coverage=summary.get("measurement_coverage"),
                                  coordinate_mode=summary.get('coordinate_mode'), scored_metrics=summary.get('scored_metrics'),
                                  diagnostic_metrics=summary.get('diagnostic_metrics'))
                    if summary.get('protocol_id') == 'fusion-step-adapted-v5.0':
                        status.update({key:summary[key] for key in ('complete_case_ids','total_case_count',
                            'participating_case_count','candidate_failure_rate','assembly_pass_rate',
                            'measurement_completion_rate','basic_inconclusive_rate','batch_mean_scope','ranking_eligible')
                            if key in summary})
                    allowed = {"case", "status", "final_score", "measurement_coverage", "measurement_weight_coverage",
                               "validation_status", "validation_passed", "participates_in_scoring", "score_kind", "score_interval",
                               "unavailable_metrics", "score_status", "aggregate_status", "scored_modules", "unmeasured_modules",
                               "coordinate_mode", "diagnostic_unavailable_metrics", "partial_lower_bound", "partial_lower_bound_kind", "measurement_count",
                               "ranking_eligible"}
                    metric_allowed = {"score", "status", "measured_score", "awarded_score", "score_source", "quality", "backend",
                                      "uncertainty", "reason_codes", "method", "method_id", "measurement_status", "measured", "fallback_used",
                                      "metric_role", "coordinate_mode"}
                    pose_fields = {'R','t','rotation_angle_deg','translation_norm_mm','rmse','seed','determinant',
                                   'orthogonality_error','bbox_center_delta_mm','coordinate_mode',
                                   'rotation_matrix','translation_mm','rmse_mm','rotation_determinant','rotation_orthogonality_error'}
                    status["scores"] = [{**{key: row[key] for key in allowed if key in row},
                                         **{key:{'status':row[key].get('status')} for key in
                                            ('candidate_basic','candidate_assembly','reference_preflight') if isinstance(row.get(key),dict)},
                                         'execution':{key:value for key,value in row.get('execution',{}).items()
                                                      if key in {'status','returncode','timed_out','wall_s','stage_timeouts'}},
                                         **({'pose':{'status':row['pose'].get('status'),
                                             'raw':{key:value for key,value in row['pose'].get('raw',{}).items() if key in pose_fields}}}
                                            if isinstance(row.get('pose'),dict) else {}),
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

"""Serial, unattended internal Fusion trial using the packaged evaluator unchanged."""
from datetime import datetime, timezone
import base64
import csv
import hashlib
import json
import math
import os
import re
from pathlib import Path, PurePosixPath
import shutil
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import uuid
import urllib.parse
import urllib.request
import zipfile

from src.io_utils import sha, strict_json
from .mcp import FusionMCP, extract_result, resolved_config

BASE = Path(__file__).resolve().parents[1]
MODEL = "lanyi/gpt-6.1-sol"
MODULES = ("validity", "shape", "dimension", "distance", "mass", "face_match", "section")


def validate_run_configuration(config):
    """Validate static launch requirements without contacting a model or CAD."""
    from .cad import normalize_cad_config, cad_capabilities
    from .adapters import capabilities
    normalized = normalize_cad_config(config)
    if config.get('cad_config_sha256') is not None and config['cad_config_sha256'] != normalized['cad_config_sha256']:
        raise ValueError('CAD configuration digest changed')
    backend = normalized['cad_backend']
    if backend != 'fusion' and config.get('execution_backend') != 'unified_gateway':
        raise ValueError('SolidWorks requires execution_backend=unified_gateway')
    provider = config.get('provider', 'opencode')
    if provider not in capabilities():
        raise ValueError('known provider required')
    if config.get('strict_tool_isolation') and not capabilities()[provider]['native_tools_disabled']:
        raise ValueError('provider does not support strict native tool exclusion')
    protocol = config.get('protocol', 'fusion-step-adapted-v4.0')
    mode = config.get('coordinate_mode', 'geometry' if protocol == 'fusion-step-adapted-v4.0' else 'strict')
    if protocol not in ('fusion-step-adapted-v2.0', 'fusion-step-adapted-v3.0', 'fusion-step-adapted-v4.0') or mode not in ('geometry', 'strict') or (protocol != 'fusion-step-adapted-v4.0' and mode != 'strict'):
        raise ValueError('unsupported scoring protocol or coordinate mode')
    capability = cad_capabilities(config)
    if backend == 'solidworks' and not capability.get('dependency_discovered'):
        raise ValueError('SolidWorks runtime dependencies are missing')
    if backend == 'fusion' and normalized['cad_config'].get('url'):
        endpoint = urllib.parse.urlparse(normalized['cad_config']['url'])
        if endpoint.scheme != 'http' or endpoint.hostname not in ('localhost', '127.0.0.1', '::1') or endpoint.username or endpoint.password:
            raise ValueError('Fusion requires a loopback HTTP endpoint without embedded credentials')
        if normalized['cad_config'].get('enabled') is False:
            raise ValueError('Fusion connection is disabled')
    return normalized, capability


def utc():
    return datetime.now(timezone.utc).isoformat()


def verify_session_messages(messages, prompt, session_id, *, smoke=False, expected_model=MODEL):
    """Check authoritative stored messages; the attached client's SSE can lag."""
    errors = []
    summary = {"session_id": session_id, "expected_model": expected_model,
               "expected_prompt_characters": len(prompt),
               "expected_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()}
    if (not isinstance(messages, list) or not messages
            or any(not isinstance(m, dict) or not isinstance(m.get("info"), dict)
                   or not isinstance(m.get("parts"), list) for m in messages)):
        return {**summary, "verified": False, "errors": ["invalid_messages"]}
    users = [m for m in messages if m["info"].get("role") == "user"]
    assistants = [m for m in messages if m["info"].get("role") == "assistant"]
    summary.update(message_count=len(messages), user_message_count=len(users), assistant_message_count=len(assistants))
    if any(m["info"].get("sessionID") != session_id for m in messages):
        errors.append("session_mismatch")
    if any(m["info"].get("error") is not None for m in messages):
        errors.append("message_error")
    if any(not isinstance(part.get("state"), dict) or part["state"].get("status") not in ("completed", "error")
           for message in assistants for part in message["parts"]
           if isinstance(part, dict) and part.get("type") == "tool"):
        errors.append("unfinished_tool")
    if len(users) != 1:
        errors.append("user_message_count")
    else:
        user = users[0]
        texts = [part.get("text") for part in user["parts"] if isinstance(part, dict) and part.get("type") == "text"]
        received = texts[0] if len(texts) == 1 and isinstance(texts[0], str) else None
        summary.update(received_prompt_characters=len(received) if received is not None else None,
                       received_prompt_sha256=hashlib.sha256(received.encode("utf-8")).hexdigest() if received is not None else None)
        if received != prompt:
            errors.append("prompt_mismatch")
        provider_id, model_id = expected_model.split("/", 1)
        if user["info"].get("model") != {"providerID": provider_id, "modelID": model_id}:
            errors.append("user_model_mismatch")
    if not assistants or messages[-1]["info"].get("role") != "assistant":
        errors.append("assistant_missing")
    else:
        if any(f"{m['info'].get('providerID')}/{m['info'].get('modelID')}" != expected_model for m in assistants):
            errors.append("assistant_model_mismatch")
        last = assistants[-1]
        info = last["info"]
        timing = info.get("time") if isinstance(info.get("time"), dict) else {}
        created, completed = timing.get("created"), timing.get("completed")
        summary.update(final_message_id=info.get("id"), final_finish=info.get("finish"),
                       final_created=created, final_completed=completed,
                       final_model=f"{info.get('providerID')}/{info.get('modelID')}")
        if (type(created) not in (int, float) or type(completed) not in (int, float)
                or not math.isfinite(created) or not math.isfinite(completed) or completed < created):
            errors.append("assistant_incomplete")
        if info.get("finish") != "stop":
            errors.append("assistant_not_stopped")
        if len(users) == 1 and info.get("parentID") != users[0]["info"].get("id"):
            errors.append("assistant_parent_mismatch")
        if smoke:
            reply = "".join(part.get("text", "") for part in last["parts"]
                            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str))
            summary["smoke_tail_acknowledged"] = reply.strip() == "SMOKE_OK"
            if not summary["smoke_tail_acknowledged"]:
                errors.append("smoke_tail_ack_missing")
    return {**summary, "verified": not errors, "errors": errors}


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def new_run(root):
    path = Path(root).absolute() / (datetime.now(timezone.utc).strftime("fusion-%Y%m%dT%H%M%S-") + uuid.uuid4().hex[:8])
    path.mkdir(parents=True, exist_ok=False)
    return path


def extract_evaluator(archive, destination):
    """Whitelist the signed-by-hash payload, reject extra/traversing entries."""
    destination = Path(destination)
    prefix = "fusion_benchmark_evaluator_v2/"
    with zipfile.ZipFile(archive) as source:
        names = source.namelist()
        if len(set(names)) != len(names):
            raise ValueError("duplicate archive entries")
        manifest = json.loads(source.read(prefix + "PACKAGE_MANIFEST.json"))
        allowed = {prefix + key for key in manifest["files"]} | {prefix + "PACKAGE_MANIFEST.json"}
        if set(names) != allowed:
            raise ValueError("archive contains files outside its manifest")
        contents = {}
        for name in names:
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name:
                raise ValueError("unsafe archive path")
            content = source.read(name)
            if name != prefix + "PACKAGE_MANIFEST.json":
                meta = manifest["files"][name[len(prefix):]]
                if len(content) != meta["bytes"] or hashlib.sha256(content).hexdigest() != meta["sha256"]:
                    raise ValueError("archive payload hash mismatch")
            contents[name] = content
        destination.mkdir(parents=True, exist_ok=False)
        for name, content in contents.items():
            path = destination.joinpath(*PurePosixPath(name).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                stream.write(content)
    return destination / prefix.rstrip("/")


def snapshot_evaluator(source, destination):
    """Freeze only evaluator source, never sibling reports, GT or credentials."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    names = {name for name in ("evaluate.py", "evaluate_v4.py", "worker.py", "worker_v4.py", "bootstrap.py", "scheduler.py", "weights.json", "weights_v3.json", "weights_v4.json", "render_report.py", "serve_report.py")
             if (source / name).is_file()}
    for name in ("report_assets/report.css", "report_assets/report.js", "report_assets/VIEWER_SOURCES.json",
                  "fusion_pipeline/__init__.py", "fusion_pipeline/input_assets.py", "fusion_pipeline/native_contract.py",
                 "report_assets/viewer/comparison-view.js", "report_assets/viewer/mesh-validation.js",
                 "report_assets/viewer/preview-geometry-worker.js", "report_assets/vendor/three.module.js",
                 "report_assets/licenses/CADViewer-MIT.txt", "report_assets/licenses/Three-MIT.txt"):
        if (source / name).is_file():
            names.add(name)
    for folder in ("src", "scheduler", "vendor"):
        if (source / folder).is_dir():
            names.update(p.relative_to(source).as_posix() for p in (source / folder).rglob("*")
                         if p.is_file() and p.suffix in (".py", ".json") and "__pycache__" not in p.parts)
    if not {"evaluate.py", "worker.py", "weights.json"} <= names:
        raise ValueError("evaluator directory lacks required source")
    destination.mkdir(parents=True, exist_ok=False)
    files = {}
    for name in sorted(names):
        source_file = source / name
        if source_file.is_symlink() or not source_file.resolve().is_relative_to(source):
            raise ValueError("evaluator source escapes directory")
        data = source_file.read_bytes()
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        files[name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    atomic(destination / "PACKAGE_MANIFEST.json", {"schema": "evaluator-source-snapshot/1", "files": files})
    return destination


def classify_guard(state):
    if state is None:
        return "guard_missing"
    if not isinstance(state, dict):
        return "guard_invalid"
    if state.get("pendingFusion"):
        return "fusion_task_unknown"
    if (type(state.get("version")) is not int or state["version"] != 1 or type(state.get("pendingFusion")) is not int
            or state["pendingFusion"] != 0 or state.get("model_verified") is not True
            or not isinstance(state.get("sessionID"), str) or not state["sessionID"]):
        return "guard_invalid"
    deadline = state.get("deadline")
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        return "guard_invalid"
    if time.time() * 1000 >= deadline:
        return "budget_exceeded"
    calls = state.get("calls")
    if (not isinstance(calls, dict) or type(state.get("total_tool_calls")) is not int
            or type(state.get("fusion_tool_calls")) is not int or state["total_tool_calls"] != len(calls)
            or not 0 <= state["fusion_tool_calls"] <= state["total_tool_calls"] <= 200):
        return "guard_invalid"
    fusion_calls = 0
    for call_id, call in calls.items():
        if (not isinstance(call_id, str) or not call_id or not isinstance(call, dict)
                or call.get("sessionID") != state["sessionID"] or type(call.get("fusion")) is not bool
                or not isinstance(call.get("tool"), str) or not call["tool"]):
            return "guard_invalid"
        fusion_calls += call["fusion"]
        status = call.get("status")
        if status in ("pending", "unknown"):
            return "fusion_task_unknown" if call["fusion"] else "tool_task_unknown"
        if status == "denied":
            if call.get("permitted") is not False:
                return "guard_invalid"
        elif status not in ("completed", "error") or call.get("permitted") is not True or call.get("terminal_seen") is not True:
            return "guard_invalid"
    if fusion_calls != state["fusion_tool_calls"]:
        return "guard_invalid"
    return "clear"


def source_hashes():
    inventory = {p.relative_to(BASE).as_posix(): sha(p)
            for folder in (BASE / "fusion_pipeline", BASE / "plugins")
            for p in folder.rglob("*") if p.is_file() and p.suffix in {".py", ".mjs"}
            and "__pycache__" not in p.parts}
    for name in ("src/__init__.py", "src/io_utils.py", "agent/fusion-modeling/SKILL.md", "agent/solidworks-modeling/SKILL.md"):
        if (BASE / name).is_file():
            inventory[name] = sha(BASE / name)
    return inventory


def verify_package(root, manifest_sha256):
    """Check the extraction against the manifest frozen before any modeling."""
    root = Path(root)
    manifest_path = root / "PACKAGE_MANIFEST.json"
    if sha(manifest_path) != manifest_sha256:
        raise ValueError("packaged evaluator manifest changed")
    files = strict_json(manifest_path)["files"]
    if not {"evaluate.py", "worker.py", "weights.json"} <= set(files):
        raise ValueError("package manifest lacks required evaluator payload")
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
              and "__pycache__" not in p.relative_to(root).parts and p.suffix not in {".pyc", ".pyo"}}
    if actual != set(files) | {"PACKAGE_MANIFEST.json"}:
        raise ValueError("package payload file set changed")
    for name, meta in files.items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name:
            raise ValueError("unsafe package payload path")
        path = root.joinpath(*relative.parts)
        if (path.is_symlink() or not path.resolve().is_relative_to(root.resolve())
                or path.stat().st_size != meta["bytes"] or sha(path) != meta["sha256"]):
            raise ValueError("package payload changed: " + name)


def scoring_options(config, evaluator):
    evaluator = Path(evaluator)
    default = ('fusion-step-adapted-v4.0' if (evaluator/'weights_v4.json').is_file() else
               'fusion-step-adapted-v3.0' if (evaluator/'weights_v3.json').is_file() else 'fusion-step-adapted-v2.0')
    protocol = config.get('protocol', default)
    files = {'fusion-step-adapted-v2.0':'weights.json', 'fusion-step-adapted-v3.0':'weights_v3.json',
             'fusion-step-adapted-v4.0':'weights_v4.json'}
    if protocol not in files:
        raise ValueError('unsupported scoring protocol')
    mode = config.get('coordinate_mode', 'geometry' if protocol=='fusion-step-adapted-v4.0' else 'strict')
    if mode not in ('geometry','strict') or protocol!='fusion-step-adapted-v4.0' and mode!='strict':
        raise ValueError('coordinate_mode must be geometry/strict; historical protocols require strict')
    weights = evaluator/files[protocol]
    if not weights.is_file():
        raise ValueError('selected scoring protocol is absent from evaluator snapshot')
    return protocol, mode, weights


def validate_evaluation(output, cases_path, returncode, weights_sha256, *, protocol=None, coordinate_mode=None):
    """Accept only a complete, consistent publication from the unchanged evaluator."""
    if returncode not in (0, 2):
        raise RuntimeError(f"packaged evaluator exit code {returncode}; output is incomplete")
    output = Path(output)
    for name in ("manifest.json", "summary.json", "report.md", "comparison.csv"):
        path = output / name
        if not path.is_file() or not path.stat().st_size:
            raise ValueError("missing evaluation publication: " + name)
    cases = strict_json(cases_path)["cases"]
    manifest = strict_json(output / "manifest.json")
    summary = strict_json(output / "summary.json")
    manifest_hash = sha(output / "manifest.json")
    if protocol is not None and manifest.get('protocol_id') != protocol:
        raise ValueError('evaluation scoring protocol mismatch')
    is_v4 = manifest.get('protocol_id') == 'fusion-step-adapted-v4.0'
    if is_v4:
        mode = manifest.get('coordinate_mode')
        if mode not in ('geometry','strict') or summary.get('coordinate_mode') != mode or (coordinate_mode is not None and coordinate_mode != mode):
            raise ValueError('evaluation coordinate mode mismatch')
        if summary.get('protocol_id') != manifest.get('protocol_id') or manifest.get('schema_version') != '4.0' or summary.get('schema_version') != '4.0':
            raise ValueError('evaluation v4 schema/protocol mismatch')
    if (manifest.get("cases") != cases or manifest.get("cases_sha256") != sha(cases_path)
            or manifest.get("weights_sha256") != weights_sha256
            or summary.get("run_manifest_sha256") != manifest_hash):
        raise ValueError("evaluation manifest/input binding mismatch")
    if summary.get("code_unchanged") is not True or summary.get("original_inputs_unchanged") is not True:
        raise ValueError("evaluation code or inputs changed")
    finals = summary.get("cases", [])
    expected_cases = {row["case"] for row in cases}
    by_id = {row["case"]: row for row in finals}
    if len(by_id) != len(finals) or set(by_id) != expected_cases:
        raise ValueError("evaluation summary case coverage mismatch")
    for case, final in by_id.items():
        if strict_json(output / case / "aggregate.json") != final or final.get("run_manifest_sha256") != manifest_hash:
            raise ValueError("evaluation aggregate does not match summary: " + case)
        score = final.get("final_score")
        if score is not None and (type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1):
            raise ValueError("invalid evaluation score")
        if is_v4 and (final.get('coordinate_mode') != manifest['coordinate_mode'] or final.get('protocol_id') != manifest['protocol_id']):
            raise ValueError('evaluation case protocol/coordinate mode mismatch')
    is_v3 = manifest.get("protocol_id") in ("fusion-step-adapted-v3.0", 'fusion-step-adapted-v4.0')
    eligible = all(row["final_score"] is not None for row in finals)
    participants = [r for r in finals if r.get("participates_in_scoring") and r["final_score"] is not None] if is_v3 else finals if eligible else []
    mean = math.fsum(row["final_score"] for row in participants) / len(participants) if participants else None
    successful = eligible or (is_v3 and bool(participants) and all(r["status"] in
                                 {"ok", "scored_with_unavailable_metrics", "candidate_invalid"} for r in finals))
    if is_v4:
        successful = bool(participants) and all(r['status'] in {'ok','scored_with_unavailable_metrics','candidate_invalid'}
                       and r.get('execution',{}).get('status')=='completed' for r in finals)
    if (returncode != (0 if successful else 2) or summary.get("all_cases_aggregatable") is not eligible
            or summary.get("batch_mean") != mean or summary.get("ranking_eligible") is not False):
        raise ValueError("evaluation completion/aggregate status mismatch")
    with (output / "comparison.csv").open(encoding="utf-8-sig", newline="") as stream:
        comparisons = list(csv.DictReader(stream))
    indexed = {(row.get("case"), row.get("module")): row for row in comparisons}
    comparison_modules = MODULES[1:] if is_v3 else MODULES
    if len(indexed) != len(comparisons) or set(indexed) != {(case, module) for case in expected_cases for module in comparison_modules}:
        raise ValueError("evaluation comparison coverage mismatch")
    for (case, module), row in indexed.items():
        expected = by_id[case].get("metrics", {}).get(module, {}).get("measured_score" if is_v4 else "score")
        value = float(row["new_score"]) if row.get("new_score") else None
        if value != expected or (not is_v3 and row.get("aggregate_status") != by_id[case]["status"]):
            raise ValueError("evaluation comparison score/status mismatch")
    report = (output / "report.md").read_text(encoding="utf-8")
    for case, final in by_id.items():
        rows = [line.split("|") for line in report.splitlines() if line.startswith("|" + case + "|")]
        expected = f'{final["final_score"] * 100:.3f}' if final["final_score"] is not None else "—" if is_v3 else "不聚合"
        if len(rows) != 1 or len(rows[0]) < 9 or rows[0][3 if is_v3 else 7] != expected:
            raise ValueError("evaluation report score/coverage mismatch")
    return summary


def stop_tree(process):
    owned = getattr(process, '_benchmark_owned_tree', None)
    if owned is not None:
        owned.stop()
        process.wait(timeout=15)
        return
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=30)
    else:
        import signal
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=15)


class Trial:
    def __init__(self, config_path, *, cad_backend=None):
        self.config_path = Path(config_path).resolve()
        self.config = strict_json(self.config_path)
        if cad_backend is not None:
            self.config['cad_backend'] = cad_backend
        normalized, capability = validate_run_configuration(self.config)
        self.config.update(normalized)
        self.cad_backend = normalized['cad_backend']
        self.cad_config_sha256 = normalized['cad_config_sha256']
        self.provider = self.config.get("provider", "opencode")
        self.unified_gateway = self.config.get("execution_backend") == "unified_gateway"
        if self.unified_gateway:
            self.config.setdefault('protocol', 'fusion-step-adapted-v4.0')
        self.config.setdefault('cad_skill', self.config.get('modeling_skill') or (self.config.get('fusion_skill') if self.cad_backend == 'fusion' else str(BASE / 'agent/solidworks-modeling/SKILL.md')))
        if not self.config.get('cad_skill'):
            self.config['cad_skill'] = str(BASE / 'agent/fusion-modeling/SKILL.md')
        if self.cad_backend == 'fusion':
            self.config.setdefault('fusion_skill', self.config['cad_skill'])
        fields = ["python", "questions_root", "private_cases", "runs_root", "private_runs_root", "cad_skill"]
        fields += [field for field in ("evaluator_zip", "evaluator_directory", "opencode", "cli_executable") if field in self.config]
        for field in fields:
            if not Path(self.config[field]).is_absolute():
                raise ValueError(f"{field} must be an absolute local path")
        if self.provider not in ("opencode", "codex", "claude") or not isinstance(self.config.get("model"), str) or not self.config["model"]:
            raise ValueError("known provider and explicit model required")
        if not self.unified_gateway and (self.provider != "opencode" or self.config.get("model") != MODEL):
            raise ValueError(f"this trial explicitly requires {MODEL}")
        cases = self.config.get("case_ids", [f"Q{i:02d}" for i in range(1, 11)])
        if (not isinstance(cases, list) or not cases or any(not isinstance(c, str) or not re.fullmatch(r"Q\d{2}", c) for c in cases)
                or len(set(cases)) != len(cases)):
            raise ValueError("case_ids must contain unique Qxx identifiers")
        if self.cad_backend != 'fusion':
            self.cli_config = {'mcp': {}}
        elif self.config['cad_config'].get('url'):
            self.cli_config = {'mcp': {'autodeskfusion': self.config['cad_config']}}
        else:
            self.cli_config = resolved_config(self.config['opencode'])
            self.config['fusion_connection'] = self.cli_config.get('mcp', {}).get('autodeskfusion', {})
            self.config['cad_config'] = self.config['fusion_connection']
            self.config.pop('cad_config_sha256', None)
            normalized, capability = validate_run_configuration(self.config)
            self.config.update(normalized)
            self.cad_config_sha256 = normalized['cad_config_sha256']
        self.run = new_run(self.config["runs_root"])
        self.private = Path(self.config["private_runs_root"]) / self.run.name
        self.private.mkdir(parents=True, exist_ok=False)
        self.state = {"schema_version": "cad-trial/1" if self.unified_gateway else "fusion-trial/1", "run_id": self.run.name, "started_at_utc": utc(),
                      "model": self.config["model"], "provider": self.provider, "state": "preparing", "isolation": "trusted_internal_same_windows_account",
                      "budget_scope": "mcp_tools_scope" if self.unified_gateway else "all_opencode_tools",
                      "rank_eligible": False, "all_artifacts_frozen": False, "batch_complete": False,
                      "runner_code_sha256": source_hashes(),
                      "cad_backend": self.cad_backend, "cad_config_sha256": self.cad_config_sha256,
                      "cad_capabilities": capability,
                      "cases": [{"case_id": case, "state": "not_started"} for case in cases]}
        if self.unified_gateway:
            for row in self.state['cases']:
                row.update(run_id=self.run.name, attempt_id='attempt-' + uuid.uuid4().hex,
                           protocol_id=self.config.get('protocol', 'fusion-step-adapted-v4.0'),
                           cad_backend=self.cad_backend, cad_config_sha256=self.cad_config_sha256)
        self._processes = []
        self.mcp = None
        self.cad_adapter = None
        self.save()

    def case_identity(self, row):
        return {key: row[key] for key in ('run_id', 'case_id', 'attempt_id', 'protocol_id', 'cad_backend', 'cad_config_sha256')}

    def event(self, event):
        event = {"timestamp_utc": utc(), **event}
        with (self.run / "runner-events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def save(self):
        self.state["updated_at_utc"] = utc()
        atomic(self.run / "pipeline-status.json", self.state)

    def verify_source(self):
        expected = self.state.get("runner_code_sha256")
        if expected is not None and source_hashes() != expected:
            raise ValueError("runner/plugin source changed after startup")

    def verify_evaluator(self):
        verify_package(self.evaluator, self.state["evaluator_manifest_sha256"])

    def verify_public_inputs(self, row):
        if row.get("input_manifest"):
            from .input_assets import verify_manifest
            manifest = row["input_manifest"]
            verify_manifest(manifest, self.ledger / "public" / row["case_id"])
            verify_manifest(manifest, Path(row["workspace"]))
            return
        workspace = Path(row["workspace"])
        for name in ("TASK.md", "drawing.dxf"):
            public = self.ledger / "public" / row["case_id"] / name
            candidate_copy = workspace / name
            if not candidate_copy.is_file() or sha(candidate_copy) != sha(public):
                raise ValueError(f"working public input changed: {row['case_id']}/{name}")

    def prepare_case_inputs(self, row):
        """Freeze one explicit list, then copy only its bound files to the worker."""
        from .input_assets import iter_assets, legacy_manifest, verify_manifest
        case = row["case_id"]
        source = Path(self.config["questions_root"]) / case
        manifest_path = source / "inputs.json"
        identity_args = []
        if getattr(self, 'cad_backend', None) and self.unified_gateway:
            identity_args = ['--protocol-id', row['protocol_id'], '--attempt-id', row['attempt_id']]
        if manifest_path.exists():
            if not getattr(self, "unified_gateway", False):
                raise ValueError("manifest inputs require execution_backend=unified_gateway")
            self.scheduler("register-case", self.ledger, case, "--manifest", manifest_path, *identity_args)
            checkpoint = strict_json(self.ledger / "checkpoint.json")
            manifest = checkpoint["cases"][case]["contract"]["inputs"]
        else:
            self.scheduler("register-case", self.ledger, case, "--task", source / "TASK.md", "--drawing", source / "drawing.dxf", *identity_args)
            manifest = legacy_manifest(case, self.ledger / "public" / case)
        frozen = self.ledger / "public" / case
        verify_manifest(manifest, frozen)
        workspace = self.ledger / "candidate" / case
        workspace.mkdir(parents=True, exist_ok=False)
        for asset in iter_assets(manifest):
            target = workspace / asset["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(frozen / asset["path"], target)
        row.update(workspace=str(workspace), input_manifest=manifest, state="queued")
        self.verify_public_inputs(row)

    def cleanup_processes(self):
        """Try every owned process tree, including when one cleanup fails."""
        errors = []
        remaining = []
        for process in reversed(getattr(self, "_processes", [])):
            try:
                stop_tree(process)
            except BaseException as error:
                errors.append(type(error).__name__ + ": " + str(error))
                remaining.append(process)
                if not isinstance(error, Exception):
                    self._cleanup_interrupt = error
        self._processes = list(reversed(remaining))
        if errors:
            self.state.setdefault("process_cleanup_errors", []).extend(errors)
        return errors

    def scheduler(self, *args):
        # Pipe decoding and child output must agree even with legacy Windows
        # locales, inherited PYTHONIOENCODING, or an isolated portable runtime.
        env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
        result = subprocess.run([self.config["python"], "-X", "utf8", "-B", "-c",
                                "import sys,runpy;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('scheduler',run_name='__main__')",
                                str(self.evaluator), *map(str, args)],
                                cwd=self.evaluator, env=env, capture_output=True, encoding="utf-8", timeout=60)
        if result.returncode:
            raise RuntimeError("packaged scheduler failed: " + (result.stderr or result.stdout)[-2000:])
        return result.stdout.strip()

    def prepare(self):
        self.check_cancel()
        self.verify_source()
        if getattr(self, 'cad_backend', None) and self.unified_gateway:
            self.state['stage'] = 'preparation'
            if not Path(self.config['cad_skill']).is_file():
                raise ValueError('public CAD modeling skill is missing')
            self.state['modeling_skill_sha256'] = sha(self.config['cad_skill'])
        if self.config.get("evaluator_directory") or not self.config.get("evaluator_zip"):
            self.evaluator = snapshot_evaluator(self.config.get("evaluator_directory", BASE), self.private / "evaluator")
            self.state["evaluator_source"] = "frozen_directory_snapshot"
        else:
            archive_hash = sha(self.config["evaluator_zip"])
            self.evaluator = extract_evaluator(self.config["evaluator_zip"], self.private / "evaluator")
            if sha(self.config["evaluator_zip"]) != archive_hash:
                raise ValueError("evaluator archive changed during extraction")
            self.state["evaluator_zip_sha256"] = archive_hash
        self.state["evaluator_manifest_sha256"] = sha(self.evaluator / "PACKAGE_MANIFEST.json")
        self.verify_evaluator()
        protocol, coordinate_mode, _ = scoring_options(self.config,self.evaluator)
        self.state.update(scoring_protocol=protocol,coordinate_mode=coordinate_mode)
        if getattr(self, "unified_gateway", False):
            from .adapters import capabilities, probe
            doctor = probe(self.provider, self.config.get("cli_executable", self.config.get(self.provider)))
            self.state["adapter"] = doctor
            if not doctor["available"]:
                raise ValueError("provider CLI lacks required launch flags")
            if self.config.get("strict_tool_isolation") and not capabilities()[self.provider]["native_tools_disabled"]:
                raise ValueError("provider does not support strict native tool exclusion")
        else:
            self.state["opencode_version"] = subprocess.run([self.config["opencode"], "--version"], capture_output=True,
                                                             encoding="utf-8", timeout=30).stdout.strip()
            models = subprocess.run([self.config["opencode"], "models", "lanyi"], capture_output=True,
                                    encoding="utf-8", timeout=30)
            if MODEL not in models.stdout.splitlines():
                raise ValueError("requested model is not advertised by this OpenCode installation")
        # The frozen private registry never enters a child prompt/environment.
        private_cases = strict_json(self.config["private_cases"])["cases"]
        identifiers = [row["case"] for row in private_cases]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("private registry contains duplicate case identifiers")
        selected = {r["case_id"] for r in self.state["cases"]}
        self.references = {r["case"]: r for r in private_cases if r["case"] in selected}
        if set(self.references) != selected:
            raise ValueError("private registry lacks a selected case")
        for case, row in self.references.items():
            for role in ("gt", "annotations"):
                if sha(row[role]) != row["expected_hashes"][role]:
                    raise ValueError(f"private {role} hash mismatch: {case}")
            if strict_json(row["annotations"])["gt_sha256"] != row["expected_hashes"]["gt"]:
                raise ValueError("annotation GT binding mismatch")
        atomic(self.private / "reference_registry.json", {"cases": list(self.references.values())})
        if protocol != 'fusion-step-adapted-v4.0' and (self.evaluator / "weights_v3.json").exists():
            from src.robust_metrics import build_section_plan
            plans = {case: build_section_plan(ref['gt'], strict_json(ref['annotations']))
                     for case, ref in self.references.items()}
            atomic(self.private / "reference_plans.json", plans)
            self.state['reference_plans_sha256'] = sha(self.private / "reference_plans.json")
        cad_args = (['--cad-backend', self.cad_backend, '--cad-config-sha256', self.cad_config_sha256]
                    if getattr(self, 'cad_backend', None) and self.unified_gateway else [])
        self.ledger = Path(self.scheduler("create-run", self.run / "ledger", self.run.name, *cad_args))
        for row in self.state["cases"]:
            self.prepare_case_inputs(row)
        if getattr(self, 'cad_backend', None) and self.unified_gateway:
            from .cad import create_adapter
            self.state['stage'] = 'cad_connection'
            self.cad_adapter = create_adapter(self.config, workspace=self.run / 'cad-workspace',
                                             audit_dir=self.run / 'audit/cad', audit=self.event)
            self.cad_adapter.connect(launch_if_needed=bool(self.config['cad_config'].get('launch_if_needed', False)))
            self.state['cad_identity'] = dict(self.cad_adapter.identity)
            self.state['cad_capabilities'].update(mcp_handshake={'executed': True, 'status': 'passed'},
                                                  cad_connection={'executed': True, 'status': 'passed'})
        else:
            self.mcp = FusionMCP(self.cli_config, self.event, timeout=180)
        self.probe("initial")
        self.state["state"] = "ready"
        self.state.pop('failure_stage', None)
        self.save()

    def probe(self, label):
        if getattr(self, 'cad_adapter', None) is not None:
            data = self.cad_adapter.probe_workspace()
            atomic(self.run / 'preflight' / f'{label}.json', data)
            docs = data.get('documents')
            if (not isinstance(docs, list) or docs or data.get('documents_count') != 0
                    or data.get('safe_to_model', data.get('workspace_empty_and_command_idle', False)) is not True):
                raise RuntimeError('CAD workspace is not empty or confirmed idle; preserve user documents and halt')
            instance = data.get('instance')
            if self.cad_backend == 'solidworks':
                if not isinstance(instance, dict) or any(instance.get(key) is None for key in ('pid', 'session_id', 'version')):
                    raise RuntimeError('CAD instance identity unconfirmed; reconciliation required')
                expected = self.config.get('expected_instance')
                if expected is not None and instance != expected:
                    raise RuntimeError('CAD instance changed; reconciliation required')
                self.config['expected_instance'] = instance
            self.state['cad_identity'] = dict(self.cad_adapter.identity)
            if instance:
                self.state['cad_identity'].update({key: instance[key] for key in ('pid', 'session_id', 'version') if key in instance})
            if data.get('fusion_version'):
                self.state['cad_identity']['version'] = data['fusion_version']
            self.record_desktop_identity()
            return data
        from .fusion_scripts import workspace_probe_script
        result = self.mcp.execute(workspace_probe_script(), readonly=True)
        atomic(self.run / "preflight" / f"{label}.json", result)
        data = extract_result(result)
        docs = data.get("documents")
        command = data.get("active_command", data.get("activeCommand"))
        if (not isinstance(docs, list) or docs or data.get("documents_count") != 0
                or command not in (None, "", "SelectCommand")):
            raise RuntimeError("Fusion workspace not empty or active command present; preserve user documents and halt")
        return data

    def child_environment(self, workspace, audit_dir, *, smoke=False):
        audit_dir.mkdir(parents=True, exist_ok=False)
        contract = {"run_id": self.run.name, "case_id": workspace.name, "attempt_id": "attempt-1",
                    "workspace": str(workspace), "audit_dir": str(audit_dir), "model": MODEL,
                    "max_tool_calls": 20 if smoke else 200, "max_seconds": 180 if smoke else 1800,
                    "started_at": utc(), "allowed_read_paths": [self.config["fusion_skill"]]}
        atomic(audit_dir / "contract.json", contract)
        mcp = {name: {"enabled": False} for name in self.cli_config.get("mcp", {}) if name != "autodeskfusion"}
        mcp["autodeskfusion"] = {"enabled": True, "timeout": 180000}
        overlay = {"model": MODEL, "small_model": MODEL,
                   "plugin": [(BASE / "plugins" / "fusion-benchmark-guard.mjs").as_uri()],
                   "mcp": mcp, "snapshot": False,
                   "permission": {"*": "deny", "read": "allow", "edit": "allow", "bash": "allow", "glob": "allow",
                                  "grep": "allow", "skill": {"*": "deny", "autodeskfusion": "allow"}, "todowrite": "allow",
                                  "autodeskfusion_*": "allow", "external_directory": "allow"},
                   "agent": {"benchmark": {"mode": "primary", "description": "Single public Fusion case, no reference access",
                                            "prompt": "Follow the single-case benchmark prompt. Do not delegate or read any reference data.",
                                            "model": MODEL}}}
        env = os.environ.copy()
        env.update({"OPENCODE_CONFIG_CONTENT": json.dumps(overlay), "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
                    "FUSION_BENCHMARK_CONTRACT": str(audit_dir / "contract.json"), "PYTHONIOENCODING": "utf-8"})
        # Prevent the child inheriting runner/private-registry environment settings.
        for key in list(env):
            if key.startswith("FUSION_PIPELINE_"):
                del env[key]
        return env

    def launch_gateway(self, workspace, audit_dir, prompt, *, smoke=False):
        from .adapters import executable, launch_fresh
        self.verify_source()
        if self.state.get('modeling_skill_sha256') and sha(self.config['cad_skill']) != self.state['modeling_skill_sha256']:
            raise ValueError('public modeling skill changed after preparation')
        audit_dir.mkdir(parents=True, exist_ok=False)
        cad_run = bool(getattr(self, 'cad_backend', None))
        connection_file = self.private / ("cad-connection.json" if cad_run else "fusion-connection.json")
        if not connection_file.exists():
            connection = ({key: self.config[key] for key in ('cad_backend', 'cad_config_sha256', 'cad_config', 'execution_backend', 'expected_instance') if key in self.config}
                          if cad_run else {"mcp": {"autodeskfusion": self.cli_config["mcp"]["autodeskfusion"]}})
            atomic(connection_file, connection)
        timeout = (300 if getattr(self, 'cad_backend', None) == 'solidworks' else 180) if smoke else 1800
        contract = {"schema": "fusion-worker/1", "run_id": self.run.name, "case_id": workspace.name,
                    "attempt_id": "attempt-" + uuid.uuid4().hex, "workspace": str(workspace), "audit_dir": str(audit_dir),
                    "model": self.config["model"], "max_tool_calls": 20 if smoke else 200, "deadline": time.time() + timeout,
                    "budget_scope": "mcp_tools_scope", "public_files": ["TASK.md"] if smoke else ["TASK.md", "drawing.dxf"],
                    "fusion_skill": self.config.get("fusion_skill", self.config.get('cad_skill')), "connection_file": str(connection_file),
                    "cancel_file": self.config.get("cancel_file"), "smoke": smoke}
        if cad_run:
            identity = (dict(run_id=self.run.name, case_id=workspace.name, attempt_id='attempt-' + uuid.uuid4().hex,
                             protocol_id=self.config.get('protocol', 'fusion-step-adapted-v4.0'), cad_backend=self.cad_backend,
                             cad_config_sha256=self.cad_config_sha256) if smoke else
                        self.case_identity(next(row for row in self.state['cases'] if row['case_id'] == workspace.name)))
            contract.update(identity, schema='cad-worker/1', cad_skill=self.config['cad_skill'],
                            expected_instance=self.config.get('expected_instance'),
                            modeling_authorization={'delegated_assumptions': True, 'source': 'benchmark:delegated',
                                'basis': 'Authorized task delegates implementation details and disclosed assumptions; explicit input dimensions remain authoritative'})
        if not smoke:
            from .input_assets import iter_assets
            row = next(row for row in self.state["cases"] if row["case_id"] == workspace.name)
            self.verify_public_inputs(row)
            contract["input_manifest"] = row["input_manifest"]
            contract["public_files"] = [asset["path"] for asset in iter_assets(row["input_manifest"])]
            if cad_run:
                contract['modeling_authorization']['task_sha256'] = row['input_manifest']['task']['sha256']
        atomic(audit_dir / "worker-contract.json", contract)
        bootstrap = BASE / "fusion-benchmark-bootstrap.py"
        if bootstrap.exists():
            worker_command = [self.config["python"], "-B", str(bootstrap), "worker", "--contract", str(audit_dir / "worker-contract.json")]
        else:
            worker_command = [self.config["python"], "-B", "-c", "import sys;sys.path.insert(0," + repr(str(BASE)) + ");from fusion_pipeline.cli import main;raise SystemExit(main())",
                              "worker", "--contract", str(audit_dir / "worker-contract.json")]
        env = {key: value for key, value in os.environ.items() if not key.startswith(("FUSION_PIPELINE_", "FUSION_BENCHMARK_"))}
        spec = {"provider": self.provider, "executable": executable(self.provider, self.config.get("cli_executable", self.config.get(self.provider))),
                "workspace": str(workspace), "audit_dir": str(audit_dir), "prompt": prompt, "model": self.config["model"],
                "worker_command": worker_command, "env": env, "timeout": timeout, "cancel_file": self.config.get("cancel_file"),
                "smoke": smoke,
                "credential_settings": self.config.get("credential_settings"),
                "strict_tool_isolation": self.config.get("strict_tool_isolation", False),
                "cad_backend": getattr(self, 'cad_backend', 'fusion'),
                "tool_timeout_s": (max(600, int(self.config['cad_config'].get('executor_timeout_s', 180))) + 60
                                   if getattr(self, 'cad_backend', None) == 'solidworks' else 180),
                "existing_mcp_names": list(self.cli_config.get("mcp", {}))}
        try:
            result = launch_fresh(spec, self._processes)
        finally:
            cleanup_errors = self.cleanup_processes()
            if cleanup_errors:
                raise RuntimeError("provider process cleanup incomplete; reconciliation required")
        state_path = audit_dir / "gateway-state.json"
        gateway = strict_json(state_path) if state_path.exists() else {}
        classification = "clear"
        if not gateway or gateway.get("contract_sha256") != sha(audit_dir / "worker-contract.json"):
            classification = "gateway_missing_or_changed"
        elif cad_run and (result.get('process_cleanup', {}).get('verified_stopped') is not True
                          or result.get('process_cleanup', {}).get('remaining_processes') != 0):
            classification = 'candidate_process_tree_unconfirmed'
        elif gateway.get("pending_cad") or gateway.get("pending_fusion"):
            classification = "cad_task_unknown" if cad_run else "fusion_task_unknown"
        elif gateway.get("cad_workspace_unconfirmed"):
            classification = "cad_workspace_unconfirmed"
        elif gateway.get("calls", 0) > contract["max_tool_calls"]:
            classification = "budget_exceeded"
        elif not result["session_verification"]["verified"]:
            classification = "session_unconfirmed"
        elif result.get("cancelled"):
            classification = "cancelled"
        elif not smoke and not gateway.get("submitted"):
            classification = "submission_unconfirmed"
        result.update(guard_classification=classification, guard=gateway, tool_calls=gateway.get("calls"),
                      fusion_tool_calls=gateway.get("fusion_calls"), budget_scope="mcp_tools_scope",
                      cad_tool_calls=gateway.get('cad_calls', gateway.get('fusion_calls')),
                      model_verified=result["session_verification"].get("model_verified") is True)
        return result

    def launch(self, workspace, audit_dir, prompt, *, smoke=False):
        if getattr(self, "unified_gateway", False):
            return self.launch_gateway(workspace, audit_dir, prompt, smoke=smoke)
        self.verify_source()
        env = self.child_environment(workspace, audit_dir, smoke=smoke)
        prompt_bytes = prompt.encode("utf-8")
        with (audit_dir / "prompt.txt").open("xb") as stream:
            stream.write(prompt_bytes)
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        started = time.monotonic()
        with socket.socket() as port_socket:
            port_socket.bind(("127.0.0.1", 0))
            port = port_socket.getsockname()[1]
        address = f"http://127.0.0.1:{port}"
        # The temporary service accepts only this launch's ephemeral credential.
        # Keep it in process memory/environment, never command arguments or reports.
        env["OPENCODE_SERVER_USERNAME"] = "opencode"
        env["OPENCODE_SERVER_PASSWORD"] = secrets.token_urlsafe(32)
        authorization = "Basic " + base64.b64encode(
            (env["OPENCODE_SERVER_USERNAME"] + ":" + env["OPENCODE_SERVER_PASSWORD"]).encode()).decode()
        def server_request(url):
            return urllib.request.Request(url, headers={"Authorization": authorization})
        with (audit_dir / "server.log").open("x", encoding="utf-8") as server_log:
            server = subprocess.Popen([self.config["opencode"], "serve", "--hostname", "127.0.0.1", "--port", str(port)],
                                      cwd=workspace, env=env, stdout=server_log, stderr=subprocess.STDOUT, **options)
            self._processes.append(server)
            interrupted = False
            try:
                deadline = time.monotonic() + 45
                while True:
                    try:
                        with urllib.request.urlopen(server_request(address + "/global/health"), timeout=2) as response:
                            if response.status == 200:
                                break
                    except OSError:
                        pass
                    if server.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("OpenCode server did not become healthy")
                    time.sleep(0.25)
                query = urllib.parse.urlencode({"directory": str(workspace)})
                # Do not print/persist config responses: resolved config can contain credentials.
                with urllib.request.urlopen(server_request(address + "/config?" + query), timeout=45) as response:
                    response.read()
                guard_path = audit_dir / "guard-state.json"
                if not guard_path.exists():
                    raise RuntimeError("OpenCode failed to load the guard; no model request was sent")
                loaded = strict_json(guard_path)
                contract = strict_json(audit_dir / "contract.json")
                expected = hashlib.sha256(json.dumps(contract, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
                if loaded.get("contract_sha256") != expected or loaded.get("total_tool_calls") != 0:
                    raise RuntimeError("guard preload contract verification failed")
                self.event({"event": "guard_preload_verified", "case_id": workspace.name, "contract_sha256": expected})
                with (audit_dir / "session.jsonl").open("x", encoding="utf-8") as log, (audit_dir / "stderr.log").open("x", encoding="utf-8") as error:
                    process = subprocess.Popen([self.config["opencode"], "run", "--attach", address, "--auto", "--model", MODEL,
                                               "--agent", "benchmark", "--format", "json", "--dir", str(workspace),
                                               "--title", f"Fusion benchmark {self.run.name} {workspace.name}"],
                                              cwd=workspace, env=env, stdin=subprocess.PIPE, stdout=log, stderr=error, **options)
                    self._processes.append(process)
                    self.state["active_process"] = {"pid": process.pid, "server_pid": server.pid,
                                                    "case_id": workspace.name, "started_at_utc": utc()}
                    self.save()
                    timed_out = False
                    try:
                        remaining = (180 if smoke else 1800) - (time.monotonic() - started)
                        # A .cmd wrapper can truncate multiline argv. Binary stdin
                        # also prevents Windows text mode from rewriting LF to CRLF.
                        process.communicate(input=prompt_bytes, timeout=max(1, remaining))
                        code = process.returncode
                    except subprocess.TimeoutExpired:
                        timed_out = True
                        stop_tree(process)
                        code = process.returncode
                terminal_guard = strict_json(guard_path)
                session_id = terminal_guard.get("sessionID")
                if not isinstance(session_id, str) or not session_id:
                    session_verification = {"verified": False, "errors": ["session_id_missing"]}
                else:
                    try:
                        url = address + "/session/" + urllib.parse.quote(session_id, safe="") + "/message?" + query
                        with urllib.request.urlopen(server_request(url), timeout=30) as response:
                            messages = json.loads(response.read().decode("utf-8"))
                        session_verification = verify_session_messages(messages, prompt, session_id, smoke=smoke)
                    except (OSError, ValueError):
                        # Error text or response bodies could contain credentials or
                        # model/tool text. Record the failure classification only.
                        session_verification = {"session_id": session_id, "verified": False,
                                                "errors": ["session_fetch_failed"]}
                atomic(audit_dir / "session-verification.json", session_verification)
                self.event({"event": "session_verification", "case_id": workspace.name, **session_verification})
                if code == 0 and not timed_out and not session_verification["verified"]:
                    raise RuntimeError("OpenCode session verification failed: " + ", ".join(session_verification["errors"]))
            except BaseException:
                interrupted = True
                raise
            finally:
                cleanup_errors = self.cleanup_processes()
                if not interrupted and getattr(self, "_cleanup_interrupt", None) is not None:
                    raise self._cleanup_interrupt
                if cleanup_errors and not interrupted:
                    raise RuntimeError("OpenCode process tree cleanup incomplete; reconciliation required")
        self.state.pop("active_process", None)
        guard_path = audit_dir / "guard-state.json"
        guard = strict_json(guard_path) if guard_path.exists() else None
        classification = classify_guard(guard)
        if isinstance(guard, dict) and guard.get("contract_sha256") != expected:
            classification = "guard_contract_changed"
        if (isinstance(guard, dict) and type(guard.get("total_tool_calls")) is int
                and guard["total_tool_calls"] > contract.get("max_tool_calls", 20 if smoke else 200)):
            classification = "budget_exceeded"
        self.save()
        return {"returncode": code, "timed_out": timed_out, "elapsed_s": time.monotonic() - started,
                "guard_classification": classification, "guard": guard,
                "session_verification": session_verification,
                "tool_calls": guard.get("total_tool_calls") if isinstance(guard, dict) else None,
                "fusion_tool_calls": guard.get("fusion_tool_calls") if isinstance(guard, dict) else None,
                "model_verified": isinstance(guard, dict) and guard.get("model_verified") is True}

    def smoke(self):
        workspace = self.run / "smoke"
        workspace.mkdir(exist_ok=False)
        (workspace / "TASK.md").write_text("Read-only integration smoke check. Do not create any CAD geometry.", encoding="utf-8")
        prompt = ("这是已授权的只读连通性测试，不是建模考试。\n"
            "只执行：1.read读取当前目录TASK.md；2.通过autodeskfusion的fusion_mcp_read查询activeCommand。\n"
            "不要执行脚本、创建/修改/关闭文档，不要访问其他目录，不要委派。\n"
            "尾行校验：上述工具返回后仅回复SMOKE_OK。")
        if getattr(self, "unified_gateway", False):
            prompt = ("这是已授权的只读连通性测试，不是建模考试。\n"
                      "只使用benchmark MCP：get_contract；read_input(name=TASK.md)；fusion_tools读取schema；"
                      "fusion_call使用fusion_mcp_read查询activeCommand。\n"
                      "不要运行脚本、创建修改关闭文档、提交文件或使用其他工具。\n尾行校验：完成后仅回复SMOKE_OK。")
        if getattr(self, 'cad_backend', None) and self.unified_gateway:
            read = ('get_solidworks_info，参数为空对象' if self.cad_backend == 'solidworks'
                    else 'fusion_mcp_read查询activeCommand，按实际schema传参')
            prompt = ('这是已授权只读连通性测试。只使用benchmark MCP：get_contract；read_input(name=TASK.md)；'
                      'cad_tools读取schema；cad_call使用' + read + '。不要创建、修改、关闭文档或提交文件。完成后仅回复SMOKE_OK。')
        result = self.launch(workspace, self.run / "audit" / "smoke", prompt, smoke=True)
        atomic(self.run / "smoke-result.json", result)
        if result["returncode"] or result["timed_out"] or result["guard_classification"] != "clear":
            raise RuntimeError("guarded OpenCode read-only smoke failed; inspect smoke-result and audit")
        if getattr(self, "unified_gateway", False):
            events = [json.loads(line) for line in (self.run / "audit/smoke/gateway-events.jsonl").read_text(encoding="utf-8").splitlines()]
            if getattr(self, 'cad_backend', None):
                if not result.get('cad_tool_calls') or not any(event.get('type') == 'cad_result' and event.get('outcome') == 'completed_success' for event in events):
                    raise RuntimeError('smoke has no successful CAD result through gateway')
            elif not result.get("fusion_tool_calls") or not any(event.get("type") == "fusion_result" and event.get("is_error") is False for event in events):
                raise RuntimeError("smoke has no successful Fusion result through gateway")
        else:
            events = [json.loads(line) for line in (self.run / "audit/smoke/guard-events.jsonl").read_text(encoding="utf-8").splitlines()]
            if not any("autodeskfusion" in str(event.get("tool", "")) for event in events):
                raise RuntimeError("smoke did not actually call the Fusion tool through the guard")
        self.probe("after_smoke")

    def model(self, row):
        if getattr(self, 'cad_backend', None) and self.unified_gateway:
            return self.model_cad(row)
        self.check_cancel()
        case = row["case_id"]
        workspace = Path(row["workspace"])
        self.verify_source()
        self.verify_public_inputs(row)
        self.probe("before_" + case)
        self.scheduler("transition", self.ledger, case, "case_queued")
        self.scheduler("transition", self.ledger, case, "modeling")
        row.update(state="modeling", started_at_utc=utc())
        self.save()
        prompt = f"""你是本题独立 Fusion 建模 Agent，仅处理 {case}。用户已授权完整自动运行；不要提问，不要委派。
只读取当前目录 TASK.md 和 drawing.dxf，通过现有 autodeskfusion MCP 在 Fusion 中重建图纸要求。先加载 autodeskfusion 技能。可使用只读DXF解析器；本机解析Python是 {self.config['python']}，包含ezdxf。不要用该Python创建CAD几何；几何必须通过Fusion原生API完成。
当前题目录：{workspace}。只读本题公开输入、autodeskfusion技能和通过Fusion工具查询的API文档。禁止读取任何历史答案、其他题、GT、私有标注、评测程序/结果、其他会话、父目录内容，禁止网络搜索答案或导入现成STEP/F3D。只在本题目录写文件。遵守固定世界坐标和DXF单位，不配准、不缩放以提高分数。
总预算1800秒、200工具调用、一次建模attempt；插件会在调用前强制预算。自身API错误可在同一次attempt内修正，记录实际错误。请尽快解析尺寸并建模，不长篇探索。
开始时Fusion应无打开文档。仅创建并操作自己的新设计。保存非空、自包含的 {workspace / 'submission.f3d'}；可选导出 submission.step。真实截图/读取实体、尺寸与特征健康后在 worker_report.json 记录模型、输入SHA256、单位、实体/草图/特征数量、错误与简化、文件路径和导出结果。不能编造成功。
完成后仅关闭本题自己创建的文档（原生F3D已导出后close(False)，不需要云保存），保持Fusion无文档供下一题使用。不要关闭/修改非本题文档。超时或后台状态不明不要重发操作，报告并停止。独立重开和可信STEP导出由流水线完成，不需要你接触评分器。现在开始。"""
        if getattr(self, "unified_gateway", False):
            from .input_assets import iter_assets
            reading = []
            for asset in iter_assets(row["input_manifest"]):
                if asset["mime"].startswith("image/"):
                    reading.append(f"read_image(id={asset['id']})")
                elif asset["mime"] == "application/dxf":
                    reading.append(f"parse_dxf(id={asset['id']})")
                else:
                    reading.append(f"read_input(name={asset['id']})")
            input_steps = "；".join(reading)
            prompt = f"""你是本题独立Fusion建模Agent，仅处理{case}。用户已授权完整自动运行，不要提问或委派。
只使用benchmark MCP工具，先get_contract确认单题合同、list_inputs发现有序输入，再read_input(name=fusion_skill)。按清单顺序逐项读取：{input_steps}。必须读取所有图片，结合视角和标注理解零件；工程尺寸以标注为依据，不能把像素直接当毫米，缺失尺寸或冲突须在报告注明实际假设。
fusion_tools提供实际Fusion工具schema，所有建模通过fusion_call执行Fusion原生API，不使用其他工具、外部几何库或现成答案。
当前工作区：{workspace}。禁止读取历史答案、其他题、GT、评分程序、父目录及其他会话。遵守图纸世界坐标和单位。200次受控MCP工具请求、1800秒、一次attempt；同attempt内可修正明确完成的API错误，结果未知立即停止不要重发。
Fusion开始时无文档，只操作自己新建的设计。完成后导出非空自包含{workspace / 'submission.f3d'}，检查实体/特征健康并关闭本题自己创建的文档(close(False))。不要关闭其他文档。用write_report记录实际模型、尺寸/单位、实体/草图/特征数量、简化与错误、导出路径和结果，不要编造成功。
最后调用submit冻结F3D和报告。提交成功后不要再调用任何工具，回复完成。维护端负责独立重开、STEP导出和评分。现在开始。"""
        result = self.launch(workspace, self.run / "audit" / case, prompt)
        row["execution"] = {k: v for k, v in result.items() if k != "guard"}
        row["ended_at_utc"] = utc()
        if result["timed_out"] or result["guard_classification"] != "clear":
            row["state"] = "reconciliation_required"
            self.save()
            raise RuntimeError(f"{case}: timed out or guard/Fusion completion unconfirmed; no further modeling dispatched")
        self.probe("after_" + case)
        self.verify_public_inputs(row)
        if getattr(self, "unified_gateway", False) and not result["returncode"]:
            receipt = strict_json(self.run / "audit" / case / "submission-receipt.json")
            if receipt.get("f3d_sha256") != sha(workspace / "submission.f3d") or receipt.get("report_sha256") != sha(workspace / "worker_report.json"):
                raise RuntimeError("submitted files changed after worker freeze")
        if result["returncode"] or not (workspace / "submission.f3d").is_file():
            row["state"] = "candidate_missing"
            self.save()
            return
        # This records only declared self-contained source integrity; the independent
        # Fusion import below is what checks external references and document health.
        dependency = {"schema_version": 1, "case_id": case, "attempt_id": "attempt-1",
                      "inspection_method": "Task requires self-contained F3D; external references remain unverified until trusted reopen",
                      "reviewer": "fusion-benchmark-runner/source-contract", "dependencies": []}
        if (workspace / "dependency_manifest.json").exists():
            shutil.copyfile(workspace / "dependency_manifest.json", self.run / "audit" / case / "agent_dependency_manifest.json")
        atomic(workspace / "dependency_manifest.json", dependency)
        self.scheduler("transition", self.ledger, case, "artifact_staged")
        self.scheduler("freeze-candidate", self.ledger, case)
        row.update(state="candidate_frozen", f3d_sha256=sha(workspace / "submission.f3d"))
        self.save()

    def model_cad(self, row):
        from .input_assets import iter_assets
        from .native_contract import validate_native_descriptor, file_hash
        self.check_cancel()
        self.verify_source()
        self.verify_public_inputs(row)
        case, workspace = row['case_id'], Path(row['workspace'])
        self.state['stage'] = 'modeling'
        self.probe('before_' + case)
        self.scheduler('transition', self.ledger, case, 'case_queued')
        self.scheduler('transition', self.ledger, case, 'modeling')
        row.update(state='modeling', started_at_utc=utc())
        self.save()
        reading = []
        for asset in iter_assets(row['input_manifest']):
            tool = 'read_image' if asset['mime'].startswith('image/') else ('parse_dxf' if asset['mime'] == 'application/dxf' else 'read_input')
            reading.append(tool + '(' + ('name' if tool == 'read_input' else 'id') + '=' + asset['id'] + ')')
        spec = self.cad_adapter.submission_spec()
        configuration = ('submit必须传configuration，指定保存的实际零件配置名称，可信重开必须激活同一配置。'
                         if self.cad_backend == 'solidworks' else 'Fusion configuration不适用，submit不传configuration。')
        prompt = f'''你是本题独立{self.cad_backend}建模Agent，仅处理{case}。用户已委托完整建模，不要提问或委派。
本次运行合同只替换历史TASK的软件/工件交付指令：cad_backend={self.cad_backend}、native_filename={spec['filename']}优先于历史TASK中指定的CAD软件或文件格式。原始几何、尺寸、单位和附件内容完全保留，不修改原始输入字节；软件/格式覆盖不得改变建模要求。
只使用benchmark MCP。先get_contract、list_inputs、read_input(name=cad_skill)，再按清单顺序读取：{'；'.join(reading)}。必须读取全部图片，工程尺寸以图纸标注为准，像素不是毫米。
cad_tools提供实际工具schema，所有建模经cad_call使用所选CAD原生工具，不使用现成答案或外部几何库。只访问本题公开输入及当前工作区{workspace}，禁止读取其他题、GT、评测程序和父目录。
本题1800秒、200次受控MCP请求、一次attempt。明确完成的API错误可修正；结果未知立即停止，不重发。参数计划与其他治理调用同样计入预算。
用户委托实现细节和缺失信息的合理假设；明确给定尺寸不可替换。假设/冲突/简化必须写入报告，不能宣称用户逐项确认。使用benchmark:delegated时绑定合同或公开TASK的SHA256；公开尺寸绑定附件ID/哈希及标注依据，不得引用私有GT。
开始时CAD应无用户文档，只创建、保存、关闭本题自己的文档。保存非空自包含{workspace / spec['filename']}，检查实体、特征和文件归属，关闭本题文档；不要关闭或修改其他文档。{configuration}
write_report记录实际模型、尺寸/单位、实体/特征、假设、简化、错误和保存结果。最后submit冻结原生文件和报告，成功后不再调用任何工具。维护端独立重开、可信STEP导出和评分。'''
        result = self.launch(workspace, self.run / 'audit' / case, prompt)
        row['execution'] = {key: value for key, value in result.items() if key != 'guard'}
        row['ended_at_utc'] = utc()
        guard, cleanup = result.get('guard', {}), result.get('process_cleanup', {})
        # A candidate can finish its explanation without opening CAD (for
        # example, an unreadable drawing). This is a missing submission, not an
        # unknown native operation. Require both process and desktop evidence.
        if (result['guard_classification'] == 'submission_unconfirmed'
                and result['timed_out'] is False and not result.get('cancelled')
                and not self._processes and cleanup.get('verified_stopped') is True
                and type(cleanup.get('remaining_processes')) is int and cleanup['remaining_processes'] == 0
                and type(guard.get('cad_calls')) is int and guard['cad_calls'] == 0
                and type(guard.get('pending_cad')) is int and guard['pending_cad'] == 0
                and not guard.get('pending_fusion') and not guard.get('cad_workspace_unconfirmed')
                and guard.get('submitted') is False and guard.get('owned_documents') == []):
            self.probe('after_' + case)
            self.verify_public_inputs(row)
            row.update(state='candidate_missing', failure_stage='submission',
                       failure_reason='no_submission_without_cad_calls')
            self.save()
            return
        if result['timed_out'] or result['guard_classification'] != 'clear':
            row.update(state='reconciliation_required', failure_stage='modeling',
                       failure_reason=result['guard_classification'])
            self.save()
            raise RuntimeError(case + ': ' + result['guard_classification'] + '; no further modeling dispatched')
        if self._processes:
            raise RuntimeError('candidate process cleanup incomplete; cannot freeze submission')
        cleanup = result.get('process_cleanup') or {}
        if cleanup.get('verified_stopped') is not True or cleanup.get('remaining_processes') != 0:
            raise RuntimeError('candidate process tree exit was not confirmed; cannot freeze submission')
        self.state['stage'] = 'submission'
        self.probe('after_' + case)
        self.verify_public_inputs(row)
        if result['returncode']:
            row.update(state='candidate_missing', failure_stage='modeling')
            self.save()
            return
        receipt = strict_json(self.run / 'audit' / case / 'submission-receipt.json')
        identity = self.case_identity(row)
        if (receipt.get('schema') != 'cad-submission/1' or receipt.get('status') != 'submitted'
                or any(receipt.get(key) != value for key, value in identity.items())):
            raise ValueError('submission receipt identity mismatch')
        native = receipt.get('native_document')
        validate_native_descriptor(native, identity=identity, path=workspace / spec['filename'])
        if file_hash(workspace / 'dependency_manifest.json') != native['dependency_sha256']:
            raise ValueError('submission dependency evidence changed')
        if file_hash(workspace / 'worker_report.json') != receipt.get('report_sha256'):
            raise ValueError('submitted worker report changed')
        native_path = workspace / 'native_document.json'
        if native_path.exists():
            raise ValueError('native descriptor target already exists; preserve evidence')
        with native_path.open('x', encoding='utf-8') as stream:
            json.dump(native, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write('\n')
        self.scheduler('transition', self.ledger, case, 'artifact_staged')
        self.scheduler('freeze-candidate', self.ledger, case)
        row.update(state='candidate_frozen', native_document=native)
        self.save()

    def export(self, row):
        if getattr(self, 'cad_backend', None) and self.unified_gateway:
            return self.export_cad(row)
        from .fusion_scripts import trusted_reopen_script
        self.verify_source()
        case = row["case_id"]
        self.probe("before_export_" + case)
        f3d = Path(row["workspace"]) / "submission.f3d"
        folder = self.run / "trusted_export" / case
        folder.mkdir(parents=True, exist_ok=False)
        step = folder / "trusted_export.step"
        report = folder / "reopen_report.json"
        script = trusted_reopen_script(str(f3d), str(step), str(report), row["f3d_sha256"])
        (folder / "trusted_reopen.py").write_text(script, encoding="utf-8")
        row["state"] = "trusted_reopen_running"
        self.save()
        result = self.mcp.execute(script, readonly=False)
        atomic(folder / "mcp_response.json", result)
        data = extract_result(result)
        self.probe("after_export_" + case)
        atomic(folder / "returned_report.json", data)
        if data.get("ok") is not True or data.get("status") != "passed" or not step.is_file() or not step.stat().st_size:
            row.update(state="trusted_export_failed", error=str(data.get("error", "health/export gate failed")))
        elif sha(f3d) != row["f3d_sha256"]:
            row.update(state="source_changed", error="source F3D changed after freeze")
        else:
            saved_report = strict_json(report)
            if saved_report != data or data.get("step_export", {}).get("sha256") != sha(step):
                raise ValueError("trusted export report/STEP does not match returned evidence")
            row.update(state="trusted_exported", trusted_step=str(step), trusted_step_sha256=sha(step),
                       trusted_report_sha256=sha(report), artifact_frozen=True)
        self.save()

    def candidate_template_evidence(self, row):
        """Bind helper records from this attempt's fixed private CAD audit directory."""
        from .native_contract import file_hash
        identity = self.case_identity(row)
        audit = self.run / 'audit' / row['case_id']
        contract_path = audit / 'worker-contract.json'
        contract_hash = file_hash(contract_path)
        contract = strict_json(contract_path)
        if (contract.get('schema') != 'cad-worker/1' or any(contract.get(key) != value for key, value in identity.items())
                or file_hash(contract_path) != contract_hash):
            raise ValueError('template evidence worker contract identity changed')
        evidence = []
        for path in sorted((audit / 'cad').glob('template-selection-*.json')):
            digest = file_hash(path)
            record = strict_json(path)
            template = record.get('template') if isinstance(record, dict) else None
            if (not isinstance(template, dict) or record.get('tool') not in {'create_new_part', 'new_part'}
                    or type(record.get('worker_pid')) is not int or record['worker_pid'] <= 0
                    or not isinstance(template.get('path'), str) or not Path(template['path']).is_absolute()
                    or Path(template['path']).suffix.lower() != '.prtdot'
                    or not isinstance(template.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', template['sha256'])
                    or type(template.get('bytes')) is not int or template['bytes'] <= 0
                    or template.get('source') not in {'vendor_template_discovery', 'verified_instance_version_standard_directory'}
                    or not isinstance(template.get('instance_version'), str) or not template['instance_version']
                    or file_hash(path) != digest):
                raise ValueError('template selection evidence is invalid or changed')
            # template.path is historical helper evidence, never a file input.
            evidence.append({'filename': path.name, 'sha256': digest, 'record': record})
        if not evidence:
            raise ValueError('candidate template selection evidence is missing')
        return {'schema': 'cad-template-selection/1', **identity, 'native_document': row['native_document'],
                'worker_contract_sha256': contract_hash, 'evidence': evidence}

    def export_cad(self, row):
        from .cad import create_adapter
        from .native_contract import validate_native_descriptor, file_hash, plain_file
        self.verify_source()
        self.state['stage'] = 'trusted_reopen'
        identity = self.case_identity(row)
        native = row['native_document']
        source = Path(row['workspace']) / native['path']
        validate_native_descriptor(native, identity=identity, path=source)
        if file_hash(Path(row['workspace']) / 'dependency_manifest.json') != native['dependency_sha256']:
            raise ValueError('frozen dependency evidence changed')
        if self._processes:
            raise RuntimeError('candidate process cleanup incomplete; cannot export')
        template_evidence = self.candidate_template_evidence(row) if self.cad_backend == 'solidworks' else None
        self.scheduler('verify', self.ledger.parent, self.ledger.name, self.ledger / 'frozen' / row['case_id'] / 'artifact_manifest.json')
        self.probe('before_export_' + row['case_id'])
        folder = self.run / 'trusted_export' / row['case_id']
        row['state'] = 'trusted_reopen_running'
        self.save()
        adapter = create_adapter(self.config, workspace=Path(row['workspace']), audit_dir=self.run / 'audit' / row['case_id'] / 'trusted-cad', audit=self.event)
        try:
            result = adapter.trusted_reopen_export(native, folder, identity)
        finally:
            adapter.close()
        self.state['stage'] = 'trusted_export'
        self.probe('after_export_' + row['case_id'])
        if result.get('status') != 'passed' or result.get('native_document') != native:
            raise ValueError('trusted export native evidence mismatch')
        step, report = plain_file(result['trusted_step']), plain_file(result['trusted_report'])
        if not step.resolve().is_relative_to(folder.resolve()) or not report.resolve().is_relative_to(folder.resolve()):
            raise ValueError('trusted export evidence escaped its maintenance folder')
        validate_native_descriptor(native, identity=identity, path=source)
        saved = strict_json(report)
        if (file_hash(step) != result.get('trusted_step_sha256') or file_hash(report) != result.get('trusted_report_sha256')
                or saved != result.get('report') or saved.get('status') != 'passed' or saved.get('ok') is not True):
            raise ValueError('trusted export report/STEP hash evidence mismatch')
        if self.cad_backend == 'solidworks' and (any(saved.get(key) != value for key, value in identity.items()) or saved.get('native_document') != native):
            raise ValueError('trusted reopen report identity mismatch')
        receipt = {'schema': 'cad-trusted-export/1', **identity, 'native_document': native,
                   'trusted_step_sha256': result['trusted_step_sha256'], 'trusted_report_sha256': result['trusted_report_sha256']}
        if template_evidence is not None:
            if self.candidate_template_evidence(row) != template_evidence:
                raise ValueError('candidate template evidence changed during trusted export')
            template_path = folder / 'template-selection-receipt.json'
            with template_path.open('x', encoding='utf-8') as stream:
                json.dump(template_evidence, stream, ensure_ascii=False, allow_nan=False, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            receipt['template_selection_receipt_sha256'] = file_hash(template_path)
        with (folder / 'export-receipt.json').open('x', encoding='utf-8') as stream:
            json.dump(receipt, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        from .reporting import trusted_export_metadata
        cad_identity = {**self.state.get('cad_identity', {}), **getattr(adapter, 'identity', {})}
        reopened_version = (saved.get('native_reopen') or {}).get('fusion_version')
        if self.cad_backend == 'fusion' and isinstance(reopened_version, str) and reopened_version:
            cad_identity['version'] = reopened_version
        row.update(state='trusted_exported', trusted_step=str(step), trusted_step_sha256=result['trusted_step_sha256'],
                   trusted_report=str(report), trusted_report_sha256=result['trusted_report_sha256'], artifact_frozen=True,
                   export_receipt_sha256=file_hash(folder / 'export-receipt.json'),
                   cad_identity=cad_identity,
                   trusted_export=trusted_export_metadata(saved))
        if template_evidence is not None:
            row['template_selection_receipt_sha256'] = receipt['template_selection_receipt_sha256']
            row['trusted_export']['template_evidence_count'] = len(template_evidence['evidence'])
        self.state['cad_capabilities']['trusted_export'] = {'executed': True, 'status': 'passed'}
        self.save()

    def verify_cad_export(self, row):
        from .native_contract import validate_native_descriptor, file_hash
        native, identity = row['native_document'], self.case_identity(row)
        workspace = Path(row['workspace'])
        validate_native_descriptor(native, identity=identity, path=workspace / native['path'])
        if file_hash(workspace / 'dependency_manifest.json') != native['dependency_sha256']:
            raise ValueError('native dependency identity changed after trusted export')
        receipt_path = self.run / 'trusted_export' / row['case_id'] / 'export-receipt.json'
        if file_hash(receipt_path) != row['export_receipt_sha256']:
            raise ValueError('trusted export receipt changed')
        receipt = strict_json(receipt_path)
        if (receipt.get('schema') != 'cad-trusted-export/1' or any(receipt.get(key) != value for key, value in identity.items())
                or receipt.get('native_document') != native):
            raise ValueError('trusted export identity mismatch')
        for role in ('trusted_step', 'trusted_report'):
            if file_hash(row[role]) != row[role + '_sha256'] or receipt.get(role + '_sha256') != row[role + '_sha256']:
                raise ValueError('trusted export hash changed: ' + role)
        if self.cad_backend == 'solidworks':
            template_path = receipt_path.with_name('template-selection-receipt.json')
            digest = file_hash(template_path)
            if (digest != row.get('template_selection_receipt_sha256')
                    or digest != receipt.get('template_selection_receipt_sha256')
                    or strict_json(template_path) != self.candidate_template_evidence(row)):
                raise ValueError('template selection receipt or candidate evidence changed')
        return receipt

    def evaluate(self):
        self.verify_source()
        self.verify_evaluator()
        valid = [r for r in self.state["cases"] if r["state"] == "trusted_exported"]
        if not valid:
            return
        for row in valid:
            case = row["case_id"]
            self.verify_public_inputs(row)
            self.scheduler("verify", self.ledger.parent, self.ledger.name, self.ledger / "frozen" / case / "artifact_manifest.json")
            if getattr(self, 'cad_backend', None) and self.unified_gateway:
                self.verify_cad_export(row)
        cases = []
        for row in valid:
            ref = self.references[row["case_id"]]
            for role in ("gt", "annotations"):
                if sha(ref[role]) != ref["expected_hashes"][role]:
                    raise ValueError("private reference changed during modeling; reject evaluation")
            cases.append({"case": row["case_id"], "candidate": row["trusted_step"], "gt": ref["gt"],
                          "annotations": ref["annotations"], "expected_hashes": {
                              "candidate": row["trusted_step_sha256"], "gt": ref["expected_hashes"]["gt"],
                              "annotations": ref["expected_hashes"]["annotations"]}})
        manifest = self.private / "evaluation_cases.json"
        atomic(manifest, {"cases": cases})
        if getattr(self, 'cad_backend', None) and self.unified_gateway:
            source_path = self.private / 'evaluation-source.json'
            with source_path.open('x', encoding='utf-8') as stream:
                json.dump({'schema': 'cad-scoring-source/1', 'run_id': self.run.name,
                           'evaluation_cases_sha256': sha(manifest), 'cases': [self.verify_cad_export(row) for row in valid]},
                          stream, ensure_ascii=False, allow_nan=False, indent=2)
                stream.write('\n')
            self.state['evaluation_source_sha256'] = sha(source_path)
            for row in valid:
                row['evaluation_source_sha256'] = self.state['evaluation_source_sha256']
        output = self.private / "evaluation"
        protocol, coordinate_mode, weights_file = scoring_options(self.config, self.evaluator)
        command = [self.config["python"], '-X', 'utf8', '-B', str(self.evaluator / "evaluate.py"), "--cases", str(manifest),
                   "--output", str(output), "--workers", "1", "--timeout", "600", "--protocol", protocol]
        if protocol == 'fusion-step-adapted-v4.0':
            command += ['--coordinate-mode', coordinate_mode]
        with (self.private / "evaluation.log").open("x", encoding="utf-8") as log:
            env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
            result = subprocess.run(command,
                                    cwd=self.evaluator, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=len(cases) * 660 + 120)
        self.state["evaluation"] = {"returncode": result.returncode, "directory": str(output)}
        self.verify_evaluator()
        self.verify_source()
        for row in valid:
            self.verify_public_inputs(row)
            if getattr(self, 'cad_backend', None) and self.unified_gateway:
                self.verify_cad_export(row)
                self.scheduler('verify', self.ledger.parent, self.ledger.name, self.ledger / 'frozen' / row['case_id'] / 'artifact_manifest.json')
        if getattr(self, 'cad_backend', None) and self.unified_gateway:
            if sha(self.private / 'evaluation-source.json') != self.state['evaluation_source_sha256']:
                raise ValueError('CAD scoring source changed during evaluation')
        summary = validate_evaluation(output, manifest, result.returncode, sha(weights_file), protocol=protocol, coordinate_mode=coordinate_mode)
        self.state["batch_mean"] = summary["batch_mean"] if len(cases) == len(self.state["cases"]) else None
        self.state["scoring_protocol"] = protocol
        self.state['coordinate_mode'] = coordinate_mode
        self.state["participating_case_count"] = summary.get("participating_case_count")
        by_id = {r["case"]: r for r in summary["cases"]}
        for row in valid:
            scored = by_id[row["case_id"]]
            row.update(state="evaluation_complete", final_score=scored["final_score"],
                       evaluation_status=scored["status"], evaluation_complete=True,
                       failure=scored.get("failure"), scores=scored.get("scores"),
                       validation_passed=scored.get("validation_passed"),
                       participates_in_scoring=scored.get("participates_in_scoring"),
                       score_kind=scored.get("score_kind"), metrics=scored.get("metrics"),
                       coordinate_mode=scored.get('coordinate_mode'),
                       pose={'status':scored.get('pose',{}).get('status')},
                       evaluation_execution=scored.get('execution'),
                       measurement_weight_coverage=scored.get('measurement_weight_coverage'),
                       diagnostic_unavailable_metrics=scored.get('diagnostic_unavailable_metrics'))
        self.save()

    def report(self):
        lines = ["# CLI + " + self.state.get('cad_backend', 'fusion') + " 自动运行报告", "", f"Run: `{self.run.name}`", "",
                 f"模型：`{self.state.get('model', MODEL)}`。运行状态：`{self.state['state']}`。", "",
                 "本次为同一 Windows 账户下的可信内部试运行，不是 OS 隔离盲测；排名资格始终 false。",
                 "建模、导出和评分均由 runner 自动编排，没有人工修模或逐题分数反馈。", "",
                 "|题目|流水线状态|耗时秒|评测状态|分数 /100|", "|---|---|---:|---|---:|"]
        for row in self.state["cases"]:
            score = row.get("final_score")
            score_text = f"{score*100:.3f}" if score is not None else "—"
            elapsed = row.get("execution", {}).get("elapsed_s")
            lines.append(f"|{row['case_id']}|{row['state']}|{elapsed:.1f}|{row.get('evaluation_status', '未评分')}|{score_text}|" if elapsed is not None
                         else f"|{row['case_id']}|{row['state']}|—|未评分|—|")
        is_v3 = self.state.get('scoring_protocol') in ('fusion-step-adapted-v3.0','fusion-step-adapted-v4.0')
        if self.state.get('scoring_protocol') == 'fusion-step-adapted-v4.0':
            lines.extend(['', 'v4 坐标模式：' + self.state.get('coordinate_mode','unknown') + '；shape/dimension/mass 计分，其他指标与坐标偏差单列诊断。', '',
                          '|题目|主分实测权重覆盖|对齐诊断状态|诊断未测|评测执行|','|---|---:|---|---|---|'])
            for row in self.state['cases']:
                coverage=row.get('measurement_weight_coverage')
                coverage_text=f'{100*coverage:.1f}%' if isinstance(coverage,(int,float)) else '—'
                lines.append('|'+row['case_id']+'|'+coverage_text+'|'+str((row.get('pose') or {}).get('status','未测得'))
                             +'|'+(', '.join(row.get('diagnostic_unavailable_metrics') or []) or '无')
                             +'|'+str((row.get('evaluation_execution') or {}).get('status','未执行'))+'|')
        lines.extend(["", "所有题逐行保留；validation通过的产物全部参与相应协议的固定权重评分，缺失正式计分测量明确列为未测得并保守计分。" if is_v3 else "所有题逐行保留；v2测量阻断仍保留无分终态。",
                      "均分和参与数量以私有评测summary.json为准；批次完成与完整实测覆盖分别报告。", "",
                      "完整运行证据：pipeline-status.json、runner-events.jsonl、audit/、ledger/、trusted_export/。",
                      "私有评测目录含 GT/标注快照，只由评测维护端访问。"])
        if self.state.get("error"):
            lines.extend(["", "停止原因：" + self.state["error"]])
        if self.state.get('cad_backend'):
            from src.report_data import project_cad_provenance
            lines.extend(['', 'CAD 来源与阶段（维护端记录）：', '```json',
                          json.dumps({'run': project_cad_provenance(self.state),
                                      'cases': [{'case_id': row['case_id'], **project_cad_provenance(row)} for row in self.state['cases']]},
                                     ensure_ascii=False, indent=2), '```'])
        (self.run / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        # HTML is a private display artifact, never part of score publication.
        if getattr(self, 'private', None) is not None:
            try:
                from .reporting import build_private_report
                self.html_report_result = build_private_report(self.private, self.state,
                                                               config=getattr(self, 'config', {}))
            except Exception:
                self.html_report_result = {'status': 'failed'}

    def check_cancel(self):
        if self.config.get("cancel_file") and Path(self.config["cancel_file"]).exists():
            raise RuntimeError("cancellation requested; preserve Fusion state for reconciliation")

    def record_desktop_identity(self):
        lease = getattr(self, '_desktop_lease', None)
        if lease is None:
            return
        path, owner = lease
        current = strict_json(path)
        if current.get('owner') != owner:
            raise RuntimeError('desktop lease ownership changed; reconciliation required')
        current.update(cad_backend=self.state.get('cad_backend', 'fusion'),
                       cad_identity=self.state.get('cad_identity', {}),
                       attempts=[{key: row[key] for key in ('case_id', 'attempt_id') if key in row} for row in self.state['cases']])
        atomic(path, current)

    def run_all(self, *, preflight_only=False):
        lock = Path(self.config["runs_root"]) / ".fusion-benchmark.lock"
        acquired = False
        desktop_lock = Path(tempfile.gettempdir()) / 'fusion-benchmark-desktop.lock'
        desktop_acquired = False
        interrupted = None
        try:
            # Failure to create or flush the lock is a reported stop, never a reason
            # to remove another process's lock or continue dispatching work.
            with lock.open("x", encoding="utf-8") as stream:
                acquired = True
                json.dump({"pid": os.getpid(), "run": str(self.run), "started_at": utc()}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            if getattr(self, 'unified_gateway', False):
                # A single user desktop can be referenced through several local
                # endpoints and projects. One shared lease covers all of them.
                with desktop_lock.open('x', encoding='utf-8') as stream:
                    desktop_acquired = True
                    owner = uuid.uuid4().hex
                    self._desktop_lease = (desktop_lock, owner)
                    json.dump({'pid': os.getpid(), 'run': str(self.run), 'started_at': utc(), 'owner': owner,
                               'cad_backend': self.state.get('cad_backend', 'fusion')}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
            self.prepare()
            self.state['stage'] = 'model_smoke'
            self.smoke()
            if preflight_only:
                self.state["state"] = "preflight_passed"
            else:
                self.state["state"] = "modeling"
                self.save()
                for row in self.state["cases"]:
                    self.model(row)
                self.state["state"] = "trusted_export"
                self.save()
                for row in self.state["cases"]:
                    if row["state"] == "candidate_frozen":
                        self.check_cancel()
                        self.export(row)
                self.state["all_artifacts_frozen"] = all(r.get("artifact_frozen") for r in self.state["cases"])
                self.state["state"] = "evaluating"
                self.state['stage'] = 'scoring'
                self.save()
                self.evaluate()
                self.state["batch_complete"] = all(r.get("evaluation_complete") for r in self.state["cases"])
                self.state["state"] = "completed" if self.state["batch_complete"] else "completed_with_missing_candidates"
            self.verify_source()
            if self.state.get("evaluator_manifest_sha256"):
                self.verify_evaluator()
        except BaseException as error:
            failure_stage = self.state.get('stage', 'preparation')
            self.state.update(state="blocked", error=type(error).__name__ + ": " + str(error), batch_complete=False,
                              failure_stage=failure_stage)
            for row in self.state['cases']:
                if row.get('state') in ('modeling', 'reconciliation_required', 'trusted_reopen_running'):
                    row.setdefault('failure_stage', failure_stage)
            if not isinstance(error, Exception):
                interrupted = error
            try:
                self.event({"event": "pipeline_blocked", "error": self.state["error"]})
            except Exception as log_error:
                self.state["audit_error"] = type(log_error).__name__ + ": " + str(log_error)
        finally:
            if self.cleanup_processes():
                self.state.update(state="blocked", error="process tree cleanup incomplete", batch_complete=False)
            adapter = getattr(self, 'cad_adapter', None)
            if adapter is not None:
                try:
                    adapter.close()
                except Exception as error:
                    self.state.update(state='blocked', error='CAD transport cleanup incomplete: ' + type(error).__name__, batch_complete=False)
            if desktop_acquired:
                try:
                    if strict_json(desktop_lock).get('owner') != self._desktop_lease[1]:
                        raise ValueError('desktop lease ownership changed')
                except (OSError, ValueError, TypeError) as error:
                    self.state.update(state='blocked', error='desktop lease reconciliation required: ' + str(error), batch_complete=False)
            if interrupted is None:
                interrupted = getattr(self, "_cleanup_interrupt", None)
            self.state["ended_at_utc"] = utc()
            try:
                self.save()
                self.report()
            except BaseException as report_error:
                self.state.update(state="blocked", error="pipeline report persistence failed: " + str(report_error), batch_complete=False)
                if interrupted is None:
                    raise
                interrupted.add_note("Pipeline report persistence also failed: " + str(report_error))
            # An interruption, unknown Fusion task or failed publication retains the
            # owned lock for reconciliation. Existing locks are never removed here.
            if acquired and self.state["state"] != "blocked":
                lock.unlink()
            if desktop_acquired and self.state['state'] != 'blocked':
                if strict_json(desktop_lock).get('owner') != self._desktop_lease[1]:
                    raise RuntimeError('desktop lease ownership changed; retained for reconciliation')
                desktop_lock.unlink()
        if interrupted is not None:
            raise interrupted
        return 0 if self.state["state"] in {"completed", "preflight_passed"} else 2

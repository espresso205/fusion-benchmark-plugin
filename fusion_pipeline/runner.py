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
    names = {name for name in ("evaluate.py", "worker.py", "bootstrap.py", "scheduler.py", "weights.json", "weights_v3.json")
             if (source / name).is_file()}
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
    for name in ("src/__init__.py", "src/io_utils.py"):
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


def validate_evaluation(output, cases_path, returncode, weights_sha256):
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
    is_v3 = manifest.get("protocol_id") == "fusion-step-adapted-v3.0"
    eligible = all(row["final_score"] is not None for row in finals)
    participants = [r for r in finals if r.get("participates_in_scoring") and r["final_score"] is not None] if is_v3 else finals if eligible else []
    mean = math.fsum(row["final_score"] for row in participants) / len(participants) if participants else None
    successful = eligible or (is_v3 and bool(participants) and all(r["status"] in
                                 {"ok", "scored_with_unavailable_metrics", "candidate_invalid"} for r in finals))
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
        expected = by_id[case].get("metrics", {}).get(module, {}).get("score")
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
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=30)
    else:
        import signal
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=15)


class Trial:
    def __init__(self, config_path):
        self.config_path = Path(config_path).resolve()
        self.config = strict_json(self.config_path)
        self.provider = self.config.get("provider", "opencode")
        self.unified_gateway = self.config.get("execution_backend") == "unified_gateway"
        fields = ["python", "questions_root", "private_cases", "runs_root", "private_runs_root", "fusion_skill"]
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
        self.run = new_run(self.config["runs_root"])
        self.private = Path(self.config["private_runs_root"]) / self.run.name
        self.private.mkdir(parents=True, exist_ok=False)
        self.state = {"schema_version": "fusion-trial/1", "run_id": self.run.name, "started_at_utc": utc(),
                      "model": self.config["model"], "provider": self.provider, "state": "preparing", "isolation": "trusted_internal_same_windows_account",
                      "budget_scope": "mcp_tools_scope" if self.unified_gateway else "all_opencode_tools",
                      "rank_eligible": False, "all_artifacts_frozen": False, "batch_complete": False,
                      "runner_code_sha256": source_hashes(),
                      "cases": [{"case_id": case, "state": "not_started"} for case in cases]}
        self._processes = []
        if self.config.get("fusion_connection"):
            self.cli_config = {"mcp": {"autodeskfusion": self.config["fusion_connection"]}}
        else:
            self.cli_config = resolved_config(self.config["opencode"])
        self.mcp = None
        self.save()

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
        workspace = Path(row["workspace"])
        for name in ("TASK.md", "drawing.dxf"):
            public = self.ledger / "public" / row["case_id"] / name
            candidate_copy = workspace / name
            if not candidate_copy.is_file() or sha(candidate_copy) != sha(public):
                raise ValueError(f"working public input changed: {row['case_id']}/{name}")

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
        result = subprocess.run([self.config["python"], "-B", "-c",
                                "import sys,runpy;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('scheduler',run_name='__main__')",
                                str(self.evaluator), *map(str, args)],
                                cwd=self.evaluator, capture_output=True, encoding="utf-8", timeout=60)
        if result.returncode:
            raise RuntimeError("packaged scheduler failed: " + (result.stderr or result.stdout)[-2000:])
        return result.stdout.strip()

    def prepare(self):
        self.check_cancel()
        self.verify_source()
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
        if (self.evaluator / "weights_v3.json").exists():
            from src.robust_metrics import build_section_plan
            plans = {case: build_section_plan(ref['gt'], strict_json(ref['annotations']))
                     for case, ref in self.references.items()}
            atomic(self.private / "reference_plans.json", plans)
            self.state['reference_plans_sha256'] = sha(self.private / "reference_plans.json")
        self.ledger = Path(self.scheduler("create-run", self.run / "ledger", self.run.name))
        for row in self.state["cases"]:
            case = row["case_id"]
            source = Path(self.config["questions_root"]) / case
            self.scheduler("register-case", self.ledger, case, "--task", source / "TASK.md", "--drawing", source / "drawing.dxf")
            workspace = self.ledger / "candidate" / case
            workspace.mkdir(parents=True, exist_ok=False)
            for filename in ("TASK.md", "drawing.dxf"):
                shutil.copyfile(self.ledger / "public" / case / filename, workspace / filename)
            row["workspace"] = str(workspace)
            row["state"] = "queued"
        self.mcp = FusionMCP(self.cli_config, self.event, timeout=180)
        self.probe("initial")
        self.state["state"] = "ready"
        self.save()

    def probe(self, label):
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
        audit_dir.mkdir(parents=True, exist_ok=False)
        connection_file = self.private / "fusion-connection.json"
        if not connection_file.exists():
            atomic(connection_file, {"mcp": {"autodeskfusion": self.cli_config["mcp"]["autodeskfusion"]}})
        timeout = 180 if smoke else 1800
        contract = {"schema": "fusion-worker/1", "run_id": self.run.name, "case_id": workspace.name,
                    "attempt_id": "attempt-" + uuid.uuid4().hex, "workspace": str(workspace), "audit_dir": str(audit_dir),
                    "model": self.config["model"], "max_tool_calls": 20 if smoke else 200, "deadline": time.time() + timeout,
                    "budget_scope": "mcp_tools_scope", "public_files": ["TASK.md"] if smoke else ["TASK.md", "drawing.dxf"],
                    "fusion_skill": self.config["fusion_skill"], "connection_file": str(connection_file),
                    "cancel_file": self.config.get("cancel_file"), "smoke": smoke}
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
        elif gateway.get("pending_fusion"):
            classification = "fusion_task_unknown"
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
        result = self.launch(workspace, self.run / "audit" / "smoke", prompt, smoke=True)
        atomic(self.run / "smoke-result.json", result)
        if result["returncode"] or result["timed_out"] or result["guard_classification"] != "clear":
            raise RuntimeError("guarded OpenCode read-only smoke failed; inspect smoke-result and audit")
        if getattr(self, "unified_gateway", False):
            events = [json.loads(line) for line in (self.run / "audit/smoke/gateway-events.jsonl").read_text(encoding="utf-8").splitlines()]
            if not result.get("fusion_tool_calls") or not any(event.get("type") == "fusion_result" and event.get("is_error") is False for event in events):
                raise RuntimeError("smoke has no successful Fusion result through gateway")
        else:
            events = [json.loads(line) for line in (self.run / "audit/smoke/guard-events.jsonl").read_text(encoding="utf-8").splitlines()]
            if not any("autodeskfusion" in str(event.get("tool", "")) for event in events):
                raise RuntimeError("smoke did not actually call the Fusion tool through the guard")
        self.probe("after_smoke")

    def model(self, row):
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
            prompt = f"""你是本题独立Fusion建模Agent，仅处理{case}。用户已授权完整自动运行，不要提问或委派。
只使用benchmark MCP工具，先get_contract确认单题合同，再read_input读取TASK.md、fusion_skill，parse_dxf解析drawing.dxf。fusion_tools提供实际Fusion工具schema，所有建模通过fusion_call执行Fusion原生API，不使用其他工具、外部几何库或现成答案。
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

    def export(self, row):
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
        output = self.private / "evaluation"
        protocol = self.config.get("protocol", "fusion-step-adapted-v3.0" if (self.evaluator / "weights_v3.json").exists() else "fusion-step-adapted-v2.0")
        weights_file = self.evaluator / ("weights_v3.json" if protocol == "fusion-step-adapted-v3.0" else "weights.json")
        with (self.private / "evaluation.log").open("x", encoding="utf-8") as log:
            result = subprocess.run([self.config["python"], '-B', str(self.evaluator / "evaluate.py"), "--cases", str(manifest),
                                     "--output", str(output), "--workers", "1", "--timeout", "600", "--protocol", protocol],
                                    cwd=self.evaluator, stdout=log, stderr=subprocess.STDOUT, timeout=len(cases) * 660 + 120)
        self.state["evaluation"] = {"returncode": result.returncode, "directory": str(output)}
        self.verify_evaluator()
        self.verify_source()
        for row in valid:
            self.verify_public_inputs(row)
        summary = validate_evaluation(output, manifest, result.returncode, sha(weights_file))
        self.state["batch_mean"] = summary["batch_mean"] if len(cases) == len(self.state["cases"]) else None
        self.state["scoring_protocol"] = protocol
        self.state["participating_case_count"] = summary.get("participating_case_count")
        by_id = {r["case"]: r for r in summary["cases"]}
        for row in valid:
            scored = by_id[row["case_id"]]
            row.update(state="evaluation_complete", final_score=scored["final_score"],
                       evaluation_status=scored["status"], evaluation_complete=True,
                       failure=scored.get("failure"), scores=scored.get("scores"),
                       validation_passed=scored.get("validation_passed"),
                       participates_in_scoring=scored.get("participates_in_scoring"),
                       score_kind=scored.get("score_kind"), metrics=scored.get("metrics"))
        self.save()

    def report(self):
        lines = ["# CLI + Fusion 自动运行报告", "", f"Run: `{self.run.name}`", "",
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
        is_v3 = self.state.get('scoring_protocol') == 'fusion-step-adapted-v3.0'
        lines.extend(["", "所有题逐行保留；v3 validation通过的产物全部参与固定权重评分，缺失测量明确列为未测得并保守计分。" if is_v3 else "所有题逐行保留；v2测量阻断仍保留无分终态。",
                      "均分和参与数量以私有评测summary.json为准；批次完成与完整实测覆盖分别报告。", "",
                      "完整运行证据：pipeline-status.json、runner-events.jsonl、audit/、ledger/、trusted_export/。",
                      "私有评测目录含 GT/标注快照，只由评测维护端访问。"])
        if self.state.get("error"):
            lines.extend(["", "停止原因：" + self.state["error"]])
        (self.run / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def check_cancel(self):
        if self.config.get("cancel_file") and Path(self.config["cancel_file"]).exists():
            raise RuntimeError("cancellation requested; preserve Fusion state for reconciliation")

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
                    json.dump({'pid': os.getpid(), 'run': str(self.run), 'started_at': utc()}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
            self.prepare()
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
                self.save()
                self.evaluate()
                self.state["batch_complete"] = all(r.get("evaluation_complete") for r in self.state["cases"])
                self.state["state"] = "completed" if self.state["batch_complete"] else "completed_with_missing_candidates"
            self.verify_source()
            if self.state.get("evaluator_manifest_sha256"):
                self.verify_evaluator()
        except BaseException as error:
            self.state.update(state="blocked", error=type(error).__name__ + ": " + str(error), batch_complete=False)
            if not isinstance(error, Exception):
                interrupted = error
            try:
                self.event({"event": "pipeline_blocked", "error": self.state["error"]})
            except Exception as log_error:
                self.state["audit_error"] = type(log_error).__name__ + ": " + str(log_error)
        finally:
            if self.cleanup_processes():
                self.state.update(state="blocked", error="process tree cleanup incomplete", batch_complete=False)
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
                desktop_lock.unlink()
        if interrupted is not None:
            raise interrupted
        return 0 if self.state["state"] in {"completed", "preflight_passed"} else 2

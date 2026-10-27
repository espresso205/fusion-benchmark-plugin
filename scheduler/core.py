"""Offline submission ledger. No CAD execution, isolation or scoring is implied.

checkpoint.json is authoritative; summary.json and events.jsonl are rebuilt views.
A per-run lock serializes operations. An abandoned lock requires operator review.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import time
import uuid

from src.io_utils import strict_json
from fusion_pipeline.input_assets import load_manifest, validate_manifest, verify_manifest, iter_assets, read_asset
from fusion_pipeline.native_contract import validate_native_descriptor, submission_spec

SCHEMA_VERSION = 2
STATES = ("preflight", "case_queued", "modeling", "artifact_staged", "candidate_frozen")
ALLOWED = dict(zip(STATES[:-2], STATES[1:-1]))
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}


class InvalidTransition(ValueError):
    pass


class RetryLimitExceeded(RuntimeError):
    pass


def _identity(value, label):
    if not isinstance(value, str) or not _ID_RE.fullmatch(value) or value.upper() in _RESERVED:
        raise ValueError(f"invalid {label}: {value!r}")


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonempty text")


def _sha(value):
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise ValueError("invalid sha256")


def _plain(path):
    """Reject reparse points before resolving anything, including parent junctions."""
    path = Path(path).absolute()
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"symlink/reparse point is not allowed: {part}")
    return path


def _hash(path):
    path = _plain(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size == 0:
        raise ValueError(f"expected a nonempty regular file without hardlink aliases: {path}")
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _encode(data):
    return (json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")


def validate_case_spec(spec):
    if not isinstance(spec, dict):
        raise ValueError("case spec must be an object")
    for field in ("case_id", "attempt_id"):
        _identity(spec.get(field), field)
    _text(spec.get("protocol_id"), "protocol_id")
    if type(spec.get("trusted_step_required")) is not bool:
        raise ValueError("trusted_step_required must be bool")
    return True


class Scheduler:
    def __init__(self, runs_root):
        self.runs_root = _plain(runs_root)
        self.run_dir = None
        self.run_id = None
        self._state = None
        self.last_error = None

    def create_run(self, run_id=None, *, cad_backend=None, cad_config_sha256=None):
        if cad_backend is not None:
            submission_spec(cad_backend)
            _sha(cad_config_sha256)
        elif cad_config_sha256 is not None:
            raise ValueError('CAD config digest requires cad_backend')
        if run_id is None:
            run_id = datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%S-%fZ-") + uuid.uuid4().hex[:8]
        _identity(run_id, "run_id")
        self.runs_root.mkdir(parents=True, exist_ok=True)
        run = _plain(self.runs_root / run_id)
        run.mkdir(exist_ok=False)
        self.run_id, self.run_dir = run_id, run
        self._state = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "revision": 0, "cases": {}, "events": []}
        if cad_backend is not None:
            self._state.update(schema_version=3, cad_backend=cad_backend, cad_config_sha256=cad_config_sha256)
        self._atomic("checkpoint.json", _encode(self._state), exclusive=True)
        self._write_views()
        return run

    @classmethod
    def load(cls, runs_root, run_id):
        _identity(run_id, "run_id")
        scheduler = cls(runs_root)
        scheduler.run_id = run_id
        scheduler.run_dir = _plain(scheduler.runs_root / run_id)
        with scheduler._transaction():
            pass
        return scheduler

    def _path(self, relative):
        if not isinstance(relative, str) or not relative or "\\" in relative:
            raise ValueError("expected a canonical relative path using forward slashes")
        parts = relative.split("/")
        for part in parts:
            if (not part or part in {".", ".."} or part.endswith((".", " "))
                    or any(ord(c) < 32 or c in ':<>"|?*' for c in part)
                    or part.split(".")[0].upper() in _RESERVED):
                raise ValueError(f"unsafe relative path: {relative}")
        if PurePosixPath(relative).is_absolute():
            raise ValueError("absolute artifact paths are not allowed")
        return _plain(self.run_dir.joinpath(*parts))

    def _atomic(self, relative, content, *, exclusive=False):
        target = self._path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.partial")
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if temporary.read_bytes() != content:
                raise OSError("atomic write readback mismatch")
            _plain(target)
            if exclusive:
                os.link(temporary, target)  # fails if the target already exists
            else:
                os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()

    @contextmanager
    def _transaction(self):
        if self.run_dir is None:
            raise RuntimeError("create or load a run first")
        lock = self._path(".scheduler.lock")
        try:
            stream = lock.open("x", encoding="utf-8")
        except FileExistsError as error:
            raise RuntimeError(f"run is locked: {lock}; inspect owner before removing an abandoned lock") from error
        try:
            with stream:
                stream.write(json.dumps({"pid": os.getpid(), "created_at_utc": datetime.now(timezone.utc).isoformat()}))
                stream.flush()
                os.fsync(stream.fileno())
            self._state = strict_json(self._path("checkpoint.json"))
            self._validate_checkpoint()
            before = deepcopy(self._state)
            self._refresh_validity()
            refreshed = deepcopy(self._state)
            try:
                yield
            except BaseException:
                # A rejected operation must not leave newly discovered corruption
                # hidden, nor commit any of that operation's partial in-memory edits.
                self._state = refreshed
                self._commit(before)
                raise
            else:
                self._commit(before)
        finally:
            lock.unlink()

    def _commit(self, before):
        if self._state != before:
            self._state["revision"] += 1
            self._atomic("checkpoint.json", _encode(self._state))
        self._write_views()

    def _validate_checkpoint(self):
        data = self._state
        fields = {"schema_version", "run_id", "revision", "cases", "events"}
        modern = isinstance(data, dict) and data.get('schema_version') == 3
        if modern:
            fields |= {'cad_backend', 'cad_config_sha256'}
        if not isinstance(data, dict) or set(data) != fields:
            raise ValueError("invalid checkpoint; legacy summary-only runs require a new run")
        if type(data["schema_version"]) is not int or data["schema_version"] not in (SCHEMA_VERSION, 3) or data["run_id"] != self.run_id:
            raise ValueError("checkpoint schema/run identity mismatch")
        if modern:
            submission_spec(data['cad_backend'])
            _sha(data['cad_config_sha256'])
        if type(data["revision"]) is not int or data["revision"] < 0:
            raise ValueError("invalid checkpoint revision")
        if not isinstance(data["cases"], dict) or not isinstance(data["events"], list):
            raise ValueError("invalid checkpoint cases/events")
        seen = set()
        for case_id, record in data["cases"].items():
            _identity(case_id, "case_id")
            if case_id.casefold() in seen:
                raise ValueError("case IDs must also be unique on Windows")
            seen.add(case_id.casefold())
            if not isinstance(record, dict):
                raise ValueError("invalid case record")
            contract = record.get("contract")
            validate_case_spec(contract)
            contract_fields = {"case_id", "attempt_id", "protocol_id", "trusted_step_required", "inputs"}
            if modern:
                contract_fields |= {'cad_backend', 'cad_config_sha256'}
                if any(contract.get(key) != data[key] for key in ('cad_backend', 'cad_config_sha256')):
                    raise ValueError('case CAD identity differs from run')
            if contract["case_id"] != case_id or set(contract) != contract_fields:
                raise ValueError("invalid registered contract")
            inputs = contract["inputs"]
            if isinstance(inputs, dict) and set(inputs) == {"task", "drawing"}:
                for role, filename in (("task", "TASK.md"), ("drawing", "drawing.dxf")):
                    self._validate_descriptor(inputs[role], f"public/{case_id}/{filename}")
            else:
                validate_manifest(inputs, case_id)
            if record.get("status") not in STATES:
                raise ValueError("unsupported offline state")
            if type(record.get("retries")) is not int or record["retries"] not in (0, 1):
                raise ValueError("invalid retry count")
            for flag in ("inputs_verified", "submission_verified", "artifact_frozen", "evaluation_complete"):
                if type(record.get(flag)) is not bool:
                    raise ValueError(f"invalid boolean: {flag}")
            if record["artifact_frozen"] or record["evaluation_complete"]:
                raise ValueError("offline ledger cannot certify CAD or evaluation completion")
            frozen = record.get("manifest_sha256")
            if frozen is not None:
                _sha(frozen)
            if (record["status"] == "candidate_frozen") != (frozen is not None):
                raise ValueError("frozen state requires a bound manifest digest")
            if record["submission_verified"] and frozen is None:
                raise ValueError("verified submission requires a manifest digest")
        for index, event in enumerate(data["events"], 1):
            if (not isinstance(event, dict) or type(event.get("sequence")) is not int
                    or event["sequence"] != index or event.get("run_id") != self.run_id):
                raise ValueError("invalid event sequence/run identity")
            case_id = event.get("case_id")
            if case_id not in data["cases"] or event.get("attempt_id") != data["cases"][case_id]["contract"]["attempt_id"]:
                raise ValueError("invalid event case/attempt")
            for field in ("event", "stage", "responsibility", "timestamp_utc"):
                _text(event.get(field), field)
            if event["stage"] not in STATES:
                raise ValueError("invalid event stage")
            if type(event.get("monotonic_s")) not in (int, float) or event["monotonic_s"] < 0:
                raise ValueError("invalid event time")
            if "request_id" not in event or (event["request_id"] is not None and not isinstance(event["request_id"], str)):
                raise ValueError("invalid request_id")

    def _validate_descriptor(self, value, expected_path):
        if not isinstance(value, dict) or set(value) != {"path", "sha256"} or value["path"] != expected_path:
            raise ValueError(f"expected file descriptor for {expected_path}")
        _sha(value["sha256"])

    def _descriptor(self, relative):
        return {"path": relative, "sha256": _hash(self._path(relative))}

    def _check_descriptor(self, descriptor, expected_path):
        self._validate_descriptor(descriptor, expected_path)
        if _hash(self._path(expected_path)) != descriptor["sha256"]:
            raise ValueError(f"file hash mismatch: {expected_path}")

    def _check_inputs(self, case_id, record):
        inputs = record["contract"]["inputs"]
        if set(inputs) != {"task", "drawing"}:
            validate_manifest(inputs, case_id)
            verify_manifest(inputs, self._path(f"public/{case_id}"))
            return
        for role, filename in (("task", "TASK.md"), ("drawing", "drawing.dxf")):
            self._check_descriptor(inputs[role], f"public/{case_id}/{filename}")

    def register_case(self, case_id, attempt_id="attempt-1", protocol_id="fusion-step-adapted-v2.0",
                      trusted_step_required=True, *, task=None, drawing=None, manifest=None):
        contract = dict(case_id=case_id, attempt_id=attempt_id, protocol_id=protocol_id,
                        trusted_step_required=trusted_step_required)
        validate_case_spec(contract)
        if manifest is not None and (task is not None or drawing is not None):
            raise ValueError("manifest and task/drawing registration are mutually exclusive")
        if manifest is None and (task is None or drawing is None):
            raise ValueError("registration requires actual public TASK.md and drawing.dxf files")
        # The old representation remains byte-for-byte compatible. New records
        # carry the versioned manifest itself; checkpoint.json remains authority.
        public_manifest = load_manifest(manifest, case_id) if manifest is not None else None
        with self._transaction():
            if any(case_id.casefold() == key.casefold() for key in self._state["cases"]):
                raise ValueError(f"duplicate case: {case_id}")
            if self._state['schema_version'] == 3:
                contract.update({key: self._state[key] for key in ('cad_backend', 'cad_config_sha256')})
            target = self._path(f"public/{case_id}")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise ValueError("public snapshot target already exists; inspect unregistered evidence")
            temporary_relative = f"public/.{case_id}.{uuid.uuid4().hex}.partial"
            temporary = self._path(temporary_relative)
            temporary.mkdir(exist_ok=False)
            published = False
            try:
                inputs = {} if public_manifest is None else deepcopy(public_manifest)
                if public_manifest is not None:
                    source_root = Path(manifest).parent
                    for descriptor in iter_assets(public_manifest):
                        _, content = read_asset(public_manifest, source_root, descriptor["id"])
                        self._atomic(f"{temporary_relative}/{descriptor['path']}", content, exclusive=True)
                    verify_manifest(public_manifest, source_root)
                    verify_manifest(public_manifest, temporary)
                else:
                    for role, source, filename in (("task", task, "TASK.md"), ("drawing", drawing, "drawing.dxf")):
                        source = _plain(source)
                        expected = _hash(source)
                        content = source.read_bytes()
                        if hashlib.sha256(content).hexdigest() != expected or _hash(source) != expected:
                            raise ValueError("public input changed during snapshot")
                        relative = f"{temporary_relative}/{filename}"
                        self._atomic(relative, content, exclusive=True)
                        if _hash(self._path(relative)) != expected:
                            raise ValueError("snapshot hash mismatch")
                        inputs[role] = {"path": f"public/{case_id}/{filename}", "sha256": expected}
                _plain(target)
                if target.exists():
                    raise ValueError("public snapshot target already exists")
                temporary.rename(target)
                published = True
                contract["inputs"] = inputs
                self._state["cases"][case_id] = {
                    "contract": contract, "status": "preflight", "retries": 0,
                    "manifest_sha256": None, "inputs_verified": True, "submission_verified": False,
                    "artifact_frozen": False, "evaluation_complete": False,
                }
                self._event(case_id, "registered")
            except BaseException:
                # Only this transaction's newly-created snapshot is removable.
                # Commit/view failures happen outside this block, retaining the
                # snapshot if the authoritative checkpoint may have committed.
                if published:
                    shutil.rmtree(_plain(target))
                raise
            finally:
                if temporary.exists():
                    shutil.rmtree(_plain(temporary))

    def register_case_spec(self, case_id, spec):
        validate_case_spec(spec)
        if spec["case_id"] != case_id:
            raise ValueError("case_id mismatch")
        self.register_case(case_id, spec["attempt_id"], spec["protocol_id"], spec["trusted_step_required"],
                           task=spec.get("task"), drawing=spec.get("drawing"), manifest=spec.get("manifest"))

    def _event(self, case_id, event, *, request_id=None, responsibility="scheduler", detail=None):
        record = self._state["cases"][case_id]
        row = {
            "sequence": len(self._state["events"]) + 1, "run_id": self.run_id, "case_id": case_id,
            "attempt_id": record["contract"]["attempt_id"], "stage": record["status"], "request_id": request_id,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(), "monotonic_s": time.monotonic(),
            "responsibility": responsibility, "event": event,
        }
        if detail is not None:
            row["detail"] = detail
        self._state["events"].append(row)

    def transition(self, case_id, new_status):
        with self._transaction():
            record = self._state["cases"][case_id]
            if ALLOWED.get(record["status"]) != new_status:
                raise InvalidTransition(f"{record['status']} -> {new_status}; freeze-candidate is required; live gates are unsupported")
            self._check_inputs(case_id, record)
            record["status"] = new_status
            self._event(case_id, new_status)

    def _dependencies(self, case_id, record):
        path = self._path(f"candidate/{case_id}/dependency_manifest.json")
        data = strict_json(path)
        if not isinstance(data, dict) or set(data) != {"schema_version", "case_id", "attempt_id", "inspection_method", "reviewer", "dependencies"}:
            raise ValueError("dependency manifest requires identity, inspection_method, reviewer and dependencies")
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise ValueError("unsupported dependency schema")
        if data["case_id"] != case_id or data["attempt_id"] != record["contract"]["attempt_id"]:
            raise ValueError("dependency manifest identity mismatch")
        _text(data["inspection_method"], "inspection_method")
        _text(data["reviewer"], "reviewer")
        if not isinstance(data["dependencies"], list):
            raise ValueError("dependencies must be a list")
        seen = set()
        for item in data["dependencies"]:
            if not isinstance(item, dict):
                raise ValueError("invalid dependency descriptor")
            relative = item.get("path")
            self._path(relative)
            if not relative.startswith(f"candidate/{case_id}/dependencies/") or relative.casefold() in seen:
                raise ValueError("dependencies must be unique files in this case's dependencies directory")
            seen.add(relative.casefold())
            self._check_descriptor(item, relative)
        return data

    def _manifest_relative(self, case_id):
        return f"frozen/{case_id}/artifact_manifest.json"

    def _native_document(self, case_id, record):
        contract = record['contract']
        identity = {'run_id': self.run_id, **{key: contract[key] for key in (
            'case_id', 'attempt_id', 'protocol_id', 'cad_backend', 'cad_config_sha256')}}
        data = strict_json(self._path(f'candidate/{case_id}/native_document.json'))
        native_path = self._path(f"candidate/{case_id}/{submission_spec(contract['cad_backend'])['filename']}")
        validate_native_descriptor(data, identity=identity, path=native_path)
        deps = self._dependencies(case_id, record)
        if data['dependency_sha256'] != _hash(self._path(f'candidate/{case_id}/dependency_manifest.json')):
            raise ValueError('native dependency evidence changed')
        if contract['cad_backend'] == 'solidworks' and deps['dependencies']:
            raise ValueError('SolidWorks v1 accepts only self-contained Parts')
        return data

    def freeze_candidate(self, case_id):
        with self._transaction():
            record = self._state["cases"][case_id]
            if record["status"] != "artifact_staged":
                raise InvalidTransition("freeze-candidate requires artifact_staged and cannot overwrite a frozen submission")
            self._check_inputs(case_id, record)
            self._dependencies(case_id, record)
            artifacts = {}
            modern = self._state['schema_version'] == 3
            files = [("dependency_manifest", "dependency_manifest.json")]
            if modern:
                artifacts['native_document'] = self._native_document(case_id, record)
            else:
                files.insert(0, ('f3d', 'submission.f3d'))
            for role, filename in files:
                artifacts[role] = {**self._descriptor(f"candidate/{case_id}/{filename}"), "required_by_protocol": True}
            raw = f"candidate/{case_id}/submission.step"
            if self._path(raw).exists():
                artifacts["raw_step"] = {**self._descriptor(raw), "required_by_protocol": False, "status": "present"}
            else:
                artifacts["raw_step"] = {"required_by_protocol": False, "status": "not_applicable",
                                         "reason": "not submitted; raw STEP is optional diagnostic evidence"}
            contract = record["contract"]
            manifest = {
                "schema_version": self._state['schema_version'], "kind": "candidate_submission", "run_id": self.run_id,
                "case_id": case_id, "attempt_id": contract["attempt_id"], "protocol_id": contract["protocol_id"],
                "inputs": deepcopy(contract["inputs"]), "artifacts": artifacts,
                "environment": {"verification_mode": "offline_file_integrity"},
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
            }
            if modern:
                manifest.update({key: contract[key] for key in ('cad_backend', 'cad_config_sha256')})
            relative = self._manifest_relative(case_id)
            self._atomic(relative, _encode(manifest), exclusive=True)
            record["manifest_sha256"] = _hash(self._path(relative))
            self._verify_submission(case_id, record)
            record["submission_verified"] = True
            record["status"] = "candidate_frozen"
            record.pop("verification_error", None)
            self._event(case_id, "candidate_frozen", detail={"verification_mode": "offline_file_integrity"})
        return self.run_dir / relative

    def _verify_submission(self, case_id, record):
        self._check_inputs(case_id, record)
        manifest_path = self._path(self._manifest_relative(case_id))
        if _hash(manifest_path) != record["manifest_sha256"]:
            raise ValueError("frozen manifest hash mismatch")
        data = strict_json(manifest_path)
        fields = {"schema_version", "kind", "run_id", "case_id", "attempt_id", "protocol_id", "inputs", "artifacts", "environment", "created_at_utc"}
        modern = self._state['schema_version'] == 3
        if modern:
            fields |= {'cad_backend', 'cad_config_sha256'}
        if not isinstance(data, dict) or set(data) != fields:
            raise ValueError("invalid submission manifest schema")
        contract = record["contract"]
        if type(data["schema_version"]) is not int or data["schema_version"] != self._state['schema_version'] or data["kind"] != "candidate_submission":
            raise ValueError("unsupported submission manifest")
        if data["run_id"] != self.run_id or any(data[key] != contract[key] for key in ("case_id", "attempt_id", "protocol_id", "inputs")):
            raise ValueError("manifest does not match registered run/case/attempt/protocol/inputs")
        if modern and any(data[key] != contract[key] for key in ('cad_backend', 'cad_config_sha256')):
            raise ValueError('frozen CAD identity mismatch')
        if data["environment"] != {"verification_mode": "offline_file_integrity"}:
            raise ValueError("invalid verification mode")
        _text(data["created_at_utc"], "created_at_utc")
        artifacts = data["artifacts"]
        roles = {'native_document' if modern else 'f3d', 'dependency_manifest', 'raw_step'}
        if not isinstance(artifacts, dict) or set(artifacts) != roles:
            raise ValueError("invalid artifact roles")
        files = [("dependency_manifest", "dependency_manifest.json")]
        if modern:
            if artifacts['native_document'] != self._native_document(case_id, record):
                raise ValueError('frozen native document changed')
        else:
            files.insert(0, ('f3d', 'submission.f3d'))
        for role, filename in files:
            item = deepcopy(artifacts[role])
            if not isinstance(item, dict) or item.pop("required_by_protocol", None) is not True:
                raise ValueError(f"{role} must be required")
            self._check_descriptor(item, f"candidate/{case_id}/{filename}")
        raw = deepcopy(artifacts["raw_step"])
        if not isinstance(raw, dict) or raw.pop("required_by_protocol", None) is not False:
            raise ValueError("raw_step must be explicitly optional")
        raw_status = raw.pop("status", None)
        relative = f"candidate/{case_id}/submission.step"
        if raw_status == "present":
            self._check_descriptor(raw, relative)
        elif raw_status == "not_applicable" and set(raw) == {"reason"}:
            _text(raw["reason"], "raw_step reason")
            if self._path(relative).exists():
                raise ValueError("raw STEP appeared after freeze")
        else:
            raise ValueError("invalid raw_step status")
        self._dependencies(case_id, record)

    def _refresh_validity(self):
        for case_id, record in self._state["cases"].items():
            inputs_were_valid = record["inputs_verified"]
            was_valid = record["submission_verified"]
            record.pop("verification_error", None)
            try:
                self._check_inputs(case_id, record)
                record["inputs_verified"] = True
            except (OSError, ValueError, TypeError, KeyError) as error:
                record["inputs_verified"] = False
                record["verification_error"] = str(error)
            record["submission_verified"] = False
            if record["manifest_sha256"] is not None and record["inputs_verified"]:
                try:
                    self._verify_submission(case_id, record)
                    record["submission_verified"] = True
                except (OSError, ValueError, TypeError, KeyError) as error:
                    record["verification_error"] = str(error)
            if inputs_were_valid != record["inputs_verified"]:
                self._event(case_id, "inputs_revalidated" if record["inputs_verified"] else "inputs_invalidated")
            if was_valid != record["submission_verified"]:
                self._event(case_id, "submission_revalidated" if record["submission_verified"] else "submission_invalidated")

    def verify_artifact_manifest(self, manifest_path):
        self.last_error = None
        with self._transaction():
            # Compare lexical paths first. Never infer authority from an external file's case name.
            target = Path(manifest_path).absolute()
            for case_id, record in self._state["cases"].items():
                if target == self.run_dir / self._manifest_relative(case_id):
                    valid = record["manifest_sha256"] is not None and record["submission_verified"]
                    self.last_error = None if valid else record.get("verification_error", "submission has not been frozen")
                    return bool(valid)
            self.last_error = "manifest is not a registered case's canonical manifest path"
            return False

    def retry(self, case_id, error_code, *, retryable, no_side_effects_confirmed=False):
        with self._transaction():
            record = self._state["cases"][case_id]
            if (retryable is not True or no_side_effects_confirmed is not True
                    or error_code not in {"plugin_unavailable", "connection_refused"}
                    or record["status"] not in {"preflight", "case_queued"} or record["retries"] >= 1):
                raise RetryLimitExceeded(f"retry denied for {case_id}: {error_code}")
            record["retries"] += 1
            self._event(case_id, "retry_authorized", detail={"error_code": error_code, "no_side_effects_confirmed": True,
                                                            "execution_performed": False})
        return True

    def _summary(self):
        cases = self._state["cases"]
        return {"schema_version": self._state['schema_version'], "run_id": self.run_id, "revision": self._state["revision"],
                "mode": "offline_file_integrity", "cases": deepcopy(cases),
                "all_submissions_verified": bool(cases) and all(c["submission_verified"] for c in cases.values()),
                "all_artifacts_frozen": False, "batch_complete": False, "rank_eligible": False}

    def _write_views(self):
        self._atomic("summary.json", _encode(self._summary()))
        rows = "".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in self._state["events"])
        self._atomic("events.jsonl", rows.encode("utf-8"))

    def status(self):
        with self._transaction():
            pass
        return self._summary()

    def case_status(self, case_id):
        return self.status()["cases"][case_id]["status"]

    def all_artifacts_frozen(self):
        return self.status()["all_artifacts_frozen"]

    def evaluation_complete(self, case_id):
        return self.status()["cases"][case_id]["evaluation_complete"]

    def batch_complete(self):
        return self.status()["batch_complete"]

    def set_evaluation_complete(self, case_id):
        raise InvalidTransition("offline scheduler has no trusted evaluation evidence adapter")

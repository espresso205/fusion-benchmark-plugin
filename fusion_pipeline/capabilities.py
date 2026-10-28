"""Static candidate capability selection, source pinning and public projection.

This module never launches a process or reads credential values.  Private source
paths and launch definitions stay in the maintenance manifest.  Snapshot creation
is exclusive: callers must allocate a fresh directory for every attempt.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
from typing import Any


SELECTION_SCHEMA = "candidate-capabilities/1"
REGISTRY_SCHEMA = "candidate-capability-registry/1"
MANIFEST_SCHEMA = "candidate-capability-manifest/1"
PUBLIC_SCHEMA = "candidate-capabilities-public/1"
LEGACY_SKILL_FIELDS = ("cad_skill", "modeling_skill", "fusion_skill")
_ID = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TOOL = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_RESERVED = frozenset({
    "benchmark", "get_contract", "list_inputs", "read_input", "read_image", "parse_dxf",
    "write_report", "submit", "list_skills", "read_skill", "list_tools", "call_tool",
    "fusion_call", "fusion_tools", "solidworks_call", "solidworks_tools",
    "cad_call", "cad_tools",
})
_SELECTION_FIELDS = frozenset({
    "schema", "study_kind", "skill_delivery", "exposure_mode", "skills", "mcp_servers",
})
_MEDIA = {".md": "text/markdown", ".txt": "text/plain", ".png": "image/png",
          ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
          ".webp": "image/webp"}


def _fail(code: str, detail: str = "") -> None:
    raise ValueError(code + (": " + detail if detail else ""))


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _object(value: Any, code: str, allowed: set | frozenset | None = None) -> dict:
    if not isinstance(value, dict) or (allowed is not None and set(value) - allowed):
        _fail(code)
    return value


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        _fail("invalid_capability_id")
    if value in _RESERVED:
        _fail("reserved_capability_id", value)
    return value


def _ids(value: Any, code: str) -> list[str]:
    if not isinstance(value, list):
        _fail(code)
    result = [_identifier(item) for item in value]
    if len(result) != len(set(result)):
        _fail("duplicate_capability_id")
    return result


def _strings(value: Any, code: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item or "\x00" in item
                                          for item in value) or (nonempty and not value):
        _fail(code)
    if len(value) != len(set(value)):
        _fail(code)
    return list(value)


def _safe_absolute(value: Any) -> Path:
    if not isinstance(value, (str, os.PathLike)) or not str(value) or "\x00" in str(value):
        _fail("invalid_capability_source_path")
    if any(part in (".", "..") for part in re.split(r"[/\\]", str(value))):
        _fail("invalid_capability_source_path")
    path = Path(value)
    if not path.is_absolute() or any(part in (".", "..") or part.endswith((" ", "."))
                                     for part in path.parts[1:]):
        _fail("invalid_capability_source_path")
    # Alternate data streams, device paths and network shares are outside the
    # explicit ordinary-file closure supported in the first version.
    if os.name == "nt" and (str(path).startswith("\\\\") or ":" in str(path)[2:]):
        _fail("invalid_capability_source_path")
    return path


def _check_no_reparse(path: Path, *, allow_missing_leaf: bool = False) -> os.stat_result | None:
    result = None
    for component in [*reversed(path.parents), path]:
        try:
            result = component.lstat()
        except FileNotFoundError:
            if allow_missing_leaf and component == path:
                return None
            _fail("capability_source_missing", str(component))
        if stat.S_ISLNK(result.st_mode) or getattr(result, "st_file_attributes", 0) & 0x400:
            _fail("capability_reparse_point", str(component))
    return result


def _read_file(value: Any) -> tuple[Path, bytes]:
    path = _safe_absolute(value)
    before = _check_no_reparse(path)
    if before is None or not stat.S_ISREG(before.st_mode):
        _fail("capability_source_not_file", str(path))
    if before.st_nlink > 1:
        _fail("capability_path_alias", str(path))
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            data = stream.read()
            after_open = os.fstat(stream.fileno())
    except OSError as exc:
        _fail("capability_source_unreadable", str(exc))
    after = _check_no_reparse(path)
    identities = {(s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
                  for s in (before, opened, after_open, after)}
    if len(identities) != 1:
        _fail("capability_source_changed", str(path))
    # Windows resolve expands ordinary aliases such as short file names.  Case
    # alone is permitted; aliases resolving to another spelling are rejected.
    if os.path.normcase(str(path.resolve())) != os.path.normcase(str(path)):
        _fail("capability_path_alias", str(path))
    return path, data


def _relative_resource(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or "\x00" in value:
        _fail("invalid_skill_resource_path")
    posix, windows = PurePosixPath(value), PureWindowsPath(value)
    parts = value.split("/")
    if posix.is_absolute() or windows.is_absolute() or any(
        not part or part in (".", "..") or part.endswith((".", " ")) for part in parts
    ) or str(posix) != value:
        _fail("invalid_skill_resource_path")
    if any(re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part) for part in parts):
        _fail("invalid_skill_resource_path")
    return value


def _static_data(path: Path, data: bytes, *, main: bool = False) -> str:
    media = _MEDIA.get(path.suffix.lower())
    if media is None or (main and not media.startswith("text/")):
        _fail("unsupported_skill_resource_type", str(path))
    if media.startswith("text/"):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            _fail("invalid_skill_utf8", str(path))
        if "\x00" in text:
            _fail("invalid_skill_utf8", str(path))
    elif media == "image/png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
        _fail("invalid_skill_image", str(path))
    elif media == "image/jpeg" and not data.startswith(b"\xff\xd8\xff"):
        _fail("invalid_skill_image", str(path))
    elif media == "image/gif" and not data.startswith((b"GIF87a", b"GIF89a")):
        _fail("invalid_skill_image", str(path))
    elif media == "image/webp" and not (data.startswith(b"RIFF") and data[8:12] == b"WEBP"):
        _fail("invalid_skill_image", str(path))
    return media


def _validate_registry(registry: dict) -> tuple[dict, dict[str, dict]]:
    _object(registry, "invalid_capability_registry",
            {"schema", "skills", "mcp_servers", "_registry_path", "_source_hashes"})
    if registry.get("schema") != REGISTRY_SCHEMA:
        _fail("unsupported_capability_registry_schema")
    skills = _object(registry.get("skills"), "invalid_capability_registry")
    servers = _object(registry.get("mcp_servers"), "invalid_capability_registry")
    validated = {"schema": REGISTRY_SCHEMA, "skills": {}, "mcp_servers": {}}
    sources: dict[str, dict] = {}

    def record(value: Any, expected: str | None = None) -> tuple[Path, bytes]:
        path, data = _read_file(value)
        sha = _sha(data)
        if expected is not None and sha != expected:
            _fail("capability_source_hash_mismatch", str(path))
        loaded_hash = registry.get("_source_hashes", {}).get(str(path))
        if loaded_hash is not None and sha != loaded_hash:
            _fail("capability_source_hash_mismatch", str(path))
        sources[str(path)] = {"sha256": sha, "bytes": data}
        return path, data

    for skill_id, raw in skills.items():
        _identifier(skill_id)
        definition = _object(raw, "invalid_skill_definition", {
            "path", "version", "resources", "cad_backends", "requires_mcp", "content_sha256", "description",
        })
        if not isinstance(definition.get("version"), str) or not definition["version"].strip():
            _fail("invalid_skill_version", skill_id)
        backends = _strings(definition.get("cad_backends"), "invalid_skill_cad_backends", nonempty=True)
        if set(backends) - {"fusion", "solidworks"}:
            _fail("invalid_skill_cad_backends", skill_id)
        _ids(definition.get("requires_mcp"), "invalid_skill_mcp_dependencies")
        declared = definition.get("content_sha256")
        if declared is not None and (not isinstance(declared, str) or not _DIGEST.fullmatch(declared)):
            _fail("invalid_skill_sha256", skill_id)
        path, data = record(definition.get("path"), declared)
        _static_data(path, data, main=True)
        resources = definition.get("resources")
        if not isinstance(resources, list):
            _fail("invalid_skill_resources", skill_id)
        resource_ids: set[str] = set()
        resource_paths = {path.name.casefold()}
        # Validate aliases before touching resources, to reject case aliases on
        # both case-sensitive and case-insensitive filesystems.
        for resource in resources:
            _object(resource, "invalid_skill_resource", {"id", "path", "sha256"})
            resource_id = _identifier(resource.get("id"))
            relative = _relative_resource(resource.get("path"))
            alias = relative.casefold()
            if resource_id in resource_ids or any(
                alias == prior or alias.startswith(prior + "/") or prior.startswith(alias + "/")
                for prior in resource_paths
            ):
                _fail("duplicate_skill_resource", skill_id)
            resource_ids.add(resource_id)
            resource_paths.add(relative.casefold())
            sha = resource.get("sha256")
            if not isinstance(sha, str) or not _DIGEST.fullmatch(sha):
                _fail("invalid_resource_sha256", resource_id)
        for resource in resources:
            resource_path = path.parent.joinpath(*resource["path"].split("/"))
            # Type rejection precedes a digest mismatch, so script resources
            # cannot become acceptable by merely changing their declared hash.
            if resource_path.suffix.lower() not in _MEDIA:
                _fail("unsupported_skill_resource_type", str(resource_path))
            resource_path, resource_data = record(resource_path, resource["sha256"])
            _static_data(resource_path, resource_data)
        if "description" in definition and not isinstance(definition["description"], str):
            _fail("invalid_skill_description")
        validated["skills"][skill_id] = copy.deepcopy(definition)

    for server_id, raw in servers.items():
        _identifier(server_id)
        definition = _object(raw, "invalid_mcp_definition")
        if server_id == "cad_native":
            if definition != {"kind": "builtin_cad_adapter"}:
                _fail("invalid_cad_native_definition")
        else:
            if definition.get("kind") != "stdio":
                _fail("unsupported_mcp_kind", server_id)
            _object(definition, "invalid_mcp_definition", {
                "kind", "command", "args", "source_files", "env_allowlist", "allowed_tools",
                "timeout_s", "max_response_bytes", "side_effect_policy",
            })
            record(definition.get("command"))
            args = definition.get("args")
            if not isinstance(args, list) or any(not isinstance(arg, str) or "\x00" in arg for arg in args):
                _fail("invalid_mcp_args", server_id)
            for source in _strings(definition.get("source_files"), "invalid_mcp_source_files", nonempty=True):
                record(source)
            env = _strings(definition.get("env_allowlist"), "invalid_mcp_env_allowlist")
            if any(not _ENV.fullmatch(name) for name in env):
                _fail("invalid_mcp_env_allowlist", server_id)
            tools = _strings(definition.get("allowed_tools"), "invalid_mcp_allowed_tools", nonempty=True)
            if any(not _TOOL.fullmatch(name) for name in tools):
                _fail("invalid_mcp_allowed_tools", server_id)
            timeout = definition.get("timeout_s")
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
                _fail("invalid_mcp_timeout", server_id)
            maximum = definition.get("max_response_bytes")
            if isinstance(maximum, bool) or not isinstance(maximum, int) or not 256 <= maximum <= 32 * 1024 * 1024:
                _fail("invalid_mcp_response_limit", server_id)
            if definition.get("side_effect_policy") != "read_only":
                _fail("unsupported_mcp_side_effect_policy", server_id)
        validated["mcp_servers"][server_id] = copy.deepcopy(definition)
    for definition in validated["skills"].values():
        if set(definition["requires_mcp"]) - set(servers):
            _fail("unknown_skill_mcp_dependency")
    return validated, sources


def _unique_json(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate_capability_registry_key", key)
        result[key] = value
    return result


def load_registry(path: Path) -> dict:
    """Read and validate a maintenance-owned registry, pinning its static files."""
    source, data = _read_file(path)
    try:
        registry = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_json,
                              parse_constant=lambda value: _fail("invalid_capability_registry_json", value))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail("invalid_capability_registry_json", str(exc))
    validated, sources = _validate_registry(registry)
    validated["_registry_path"] = str(source)
    validated["_source_hashes"] = {name: item["sha256"] for name, item in sources.items()}
    return validated


def _selected_definitions(selection: dict, registry: dict) -> dict:
    return {"skills": {key: registry["skills"][key] for key in selection["skills"]},
            "mcp_servers": {key: registry["mcp_servers"][key] for key in selection["mcp_servers"]}}


def _selected_source_names(definitions: dict) -> set[str]:
    names = set()
    for skill in definitions["skills"].values():
        path = Path(skill["path"])
        names.add(str(path))
        names.update(str(path.parent.joinpath(*resource["path"].split("/"))) for resource in skill["resources"])
    for server in definitions["mcp_servers"].values():
        if server["kind"] == "stdio":
            names.add(str(Path(server["command"])))
            names.update(str(Path(source)) for source in server["source_files"])
    return names


def normalize_selection(config: dict, registry: dict, *, cad_backend: str) -> dict:
    """Resolve strict ordered selection; omitted field preserves legacy behavior.

    Source hashes are private resolution evidence, not part of selection_sha256.
    They bind the later freeze to these exact bytes and definitions.
    """
    _object(config, "invalid_capability_config")
    if cad_backend not in {"fusion", "solidworks"}:
        _fail("unsupported_cad_backend")
    if "candidate_capabilities" not in config:
        return {"schema": SELECTION_SCHEMA, "selection_origin": "legacy_default",
                "study_kind": "capability_ablation", "skill_delivery": "gateway_read",
                "exposure_mode": "gateway_proxy", "skills": [], "mcp_servers": ["cad_native"],
                "cad_backend": cad_backend,
                "legacy_skill_fields": {key: copy.deepcopy(config[key]) for key in LEGACY_SKILL_FIELDS if key in config}}
    if any(key in config for key in LEGACY_SKILL_FIELDS):
        _fail("legacy_skill_selection_conflict")
    raw = _object(config["candidate_capabilities"], "invalid_capability_selection", _SELECTION_FIELDS)
    if set(raw) != _SELECTION_FIELDS:
        _fail("invalid_capability_selection", "all selection fields are required")
    if raw["schema"] != SELECTION_SCHEMA:
        _fail("unsupported_capability_selection_schema")
    if raw["study_kind"] not in ("capability_ablation", "negative_control_no_cad"):
        _fail("unsupported_capability_study_kind")
    if raw["skill_delivery"] != "gateway_read":
        _fail("unsupported_skill_delivery")
    if raw["exposure_mode"] != "gateway_proxy":
        _fail("unsupported_capability_exposure_mode")
    selection = copy.deepcopy(raw)
    selection["skills"] = _ids(raw["skills"], "invalid_skill_selection")
    selection["mcp_servers"] = _ids(raw["mcp_servers"], "invalid_mcp_selection")
    if "cad_native" not in selection["mcp_servers"] and selection["study_kind"] != "negative_control_no_cad":
        _fail("cad_capability_required")
    validated, sources = _validate_registry(registry)
    for skill_id in selection["skills"]:
        if skill_id not in validated["skills"]:
            _fail("unknown_skill_id", skill_id)
        skill = validated["skills"][skill_id]
        if cad_backend not in skill["cad_backends"]:
            _fail("skill_cad_backend_incompatible", skill_id)
        if set(skill["requires_mcp"]) - set(selection["mcp_servers"]):
            _fail("skill_mcp_dependency_missing", skill_id)
    for server_id in selection["mcp_servers"]:
        if server_id not in validated["mcp_servers"]:
            _fail("unknown_mcp_id", server_id)
    selection["selection_origin"] = "explicit"
    selection["cad_backend"] = cad_backend
    selection["selection_sha256"] = _digest(selection)
    definitions = _selected_definitions(selection, validated)
    selection["_registry_definition_sha256"] = _digest(definitions)
    selection["_source_hashes"] = {name: sources[name]["sha256"] for name in sorted(_selected_source_names(definitions))}
    return selection


def freeze_capabilities(selection: dict, registry: dict, output_dir: Path) -> dict:
    """Freeze selected bytes into a new private snapshot without overwrites.

    A legacy selection remains on the caller's existing legacy path; it must not
    be represented as a new explicit capability snapshot with fabricated IDs.
    """
    output = _safe_absolute(output_dir)
    if output.exists() or output.is_symlink():
        _fail("capability_snapshot_exists", str(output))
    _check_no_reparse(output, allow_missing_leaf=True)
    if selection.get("selection_origin") != "explicit":
        _fail("legacy_capabilities_not_freezable")
    raw = {key: copy.deepcopy(selection[key]) for key in _SELECTION_FIELDS if key in selection}
    current = normalize_selection({"candidate_capabilities": raw}, registry, cad_backend=selection.get("cad_backend"))
    if current["selection_sha256"] != selection.get("selection_sha256"):
        _fail("capability_selection_changed")
    if current["_registry_definition_sha256"] != selection.get("_registry_definition_sha256"):
        _fail("capability_registry_changed")
    if current["_source_hashes"] != selection.get("_source_hashes"):
        _fail("capability_source_hash_mismatch")
    # Read and hash once more immediately before allocation; all writes below
    # consume these pinned byte buffers, never reopen mutable source files.
    payloads = {}
    for name, expected in current["_source_hashes"].items():
        _, data = _read_file(name)
        if _sha(data) != expected:
            _fail("capability_source_hash_mismatch", name)
        payloads[name] = data
    try:
        output.mkdir(exist_ok=False)
    except FileExistsError:
        _fail("capability_snapshot_exists", str(output))
    manifest = {key: copy.deepcopy(current[key]) for key in _SELECTION_FIELDS if key not in {"schema", "skills", "mcp_servers"}}
    manifest.update({
        "schema": MANIFEST_SCHEMA, "selection_origin": "explicit", "cad_backend": current["cad_backend"],
        "selection_sha256": current["selection_sha256"],
        "ordered_enabled_skill_ids": list(current["skills"]),
        "ordered_enabled_mcp_ids": list(current["mcp_servers"]),
        "snapshot_root": str(output), "skills": {}, "requested_server_definitions": {},
        "skill_content_and_resource_hashes": {}, "observed_server_identity": {},
        "expected_tool_surface": [], "observed_tool_surface": [],
        "candidate_visible_tool_schema_hash": None, "exposure_verification_status": "not_verified",
        "actual_skill_reads": [], "per_server_tool_calls": {},
        "source_hashes": copy.deepcopy(current["_source_hashes"]),
    })

    def save(relative: Path, data: bytes) -> str:
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)
        return str(target)

    for skill_id in current["skills"]:
        definition = registry["skills"][skill_id]
        source = Path(definition["path"])
        snapshot = save(Path("skills") / skill_id / source.name, payloads[str(source)])
        skill = {"id": skill_id, "version": definition["version"], "source_path": str(source),
                 "content_sha256": _sha(payloads[str(source)]), "snapshot_path": snapshot, "resources": []}
        if "description" in definition:
            skill["description"] = definition["description"]
        for resource in definition["resources"]:
            resource_path = source.parent.joinpath(*resource["path"].split("/"))
            data = payloads[str(resource_path)]
            skill["resources"].append({"id": resource["id"], "path": resource["path"], "sha256": _sha(data),
                "media_type": _MEDIA[resource_path.suffix.lower()], "source_path": str(resource_path),
                "snapshot_path": save(Path("skills") / skill_id / resource["path"], data)})
        manifest["skills"][skill_id] = skill
        manifest["skill_content_and_resource_hashes"][skill_id] = {
            "content_sha256": skill["content_sha256"],
            "resources": {resource["id"]: resource["sha256"] for resource in skill["resources"]}}
    for server_id in current["mcp_servers"]:
        definition = copy.deepcopy(registry["mcp_servers"][server_id])
        if definition["kind"] == "stdio":
            definition["command_sha256"] = current["_source_hashes"][str(Path(definition["command"]))]
            definition["source_file_hashes"] = []
            for index, source in enumerate(definition["source_files"]):
                path = Path(source)
                data = payloads[str(path)]
                definition["source_file_hashes"].append({"path": str(path), "sha256": _sha(data),
                    "snapshot_path": save(Path("mcp_sources") / server_id / str(index) / path.name, data)})
        manifest["requested_server_definitions"][server_id] = definition
    save(Path("capability-manifest.json"), (json.dumps(manifest, ensure_ascii=False, indent=2,
         allow_nan=False) + "\n").encode("utf-8"))
    return manifest


def public_capabilities(manifest: dict) -> dict:
    """Allowlist-only selected capability metadata for a candidate contract."""
    if manifest.get("schema") != MANIFEST_SCHEMA:
        _fail("unsupported_capability_manifest_schema")
    public = {"schema": PUBLIC_SCHEMA, "selection_origin": manifest["selection_origin"],
              "study_kind": manifest["study_kind"], "skill_delivery": manifest["skill_delivery"],
              "exposure_mode": manifest["exposure_mode"], "selection_sha256": manifest["selection_sha256"],
              "skills": [], "mcp_servers": list(manifest["ordered_enabled_mcp_ids"])}
    for skill_id in manifest["ordered_enabled_skill_ids"]:
        skill = manifest["skills"][skill_id]
        item = {key: copy.deepcopy(skill[key]) for key in ("id", "version", "content_sha256")}
        if "description" in skill:
            item["description"] = skill["description"]
        item["resources"] = [{key: resource[key] for key in ("id", "sha256", "media_type")}
                             for resource in skill["resources"]]
        public["skills"].append(item)
    return public


def verify_frozen_capabilities(manifest: dict, *, verify_sources: bool = True) -> dict:
    """Recheck selected frozen files and, by default, their original sources.

    Consumers call this before launching services and after a candidate exits.
    Gateway reads may use ``verify_sources=False`` to check immutable copied
    bytes without depending on originals.  This reports file integrity only;
    it does not assert successful exposure, service discovery, or model use.
    """
    if manifest.get("schema") != MANIFEST_SCHEMA:
        _fail("unsupported_capability_manifest_schema")
    root = _safe_absolute(manifest["snapshot_root"])
    _check_no_reparse(root)
    count = 0

    def check(value: Any, expected: Any, *, snapshot: bool = False) -> None:
        nonlocal count
        path = _safe_absolute(value)
        if snapshot and (path == root or not path.is_relative_to(root)):
            _fail("capability_snapshot_path_escape")
        if not isinstance(expected, str) or not _DIGEST.fullmatch(expected):
            _fail("invalid_capability_source_sha256")
        _, data = _read_file(path)
        if _sha(data) != expected:
            _fail("capability_source_hash_mismatch", str(path))
        count += 1

    for skill_id in manifest["ordered_enabled_skill_ids"]:
        skill = manifest["skills"][skill_id]
        check(skill["snapshot_path"], skill["content_sha256"], snapshot=True)
        for resource in skill["resources"]:
            check(resource["snapshot_path"], resource["sha256"], snapshot=True)
    for server_id in manifest["ordered_enabled_mcp_ids"]:
        server = manifest["requested_server_definitions"][server_id]
        if server["kind"] == "stdio":
            for source in server["source_file_hashes"]:
                check(source["snapshot_path"], source["sha256"], snapshot=True)
            if verify_sources:
                check(server["command"], server["command_sha256"])
                for source in server["source_file_hashes"]:
                    check(source["path"], source["sha256"])
    if verify_sources:
        for source, expected in manifest["source_hashes"].items():
            check(source, expected)
    return {"status": "verified", "selection_sha256": manifest["selection_sha256"], "verified_file_count": count}

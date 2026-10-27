"""Generate maintenance-owned Fusion scripts; this module never connects to Fusion.

The scripts use the installed Fusion Python API, not candidate Python. F3D only:
external references are rejected because this interface has no dependency manifest.
Offline doubles validate control flow; a live Fusion acceptance run is still required.
No public API used here proves that every global/background Fusion task is idle.
"""
from __future__ import annotations

import os
import re


_COMMON = '''import adsk.core, adsk.fusion
import datetime, hashlib, json, math, os, pathlib, shutil, tempfile, traceback

def _items(collection):
    return [collection.item(i) for i in range(collection.count)]

def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def _idle(app):
    return app.userInterface.activeCommand in (None, "", "SelectCommand")

def _snapshot(app):
    rows = []
    for doc in _items(app.documents):
        rows.append({"name": doc.name, "id": doc.creationId,
                     "id_kind": "creationId", "isModified": bool(doc.isModified),
                     "isSaved": bool(doc.isSaved), "isActive": bool(doc.isActive),
                     "isValid": bool(doc.isValid)})
    return {"fusion_version": app.version, "activeCommand": app.userInterface.activeCommand,
            "documents_count": app.documents.count, "documents": rows,
            "background_tasks_verified": False,
            "background_tasks_note": "No global background-task completion API is asserted by this probe."}

def _require(condition, reason):
    if not condition:
        raise RuntimeError(reason)

'''


def workspace_probe_script() -> str:
    """Read-only document inventory, printed as one JSON object by run(context)."""
    return _COMMON + '''def run(_context: str):
    app = adsk.core.Application.get()
    report = _snapshot(app)
    report["benchmark_probe"] = True
    report["schema_version"] = "fusion-workspace-probe/v1"
    report["workspace_empty_and_command_idle"] = app.documents.count == 0 and _idle(app)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
'''


def trusted_reopen_script(
    f3d_path: str | os.PathLike[str],
    step_path: str | os.PathLike[str],
    report_path: str | os.PathLike[str],
    expected_source_sha256: str,
) -> str:
    """Return a run(context) script for frozen F3D reimport and guarded STEP export.

    Output parents must already exist. Existing STEP/report files are never replaced.
    The report is created exclusively before Fusion mutations and finalized on failure.
    Only the exact Document returned by importToNewDocument may be closed. An import
    that raises before returning ownership leaves any unknown document untouched.
    Warnings are recorded; all other nonhealthy feature states block publication.
    A private temporary STEP is published with an exclusive hard link after cleanup;
    the destination filesystem must support same-volume hard links (e.g. NTFS).
    """
    if not isinstance(expected_source_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_source_sha256):
        raise ValueError("expected_source_sha256 must contain 64 hexadecimal characters")
    paths = [os.fspath(path) for path in (f3d_path, step_path, report_path)]
    if any(not isinstance(path, str) or not path or "\x00" in path for path in paths):
        raise ValueError("All three paths must be nonempty text paths without NUL")
    # Transport only ASCII hex: some bridges misdecode raw Unicode or escape
    # backslashes again. Reconstruct exact path bytes inside the Fusion runtime.
    config = "".join(
        f"{name} = bytes.fromhex('{path.encode('utf-8').hex()}').decode('utf-8')\n"
        for name, path in zip(("SOURCE", "DESTINATION", "REPORT"), paths)
    ) + f"EXPECTED = {expected_source_sha256.lower()!r}\n\n"
    return _COMMON + config + _REOPEN


_REOPEN = '''def _geometry(design):
    root = design.rootComponent
    records = []
    bodies = [("root", body) for body in _items(root.bRepBodies)]
    for occurrence in _items(root.allOccurrences):
        # Occurrence bodies are proxies in root/world coordinates, preserving placements.
        bodies.extend((occurrence.fullPathName, body) for body in _items(occurrence.bRepBodies))
    for context, body in bodies:
        valid = bool(body.isValid)
        _require(valid, "invalid_brep_body")
        box = body.boundingBox
        bounds = [[float(v) * 10.0 for v in p.asArray()] for p in (box.minPoint, box.maxPoint)]
        volume = float(body.volume) * 1000.0
        records.append({"name": body.name, "context": context, "isValid": valid,
                        "isSolid": bool(body.isSolid), "volume_mm3": volume, "bbox_mm": bounds})
    valid_records = [row for row in records if row["isValid"]]
    finite = all(math.isfinite(value) for row in records for point in row["bbox_mm"] for value in point)
    finite = finite and all(math.isfinite(row["volume_mm3"]) for row in records)
    # Keep JSON strict even if a malformed body yields nonfinite native values.
    _require(finite, "nonfinite_geometry")
    bounds = None
    if records:
        bounds = [[min(row["bbox_mm"][0][axis] for row in records) for axis in range(3)],
                  [max(row["bbox_mm"][1][axis] for row in records) for axis in range(3)]]
    return {"units": "mm", "volume_units": "mm^3", "coordinate_frame": "Fusion root/world",
            "body_count": len(records), "valid_body_count": len(valid_records),
            "solid_body_count": sum(row["isSolid"] for row in records),
            "volume_mm3": sum(row["volume_mm3"] for row in records), "bbox_mm": bounds, "bodies": records}

def _health(design):
    records = []
    design_type = design.designType
    if design_type == adsk.fusion.DesignTypes.DirectDesignType:
        design_type_name = "direct"
        timeline_status, timeline_count = "not_applicable", None
    elif design_type == adsk.fusion.DesignTypes.ParametricDesignType:
        design_type_name = "parametric"
        timeline_status, timeline_count = "checked", None
    else:
        raise RuntimeError("unsupported_design_type")
    def record(entity, kind, location):
        records.append({"kind": kind, "location": location, "name": entity.name,
                        "health": int(entity.healthState), "message": entity.errorOrWarningMessage})
    def timeline_items(collection, prefix):
        for index, item in enumerate(_items(collection)):
            location = prefix + str(index)
            if item.isGroup:
                group = adsk.fusion.TimelineGroup.cast(item)
                _require(group is not None, "unreadable_timeline_group")
                # Groups themselves have Unknown health; inspect their children.
                timeline_items(group, location + "/")
            else:
                record(item, "timeline", location)
    if design_type == adsk.fusion.DesignTypes.ParametricDesignType:
        timeline = design.timeline
        timeline_items(timeline, "")
        timeline_count = timeline.count
    for component_index, component in enumerate(_items(design.allComponents)):
        for feature_index, feature in enumerate(_items(component.features)):
            record(feature, "feature", str(component_index) + "/" + str(feature_index))
    healthy = int(adsk.fusion.FeatureHealthStates.HealthyFeatureHealthState)
    warning = int(adsk.fusion.FeatureHealthStates.WarningFeatureHealthState)
    return {"design_type": design_type_name, "timeline_status": timeline_status,
            "timeline_count": timeline_count, "records": records,
            "warnings": [row for row in records if row["health"] == warning],
            "blocking": [row for row in records if row["health"] not in (healthy, warning)]}

def run(_context: str):
    source = pathlib.Path(SOURCE).resolve()
    destination = pathlib.Path(DESTINATION).absolute()
    report_path = pathlib.Path(REPORT).absolute()
    # Reserve the report first. If it exists, raise without touching it or Fusion.
    report_stream = report_path.open("x", encoding="utf-8")
    report = {"schema_version": "fusion-trusted-reopen/v1", "status": "failed",
              "benchmark_export": True, "ok": False,
              "started_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "source_path": str(source), "step_path": str(destination),
              "expected_source_sha256": EXPECTED, "own_document_closed": False,
              "background_tasks_verified": False, "errors": [],
              "limitations": ["Global Fusion background tasks are not enumerated.",
                              "External dependencies are rejected; only self-contained F3D is supported."]}
    app = None
    owned = None
    stage = None
    ready_to_publish = False
    try:
        _require(source.suffix.lower() == ".f3d", "only_f3d_supported")
        _require(destination.suffix.lower() in (".step", ".stp"), "step_extension_required")
        _require(len({str(p.resolve()).casefold() for p in (source, destination, report_path)}) == 3, "paths_must_be_distinct")
        _require(source.is_file(), "source_missing")
        _require(not os.path.lexists(destination), "step_destination_exists")
        _require(destination.parent.is_dir(), "step_parent_missing")
        report["source_sha256_before"] = _sha256(source)
        _require(report["source_sha256_before"] == EXPECTED, "source_hash_mismatch")
        app = adsk.core.Application.get()
        report["workspace_before"] = _snapshot(app)
        report["fusion_version"] = app.version
        _require(app.documents.count == 0, "workspace_not_empty")
        _require(_idle(app), "active_command_not_idle")
        stage = tempfile.TemporaryDirectory(prefix=".fusion-reopen-", dir=str(destination.parent))
        stage_path = pathlib.Path(stage.name)
        snapshot = stage_path / "frozen-source.f3d"
        shutil.copyfile(source, snapshot)
        _require(_sha256(snapshot) == EXPECTED, "source_changed_during_snapshot")
        _require(app.documents.count == 0 and _idle(app), "workspace_changed_before_import")
        options = app.importManager.createFusionArchiveImportOptions(str(snapshot))
        _require(options is not None, "import_options_failed")
        owned = app.importManager.importToNewDocument(options)
        _require(owned is not None, "import_did_not_return_document")
        _require(owned.isValid, "imported_document_invalid")
        report["reopened_document"] = {"name": owned.name, "id": owned.creationId, "id_kind": "creationId"}
        _require(app.documents.count == 1 and app.documents.item(0) == owned, "unexpected_documents_after_import")
        _require(_idle(app), "command_busy_after_import")
        design = adsk.fusion.Design.cast(owned.products.itemByProductType("DesignProductType"))
        _require(design is not None and design.isValid, "no_valid_design_product")
        refs = owned.allDocumentReferences.count
        external = [{"name": occ.name, "path": occ.fullPathName}
                    for occ in _items(design.rootComponent.allOccurrences) if occ.isReferencedComponent]
        report["dependencies"] = {"document_reference_count": refs, "external_occurrences": external,
                                   "closed": refs == 0 and not external,
                                   "policy": "reject_all_external_document_references"}
        _require(report["dependencies"]["closed"], "external_dependencies_not_closed")
        report["compute_all_completed"] = bool(design.computeAll())
        _require(report["compute_all_completed"], "compute_all_incomplete")
        report["health"] = _health(design)
        _require(not report["health"]["blocking"], "unhealthy_features")
        report["geometry"] = _geometry(design)
        geometry = report["geometry"]
        _require(geometry["body_count"] > 0, "no_brep_bodies")
        _require(geometry["solid_body_count"] == geometry["body_count"], "non_solid_brep_body")
        _require(all(row["volume_mm3"] > 0 for row in geometry["bodies"]), "nonpositive_body_volume")
        _require(_sha256(source) == EXPECTED, "source_changed_after_import")
        _require(app.documents.count == 1 and app.documents.item(0) == owned and _idle(app), "workspace_changed_before_export")
        staged_step = stage_path / "trusted.step"
        export_options = design.exportManager.createSTEPExportOptions(str(staged_step))
        _require(export_options is not None, "step_export_options_failed")
        _require(design.exportManager.execute(export_options), "step_export_failed")
        _require(staged_step.is_file() and staged_step.stat().st_size > 0, "step_export_empty")
        report["step_export"] = {"bytes": staged_step.stat().st_size, "sha256": _sha256(staged_step),
                                 "export_manager_success": True, "published": False}
        report["source_sha256_after"] = _sha256(source)
        _require(report["source_sha256_after"] == EXPECTED, "source_changed_during_export")
        _require(app.documents.count == 1 and app.documents.item(0) == owned and _idle(app), "workspace_changed_after_export")
        ready_to_publish = True
    except Exception as error:
        report["errors"].append({"phase": "reopen_export", "type": type(error).__name__, "message": str(error),
                                 "traceback": traceback.format_exc()})
    finally:
        # Never enumerate and close documents by name/id: IDs may be shared by copies.
        if owned is not None:
            try:
                if owned.isValid:
                    _require(_idle(app), "command_busy_during_cleanup")
                    _require(owned.close(False), "own_document_close_failed")
                report["own_document_closed"] = not owned.isValid or not any(doc == owned for doc in _items(app.documents))
                _require(report["own_document_closed"], "own_document_still_open")
            except Exception as error:
                report["errors"].append({"phase": "cleanup", "type": type(error).__name__, "message": str(error)})
        try:
            if app is not None:
                report["workspace_after"] = _snapshot(app)
            if source.is_file():
                report["source_sha256_after"] = _sha256(source)
            else:
                report["source_sha256_after"] = None
            if ready_to_publish and not report["errors"]:
                _require(report["own_document_closed"], "cleanup_not_verified")
                _require(app.documents.count == 0 and _idle(app), "workspace_not_empty_after_cleanup")
                _require(source.is_file(), "source_missing_after_cleanup")
                report["source_sha256_after"] = _sha256(source)
                _require(report["source_sha256_after"] == EXPECTED, "source_changed_during_cleanup")
                # Atomic, exclusive creation; no check-then-overwrite race at the final path.
                os.link(str(staged_step), str(destination))
                report["step_export"]["published"] = True
                report["status"] = "passed"
        except Exception as error:
            report["errors"].append({"phase": "publication", "type": type(error).__name__, "message": str(error)})
        if stage is not None:
            try:
                stage.cleanup()
            except Exception as error:
                report["errors"].append({"phase": "temporary_files_cleanup", "type": type(error).__name__, "message": str(error)})
        if report["errors"]:
            report["status"] = "failed"
        report["ok"] = report["status"] == "passed"
        report["ended_at_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        try:
            json.dump(report, report_stream, ensure_ascii=False, allow_nan=False, indent=2)
            report_stream.write(chr(10))
            report_stream.flush()
            os.fsync(report_stream.fileno())
        except Exception as error:
            report["status"] = "failed"
            report["ok"] = False
            report["errors"].append({"phase": "report_finalize", "type": type(error).__name__, "message": str(error)})
            # Best effort downgrade of any already-written success record. The caller
            # must also require successful script completion, never merely a JSON file.
            try:
                report_stream.seek(0)
                report_stream.truncate()
                json.dump(report, report_stream, ensure_ascii=False, allow_nan=False, indent=2)
                report_stream.write(chr(10))
                report_stream.flush()
                os.fsync(report_stream.fileno())
            except Exception:
                pass
            print(json.dumps(report, ensure_ascii=False, allow_nan=False))
            raise
        finally:
            report_stream.close()
        print(json.dumps(report, ensure_ascii=False, allow_nan=False))
'''

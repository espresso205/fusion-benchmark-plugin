"""Independent synthetic Fusion archive creation for live maintenance preflight.

The caller must place this script and its archive in a separate smoke directory,
never a benchmark question directory. Nothing is executed when importing this module.
"""
from __future__ import annotations

import os
from pathlib import Path

from .fusion_scripts import _COMMON


def create_archive_script(f3d_path: str | os.PathLike[str]) -> str:
    """Generate a guarded run(context) script that exports a synthetic 10 mm cube.

    Requires a new absolute .f3d destination and an existing parent directory.
    All inputs are embedded as Python string literals, never interpolated as code.
    """
    value = os.fspath(f3d_path)
    if not isinstance(value, str) or "\x00" in value or not Path(value).is_absolute():
        raise ValueError("Smoke archive path must be absolute text without NUL")
    if Path(value).suffix.lower() != ".f3d":
        raise ValueError("Smoke archive must have the .f3d extension")
    # Match trusted_reopen_script's ASCII transport: the bridge can misdecode
    # raw Unicode and double backslashes before executing the native script.
    return _COMMON + "ARCHIVE = bytes.fromhex('{}').decode('utf-8')\n\n".format(value.encode('utf-8').hex()) + _CREATE


_CREATE = '''def run(_context: str):
    archive = pathlib.Path(ARCHIVE)
    report = {"schema_version": "fusion-integration-smoke/v1", "benchmark_probe": True,
              "smoke_created": False, "ok": False, "own_document_closed": False,
              "purpose": "synthetic maintenance preflight; not a benchmark question",
              "archive_path": str(archive), "errors": [], "background_tasks_verified": False}
    app = None
    owned = None
    stage = None
    ready = False
    try:
        _require(archive.is_absolute(), "absolute_archive_path_required")
        _require(not os.path.lexists(archive), "archive_destination_exists")
        _require(archive.parent.is_dir(), "archive_parent_missing")
        app = adsk.core.Application.get()
        report["workspace_before"] = _snapshot(app)
        _require(app.documents.count == 0, "workspace_not_empty")
        _require(_idle(app), "active_command_not_idle")
        stage = tempfile.TemporaryDirectory(prefix=".fusion-smoke-", dir=str(archive.parent))
        staged_archive = pathlib.Path(stage.name) / "smoke.f3d"
        _require(app.documents.count == 0 and _idle(app), "workspace_changed_before_create")
        owned = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
        _require(owned is not None and owned.isValid, "new_document_failed")
        _require(app.documents.count == 1 and app.documents.item(0) == owned, "unexpected_documents_after_create")
        owned.name = "Fusion maintenance smoke cube"
        design = adsk.fusion.Design.cast(owned.products.itemByProductType("DesignProductType"))
        _require(design is not None and design.isValid, "new_design_failed")
        root = design.rootComponent
        sketch = root.sketches.add(root.xYConstructionPlane)
        _require(sketch is not None, "smoke_sketch_failed")
        sketch.sketchCurves.sketchLines.addTwoPointRectangle(
            adsk.core.Point3D.create(0, 0, 0), adsk.core.Point3D.create(1, 1, 0))
        _require(sketch.profiles.count == 1, "smoke_profile_count")
        extrusion = root.features.extrudeFeatures.addSimple(
            sketch.profiles.item(0), adsk.core.ValueInput.createByReal(1),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
        _require(extrusion is not None, "smoke_extrude_failed")
        _require(design.computeAll(), "smoke_compute_incomplete")
        _require(root.bRepBodies.count == 1, "smoke_body_count")
        body = root.bRepBodies.item(0)
        _require(body.isValid and body.isSolid and abs(body.volume - 1.0) < 1e-8, "smoke_geometry_invalid")
        _require(int(extrusion.healthState) == int(adsk.fusion.FeatureHealthStates.HealthyFeatureHealthState), "smoke_feature_unhealthy")
        report["geometry"] = {"body_count": 1, "solid": True, "units": "mm", "volume_mm3": float(body.volume) * 1000.0,
                              "bbox_mm": [[float(v) * 10 for v in p.asArray()] for p in (body.boundingBox.minPoint, body.boundingBox.maxPoint)]}
        _require(app.documents.count == 1 and app.documents.item(0) == owned and _idle(app), "workspace_changed_before_archive")
        options = design.exportManager.createFusionArchiveExportOptions(str(staged_archive))
        _require(options is not None, "archive_export_options_failed")
        _require(design.exportManager.execute(options), "archive_export_failed")
        _require(staged_archive.is_file() and staged_archive.stat().st_size > 0, "archive_export_empty")
        report["archive_sha256"] = _sha256(staged_archive)
        report["archive_bytes"] = staged_archive.stat().st_size
        ready = True
    except Exception as error:
        report["errors"].append({"phase": "create_archive", "type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()})
    finally:
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
            if ready and not report["errors"]:
                _require(report["own_document_closed"], "cleanup_not_verified")
                _require(app.documents.count == 0 and _idle(app), "workspace_not_empty_after_cleanup")
                os.link(str(staged_archive), str(archive))
                report["smoke_created"] = True
                report["ok"] = True
        except Exception as error:
            report["errors"].append({"phase": "publish_archive", "type": type(error).__name__, "message": str(error)})
        if stage is not None:
            try:
                stage.cleanup()
            except Exception as error:
                report["errors"].append({"phase": "temporary_files_cleanup", "type": type(error).__name__, "message": str(error)})
        if report["errors"]:
            report["ok"] = False
            report["error"] = "; ".join(row["message"] for row in report["errors"])
        print(json.dumps(report, ensure_ascii=False, allow_nan=False))
'''

"""Execute generated scripts against an offline Fusion API double, never Fusion."""
import contextlib
import hashlib
import importlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import patch


class Collection:
    def __init__(self, items=()):
        self.items = list(items)

    @property
    def count(self):
        return len(self.items)

    def item(self, index):
        return self.items[index]


class Document:
    def __init__(self, app, name="imported"):
        self.app = app
        self.name = name
        self.creationId = "creation-id"
        self.isModified = True
        self.isSaved = False
        self.isValid = True
        self.isActive = True
        self.allDocumentReferences = Collection()
        self.products = NS(itemByProductType=lambda _: app.design)
        self.close_ok = True

    def close(self, save):
        if save:
            raise AssertionError("Closing must never prompt for saving")
        if self.close_ok:
            self.app.documents.items.remove(self)
            self.isValid = False
            if self.app.after_close:
                self.app.after_close()
        return self.close_ok


class FusionDouble:
    def __init__(self):
        self.documents = Collection()
        self.userInterface = NS(activeCommand="SelectCommand")
        self.version = "offline-api-double"
        self.import_error = None
        self.after_import = None
        self.after_export = None
        self.after_close = None
        self.imported = None
        self.imported_bytes = None
        self.export_ok = True
        point = lambda xyz: NS(asArray=lambda: xyz)
        body = NS(name="synthetic body", isValid=True, isSolid=True, volume=6.0,
                  boundingBox=NS(minPoint=point([0., 0., 0.]), maxPoint=point([1., 2., 3.])))
        self.feature = NS(name="synthetic feature", healthState=0, errorOrWarningMessage="", isValid=True)
        self.timeline_item = NS(name="synthetic feature", healthState=0, errorOrWarningMessage="", isGroup=False, isValid=True)
        self.root = NS(name="root", bRepBodies=Collection([body]), allOccurrences=Collection(), features=Collection([self.feature]))
        self.design = NS(isValid=True, rootComponent=self.root, timeline=Collection([self.timeline_item]),
                         allComponents=Collection([self.root]), computeAll=lambda: True)
        self.design.exportManager = NS(createSTEPExportOptions=lambda path: path, execute=self.export)
        self.importManager = NS(createFusionArchiveImportOptions=lambda path: path, importToNewDocument=self.import_doc)
        self.core = ModuleType("adsk.core")
        self.core.Application = NS(get=lambda: self)
        self.fusion = ModuleType("adsk.fusion")
        self.fusion.Design = NS(cast=lambda value: value)
        self.fusion.TimelineGroup = NS(cast=lambda value: value)
        self.fusion.FeatureHealthStates = NS(HealthyFeatureHealthState=0, WarningFeatureHealthState=1)
        self.adsk = ModuleType("adsk")
        self.adsk.core, self.adsk.fusion = self.core, self.fusion

    def import_doc(self, path):
        if self.import_error:
            raise self.import_error
        self.imported_bytes = Path(path).read_bytes()
        self.imported = Document(self)
        self.documents.items.append(self.imported)
        if self.after_import:
            self.after_import()
        return self.imported

    def export(self, path):
        Path(path).write_bytes(b"ISO-10303-21;\nsynthetic STEP fixture\nEND-ISO-10303-21;")
        if self.after_export:
            self.after_export()
        return self.export_ok

    def execute(self, script):
        namespace = {}
        output = io.StringIO()
        with patch.dict(sys.modules, {"adsk": self.adsk, "adsk.core": self.core, "adsk.fusion": self.fusion}), contextlib.redirect_stdout(output):
            exec(compile(script, "<generated-fusion-script>", "exec"), namespace)
            namespace["run"]("offline-test")
        return output.getvalue()


class FusionScriptTests(unittest.TestCase):
    def setUp(self):
        try:
            self.api = importlib.import_module("fusion_pipeline.fusion_scripts")
        except ImportError:
            self.fail("Fusion maintenance script builders are not implemented")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder / "input.f3d"
        self.source.write_bytes(b"synthetic frozen F3D bytes; not a real archive")
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.step = self.folder / "trusted.step"
        self.report = self.folder / "report.json"
        self.app = FusionDouble()

    def run_reopen(self, digest=None):
        script = self.api.trusted_reopen_script(str(self.source), str(self.step), str(self.report), digest or self.digest)
        self.app.execute(script)
        return json.loads(self.report.read_text(encoding="utf-8"))

    def test_probe_reads_modified_document_without_closing_it(self):
        doc = Document(self.app, "untitled user work")
        self.app.documents.items.append(doc)
        report = json.loads(self.app.execute(self.api.workspace_probe_script()))
        self.assertTrue(report["benchmark_probe"])
        self.assertEqual(report["documents_count"], 1)
        self.assertEqual(report["documents"][0]["id"], "creation-id")
        self.assertTrue(report["documents"][0]["isModified"])
        self.assertEqual(report["activeCommand"], "SelectCommand")
        self.assertEqual(self.app.documents.items, [doc])
        self.assertFalse(report["background_tasks_verified"])

    def test_reopens_frozen_bytes_exports_mm_evidence_and_closes_only_own_document(self):
        report = self.run_reopen()
        self.assertEqual(report["status"], "passed")
        self.assertTrue(report["benchmark_export"])
        self.assertTrue(report["ok"])
        self.assertEqual(report["source_sha256_after"], self.digest)
        self.assertEqual(report["geometry"]["body_count"], 1)
        self.assertEqual(report["geometry"]["volume_mm3"], 6000.0)
        self.assertEqual(report["geometry"]["bbox_mm"], [[0., 0., 0.], [10., 20., 30.]])
        self.assertTrue(report["own_document_closed"])
        self.assertEqual(self.app.documents.count, 0)
        self.assertEqual(self.app.imported_bytes, self.source.read_bytes())
        self.assertGreater(self.step.stat().st_size, 0)

    def test_existing_document_blocks_import_without_touching_user_work(self):
        doc = Document(self.app, "user work")
        self.app.documents.items.append(doc)
        report = self.run_reopen()
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["ok"])
        self.assertEqual(self.app.documents.items, [doc])
        self.assertIsNone(self.app.imported)
        self.assertFalse(self.step.exists())

    def test_active_command_blocks_import(self):
        self.app.userInterface.activeCommand = "SketchLine"
        self.assertEqual(self.run_reopen()["status"], "failed")
        self.assertIsNone(self.app.imported)

    def test_wrong_source_hash_blocks_import(self):
        self.assertEqual(self.run_reopen("0" * 64)["status"], "failed")
        self.assertIsNone(self.app.imported)

    def test_existing_step_is_not_overwritten(self):
        self.step.write_bytes(b"prior evidence")
        self.assertEqual(self.run_reopen()["status"], "failed")
        self.assertEqual(self.step.read_bytes(), b"prior evidence")
        self.assertIsNone(self.app.imported)

    def test_existing_report_is_not_overwritten(self):
        self.report.write_bytes(b"prior report")
        with self.assertRaises(FileExistsError):
            self.run_reopen()
        self.assertEqual(self.report.read_bytes(), b"prior report")
        self.assertIsNone(self.app.imported)

    def test_external_occurrence_is_rejected_and_imported_document_cleaned(self):
        self.app.root.allOccurrences.items.append(NS(name="external", fullPathName="external:1", isReferencedComponent=True, isValid=True))
        report = self.run_reopen()
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["dependencies"]["closed"])
        self.assertTrue(report["own_document_closed"])
        self.assertFalse(self.step.exists())

    def test_document_reference_is_rejected_even_without_external_occurrence(self):
        self.app.after_import = lambda: self.app.imported.allDocumentReferences.items.append(NS())
        report = self.run_reopen()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["dependencies"]["document_reference_count"], 1)
        self.assertFalse(self.step.exists())

    def test_warning_is_reported_and_error_blocks_export(self):
        self.app.timeline_item.healthState = 1
        self.app.timeline_item.errorOrWarningMessage = "synthetic warning"
        report = self.run_reopen()
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["health"]["warnings"][0]["message"], "synthetic warning")

    def test_feature_error_blocks_export_and_still_closes(self):
        self.app.feature.healthState = 2
        report = self.run_reopen()
        self.assertEqual(report["status"], "failed")
        self.assertTrue(report["own_document_closed"])
        self.assertFalse(self.step.exists())

    def test_non_solid_body_blocks_export(self):
        self.app.root.bRepBodies.item(0).isSolid = False
        self.assertEqual(self.run_reopen()["status"], "failed")
        self.assertFalse(self.step.exists())

    def test_source_mutation_during_export_rejects_publication(self):
        self.app.after_export = lambda: self.source.write_bytes(b"changed")
        report = self.run_reopen()
        self.assertEqual(report["status"], "failed")
        self.assertFalse(self.step.exists())
        self.assertTrue(report["own_document_closed"])

    def test_another_document_appearing_is_never_closed(self):
        other = Document(self.app, "user opened while exporting")
        self.app.after_export = lambda: self.app.documents.items.append(other)
        report = self.run_reopen()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(self.app.documents.items, [other])
        self.assertFalse(self.step.exists())

    def test_export_failure_cleans_only_owned_document(self):
        self.app.export_ok = False
        report = self.run_reopen()
        self.assertEqual(report["status"], "failed")
        self.assertTrue(report["own_document_closed"])
        self.assertFalse(self.step.exists())

    def test_close_failure_prevents_success_and_step_publication(self):
        self.app.after_import = lambda: setattr(self.app.imported, "close_ok", False)
        report = self.run_reopen()
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["own_document_closed"])
        self.assertFalse(self.step.exists())

    def test_destination_created_during_export_is_preserved(self):
        self.app.after_export = lambda: self.step.write_bytes(b"concurrent evidence")
        report = self.run_reopen()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(self.step.read_bytes(), b"concurrent evidence")
        self.assertTrue(report["own_document_closed"])

    def test_import_exception_preserves_unknown_documents(self):
        unknown = Document(self.app, "unowned document")
        def ambiguous_import(_):
            self.app.documents.items.append(unknown)
            raise RuntimeError("import interrupted before returning ownership")
        self.app.importManager.importToNewDocument = ambiguous_import
        report = self.run_reopen()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(self.app.documents.items, [unknown])
        self.assertEqual(report["workspace_after"]["documents_count"], 1)

    def test_nonfinite_geometry_writes_valid_failure_json(self):
        self.app.root.bRepBodies.item(0).volume = float("nan")
        report = self.run_reopen()
        self.assertEqual(report["status"], "failed")
        self.assertTrue(report["own_document_closed"])
        self.assertFalse(self.step.exists())

    def test_timeline_group_checks_child_errors_instead_of_unknown_group_state(self):
        group = Collection([self.app.timeline_item])
        group.isGroup = True
        group.healthState = 5
        self.app.design.timeline.items = [group]
        self.app.timeline_item.healthState = 2
        report = self.run_reopen()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["health"]["blocking"][0]["health"], 2)
        self.assertFalse(self.step.exists())

    def test_path_strings_cannot_inject_python(self):
        unusual_source = self.folder / "quoted' source.f3d"
        self.source.rename(unusual_source)
        self.source = unusual_source
        self.assertEqual(self.run_reopen()["status"], "passed")

    def test_bad_expected_hash_is_rejected_before_generating_script(self):
        with self.assertRaises(ValueError):
            self.api.trusted_reopen_script(self.source, self.step, self.report, "not-a-hash")

    def test_source_removed_during_document_cleanup_prevents_publication(self):
        self.app.after_close = self.source.unlink
        report = self.run_reopen()
        self.assertEqual(report["status"], "failed")
        self.assertFalse(self.step.exists())
        self.assertTrue(report["own_document_closed"])

    def test_report_fsync_failure_is_not_left_marked_passed(self):
        with patch("os.fsync", side_effect=OSError("synthetic disk flush failure")):
            with self.assertRaises(OSError):
                self.run_reopen()
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["ok"])
        self.assertTrue(any(row["phase"] == "report_finalize" for row in report["errors"]))


if __name__ == "__main__":
    unittest.main()

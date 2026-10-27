"""Offline smoke-script contract checks; no live Fusion is called here."""
import importlib
import json
from pathlib import Path
import tempfile
import unittest
import sys
from unittest.mock import patch

from test_fusion_scripts import Document, FusionDouble


class IntegrationSmokeTests(unittest.TestCase):
    def setUp(self):
        try:
            self.api = importlib.import_module("fusion_pipeline.integration_smoke")
        except ImportError:
            self.fail("Independent Fusion smoke script builder is not implemented")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.archive = Path(self.temp.name) / "smoke cube.f3d"

    def test_generated_script_compiles_with_quoted_absolute_path(self):
        script = self.api.create_archive_script(self.archive.with_name("quoted' smoke.f3d"))
        compile(script, "<fusion-integration-smoke>", "exec")

    def test_archive_path_survives_bridge_unicode_and_backslash_transport(self):
        archive = self.archive.parent / "中文 空格🧩" / "quoted' cube.f3d"
        script = self.api.create_archive_script(archive)
        transported = script.encode('utf-8').decode('latin-1').replace('\\', '\\\\')
        app, namespace = FusionDouble(), {}
        with patch.dict(sys.modules, {'adsk':app.adsk,'adsk.core':app.core,'adsk.fusion':app.fusion}):
            exec(compile(transported,'<bridge-smoke-path>','exec'),namespace)
        self.assertEqual(namespace['ARCHIVE'],str(archive))
        self.assertTrue(script.isascii())

    def test_relative_path_is_rejected(self):
        with self.assertRaises(ValueError):
            self.api.create_archive_script("relative.f3d")

    def test_non_f3d_path_is_rejected(self):
        with self.assertRaises(ValueError):
            self.api.create_archive_script(self.archive.with_suffix(".step"))

    def test_existing_document_is_preserved_and_smoke_is_refused(self):
        app = FusionDouble()
        doc = Document(app, "user work")
        app.documents.items.append(doc)
        report = json.loads(app.execute(self.api.create_archive_script(self.archive)))
        self.assertTrue(report["benchmark_probe"])
        self.assertFalse(report["ok"])
        self.assertFalse(report["smoke_created"])
        self.assertEqual(app.documents.items, [doc])
        self.assertFalse(self.archive.exists())

    def test_busy_command_prevents_document_creation(self):
        app = FusionDouble()
        app.userInterface.activeCommand = "SketchLine"
        report = json.loads(app.execute(self.api.create_archive_script(self.archive)))
        self.assertFalse(report["ok"])
        self.assertEqual(app.documents.count, 0)
        self.assertFalse(self.archive.exists())

    def test_existing_archive_is_not_overwritten(self):
        self.archive.write_bytes(b"prior artifact")
        app = FusionDouble()
        report = json.loads(app.execute(self.api.create_archive_script(self.archive)))
        self.assertFalse(report["ok"])
        self.assertEqual(self.archive.read_bytes(), b"prior artifact")
        self.assertEqual(app.documents.count, 0)


if __name__ == "__main__":
    unittest.main()

"""Portable distribution boundaries, using synthetic files rather than live CAD."""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_portable_plugin.py"


def file_row(name, data):
    return {"path": name, "size": len(data), "hash_mode": "sha256",
            "hash_value": base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()}


class PortablePluginTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "portable builder has not been implemented")
        spec = importlib.util.spec_from_file_location("portable_builder", SCRIPT)
        self.api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.api)
        self.temp = tempfile.TemporaryDirectory(prefix="portable test ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, relative, data=b"public"):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def test_source_plan_excludes_private_and_unlisted_modules(self):
        source = self.root / "source"
        for name in self.api.REQUIRED_SOURCE_FILES:
            self.write("source/" + name)
        for name in ("private/cases.json", "runs/report.json", "deliverables/prior.zip",
                     "fusion_pipeline/local_secrets.py", "src/local_secrets.py", ".env"):
            self.write("source/" + name, b"private-marker")
        plan = self.api.source_files(source)
        self.assertIn("evaluate.py", plan)
        self.assertIn("agent/fusion-benchmark/SKILL.md", plan)
        self.assertFalse(any(b"private-marker" in p.read_bytes() for p in plan.values()))

    def test_missing_required_source_fails_before_creating_output(self):
        with self.assertRaisesRegex(ValueError, "missing required source"):
            self.api.source_files(self.root)

    def test_dependency_records_copy_owned_files_but_not_scripts_or_install_metadata(self):
        site = self.root / "site"
        self.write("site/sample/__init__.py", b"VALUE = 1\n")
        self.write("site/sample-1.0.dist-info/LICENSE", b"MIT license")
        self.write("site/sample-1.0.dist-info/direct_url.json", b"private install URL")
        self.write("site/sample/private.env", b"unowned secret")
        rows = [file_row("sample/__init__.py", b"VALUE = 1\n"),
                file_row("sample-1.0.dist-info/LICENSE", b"MIT license"),
                file_row("sample-1.0.dist-info/direct_url.json", b"private install URL"),
                {"path": "../../Scripts/sample.exe", "size": 123, "hash_mode": None, "hash_value": None}]
        result = self.api.dependency_files(site, [{"name": "sample", "version": "1.0", "files": rows}])
        self.assertEqual(set(result), {"sample/__init__.py", "sample-1.0.dist-info/LICENSE"})

    def test_changed_installed_dependency_is_rejected(self):
        site = self.root / "site"
        self.write("site/sample.py", b"modified")
        with self.assertRaisesRegex(ValueError, "RECORD.*mismatch"):
            self.api.dependency_files(site, [{"name": "sample", "version": "1",
                "files": [file_row("sample.py", b"original")]}])

    def test_dependency_absolute_paths_and_missing_files_are_rejected(self):
        for name in ("C:/private/token", "//host/share/token", "sample.py"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.api.dependency_files(self.root, [{"name": "sample", "version": "1",
                    "files": [{"path": name, "size": None, "hash_mode": None, "hash_value": None}]}])

    def test_runtime_uses_relative_search_paths_and_omits_venv_and_global_site_packages(self):
        base = self.root / "base"
        for name in ("python.exe", "python312.dll", "python3.dll", "vcruntime140.dll", "LICENSE.txt"):
            self.write("base/" + name)
        self.write("base/Lib/os.py", b"stdlib")
        self.write("base/Lib/encodings/__init__.py", b"stdlib")
        self.write("base/Lib/site-packages/private.py", b"private marker")
        self.write("base/Lib/sitecustomize.py", b"private marker")
        self.write("base/pyvenv.cfg", b"home = C:/old-machine")
        target = self.root / "relocated Chinese 路径" / "runtime" / "python"
        self.api.copy_runtime(base, target, ["os", "encodings"], "312")
        self.assertTrue((target / "Lib/os.py").is_file())
        self.assertFalse((target / "Lib/site-packages/private.py").exists())
        self.assertFalse((target / "Lib/sitecustomize.py").exists())
        self.assertFalse((target / "pyvenv.cfg").exists())
        paths = (target / "python312._pth").read_text().splitlines()
        self.assertIn("Lib/site-packages", paths)
        self.assertFalse(any(":" in p or str(base) in p for p in paths))

    @unittest.skipUnless(os.name == "nt" and sys.version_info[:2] == (3, 12), "Windows CPython 3.12 runtime integration")
    def test_real_interpreter_imports_bundle_root_after_relocation(self):
        target = self.root / "original bundle" / "runtime" / "python"
        self.api.copy_runtime(sys.base_prefix, target, sys.stdlib_module_names, "312")
        (target.parents[1] / "portability_probe.py").write_text("VALUE = 'root found'\n", encoding="utf-8")
        relocated = self.root / "another 中文 path"
        target.parents[1].rename(relocated)
        result = subprocess.run([str(relocated / "runtime/python/python.exe"), "-B", "-c",
                                 "import portability_probe; print(portability_probe.VALUE)"],
                                cwd=self.root, capture_output=True, text=True,
                                env=dict(os.environ, PYTHONHOME="Z:/missing", PYTHONPATH="Z:/missing"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "root found")

    @unittest.skipUnless(os.name == "nt" and sys.version_info[:2] == (3, 12), "Windows CPython 3.12 runtime integration")
    def test_snapshot_evaluator_and_worker_import_snapshot_src(self):
        bundle = self.root / "bundle"
        self.api.copy_runtime(sys.base_prefix, bundle / "runtime/python", sys.stdlib_module_names, "312")
        self.write("bundle/src/__init__.py", b"# Package-root decoy; snapshot must win.\n")
        snapshot = self.root / "private snapshot 中文"
        (snapshot / "src").mkdir(parents=True)
        source = SCRIPT.parents[1]
        for path in (source / "src").glob("*.py"):
            shutil.copyfile(path, snapshot / "src" / path.name)
        for name in ("evaluate.py", "worker.py"):
            shutil.copyfile(source / name, snapshot / name)
            program = ("import runpy,pathlib;runpy.run_path(" + repr(str(snapshot / name)) +
                       ",run_name='snapshot_probe');import src;assert pathlib.Path(src.__file__).resolve()==pathlib.Path(" +
                       repr(str(snapshot / "src/__init__.py")) + ");print('snapshot source confirmed')")
            result = subprocess.run([str(bundle / "runtime/python/python.exe"), "-B", "-c", program],
                                    cwd=self.root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, name + ": " + result.stderr)
            self.assertIn("snapshot source confirmed", result.stdout)

    def test_manifest_verification_detects_tampering_and_extra_files(self):
        bundle = self.root / "bundle"
        self.write("bundle/one.txt", b"abc")
        self.api.write_manifest(bundle, {"python_version": "3.12.14"})
        report = self.api.verify_bundle(bundle)
        self.assertEqual(report["verified_files"], 1)
        (bundle / "one.txt").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "hash|size"):
            self.api.verify_bundle(bundle)
        (bundle / "one.txt").write_bytes(b"abc")
        self.write("bundle/private.txt", b"unlisted")
        with self.assertRaisesRegex(ValueError, "file set"):
            self.api.verify_bundle(bundle)

    def test_verifier_rejects_incomplete_marker_and_nested_unlisted_marker(self):
        bundle = self.root / "bundle"
        self.write("bundle/one.txt", b"abc")
        self.api.write_manifest(bundle, {})
        self.write("bundle/private/INCOMPLETE", b"private")
        with self.assertRaisesRegex(ValueError, "file set"):
            self.api.verify_bundle(bundle)
        (bundle / "private/INCOMPLETE").unlink()
        self.write("bundle/INCOMPLETE", b"unfinished")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.api.verify_bundle(bundle)

    def test_manifest_records_native_dependencies_and_licenses(self):
        bundle = self.root / "bundle"
        self.write("bundle/runtime/python/python312.dll", b"native")
        self.write("bundle/licenses/Python.txt", b"license")
        self.api.write_manifest(bundle, {"python_version": "3.12.14", "distributions": [{"name": "sample", "version": "1"}]})
        data = json.loads((bundle / "PACKAGE_MANIFEST.json").read_text())
        self.assertEqual(data["files"]["runtime/python/python312.dll"]["sha256"], hashlib.sha256(b"native").hexdigest())
        self.assertEqual(data["runtime"]["distributions"][0]["name"], "sample")

    def test_declared_apache_license_gets_text_when_distribution_omits_it(self):
        bundle = self.root / "bundle"
        self.write("bundle/runtime/python/LICENSE.txt", b"Python license")
        self.write("bundle/licenses/Apache-2.0.txt", b"Apache License Version 2.0")
        self.api.write_licenses(bundle, {"distributions": [{"name": "proxy", "version": "1",
                               "license": "Apache-2.0", "files": []}]}, {})
        data = json.loads((bundle / "licenses/inventory.json").read_text())
        paths = data["distributions"][0]["license_files"]
        self.assertEqual(len(paths), 1)
        self.assertEqual((bundle / paths[0]).read_bytes(), b"Apache License Version 2.0")

    def test_bootstrap_resolves_bundle_from_unrelated_working_directory(self):
        bootstrap = SCRIPT.parents[1] / "fusion-benchmark-bootstrap.py"
        self.assertTrue(bootstrap.is_file(), "portable bootstrap is missing")
        bundle = self.root / "bundle 空格"
        self.write("bundle 空格/fusion_pipeline/__init__.py", b"")
        self.write("bundle 空格/fusion_pipeline/cli.py",
                   b"import json, sys\ndef main():\n print(json.dumps(sys.argv[1:], ensure_ascii=False))\n return 7\n")
        shutil.copyfile(bootstrap, bundle / bootstrap.name)
        result = subprocess.run([sys.executable, "-I", str(bundle / bootstrap.name), "doctor", "path 中文"],
                                cwd=self.root, capture_output=True)
        self.assertEqual(result.returncode, 7, result.stderr)
        try:
            decoded = result.stdout.decode("utf-8")
        except UnicodeDecodeError:
            self.fail("bootstrap output must remain UTF-8 on Windows code-page terminals")
        self.assertEqual(json.loads(decoded), ["doctor", "path 中文"])


if __name__ == "__main__":
    unittest.main()

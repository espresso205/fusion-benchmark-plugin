"""Exercise the offline analysis command through the shipped plugin entry point."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
ANALYSIS_FILES = {
    "analyze_ablation.py",
    "analysis/__init__.py",
    "analysis/result_reader.py",
    "analysis/statistics.py",
    "analysis/ablation.py",
    "analysis/reporting.py",
    "analysis/system_comparison.py",
    "docs/ABLATION.md",
}


class AblationEntrypointTests(unittest.TestCase):
    def run_bootstrap(self, *arguments):
        # Fail if the offline command imports management or geometry code,
        # even on a machine where their optional dependencies are installed.
        program = """
import importlib.abc
import runpy
import sys

class OfflineImportGuard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        blocked = {
            'fusion_pipeline.service', 'fusion_pipeline.runner',
            'fusion_pipeline.worker_gateway', 'worker',
            'OCP', 'build123d', 'numpy', 'scipy', 'trimesh', 'open3d',
        }
        if any(fullname == name or fullname.startswith(name + '.') for name in blocked):
            raise AssertionError('offline analysis imported ' + fullname)

sys.meta_path.insert(0, OfflineImportGuard())
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
"""
        with tempfile.TemporaryDirectory(prefix="ablation caller ") as caller:
            return subprocess.run(
                [sys.executable, "-I", "-S", "-B", "-c", program,
                 str(ROOT / "fusion-benchmark-bootstrap.py"), *arguments],
                cwd=caller, capture_output=True, text=True, encoding="utf-8",
                timeout=30,
            )

    def test_top_level_help_lists_offline_analysis_command(self):
        result = self.run_bootstrap("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ablation", result.stdout)

    def test_ablation_help_uses_public_cli_without_config_or_service(self):
        result = self.run_bootstrap("ablation", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        for option in ("--input", "--manifest", "--output"):
            self.assertIn(option, result.stdout)
        self.assertNotIn("--config", result.stdout)

    def test_input_arguments_reach_analysis_without_starting_management(self):
        with tempfile.TemporaryDirectory(prefix="ablation inputs ") as temp:
            missing = Path(temp) / "missing source 中文"
            output = Path(temp) / "new analysis"
            result = self.run_bootstrap(
                "ablation", "--input", str(missing), "--output", str(output),
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn(str(missing), result.stderr)
            self.assertNotIn("--config", result.stderr)
            self.assertNotIn("offline analysis imported", result.stderr)
            self.assertFalse(output.exists())

    def test_portable_source_plan_includes_public_analysis_and_guide(self):
        spec = importlib.util.spec_from_file_location(
            "ablation_portable_builder", ROOT / "scripts/build_portable_plugin.py",
        )
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        plan = builder.source_files(ROOT)
        self.assertTrue(ANALYSIS_FILES <= set(plan), ANALYSIS_FILES - set(plan))
        for name in ANALYSIS_FILES:
            self.assertEqual(plan[name], ROOT / name)
            self.assertTrue(plan[name].is_file(), name)


if __name__ == "__main__":
    unittest.main()

import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
import zipfile

from fusion_pipeline.mcp import extract_result
from fusion_pipeline.runner import extract_evaluator, classify_guard, new_run


class RunnerTests(unittest.TestCase):
    def test_new_run_never_reuses_existing_directory(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = new_run(Path(d)), new_run(Path(d))
            self.assertNotEqual(a, b)
            self.assertTrue(a.is_dir() and b.is_dir())

    def test_package_integrity_rejects_tamper_and_traversal(self):
        for bad in (False, True):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                data = b"# evaluator"
                manifest = {"files": {"evaluate.py": {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}}}
                archive = root / "tools.zip"
                with zipfile.ZipFile(archive, "w") as z:
                    z.writestr("fusion_benchmark_evaluator_v2/evaluate.py", data if not bad else b"changed")
                    z.writestr("fusion_benchmark_evaluator_v2/PACKAGE_MANIFEST.json", json.dumps(manifest))
                    if bad:
                        z.writestr("../escape.txt", "no")
                if bad:
                    self.assertRaises(ValueError, extract_evaluator, archive, root / "out")
                else:
                    out = extract_evaluator(archive, root / "out")
                    self.assertEqual((out / "evaluate.py").read_bytes(), data)

    def test_pending_fusion_and_unknown_guard_stop_batch(self):
        self.assertEqual(classify_guard(None), "guard_missing")
        self.assertEqual(classify_guard({"pendingFusion": {"request": {}}}), "fusion_task_unknown")
        self.assertEqual(classify_guard({"pendingFusion": {}}), "guard_invalid")
        self.assertEqual(classify_guard({"version": 1, "pendingFusion": 0, "model_verified": True,
                                         "sessionID": "synthetic-session", "deadline": time.time() * 1000 + 60000,
                                         "calls": {}, "total_tool_calls": 0, "fusion_tool_calls": 0}), "clear")

    def test_extract_nested_result_and_reject_error(self):
        result = {"content": [{"type": "text", "text": json.dumps({"result": {"benchmark_probe": True, "documents": []}})}]}
        self.assertEqual(extract_result(result)["documents"], [])
        self.assertRaises(RuntimeError, extract_result, {"isError": True, "content": []})
        self.assertRaises(ValueError, extract_result, {"content": []})

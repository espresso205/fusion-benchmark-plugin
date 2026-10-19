"""Real CLI coverage for v3, with a deliberately unsupported measurement."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import shutil

import evaluate
from src.contract import atomic_write_json
from tests.test_contract_v3 import records

from src.io_utils import sha
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs

BASE = Path(__file__).resolve().parents[1]


class V3PipelineTests(unittest.TestCase):
    def test_worker_crash_after_validation_cannot_score_changed_staged_inputs(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            inputs = {}
            for role in ('gt', 'candidate', 'annotations'):
                p = root/role
                p.write_bytes(role.encode())
                inputs[role] = str(p)
            hashes = {r: sha(p) for r, p in inputs.items()}
            output = root/'run'
            output.mkdir()
            row = {'case': 'Q01', **inputs, 'expected_hashes': hashes}
            def crashed_worker(*args, **kwargs):
                record = records()['validity']
                record.update(input_hashes=hashes, run_manifest_sha256='d'*64)
                atomic_write_json(output/'Q01/results/validity.json', record)
                (output/'Q01/inputs/candidate.step').write_bytes(b'changed after validation')
                class Process:
                    returncode = 1
                    def wait(self, timeout): return 1
                return Process()
            with patch.object(evaluate.subprocess, 'Popen', side_effect=crashed_worker):
                result = evaluate.run_case(row, output, 'd'*64, None, 10, 'fusion-step-adapted-v3.0')
            self.assertIsNone(result['final_score'])
            self.assertFalse(result['participates_in_scoring'])
            self.assertTrue(any(r.get('module') == 'staged_inputs' for r in result['reasons']))

    def test_validation_passed_case_is_scored_with_unavailable_dimension_and_persisted_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            step = root/'box.step'
            writer = STEPControl_Writer()
            writer.Transfer(BRepPrimAPI_MakeBox(10, 20, 30).Shape(), STEPControl_AsIs)
            writer.Write(str(step))
            ann = root/'annotations.json'
            ann.write_text(json.dumps({'gt_sha256': sha(step),
                'items': [{'id': 'unknown', 'type': 'unsupported_probe', 'nominal': 10}],
                'sections': [{'id': 'boundary', 'axis': 'z', 'offset': 30}]}), encoding='utf-8')
            cases = root/'cases.json'
            cases.write_text(json.dumps({'cases': [{'case': 'box', 'gt': str(step),
                'candidate': str(step), 'annotations': str(ann),
                'expected_hashes': {'gt': sha(step), 'candidate': sha(step), 'annotations': sha(ann)}}]}))
            output = root/'run'
            run = subprocess.run([sys.executable, str(BASE/'evaluate.py'), '--cases', str(cases),
                '--output', str(output), '--protocol', 'fusion-step-adapted-v3.0'],
                cwd=BASE, capture_output=True, text=True, timeout=120)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            summary = json.loads((output/'summary.json').read_text(encoding='utf-8'))
            row = summary['cases'][0]
            self.assertTrue(row['validation_passed'])
            self.assertTrue(row['participates_in_scoring'])
            self.assertAlmostEqual(row['final_score'], .75, places=5)
            self.assertAlmostEqual(summary['batch_mean'], .75, places=5)
            self.assertIsNone(row['metrics']['dimension']['measured_score'])
            self.assertEqual(row['metrics']['dimension']['awarded_score'], 0.)
            self.assertEqual(row['score_kind'], 'policy_lower_bound')
            self.assertTrue((output/'box'/'section_plan.json').is_file())
            text = (output/'report.md').read_text(encoding='utf-8')
            self.assertIn('未测得', text)
            self.assertIn('固定权重', text)


if __name__ == '__main__':
    unittest.main()

"""Exercise input attachments through the public report API and offline CLI."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from report_fixtures import make_run
from src.html_reporting import build_report


class InputIntegrationTests(unittest.TestCase):
    def test_inputs_copy_into_portable_report_without_changing_scores(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'scores'; source.mkdir(); make_run(source)
            materials = root / '公开资料'; materials.mkdir()
            asset = materials / 'drawing.dxf'; asset.write_bytes(b'0\nSECTION\n2\nHEADER\n0\nENDSEC\n0\nEOF\n')
            manifest = materials / 'inputs.json'
            # Use an actual scored case from the existing fixture.
            baseline = build_report(source, root / 'baseline', meshes=False)
            def read_data(output):
                text = Path(output['index_html']).read_text(encoding='utf-8')
                return json.loads(re.search(r'<script id="report-data" type="application/json">(.*?)</script>', text, re.S).group(1))
            original = read_data(baseline)
            case = original['cases'][0]['case']
            manifest.write_text(json.dumps({'schema_version': 'fusion-report-inputs/1', 'cases': [{'case': case, 'assets': [
                {'path': 'drawing.dxf', 'label': '工程图', 'sha256': hashlib.sha256(asset.read_bytes()).hexdigest()},
                {'path': 'missing.pdf', 'label': '补充资料', 'sha256': '0' * 64}]}]}, ensure_ascii=False), encoding='utf-8')
            output = build_report(source, root / 'with-inputs', meshes=False, inputs=manifest)
            actual = read_data(output)
            self.assertEqual(actual['summary'], original['summary'])
            self.assertEqual([r['final_score'] for r in actual['cases']], [r['final_score'] for r in original['cases']])
            self.assertEqual(output['status'], 'partial')
            item = actual['cases'][0]['input_materials']['items'][0]
            copied = root / 'with-inputs' / item['url']
            self.assertEqual(copied.read_bytes(), asset.read_bytes())
            self.assertNotIn(str(materials), json.dumps(actual, ensure_ascii=False))
            result_manifest = json.loads((root / 'with-inputs/report-manifest.json').read_text(encoding='utf-8'))
            self.assertIn(item['url'], result_manifest['files'])
            command = [sys.executable, '-B', str(Path(__file__).resolve().parents[1] / 'render_report.py'),
                       '--input', str(source), '--output', str(root / 'cli'), '--inputs', str(manifest), '--no-mesh']
            result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['case_count'], len(original['cases']))


if __name__ == '__main__':
    unittest.main()

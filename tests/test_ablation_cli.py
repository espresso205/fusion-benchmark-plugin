import csv
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
WEIGHTS = dict(shape=.3, dimension=.25, distance=.15, mass=.1, face_match=.1, section=.1)


def fixture(folder):
    (folder / 'results').mkdir(parents=True)
    rows = [('exact', [1, 1, 1, 1, 1, 1], 1.),
            ('shape_error', [.5, 1, 1, 1, 1, 1], .85),
            ('dimension_error', [1, .2, 1, 1, 1, 1], .8)]
    gate = {'angle': 'validity', 'rule': 'candidate must be valid AND watertight; otherwise final score = 0 with status invalid_shape'}
    (folder / 'weights.json').write_text(json.dumps({'weights': WEIGHTS, 'gate': gate}), encoding='utf-8')
    for index, metric in enumerate(WEIGHTS):
        payload = {'angle': metric, 'protocol': 'synthetic test measurement', 'cases': [
            {'case': case, 'status': 'ok', 'score': scores[index], 'raw': {}}
            for case, scores, _ in rows]}
        (folder / 'results' / (metric + '.json')).write_text(json.dumps(payload), encoding='utf-8')
    validity = {'angle': 'validity', 'protocol': 'synthetic test validation', 'cases': [
        {'case': case, 'status': 'ok', 'score': 1., 'raw': {}} for case, _, _ in rows]}
    (folder / 'results' / 'validity.json').write_text(json.dumps(validity), encoding='utf-8')
    final = {'weights': WEIGHTS, 'gate': gate, 'cases': [
        {'case': case, 'status': 'ok', 'final_score': total, **dict(zip(WEIGHTS, scores))}
        for case, scores, total in rows]}
    (folder / 'results' / 'final_scores.json').write_text(json.dumps(final), encoding='utf-8')


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / '结果 空格'
        fixture(self.source)

    def run_cli(self, *args):
        # Site-packages are disabled: offline analysis must need only stdlib.
        return subprocess.run([sys.executable, '-X', 'utf8', '-B', '-S', str(ROOT / 'analyze_ablation.py'), *map(str, args)],
                              cwd=self.root, capture_output=True, text=True, encoding='utf-8')

    def test_creates_three_reports_with_hand_checked_scores_and_unchanged_sources(self):
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.source.rglob('*') if p.is_file()}
        output = self.root / '分析 output'
        result = self.run_cli('--input', self.source, '--output', output)
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads((output / 'ablation.json').read_text(encoding='utf-8'))
        self.assertEqual(doc['schema_version'], 'fusion-ablation/1')
        self.assertFalse(doc['ranking_eligible'])
        variants = doc['analyses'][0]['cohorts']['complete_measurements']['variants']
        self.assertEqual(len(variants), 13)
        removed = next(v for v in variants if v['id'] == 'without_dimension_renormalized')
        self.assertAlmostEqual(next(r for r in removed['ranking'] if r['case'] == 'dimension_error')['score'], 1.)
        with (output / 'ablation.csv').open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 78)
        self.assertIn('original_weight_coverage', rows[0])
        self.assertIn('样本排序', (output / 'ablation.md').read_text(encoding='utf-8'))
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before})
        self.assertFalse((output / 'INCOMPLETE').exists())

    def test_rejects_existing_output_without_overwriting(self):
        output = self.root / 'existing'
        output.mkdir()
        sentinel = output / 'keep.txt'
        sentinel.write_text('unchanged', encoding='utf-8')
        result = self.run_cli('--input', self.source, '--output', output)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(sentinel.read_text(), 'unchanged')
        self.assertEqual(list(output.iterdir()), [sentinel])

    def test_rejects_output_inside_historical_source(self):
        output = self.source / 'new-analysis'
        result = self.run_cli('--input', self.source, '--output', output)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(output.exists())

    def test_results_directory_without_weights_cannot_write_inside_legacy_run(self):
        (self.source / 'weights.json').unlink()
        output = self.source / 'new-analysis'
        result = self.run_cli('--input', self.source / 'results', '--output', output)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('historical input directories', result.stderr)
        self.assertFalse(output.exists())

    def test_final_scores_without_weights_cannot_write_inside_legacy_run(self):
        (self.source / 'weights.json').unlink()
        output = self.source / 'new-analysis'
        result = self.run_cli('--input', self.source / 'results/final_scores.json', '--output', output)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('historical input directories', result.stderr)
        self.assertFalse(output.exists())

    def test_manifest_relative_sources_and_explicit_labels(self):
        manifest = self.root / 'manifest.json'
        manifest.write_text(json.dumps({'sources': [{'path': self.source.name, 'id': '公开样例', 'system': 'model-A'}]},
                                       ensure_ascii=False), encoding='utf-8')
        output = self.root / 'manifest-output'
        result = self.run_cli('--manifest', manifest, '--output', output)
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads((output / 'ablation.json').read_text(encoding='utf-8'))
        self.assertEqual(doc['analyses'][0]['source_id'], '公开样例')
        self.assertEqual(doc['system_comparisons']['status'], 'not_applicable')
        self.assertTrue(any(s['path'] == str(manifest.resolve()) for s in doc['source_files']))

    def test_duplicate_manifest_keys_and_source_ids_are_errors(self):
        manifest = self.root / 'bad.json'
        for content in ('{"sources": [], "sources": []}', json.dumps({'sources': [
            {'path': str(self.source), 'id': 'same'}, {'path': str(self.source), 'id': 'same'}]})):
            with self.subTest(content=content):
                manifest.write_text(content, encoding='utf-8')
                result = self.run_cli('--manifest', manifest, '--output', self.root / 'never-created')
                self.assertEqual(result.returncode, 2)
                self.assertFalse((self.root / 'never-created').exists())

    def test_invalid_input_never_creates_partial_output(self):
        output = self.root / 'bad-output'
        result = self.run_cli('--input', self.root / 'missing', '--output', output)
        self.assertEqual(result.returncode, 2)
        self.assertIn(str(self.root / 'missing'), result.stderr)
        self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()

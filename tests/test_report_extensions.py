"""Public task metadata and private interface inputs have separate contracts."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


def sha(data):
    return hashlib.sha256(data).hexdigest()


class ReportExtensionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def write_manifest(self, schema, cases):
        path = self.root / 'manifest.json'
        path.write_text(json.dumps({'schema_version': schema, 'cases': cases}), encoding='utf-8')
        return path

    def test_explicit_task_and_prompt_survive_without_invented_images(self):
        from src.report_inputs import build_input_assets
        manifest = self.write_manifest('fusion-report-inputs/1', [
            {'case': '201', 'task_type': 'editing', 'prompt': '将孔径改为 8 mm。\n保留底座。', 'assets': []},
            {'case': '101', 'assets': []}])
        result = build_input_assets(manifest, self.root / 'report', ['201', '101'])
        self.assertIn('201', result)
        self.assertEqual(result['201']['task_type'], 'editing')
        self.assertEqual(result['201']['prompt'], '将孔径改为 8 mm。\n保留底座。')
        self.assertEqual(result['201']['items'], [])
        self.assertNotIn('101', result)

    def test_invalid_task_metadata_is_rejected_before_copying(self):
        from src.report_inputs import build_input_assets
        for extra in ({'task_type': 'guess'}, {'prompt': ['not', 'text']}, {'prompt': 'bad\x00'}):
            manifest = self.write_manifest('fusion-report-inputs/1', [{'case': '101', 'assets': [], **extra}])
            with self.assertRaises(ValueError):
                build_input_assets(manifest, self.root / 'report', ['101'])
        self.assertFalse((self.root / 'report').exists())

    def interface_api(self):
        self.assertIsNotNone(importlib.util.find_spec('src.report_interfaces'), 'private interface adapter required')
        from src.report_interfaces import load_interfaces
        return load_interfaces

    def fixture(self, **changes):
        (self.root / 'region.step').write_bytes(b'region')
        entry = {'case': '101', 'gt_sha256': sha(b'gt'),
                 'regions': [{'path': 'region.step', 'sha256': sha(b'region'), 'fit_type': 'KOR'}]}
        entry.update(changes)
        manifest = self.write_manifest('fusion-report-interfaces/1', [entry])
        sources = {'101': {'world': {'reference': {'sha256': sha(b'gt')}}}}
        return manifest, sources

    def test_private_interfaces_require_matching_gt_and_region_bytes(self):
        api = self.interface_api()
        manifest, sources = self.fixture()
        result = api(manifest, sources)
        self.assertEqual(result['101']['regions'][0]['path'], str(self.root / 'region.step'))
        self.assertEqual(result['101']['regions'][0]['fit_type'], 'KOR')
        manifest, sources = self.fixture(gt_sha256='0' * 64)
        self.assertEqual(api(manifest, sources)['101']['error'], 'interface_reference_mismatch')
        manifest, sources = self.fixture()
        (self.root / 'region.step').write_bytes(b'changed')
        rejected = api(manifest, sources)['101']
        self.assertEqual(rejected['error'], 'interface_input_unavailable')
        self.assertEqual(rejected['regions'], [])

    def test_private_interfaces_do_not_escape_root_or_scan_other_cases(self):
        api = self.interface_api()
        for path in ('../region.step', 'C:/private/region.step', '//host/region.step'):
            manifest, sources = self.fixture(regions=[{'path': path, 'sha256': sha(b'region'), 'fit_type': 'KIR'}])
            self.assertEqual(api(manifest, sources)['101']['error'], 'interface_input_unavailable')
        manifest, _ = self.fixture()
        self.assertEqual(api(manifest, {}), {})

    def test_ambiguous_or_unknown_interface_semantics_are_rejected(self):
        api = self.interface_api()
        manifest, sources = self.fixture(regions=[{'path': 'region.step', 'sha256': sha(b'region'), 'fit_type': 'anything'}])
        with self.assertRaises(ValueError):
            api(manifest, sources)

    def test_report_build_integrates_metadata_and_downloads_without_cad(self):
        from src.html_reporting import build_report
        from report_fixtures import make_run
        import re
        source = self.root / 'source'
        make_run(source, kinds=('complete',))
        metadata = self.write_manifest('fusion-report-inputs/1', [
            {'case': 'complete', 'task_type': 'generation', 'prompt': '重建图纸中的零件。', 'assets': []}])
        output = self.root / 'report'
        build_report(source, output, meshes=False, inputs=metadata)
        text = (output / 'index.html').read_text(encoding='utf-8')
        data = json.loads(re.search(r'<script id="report-data" type="application/json">(.*?)</script>', text, re.S).group(1))
        row = data['cases'][0]
        self.assertEqual(row.get('task_type'), 'generation')
        self.assertEqual(row.get('prompt'), '重建图纸中的零件。')
        self.assertIn('candidate', row.get('downloads', {}))
        self.assertTrue((output / data['downloads']['submission_zip']['url']).is_file())
        self.assertNotIn(str(source), text)


if __name__ == '__main__':
    unittest.main()

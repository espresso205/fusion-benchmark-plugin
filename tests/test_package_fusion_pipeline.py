"""Read-only package planning checks; never creates a release bundle."""
import contextlib
import hashlib
import importlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile


class PackagePlanningTests(unittest.TestCase):
    def setUp(self):
        try:
            self.api = importlib.import_module('scripts.package_fusion_pipeline')
        except ImportError:
            self.fail('The whitelist package planner must exist')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        for name in self.api.OVERLAY_FILES:
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'# public overlay\n')
        (self.source / 'examples').mkdir(exist_ok=True)
        (self.source / 'examples/fusion_pipeline_config.json').write_text(json.dumps({
            'model': 'lanyi/gpt-6.1-sol', 'evaluator_zip': 'C:/old-location/archive.zip'
        }))
        (self.source / 'fusion_pipeline').mkdir()
        for name in ('__init__.py', '__main__.py', 'runner.py', 'mcp.py', 'fusion_scripts.py', 'integration_smoke.py'):
            (self.source / 'fusion_pipeline' / name).write_text('# public module\n')
        self.archive = self.root / 'frozen.zip'
        self.files = {name: ('# frozen ' + name).encode() for name in self.api.REQUIRED_EVALUATOR_FILES}
        self.make_archive()

    def make_archive(self, *, tamper=False, extra=None):
        manifest = {'files': {name: {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                              for name, data in self.files.items()}}
        with zipfile.ZipFile(self.archive, 'w') as target:
            for name, data in self.files.items():
                target.writestr('fusion_benchmark_evaluator_v2/' + name, b'tampered' if tamper else data)
            target.writestr('fusion_benchmark_evaluator_v2/PACKAGE_MANIFEST.json', json.dumps(manifest))
            if extra:
                target.writestr(extra, 'unlisted content')

    def test_frozen_evaluator_and_zip_bytes_are_preserved(self):
        plan = self.api.prepare_bundle(self.source, self.archive)
        self.assertEqual(plan['files']['evaluate.py']['content'], self.files['evaluate.py'])
        self.assertEqual(plan['files']['src/io_utils.py']['content'], self.files['src/io_utils.py'])
        self.assertEqual(plan['files']['payload/evaluator.zip']['content'], self.archive.read_bytes())
        self.assertEqual(plan['files']['evaluate.py']['source'], 'frozen-evaluator')
        self.assertEqual(plan['files']['fusion_pipeline/runner.py']['source'], 'pipeline-overlay')

    def test_unlisted_local_configuration_private_data_and_logs_are_excluded(self):
        for name in ('opencode.jsonc', 'local-config.json', 'private/cases.json', 'runs/session.jsonl', 'src/io_utils.py'):
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('private-local-marker')
        plan = self.api.prepare_bundle(self.source, self.archive)
        self.assertNotIn('opencode.jsonc', plan['files'])
        self.assertNotIn('private/cases.json', plan['files'])
        self.assertNotIn('runs/session.jsonl', plan['files'])
        self.assertFalse(any(b'private-local-marker' in row['content'] for row in plan['files'].values()))

    def test_extra_pipeline_python_module_is_not_packaged(self):
        (self.source / 'fusion_pipeline/local_config.py').write_text('PRIVATE_MODULE_MARKER = "do not distribute"')
        plan = self.api.prepare_bundle(self.source, self.archive)
        self.assertEqual({name for name in plan['files'] if name.startswith('fusion_pipeline/')}, {
            'fusion_pipeline/__init__.py', 'fusion_pipeline/__main__.py', 'fusion_pipeline/runner.py',
            'fusion_pipeline/mcp.py', 'fusion_pipeline/fusion_scripts.py', 'fusion_pipeline/integration_smoke.py',
        })
        self.assertFalse(any(b'PRIVATE_MODULE_MARKER' in row['content'] for row in plan['files'].values()))

    def test_local_example_secrets_and_real_paths_are_replaced_with_public_template(self):
        example_path = self.source / 'examples/fusion_pipeline_config.json'
        example_path.write_text(json.dumps({
            'model': 'lanyi/gpt-6.1-sol', 'api_key': 'PRIVATE_EXAMPLE_SECRET',
            'headers': {'Authorization': 'PRIVATE_EXAMPLE_SECRET'},
            'python': 'Z:/PRIVATE_EXAMPLE_PATH/python.exe',
            'private_cases': 'Z:/PRIVATE_EXAMPLE_PATH/cases.json',
        }))
        before = example_path.read_bytes()
        plan = self.api.prepare_bundle(self.source, self.archive)
        example = json.loads(plan['files']['examples/fusion_pipeline_config.json']['content'])
        self.assertEqual(set(example), {'model', 'evaluator_zip', 'opencode', 'python', 'questions_root',
                                       'private_cases', 'runs_root', 'private_runs_root', 'fusion_skill'})
        self.assertTrue(all(value.startswith('C:/benchmark/') for key, value in example.items() if key != 'model'))
        self.assertEqual(example['model'], 'lanyi/gpt-6.1-sol')
        self.assertEqual(plan['files']['examples/fusion_pipeline_config.json']['source'], 'generated')
        self.assertFalse(any(b'PRIVATE_EXAMPLE_' in row['content'] for row in plan['files'].values()))
        self.assertEqual(example_path.read_bytes(), before)

    def test_expected_evaluator_hash_mismatch_stops_before_output(self):
        output = self.root / 'never-created'
        with self.assertRaisesRegex(ValueError, 'SHA256 mismatch'):
            self.api.main(['--source-root', str(self.source), '--evaluator-zip', str(self.archive),
                           '--output-parent', str(output), '--expected-evaluator-sha256', '0' * 64])
        self.assertFalse(output.exists())

    def test_expected_evaluator_hash_is_verified_and_recorded(self):
        expected = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        output = self.root / 'never-created'
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            code = self.api.main(['--source-root', str(self.source), '--evaluator-zip', str(self.archive),
                                  '--output-parent', str(output), '--expected-evaluator-sha256', expected.upper(), '--check-only'])
        self.assertEqual(code, 0)
        result = json.loads(captured.getvalue())
        self.assertEqual(result['evaluator_sha256'], expected)
        self.assertEqual(result['expected_evaluator_sha256'], expected)
        self.assertFalse(output.exists())

    def test_invalid_expected_hash_is_rejected(self):
        with self.assertRaisesRegex(ValueError, '64 hexadecimal'):
            self.api.prepare_bundle(self.source, self.archive, expected_evaluator_sha256='not-a-hash')

    def test_tampered_or_extra_archive_entries_are_rejected(self):
        self.make_archive(tamper=True)
        with self.assertRaisesRegex(ValueError, 'hash|size'):
            self.api.prepare_bundle(self.source, self.archive)
        self.make_archive(extra='../outside.txt')
        with self.assertRaisesRegex(ValueError, 'manifest|unsafe'):
            self.api.prepare_bundle(self.source, self.archive)

    def test_manifest_cannot_authorize_a_traversal_path(self):
        self.files['../outside.txt'] = b'bad'
        self.make_archive()
        with self.assertRaisesRegex(ValueError, 'unsafe'):
            self.api.prepare_bundle(self.source, self.archive)

    def test_example_targets_bundled_payload_and_readme_uses_pipeline_entry(self):
        plan = self.api.prepare_bundle(self.source, self.archive)
        example = json.loads(plan['files']['examples/fusion_pipeline_config.json']['content'])
        self.assertEqual(example['evaluator_zip'], 'C:/benchmark/payload/evaluator.zip')
        self.assertEqual(plan['files']['README.md']['source'], 'generated')
        self.assertEqual(plan['files']['AGENTS.md']['source'], 'generated')

    def test_missing_required_overlay_stops_planning(self):
        (self.source / 'plugins/fusion-benchmark-guard.mjs').unlink()
        with self.assertRaisesRegex(ValueError, 'missing'):
            self.api.prepare_bundle(self.source, self.archive)

    def test_check_only_does_not_create_output_directory(self):
        output = self.root / 'never-created'
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            code = self.api.main(['--source-root', str(self.source), '--evaluator-zip', str(self.archive),
                                  '--output-parent', str(output), '--check-only'])
        self.assertEqual(code, 0)
        self.assertFalse(output.exists())
        self.assertEqual(json.loads(captured.getvalue())['mode'], 'check-only')


if __name__ == '__main__':
    unittest.main()

"""Private HTML integration: no Fusion or model requests are made."""
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from fusion_pipeline import runner
from fusion_pipeline.service import ManagementService
from scripts import build_portable_plugin


class HtmlPluginTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.trial = runner.Trial.__new__(runner.Trial)
        self.trial.run = self.root / 'runs' / 'fusion-test'
        self.trial.run.mkdir(parents=True)
        self.trial.private = self.root / 'private' / 'fusion-test'
        self.trial.private.mkdir(parents=True)
        self.candidate = self.trial.run / 'ledger' / 'candidate' / 'Q02'
        self.candidate.mkdir(parents=True)
        (self.candidate / 'TASK.md').write_text('public question', encoding='utf-8')
        self.trial.config = {'runs_root': str(self.root / 'runs'),
                             'private_runs_root': str(self.root / 'private')}
        self.trial.state = dict(state='completed_with_missing_candidates',
            scoring_protocol='fusion-step-adapted-v4.0', coordinate_mode='geometry',
            ranking_eligible=False, batch_complete=False,
            cases=[dict(case_id='Q01', state='evaluation_complete', final_score=.8,
                        evaluation_status='ok', execution={'elapsed_s': 1}),
                   dict(case_id='Q02', state='not_submitted', workspace=str(self.candidate))])

    def fake_renderer(self, source, output, **kwargs):
        # The renderer boundary replaces expensive geometry only; keep adapter,
        # runner persistence, private path selection and MCP projection real.
        self.assertEqual(output, self.trial.private / 'html-report')
        self.assertEqual([r['case_id'] for r in kwargs['pipeline']['cases']], ['Q01', 'Q02'])
        self.assertEqual(kwargs['timeout'], 10.)
        output.mkdir(exist_ok=False)
        (output / 'index.html').write_text('private reference preview', encoding='utf-8')
        (output / 'manifest.json').write_text('{}', encoding='utf-8')
        return dict(status='completed', index_html=str(output / 'index.html'),
                    manifest=str(output / 'manifest.json'), case_count=2)

    def renderer_module(self, builder=None):
        module = types.ModuleType('src.html_reporting')
        module.try_build_report = builder or self.fake_renderer
        return patch.dict(sys.modules, {'src.html_reporting': module})

    def test_runner_generates_private_report_with_pipeline_only_cases(self):
        before = dict(self.trial.state)
        with self.renderer_module():
            self.trial.report()
        self.assertTrue((self.trial.private / 'html-report/index.html').is_file())
        self.assertEqual(self.trial.state, before)
        self.assertEqual(list(self.candidate.iterdir()), [self.candidate / 'TASK.md'])
        self.assertFalse((self.trial.run / 'html-report').exists())
        status = json.loads((self.trial.private / 'html-report-generation.json').read_text(encoding='utf-8'))
        self.assertEqual(status['status'], 'completed')

    def test_report_failure_preserves_completed_scores_and_exit_code(self):
        trial = self.trial
        trial.state['cases'] = [dict(case_id='Q01', state='evaluation_complete',
            evaluation_complete=True, final_score=.8, execution={'elapsed_s': 1})]
        trial.prepare = trial.smoke = trial.evaluate = lambda: None
        trial.model = lambda row: None
        trial._processes = []
        def broken_renderer(*args, **kwargs):
            raise RuntimeError('PRIVATE_REFERENCE_PATH must not be published')
        with self.renderer_module(broken_renderer):
            code = trial.run_all()
        self.assertEqual(code, 0)
        self.assertEqual(trial.state['state'], 'completed')
        self.assertEqual(trial.state['cases'][0]['final_score'], .8)
        status_path = trial.private / 'html-report-generation.json'
        self.assertTrue(status_path.is_file())
        status = json.loads(status_path.read_text(encoding='utf-8'))
        self.assertEqual(status['status'], 'failed')
        self.assertNotIn('PRIVATE_REFERENCE_PATH', json.dumps(status))

    def test_historical_protocol_does_not_generate_html(self):
        self.trial.state['scoring_protocol'] = 'fusion-step-adapted-v3.0'
        def unexpected(*args, **kwargs):
            self.fail('v3 must retain its existing reporting behavior')
        with self.renderer_module(unexpected):
            self.trial.report()
        self.assertFalse((self.trial.private / 'html-report').exists())
        self.assertTrue((self.trial.run / 'report.md').is_file())

    def test_real_pipeline_only_report_keeps_unscored_tasks_and_omits_private_inputs(self):
        from fusion_pipeline.reporting import build_private_report
        self.trial.state['cases'][0].update(reference='PRIVATE_REFERENCE_MARKER', annotations='PRIVATE_ANNOTATION_MARKER')
        result = build_private_report(self.trial.private, self.trial.state, config=self.trial.config)
        self.assertIn(result['status'], ('completed', 'partial'), result)
        html = Path(result['index_html']).read_text(encoding='utf-8')
        embedded = re.search(r'<script id="report-data" type="application/json">(.*?)</script>', html, re.S)
        self.assertIsNotNone(embedded)
        data = json.loads(embedded.group(1))
        self.assertEqual([row['case'] for row in data['cases']], ['Q01', 'Q02'])
        self.assertEqual(data['summary']['pipeline_unscored_case_count'], 2)
        self.assertTrue(all(row['final_score'] is None for row in data['cases']))
        self.assertFalse(data['ranking_eligible'])
        self.assertNotIn('PRIVATE_REFERENCE_MARKER', html)
        self.assertNotIn('PRIVATE_ANNOTATION_MARKER', html)
        self.assertNotIn(str(self.candidate), html)
        self.assertEqual(list(self.candidate.iterdir()), [self.candidate / 'TASK.md'])

    def service(self):
        identifier = 'a' * 32
        folder = self.root / 'runs/.service' / identifier
        folder.mkdir(parents=True)
        runner.atomic(folder / 'job.json', dict(run_id=identifier, state='completed', run_path=str(self.trial.run)))
        runner.atomic(self.trial.run / 'pipeline-status.json', self.trial.state)
        config = self.root / 'config.json'
        runner.atomic(config, self.trial.config)
        return ManagementService(config), identifier

    def test_results_only_link_existing_reports_and_safe_generation_status(self):
        service, identifier = self.service()
        absent = service.call('results', {'run_id': identifier})
        self.assertNotIn('report', absent)
        self.assertFalse(absent.get('files'))
        with self.renderer_module():
            self.trial.report()
        result = service.call('results', {'run_id': identifier})
        self.assertEqual(Path(result['files']['html']), self.trial.private / 'html-report/index.html')
        self.assertEqual(result['html_report_generation']['status'], 'completed')
        self.assertTrue(all(Path(p).is_file() for p in result['files'].values()))
        (self.trial.private / 'html-report/index.html').unlink()
        missing = service.call('results', {'run_id': identifier})
        self.assertNotIn('html', missing.get('files', {}))

    def test_snapshot_includes_report_entrypoints_but_excludes_unlisted_assets(self):
        source = self.root / 'source'
        for name in ('evaluate.py', 'worker.py', 'weights.json', 'render_report.py', 'serve_report.py',
                     'src/report_data.py', 'src/report_inputs.py', 'src/html_reporting.py', 'src/display_assets.py',
                     'src/display_mesh.py', 'src/display_worker.py', 'report_assets/report.css',
                     'report_assets/report.js', 'report_assets/VIEWER_SOURCES.json',
                     'report_assets/viewer/comparison-view.js', 'report_assets/viewer/mesh-validation.js',
                     'report_assets/viewer/preview-geometry-worker.js', 'report_assets/vendor/three.module.js',
                     'report_assets/licenses/CADViewer-MIT.txt', 'report_assets/licenses/Three-MIT.txt'):
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('{}' if name.endswith('.json') else '# fixture', encoding='utf-8')
        for name in ('report_assets/private.txt', 'report_assets/secret.json', 'html-report/index.html',
                     'report_assets/viewer/private.js', 'report_assets/vendor/private.json'):
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('private marker', encoding='utf-8')
        output = runner.snapshot_evaluator(source, self.root / 'snapshot')
        self.assertTrue((output / 'render_report.py').is_file())
        self.assertTrue((output / 'serve_report.py').is_file())
        self.assertTrue((output / 'report_assets/report.js').is_file())
        self.assertTrue((output / 'report_assets/viewer/comparison-view.js').is_file())
        self.assertTrue((output / 'report_assets/vendor/three.module.js').is_file())
        self.assertTrue((output / 'report_assets/licenses/CADViewer-MIT.txt').is_file())
        self.assertFalse(any('private marker' in p.read_text(encoding='utf-8') for p in output.rglob('*') if p.is_file()))

    def test_portable_plan_includes_report_runtime_and_private_adapter(self):
        required = set(build_portable_plugin.REQUIRED_SOURCE_FILES)
        self.assertTrue({'render_report.py', 'serve_report.py', 'src/report_data.py',
            'src/html_reporting.py', 'src/report_inputs.py', 'src/display_assets.py', 'src/display_mesh.py',
            'src/display_worker.py', 'fusion_pipeline/reporting.py',
            'report_assets/report.css', 'report_assets/report.js',
            'report_assets/VIEWER_SOURCES.json', 'report_assets/viewer/comparison-view.js',
            'report_assets/viewer/mesh-validation.js', 'report_assets/viewer/preview-geometry-worker.js',
            'report_assets/vendor/three.module.js', 'report_assets/licenses/CADViewer-MIT.txt',
            'report_assets/licenses/Three-MIT.txt'} <= required)

    def test_offline_report_help_requires_no_pipeline_configuration(self):
        result = subprocess.run([sys.executable, '-B', '-m', 'fusion_pipeline.cli', 'report', '--help'],
                                capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--input', result.stdout)
        self.assertIn('--output', result.stdout)


if __name__ == '__main__':
    unittest.main()

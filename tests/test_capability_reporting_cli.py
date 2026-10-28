"""Offline study commands and public report provenance; no model or CAD calls."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from tests import test_capability_study as study_fixtures


class CapabilityCommandTests(unittest.TestCase):
    def test_plan_and_analyze_without_service_or_processes(self):
        from fusion_pipeline.cli import main
        fixture = study_fixtures.CapabilityStudyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        spec = fixture.root / 'spec.json'
        spec.write_text(json.dumps(fixture.small_spec()), encoding='utf-8')
        with patch('subprocess.Popen', side_effect=AssertionError('offline process')), \
                patch('subprocess.run', side_effect=AssertionError('offline process')), \
                patch('fusion_pipeline.service.ManagementService', side_effect=AssertionError('offline service')), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['capability-study', 'plan', '--spec', str(spec),
                                   '--output', str(fixture.root / 'plan')]), 0)
            plan = json.loads((fixture.root / 'plan/experiment-plan.json').read_text(encoding='utf-8'))
            run = fixture.manifest(plan['cells'][0])
            self.assertEqual(main(['capability-study', 'analyze', '--plan',
                                   str(fixture.root / 'plan/experiment-plan.json'), '--runs', str(run),
                                   '--output', str(fixture.root / 'analysis')]), 0)
        for suffix in ('json', 'csv', 'md'):
            self.assertTrue((fixture.root / f'analysis/capability-analysis.{suffix}').is_file())


class CapabilityReportTests(unittest.TestCase):
    def selection(self, profile='claude_low', level='low'):
        return {'candidate_profile_id': profile, 'provider': 'claude', 'requested_model': 'model-a',
                'requested_reasoning': {'mode': 'explicit', 'control': 'effort', 'value': level},
                'resolved_reasoning': {'mode': 'explicit', 'control': 'effort', 'value': level},
                'reasoning_verification_status': 'not_observed',
                'reasoning_support_evidence': {'status': 'verified', 'secret': 'PRIVATE_MARKER'},
                'capability_selection': {'skills': [], 'mcp_servers': ['cad_native'],
                                         'exposure_mode': 'gateway_proxy', 'secret': 'PRIVATE_MARKER'},
                'actual_skill_reads': [], 'per_server_tool_calls': {'cad_native': 2},
                'exposure_verification_status': 'verified', 'registry_path': 'PRIVATE_MARKER'}

    def test_allowlisted_case_and_batch_evidence_with_historical_unknown(self):
        from src.report_data import load_report
        from src.html_reporting import render_html
        pipeline = {'candidate_selection': self.selection(), 'cases': [
            {'case_id': 'Q01', 'state': 'failed', 'group_id': 'A', 'candidate_selection': self.selection()},
            {'case_id': 'Q02', 'state': 'failed', 'experiment': {'group_id': 'B', 'secret': 'PRIVATE_MARKER'},
             'candidate_selection': self.selection('claude_high', 'high')},
            {'case_id': 'Q03', 'state': 'failed'}]}
        data = load_report(None, pipeline=pipeline).data
        self.assertEqual(data['candidate_selection']['candidate_profile_id'], 'claude_low')
        first, second, historic = data['cases']
        self.assertEqual(first['candidate_selection']['actual_skill_reads'], [])
        self.assertEqual(first['group_id'], 'A')
        self.assertEqual(second['group_id'], 'B')
        self.assertNotIn('candidate_selection', historic)
        html = render_html(data)
        self.assertNotIn('PRIVATE_MARKER', html)
        for text in ('comparison-first-v2', 'profile-filter', 'group-filter', 'reasoning-filter',
                     'claude_high', 'gateway_proxy', '历史运行，未记录能力选择', '实际使用'):
            self.assertIn(text, html)
        self.assertLess(html.index('visual-panel'), html.index('candidate-evidence'))

    def test_single_value_does_not_add_redundant_filters(self):
        from src.report_data import load_report
        from src.html_reporting import render_html
        data = load_report(None, pipeline={'cases': [
            {'case_id': 'Q01', 'candidate_selection': self.selection(), 'group_id': 'A'}]}).data
        html = render_html(data)
        self.assertNotIn('id="profile-filter"', html)
        self.assertNotIn('id="group-filter"', html)
        self.assertNotIn('id="reasoning-filter"', html)


class CapabilityPackageClosureTests(unittest.TestCase):
    def image_files(self):
        from scripts import build_portable_plugin as portable, package_fusion_pipeline as overlay
        overlay_files = set(overlay.OVERLAY_FILES) | {'fusion_pipeline/' + n for n in overlay.REQUIRED_PIPELINE_MODULES}
        # These files are retained unchanged from the frozen evaluator payload.
        overlay_files.update(overlay.REQUIRED_EVALUATOR_FILES)
        return [('portable', set(portable.REQUIRED_SOURCE_FILES)), ('overlay', overlay_files)]

    def write_relocated_image(self, root, name, files):
        source = Path(__file__).resolve().parents[1]
        initial = root / (name + '-initial')
        for relative in files:
            target = initial / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((source / relative).read_bytes())
        relocated = root / ('运行 image ' + name)
        initial.rename(relocated)
        return relocated

    def image_cli(self, image, *args):
        script = ('import sys,subprocess; sys.path.insert(0,sys.argv.pop(1)); '
                  'deny=lambda *a,**k: (_ for _ in ()).throw(AssertionError("offline child")); '
                  'subprocess.Popen=deny; subprocess.run=deny; '
                  'from fusion_pipeline.cli import main; code=main(); '
                  'assert sys.argv[2]!="analyze" or "fusion_pipeline.runner" not in sys.modules; '
                  'raise SystemExit(code)')
        return subprocess.run([sys.executable, '-X', 'utf8', '-B', '-I', '-S', '-c', script,
                               str(image), *map(str, args)], cwd=image.parent,
                              capture_output=True, text=True, encoding='utf-8', timeout=20)

    def test_whitelisted_runtime_images_execute_offline_plan_and_analysis(self):
        fixture = study_fixtures.CapabilityStudyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        spec = fixture.root / 'spec.json'
        draft = fixture.small_spec()
        draft.update(common_identity_mode='draft', common={}, admission={'status': 'not_verified'})
        spec.write_text(json.dumps(draft), encoding='utf-8')
        for name, files in self.image_files():
            with self.subTest(image=name):
                image = self.write_relocated_image(fixture.root, name, files)
                plan_dir = fixture.root / (name + '-plan')
                result = self.image_cli(image, 'capability-study', 'plan', '--spec', spec, '--output', plan_dir)
                self.assertEqual(result.returncode, 0, result.stderr)
                plan = json.loads((plan_dir / 'experiment-plan.json').read_text(encoding='utf-8'))
                self.assertEqual(plan['common_identity_validation']['status'], 'incomplete')
                self.assertEqual(plan['common_identity_capture']['status'], 'not_captured')
                from fusion_pipeline.capability_study import run_manifest_template
                run = fixture.root / (name + '-run.json')
                run.write_text(json.dumps(run_manifest_template(plan_dir / 'experiment-plan.json',
                    plan['cells'][0]['cell_id'], 'fixture_run', 'attempt_1')), encoding='utf-8')
                analysis = fixture.root / (name + '-analysis')
                result = self.image_cli(image, 'capability-study', 'analyze', '--plan', plan_dir / 'experiment-plan.json',
                                        '--runs', run, '--output', analysis)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((analysis / 'capability-analysis.md').is_file())

    def test_installed_images_capture_actual_identity_and_reject_mutations(self):
        fixture = study_fixtures.CapabilityStudyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        def sha(path):
            return hashlib.sha256(path.read_bytes()).hexdigest()
        def digest(value):
            return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
        for name, files in self.image_files():
            with self.subTest(image=name):
                image = self.write_relocated_image(fixture.root, name, files)
                data = image / 'synthetic-data'
                shutil.copytree(fixture.questions, data / 'questions')
                private = json.loads(fixture.private_cases.read_text(encoding='utf-8'))
                for row in private['cases']:
                    for role in ('gt', 'annotations'):
                        target = data / Path(row[role]).name
                        shutil.copyfile(row[role], target)
                        row[role] = str(target)
                private_path = data / 'private-cases.json'
                private_path.write_text(json.dumps(private), encoding='utf-8')
                spec = fixture.small_spec()
                spec.update(common_identity_mode='capture', common={}, admission={'status': 'not_verified'})
                spec['base_config'].update(questions_root=str(data / 'questions'), private_cases=str(private_path),
                                           evaluator_directory=str(image))
                spec_path = fixture.root / (name + '-capture-spec.json')
                spec_path.write_text(json.dumps(spec), encoding='utf-8')
                plan_dir = fixture.root / (name + '-captured-plan')
                result = self.image_cli(image, 'capability-study', 'plan', '--spec', spec_path, '--output', plan_dir)
                self.assertEqual(result.returncode, 0, result.stderr)
                plan_path = plan_dir / 'experiment-plan.json'
                plan = json.loads(plan_path.read_text(encoding='utf-8'))
                common = plan['common']
                self.assertEqual(plan['common_identity_capture']['status'], 'captured')
                self.assertEqual(plan['common_identity_validation']['status'], 'complete')
                self.assertIn('fusion_pipeline/capability_study.py', common['plugin_source_files'])
                for relative, expected in common['plugin_source_files'].items():
                    self.assertEqual(expected, sha(image / relative), relative)
                self.assertEqual(common['plugin_source_sha256'], digest(common['plugin_source_files']))
                for relative, expected in common['evaluator_source_files'].items():
                    self.assertEqual(expected, {'bytes': (image / relative).stat().st_size,
                                                'sha256': sha(image / relative)}, relative)
                weight_file = {'fusion-step-adapted-v4.0': 'weights_v4.json',
                               'fusion-step-adapted-v2.0': 'weights.json'}[common['scoring_protocol']]
                self.assertEqual(common['evaluator_weights_sha256'], digest({
                    'files': common['evaluator_source_files'], 'protocol': common['scoring_protocol'],
                    'weights_sha256': sha(image / weight_file)}))
                protocol_digest = hashlib.sha256()
                for filename in ('PROTOCOL.md', 'SOLIDWORKS.md', 'FUSION.md'):
                    raw = (image / 'agent/common-protocol' / filename).read_bytes()
                    protocol_digest.update(filename.encode() + b'\0' + str(len(raw)).encode() + b'\0' + raw)
                self.assertEqual(common['common_protocol_sha256'], protocol_digest.hexdigest())
                inputs, references = [], []
                for captured, row in zip(common['case_identities'], private['cases'], strict=True):
                    manifest = captured['input_manifest']
                    for asset in [manifest['task'], *manifest['attachments']]:
                        self.assertEqual(asset['sha256'], sha(data / 'questions' / row['case'] / asset['path']))
                    self.assertEqual(captured['input_manifest_sha256'], digest(manifest))
                    self.assertEqual(captured['gt_sha256'], sha(Path(row['gt'])))
                    self.assertEqual(captured['annotations_sha256'], sha(Path(row['annotations'])))
                    inputs.append({'case_id': row['case'], 'input_manifest': manifest})
                    references.append({'case_id': row['case'], 'gt': sha(Path(row['gt'])),
                                       'annotations': sha(Path(row['annotations']))})
                self.assertEqual(common['input_manifest_sha256'], digest(inputs))
                self.assertEqual(common['gt_annotations_sha256'], digest(references))
                self.assertEqual(common['dataset_sha256'], digest({
                    'input_manifest_sha256': digest(inputs), 'gt_annotations_sha256': digest(references)}))
                # A verification plan uses this image's captured identity, never checkout hashes.
                spec.update(common_identity_mode='verify', common=common)
                spec_path.write_text(json.dumps(spec), encoding='utf-8')
                result = self.image_cli(image, 'capability-study', 'plan', '--spec', spec_path,
                                        '--output', fixture.root / (name + '-verified-plan'))
                self.assertEqual(result.returncode, 0, result.stderr)
                mutations = [(image / 'fusion_pipeline/candidate_prompt.py', 'planned_common_identity_mismatch'),
                             (image / 'agent/common-protocol/FUSION.md', 'planned_common_identity_mismatch'),
                             (image / 'worker.py', 'planned_common_identity_mismatch'),
                             (data / 'questions/Q01/TASK.md', 'planned_common_identity_mismatch'),
                             (Path(private['cases'][0]['gt']), 'study_private_gt_hash_mismatch'),
                             (Path(private['cases'][0]['annotations']), 'study_private_annotations_hash_mismatch')]
                for index, (target, error) in enumerate(mutations):
                    original = target.read_bytes()
                    try:
                        target.write_bytes(original + b'\n# changed installed bytes\n')
                        rejected_output = fixture.root / f'{name}-rejected-{index}'
                        result = self.image_cli(image, 'capability-study', 'plan', '--spec', spec_path,
                                                '--output', rejected_output)
                        self.assertNotEqual(result.returncode, 0, target)
                        self.assertIn(error, result.stderr)
                        self.assertFalse(rejected_output.exists())
                    finally:
                        target.write_bytes(original)
                from fusion_pipeline.capability_study import run_manifest_template
                run = fixture.root / (name + '-captured-run.json')
                run.write_text(json.dumps(run_manifest_template(plan_path, plan['cells'][0]['cell_id'],
                    'synthetic_run', 'attempt_1')), encoding='utf-8')
                # Saved analysis remains independent of installed source/data identity recapture.
                shutil.rmtree(data)
                (image / 'worker.py').write_text('# unavailable original scorer\n', encoding='utf-8')
                analysis_dir = fixture.root / (name + '-captured-analysis')
                result = self.image_cli(image, 'capability-study', 'analyze', '--plan', plan_path,
                                        '--runs', run, '--output', analysis_dir)
                self.assertEqual(result.returncode, 0, result.stderr)
                for suffix in ('json', 'csv', 'md'):
                    self.assertTrue((analysis_dir / f'capability-analysis.{suffix}').is_file())

    def test_both_real_packaging_whitelists_include_public_runtime_closure(self):
        from scripts import build_portable_plugin as portable, package_fusion_pipeline as overlay
        required = {'fusion_pipeline/' + name + '.py' for name in (
            'capabilities', 'candidate_profiles', 'reasoning', 'candidate_config', 'candidate_prompt',
            'aux_mcp', 'capability_study', 'candidate_runtime', 'cli', 'service', 'worker_gateway', 'adapters')}
        required.update({'agent/common-protocol/' + name + '.md' for name in ('PROTOCOL', 'FUSION', 'SOLIDWORKS')})
        required.update({'agent/fusion-guidance/SKILL.md', 'agent/solidworks-guidance/SKILL.md',
                         'docs/CAPABILITY_STUDY.md', 'docs/CANDIDATE_CAPABILITIES.md', 'docs/CANDIDATE_CLIS.md', 'docs/PROTOCOL_SKILL_SPLIT.md',
                         'examples/candidate-profiles.template.json', 'examples/candidate-capabilities.template.json'})
        source = Path(__file__).resolve().parents[1]
        overlay_files = set(overlay.OVERLAY_FILES) | {'fusion_pipeline/' + n for n in overlay.REQUIRED_PIPELINE_MODULES}
        for files in (set(portable.REQUIRED_SOURCE_FILES), overlay_files):
            self.assertFalse(required - files, sorted(required - files))
        self.assertTrue(all((source / name).is_file() for name in required))

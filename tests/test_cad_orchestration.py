"""CAD orchestration boundaries; no model process or CAD application is invoked."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class CadOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {name: str(self.root / name) for name in (
            'python', 'questions_root', 'private_cases', 'runs_root', 'private_runs_root', 'fusion_skill')}
        self.config.update(provider='claude', model='test/model', execution_backend='unified_gateway',
                           fusion_connection={'url': 'http://127.0.0.1:9000/mcp'}, case_ids=['Q01'])

    def trial(self, **changes):
        from fusion_pipeline.runner import Trial
        path = self.root / 'config.json'
        path.write_text(json.dumps(dict(self.config, **changes)), encoding='utf-8')
        return Trial(path)

    def test_unknown_backend_is_rejected_before_run_creation(self):
        with self.assertRaisesRegex(ValueError, 'backend'):
            self.trial(cad_backend='unknown')
        self.assertFalse(Path(self.config['runs_root']).exists())

    def test_missing_solidworks_runtime_is_rejected_before_run_creation(self):
        with self.assertRaisesRegex(ValueError, 'runtime'):
            self.trial(cad_backend='solidworks', fusion_connection=None,
                       solidworks_connection={'bundle_path': str(self.root / 'missing-runtime')})
        self.assertFalse(Path(self.config['runs_root']).exists())

    def test_preflight_checks_strict_configuration_without_constructing_cad_adapter(self):
        from fusion_pipeline.service import ManagementService
        path = self.root / 'config.json'
        path.write_text(json.dumps(dict(self.config, provider='codex', strict_tool_isolation=True)))
        with (patch('fusion_pipeline.cad.create_adapter', side_effect=AssertionError('static preflight cannot connect')),
              patch('fusion_pipeline.adapters.probe', return_value={'available': False})):
            result = ManagementService(path).call('preflight', {})
        self.assertEqual(result['configuration_validation']['status'], 'failed')
        self.assertFalse(result['cad']['available'])
        self.assertFalse(result['cad']['cad_connection']['executed'])

    def test_strict_codex_is_rejected_before_run_creation(self):
        with self.assertRaisesRegex(ValueError, 'strict|native tool'):
            self.trial(provider='codex', strict_tool_isolation=True)
        self.assertFalse(Path(self.config['runs_root']).exists())

    def test_legacy_config_selects_fusion_and_freezes_case_identity(self):
        trial = self.trial()
        self.assertEqual(trial.state.get('cad_backend'), 'fusion')
        self.assertEqual(trial.state.get('schema_version'), 'cad-trial/1')
        row = trial.state['cases'][0]
        self.assertEqual(row.get('protocol_id'), 'fusion-step-adapted-v4.0')
        self.assertEqual(row.get('run_id'), trial.run.name)
        self.assertEqual(row.get('cad_config_sha256'), trial.state.get('cad_config_sha256'))
        self.assertTrue(row.get('attempt_id'))

    def test_management_accepts_backend_override_but_rejects_it_before_process_launch(self):
        from fusion_pipeline.service import ManagementService, dispatch
        path = self.root / 'config.json'
        path.write_text(json.dumps(self.config), encoding='utf-8')
        service = ManagementService(path)
        properties = next(t for t in service.tools if t['name'] == 'start_run')['inputSchema']['properties']
        self.assertIn('cad_backend', properties)
        with patch('fusion_pipeline.service.subprocess.Popen', side_effect=AssertionError('model process must not start')):
            result = dispatch(service, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': 'start_run', 'arguments': {'cad_backend': 'unknown'}}})
        self.assertTrue(result['result']['isError'])
        self.assertFalse(Path(self.config['runs_root']).exists())

    def test_report_includes_cad_provenance_without_candidate_claims_or_private_paths(self):
        from src.report_data import load_report
        from src.html_reporting import render_html
        pipeline = {'cad_backend': 'solidworks', 'cad_config_sha256': 'a' * 64,
            'cad_identity': {'version': '34.4', 'bridge_version': '0.2.1rc2', 'adapter_version': 'adapter-test-1',
                             'adapter_sha256': 'c' * 64, 'helper_sha256': 'd' * 64, 'secret': 'PRIVATE'},
            'cases': [{'case_id': 'Q01', 'state': 'trusted_export_failed', 'failure_stage': 'trusted_export',
                       'cad_backend': 'solidworks', 'worker_report': {'version': 'FALSE_CANDIDATE_VERSION'},
                       'native_document': {'format': 'SLDPRT', 'configuration': 'Default', 'sha256': 'b' * 64,
                                           'path': 'PRIVATE_PATH'}, 'trusted_step': 'PRIVATE_PATH'}]}
        data = load_report(None, pipeline=pipeline).data
        self.assertEqual(data.get('cad_backend'), 'solidworks')
        self.assertEqual(data['cases'][0].get('failure_stage'), 'trusted_export')
        self.assertEqual(data['cases'][0].get('native_document', {}).get('configuration'), 'Default')
        rendered = render_html(data)
        self.assertIn('solidworks', rendered)
        self.assertIn('Default', rendered)
        visible = rendered.split('<script id="report-data"', 1)[0]
        self.assertIn('SolidWorks', visible)
        self.assertIn('失败阶段：模型导出', visible)
        for technical in ('34.4', '0.2.1rc2', 'adapter-test-1', 'SHA256', 'SLDPRT', 'Default',
                          'a' * 64, 'b' * 64, 'c' * 64, 'd' * 64):
            self.assertNotIn(technical, visible)
        from tests.test_html_reporting import Page
        self.assertEqual(json.loads(''.join(Page(rendered).data)), data)
        self.assertNotIn('PRIVATE', rendered)
        self.assertNotIn('FALSE_CANDIDATE_VERSION', rendered)

    def test_normalized_fusion_report_preserves_verified_metadata(self):
        from fusion_pipeline.reporting import trusted_export_metadata
        evidence = {'status': 'passed', 'native_reopen': {
            'dependencies': {'closed': True},
            'health': {'warnings': [{'name': 'Fillet', 'message': 'Review small face', 'health': 'warning',
                                     'private_path': 'must-not-publish'}]}}}
        projected = trusted_export_metadata(evidence)
        self.assertEqual(projected['dependency_status'], 'self_contained_verified')
        self.assertEqual(projected['warnings'], [{'name': 'Fillet', 'message': 'Review small face', 'health': 'warning'}])

    def test_allowed_native_open_and_export_warnings_are_visible_without_private_paths(self):
        from fusion_pipeline.reporting import trusted_export_metadata
        from fusion_pipeline.solidworks_helper import classify_native_warnings
        from src.report_data import load_report
        from src.html_reporting import render_html
        opened, exported = classify_native_warnings('load', 2), classify_native_warnings('save', 128)
        opened['path'] = 'PRIVATE_OPEN_PATH'
        exported['flags'][0]['traceback'] = 'PRIVATE_EXPORT_PATH'
        metadata = trusted_export_metadata({'status': 'passed', 'native_reopen': {
            'open_warnings': opened, 'export_warnings': exported}})
        self.assertEqual([item['code'] for item in metadata['warnings']], [2, 128])
        self.assertEqual([item['phase'] for item in metadata['warnings']], ['native_reopen', 'step_export'])
        self.assertTrue(all(item['classification'] == 'allowed' for item in metadata['warnings']))
        data = load_report(None, pipeline={'cases': [{'case_id': 'Q01', 'cad_backend': 'solidworks',
                           'state': 'trusted_exported', 'trusted_export': metadata}]}).data
        visible = render_html(data).split('<script id="report-data"', 1)[0]
        for evidence in (opened, exported):
            self.assertIn(evidence['flags'][0]['reason'], visible)
        self.assertNotIn('code=', visible)
        self.assertNotIn('bit=', visible)
        self.assertNotIn('PRIVATE', json.dumps(data))

    def prepared_candidate(self):
        from fusion_pipeline.runner import Trial, atomic
        from fusion_pipeline.native_contract import make_native_descriptor, file_hash
        trial = Trial.__new__(Trial)
        trial.run = self.root / 'run'
        trial.run.mkdir()
        trial.private = self.root / 'private'
        trial.private.mkdir()
        trial.ledger = trial.run / 'ledger'
        workspace = trial.ledger / 'candidate/Q01'
        workspace.mkdir(parents=True)
        audit = trial.run / 'audit/Q01'
        audit.mkdir(parents=True)
        trial.cad_backend = 'solidworks'
        trial.unified_gateway = True
        trial.config = {'model': 'test/model', 'cad_skill': str(self.root / 'public-skill.md')}
        trial._processes = []
        row = dict(run_id='run', case_id='Q01', attempt_id='attempt-7', protocol_id='fusion-step-adapted-v4.0',
                   cad_backend='solidworks', cad_config_sha256='a' * 64, workspace=str(workspace),
                   input_manifest={'task': {}, 'attachments': []})
        trial.state = {'run_id': 'run', 'cases': [row], 'cad_capabilities': {}}
        trial.verify_source = lambda: None
        trial.verify_public_inputs = lambda row: None
        trial.probe = lambda label: {'documents': []}
        trial.check_cancel = lambda: None
        trial.save = lambda: None
        trial.cad_adapter = SimpleNamespace(submission_spec=lambda: {'filename': 'submission.SLDPRT', 'format': 'sldprt', 'document_type': 'Part'})
        native = workspace / 'submission.SLDPRT'
        native.write_bytes(bytes.fromhex('d0cf11e0a1b11ae1') + b'synthetic COM fixture')
        dependency = workspace / 'dependency_manifest.json'
        atomic(dependency, {'schema_version': 1, 'case_id': 'Q01', 'attempt_id': 'attempt-7',
            'inspection_method': 'Source contract declaration only; external references unverified until independent trusted reopen',
            'reviewer': 'cad-gateway/source-contract', 'dependencies': []})
        atomic(workspace / 'worker_report.json', {'model': 'candidate claim'})
        identity = trial.case_identity(row)
        atomic(audit / 'worker-contract.json', dict(identity, schema='cad-worker/1'))
        atomic(audit / 'cad/template-selection-fixture.json', {'tool': 'create_new_part', 'worker_pid': 123,
            'template': {'path': str(self.root / 'installation-template.prtdot'), 'sha256': 'e' * 64,
                         'bytes': 100, 'source': 'vendor_template_discovery', 'instance_version': '34.4.1'}})
        descriptor = make_native_descriptor(native, identity=identity, configuration='Default', dependency_sha256=file_hash(dependency))
        receipt = dict(identity, schema='cad-submission/1', status='submitted', native_document=descriptor,
                       report_sha256=file_hash(workspace / 'worker_report.json'))
        atomic(audit / 'submission-receipt.json', receipt)
        commands = []
        trial.scheduler = lambda *args: commands.append(args)
        trial.launch = lambda *args, **kwargs: {'returncode': 0, 'timed_out': False, 'guard_classification': 'clear',
            'process_cleanup': {'verified_stopped': True, 'remaining_processes': 0}}
        return trial, row, receipt, commands

    def test_freeze_preserves_bound_dependency_bytes_and_actual_identity(self):
        trial, row, receipt, commands = self.prepared_candidate()
        workspace = Path(row['workspace'])
        before = (workspace / 'dependency_manifest.json').read_bytes()
        with patch('fusion_pipeline.input_assets.iter_assets', return_value=[]):
            trial.model(row)
        self.assertEqual(row['state'], 'candidate_frozen')
        self.assertEqual((workspace / 'dependency_manifest.json').read_bytes(), before)
        self.assertEqual(json.loads((workspace / 'native_document.json').read_text()), receipt['native_document'])
        self.assertIn(('freeze-candidate', trial.ledger, 'Q01'), commands)

    def test_backend_delivery_override_preserves_historical_task_bytes(self):
        from fusion_pipeline.native_contract import file_hash
        trial, row, _, _ = self.prepared_candidate()
        task = Path(row['workspace']) / 'TASK.md'
        original = (b'\xef\xbb\xbf# Drawing task\r\nDiameter 6 mm.\r\n'
                    b'Build a native Autodesk Fusion model and submit the required F3D.\r\n')
        task.write_bytes(original)
        row['input_manifest']['task'] = {'id': 'task', 'path': 'TASK.md', 'mime': 'text/markdown',
                                         'sha256': file_hash(task)}
        launch = trial.launch
        prompts = []
        trial.launch = lambda workspace, audit, prompt: (prompts.append(prompt) or launch(workspace, audit, prompt))
        trial.model(row)
        self.assertEqual(task.read_bytes(), original)
        self.assertEqual(file_hash(task), row['input_manifest']['task']['sha256'])
        self.assertIn('本次运行合同只替换历史TASK的软件/工件交付指令', prompts[0])
        self.assertIn('cad_backend=solidworks', prompts[0])
        self.assertIn('native_filename=submission.SLDPRT', prompts[0])
        self.assertIn('几何、尺寸、单位和附件内容', prompts[0])
        self.assertIn('不修改原始输入字节', prompts[0])

    def test_cross_attempt_receipt_cannot_be_frozen(self):
        from fusion_pipeline.runner import atomic
        trial, row, receipt, commands = self.prepared_candidate()
        receipt['attempt_id'] = 'other-attempt'
        atomic(trial.run / 'audit/Q01/submission-receipt.json', receipt)
        with patch('fusion_pipeline.input_assets.iter_assets', return_value=[]), self.assertRaisesRegex(ValueError, 'identity|receipt'):
            trial.model(row)
        self.assertFalse(any(command[0] == 'freeze-candidate' for command in commands))

    def test_candidate_process_still_owned_prevents_freeze(self):
        trial, row, _, commands = self.prepared_candidate()
        trial._processes = [object()]
        with patch('fusion_pipeline.input_assets.iter_assets', return_value=[]), self.assertRaisesRegex(RuntimeError, 'process'):
            trial.model(row)
        self.assertFalse(any(command[0] == 'freeze-candidate' for command in commands))

    def test_export_hash_mismatch_cannot_publish_scoring_candidate(self):
        from fusion_pipeline.runner import atomic
        from fusion_pipeline.native_contract import file_hash
        trial, row, receipt, _ = self.prepared_candidate()
        row.update(state='candidate_frozen', native_document=receipt['native_document'])
        def export(native, folder, identity):
            folder.mkdir(parents=True)
            step, report = folder / 'trusted.step', folder / 'reopen.json'
            step.write_text('ISO-10303-21; synthetic adapter verification boundary', encoding='ascii')
            atomic(report, {'status': 'passed'})
            return {'status': 'passed', 'native_document': native, 'trusted_step': str(step),
                    'trusted_step_sha256': '0' * 64, 'trusted_report': str(report),
                    'trusted_report_sha256': file_hash(report), 'report': {'status': 'passed'}}
        adapter = SimpleNamespace(trusted_reopen_export=export, close=lambda: None)
        with patch('fusion_pipeline.cad.create_adapter', return_value=adapter), self.assertRaisesRegex(ValueError, 'hash|evidence'):
            trial.export(row)
        self.assertNotEqual(row['state'], 'trusted_exported')

    def test_new_fusion_case_report_retains_probed_version_and_adapter_source(self):
        import zipfile
        from fusion_pipeline.runner import atomic
        from fusion_pipeline.native_contract import make_native_descriptor, file_hash
        from src.report_data import load_report
        trial, row, _, _ = self.prepared_candidate()
        trial.cad_backend = 'fusion'
        trial.state.update(schema_version='cad-trial/1', cad_backend='fusion',
                           cad_identity={'backend': 'fusion', 'version': '2600.probed'})
        row['cad_backend'] = 'fusion'
        workspace = Path(row['workspace'])
        with zipfile.ZipFile(workspace / 'submission.f3d', 'w') as archive:
            archive.writestr('fixture', 'synthetic native bytes')
        native = make_native_descriptor(workspace / 'submission.f3d', identity=trial.case_identity(row),
                                        dependency_sha256=file_hash(workspace / 'dependency_manifest.json'))
        row.update(state='candidate_frozen', native_document=native)
        def export(native, folder, identity):
            folder.mkdir(parents=True)
            step, report = folder / 'trusted.step', folder / 'reopen.json'
            step.write_text('ISO-10303-21; synthetic orchestration fixture', encoding='ascii')
            data = dict(identity, schema='cad-trusted-export/1', status='passed', ok=True, native_document=native,
                        native_reopen={'dependencies': {'closed': True}})
            atomic(report, data)
            return {'status': 'passed', 'native_document': native, 'trusted_step': str(step),
                    'trusted_step_sha256': file_hash(step), 'trusted_report': str(report),
                    'trusted_report_sha256': file_hash(report), 'report': data}
        adapter = SimpleNamespace(trusted_reopen_export=export, close=lambda: None,
                                  identity={'backend': 'fusion', 'adapter_version': '1.0', 'adapter_sha256': 'd' * 64})
        with patch('fusion_pipeline.cad.create_adapter', return_value=adapter):
            trial.export(row)
        trial.verify_cad_export(row)
        projected = load_report(None, pipeline=trial.state).data['cases'][0]
        self.assertEqual(projected['cad_identity']['version'], '2600.probed')
        self.assertEqual(projected['cad_identity']['adapter_version'], '1.0')
        self.assertEqual(projected['cad_identity']['adapter_sha256'], 'd' * 64)
        self.assertEqual(projected['trusted_export']['dependency_status'], 'self_contained_verified')

    def test_export_receipt_binds_sources_and_detects_post_export_tampering(self):
        from fusion_pipeline.runner import atomic
        from fusion_pipeline.native_contract import file_hash
        trial, row, receipt, _ = self.prepared_candidate()
        row.update(state='candidate_frozen', native_document=receipt['native_document'])
        def export(native, folder, identity):
            folder.mkdir(parents=True)
            step, report = folder / 'trusted.step', folder / 'reopen.json'
            step.write_text('ISO-10303-21; synthetic adapter verification boundary', encoding='ascii')
            data = dict(identity, status='passed', ok=True, native_document=native)
            atomic(report, data)
            return {'status': 'passed', 'native_document': native, 'trusted_step': str(step),
                    'trusted_step_sha256': file_hash(step), 'trusted_report': str(report),
                    'trusted_report_sha256': file_hash(report), 'report': data}
        adapter = SimpleNamespace(trusted_reopen_export=export, close=lambda: None)
        with patch('fusion_pipeline.cad.create_adapter', return_value=adapter):
            trial.export(row)
        self.assertEqual(row['state'], 'trusted_exported')
        trial.verify_cad_export(row)
        Path(row['trusted_step']).write_text('changed after verified export')
        with self.assertRaisesRegex(ValueError, 'hash|changed|identity'):
            trial.verify_cad_export(row)

    def test_template_evidence_is_bound_across_candidate_and_maintenance_adapters(self):
        from fusion_pipeline.runner import atomic
        from fusion_pipeline.native_contract import file_hash
        trial, row, receipt, _ = self.prepared_candidate()
        row.update(state='candidate_frozen', native_document=receipt['native_document'])
        candidate_audit = trial.run / 'audit/Q01/cad'
        candidate_record = candidate_audit / 'template-selection-fixture.json'
        expected_digest = file_hash(candidate_record)
        maintenance_audit = trial.run / 'audit/Q01/trusted-cad'
        atomic(maintenance_audit / 'template-selection-unrelated.json', {'not': 'candidate evidence'})
        def export(native, folder, identity):
            folder.mkdir(parents=True)
            step, report = folder / 'trusted.step', folder / 'reopen.json'
            step.write_text('ISO-10303-21; synthetic adapter boundary', encoding='ascii')
            data = dict(identity, status='passed', ok=True, native_document=native, template_selection_evidence=[])
            atomic(report, data)
            return {'status': 'passed', 'native_document': native, 'trusted_step': str(step),
                    'trusted_step_sha256': file_hash(step), 'trusted_report': str(report),
                    'trusted_report_sha256': file_hash(report), 'report': data}
        def maintenance_adapter(config, *, audit_dir, **kwargs):
            self.assertEqual(audit_dir, maintenance_audit)
            return SimpleNamespace(trusted_reopen_export=export, close=lambda: None)
        with patch('fusion_pipeline.cad.create_adapter', side_effect=maintenance_adapter):
            trial.export(row)
        outer = trial.verify_cad_export(row)
        path = trial.run / 'trusted_export/Q01/template-selection-receipt.json'
        bound = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual({key: bound[key] for key in trial.case_identity(row)}, trial.case_identity(row))
        self.assertEqual(bound['native_document'], receipt['native_document'])
        self.assertEqual(bound['worker_contract_sha256'], file_hash(trial.run / 'audit/Q01/worker-contract.json'))
        self.assertEqual([item['filename'] for item in bound['evidence']], ['template-selection-fixture.json'])
        self.assertEqual(bound['evidence'][0]['sha256'], expected_digest)
        self.assertEqual(bound['evidence'][0]['record']['template']['sha256'], 'e' * 64)
        self.assertEqual(outer['template_selection_receipt_sha256'], file_hash(path))
        self.assertEqual(row['template_selection_receipt_sha256'], file_hash(path))
        from src.report_data import load_report
        projected = load_report(None, pipeline=trial.state).data['cases'][0]
        self.assertEqual(projected['trusted_export']['template_evidence_count'], 1)
        self.assertEqual(projected['template_selection_receipt_sha256'], file_hash(path))
        self.assertNotIn('installation-template', json.dumps(projected))
        original = path.read_bytes()
        path.write_bytes(original + b' ')
        with self.assertRaisesRegex(ValueError, 'template'):
            trial.verify_cad_export(row)
        path.write_bytes(original)
        atomic(candidate_record, {'changed': True})
        with self.assertRaisesRegex(ValueError, 'template|evidence'):
            trial.verify_cad_export(row)

    def test_template_evidence_from_another_attempt_is_rejected_before_export(self):
        from fusion_pipeline.runner import atomic
        trial, row, receipt, _ = self.prepared_candidate()
        row.update(state='candidate_frozen', native_document=receipt['native_document'])
        contract = dict(trial.case_identity(row), schema='cad-worker/1', attempt_id='different-attempt')
        atomic(trial.run / 'audit/Q01/worker-contract.json', contract)
        with (patch('fusion_pipeline.cad.create_adapter', side_effect=AssertionError('must reject before CAD connection')),
              self.assertRaisesRegex(ValueError, 'template|identity|attempt')):
            trial.export(row)

    def test_template_evidence_rejects_hardlinks_and_missing_records(self):
        import os
        trial, row, receipt, _ = self.prepared_candidate()
        row.update(state='candidate_frozen', native_document=receipt['native_document'])
        record = trial.run / 'audit/Q01/cad/template-selection-fixture.json'
        os.link(record, self.root / 'aliased-template-evidence.json')
        with self.assertRaisesRegex(ValueError, 'aliases|evidence'):
            trial.candidate_template_evidence(row)
        record.unlink()
        with self.assertRaisesRegex(ValueError, 'missing'):
            trial.candidate_template_evidence(row)

    def test_mcp_timeout_budget_is_passed_to_each_provider(self):
        from fusion_pipeline.adapters import build_invocation
        for provider in ('codex', 'opencode', 'claude'):
            folder = self.root / provider
            folder.mkdir()
            result = build_invocation({'provider': provider, 'model': 'test/model', 'executable': 'never-run',
                'workspace': str(folder), 'audit_dir': str(folder), 'prompt': 'synthetic',
                'worker_command': ['python', 'worker'], 'env': {}, 'credential_settings': str(folder / 'absent'),
                'tool_timeout_s': 660})
            if provider == 'codex':
                self.assertIn('mcp_servers.benchmark.tool_timeout_sec=660', result['args'])
            elif provider == 'opencode':
                self.assertEqual(json.loads(result['env']['OPENCODE_CONFIG_CONTENT'])['mcp']['benchmark']['timeout'], 660000)
            else:
                self.assertEqual(result['env']['MCP_TOOL_TIMEOUT'], '660000')

    def test_gateway_contract_reuses_ledger_identity_and_excludes_private_configuration(self):
        from fusion_pipeline.runner import atomic
        from src.io_utils import sha
        trial = self.trial()
        row = trial.state['cases'][0]
        workspace = trial.run / 'case/Q01'
        workspace.mkdir(parents=True)
        manifest = {'task': {'id': 'task', 'path': 'TASK.md', 'mime': 'text/markdown', 'sha256': 'b' * 64}, 'attachments': []}
        row.update(workspace=str(workspace), input_manifest=manifest)
        trial.verify_source = lambda: None
        trial.verify_public_inputs = lambda row: None
        def launch(spec, processes):
            audit = Path(spec['audit_dir'])
            contract = json.loads((audit / 'worker-contract.json').read_text())
            connection = json.loads(Path(contract['connection_file']).read_text())
            self.assertEqual({key: contract[key] for key in trial.case_identity(row)}, trial.case_identity(row))
            self.assertEqual(contract['schema'], 'cad-worker/1')
            self.assertNotIn('private_cases', connection)
            self.assertNotIn('private_runs_root', connection)
            self.assertEqual(contract['modeling_authorization']['task_sha256'], 'b' * 64)
            atomic(audit / 'gateway-state.json', {'contract_sha256': sha(audit / 'worker-contract.json'),
                'calls': 3, 'cad_calls': 1, 'pending_cad': 1, 'submitted': False})
            return {'returncode': 0, 'session_verification': {'verified': True},
                    'process_cleanup': {'verified_stopped': True, 'remaining_processes': 0}}
        with patch('fusion_pipeline.adapters.executable', return_value='never-run'), patch('fusion_pipeline.adapters.launch_fresh', side_effect=launch):
            result = trial.launch_gateway(workspace, trial.run / 'audit/Q01', 'synthetic')
        self.assertEqual(result['guard_classification'], 'cad_task_unknown')

    def test_preparation_registers_actual_protocol_and_attempt_in_schema_three_ledger(self):
        from tests.test_runner_input_assets import RunnerInputTests
        from scheduler.core import Scheduler
        fixture = RunnerInputTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        trial = fixture.trial
        trial.cad_backend = 'solidworks'
        trial.cad_config_sha256 = 'a' * 64
        trial.ledger = Scheduler(fixture.root / 'cad-ledger').create_run('cad-test', cad_backend='solidworks', cad_config_sha256='a' * 64)
        row = {'run_id': 'cad-test', 'case_id': 'Q01', 'attempt_id': 'attempt-7', 'protocol_id': 'fusion-step-adapted-v4.0',
               'cad_backend': 'solidworks', 'cad_config_sha256': 'a' * 64}
        trial.prepare_case_inputs(row)
        checkpoint = json.loads((trial.ledger / 'checkpoint.json').read_text())
        self.assertEqual(checkpoint['schema_version'], 3)
        contract = checkpoint['cases']['Q01']['contract']
        self.assertEqual(contract['attempt_id'], 'attempt-7')
        self.assertEqual(contract['protocol_id'], 'fusion-step-adapted-v4.0')
        self.assertEqual(contract['cad_backend'], 'solidworks')

    def test_changed_desktop_lease_is_retained_and_recorded_as_blocked(self):
        from tests.test_fusion_runner_safety import RunnerSafetyTests
        from fusion_pipeline import runner
        fixture = RunnerSafetyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        trial = fixture.trial
        trial.unified_gateway = True
        desktop = fixture.root / 'desktop'
        desktop.mkdir()
        lease = desktop / 'fusion-benchmark-desktop.lock'
        trial.prepare = lambda: lease.write_text(json.dumps({'owner': 'different-owner'}))
        trial.smoke = lambda: None
        with patch.object(runner.tempfile, 'gettempdir', return_value=str(desktop)):
            self.assertEqual(trial.run_all(preflight_only=True), 2)
        self.assertEqual(trial.state['state'], 'blocked')
        self.assertEqual(json.loads(lease.read_text())['owner'], 'different-owner')


if __name__ == '__main__':
    unittest.main()

"""Completed input rejection, pre-dispatch probes and safe no-submission exits.

All CAD/CLI boundaries are synthetic; no desktop or model is started.
"""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_cad_gateway as gateway_tests
from tests import test_cad_orchestration as orchestration_tests
from fusion_pipeline.solidworks import SolidWorksAdapter, file_sha256
from fusion_pipeline.stdio_mcp import CadCallUnknown


class ThreadProfileCompletionTests(unittest.TestCase):
    SOURCE_HASHES = {
        '_vendor/solidworks_mcp/automation/features.py': '16fef5556cbcbdb6832c5e44aa5f0faabb155770f648cf5c38d04b65ddab6d78',
        '_vendor/solidworks_mcp/automation/thread_feature.py': '9635f52c1712b9d590befad593406f251e94883cab07b0b4d9ce342a92ca3b40',
        'tools.py': '6f34cfe3763cdca745a41460fa4846a07cace89815e141adf1a2e1a05434c68a'}

    def setUp(self):
        fixture = Path(__file__).with_name('fixtures')/'solidworks_extrude_missing_sketch.json'
        self.payload = json.loads(json.loads(fixture.read_text('utf-8'))['result']['content'][0]['text'])
        message = 'Profile must be an existing local absolute .sldlfp file'
        self.payload.update(tool='create_thread', message=message, error=message,
            text='[ERROR] '+message, error_code='invalid_input', vendor_error_code=107,
            vendor_error_name='swInvalidInput', arguments={'profile_file': 'Metric Tap.SLDLFP',
                'size': 'M18x1.5', 'diameter': 18, 'pitch': 1.5, 'depth': 22,
                'reverse_direction': True, 'right_handed': True, 'unit': 'mm'})
        self.arguments = {**self.payload['arguments'], 'parameter_plan_token': 'synthetic-plan'}

    @contextmanager
    def adapter(self, payload=None, changed_source=False):
        with tempfile.TemporaryDirectory() as folder:
            adapter = SolidWorksAdapter.__new__(SolidWorksAdapter)
            adapter.audit_dir, adapter.deadline = Path(folder), None
            adapter.config = {'executor_timeout_s': 180, 'bundle_path': str(Path(folder)/'vendor')}
            adapter.instance = deepcopy(self.payload['benchmark_instance'])
            adapter.tools = {'create_thread': {'inputSchema': {'required': ['parameter_plan_token']}}}
            reply = gateway_tests.result(payload or self.payload)
            calls = []
            def call(name, arguments, **kwargs):
                calls.append((name, deepcopy(arguments)))
                return reply
            adapter.client = SimpleNamespace(call_tool=call, unknown=False)
            source = Path(adapter.config['bundle_path'])/'runtime/solidworks-plugin/src/sim_plugin_solidworks'
            def digest(path):
                path = Path(path)
                if path.is_relative_to(source):
                    return '0'*64 if changed_source else self.SOURCE_HASHES[path.relative_to(source).as_posix()]
                return file_sha256(path)
            with patch('fusion_pipeline.solidworks.file_sha256', side_effect=digest):
                yield adapter, calls

    def test_reviewed_thread_profile_rejection_finishes_without_retry_or_unknown(self):
        with self.adapter() as (adapter, calls):
            try:
                _, payload = adapter._call('create_thread', self.arguments)
            except CadCallUnknown:
                self.fail('Reviewed no-write profile validation was escalated to unknown')
            self.assertFalse(payload['success'])
            self.assertFalse(adapter.client.unknown)
            self.assertEqual(calls, [('create_thread', self.arguments)])
            record = json.loads(next(adapter.audit_dir.glob('cad-call-*.json')).read_text('utf-8'))
            self.assertEqual(record['outcome'], 'completed_failure')

    def test_untrusted_or_partially_started_input_failures_still_block(self):
        variants = [
            (('data', 'mutation_started'), True),
            (('data', 'completion_state'), 'unknown'),
            (('postconditions',), {'operation_started': True}),
            (('message',), 'Other invalid input'),
            (('arguments', 'profile_file'), 'different.SLDLFP'),
            (('process_isolation', 'returncode'), 1),
            (('process_isolation', 'same_worker_instance_guard'), False),
            (('executor', 'timeout_exceeded'), True),
            (('benchmark_instance', 'pid'), 9001)]
        for path, value in variants:
            payload = deepcopy(self.payload)
            cursor = payload
            for key in path[:-1]: cursor = cursor[key]
            cursor[path[-1]] = value
            with self.subTest(path=path), self.adapter(payload) as (adapter, calls):
                with self.assertRaises(CadCallUnknown):
                    adapter._call('create_thread', self.arguments)
                self.assertTrue(adapter.client.unknown)
                self.assertEqual(len(calls), 1)
        with self.adapter(changed_source=True) as (adapter, _):
            with self.assertRaises(CadCallUnknown):
                adapter._call('create_thread', self.arguments)

    def test_gateway_returns_input_error_without_pending_or_consuming_plan_position(self):
        fixture = gateway_tests.CadGatewayTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        arguments = self.payload['arguments']
        types = {str: 'string', bool: 'boolean', int: 'number', float: 'number'}
        properties = {key: {'type': types[type(value)]} for key, value in arguments.items()}
        properties['parameter_plan_token'] = {'type': 'string'}
        fixture.native.tools['create_thread'] = gateway_tests.descriptor('create_thread', properties, properties)
        fixture.create()
        fixture.authorize([('create_thread', arguments)])
        with self.adapter() as (adapter, calls), patch.object(fixture.native, 'call_tool', side_effect=adapter.call_tool):
            reply = fixture.call('create_thread', {**arguments, 'parameter_plan_token': 'issued-native-token'})
        from fusion_pipeline.solidworks import parse_tool_result
        self.assertEqual(parse_tool_result(reply)['error_code'], 'invalid_input')
        state = json.loads((fixture.audit/'gateway-state.json').read_text('utf-8'))
        self.assertEqual(state['pending_cad'], 0)
        self.assertEqual(state['parameter_plan']['next_index'], 0)
        self.assertEqual(len(calls), 1)


class BeforeDispatchProbeTests(unittest.TestCase):
    setUp = gateway_tests.CadGatewayTests.setUp
    gateway = gateway_tests.CadGatewayTests.gateway
    call = gateway_tests.CadGatewayTests.call

    def state(self):
        return json.loads((self.audit/'gateway-state.json').read_text('utf-8'))

    def events(self):
        return [json.loads(line) for line in (self.audit/'gateway-events.jsonl').read_text('utf-8').splitlines()]

    def test_pre_call_failure_has_no_pending_native_call_and_requires_fresh_safe_probe(self):
        with patch.object(self.native, 'probe_workspace', side_effect=ValueError('workspace busy')):
            with self.assertRaisesRegex(RuntimeError, 'cad_workspace_unconfirmed'):
                self.call('list_open_documents')
        self.assertEqual(self.state()['pending_cad'], 0)
        self.assertTrue(self.state()['cad_workspace_unconfirmed'])
        self.assertFalse(any(row['type'] == 'cad_started' for row in self.events()))
        self.assertEqual(self.native.calls, [])
        self.call('list_open_documents')
        self.assertFalse(self.state()['cad_workspace_unconfirmed'])
        self.assertEqual(len(self.native.calls), 1)

    def test_repeated_unsafe_probe_never_dispatches(self):
        with patch.object(self.native, 'probe_workspace', side_effect=ValueError('still busy')):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, 'cad_workspace_unconfirmed'):
                    self.call('list_open_documents')
        self.assertEqual(self.state()['pending_cad'], 0)
        self.assertEqual(len(self.native.calls), 0)
        self.assertTrue(self.state()['cad_workspace_unconfirmed'])

    def test_dispatched_failure_remains_pending_and_cannot_reprobe_or_replay(self):
        before = self.native.probe_workspace()
        with patch.object(self.native, 'probe_workspace', side_effect=[before, ValueError('busy after call')]):
            with self.assertRaisesRegex(RuntimeError, 'cad_task_unknown'):
                self.call('list_open_documents')
        self.assertEqual(self.state()['pending_cad'], 1)
        with patch.object(self.native, 'probe_workspace', side_effect=AssertionError('must not probe pending mutation')):
            with self.assertRaisesRegex(RuntimeError, 'cad_task_unknown'):
                self.call('list_open_documents')
        self.assertEqual(len(self.native.calls), 1)

    def test_unknown_probe_transport_cannot_be_repeated_by_restarting_gateway(self):
        with patch.object(self.native, 'probe_workspace', side_effect=CadCallUnknown('probe worker unconfirmed')):
            with self.assertRaisesRegex(RuntimeError, 'cad_workspace_unconfirmed'):
                self.call('list_open_documents')
        self.assertEqual(self.state()['pending_cad'], 0)
        with patch.object(self.native, 'probe_workspace', side_effect=AssertionError('must not repeat unknown probe')):
            with self.assertRaisesRegex(RuntimeError, 'cad_workspace_unconfirmed'):
                self.call('list_open_documents')
        self.assertEqual(self.state()['maintenance_probe_calls'], 1)
        self.assertEqual(self.native.calls, [])

    def test_concurrent_gateway_request_cannot_bypass_probe_lock(self):
        gateway = self.gateway()
        gateway.lock_path.touch()
        with self.assertRaisesRegex(RuntimeError, 'gateway_busy_or_unreconciled'):
            self.call('list_open_documents')
        self.assertEqual(self.native.calls, [])


class NoCadSubmissionTests(unittest.TestCase):
    setUp = orchestration_tests.CadOrchestrationTests.setUp
    prepared_candidate = orchestration_tests.CadOrchestrationTests.prepared_candidate

    def prepared_missing(self):
        trial, row, _, commands = self.prepared_candidate()
        result = {'returncode': 0, 'timed_out': False, 'cancelled': False,
            'guard_classification': 'submission_unconfirmed',
            'process_cleanup': {'verified_stopped': True, 'remaining_processes': 0},
            'guard': {'cad_calls': 0, 'pending_cad': 0, 'submitted': False, 'owned_documents': []}}
        trial.launch = lambda *args, **kwargs: result
        return trial, row, result, commands

    def test_safe_zero_cad_exit_records_missing_candidate_and_returns_without_freezing(self):
        trial, row, _, commands = self.prepared_missing()
        probes = []
        trial.probe = lambda label: probes.append(label) or {'documents': []}
        with patch('fusion_pipeline.input_assets.iter_assets', return_value=[]):
            try:
                trial.model(row)
            except RuntimeError:
                self.fail('Safe zero-CAD no-submission exit incorrectly blocked the batch')
        self.assertEqual(row['state'], 'candidate_missing')
        self.assertEqual(row['failure_reason'], 'no_submission_without_cad_calls')
        self.assertNotIn('native_document', row)
        self.assertNotIn('score', row)
        self.assertIn('after_Q01', probes)
        self.assertFalse(any(command[0] == 'freeze-candidate' for command in commands))

    def test_unsafe_missing_submissions_still_require_reconciliation(self):
        for variant in ('called', 'pending', 'process', 'document', 'probe', 'timeout', 'workspace'):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as folder:
                self.root = Path(folder)
                trial, row, result, commands = self.prepared_missing()
                if variant == 'called': result['guard']['cad_calls'] = 1
                if variant == 'pending': result['guard']['pending_cad'] = 1
                if variant == 'process': result['process_cleanup']['verified_stopped'] = False
                if variant == 'document': result['guard']['owned_documents'] = [{'title': 'Part'}]
                if variant == 'timeout': result['timed_out'] = True
                if variant == 'workspace': result['guard']['cad_workspace_unconfirmed'] = True
                if variant == 'probe':
                    def probe(label):
                        if label.startswith('after_'): raise RuntimeError('desktop not idle')
                    trial.probe = probe
                with patch('fusion_pipeline.input_assets.iter_assets', return_value=[]):
                    with self.assertRaises(RuntimeError): trial.model(row)
                self.assertNotEqual(row['state'], 'candidate_missing')
                self.assertFalse(any(command[0] == 'freeze-candidate' for command in commands))


class GuardClassificationTests(unittest.TestCase):
    setUp = orchestration_tests.CadOrchestrationTests.setUp
    trial = orchestration_tests.CadOrchestrationTests.trial

    def test_unconfirmed_workspace_is_reported_separately_but_pending_takes_precedence(self):
        from fusion_pipeline.runner import atomic
        from src.io_utils import sha
        for pending, expected in ((0, 'cad_workspace_unconfirmed'), (1, 'cad_task_unknown')):
            with self.subTest(pending=pending):
                trial = self.trial()
                row = trial.state['cases'][0]
                workspace = trial.run/'case/Q01'
                workspace.mkdir(parents=True)
                row.update(workspace=str(workspace), input_manifest={'task': {
                    'id': 'task', 'path': 'TASK.md', 'mime': 'text/markdown', 'sha256': 'b'*64}, 'attachments': []})
                trial.verify_source = lambda: None
                trial.verify_public_inputs = lambda row: None
                def launch(spec, processes):
                    audit = Path(spec['audit_dir'])
                    atomic(audit/'gateway-state.json', {'contract_sha256': sha(audit/'worker-contract.json'),
                        'calls': 3, 'cad_calls': 0, 'pending_cad': pending, 'submitted': False,
                        'cad_workspace_unconfirmed': True})
                    return {'returncode': 0, 'session_verification': {'verified': True},
                        'process_cleanup': {'verified_stopped': True, 'remaining_processes': 0}}
                with patch('fusion_pipeline.adapters.executable', return_value='never-run'), patch(
                        'fusion_pipeline.adapters.launch_fresh', side_effect=launch):
                    result = trial.launch_gateway(workspace, trial.run/'audit/Q01', 'synthetic')
                self.assertEqual(result['guard_classification'], expected)

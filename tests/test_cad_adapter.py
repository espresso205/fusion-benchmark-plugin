import json
import tempfile
import unittest
import os
import zipfile
from copy import deepcopy
from contextlib import contextmanager
from unittest.mock import patch
from pathlib import Path

from fusion_pipeline.cad import normalize_cad_config, cad_capabilities
from fusion_pipeline.solidworks import parse_tool_result, classify_result


class ReadOnlyBudgetTests(unittest.TestCase):
    def test_info_and_workspace_use_bounded_client_budget_at_late_smoke_deadline(self):
        from types import SimpleNamespace
        from fusion_pipeline.solidworks import SolidWorksAdapter
        for tool in ('get_solidworks_info', 'benchmark_workspace'):
            with self.subTest(tool=tool), tempfile.TemporaryDirectory() as folder:
                adapter = SolidWorksAdapter.__new__(SolidWorksAdapter)
                adapter.config, adapter.deadline = {'executor_timeout_s':180}, 192
                adapter.audit_dir, adapter.tools = Path(folder), {}
                calls = []
                def call_tool(name, arguments, timeout):
                    calls.append((name, timeout))
                    return {'content':[{'type':'text','text':json.dumps({'success':True, 'ok':True,
                        'process_isolation':{'enabled':True,'same_worker_instance_guard':True},
                        'executor':{'timeout_enforced':True}})}]}
                adapter.client = SimpleNamespace(call_tool=call_tool, unknown=False)
                with patch('fusion_pipeline.solidworks.time.time', return_value=0):
                    try:
                        adapter._call(tool,{})
                    except TimeoutError:
                        self.fail('Read-only probe incorrectly required the modeling worker budget')
                self.assertEqual(calls, [(tool,45)])
                self.assertFalse(adapter.client.unknown)

    def test_budget_reserves_complete_postprobe_before_native_dispatch(self):
        from types import SimpleNamespace
        from fusion_pipeline.solidworks import SolidWorksAdapter
        adapter = SolidWorksAdapter.__new__(SolidWorksAdapter)
        adapter.config, adapter.deadline = {'executor_timeout_s':180}, 192
        adapter.client = SimpleNamespace(unknown=False)
        check = getattr(adapter, 'require_call_budget', None)
        self.assertTrue(callable(check), 'Missing compound native-call/post-probe budget guard')
        with patch('fusion_pipeline.solidworks.time.time', return_value=0):
            evidence = check('get_solidworks_info', {}, post_probe=True)
            self.assertEqual(evidence['native_budget_s'],45)
            self.assertEqual(evidence['post_probe_budget_s'],45)
            self.assertEqual(evidence['required_s'],95)
            self.assertEqual(evidence['remaining_s'],192)
            with self.assertRaises(TimeoutError):
                check('extrude_sketch',{},post_probe=True)
            adapter.deadline=245
            self.assertEqual(check('extrude_sketch',{},post_probe=True)['native_budget_s'],195)
            adapter.deadline=94
            with self.assertRaises(TimeoutError):
                check('get_solidworks_info',{},post_probe=True)
        self.assertFalse(adapter.client.unknown)


class MissingSketchPreconditionTests(unittest.TestCase):
    """Replay the PNG02 failure structure with synthetic host identities/paths."""
    SOURCE_HASHES = {
        '_vendor/solidworks_mcp/automation/features.py': '16fef5556cbcbdb6832c5e44aa5f0faabb155770f648cf5c38d04b65ddab6d78',
        'tools.py': '6f34cfe3763cdca745a41460fa4846a07cace89815e141adf1a2e1a05434c68a'}

    def setUp(self):
        fixture = Path(__file__).with_name('fixtures')/'solidworks_extrude_missing_sketch.json'
        self.raw = json.loads(fixture.read_text(encoding='utf-8'))['result']
        self.payload = parse_tool_result(self.raw)
        self.arguments = {**self.payload['arguments'], 'parameter_plan_token': 'test-plan-token'}

    @contextmanager
    def adapter(self, payload=None, *, changed_source=False):
        from types import SimpleNamespace
        from fusion_pipeline.solidworks import SolidWorksAdapter, file_sha256
        with tempfile.TemporaryDirectory() as folder:
            adapter = SolidWorksAdapter.__new__(SolidWorksAdapter)
            adapter.audit_dir, adapter.deadline = Path(folder), None
            adapter.config = {'executor_timeout_s': 180, 'bundle_path': str(Path(folder)/'vendor')}
            adapter.instance = deepcopy(self.payload['benchmark_instance'])
            adapter.tools = {'extrude_sketch': {'inputSchema': {'required': ['parameter_plan_token']}}}
            result = self.raw if payload is None else {'content': [{'type': 'text', 'text': json.dumps(payload)}], 'isError': False}
            calls = []
            def call_tool(name, arguments, **kwargs):
                calls.append((name, deepcopy(arguments)))
                return result
            adapter.client = SimpleNamespace(call_tool=call_tool, unknown=False)
            source = Path(adapter.config['bundle_path'])/'runtime/solidworks-plugin/src/sim_plugin_solidworks'
            def hash_file(path):
                path = Path(path)
                if path.is_relative_to(source):
                    return '0'*64 if changed_source else self.SOURCE_HASHES[path.relative_to(source).as_posix()]
                return file_sha256(path)
            # Only the external installed-vendor hash boundary is doubled.
            with patch('fusion_pipeline.solidworks.file_sha256', side_effect=hash_file):
                yield adapter, calls

    def test_actual_missing_named_sketch_reply_is_a_completed_failure_without_retry(self):
        from fusion_pipeline.stdio_mcp import CadCallUnknown
        with self.adapter() as (adapter, calls):
            try:
                raw, payload = adapter._call('extrude_sketch', self.arguments)
            except CadCallUnknown:
                self.fail('Verified preflight name lookup failure was escalated to unknown')
            self.assertEqual(raw, self.raw)
            self.assertFalse(payload['success'])
            self.assertFalse(adapter.client.unknown)
            self.assertEqual(calls, [('extrude_sketch', self.arguments)])
            evidence = json.loads(next(adapter.audit_dir.glob('cad-call-*.json')).read_text(encoding='utf-8'))
            self.assertEqual(evidence['outcome'], 'completed_failure')

    def test_partial_or_uncertain_variants_remain_unknown(self):
        from fusion_pipeline.stdio_mcp import CadCallUnknown
        variants = [('mutation_started', True), ('partial_mutation_possible', True),
                    ('partial_change_possible', True), ('completion_state', 'unknown'),
                    ('reconciliation_required', True), ('automatic_retry_allowed', True)]
        for key, value in variants:
            with self.subTest(key=key):
                payload = deepcopy(self.payload)
                payload['data'][key] = value
                with self.adapter(payload) as (adapter, calls):
                    with self.assertRaises(CadCallUnknown):
                        adapter._call('extrude_sketch', self.arguments)
                    self.assertTrue(adapter.client.unknown)
                    self.assertEqual(len(calls), 1)

    def test_other_selection_failure_or_missing_guard_does_not_gain_exception(self):
        from fusion_pipeline.stdio_mcp import CadCallUnknown
        def variant(path, value):
            data = deepcopy(self.payload)
            cursor = data
            for key in path[:-1]: cursor = cursor[key]
            cursor[path[-1]] = value
            return data
        variants = [
            variant(('mutation_started',), True),
            variant(('postconditions',), {'operation_started': True}),
            variant(('message',), 'Explicit sketch selection failed'),
            variant(('error_code',), 'feature_operation_failed'),
            variant(('vendor_error_code',), 103),
            variant(('tool',), 'cut_extrude'),
            variant(('arguments', 'sketch_name'), '草图1'),
            variant(('process_isolation', 'returncode'), 1),
            variant(('process_isolation', 'returncode'), False),
            variant(('process_isolation', 'same_worker_instance_guard'), False),
            variant(('executor', 'timeout_exceeded'), True),
            variant(('benchmark_instance', 'pid'), self.payload['benchmark_instance']['pid']+1)]
        for index, payload in enumerate(variants):
            with self.subTest(index=index), self.adapter(payload) as (adapter, calls):
                with self.assertRaises(CadCallUnknown):
                    adapter._call('extrude_sketch', self.arguments)
                self.assertTrue(adapter.client.unknown)
                self.assertEqual(len(calls), 1)

    def test_unreviewed_vendor_source_does_not_gain_exception(self):
        from fusion_pipeline.stdio_mcp import CadCallUnknown
        with self.adapter(changed_source=True) as (adapter, calls):
            with self.assertRaises(CadCallUnknown):
                adapter._call('extrude_sketch', self.arguments)
            self.assertTrue(adapter.client.unknown)

    def test_missing_bound_instance_does_not_gain_exception(self):
        from fusion_pipeline.stdio_mcp import CadCallUnknown
        with self.adapter() as (adapter, calls):
            adapter.instance = None
            with self.assertRaises(CadCallUnknown):
                adapter._call('extrude_sketch', self.arguments)
            self.assertTrue(adapter.client.unknown)


class CadConfigTests(unittest.TestCase):
    def test_normalized_configuration_can_be_normalized_again(self):
        config = {'cad_backend':'solidworks', 'solidworks':{'bundle':'C:/example/sw'}}
        frozen = normalize_cad_config(config)
        self.assertEqual(normalize_cad_config({**config, **frozen}), frozen)
    def test_generator_alias_normalizes_to_same_config(self):
        first = normalize_cad_config({'cad_backend':'solidworks', 'solidworks':{'bundle':'C:/example/sw'}})
        second = normalize_cad_config({'cad_backend':'solidworks', 'solidworks_connection':{'bundle_path':'C:/example/sw'}})
        self.assertEqual(first, second)
    def test_legacy_defaults_to_fusion(self):
        value = normalize_cad_config({'fusion_connection': {'url': 'http://localhost:1234/mcp'}})
        self.assertEqual(value['cad_backend'], 'fusion')
        self.assertEqual(len(value['cad_config_sha256']), 64)

    def test_unknown_backend_and_legacy_solidworks_rejected(self):
        for config in ({'cad_backend': 'invented'}, {'cad_backend': 'solidworks', 'execution_backend': 'legacy'}):
            with self.assertRaises(ValueError):
                normalize_cad_config(config)

    def test_conflicting_connection_rejected(self):
        with self.assertRaises(ValueError):
            normalize_cad_config({'cad_backend': 'solidworks', 'execution_backend': 'unified_gateway',
                                  'fusion_connection': {'url': 'http://localhost/mcp'}})

    def test_runtime_instance_is_not_configuration_digest(self):
        config = {'cad_backend': 'solidworks', 'execution_backend': 'unified_gateway',
                  'solidworks_connection': {'bundle_path': 'C:/example/solidworks'}}
        first = normalize_cad_config(config)
        second = normalize_cad_config(dict(config, expected_instance={'pid': 123}))
        self.assertEqual(first['cad_config_sha256'], second['cad_config_sha256'])

    def test_static_capabilities_never_claim_connection(self):
        value = cad_capabilities({'cad_backend': 'solidworks', 'execution_backend': 'unified_gateway',
                                  'solidworks_connection': {'bundle_path': 'C:/missing/solidworks'}})
        self.assertFalse(value['available'])
        self.assertFalse(value['cad_connection']['executed'])


class BusinessResultTests(unittest.TestCase):
    def test_worker_exception_without_no_mutation_evidence_is_unknown(self):
        self.assertEqual(classify_result({'ok':False,'success':False,'error_code':'com_worker_request_error','data':{}}), 'unknown')
    def test_business_failure_is_distinct_from_mcp_completion(self):
        raw = {'content': [{'type': 'text', 'text': json.dumps({'ok': False, 'success': False,
                 'status': 'error', 'data': {}, 'error_code': 'invalid_arguments'})}]}
        self.assertEqual(classify_result(parse_tool_result(raw)), 'completed_failure')

    def test_unknown_inner_state_overrides_success(self):
        self.assertEqual(classify_result({'success': True, 'ok': True,
                         'data': {'completion_state': 'unknown'}}), 'unknown')

    def test_conflicting_result_flags_are_unknown(self):
        self.assertEqual(classify_result({'ok': False, 'success': True, 'data': {}}), 'unknown')

    def test_unstructured_or_multiple_payloads_rejected(self):
        for content in ([{'type': 'text', 'text': 'ok'}],
                        [{'type': 'text', 'text': '{"success":true}'},
                         {'type': 'text', 'text': '{"success":false}'}]):
            with self.assertRaises(ValueError):
                parse_tool_result({'content': content})

    def test_malformed_raw_return_is_retained_before_parse_fails(self):
        from fusion_pipeline.solidworks import SolidWorksAdapter
        from fusion_pipeline.stdio_mcp import CadCallUnknown
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            adapter = SolidWorksAdapter.__new__(SolidWorksAdapter)
            adapter.audit_dir, adapter.deadline = Path(folder), None
            adapter.config = {'executor_timeout_s':180}
            adapter.tools = {'create_new_part':{'inputSchema':{'required':['parameter_plan_token']}}}
            adapter.client = SimpleNamespace(call_tool=lambda *a, **k:{'content':[{'type':'text','text':'broken raw return'}]}, unknown=False)
            with self.assertRaises(CadCallUnknown):
                adapter._call('create_new_part', {})
            saved = list(Path(folder).glob('cad-raw-*.json'))
            self.assertEqual(len(saved), 1)
            self.assertIn('broken raw return', saved[0].read_text(encoding='utf-8'))

    def test_cleanup_uncertainty_propagates_to_lease_owner(self):
        from fusion_pipeline.solidworks import SolidWorksAdapter
        from fusion_pipeline.stdio_mcp import CadCallUnknown
        from types import SimpleNamespace
        adapter = SolidWorksAdapter.__new__(SolidWorksAdapter)
        adapter.client = SimpleNamespace(close=lambda:None, unknown=True, cleanup_evidence=[{'terminated':True}])
        with self.assertRaises(CadCallUnknown):
            adapter.close()

    def test_failed_evidence_write_after_native_reply_is_unknown(self):
        from fusion_pipeline.solidworks import SolidWorksAdapter
        from fusion_pipeline.stdio_mcp import CadCallUnknown
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            adapter = SolidWorksAdapter.__new__(SolidWorksAdapter)
            adapter.audit_dir, adapter.deadline = Path(folder), None
            adapter.config = {'executor_timeout_s':180}
            adapter.tools = {'create_new_part':{'inputSchema':{'required':['parameter_plan_token']}}}
            raw = {'content':[{'type':'text','text':'{"success":true,"ok":true,"data":{}}'}]}
            adapter.client = SimpleNamespace(call_tool=lambda *a, **k:raw, unknown=False)
            with patch('fusion_pipeline.solidworks.write_json_new', side_effect=OSError('disk full')):
                with self.assertRaises(CadCallUnknown):
                    adapter._call('create_new_part', {})
            self.assertTrue(adapter.client.unknown)

    def test_malformed_content_block_has_unknown_completion(self):
        from fusion_pipeline.solidworks import classify_tool_result
        self.assertEqual(classify_tool_result({'content':[None]}), 'unknown')

    def test_malformed_business_data_has_unknown_completion(self):
        self.assertEqual(classify_result({'success':False,'ok':False,
            'error_code':'com_worker_request_error','data':None}), 'unknown')

    def test_malformed_error_or_completion_types_are_unknown(self):
        for payload in ({'ok':False,'success':False,'error_code':[],'data':{}},
                        {'ok':True,'success':True,'status':[],'data':{}},
                        {'ok':False,'success':False,'error_code':'com_worker_request_error','data':{'completion_state':[]}}):
            self.assertEqual(classify_result(payload),'unknown')

    def test_malformed_error_code_marks_actual_adapter_transport_unknown(self):
        from fusion_pipeline.solidworks import SolidWorksAdapter
        from fusion_pipeline.stdio_mcp import CadCallUnknown
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            adapter=SolidWorksAdapter.__new__(SolidWorksAdapter)
            adapter.audit_dir,adapter.deadline=Path(folder),None
            adapter.config={'executor_timeout_s':180}
            adapter.tools={}
            adapter.client=SimpleNamespace(unknown=False,call_tool=lambda *a,**k:{'content':[
                {'type':'text','text':'{"ok":false,"success":false,"error_code":[],"data":{}}'}]})
            with self.assertRaises(CadCallUnknown):
                adapter._call('create_new_part',{})
            self.assertTrue(adapter.client.unknown)


class FusionAdapterEvidenceTests(unittest.TestCase):
    def test_new_report_binds_identity_and_preserves_native_report(self):
        from fusion_pipeline.cad import FusionAdapter
        from fusion_pipeline.solidworks import file_sha256
        from fusion_pipeline.native_contract import make_native_descriptor
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            source = workspace/'submission.f3d'
            with zipfile.ZipFile(source, 'w') as archive:
                archive.writestr('fixture', 'native fixture')
            identity = {'run_id':'run1','case_id':'Q01','attempt_id':'attempt-1',
                'protocol_id':'fusion-step-adapted-v4.0','cad_backend':'fusion','cad_config_sha256':'a'*64}
            native = make_native_descriptor(source, identity=identity, dependency_sha256='b'*64)
            folder = workspace/'export'
            legacy = {}
            def execute(script, readonly):
                step = folder/'trusted_export.step'
                step.write_bytes(b'ISO-10303-21;\nlegacy-verified-fixture')
                legacy.update(benchmark_export=True, ok=True, status='passed', step_export={'sha256':file_sha256(step)})
                # Native Fusion script writes the path it was actually passed.
                import re
                encoded = re.search(r"REPORT = bytes.fromhex\('([^']+)'\)", script).group(1)
                Path(bytes.fromhex(encoded).decode()).write_text(json.dumps(legacy),encoding='utf-8')
                return {'content':[{'type':'text','text':json.dumps(legacy)}]}
            adapter=FusionAdapter.__new__(FusionAdapter)
            adapter.workspace=workspace
            adapter.identity={}
            adapter.client=SimpleNamespace(execute=execute)
            result=adapter.trusted_reopen_export(native,folder,identity)
            self.assertEqual(result['report']['run_id'],'run1')
            self.assertEqual(result['report']['native_document'],native)
            self.assertEqual(result['report']['native_reopen'],legacy)
            self.assertEqual(json.loads((folder/'fusion-native-report.json').read_text(encoding='utf-8')),legacy)


@unittest.skipUnless(os.environ.get('SOLIDWORKS_TEST_BUNDLE'), 'explicit local vendor discovery fixture not configured')
class VendorDiscoveryTests(unittest.TestCase):
    def test_private_helper_controls_are_registered_but_hidden_from_candidate(self):
        from fusion_pipeline.cad import create_adapter
        from fusion_pipeline.solidworks_helper import PRIVATE_TOOLS
        with tempfile.TemporaryDirectory() as folder:
            adapter = create_adapter({'cad_backend':'solidworks', 'solidworks':{'bundle':os.environ['SOLIDWORKS_TEST_BUNDLE']}},
                                     workspace=folder, audit_dir=Path(folder)/'logs')
            try:
                self.assertTrue(PRIVATE_TOOLS.issubset(adapter.client.tools))
                self.assertFalse(PRIVATE_TOOLS.intersection(adapter.tools))
            finally:
                adapter.close()

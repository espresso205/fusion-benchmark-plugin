"""A completed health review can reject a geometry contract without being pending."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fusion_pipeline.solidworks import SolidWorksAdapter, file_sha256
from fusion_pipeline.stdio_mcp import CadCallUnknown


class HealthReviewCompletionTests(unittest.TestCase):
    def setUp(self):
        self.arguments = {'expected_geometry': {'height_mm': 10}, 'require_material': False}
        checks = {name: {'ok': True, 'success': True, 'status': 'success', 'data': {}, 'error_code': None}
                  for name in ('document', 'rebuild', 'features', 'feature_tree', 'sketch', 'mass',
                               'bounding_box', 'properties', 'configurations')}
        checks['document']['data'] = {'type': 'Part', 'type_code': 1}
        checks['rebuild'].update(tool='rebuild_active_document', data={'raw_result': True,
            'type': 'Part', 'scope': 'active_configuration', 'top_only': False, 'geometry_verified': False})
        contracts = [{'name': 'bounding_box_height', 'expected': 10, 'actual': 5,
                      'unit': 'mm', 'tolerance': .1, 'success': False, 'absolute_error': 5}]
        message = 'Model health checks ran, but 1 geometry/material contract(s) failed'
        self.payload = {'ok': False, 'success': False, 'status': 'error', 'tool': 'review_model_health',
            'message': message, 'error': message, 'error_code': 'geometry_contract_failed',
            'arguments': deepcopy(self.arguments), 'data': {'checks': checks, 'check_count': 9,
                'successful_check_count': 9, 'failed_check_count': 0, 'successful_checks': list(checks),
                'failed_checks': [], 'geometry_contracts': contracts, 'geometry_contract_count': 1,
                'geometry_contracts_passed': 0, 'failed_geometry_contracts': deepcopy(contracts)},
            'benchmark_instance': {'pid': 123, 'session_id': 1, 'version': '34.4.1',
                                   'executable': 'C:/synthetic/SOLIDWORKS'},
            'executor': {'timeout_enforced': True, 'timeout_policy': 'hard_process_termination',
                'timeout_exceeded': False, 'soft_timeout_exceeded': False},
            'process_isolation': {'enabled': True, 'same_worker_instance_guard': True,
                'helper_version': '1', 'worker_pid': 456, 'returncode': 0}}

    @contextmanager
    def adapter(self, payload=None, *, source_matches=True):
        with tempfile.TemporaryDirectory() as directory:
            adapter = SolidWorksAdapter.__new__(SolidWorksAdapter)
            adapter.audit_dir, adapter.deadline = Path(directory), None
            adapter.config = {'executor_timeout_s': 180, 'bundle_path': str(Path(directory)/'vendor')}
            adapter.tools = {'review_model_health': {'inputSchema': {'required': []}}}
            adapter.instance = deepcopy(self.payload['benchmark_instance'])
            raw = {'content': [{'type': 'text', 'text': json.dumps(payload or self.payload)}], 'isError': False}
            calls = []
            def call_tool(name, arguments, **kwargs):
                calls.append((name, deepcopy(arguments)))
                return raw
            adapter.client = SimpleNamespace(call_tool=call_tool, unknown=False)
            vendor = Path(adapter.config['bundle_path'])/'runtime/solidworks-plugin/src/sim_plugin_solidworks/tools.py'
            def digest(path):
                if Path(path) == vendor:
                    return '6f34cfe3763cdca745a41460fa4846a07cace89815e141adf1a2e1a05434c68a' if source_matches else '0'*64
                return file_sha256(path)
            with patch('fusion_pipeline.solidworks.file_sha256', side_effect=digest):
                yield adapter, calls, raw

    def test_completed_geometry_mismatch_returns_original_failure_without_retry(self):
        with self.adapter() as (adapter, calls, raw):
            try:
                returned, payload = adapter._call('review_model_health', self.arguments)
            except CadCallUnknown:
                self.fail('All health checks completed; geometry mismatch must remain a known failure')
            self.assertEqual(returned, raw)
            self.assertFalse(payload['success'])
            self.assertFalse(adapter.client.unknown)
            self.assertEqual(calls, [('review_model_health', self.arguments)])
            evidence = json.loads(next(adapter.audit_dir.glob('cad-call-*.json')).read_text(encoding='utf-8'))
            self.assertEqual(evidence['outcome'], 'completed_failure')

    def test_unknown_nested_state_has_priority_over_completed_review(self):
        for key, value in [('completion_state', 'unknown'), ('partial_mutation_possible', True),
                           ('partial_change_possible', True), ('reconciliation_required', True)]:
            payload = deepcopy(self.payload)
            payload['data']['checks']['rebuild']['data'][key] = value
            with self.subTest(key=key), self.adapter(payload) as (adapter, calls, _):
                with self.assertRaises(CadCallUnknown): adapter._call('review_model_health', self.arguments)
                self.assertTrue(adapter.client.unknown)
                self.assertEqual(len(calls), 1)

    def test_missing_failed_or_inconsistent_checks_remain_unknown(self):
        variants = []
        for path, value in [
            (('check_count',), 8), (('successful_check_count',), 8), (('failed_check_count',), 1),
            (('failed_checks',), ['rebuild']), (('successful_checks',), ['rebuild']*9),
            (('checks','rebuild','success'), False), (('checks','rebuild','data','raw_result'), False),
            (('checks','rebuild','data','scope'), 'all_configurations'),
            (('checks','document','data','type_code'), 2),
            (('geometry_contract_count',), True), (('geometry_contracts_passed',), 1),
            (('failed_geometry_contracts',), []), (('geometry_contracts',), []),
        ]:
            payload = deepcopy(self.payload)
            current = payload['data']
            for key in path[:-1]: current = current[key]
            current[path[-1]] = value
            variants.append(payload)
        payload = deepcopy(self.payload)
        del payload['data']['checks']['features']
        variants.append(payload)
        for index, payload in enumerate(variants):
            with self.subTest(index=index), self.adapter(payload) as (adapter, _, _):
                with self.assertRaises(CadCallUnknown): adapter._call('review_model_health', self.arguments)

    def test_unverified_runtime_identity_or_arguments_remain_unknown(self):
        for path, value in [
            (('tool',), 'extrude_sketch'), (('error_code',), 'workflow_failed'),
            (('arguments',), {}), (('message',), 'Some checks failed'),
            (('benchmark_instance','pid'), 124), (('process_isolation','returncode'), 1),
            (('process_isolation','returncode'), False), (('process_isolation','worker_pid'), 0),
            (('process_isolation','same_worker_instance_guard'), False),
            (('executor','timeout_enforced'), False), (('executor','timeout_exceeded'), True),
        ]:
            payload = deepcopy(self.payload)
            current = payload
            for key in path[:-1]: current = current[key]
            current[path[-1]] = value
            with self.subTest(path=path, value=value), self.adapter(payload) as (adapter, _, _):
                with self.assertRaises(CadCallUnknown): adapter._call('review_model_health', self.arguments)

    def test_changed_vendor_does_not_gain_completion_exception(self):
        with self.adapter(source_matches=False) as (adapter, _, _):
            with self.assertRaises(CadCallUnknown): adapter._call('review_model_health', self.arguments)

    def test_known_failure_does_not_clear_prior_unknown(self):
        with self.adapter() as (adapter, _, _):
            adapter.client.unknown = True
            adapter._call('review_model_health', self.arguments)
            self.assertTrue(adapter.client.unknown)

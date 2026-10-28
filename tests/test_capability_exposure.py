"""Versioned Gateway exposure with real, attempt-owned auxiliary stdio."""
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

import test_cad_gateway as legacy
from fusion_pipeline.capabilities import load_registry, normalize_selection, freeze_capabilities
from fusion_pipeline.worker_gateway import WorkerGateway

PUBLIC = 'aux__engineering_lookup__lookup_public_note'
FIXTURE = Path(__file__).parent / 'fixtures' / 'auxiliary_mcp_server.py'


class CapabilityExposureTests(unittest.TestCase):
    setUp = legacy.CadGatewayTests.setUp
    plan = legacy.CadGatewayTests.plan

    def gateway(self, *, skills=True, auxiliary=False, no_cad=False, mode='normal', budget=80):
        skill_dir = self.root / 'sources'
        skill_dir.mkdir()
        (skill_dir / 'SKILL.md').write_text('Optional strategy: inspect ribs.', encoding='utf-8')
        (skill_dir / 'note.txt').write_text('Declared supporting note.', encoding='utf-8')
        (skill_dir / 'BETA.md').write_text('SKILL_BETA_MARKER disabled advice.', encoding='utf-8')
        self.sentinel = self.root / 'sentinel.jsonl'
        registry = {'schema': 'candidate-capability-registry/1', 'skills': {'guidance': {
            'path': str(skill_dir / 'SKILL.md'), 'version': '1', 'cad_backends': ['solidworks'],
            'requires_mcp': [], 'resources': [{'id': 'note', 'path': 'note.txt',
                'sha256': hashlib.sha256((skill_dir / 'note.txt').read_bytes()).hexdigest()}]},
                'beta': {'path': str(skill_dir / 'BETA.md'), 'version': '1', 'cad_backends': ['solidworks'],
                         'requires_mcp': [], 'resources': [], 'description': 'BETA_DESCRIPTION_MARKER'}},
            'mcp_servers': {'cad_native': {'kind': 'builtin_cad_adapter'}, 'engineering_lookup': {
                'kind': 'stdio', 'command': sys.executable,
                'args': ['-B', str(FIXTURE.resolve()), str(self.sentinel), mode],
                'source_files': [str(FIXTURE.resolve())], 'env_allowlist': [],
                'allowed_tools': ['lookup_public_note'], 'timeout_s': 0.4,
                'max_response_bytes': 4096, 'side_effect_policy': 'read_only'}}}
        registry_path = self.root / 'registry.json'
        registry_path.write_text(json.dumps(registry), encoding='utf-8')
        registry = load_registry(registry_path)
        selection = normalize_selection({'candidate_capabilities': {'schema': 'candidate-capabilities/1',
            'study_kind': 'negative_control_no_cad' if no_cad else 'capability_ablation',
            'skill_delivery': 'gateway_read', 'exposure_mode': 'gateway_proxy',
            'skills': skills if isinstance(skills, list) else ['guidance'] if skills else [],
            'mcp_servers': ([] if no_cad else ['cad_native']) + (['engineering_lookup'] if auxiliary else [])}},
            registry, cad_backend='solidworks')
        self.manifest = freeze_capabilities(selection, registry, self.root / 'frozen')
        manifest_path = self.root / 'frozen' / 'capability-manifest.json'
        self.contract.update(schema='cad-worker/2', capability_manifest=str(manifest_path),
            capability_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(), max_tool_calls=budget)
        self.path.write_text(json.dumps(self.contract), encoding='utf-8')
        gateway = WorkerGateway(self.path, upstream=self.native)
        self.addCleanup(gateway.close)
        return gateway

    def events(self):
        return [json.loads(line) for line in self.sentinel.read_text().splitlines()] if self.sentinel.exists() else []

    def test_empty_skills_deny_aliases_resources_and_paths_before_file_reads(self):
        gateway = self.gateway(skills=False)
        self.assertEqual(gateway.call('list_skills', {}), {'skills': []})
        self.assertNotIn('cad_skill', gateway.instructions)
        for name, arguments in [('read_input', {'name': 'cad_skill'}),
                ('read_input', {'name': 'fusion_skill'}), ('read_input', {'name': 'modeling_skill'}),
                ('read_input', {'name': str(self.root / 'sources' / 'SKILL.md')}),
                ('read_skill', {'id': 'guidance'}), ('read_skill', {'id': '../guidance'}),
                ('read_skill', {'id': 'guidance', 'resource_id': 'note'})]:
            with self.subTest(name=name, arguments=arguments), self.assertRaisesRegex(RuntimeError, 'denied|skill_not_enabled'):
                gateway.call(name, arguments)
        self.assertFalse(self.sentinel.exists())

    def test_selected_public_projection_and_actual_resource_read_evidence(self):
        gateway = self.gateway()
        contract = gateway.call('get_contract', {})
        public = contract['public_capabilities']
        self.assertEqual(public['skills'][0]['id'], 'guidance')
        self.assertNotIn('snapshot_path', json.dumps(contract))
        self.assertNotIn(str(self.root / 'registry.json'), json.dumps(contract))
        self.assertNotIn('BETA', json.dumps(contract) + gateway.instructions)
        self.assertEqual([s['id'] for s in gateway.call('list_skills', {})['skills']], ['guidance'])
        with self.assertRaisesRegex(RuntimeError, '^skill_not_enabled$'):
            gateway.call('read_skill', {'id': 'beta'})
        self.assertEqual(gateway.call('read_skill', {'id': 'guidance'})['text'], 'Optional strategy: inspect ribs.')
        self.assertEqual(gateway.call('read_skill', {'id': 'guidance', 'resource_id': 'note'})['text'], 'Declared supporting note.')
        evidence = json.loads((self.audit / 'capability-evidence.json').read_text())
        self.assertEqual([e['resource_id'] for e in evidence['actual_skill_reads']], [None, 'note'])
        self.assertEqual(evidence['exposure_verification_status'], 'not_verified')

    def test_multiple_selected_skills_keep_order_and_resource_id_boundary(self):
        gateway = self.gateway(skills=['beta', 'guidance'])
        self.assertEqual([entry['id'] for entry in gateway.call('list_skills', {})['skills']], ['beta', 'guidance'])
        self.assertIn('SKILL_BETA_MARKER', gateway.call('read_skill', {'id': 'beta'})['text'])
        for resource_id in ['../note.txt', 'note.txt', str(self.root / 'sources' / 'note.txt'), 'undeclared']:
            with self.subTest(resource_id=resource_id), self.assertRaisesRegex(RuntimeError, 'skill_resource_denied'):
                gateway.call('read_skill', {'id': 'guidance', 'resource_id': resource_id})

    def test_returned_tools_list_mutation_does_not_rewrite_observed_evidence(self):
        gateway = self.gateway(auxiliary=True)
        response = gateway.list_tools()
        expected = json.loads(json.dumps(response))
        response['tools'][0]['description'] = 'Caller mutation'
        gateway.close()
        evidence = json.loads((self.audit / 'capability-evidence.json').read_text())
        self.assertEqual(evidence['tools_list_responses'][0]['response'], expected)

    def test_disabled_auxiliary_forgery_never_starts_process(self):
        gateway = self.gateway(skills=False)
        response = gateway.list_tools()
        self.assertNotIn(PUBLIC, [t['name'] for t in response['tools']])
        with self.assertRaisesRegex(RuntimeError, 'unknown_worker_tool'):
            gateway.call(PUBLIC, {'query': 'steel'})
        self.assertFalse(self.sentinel.exists())

    def test_real_discovery_calls_and_exact_tools_list_exposure_evidence(self):
        gateway = self.gateway(auxiliary=True)
        self.assertFalse(self.sentinel.exists())
        response = gateway.list_tools()
        self.assertEqual(next(t for t in response['tools'] if t['name'] == PUBLIC)['description'], 'Read a public engineering note.')
        result = gateway.call(PUBLIC, {'query': 'steel'})
        self.assertEqual(result['content'][0]['text'], 'Public note: steel')
        evidence = json.loads((self.audit / 'capability-evidence.json').read_text())
        self.assertEqual(evidence['tools_list_responses'][0]['response'], response)
        self.assertEqual(evidence['per_server_tool_calls']['engineering_lookup'], 1)
        self.assertEqual(evidence['exposure_verification_status'], 'tools_list_recorded')
        self.assertTrue(gateway.close()['verified_stopped'])

    def test_basic_cad_and_auxiliary_calls_share_one_small_budget(self):
        gateway = self.gateway(auxiliary=True, budget=3)
        gateway.list_tools()
        gateway.call('get_contract', {})
        gateway.call('cad_tools', {})
        gateway.call(PUBLIC, {'query': 'first'})
        with self.assertRaisesRegex(RuntimeError, 'tool_budget_exceeded'):
            gateway.call(PUBLIC, {'query': 'extra'})
        self.assertEqual(len([e for e in self.events() if e['event'] == 'tools/call']), 1)

    def test_auxiliary_timeout_does_not_mark_cad_unknown(self):
        gateway = self.gateway(auxiliary=True, mode='timeout')
        gateway.list_tools()
        with self.assertRaisesRegex(RuntimeError, 'auxiliary_timeout'):
            gateway.call(PUBLIC, {'query': 'steel'})
        self.assertEqual(gateway._state()['pending_cad'], 0)
        self.assertTrue(gateway.call('cad_tools', {})['tools'])

    def test_cancellation_revokes_and_closes_started_auxiliary(self):
        gateway = self.gateway(auxiliary=True)
        gateway.list_tools()
        cancel = self.root / 'cancel'
        gateway.contract['cancel_file'] = str(cancel)
        cancel.touch()
        with self.assertRaisesRegex(RuntimeError, 'attempt_cancelled'):
            gateway.call(PUBLIC, {'query': 'forbidden'})
        self.assertTrue(gateway.close()['verified_stopped'])
        self.assertFalse(any(e['event'] == 'tools/call' for e in self.events()))

    def test_negative_control_removes_cad_surface_and_prevents_native_submission(self):
        gateway = self.gateway(skills=False, no_cad=True)
        names = [t['name'] for t in gateway.list_tools()['tools']]
        self.assertNotIn('cad_tools', names)
        self.assertNotIn('cad_call', names)
        self.assertNotIn('cad_call', gateway.instructions)
        with self.assertRaisesRegex(RuntimeError, 'cad_capability_disabled'):
            gateway.call('submit', {})
        self.assertEqual(self.native.calls, [])

    def test_manifest_tampering_fails_before_exposure(self):
        gateway = self.gateway()
        manifest_path = Path(self.contract['capability_manifest'])
        manifest_path.write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'capability_integrity_failed'):
            WorkerGateway(self.path, upstream=self.native)

    def test_snapshot_tampering_rejected_on_read_without_evidence(self):
        gateway = self.gateway()
        Path(self.manifest['skills']['guidance']['snapshot_path']).write_text('tampered')
        with self.assertRaisesRegex(RuntimeError, 'capability_integrity_failed'):
            gateway.call('read_skill', {'id': 'guidance'})

    def test_inflight_auxiliary_cancellation_stops_process_without_cad_pending(self):
        gateway = self.gateway(auxiliary=True, mode='timeout')
        gateway.list_tools()
        cancel = self.root / 'cancel'
        gateway.contract['cancel_file'] = str(cancel)
        errors = []
        def call():
            try:
                gateway.call(PUBLIC, {'query': 'blocked'})
            except RuntimeError as error:
                errors.append(str(error))
        thread = threading.Thread(target=call)
        thread.start()
        until = time.monotonic() + 3
        while time.monotonic() < until and not any(e['event'] == 'tools/call' for e in self.events()):
            time.sleep(0.01)
        cancel.touch()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors)
        self.assertEqual(gateway._state()['pending_cad'], 0)
        self.assertTrue(gateway.close()['verified_stopped'])

    def test_native_submission_revokes_and_cleans_auxiliary_service(self):
        gateway = self.gateway(skills=False, auxiliary=True)
        gateway.list_tools()
        def cad(name, arguments):
            return gateway.call('cad_call', {'tool': name, 'arguments': arguments})
        native = self.workspace / 'submission.SLDPRT'
        for name, arguments in [('create_new_part', {}), ('save_document', {'filepath': str(native)})]:
            cad('validate_parameter_plan', {'plan': self.plan([(name, arguments)])})
            cad(name, {**arguments, 'parameter_plan_token': 'issued-native-token'})
        cad('close_document', {'document_title': native.name, 'expected_path': str(native), 'save': False})
        gateway.call('write_report', {'report': {'status': 'declared'}})
        receipt = gateway.call('submit', {'configuration': 'Default'})
        self.assertEqual(receipt['native_document']['configuration'], 'Default')
        with self.assertRaisesRegex(RuntimeError, 'attempt_submitted'):
            gateway.call(PUBLIC, {'query': 'forbidden'})
        with self.assertRaisesRegex(RuntimeError, 'attempt_submitted'):
            gateway.list_tools()
        evidence = json.loads((self.audit / 'capability-evidence.json').read_text())
        self.assertTrue(evidence['cleanup_evidence']['verified_stopped'])
        self.assertFalse(any(e['event'] == 'tools/call' for e in self.events()))


class ExplicitCadGateRegressionTests(legacy.CadGatewayTests):
    """Every legacy native gate also executes with explicit empty skills."""
    def gateway(self):
        if not self.path.exists():
            registry = {'schema': 'candidate-capability-registry/1', 'skills': {},
                        'mcp_servers': {'cad_native': {'kind': 'builtin_cad_adapter'}}}
            selection = normalize_selection({'candidate_capabilities': {'schema': 'candidate-capabilities/1',
                'study_kind': 'capability_ablation', 'skill_delivery': 'gateway_read',
                'exposure_mode': 'gateway_proxy', 'skills': [], 'mcp_servers': ['cad_native']}},
                registry, cad_backend=self.contract['cad_backend'])
            freeze_capabilities(selection, registry, self.root / 'frozen')
            manifest = self.root / 'frozen' / 'capability-manifest.json'
            self.contract.update(schema='cad-worker/2', capability_manifest=str(manifest),
                capability_manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest())
        return super().gateway()

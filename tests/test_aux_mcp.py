"""Controlled auxiliary service tests using real disposable stdio processes."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fusion_pipeline import aux_mcp


PUBLIC = 'aux__engineering_lookup__lookup_public_note'
FIXTURE = Path(__file__).parent / 'fixtures' / 'auxiliary_mcp_server.py'


class AuxiliaryMCPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sentinel = self.root / 'events.jsonl'

    def manager(self, mode='normal', enabled=True, deadline=None, **overrides):
        definition = {'kind': 'stdio', 'command': sys.executable,
                      'args': ['-B', str(FIXTURE), str(self.sentinel), mode],
                      'source_files': [str(FIXTURE)],
                      'source_file_hashes': [{'path': str(FIXTURE),
                          'sha256': hashlib.sha256(FIXTURE.read_bytes()).hexdigest()}],
                      'command_sha256': hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
                      'env_allowlist': [], 'allowed_tools': ['lookup_public_note'],
                      'side_effect_policy': 'read_only', 'timeout_s': 1.5, 'max_response_bytes': 4096}
        definition.update(overrides)
        manifest = {'ordered_enabled_mcp_ids': ['cad_native', 'engineering_lookup'] if enabled else ['cad_native'],
                    'requested_server_definitions': {'engineering_lookup': definition}}
        self.latest_manifest = copy.deepcopy(manifest)
        manager = aux_mcp.AuxiliaryMCPManager(manifest, self.root / 'audit',
                    time.monotonic() + 15 if deadline is None else deadline, dict(os.environ))
        self.addCleanup(manager.close)
        return manager

    def events(self):
        return [json.loads(line) for line in self.sentinel.read_text().splitlines()] if self.sentinel.exists() else []

    def test_disabled_service_never_starts_even_on_forged_call(self):
        manager = self.manager(enabled=False)
        self.assertEqual(manager.discover(), {})
        with self.assertRaisesRegex(aux_mcp.AuxiliaryMCPError, 'auxiliary_tool_not_enabled'):
            manager.call(PUBLIC, {'query': 'steel'})
        self.assertFalse(self.sentinel.exists())
        self.assertTrue(manager.close()['verified_stopped'])

    def test_real_initialize_discover_call_and_audit_receipt(self):
        manager = self.manager()
        surface = manager.discover()
        self.assertEqual(list(surface), [PUBLIC])
        self.assertEqual(surface[PUBLIC]['inputSchema']['required'], ['query'])
        self.assertEqual(surface[PUBLIC]['description'], 'Read a public engineering note.')
        self.assertEqual(manager.call(PUBLIC, {'query': 'steel'})['content'][0]['text'], 'Public note: steel')
        self.assertEqual([e['event'] for e in self.events()][:4],
                         ['started', 'initialize', 'notifications/initialized', 'tools/list'])
        evidence = manager.evidence()
        self.assertEqual(evidence['per_server_tool_calls'], {'engineering_lookup': 1})
        self.assertEqual(evidence['observed_server_identity']['engineering_lookup']['name'], 'public-note-fixture')
        self.assertEqual(len(evidence['candidate_visible_tool_schema_hash']), 64)
        self.assertTrue((self.root / 'audit' / 'auxiliary-mcp.jsonl').is_file())
        receipt = manager.close()
        self.assertTrue(receipt['verified_stopped'])
        self.assertEqual(receipt['servers'][0]['remaining_processes'], 0)

    def test_returned_schema_cannot_mutate_frozen_dispatch(self):
        manager = self.manager()
        surface = manager.discover()
        surface[PUBLIC]['upstream_name'] = 'delete_everything'
        manager.call(PUBLIC, {'query': 'ok'})
        calls = [e for e in self.events() if e['event'] == 'tools/call']
        self.assertEqual(calls[0]['arguments']['name'], 'lookup_public_note')

    def test_forbidden_or_invalid_arguments_never_reach_upstream(self):
        manager = self.manager()
        manager.discover()
        for name, args in [(PUBLIC + '_write', {}), (PUBLIC, []), (PUBLIC, {'query': float('nan')})]:
            with self.assertRaises(aux_mcp.AuxiliaryMCPError):
                manager.call(name, args)
        self.assertFalse(any(e['event'] == 'tools/call' for e in self.events()))

    def test_transport_faults_are_bounded_auxiliary_failures(self):
        for mode, code in [('invalid', 'auxiliary_invalid_json'), ('oversize', 'auxiliary_response_too_large'),
                           ('exit', 'auxiliary_process_exited'), ('timeout', 'auxiliary_timeout')]:
            with self.subTest(mode=mode):
                manager = self.manager(mode, timeout_s=0.35)
                manager.discover()
                started = time.monotonic()
                with self.assertRaises(aux_mcp.AuxiliaryMCPError) as caught:
                    manager.call(PUBLIC, {'query': 'steel'})
                self.assertEqual(caught.exception.code, code)
                self.assertFalse(getattr(caught.exception, 'reconciliation_required', False))
                self.assertLess(time.monotonic() - started, 5)
                self.assertTrue(manager.close()['verified_stopped'])

    def test_schema_drift_stops_call_before_dispatch(self):
        manager = self.manager('drift')
        manager.discover()
        with self.assertRaisesRegex(aux_mcp.AuxiliaryMCPError, 'auxiliary_schema_drift'):
            manager.call(PUBLIC, {'query': 'steel'})
        self.assertFalse(any(e['event'] == 'tools/call' for e in self.events()))

    def test_duplicate_or_writable_tools_fail_discovery(self):
        for mode in ['duplicate', 'writable']:
            with self.subTest(mode=mode):
                manager = self.manager(mode)
                with self.assertRaises(aux_mcp.AuxiliaryMCPError):
                    manager.discover()
                self.assertTrue(manager.close()['verified_stopped'])

    def test_definition_and_source_drift_rejected_before_start(self):
        for overrides in [{'side_effect_policy': 'write'}, {'allowed_tools': []},
                          {'command_sha256': '0' * 64}, {'source_file_hashes': []}]:
            with self.subTest(overrides=overrides):
                with self.assertRaises(aux_mcp.AuxiliaryMCPError):
                    manager = self.manager(**overrides)
                    manager.discover()
                self.assertFalse(self.sentinel.exists())

    def test_revocation_and_expired_deadline_prevent_start(self):
        manager = self.manager()
        manager.revoke('submitted')
        with self.assertRaisesRegex(aux_mcp.AuxiliaryMCPError, 'auxiliary_revoked'):
            manager.discover()
        self.assertFalse(self.sentinel.exists())
        manager = self.manager()
        manager.deadline = time.monotonic() - 1
        with self.assertRaisesRegex(aux_mcp.AuxiliaryMCPError, 'auxiliary_deadline_expired'):
            manager.discover()
        self.assertFalse(self.sentinel.exists())

    def test_cancel_interrupts_inflight_call_and_cleans_tree(self):
        manager = self.manager('timeout', timeout_s=10)
        manager.discover()
        errors = []
        def invoke():
            try:
                manager.call(PUBLIC, {'query': 'steel'})
            except aux_mcp.AuxiliaryMCPError as error:
                errors.append(error.code)
        thread = threading.Thread(target=invoke)
        thread.start()
        until = time.monotonic() + 3
        while not any(e['event'] == 'tools/call' for e in self.events()) and time.monotonic() < until:
            time.sleep(0.02)
        self.assertTrue(any(e['event'] == 'tools/call' for e in self.events()))
        manager.cancel()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ['auxiliary_revoked'])
        self.assertTrue(manager.close()['verified_stopped'])

    def test_exited_server_child_is_owned_and_cleaned(self):
        manager = self.manager('child')
        manager.discover()
        manager.call(PUBLIC, {'query': 'steel'})
        self.assertTrue(any(e['event'] == 'child' for e in self.events()))
        receipt = manager.close()
        self.assertTrue(receipt['verified_stopped'])
        self.assertEqual(receipt['servers'][0]['remaining_processes'], 0)

    def test_cleanup_failure_is_explicit_and_can_be_retried(self):
        manager = self.manager()
        manager.discover()
        from fusion_pipeline.adapters import _WindowsProcessTree, _PosixProcessTree
        cls = _WindowsProcessTree if os.name == 'nt' else _PosixProcessTree
        with patch.object(cls, 'stop', side_effect=OSError('cleanup fixture failure')):
            receipt = manager.close()
        self.assertFalse(receipt['verified_stopped'])
        self.assertEqual(receipt['servers'][0]['error_type'], 'OSError')
        self.assertTrue(manager.close()['verified_stopped'])

    def test_oversized_arguments_do_not_count_as_dispatched_calls(self):
        manager = self.manager()
        manager.discover()
        with self.assertRaisesRegex(aux_mcp.AuxiliaryMCPError, 'auxiliary_request_too_large'):
            manager.call(PUBLIC, {'query': 'x' * 8192})
        self.assertEqual(manager.evidence()['per_server_tool_calls']['engineering_lookup'], 0)
        self.assertFalse(any(e['event'] == 'tools/call' for e in self.events()))

    def test_external_schema_reference_is_rejected_before_exposure(self):
        manager = self.manager('external_schema')
        with self.assertRaisesRegex(aux_mcp.AuxiliaryMCPError, 'auxiliary_invalid_schema'):
            manager.discover()

    def test_only_allowlisted_environment_is_inherited(self):
        with patch.dict(os.environ, {'AUX_PUBLIC_TEST': 'public', 'AUX_PRIVATE_TEST': 'private'}):
            manager = self.manager(env_allowlist=['AUX_PUBLIC_TEST'])
        manager.discover()
        started = self.events()[0]
        self.assertEqual(started['allowed'], 'public')
        self.assertFalse(started['forbidden_present'])

    def test_stderr_is_drained_and_retained_bytes_are_bounded(self):
        manager = self.manager('stderr')
        manager.discover()
        self.assertEqual(manager.call(PUBLIC, {'query': 'steel'})['content'][0]['text'], 'Public note: steel')
        self.assertTrue(manager.close()['verified_stopped'])
        stderr = list((self.root / 'audit').glob('*.stderr'))
        self.assertEqual(len(stderr), 1)
        self.assertGreater(stderr[0].stat().st_size, 0)
        self.assertLessEqual(stderr[0].stat().st_size, 4096)

    def test_conflicting_namespaces_are_rejected_without_tool_dispatch(self):
        self.manager()
        definition = copy.deepcopy(self.latest_manifest['requested_server_definitions']['engineering_lookup'])
        first = copy.deepcopy(definition)
        first.update(allowed_tools=['b__lookup_public_note'])
        first['args'][-1] = 'prefix'
        manifest = {'ordered_enabled_mcp_ids': ['a', 'a__b'],
                    'requested_server_definitions': {'a': first, 'a__b': definition}}
        manager = aux_mcp.AuxiliaryMCPManager(manifest, self.root / 'audit', time.monotonic() + 15, {})
        self.addCleanup(manager.close)
        with self.assertRaisesRegex(aux_mcp.AuxiliaryMCPError, 'auxiliary_tool_name_conflict'):
            manager.discover()
        self.assertFalse(any(e['event'] == 'tools/call' for e in self.events()))
        self.assertTrue(manager.close()['verified_stopped'])

    def test_nonfinite_deadline_is_rejected(self):
        for deadline in [float('nan'), float('inf')]:
            with self.subTest(deadline=deadline):
                with self.assertRaisesRegex(aux_mcp.AuxiliaryMCPError, 'auxiliary_invalid_deadline'):
                    self.manager(deadline=deadline)
        self.assertFalse(self.sentinel.exists())

    def test_server_that_stops_reading_cannot_block_request_write(self):
        manager = self.manager('stop_reading', max_response_bytes=1024 * 1024, timeout_s=0.35)
        manager.discover()
        errors = []
        def invoke():
            try:
                manager.call(PUBLIC, {'query': 'x' * 700000})
            except aux_mcp.AuxiliaryMCPError as error:
                errors.append(error.code)
        thread = threading.Thread(target=invoke, daemon=True)
        thread.start()
        thread.join(2)
        remained_blocked = thread.is_alive()
        manager.cancel()
        thread.join(3)
        self.assertFalse(remained_blocked, 'request write exceeded its timeout')
        self.assertEqual(errors, ['auxiliary_timeout'])

    def test_persistent_audit_failure_during_call_still_cleans_owned_process(self):
        manager = self.manager()
        manager.discover()
        process = manager._services['engineering_lookup'].process
        with patch.object(manager, '_event', side_effect=OSError('audit unavailable')):
            try:
                manager.call(PUBLIC, {'query': 'steel'})
            except Exception as error:
                failure = error
            else:
                self.fail('audit failure must fail the operation')
            self.assertIsInstance(failure, aux_mcp.AuxiliaryMCPError)
            self.assertIsNotNone(process.poll(), 'audit failure leaked a running owned process')
            receipt = failure.cleanup_receipt
            self.assertTrue(receipt['verified_stopped'])
            self.assertEqual(receipt['audit_status'], 'failed')
            self.assertTrue(failure.audit_failure_evidence)

    def test_persistent_audit_failure_during_submission_revocation_still_cleans_process(self):
        manager = self.manager()
        manager.discover()
        process = manager._services['engineering_lookup'].process
        with patch.object(manager, '_event', side_effect=OSError('audit unavailable')):
            try:
                receipt = manager.revoke('submitted')
            except Exception as error:
                self.fail('revocation must return its cleanup/audit receipt: ' + type(error).__name__)
            self.assertIsNotNone(process.poll(), 'submission revocation leaked its owned process')
            self.assertTrue(receipt['verified_stopped'])
            self.assertEqual(receipt['audit_status'], 'failed')
            self.assertTrue(receipt['audit_failure_evidence'])
            self.assertEqual(manager.evidence()['cleanup_evidence'], receipt)
            with self.assertRaisesRegex(aux_mcp.AuxiliaryMCPError, 'auxiliary_revoked'):
                manager.call(PUBLIC, {'query': 'steel'})


if __name__ == '__main__':
    unittest.main()

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fusion_pipeline.cad_probe import run_probe


class FakeAdapter:
    identity = {'backend': 'solidworks', 'instance': None}
    closed = False
    def list_tools(self):
        return {'create_new_part': {'name': 'create_new_part', 'inputSchema': {'type': 'object'}}}
    def connect(self, launch_if_needed=False):
        self.launch = launch_if_needed
        return {'safe': True, 'documents': [], 'instance': {'pid': 77, 'session_id': 1, 'version': '34.4'}}
    def close(self):
        self.closed = True


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'cad_backend': 'solidworks'}), encoding='utf-8')
        self.adapter = FakeAdapter()

    def test_discovery_never_calls_connect_and_closes_transport(self):
        with patch('fusion_pipeline.cad_probe.create_adapter', return_value=self.adapter), \
             patch.object(self.adapter, 'connect', side_effect=AssertionError('no CAD')):
            report = run_probe(self.config, self.root/'out', stage='discovery')
        self.assertEqual(report['status'], 'passed')
        self.assertFalse(report['cad_connection']['executed'])
        self.assertTrue(self.adapter.closed)

    def test_connect_uses_explicit_launch_flag_and_shared_lease(self):
        lease = self.root / 'fusion-benchmark-desktop.lock'
        with patch('fusion_pipeline.cad_probe.create_adapter', return_value=self.adapter), \
             patch('fusion_pipeline.cad_probe.desktop_lease_path', return_value=lease):
            report = run_probe(self.config, self.root/'out', stage='connect')
        self.assertEqual(report['status'], 'passed')
        self.assertFalse(self.adapter.launch)
        self.assertFalse(lease.exists())

    def test_existing_lease_is_never_removed_or_contacted(self):
        lease = self.root/'fusion-benchmark-desktop.lock'
        lease.write_text('other owner')
        with patch('fusion_pipeline.cad_probe.create_adapter') as factory, \
             patch('fusion_pipeline.cad_probe.desktop_lease_path', return_value=lease):
            report = run_probe(self.config, self.root/'out', stage='connect')
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(lease.read_text(), 'other owner')
        factory.assert_not_called()

    def test_unknown_connect_retains_lease_and_failure_evidence(self):
        from fusion_pipeline.stdio_mcp import CadCallUnknown
        lease = self.root/'fusion-benchmark-desktop.lock'
        with patch('fusion_pipeline.cad_probe.create_adapter', return_value=self.adapter), \
             patch('fusion_pipeline.cad_probe.desktop_lease_path', return_value=lease), \
             patch.object(self.adapter, 'connect', side_effect=CadCallUnknown('unconfirmed')):
            report = run_probe(self.config, self.root/'out', stage='connect')
        self.assertTrue(report['reconciliation_required'])
        self.assertTrue(lease.exists())
        self.assertEqual(json.loads((self.root/'out/probe.json').read_text()), report)

    def test_interruption_during_connect_preserves_unknown_lease(self):
        lease = self.root/'fusion-benchmark-desktop.lock'
        with patch('fusion_pipeline.cad_probe.create_adapter', return_value=self.adapter), \
             patch('fusion_pipeline.cad_probe.desktop_lease_path', return_value=lease), \
             patch.object(self.adapter, 'connect', side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                run_probe(self.config, self.root/'out', stage='connect')
        self.assertTrue(lease.exists())
        report = json.loads((self.root/'out/probe.json').read_text())
        self.assertTrue(report['reconciliation_required'])

    def test_report_publication_failure_preserves_lease(self):
        lease = self.root/'fusion-benchmark-desktop.lock'
        original = Path.open
        def failing_open(path, *args, **kwargs):
            if path.name == 'probe.json':
                raise OSError('injected evidence storage failure')
            return original(path, *args, **kwargs)
        with patch('fusion_pipeline.cad_probe.create_adapter', return_value=self.adapter), \
             patch('fusion_pipeline.cad_probe.desktop_lease_path', return_value=lease), \
             patch.object(Path, 'open', failing_open):
            with self.assertRaises(OSError):
                run_probe(self.config, self.root/'out', stage='connect')
        self.assertTrue(lease.exists())

    def test_cleanup_unknown_after_success_preserves_lease(self):
        from types import SimpleNamespace
        lease = self.root/'fusion-benchmark-desktop.lock'
        self.adapter.client = SimpleNamespace(unknown=False, cleanup_evidence=[])
        def close_unknown():
            self.adapter.client.unknown = True
            self.adapter.client.cleanup_evidence = [{'unconfirmed': True}]
        with patch('fusion_pipeline.cad_probe.create_adapter', return_value=self.adapter), \
             patch('fusion_pipeline.cad_probe.desktop_lease_path', return_value=lease), \
             patch.object(self.adapter, 'close', side_effect=close_unknown):
            report = run_probe(self.config, self.root/'out', stage='connect')
        self.assertEqual(report['status'], 'failed')
        self.assertTrue(report['reconciliation_required'])
        self.assertTrue(lease.exists())

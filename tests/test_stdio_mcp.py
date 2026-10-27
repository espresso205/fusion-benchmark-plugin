import sys
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fusion_pipeline.stdio_mcp import StdioMCP, CadCallUnknown


SERVER = '''import json,sys
for line in sys.stdin:
 r=json.loads(line)
 if 'id' not in r: continue
 m=r['method']
 if m=='initialize': out={'protocolVersion':'2025-03-26','serverInfo':{'name':'fixture','version':'1'},'capabilities':{}}
 elif m=='tools/list': out={'tools':[{'name':'probe','inputSchema':{'type':'object','properties':{}}}]}
 elif m=='tools/call':
  if r['params']['name']=='disconnect': sys.exit(0)
  out={'content':[{'type':'text','text':'{"success":false,"ok":false,"data":{}}'}]}
 else: out={}
 print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':out}),flush=True)
'''


class StdioTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows owned process identity')
    def test_duplicate_reused_pids_close_cleanly_but_preserve_existing_unknown(self):
        for prior_unknown in (False, True):
            with self.subTest(prior_unknown=prior_unknown), tempfile.TemporaryDirectory() as folder:
                client = StdioMCP(sys.executable, ['-u', '-c', SERVER], cwd=folder, audit_dir=folder, timeout=2)
                # Two completed workers may legitimately have reused the same PID.
                records = [{'pid': 123, 'created': time, 'executable': 'C:/fixture/python.exe'} for time in (100, 200)]
                client.worker_record_path.write_text('\n'.join(json.dumps(record) for record in records)+'\n', encoding='utf-8')
                actual = {'pid': 123, 'created': 300, 'executable': 'C:/unrelated/other.exe'}
                kernel = SimpleNamespace(CloseHandle=Mock(), TerminateProcess=Mock(), WaitForSingleObject=Mock())
                client.unknown = prior_unknown
                with patch('fusion_pipeline.solidworks_helper._process_handle', return_value=(kernel, 999)), patch(
                        'fusion_pipeline.solidworks_helper._process_record_handle', return_value=actual):
                    client.close()
                self.assertEqual(client.unknown, prior_unknown)
                self.assertEqual(len(client.cleanup_evidence), 2)
                self.assertTrue(all(item.get('already_exited') and item.get('pid_reused') for item in client.cleanup_evidence))
                self.assertEqual([item['recorded_identity']['created'] for item in client.cleanup_evidence], [100, 200])
                kernel.TerminateProcess.assert_not_called()

    def test_handshake_and_business_failure_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            client = StdioMCP(sys.executable, ['-u', '-c', SERVER], cwd=folder, audit_dir=folder, timeout=2)
            try:
                self.assertIn('probe', client.tools)
                result = client.call_tool('probe', {})
                self.assertIn('false', result['content'][0]['text'])
            finally:
                client.close()
            self.assertIsNotNone(client.process.poll())

    def test_disconnect_marks_transport_unusable_without_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            client = StdioMCP(sys.executable, ['-u', '-c', SERVER], cwd=folder, audit_dir=folder, timeout=2)
            try:
                with self.assertRaises(CadCallUnknown):
                    client.call_tool('disconnect', {})
                self.assertTrue(client.unknown)
                with self.assertRaises(CadCallUnknown):
                    client.call_tool('probe', {})
            finally:
                client.close()

    def test_deadline_prevents_sending_request(self):
        with tempfile.TemporaryDirectory() as folder:
            client = StdioMCP(sys.executable, ['-u', '-c', SERVER], cwd=folder, audit_dir=folder, timeout=2)
            try:
                client.deadline = 0
                with self.assertRaises(TimeoutError):
                    client.call_tool('probe', {})
                self.assertFalse(client.unknown)
            finally:
                client.close()

    def test_notification_flood_cannot_extend_request_deadline(self):
        with tempfile.TemporaryDirectory() as folder:
            client = StdioMCP(sys.executable, ['-u', '-c', SERVER], cwd=folder, audit_dir=folder, timeout=2)
            try:
                messages = Mock()
                messages.get.side_effect = [
                    {'jsonrpc': '2.0', 'method': 'progress'},
                    {'jsonrpc': '2.0', 'id': 'test-request', 'result': {'too_late': True}}]
                with patch.object(client, '_responses', messages), \
                     patch('fusion_pipeline.stdio_mcp.uuid.uuid4', return_value=SimpleNamespace(hex='test-request')), \
                     patch('fusion_pipeline.stdio_mcp.time.monotonic', side_effect=[0, 0, 3]):
                    with self.assertRaises(CadCallUnknown):
                        client.call_tool('probe', {}, timeout=2)
                self.assertTrue(client.unknown)
                self.assertEqual(messages.get.call_count, 1)
            finally:
                client.close()

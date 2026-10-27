import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fusion_pipeline.cad import FusionAdapter
from fusion_pipeline.mcp import FusionMCP
from fusion_pipeline.stdio_mcp import CadCallUnknown


class Response:
    def __init__(self, payload):
        self.headers = {}
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode('utf-8')


class FusionTransportTests(unittest.TestCase):
    def server(self, calls, *, timeout_on_execute=True):
        def respond(request, timeout):
            body=json.loads(request.data)
            calls.append(body)
            method=body['method']
            if method=='initialize':
                result={'serverInfo':{'name':'local-fixture','version':'1'}}
            elif method=='tools/list':
                result={'tools':[{'name':'fusion_mcp_execute','inputSchema':{'type':'object'}}]}
            elif method=='notifications/initialized':
                result={}
            elif timeout_on_execute:
                raise TimeoutError('transport details and credentials must not leak')
            else:
                result={'content':[{'type':'text','text':'{"benchmark_probe":true,"documents":[]}'}]}
            return Response({'jsonrpc':'2.0','id':body.get('id'),'result':result})
        return respond

    def test_readonly_timeout_blocks_later_mutation_and_remains_runtime_error(self):
        calls=[]
        events=[]
        with patch('fusion_pipeline.mcp.urllib.request.urlopen',side_effect=self.server(calls)):
            client=FusionMCP({'mcp':{'autodeskfusion':{'url':'http://127.0.0.1:1234/mcp'}}},audit=events.append)
            with self.assertRaises(CadCallUnknown) as raised:
                client.execute('readonly diagnostic',readonly=True)
            self.assertIsInstance(raised.exception,RuntimeError)
            self.assertTrue(client.unknown)
            self.assertNotIn('credentials',str(raised.exception))
            dispatched=len(calls)
            with self.assertRaises(CadCallUnknown):
                client.execute('must never dispatch',readonly=False)
            self.assertEqual(len(calls),dispatched)
            self.assertEqual(events[-1]['event'],'mcp_request_unknown')
            with self.assertRaises(CadCallUnknown):
                client.close()

    def test_adapter_close_retains_unknown_for_the_desktop_lease_owner(self):
        calls=[]
        with tempfile.TemporaryDirectory() as folder, patch(
                'fusion_pipeline.mcp.urllib.request.urlopen',side_effect=self.server(calls)):
            adapter=FusionAdapter({'fusion_connection':{'url':'http://localhost:1234/mcp'}},
                workspace=folder,audit_dir=Path(folder)/'audit')
            with self.assertRaises(CadCallUnknown):
                adapter.connect()
            with self.assertRaises(CadCallUnknown):
                adapter.close()
            self.assertTrue(adapter.client.unknown)
            with self.assertRaises(CadCallUnknown):
                adapter.call_tool('fusion_mcp_execute',{'must':'not dispatch'})
            self.assertEqual(sum(call['method']=='tools/call' for call in calls),1)

    def test_real_probe_keeps_lease_after_fusion_readonly_transport_timeout(self):
        from fusion_pipeline.cad_probe import run_probe
        calls=[]
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            lease=root/'desktop.lock'
            with patch('fusion_pipeline.mcp.urllib.request.urlopen',side_effect=self.server(calls)), \
                 patch('fusion_pipeline.cad_probe.desktop_lease_path',return_value=lease):
                report=run_probe({'fusion_connection':{'url':'http://localhost:1234/mcp'}},root/'probe',stage='connect')
            self.assertEqual(report['cad_connection']['status'],'unknown')
            self.assertTrue(report['reconciliation_required'])
            self.assertTrue(lease.is_file())
            self.assertEqual(json.loads((root/'probe/probe.json').read_text(encoding='utf-8')),report)
            executed=[call for call in calls if call['method']=='tools/call']
            self.assertEqual(len(executed),1)
            self.assertTrue(executed[0]['params']['arguments']['object']['readOnly'])

    def test_confirmed_transport_can_close_without_cad_rpc(self):
        calls=[]
        with patch('fusion_pipeline.mcp.urllib.request.urlopen',side_effect=self.server(calls,timeout_on_execute=False)):
            client=FusionMCP({'mcp':{'autodeskfusion':{'url':'http://localhost:1234/mcp'}}})
            client.execute('readonly diagnostic',readonly=True)
            count=len(calls)
            client.close()
            self.assertFalse(client.unknown)
            self.assertEqual(len(calls),count)
            with self.assertRaises(RuntimeError):
                client.execute('closed client',readonly=True)
            self.assertEqual(len(calls),count)

    def test_audit_write_failure_after_dispatch_does_not_erase_unknown(self):
        calls=[]
        def audit(event):
            if event['event']=='mcp_request_unknown':
                raise OSError('audit unavailable')
        with patch('fusion_pipeline.mcp.urllib.request.urlopen',side_effect=self.server(calls)):
            client=FusionMCP({'mcp':{'autodeskfusion':{'url':'http://localhost:1234/mcp'}}},audit=audit)
            with self.assertRaises(CadCallUnknown):
                client.execute('readonly diagnostic',readonly=True)
            self.assertTrue(client.unknown)


if __name__=='__main__':
    unittest.main()

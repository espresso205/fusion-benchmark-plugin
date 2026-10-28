"""Observable fake CLI processes call the production stdio Gateway.

These tests exercise startup/transport/evidence only. They do not establish
vendor model, reasoning, vision, authentication or CAD compatibility.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from fusion_pipeline import runner
from fusion_pipeline.service import ManagementService
from src.io_utils import sha


FAKE_CLI = r'''
import json, os, pathlib, subprocess, sys
provider, args = sys.argv[1], sys.argv[2:]
if '--version' in args:
    print('observable-fixture-1'); raise SystemExit(0)
if '--help' in args:
    print('--format --model --ignore-user-config --ephemeral --json --strict-mcp-config --session-id --tools')
    raise SystemExit(0)
root = pathlib.Path(__file__).parent
record = root / (provider + '-capture.json')
mode_path = root / (provider + '-mode.txt')
mode = mode_path.read_text('utf-8') if mode_path.exists() else 'normal'
if provider == 'opencode' and args[:1] == ['export']:
    saved = json.loads(record.read_text('utf-8'))
    print(json.dumps({'messages': [
      {'info': {'role': 'user', 'sessionID': 'fixture-session', 'model': {'providerID': 'fixture', 'modelID': 'model'}},
       'parts': [{'type': 'text', 'text': saved['prompt']}]},
      {'info': {'role': 'assistant', 'sessionID': 'fixture-session', 'providerID': 'fixture',
                'modelID': 'model', 'finish': 'stop', 'time': {'created': 1, 'completed': 2}},
       'parts': [{'type': 'text', 'text': 'SMOKE_OK'}]}]}))
    raise SystemExit(0)
prompt = sys.stdin.read()
if provider == 'claude':
    mcps = json.loads(pathlib.Path(args[args.index('--mcp-config') + 1]).read_text('utf-8'))['mcpServers']
    command = [mcps['benchmark']['command'], *mcps['benchmark']['args']]
elif provider == 'codex':
    settings = [args[i+1] for i, value in enumerate(args[:-1]) if value == '-c']
    values = dict(value.split('=', 1) for value in settings)
    command = [json.loads(values['mcp_servers.benchmark.command']), *json.loads(values['mcp_servers.benchmark.args'])]
else:
    overlay = json.loads(os.environ['OPENCODE_CONFIG_CONTENT'])
    command = overlay['mcp']['benchmark']['command']
worker = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding='utf-8')
def request(identifier, method, params=None):
    payload = {'jsonrpc': '2.0', 'id': identifier, 'method': method}
    if params is not None: payload['params'] = params
    worker.stdin.write(json.dumps(payload) + '\n'); worker.stdin.flush()
    line = worker.stdout.readline()
    if not line: raise RuntimeError('gateway returned EOF: ' + worker.stderr.read())
    reply = json.loads(line)
    if 'error' in reply: raise RuntimeError(str(reply))
    return reply['result']
initial = request(1, 'initialize', {'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'observable-fixture','version':'1'}})
surface = request(2, 'tools/list')
contract = request(3, 'tools/call', {'name': 'get_contract', 'arguments': {}})
skills = request(4, 'tools/call', {'name': 'list_skills', 'arguments': {}})
skill = request(5, 'tools/call', {'name': 'read_skill', 'arguments': {'id': 'guide'}})
denied = request(6, 'tools/call', {'name': 'read_input', 'arguments': {'name': 'cad_skill'}})
auxiliary = request(7, 'tools/call', {'name': 'aux__engineering_lookup__lookup_public_note', 'arguments': {'query':'steel'}})
worker.stdin.close()
worker.wait(timeout=15)
if worker.returncode: raise RuntimeError(worker.stderr.read())
record.write_text(json.dumps({'provider':provider,'argv':args,'prompt':prompt,'cwd':os.getcwd(),
    'environment_names': sorted(os.environ), 'command':command, 'surface':surface,
    'contract':contract,'skill':skill,'denied':denied,'auxiliary':auxiliary}), encoding='utf-8')
session = 'fixture-session'
reason_value = 'low' if mode == 'wrong_reasoning' else 'high'
if provider == 'claude':
    print(json.dumps({'type':'system','subtype':'init','session_id':session,
                      'model':'wrong-model' if mode == 'wrong_model' else 'fixture/model',
                      'reasoning':{'control':'effort','value':reason_value}}))
    print(json.dumps({'type':'result','subtype':'success','session_id':session,'result':'SMOKE_OK'}))
elif provider == 'codex':
    print(json.dumps({'type':'session.created','thread_id':session,'reasoning':{'control':'effort','value':reason_value}}))
    print(json.dumps({'type':'thread.started','thread_id':session}))
    print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'SMOKE_OK'}}))
    print(json.dumps({'type':'turn.completed','thread_id':session}))
else:
    print(json.dumps({'type':'session.created','sessionID':session,'reasoning':{'control':'variant','value':reason_value}}))
    print(json.dumps({'type':'step_finish','sessionID':session,'part':{'reason':'stop'}}))
'''


class CandidateLaunchIntegrationTests(unittest.TestCase):
    def test_one_management_service_isolates_three_profiles_and_observes_real_gateway_reads(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            script = root / 'observable_cli.py'
            script.write_text(FAKE_CLI, encoding='utf-8')
            # Hidden cmd.exe launches use the system code page independently of
            # Python UTF-8 mode. Keep batch bytes ASCII and expand Unicode paths
            # from the process environment so both launch modes use exact paths.
            self.enterContext(patch.dict(os.environ, {
                'CANDIDATE_FIXTURE_PYTHON':sys.executable, 'CANDIDATE_FIXTURE_SCRIPT':str(script)}))
            skill = root / 'SKILL.md'
            skill.write_text('SELECTED_OPTIONAL_GUIDANCE', encoding='utf-8')
            registry = root / 'registry.json'
            auxiliary_fixture = Path(__file__).parent/'fixtures/auxiliary_mcp_server.py'
            registry.write_text(json.dumps({'schema':'candidate-capability-registry/1',
                'skills':{'guide':{'path':str(skill),'version':'1','resources':[],
                    'cad_backends':['fusion'],'requires_mcp':[]}}, 'mcp_servers':{'engineering_lookup':{
                    'kind':'stdio','command':sys.executable,
                    'args':['-B',str(auxiliary_fixture.resolve()),str(root/'auxiliary-sentinel.jsonl'),'normal'],
                    'source_files':[str(auxiliary_fixture.resolve())], 'env_allowlist':[],
                    'allowed_tools':['lookup_public_note'], 'timeout_s':3,'max_response_bytes':4096,
                    'side_effect_policy':'read_only'}}}))
            profiles, support = {}, {}
            for provider in ('claude', 'codex', 'opencode'):
                binary = root / (provider + ('.cmd' if os.name == 'nt' else '-cli'))
                if os.name == 'nt':
                    binary.write_text('@echo off\n"%CANDIDATE_FIXTURE_PYTHON%" "%CANDIDATE_FIXTURE_SCRIPT%" '
                                      + provider + ' %*\n', encoding='ascii')
                else:
                    binary.write_text('#!/bin/sh\nexec "' + sys.executable + '" "' + str(script) + '" ' + provider + ' "$@"\n')
                    binary.chmod(0o755)
                profiles[provider] = {'provider':provider,'model':'fixture/model','cli_executable':str(binary),
                    'reasoning':{'mode':'explicit','control':'variant' if provider == 'opencode' else 'effort','value':'high'},
                    'strict_tool_isolation':provider != 'codex'}
                support[provider] = {'provider':provider,'model':'fixture/model','cli_version':'observable-fixture-1',
                    'status':'verified','controls':{'variant' if provider == 'opencode' else 'effort':['high']},
                    'resolved_executable':str(binary.resolve()),'executable_sha256':sha(binary),
                    'evidence_sha256':'f'*64, 'support_source':'synthetic_observable_cli_fixture'}
                if provider == 'opencode':
                    support[provider]['variant_options'] = {'high':{'reasoningEffort':'high'}}
                if provider == 'codex':
                    profiles[provider]['native_tools_authorization'] = {'source':'fixture','scope':'synthetic test only'}
            config = {name:str(root/name) for name in ('questions_root','private_cases','runs_root','private_runs_root')}
            config.update(python=sys.executable, provider='opencode', model='SENTINEL_WRONG_MODEL',
                execution_backend='unified_gateway', case_ids=['Q01'], candidate_profiles=profiles,
                candidate_reasoning_support=support,
                capability_registry=str(registry), fusion_connection={'url':'http://127.0.0.1:9/mcp'},
                candidate_capabilities={'schema':'candidate-capabilities/1','study_kind':'negative_control_no_cad',
                    'skill_delivery':'gateway_read','exposure_mode':'gateway_proxy','skills':['guide'],
                    'mcp_servers':['engineering_lookup']})
            path = root/'base.json'
            path.write_text(json.dumps(config), encoding='utf-8')
            service = ManagementService(path)
            jobs = []
            # Only the coordinator spawn is intercepted. Every fake candidate and
            # its production Gateway are actual child processes below.
            with patch('fusion_pipeline.service.subprocess.Popen'):
                for provider in profiles:
                    started = service.call('start_run', {'candidate_profile':provider})
                    jobs.append((provider, root/'runs_root/.service'/started['run_id']/'config.json'))
            for provider, job in jobs:
                with self.subTest(provider=provider), patch.object(runner, 'source_hashes', return_value={'fixture':'a'*64}):
                    trial = runner.Trial(job)
                    trial.probe = lambda _: self.fail('negative-control launch contacted CAD')
                    trial.smoke()
                    capture = json.loads((root/(provider+'-capture.json')).read_text('utf-8'))
                    self.assertEqual(capture['provider'], provider)
                    self.assertIn('fixture/model', capture['argv'])
                    self.assertNotIn('SENTINEL_WRONG_MODEL', capture['prompt'])
                    self.assertTrue(capture['denied']['isError'])
                    names = [entry['name'] for entry in capture['surface']['tools']]
                    self.assertIn('read_skill', names)
                    self.assertNotIn('cad_call', names)
                    self.assertNotIn('cad_tools', names)
                    self.assertIn('aux__engineering_lookup__lookup_public_note', names)
                    self.assertIn('Public note: steel', json.dumps(capture['auxiliary']))
                    self.assertIn('SELECTED_OPTIONAL_GUIDANCE', json.dumps(capture['skill']))
                    selected = trial.state['candidate_selection']
                    self.assertEqual(selected['candidate_profile_id'], provider)
                    self.assertEqual(selected['exposure_verification_status'], 'tools_list_recorded')
                    self.assertEqual(selected['actual_skill_reads'][0]['id'], 'guide')
                    self.assertEqual(selected['per_server_tool_calls']['engineering_lookup'], 1)
                    self.assertTrue(selected['cleanup_verified'])
                    self.assertEqual(selected['observed_reasoning']['value'], 'high')
                    self.assertEqual(selected['reasoning_verification_status'], 'observed')
                    if provider == 'codex':
                        self.assertIn('model_reasoning_effort="high"', capture['argv'])
                    else:
                        flag = '--variant' if provider == 'opencode' else '--effort'
                        self.assertEqual(capture['argv'][capture['argv'].index(flag)+1], 'high')
                    self.assertEqual(trial._processes, [])
                    for mode in (('wrong_reasoning', 'wrong_model') if provider == 'claude' else ('wrong_reasoning',)):
                        (root/(provider+'-mode.txt')).write_text(mode, encoding='utf-8')
                        failed = runner.Trial(job)
                        failed.probe = lambda _: self.fail('failed candidate contacted CAD')
                        with self.assertRaisesRegex(RuntimeError, 'smoke failed'):
                            failed.smoke()
                        outcome = json.loads((failed.run/'smoke-result.json').read_text('utf-8'))
                        self.assertEqual(outcome['guard_classification'], 'session_unconfirmed')
                        self.assertEqual(failed._processes, [])
                        evidence = json.loads((root/(provider+'-capture.json')).read_text('utf-8'))
                        self.assertEqual(evidence['provider'], provider)
                        self.assertEqual(evidence['argv'][evidence['argv'].index('--model')+1], 'fixture/model')


if __name__ == '__main__':
    unittest.main()

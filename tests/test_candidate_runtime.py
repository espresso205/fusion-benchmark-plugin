"""Runtime identity and explicit-capability integration; no paid model or CAD."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from fusion_pipeline import runner
from fusion_pipeline.candidate_config import resolve_candidate_configuration
from src.io_utils import sha


class CandidateRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.skill = self.root / 'skill.md'
        self.skill.write_text('OPTIONAL_GUIDANCE_SENTINEL', encoding='utf-8')
        self.registry = self.root / 'registry.json'
        self.registry.write_text(json.dumps({'schema': 'candidate-capability-registry/1',
            'skills': {'guide': {'path': str(self.skill), 'version': '1', 'resources': [],
                                 'cad_backends': ['fusion'], 'requires_mcp': []}},
            'mcp_servers': {'cad_native': {'kind': 'builtin_cad_adapter'}}}), encoding='utf-8')
        self.config = dict(provider='claude', model='requested', cli_executable=sys.executable,
            reasoning={'mode': 'cli_default'}, capability_registry=str(self.registry),
            candidate_capabilities={'schema': 'candidate-capabilities/1', 'study_kind': 'capability_ablation',
                'skill_delivery': 'gateway_read', 'exposure_mode': 'gateway_proxy',
                'skills': [], 'mcp_servers': ['cad_native']})
        self.doctor = {'available': True, 'required_flags_present': True,
                       'executable': sys.executable, 'version': 'fixture-cli 1'}

    def freeze(self, config=None):
        from fusion_pipeline.candidate_runtime import freeze_attempt, observe_launch
        config = resolve_candidate_configuration(config or self.config)
        launch = observe_launch(config, self.doctor)
        contract = dict(run_id='run', case_id='Q01', attempt_id='attempt-1', cad_backend='fusion',
                        workspace=str(self.root / 'worker'), deadline=12345, max_tool_calls=200)
        result, prompt = freeze_attempt(config, self.root / 'attempt', contract=contract,
            observed_launch=launch, case={'case_id': 'Q01'}, input_manifest={'task': {'sha256': 'a'*64}},
            source_hashes={'runner.py': 'b'*64})
        return config, result, prompt

    def test_empty_skill_freeze_uses_common_protocol_without_legacy_alias(self):
        _, evidence, prompt = self.freeze()
        self.assertEqual(evidence['ordered_enabled_skill_ids'], [])
        self.assertNotIn('OPTIONAL_GUIDANCE_SENTINEL', prompt)
        self.assertNotIn('read_input(name=cad_skill)', prompt)
        self.assertEqual(evidence['cli_version'], 'fixture-cli 1')
        self.assertEqual(evidence['executable_sha256'], sha(sys.executable))
        self.assertEqual(evidence['prompt_sha256'], __import__('hashlib').sha256(prompt.encode()).hexdigest())
        self.assertIsNone(evidence['observed_model_evidence'])
        frozen = json.loads(Path(evidence['capability_manifest']).read_text('utf-8'))
        for key in ('candidate_profile_sha256', 'prompt_sha256', 'common_protocol_sha256',
                    'input_manifest_sha256', 'resolved_candidate_launch_sha256'):
            self.assertEqual(frozen[key], evidence[key])

    def test_verify_detects_request_profile_executable_and_source_drift(self):
        from fusion_pipeline.candidate_runtime import verify_attempt
        config = copy.deepcopy(self.config)
        config['candidate_capabilities']['skills'] = ['guide']
        config, evidence, _ = self.freeze(config)
        verify_attempt(evidence, config, source_hashes={'runner.py': 'b'*64})
        changed = copy.deepcopy(config)
        changed['model'] = 'other'
        with self.assertRaisesRegex(ValueError, 'candidate_configuration_changed'):
            verify_attempt(evidence, changed, source_hashes={'runner.py': 'b'*64})
        with self.assertRaisesRegex(ValueError, 'source_changed'):
            verify_attempt(evidence, config, source_hashes={'runner.py': 'c'*64})
        self.skill.write_text('changed', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'source_hash_mismatch'):
            verify_attempt(evidence, config, source_hashes={'runner.py': 'b'*64})

    def test_explicit_reasoning_requires_observed_binary_version_binding(self):
        from fusion_pipeline.candidate_runtime import observe_launch
        config = copy.deepcopy(self.config)
        config['reasoning'] = {'mode': 'explicit', 'control': 'effort', 'value': 'high'}
        config['reasoning_support'] = {'status': 'verified', 'provider': 'claude', 'model': 'requested',
            'cli_version': 'fixture-cli 1', 'evidence_sha256': 'a'*64, 'controls': {'effort': ['high']}}
        config = resolve_candidate_configuration(config)
        with self.assertRaisesRegex(ValueError, 'reasoning_support_unverified'):
            observe_launch(config, self.doctor)
        config['reasoning_support'].update(resolved_executable=str(Path(sys.executable).resolve()),
                                           executable_sha256=sha(sys.executable))
        self.assertEqual(observe_launch(config, self.doctor)['cli_version'], 'fixture-cli 1')
        with self.assertRaisesRegex(ValueError, 'reasoning_support_unverified'):
            observe_launch(config, dict(self.doctor, version='fixture-cli 2'))

    def test_trial_resolves_profile_before_validation_and_omits_legacy_skill_defaults(self):
        config = copy.deepcopy(self.config)
        config.update({name: str(self.root / name) for name in
            ('python', 'questions_root', 'private_cases', 'runs_root', 'private_runs_root')})
        config.update(execution_backend='unified_gateway', case_ids=['Q01'],
                      fusion_connection={'url': 'http://127.0.0.1:9000/mcp'})
        config.update(provider='codex', strict_tool_isolation=True, candidate_profile='chosen',
            candidate_profiles={'chosen': {'provider': 'claude', 'model': 'profile-model',
                'cli_executable': sys.executable, 'reasoning': {'mode': 'cli_default'},
                'strict_tool_isolation': True}})
        path = self.root / 'config.json'
        path.write_text(json.dumps(config))
        trial = runner.Trial(path)
        self.assertEqual(trial.provider, 'claude')
        self.assertEqual(trial.state['model'], 'profile-model')
        self.assertNotIn('cad_skill', trial.config)
        self.assertNotIn('fusion_skill', trial.config)
        self.assertEqual(trial.state['candidate_selection']['candidate_profile_id'], 'chosen')

    def test_negative_smoke_does_not_probe_cad_or_require_cad_tools(self):
        from fusion_pipeline.runner import Trial
        trial = Trial.__new__(Trial)
        trial.run, trial.unified_gateway, trial.cad_backend = self.root, True, 'fusion'
        trial.config = copy.deepcopy(self.config)
        trial.config['candidate_capabilities'].update(study_kind='negative_control_no_cad', mcp_servers=[])
        trial.probe = lambda _: self.fail('negative control must never probe CAD')
        def launch(workspace, audit, prompt, **kwargs):
            self.assertNotIn('cad_tools', prompt)
            self.assertNotIn('cad_call', prompt)
            audit.mkdir(parents=True)
            (audit / 'capability-evidence.json').write_text(json.dumps({
                'exposure_verification_status': 'tools_list_recorded',
                'observed_tool_surface': [{'name': 'get_contract'}, {'name': 'write_report'}]}))
            return {'returncode': 0, 'timed_out': False, 'guard_classification': 'clear',
                    'cad_tool_calls': 0, 'guard': {'cad_calls': 0, 'submitted': False}}
        trial.launch = launch
        trial.smoke()

    def test_planned_missing_and_partial_measurement_keep_scores_unknown(self):
        from fusion_pipeline.candidate_runtime import finalize_study_run
        template = {'schema': 'candidate-capability-run/1', 'outcome': {}, 'usage': {}}
        missing = {'state': 'candidate_missing', 'negative_control_no_cad': True,
                   'failure_stage': 'submission', 'failure_reason': 'negative_control_no_cad'}
        result = finalize_study_run(template, missing)
        self.assertFalse(result['outcome']['native_submitted'])
        self.assertIsNone(result['outcome']['formal_score'])
        self.assertIsNone(result['outcome']['primary_coverage'])
        self.assertEqual(result['outcome']['failure_category'], 'negative_control_no_cad')
        partial = {'state': 'evaluation_complete', 'evaluation_complete': True, 'native_document': {},
                   'trusted_step': 'step', 'measurement_weight_coverage': .7, 'final_score': 40,
                   'score_kind': 'policy_lower_bound'}
        result = finalize_study_run(template, partial)
        self.assertFalse(result['outcome']['primary_complete'])
        self.assertIsNone(result['outcome']['formal_score'])
        self.assertEqual(partial['final_score'], 40)
        self.assertEqual(result['outcome']['policy_score_or_lower_bound'], 40)

    def test_profile_with_legacy_capabilities_preserves_original_prompt_and_contract(self):
        from fusion_pipeline.candidate_runtime import freeze_legacy_attempt, observe_launch, verify_attempt
        config = dict(provider='claude', model='requested', cli_executable=sys.executable,
                      cad_skill=str(self.skill))
        config = resolve_candidate_configuration(config)
        contract = dict(run_id='run', case_id='Q01', attempt_id='attempt-1', max_tool_calls=200, deadline=12345)
        evidence = freeze_legacy_attempt(config, self.root/'legacy', contract=contract,
            observed_launch=observe_launch(config, self.doctor), prompt='ORIGINAL LEGACY PROMPT',
            input_manifest={}, source_hashes={})
        self.assertEqual(evidence['selection_origin'], 'legacy_default')
        self.assertNotIn('capability_manifest', evidence)
        self.assertEqual(evidence['requested_model'], 'requested')
        verify_attempt(evidence, config, source_hashes={}, prompt='ORIGINAL LEGACY PROMPT', input_manifest={})

    def test_trial_plan_uses_raw_hash_and_checks_inputs_from_other_planned_cases(self):
        from tests.test_capability_study import CapabilityStudyTests
        fixture = CapabilityStudyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with patch.object(runner, 'source_hashes', return_value={'fixture':'a'*64}):
            spec = fixture.spec(profiles={'claude': fixture.profile()},
                groups=[{'id':'A', 'skills':[], 'mcp_servers':['cad_native']}], repetitions=1)
            spec['base_config'].update({name:str(fixture.root/name) for name in ('runs_root','private_runs_root')})
            spec['base_config'].update(python=sys.executable, execution_backend='unified_gateway',
                                      fusion_connection={'url':'http://127.0.0.1:9/mcp'})
            spec['profile_evidence'] = {'claude':{'configuration_valid':True,'cli_available':True,
                'required_flags_present':True,'cli_version':'fixture-v1','resolved_executable':str(fixture.binary),
                'executable_sha256':sha(fixture.binary)}}
            spec['admission'] = {'status':'passed','cases':{item['case_id']:{
                **{key:item[key] for key in ('input_manifest_sha256','gt_sha256','annotations_sha256')},
                'drawing_readability':{'status':'passed','evidence_sha256':'1'*64},
                'gt_primary_measurability':{'status':'passed','evidence_sha256':'2'*64}}
                for item in spec['common']['case_identities']}}
            plan = fixture.build(spec)
            cell = next(item for item in plan['cells'] if item['case_id']=='Q01')
            path = fixture.root/'plan'/cell['config_path']
            trial = runner.Trial(path)
            self.assertEqual(trial.raw_config, json.loads(path.read_text('utf-8')))
            self.assertEqual(trial.state['cases'][0]['capability_study_run']['cell_id'], cell['cell_id'])
            task = fixture.questions/'Q02/TASK.md'
            task.write_text('changed another planned case', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'public input.*identity mismatch|planned_common_identity_mismatch'):
                runner.Trial(path)

    def test_planned_cad_version_requires_independent_observation(self):
        trial = runner.Trial.__new__(runner.Trial)
        trial.config = {}
        trial.cad_backend = 'fusion'
        trial.state = {'cases':[{'capability_study_run':{'frozen_identity':{
            'cad_identity':{'backend':'fusion','version':'expected-version'}}}}]}
        with self.assertRaisesRegex(ValueError, 'study_cad_identity_unverified'):
            trial.verify_study_cad_identity()
        trial.state['cad_identity'] = {'version':'wrong-version'}
        with self.assertRaisesRegex(ValueError, 'planned_cad_identity_mismatch'):
            trial.verify_study_cad_identity()
        trial.state['cad_identity'] = {'version':'expected-version'}
        trial.verify_study_cad_identity()

    def test_frozen_launch_records_resolved_reasoning_and_support_before_dispatch(self):
        config = copy.deepcopy(self.config)
        config['reasoning'] = {'mode':'explicit', 'control':'effort', 'value':'high'}
        config['reasoning_support'] = {'status':'verified', 'provider':'claude', 'model':'requested',
            'cli_version':'fixture-cli 1', 'evidence_sha256':'a'*64, 'controls':{'effort':['high']},
            'resolved_executable':str(Path(sys.executable).resolve()), 'executable_sha256':sha(sys.executable)}
        _, runtime, _ = self.freeze(config)
        frozen = json.loads(Path(runtime['capability_manifest']).read_text('utf-8'))
        self.assertEqual(frozen['resolved_reasoning'], config['reasoning'])
        self.assertEqual(frozen['emitted_reasoning_parameters'], ['--effort','high'])
        self.assertEqual(frozen['reasoning_support_evidence']['cli_version'], 'fixture-cli 1')

    def test_unattempted_planned_cell_records_pipeline_failure_without_zero_score(self):
        from fusion_pipeline.candidate_runtime import finalize_study_run
        result = finalize_study_run({'outcome':{},'usage':{}}, {'state':'queued'},
            pipeline={'state':'blocked','failure_stage':'preparation','error':'ValueError: candidate_cli_unverified'})
        self.assertEqual(result['outcome']['failure_stage'], 'preparation')
        self.assertEqual(result['outcome']['failure_category'], 'candidate_not_dispatched')
        self.assertIsNone(result['outcome']['formal_score'])
        negative = finalize_study_run({'outcome':{},'usage':{}},
            {'state':'queued','negative_control_no_cad':True},
            pipeline={'state':'blocked','failure_stage':'preparation'})
        self.assertTrue(negative.get('negative_control_no_cad'))

    def test_auxiliary_only_negative_control_skips_desktop_and_scoring(self):
        trial = runner.Trial.__new__(runner.Trial)
        trial.run = self.root/'run'
        trial.run.mkdir()
        trial.private = self.root/'private'
        trial.unified_gateway = True
        trial.config = {'runs_root':str(self.root), 'candidate_capabilities':{
            'study_kind':'negative_control_no_cad', 'mcp_servers':['engineering_lookup']}}
        trial.state = {'cases':[{'case_id':'Q01', 'state':'queued'}]}
        trial.prepare = lambda: None
        trial.smoke = lambda: None
        trial.model = lambda row: row.update(state='candidate_missing', negative_control_no_cad=True)
        trial.verify_source = lambda: None
        trial.save = lambda: None
        trial.report = lambda: None
        trial.cleanup_processes = lambda: []
        trial.evaluate = lambda: self.fail('negative control cannot enter scoring')
        trial.export = lambda _: self.fail('negative control cannot export CAD')
        with patch.object(runner.tempfile, 'gettempdir', side_effect=AssertionError('must not acquire CAD desktop lease')):
            self.assertEqual(trial.run_all(), 2)
        self.assertEqual(trial.state['state'], 'completed_with_missing_candidates')
        self.assertFalse(trial.state['batch_complete'])

    def test_launch_exception_persists_private_failure_and_cleanup_evidence(self):
        config = copy.deepcopy(self.config)
        config.update({name:str(self.root/name) for name in
            ('questions_root','private_cases','runs_root','private_runs_root')})
        config.update(python=sys.executable, execution_backend='unified_gateway', case_ids=['Q01'])
        config['candidate_capabilities'].update(study_kind='negative_control_no_cad', mcp_servers=[])
        path = self.root/'config.json'
        path.write_text(json.dumps(config), encoding='utf-8')
        trial = runner.Trial(path)
        with patch('fusion_pipeline.adapters.probe', return_value=self.doctor), \
                patch('fusion_pipeline.adapters.launch_fresh', side_effect=RuntimeError('authentication_rejected')):
            with self.assertRaisesRegex(RuntimeError, 'authentication_rejected'):
                trial.smoke()
        attempt = next((trial.private/'attempts').glob('*/candidate-attempt.json'))
        failure = json.loads(attempt.read_text('utf-8'))['failure_and_cleanup_evidence']
        self.assertEqual(failure['classification'], 'candidate_launch_failed')
        self.assertEqual(failure['error_type'], 'RuntimeError')
        self.assertEqual(failure['owned_processes_remaining'], 0)

    def test_snapshot_and_executable_byte_drift_fail_before_reuse(self):
        from fusion_pipeline.candidate_runtime import verify_attempt
        binary = self.root/'fixture.exe'
        binary.write_bytes(b'synthetic executable identity')
        config = copy.deepcopy(self.config)
        config['cli_executable'] = str(binary)
        self.doctor['executable'] = str(binary)
        config, runtime, prompt = self.freeze(config)
        profile = Path(runtime['profile_manifest'])
        original = profile.read_bytes()
        profile.write_bytes(original + b' ')
        with self.assertRaisesRegex(ValueError, 'snapshot_or_executable_changed'):
            verify_attempt(runtime, config, source_hashes={'runner.py':'b'*64})
        profile.write_bytes(original)
        with self.assertRaisesRegex(ValueError, 'prompt_changed'):
            verify_attempt(runtime, config, source_hashes={'runner.py':'b'*64}, prompt=prompt+'injected')
        binary.write_bytes(b'changed executable identity')
        with self.assertRaisesRegex(ValueError, 'snapshot_or_executable_changed'):
            verify_attempt(runtime, config, source_hashes={'runner.py':'b'*64})

    def test_runtime_complete_score_requires_finite_numeric_coverage_and_score(self):
        from fusion_pipeline.candidate_runtime import finalize_study_run
        row = {'state':'evaluation_complete', 'evaluation_complete':True, 'native_document':{},
               'trusted_step':'step', 'measurement_weight_coverage':1, 'final_score':80}
        template = {'outcome':{},'usage':{}}
        for coverage in (None, True, False, .5, float('nan'), float('inf')):
            with self.subTest(coverage=coverage):
                result = finalize_study_run(template, dict(row, measurement_weight_coverage=coverage))
                self.assertFalse(result['outcome']['primary_complete'])
                self.assertIsNone(result['outcome']['formal_score'])
        for score in (float('nan'), float('inf'), True):
            with self.subTest(score=score):
                result = finalize_study_run(template, dict(row, final_score=score))
                self.assertFalse(result['outcome']['primary_complete'])
                self.assertIsNone(result['outcome']['formal_score'])
                self.assertIsNone(result['outcome']['policy_score_or_lower_bound'])
        self.assertTrue(finalize_study_run(template, row)['outcome']['primary_complete'])

    def test_failed_negative_candidate_preserves_identity_without_inventing_cad_uncertainty(self):
        from fusion_pipeline.candidate_runtime import finalize_study_run
        base = {'returncode':1, 'timed_out':False, 'cancelled':False,
            'guard_classification':'submission_unconfirmed',
            'guard':{'cad_calls':0,'pending_cad':0,'submitted':False},
            'process_cleanup':{'verified_stopped':True,'remaining_processes':0}}
        variants = [base, dict(base,timed_out=True), dict(base,cancelled=True,guard_classification='cancelled'),
                    dict(base,guard_classification='auxiliary_service_failure'),
                    dict(base,guard_classification='candidate_process_tree_unconfirmed',
                         process_cleanup={'verified_stopped':False,'remaining_processes':1})]
        for result in variants:
            with self.subTest(result=result):
                trial = runner.Trial.__new__(runner.Trial)
                trial.state, trial.run, trial._processes = {}, self.root, []
                trial.save = lambda: None
                trial.verify_public_inputs = lambda _: None
                trial.launch = lambda *a, **k: result
                row = {'workspace':str(self.root),'case_id':'Q01'}
                with self.assertRaisesRegex(RuntimeError, 'negative control execution unconfirmed'):
                    trial.model_negative_control(row)
                self.assertEqual(row['state'], 'reconciliation_required')
                self.assertTrue(row.get('negative_control_no_cad'))
                outcome = finalize_study_run({'outcome':{},'usage':{}},row)['outcome']
                self.assertFalse(outcome['cad_unknown'])
                self.assertEqual(outcome['failure_category'], 'negative_control_no_cad')
                self.assertEqual(row['execution']['guard_classification'], result['guard_classification'])

    def test_cad_uncertainty_requires_unknown_guard_or_pending_native_evidence(self):
        from fusion_pipeline.candidate_runtime import finalize_study_run
        template = {'outcome':{},'usage':{}}
        for row in ({'state':'reconciliation_required'},
                    {'state':'reconciliation_required','failure_reason':'cancelled'}):
            self.assertFalse(finalize_study_run(template,row)['outcome']['cad_unknown'])
        for row in ({'failure_reason':'cad_task_unknown'}, {'failure_reason':'fusion_task_unknown'},
                    {'cad_execution_evidence':{'pending_cad':1}},
                    {'cad_execution_evidence':{'pending_fusion':{'request_id':'pending'}}}):
            self.assertTrue(finalize_study_run(template,row)['outcome']['cad_unknown'])


if __name__ == '__main__':
    unittest.main()

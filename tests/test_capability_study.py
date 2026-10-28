"""Synthetic offline study tests: no CLI, model or CAD sessions."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch


class CapabilityStudyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.binary = self.root / 'fixture.exe'
        self.binary.write_bytes(b'not executable; synthetic identity only')
        self.skill = self.root / 'guide.md'
        self.skill.write_text('Synthetic optional strategy', encoding='utf-8')
        self.tool = self.root / 'lookup.py'
        self.tool.write_text('# synthetic service; never launched', encoding='utf-8')
        self.registry = self.root / 'registry.json'
        self.registry.write_text(json.dumps({
            'schema': 'candidate-capability-registry/1',
            'skills': {'guide': {'path': str(self.skill), 'version': '1', 'resources': [],
                                'cad_backends': ['fusion'], 'requires_mcp': []}},
            'mcp_servers': {'cad_native': {'kind': 'builtin_cad_adapter'},
                'lookup': {'kind': 'stdio', 'command': str(self.binary), 'args': [],
                           'source_files': [str(self.tool)], 'env_allowlist': [],
                           'allowed_tools': ['lookup'], 'timeout_s': 10,
                           'max_response_bytes': 1024, 'side_effect_policy': 'read_only'}}}), encoding='utf-8')
        self.questions = self.root / 'questions'
        rows = []
        for case_id in ('Q01', 'Q02'):
            public = self.questions / case_id
            public.mkdir(parents=True)
            (public / 'TASK.md').write_text('Synthetic task ' + case_id, encoding='utf-8')
            (public / 'drawing.dxf').write_text('0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n', encoding='utf-8')
            gt = self.root / (case_id + '.step')
            gt.write_bytes(b'Synthetic STEP identity only: ' + case_id.encode())
            gt_hash = hashlib.sha256(gt.read_bytes()).hexdigest()
            annotation = self.root / (case_id + '.annotations.json')
            annotation.write_text(json.dumps({'gt_sha256': gt_hash}), encoding='utf-8')
            rows.append({'case': case_id, 'gt': str(gt), 'annotations': str(annotation),
                         'expected_hashes': {'gt': gt_hash, 'annotations': hashlib.sha256(annotation.read_bytes()).hexdigest()}})
        from fusion_pipeline.input_assets import legacy_manifest
        second = self.questions / 'Q02'
        (second / 'inputs.json').write_text(json.dumps(legacy_manifest('Q02', second)), encoding='utf-8')
        self.private_cases = self.root / 'private-cases.json'
        self.private_cases.write_text(json.dumps({'cases': rows}), encoding='utf-8')
        self.evaluator = self.root / 'evaluator'
        self.evaluator.mkdir()
        for name in ('evaluate.py', 'worker.py'):
            (self.evaluator / name).write_text('# synthetic evaluator source\n', encoding='utf-8')
        for name in ('weights.json', 'weights_v4.json'):
            (self.evaluator / name).write_text('{}\n', encoding='utf-8')

    def profile(self, provider='claude', reasoning=None):
        result = {'provider': provider, 'model': 'fixture-model', 'cli_executable': str(self.binary),
                  'strict_tool_isolation': provider != 'codex',
                  'reasoning': reasoning or {'mode': 'cli_default'}}
        if provider == 'codex':
            result['native_tools_authorization'] = {'source': 'synthetic', 'scope': 'test only'}
        return result

    def spec(self, *, profiles=None, groups=None, repetitions=2):
        profiles = profiles or {p: self.profile(p) for p in ('claude', 'codex', 'opencode')}
        spec = {'schema': 'candidate-capability-experiment/1', 'experiment_id': 'fixture',
                'candidate_profile_ids': list(profiles), 'candidate_profiles': profiles,
                'base_config': {'cad_backend': 'fusion', 'capability_registry': str(self.registry),
                                'modeling_timeout_s': 1800, 'questions_root': str(self.questions),
                                'private_cases': str(self.private_cases), 'evaluator_directory': str(self.evaluator),
                                'cad_identity': {'backend': 'fusion', 'version': 'fixture-only'}},
                'common': {'dataset_sha256': '1' * 64, 'gt_annotations_sha256': '2' * 64,
                           'input_manifest_sha256': '6' * 64,
                           'common_protocol_sha256': '3' * 64, 'budget_policy': {'seconds': 1800, 'requests': 200},
                           'plugin_source_sha256': '4' * 64, 'evaluator_weights_sha256': '5' * 64,
                           'cad_identity': {'backend': 'fusion', 'version': 'fixture-only'},
                           'coordinate_mode': 'geometry'},
                'case_ids': ['Q01', 'Q02'], 'repetitions': repetitions, 'schedule_seed': 71,
                'groups': groups or [
                    {'id': 'A', 'skills': [], 'mcp_servers': ['cad_native']},
                    {'id': 'B', 'skills': ['guide'], 'mcp_servers': ['cad_native']},
                    {'id': 'C', 'skills': [], 'mcp_servers': ['cad_native', 'lookup']},
                    {'id': 'D', 'skills': ['guide'], 'mcp_servers': ['cad_native', 'lookup']}],
                'retry_policy': 'retain_all_attempts_no_best_score_selection'}
        from fusion_pipeline.capability_study import capture_common_identity
        spec['common'] = capture_common_identity(spec['base_config'], spec['case_ids'])
        spec['admission'] = {'status': 'passed', 'cases': {
            row['case_id']: {key: row[key] for key in ('input_manifest_sha256', 'gt_sha256', 'annotations_sha256')} | {
                'drawing_readability': {'status': 'passed', 'evidence_sha256': 'a' * 64},
                'gt_primary_measurability': {'status': 'passed', 'evidence_sha256': 'b' * 64}}
            for row in spec['common']['case_identities']}}
        return spec

    def build(self, spec=None, name='plan'):
        from fusion_pipeline.capability_study import build_plan
        return build_plan(spec or self.spec(), self.root / name)

    def test_frozen_48_cells_balanced_serial_no_processes(self):
        from fusion_pipeline import capability_study as study
        before = self.spec()
        with patch('subprocess.Popen', side_effect=AssertionError('offline process')), \
                patch('subprocess.run', side_effect=AssertionError('offline process')):
            plan = self.build(before)
        self.assertEqual(before, self.spec())
        keys = {(c['candidate_profile_id'], c['group_id'], c['case_id'], c['repetition']) for c in plan['cells']}
        self.assertEqual(len(keys), 48)
        self.assertEqual(plan['execution_mode'], 'serial')
        second = self.build(name='second')
        self.assertEqual([c['cell_id'] for c in plan['cells']], [c['cell_id'] for c in second['cells']])
        for start in range(0, 48, 12):
            block = plan['cells'][start:start + 12]
            self.assertEqual(len({(c['case_id'], c['repetition']) for c in block}), 1)
            self.assertEqual(len({(c['candidate_profile_id'], c['group_id']) for c in block}), 12)
        cell = plan['cells'][0]
        config = json.loads((self.root / 'plan' / cell['config_path']).read_text(encoding='utf-8'))
        self.assertEqual(study.validate_plan_execution(self.root / 'plan/experiment-plan.json', cell['cell_id'], config), cell)
        self.assertTrue((self.root / 'plan' / plan['profiles']['claude']['snapshot_path']).is_file())
        with self.assertRaises(FileExistsError):
            self.build()

    def test_duplicate_and_invalid_design_rejected_before_output(self):
        from fusion_pipeline.capability_study import build_plan
        for field, value in [('candidate_profile_ids', ['claude', 'claude']), ('case_ids', ['Q01', 'Q01']),
                             ('repetitions', True), ('groups', [{'id': 'A', 'skills': [], 'mcp_servers': []}])]:
            spec = self.spec()
            spec[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                build_plan(spec, self.root / ('bad_' + field))
            self.assertFalse((self.root / ('bad_' + field)).exists())

    def test_execution_rejects_override_and_snapshot_or_source_drift(self):
        from fusion_pipeline.capability_study import validate_plan_execution
        plan = self.build()
        cell = plan['cells'][0]
        path = self.root / 'plan/experiment-plan.json'
        config = json.loads((path.parent / cell['config_path']).read_text(encoding='utf-8'))
        changed = copy.deepcopy(config)
        changed['reasoning'] = {'mode': 'explicit', 'control': 'effort', 'value': 'high'}
        with self.assertRaisesRegex(ValueError, 'planned_candidate_configuration_mismatch'):
            validate_plan_execution(path, cell['cell_id'], changed)
        self.skill.write_text('changed after freeze', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'capability_source_hash_mismatch|study_snapshot_mismatch'):
            validate_plan_execution(path, cell['cell_id'], config)

    def manifest(self, cell, *, submitted=True, complete=True, score=50, name=None):
        from fusion_pipeline.capability_study import run_manifest_template
        value = run_manifest_template(self.root / 'plan/experiment-plan.json', cell['cell_id'],
                                      'run_' + cell['cell_id'], 'attempt_1')
        value['outcome'].update(native_submitted=submitted, trusted_export=submitted,
                                primary_complete=complete and submitted,
                                formal_score=score if submitted and complete else None,
                                primary_coverage=1 if complete and submitted else None)
        path = self.root / (name or (cell['cell_id'] + '.json'))
        path.write_text(json.dumps(value), encoding='utf-8')
        return path

    def small_spec(self):
        return self.spec(profiles={'claude': self.profile()}, groups=[
            {'id': 'A', 'skills': [], 'mcp_servers': ['cad_native']},
            {'id': 'B', 'skills': ['guide'], 'mcp_servers': ['cad_native']}], repetitions=1)

    def test_analysis_retains_planned_denominator_nulls_and_only_complete_pairs(self):
        from fusion_pipeline.capability_study import analyze_plan
        plan = self.build(self.small_spec())
        paths = []
        for cell in plan['cells']:
            missing = cell['group_id'] == 'A' and cell['case_id'] == 'Q02'
            path = self.manifest(cell, submitted=not missing, score=40 if cell['group_id'] == 'A' else 60)
            value = json.loads(path.read_text(encoding='utf-8'))
            value['usage']['actual_skill_reads'] = 0
            value['usage']['cost'] = None
            path.write_text(json.dumps(value), encoding='utf-8')
            paths.append(path)
        with patch('subprocess.Popen', side_effect=AssertionError('offline analysis process')):
            result = analyze_plan(self.root / 'plan/experiment-plan.json', paths, self.root / 'analysis')
        strata = {r['group_id']: r for r in result['strata']}
        self.assertEqual(strata['A']['planned_count'], 2)
        self.assertEqual(strata['A']['native_submission_rate'], 0.5)
        self.assertEqual(strata['B']['native_submission_rate'], 1)
        self.assertIsNone(next(c for c in result['cells'] if c['group_id'] == 'A' and c['case_id'] == 'Q02')['outcome']['formal_score'])
        pair = result['within_profile_comparisons'][0]
        self.assertEqual(pair['contrast'], 'B-A')
        self.assertEqual(pair['paired_planned_count'], 2)
        self.assertEqual(pair['quality_pair_count'], 1)
        self.assertEqual(pair['mean_quality_difference'], 20)
        self.assertEqual(pair['unpaired'][0]['case_id'], 'Q02')
        self.assertEqual(strata['B']['skill_usage_status'], 'enabled_not_used')
        self.assertIsNone(strata['B']['usage_means']['cost'])
        for suffix in ('json', 'csv', 'md'):
            self.assertTrue((self.root / 'analysis' / ('capability-analysis.' + suffix)).is_file())
        with self.assertRaises(FileExistsError):
            analyze_plan(self.root / 'plan/experiment-plan.json', paths, self.root / 'analysis')

    def test_complete_score_requires_numeric_full_primary_coverage(self):
        from fusion_pipeline.capability_study import analyze_plan
        plan = self.build(self.small_spec())
        paths = [self.manifest(cell) for cell in plan['cells']]
        originals = [json.loads(path.read_text(encoding='utf-8')) for path in paths]
        for index, coverage in enumerate((0.5, None, True, False, 0, '1', float('nan'), float('inf'))):
            for path, original in zip(paths, originals):
                value = copy.deepcopy(original)
                value['outcome']['primary_coverage'] = coverage
                path.write_text(json.dumps(value), encoding='utf-8')
            with self.subTest(coverage=coverage):
                result = analyze_plan(self.root/'plan/experiment-plan.json', paths, self.root/('bad_coverage_'+str(index)))
                self.assertEqual(result['planned_cell_count'], 4)
                self.assertEqual(len(result['cells']), 4)
                self.assertEqual(len(result['rejected_runs']), 4)
                self.assertTrue(all(row['status']!='observed' for row in result['cells']))
                self.assertTrue(all(row['primary_complete_rate']==0 for row in result['strata']))
                self.assertEqual(result['within_profile_comparisons'][0]['quality_pair_count'], 0)
        for path, original in zip(paths, originals):
            path.write_text(json.dumps(original), encoding='utf-8')
        valid = analyze_plan(self.root/'plan/experiment-plan.json', paths, self.root/'valid_coverage')
        self.assertEqual(len(valid['rejected_runs']), 0)
        self.assertTrue(all(row['primary_complete_rate']==1 for row in valid['strata']))
        self.assertEqual(valid['within_profile_comparisons'][0]['quality_pair_count'], 2)

    def test_missing_metrics_aux_failure_and_rejected_identity_not_erased(self):
        from fusion_pipeline.capability_study import analyze_plan
        plan = self.build(self.small_spec())
        paths = []
        for cell in plan['cells']:
            if cell['group_id'] == 'A' and cell['case_id'] == 'Q02':
                continue
            path = self.manifest(cell, complete=cell['case_id'] != 'Q02')
            value = json.loads(path.read_text(encoding='utf-8'))
            if cell['group_id'] == 'B' and cell['case_id'] == 'Q02':
                value['outcome'].update(failure_category='auxiliary_service_failure', failure_stage='modeling',
                                        policy_lower_bound=12)
            if cell['group_id'] == 'B' and cell['case_id'] == 'Q01':
                value['frozen_identity']['plugin_source_sha256'] = '9' * 64
            path.write_text(json.dumps(value), encoding='utf-8')
            paths.append(path)
        result = analyze_plan(self.root / 'plan/experiment-plan.json', paths, self.root / 'analysis')
        self.assertEqual(result['planned_cell_count'], 4)
        self.assertEqual(len(result['rejected_runs']), 1)
        self.assertEqual(result['rejected_runs'][0]['reason'], 'planned_common_identity_mismatch')
        by_key = {(c['group_id'], c['case_id']): c for c in result['cells']}
        self.assertEqual(by_key['A', 'Q02']['status'], 'missing_run')
        self.assertEqual(by_key['B', 'Q01']['status'], 'rejected_run')
        self.assertIsNone(by_key['B', 'Q02']['outcome']['formal_score'])
        self.assertEqual(by_key['B', 'Q02']['outcome']['policy_lower_bound'], 12)
        self.assertEqual(result['strata'][1]['failure_categories'], {'auxiliary_service_failure': 1, 'rejected_run': 1})
        self.assertEqual(result['within_profile_comparisons'][0]['quality_pair_count'], 0)

    def test_unexplained_multi_attempt_rejected_instead_of_selecting_best(self):
        from fusion_pipeline.capability_study import analyze_plan
        plan = self.build(self.small_spec())
        cell = plan['cells'][0]
        first = self.manifest(cell, score=10)
        second = self.manifest(cell, score=99, name='second_attempt.json')
        value = json.loads(second.read_text(encoding='utf-8'))
        value['attempt_id'] = 'attempt_2'
        value['attempt_index'] = 2
        second.write_text(json.dumps(value), encoding='utf-8')
        result = analyze_plan(self.root / 'plan/experiment-plan.json', [first, second], self.root / 'analysis')
        row = next(c for c in result['cells'] if c['cell_id'] == cell['cell_id'])
        self.assertEqual(row['status'], 'rejected_run')
        self.assertEqual(len(row['attempts']), 2)
        self.assertIsNone(row['outcome']['formal_score'])
        self.assertEqual({r['reason'] for r in result['rejected_runs']}, {'unexplained_multiple_attempts'})

    def test_execution_reasoning_and_evidence_reasoning_must_match_plan(self):
        from fusion_pipeline.capability_study import analyze_plan
        plan = self.build(self.small_spec())
        path = self.manifest(plan['cells'][0])
        value = json.loads(path.read_text(encoding='utf-8'))
        value['requested_reasoning'] = {'mode': 'explicit', 'control': 'effort', 'value': 'high'}
        path.write_text(json.dumps(value), encoding='utf-8')
        result = analyze_plan(self.root / 'plan/experiment-plan.json', [path], self.root / 'analysis')
        self.assertEqual(result['rejected_runs'][0]['reason'], 'planned_candidate_configuration_mismatch')

    def test_profiles_separate_capability_effects_and_system_comparisons(self):
        from fusion_pipeline.capability_study import analyze_plan
        plan = self.build()
        paths = [self.manifest(cell) for cell in plan['cells']]
        result = analyze_plan(self.root / 'plan/experiment-plan.json', paths, self.root / 'analysis')
        self.assertEqual(len(result['strata']), 12)
        self.assertEqual({r['candidate_profile_id'] for r in result['within_profile_comparisons']}, {'claude', 'codex', 'opencode'})
        self.assertEqual(len(result['within_profile_comparisons']), 12)
        self.assertEqual(len(result['profile_comparisons']), 12)
        self.assertTrue(all(r['comparison_kind'] == 'system_settings' for r in result['profile_comparisons']))
        self.assertTrue(all(r['quality_pair_count'] == 4 for r in result['profile_comparisons']))
        self.assertEqual(len(result['interactions']), 3)
        self.assertTrue(all(r['mean_quality_interaction'] == 0 for r in result['interactions']))

    def test_reasoning_only_requires_verified_equal_everything_else_and_effort(self):
        from fusion_pipeline.capability_study import analyze_plan
        profiles = {name: self.profile('codex', {'mode': 'explicit', 'control': 'effort', 'value': name})
                    for name in ('low', 'high')}
        spec = self.spec(profiles=profiles, groups=[{'id': 'A', 'skills': [], 'mcp_servers': ['cad_native']}], repetitions=1)
        identity = {'status': 'verified', 'cli_version': 'fixture1', 'resolved_executable_sha256': '7' * 64,
                    'observed_model': 'fixture-model', 'model_connection_identity': {'service': 'fixture'},
                    'observed_isolation': {'native_tools': 'authorized'},
                    'generation_controls': {'temperature': 0}, 'reasoning_support_status': 'verified'}
        spec['profile_evidence'] = {'low': copy.deepcopy(identity), 'high': copy.deepcopy(identity)}
        plan = self.build(spec)
        paths = [self.manifest(c) for c in plan['cells']]
        result = analyze_plan(self.root / 'plan/experiment-plan.json', paths, self.root / 'analysis')
        self.assertEqual(result['profile_comparisons'][0]['comparison_kind'], 'reasoning_only')
        spec['profile_evidence']['high']['generation_controls']['temperature'] = 0.7
        other = self.build(spec, 'other')
        other_paths = []
        from fusion_pipeline.capability_study import run_manifest_template
        for cell in other['cells']:
            path = self.root / ('other_' + cell['cell_id'] + '.json')
            path.write_text(json.dumps(run_manifest_template(self.root / 'other/experiment-plan.json', cell['cell_id'], 'r', 'a')), encoding='utf-8')
            other_paths.append(path)
        result = analyze_plan(self.root / 'other/experiment-plan.json', other_paths, self.root / 'other_analysis')
        self.assertEqual(result['profile_comparisons'][0]['comparison_kind'], 'system_settings')

    def test_prespecified_infrastructure_retry_retained_and_never_best_score(self):
        from fusion_pipeline.capability_study import analyze_plan
        spec = self.small_spec()
        spec['infrastructure_retry'] = {'max_attempts': 2, 'selection_rule': 'first_non_infrastructure_attempt'}
        plan = self.build(spec)
        cell = plan['cells'][0]
        first = self.manifest(cell, submitted=False, name='infra.json')
        value = json.loads(first.read_text(encoding='utf-8'))
        value['outcome']['failure_category'] = 'infrastructure_failure'
        first.write_text(json.dumps(value), encoding='utf-8')
        second = self.manifest(cell, score=25, name='retry.json')
        value = json.loads(second.read_text(encoding='utf-8'))
        value.update(attempt_id='attempt_2', attempt_index=2,
                     retry={'reason': 'infrastructure_failure', 'previous_attempt_id': 'attempt_1', 'evidence_sha256': '8' * 64})
        second.write_text(json.dumps(value), encoding='utf-8')
        result = analyze_plan(self.root / 'plan/experiment-plan.json', [second, first], self.root / 'analysis')
        row = next(c for c in result['cells'] if c['cell_id'] == cell['cell_id'])
        self.assertEqual(row['selected_attempt_id'], 'attempt_2')
        self.assertEqual(len(row['attempts']), 2)
        self.assertEqual(row['outcome']['formal_score'], 25)
        self.assertEqual(result['rejected_runs'], [])

    def test_analysis_portable_without_original_sources_and_keeps_exposure_unknown(self):
        from fusion_pipeline.capability_study import analyze_plan
        plan = self.build(self.small_spec())
        paths = [self.manifest(cell) for cell in plan['cells']]
        shutil.copytree(self.root / 'plan', self.root / 'copied')
        self.skill.unlink()
        self.registry.unlink()
        result = analyze_plan(self.root / 'copied/experiment-plan.json', paths, self.root / 'analysis')
        self.assertEqual(len(result['rejected_runs']), 0)
        self.assertEqual(result['strata'][1]['skill_usage_status'], 'enabled_usage_unknown')
        self.assertEqual(result['strata'][1]['exposure_verification_counts'], {'not_verified': 2})

    def test_group_reordering_common_capabilities_rejected_as_additional_factor(self):
        from fusion_pipeline.capability_study import build_plan
        registry = json.loads(self.registry.read_text(encoding='utf-8'))
        registry['skills']['guide2'] = copy.deepcopy(registry['skills']['guide'])
        self.registry.write_text(json.dumps(registry), encoding='utf-8')
        spec = self.small_spec()
        spec['groups'][0]['skills'] = ['guide', 'guide2']
        spec['groups'][1]['skills'] = ['guide2', 'guide']
        with self.assertRaisesRegex(ValueError, 'study_common_capability_order_mismatch'):
            build_plan(spec, self.root / 'bad_order')

    def test_run_extra_launch_fields_and_nonfinite_measurement_are_rejected(self):
        from fusion_pipeline.capability_study import analyze_plan
        plan = self.build(self.small_spec())
        path = self.manifest(plan['cells'][0])
        value = json.loads(path.read_text(encoding='utf-8'))
        value['provider'] = 'different_cli'
        path.write_text(json.dumps(value), encoding='utf-8')
        result = analyze_plan(self.root / 'plan/experiment-plan.json', [path], self.root / 'analysis')
        self.assertEqual(result['rejected_runs'][0]['reason'], 'planned_candidate_configuration_mismatch')
        value.pop('provider')
        value['outcome']['formal_score'] = float('nan')
        path.write_text(json.dumps(value), encoding='utf-8')
        result = analyze_plan(self.root / 'plan/experiment-plan.json', [path], self.root / 'nan_analysis')
        self.assertEqual(result['rejected_runs'][0]['reason'], 'study_run_unreadable')

    def test_runtime_readiness_requires_frozen_cli_evidence_and_matches_fresh_observation(self):
        from fusion_pipeline.capability_study import validate_plan_execution
        plan = self.build(self.small_spec())
        cell = plan['cells'][0]
        path = self.root / 'plan/experiment-plan.json'
        config = json.loads((path.parent / cell['config_path']).read_text(encoding='utf-8'))
        with self.assertRaisesRegex(ValueError, 'study_profile_not_verified'):
            validate_plan_execution(path, cell['cell_id'], config, require_verified=True)
        spec = self.small_spec()
        frozen = {'configuration_valid': True, 'cli_available': True, 'required_flags_present': True,
                  'cli_version': 'fixture-v1', 'resolved_executable': str(self.binary),
                  'executable_sha256': hashlib.sha256(self.binary.read_bytes()).hexdigest()}
        spec['profile_evidence'] = {'claude': frozen}
        plan = self.build(spec, 'verified')
        cell = plan['cells'][0]
        path = self.root / 'verified/experiment-plan.json'
        config = json.loads((path.parent / cell['config_path']).read_text(encoding='utf-8'))
        self.assertEqual(validate_plan_execution(path, cell['cell_id'], config, require_verified=True,
                                               observed_launch=frozen), cell)
        observed = {**frozen, 'cli_version': 'fixture-v2'}
        with self.assertRaisesRegex(ValueError, 'planned_candidate_configuration_mismatch'):
            validate_plan_execution(path, cell['cell_id'], config, require_verified=True, observed_launch=observed)

    def test_malformed_manifest_types_are_retained_rejections(self):
        from fusion_pipeline.capability_study import analyze_plan
        plan = self.build(self.small_spec())
        path = self.manifest(plan['cells'][0])
        original = json.loads(path.read_text(encoding='utf-8'))
        invalid = []
        value = copy.deepcopy(original)
        value['cell_id'] = []
        invalid.append(value)
        value = copy.deepcopy(original)
        value['outcome']['failure_category'] = {'invalid': 'object'}
        invalid.append(value)
        value = copy.deepcopy(original)
        value['repetition'] = True
        invalid.append(value)
        for index, value in enumerate(invalid):
            path.write_text(json.dumps(value), encoding='utf-8')
            with self.subTest(index=index):
                result = analyze_plan(self.root / 'plan/experiment-plan.json', [path], self.root / ('bad_analysis_' + str(index)))
                self.assertEqual(len(result['rejected_runs']), 1)
                self.assertEqual(result['planned_cell_count'], 4)

    def test_empty_or_incomplete_common_identity_keeps_draft_but_excludes_comparisons(self):
        from fusion_pipeline.capability_study import analyze_plan, validate_plan_execution
        original = self.small_spec()
        variants = [{}]
        for key in original['common']:
            partial = copy.deepcopy(original['common'])
            partial.pop(key)
            variants.append(partial)
        variants.extend([{**original['common'], 'plugin_source_sha256': 'not-a-hash'},
                         {**original['common'], 'budget_policy': {'seconds': True, 'requests': 200}},
                         {**original['common'], 'cad_identity': {'backend': 'fusion', 'version': ''}}])
        for index, common in enumerate(variants):
            spec = copy.deepcopy(original)
            spec['common'] = common
            spec['common_identity_mode'] = 'draft'
            spec['profile_evidence'] = {'claude': {
                'configuration_valid': True, 'cli_available': True, 'required_flags_present': True,
                'cli_version': 'fixture-v1', 'resolved_executable': str(self.binary),
                'executable_sha256': hashlib.sha256(self.binary.read_bytes()).hexdigest()}}
            name = 'plan' if index == 0 else 'draft_' + str(index)
            plan = self.build(spec, name)
            path = self.root / name / 'experiment-plan.json'
            from fusion_pipeline.capability_study import run_manifest_template
            paths = []
            for cell in plan['cells']:
                run = run_manifest_template(path, cell['cell_id'], 'run_' + cell['cell_id'], 'a1')
                run['outcome'].update(native_submitted=True, trusted_export=True, primary_complete=True,
                                      formal_score=80, primary_coverage=1)
                record = self.root / (name + '_' + cell['cell_id'] + '.json')
                record.write_text(json.dumps(run), encoding='utf-8')
                paths.append(record)
            with self.subTest(index=index):
                result = analyze_plan(path, paths, self.root / ('draft_analysis_' + str(index)))
                self.assertEqual(result['planned_cell_count'], 4)
                self.assertEqual(result['common_identity_validation']['status'], 'incomplete')
                self.assertEqual(len(result['cells']), 4)
                self.assertTrue(all(r['status'] == 'observed' and not r['comparable'] for r in result['cells']))
                self.assertTrue(all(r['outcome']['formal_score'] == 80 for r in result['cells']))
                contrast = result['within_profile_comparisons'][0]
                self.assertEqual(contrast['quality_pair_count'], 0)
                self.assertIsNone(contrast['native_submission_rate_difference'])
                self.assertFalse(contrast['comparison_eligible'])
                config = json.loads((path.parent / plan['cells'][0]['config_path']).read_text(encoding='utf-8'))
                with self.assertRaisesRegex(ValueError, 'study_common_identity_incomplete'):
                    validate_plan_execution(path, plan['cells'][0]['cell_id'], config, require_verified=True)

    def test_malformed_retry_metadata_rejected_without_aborting_analysis(self):
        from fusion_pipeline.capability_study import analyze_plan
        spec = self.small_spec()
        spec['infrastructure_retry'] = {'max_attempts': 2, 'selection_rule': 'first_non_infrastructure_attempt'}
        plan = self.build(spec)
        cell = plan['cells'][0]
        first = self.manifest(cell, submitted=False, name='first_infra.json')
        value = json.loads(first.read_text(encoding='utf-8'))
        value['outcome']['failure_category'] = 'infrastructure_failure'
        first.write_text(json.dumps(value), encoding='utf-8')
        second = self.manifest(cell, name='second_bad_retry.json')
        original = json.loads(second.read_text(encoding='utf-8'))
        for index, retry in enumerate(([], None, 'retry', 1, True, {}, {'reason': 'infrastructure_failure'})):
            value = {**original, 'attempt_index': 2, 'attempt_id': 'second', 'retry': retry}
            second.write_text(json.dumps(value), encoding='utf-8')
            with self.subTest(retry=retry):
                result = analyze_plan(self.root / 'plan/experiment-plan.json', [first, second],
                                      self.root / ('bad_retry_analysis_' + str(index)))
                self.assertEqual(result['planned_cell_count'], 4)
                row = next(c for c in result['cells'] if c['cell_id'] == cell['cell_id'])
                self.assertEqual(row['status'], 'rejected_run')
                self.assertEqual(len(row['attempts']), 2)
                self.assertEqual(result['rejected_runs'][0]['reason'], 'study_retry_metadata_invalid')

    def test_captured_common_identity_binds_real_inputs_gt_protocol_and_source(self):
        from fusion_pipeline.capability_study import capture_common_identity, validate_plan_execution
        spec = self.small_spec()
        actual = capture_common_identity(spec['base_config'], ['Q01', 'Q02'])
        self.assertEqual([r['case_id'] for r in actual['case_identities']], ['Q01', 'Q02'])
        self.assertNotEqual(actual['input_manifest_sha256'], actual['gt_annotations_sha256'])
        self.assertEqual(actual['budget_policy'], {'seconds': 1800, 'requests': 200})
        spec['profile_evidence'] = {'claude': {
            'configuration_valid': True, 'cli_available': True, 'required_flags_present': True,
            'cli_version': 'fixture-v1', 'resolved_executable': str(self.binary),
            'executable_sha256': hashlib.sha256(self.binary.read_bytes()).hexdigest()}}
        plan = self.build(spec)
        path = self.root / 'plan/experiment-plan.json'
        cell = plan['cells'][0]
        config = json.loads((path.parent / cell['config_path']).read_text(encoding='utf-8'))
        (self.evaluator / 'worker.py').write_text('# mutated actual scoring source\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'planned_common_identity_mismatch'):
            validate_plan_execution(path, cell['cell_id'], config, require_verified=True)

    def test_capture_rejects_claimed_identity_and_private_hash_drift_before_output(self):
        from fusion_pipeline.capability_study import build_plan, capture_common_identity
        spec = self.small_spec()
        spec['common']['dataset_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'planned_common_identity_mismatch'):
            build_plan(spec, self.root / 'claimed')
        self.assertFalse((self.root / 'claimed').exists())
        spec['common_identity_mode'] = 'capture'
        plan = build_plan(spec, self.root / 'captured')
        self.assertNotEqual(plan['common']['dataset_sha256'], '0' * 64)
        (self.root / 'Q01.step').write_bytes(b'changed GT')
        with self.assertRaisesRegex(ValueError, 'study_private_gt_hash_mismatch'):
            capture_common_identity(spec['base_config'], spec['case_ids'])

    def test_capture_observes_order_and_input_bytes_and_rejects_stale_manifest(self):
        from fusion_pipeline.capability_study import capture_common_identity
        spec = self.small_spec()
        before = capture_common_identity(spec['base_config'], ['Q01', 'Q02'])
        reverse = capture_common_identity(spec['base_config'], ['Q02', 'Q01'])
        self.assertNotEqual(before['input_manifest_sha256'], reverse['input_manifest_sha256'])
        (self.questions / 'Q01/TASK.md').write_text('changed actual legacy input', encoding='utf-8')
        changed = capture_common_identity(spec['base_config'], ['Q01', 'Q02'])
        self.assertNotEqual(before['dataset_sha256'], changed['dataset_sha256'])
        self.assertEqual(before['gt_annotations_sha256'], changed['gt_annotations_sha256'])
        (self.questions / 'Q02/TASK.md').write_text('changed manifest-bound input', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'public input.*identity mismatch'):
            capture_common_identity(spec['base_config'], ['Q01', 'Q02'])

    def test_formal_admission_requires_maintainer_checks_bound_to_captured_case_bytes(self):
        from fusion_pipeline.capability_study import analyze_plan, validate_plan_execution, run_manifest_template
        original = self.small_spec()
        invalid = [{}, {'status': 'not_verified'}]
        missing = copy.deepcopy(original['admission'])
        missing['cases'].pop('Q02')
        invalid.append(missing)
        stale = copy.deepcopy(original['admission'])
        stale['cases']['Q01']['input_manifest_sha256'] = '0' * 64
        invalid.append(stale)
        unproved = copy.deepcopy(original['admission'])
        unproved['cases']['Q01']['gt_primary_measurability']['evidence_sha256'] = ''
        invalid.append(unproved)
        for index, admission in enumerate(invalid):
            spec = copy.deepcopy(original)
            spec['admission'] = admission
            plan = self.build(spec, 'admission_' + str(index))
            path = self.root / ('admission_' + str(index)) / 'experiment-plan.json'
            cell = plan['cells'][0]
            config = json.loads((path.parent / cell['config_path']).read_text(encoding='utf-8'))
            record = run_manifest_template(path, cell['cell_id'], 'r', 'a1')
            record['outcome'].update(native_submitted=True, trusted_export=True, primary_complete=True,
                                     formal_score=80, primary_coverage=1)
            run_path = self.root / ('admission_run_' + str(index) + '.json')
            run_path.write_text(json.dumps(record), encoding='utf-8')
            with self.subTest(index=index):
                with self.assertRaisesRegex(ValueError, 'study_admission_not_verified'):
                    validate_plan_execution(path, cell['cell_id'], config, require_verified=True)
                result = analyze_plan(path, [run_path], self.root / ('admission_analysis_' + str(index)))
                self.assertFalse(result['within_profile_comparisons'][0]['comparison_eligible'])
                self.assertEqual(result['planned_cell_count'], 4)
                observed = next(row for row in result['cells'] if row['cell_id'] == cell['cell_id'])
                self.assertEqual(observed['outcome']['formal_score'], 80)
                self.assertFalse(observed['comparable'])
                self.assertIn('admission', ' '.join(result['limitations']).lower())


if __name__ == '__main__':
    unittest.main()

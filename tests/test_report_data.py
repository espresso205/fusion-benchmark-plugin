import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from report_fixtures import make_run, revise_case, sha, write_json


class ReportDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def load(self, *args, **kwargs):
        self.assertIsNotNone(importlib.util.find_spec('src.report_data'), 'verified report data reader must exist')
        from src.report_data import load_report
        return load_report(*args, **kwargs)

    def test_preserves_null_credit_interval_and_denominator(self):
        make_run(self.root)
        bundle = self.load(self.root)
        cases = {r['case']: r for r in bundle.data['cases']}
        lower = cases['lower']
        self.assertEqual(lower['score_kind'], 'policy_lower_bound')
        self.assertIsNone(lower['metrics']['mass']['measured_score'])
        self.assertEqual(lower['metrics']['mass']['awarded_score'], 0)
        self.assertAlmostEqual(lower['final_score'], 8.5 / 13)
        self.assertAlmostEqual(lower['score_interval'][1], 10.5 / 13)
        self.assertAlmostEqual(lower['measurement_weight_coverage'], 11 / 13)
        self.assertEqual(bundle.data['summary']['participating_case_count'], 2)
        self.assertAlmostEqual(bundle.data['summary']['batch_mean'], 9.5 / 13)
        self.assertEqual(cases['timeout']['geometry']['candidate_status'], 'unknown')
        self.assertEqual(cases['invalid']['geometry']['candidate_status'], 'invalid')
        self.assertIsNone(cases['timeout']['final_score'])

    def test_unscored_cases_keep_null_intervals(self):
        make_run(self.root, kinds=('invalid', 'timeout'))
        for case in self.load(self.root).data['cases']:
            with self.subTest(case=case['case']):
                self.assertIsNone(case['final_score'])
                self.assertIsNone(case['score_interval'])

    def project_dimension_item(self, item):
        make_run(self.root, kinds=('complete',))
        path = self.root / 'complete/results/dimension.json'
        module = json.loads(path.read_text())
        module['raw']['items'] = [item, {'id': 'plane-pass', 'type': 'plane_pair',
            'expected': 10., 'measured': 10., 'pass': True, 'difference_mm': 0.}]
        write_json(path, module)
        case = self.load(self.root).data['cases'][0]
        self.assertEqual(case['integrity_status'], 'verified')
        return case['metrics']['dimension']['raw']['items'][0]

    def test_torus_failure_retains_both_radius_measurements_and_differences(self):
        item = self.project_dimension_item({'id': 'blend', 'pass': False,
            'expected_minor': 1., 'measured_minor': 2., 'minor_radius_difference_mm': 1.,
            'expected_major': 5., 'measured_major': 7., 'major_radius_difference_mm': 2.,
            'count': 1, 'fallback_association': True, 'failure_type': 'outside_tolerance',
            'private_annotations': {'secret': 'never export'}})
        self.assertEqual(item.get('type'), 'torus')
        self.assertEqual({key: item.get(key) for key in ('expected_minor', 'measured_minor',
            'minor_radius_difference_mm', 'expected_major', 'measured_major', 'major_radius_difference_mm')},
            {'expected_minor': 1., 'measured_minor': 2., 'minor_radius_difference_mm': 1.,
             'expected_major': 5., 'measured_major': 7., 'major_radius_difference_mm': 2.})
        self.assertNotIn('private_annotations', item)

    def test_cylinder_pattern_radius_failure_retains_measured_radii_and_deltas(self):
        item = self.project_dimension_item({'id': 'radial-pattern', 'pass': False,
            'expected': 4., 'measured': 4., 'count': 2, 'expected_count': 2,
            'pattern_radius': {'expected': 10., 'measured': [9., 12.], 'pass': False,
                               'private_note': 'never export'},
            'pattern_radius_difference_mm': [-1., 2.], 'failure_type': 'pattern_radius_mismatch'})
        self.assertEqual(item.get('pattern_radius'), {'expected': 10., 'measured': [9., 12.], 'pass': False})
        self.assertEqual(item.get('pattern_radius_difference_mm'), [-1., 2.])

    def test_cylinder_pattern_position_failure_retains_pairs_and_centroid_error(self):
        item = self.project_dimension_item({'id': 'position-pattern', 'pass': False,
            'expected': 4., 'measured': 4., 'count': 3, 'expected_count': 3,
            'pattern_positions': {'pairwise_expected': [3., 4., 5.], 'pairwise_measured': [4., 6., 7.],
                'centroid_xy': [3., 4.], 'pairwise_pass': False, 'centroid_pass': False,
                'private_annotation': {'input': 'never export'}},
            'pattern_pairwise_difference_mm': [1., 2., 2.], 'pattern_centroid_norm_mm': 5.,
            'pattern_centroid_excess_mm': 4., 'failure_type': 'pattern_position_mismatch'})
        self.assertEqual(item.get('pattern_positions'), {'pairwise_expected': [3., 4., 5.],
            'pairwise_measured': [4., 6., 7.], 'centroid_xy': [3., 4.],
            'pairwise_pass': False, 'centroid_pass': False})
        self.assertEqual(item.get('pattern_pairwise_difference_mm'), [1., 2., 2.])
        self.assertEqual(item.get('pattern_centroid_norm_mm'), 5.)
        self.assertEqual(item.get('pattern_centroid_excess_mm'), 4.)

    def test_raw_is_allowlisted_and_model_paths_stay_private(self):
        make_run(self.root, kinds=('lower',))
        b = self.load(self.root, run_name='run C:/private/key.json <b>name</b>')
        case = b.data['cases'][0]
        self.assertEqual(case['metrics']['shape']['raw']['volumetric_iou'], 1.)
        self.assertEqual(case['metrics']['dimension']['raw']['items'][0]['type'], 'plane_pair')
        self.assertEqual(case['metrics']['dimension']['raw']['items'][0]['difference_mm'], 2.)
        text = json.dumps(b.data)
        for secret in ('DO NOT EXPORT', 'private_annotations', 'C:/private', str(self.root), '</script>'):
            self.assertNotIn(secret, text)
        self.assertTrue(Path(b.model_sources['lower']['world']['candidate']['path']).is_absolute())
        self.assertTrue(all(not Path(s['path']).is_absolute() for s in b.source_files))

    def test_changed_candidate_bytes_quarantines_only_that_case(self):
        make_run(self.root, kinds=('lower', 'complete'))
        (self.root / 'lower/inputs/candidate.step').write_bytes(b'changed')
        b = self.load(self.root)
        c = {r['case']: r for r in b.data['cases']}
        self.assertEqual(c['lower']['integrity_status'], 'untrusted')
        self.assertIsNone(c['lower']['final_score'])
        self.assertEqual(c['complete']['integrity_status'], 'verified')
        self.assertEqual(b.data['summary']['participating_case_count'], 1)
        self.assertNotIn('lower', b.model_sources)

    def test_disappeared_model_removes_preview_without_changing_score(self):
        make_run(self.root, kinds=('lower',))
        (self.root / 'lower/derived/candidate_aligned.step').unlink()
        b = self.load(self.root)
        self.assertAlmostEqual(b.data['cases'][0]['final_score'], 8.5 / 13)
        self.assertNotIn('candidate', b.model_sources['lower']['scoring'])
        self.assertIn('candidate', b.model_sources['lower']['world'])

    def test_strict_uses_original_candidate_in_both_views(self):
        make_run(self.root, mode='strict', kinds=('complete',))
        b = self.load(self.root)
        self.assertEqual(b.data['coordinate_mode'], 'strict')
        self.assertEqual(b.model_sources['complete']['world']['candidate'], b.model_sources['complete']['scoring']['candidate'])

    def test_altered_aggregate_and_total_cannot_authorize_a_score(self):
        summary = make_run(self.root, kinds=('lower', 'complete'))
        revise_case(self.root, summary, 'lower', lambda r: r.update(final_score=1.))
        summary['batch_mean'] = (1. + 10.5 / 13) / 2
        write_json(self.root / 'summary.json', summary)
        b = self.load(self.root)
        cases = {r['case']: r for r in b.data['cases']}
        self.assertIsNone(cases['lower']['final_score'])
        self.assertEqual(cases['complete']['integrity_status'], 'verified')

    def test_altered_summary_mean_does_not_authorize_any_scores(self):
        summary = make_run(self.root)
        summary['batch_mean'] = 0.123
        write_json(self.root / 'summary.json', summary)
        b = self.load(self.root)
        self.assertEqual(b.data['summary']['participating_case_count'], 0)
        self.assertTrue(b.data['warnings'])

    def test_corrupt_manifest_never_authorizes_scores(self):
        make_run(self.root)
        (self.root / 'manifest.json').write_text('{broken', encoding='utf-8')
        b = self.load(self.root)
        self.assertEqual(len(b.data['cases']), 4)
        self.assertEqual(b.data['summary']['participating_case_count'], 0)

    def test_diagnostic_loss_is_accepted_only_if_final_result_disclosed_it(self):
        summary = make_run(self.root, kinds=('complete',))
        from src.contract_v4 import aggregate_case, refresh_assessment
        path = self.root / 'complete/results/distance.json'
        path.write_text('{broken', encoding='utf-8')
        self.assertEqual(self.load(self.root).data['cases'][0]['integrity_status'], 'untrusted')
        modules = {p.stem: json.loads(p.read_text()) for p in path.parent.glob('*.json') if p != path}
        old = summary['cases'][0]
        row = aggregate_case('complete', modules, modules['shape']['input_hashes'])
        row.update(run_manifest_sha256=old['run_manifest_sha256'],
                   diagnostic_read_errors=[{'module': 'distance', 'detail': 'distance: parse/read error'}])
        row['reasons'].append({'module': 'distance', 'status': 'diagnostic_unavailable', 'detail': 'parse/read error'})
        refresh_assessment(row, old['execution'])
        summary['cases'][0] = row
        write_json(self.root / 'complete/aggregate.json', row)
        write_json(self.root / 'summary.json', summary)
        b = self.load(self.root)
        self.assertEqual(b.data['cases'][0]['integrity_status'], 'verified')
        self.assertAlmostEqual(b.data['cases'][0]['final_score'], 10.5 / 13)
        self.assertIn('distance', b.data['cases'][0]['diagnostic_unavailable_metrics'])

    def test_module_identity_mismatch_does_not_hide_as_diagnostic_loss(self):
        make_run(self.root, kinds=('complete',))
        p = self.root / 'complete/results/distance.json'
        m = json.loads(p.read_text()); m['input_hashes']['candidate'] = '0' * 64
        write_json(p, m)
        self.assertEqual(self.load(self.root).data['cases'][0]['integrity_status'], 'untrusted')

    def test_wrong_evaluated_candidate_hash_quarantines_case(self):
        make_run(self.root, kinds=('complete',))
        path = self.root / 'complete/results/shape.json'
        module = json.loads(path.read_text())
        module['evaluation_input_hashes']['candidate'] = module['input_hashes']['candidate']
        write_json(path, module)
        self.assertEqual(self.load(self.root).data['cases'][0]['integrity_status'], 'untrusted')

    def test_staged_annotation_identity_mismatch_quarantines_case_without_exporting_it(self):
        make_run(self.root, kinds=('complete',))
        (self.root / 'complete/inputs/annotations.json').write_bytes(b'changed private annotations')
        b = self.load(self.root)
        self.assertEqual(b.data['cases'][0]['integrity_status'], 'untrusted')
        self.assertNotIn('changed private annotations', json.dumps(b.data))

    def test_pipeline_outer_join_keeps_export_failure(self):
        make_run(self.root, kinds=('complete',))
        pipeline = {'cases': [{'case_id': 'complete', 'state': 'evaluation_complete'},
                              {'case_id': 'export-failed', 'state': 'export_failed', 'error': 'C:/secret/token'}]}
        b = self.load(self.root, pipeline=pipeline)
        self.assertEqual(b.data['summary']['total_case_count'], 2)
        self.assertEqual(b.data['summary']['pipeline_unscored_case_count'], 1)
        row = next(r for r in b.data['cases'] if r['case'] == 'export-failed')
        self.assertEqual(row['pipeline_state'], 'export_failed')
        self.assertIsNone(row['final_score'])

    def test_pipeline_only_and_unsafe_case_identity(self):
        b = self.load(None, pipeline={'coordinate_mode': 'strict', 'cases': [{'case_id': 'one', 'state': 'failed'}]})
        self.assertEqual(b.data['coordinate_mode'], 'strict')
        self.assertEqual(b.data['summary']['total_case_count'], 1)
        with self.assertRaises(ValueError):
            self.load(None, pipeline={'cases': [{'case_id': '../secret', 'state': 'failed'}]})

    def test_untrusted_analysis_rejects_appendix_only(self):
        make_run(self.root, kinds=('complete',))
        path = self.root / 'ablation.json'
        write_json(path, {'schema_version': 'fusion-ablation/1', 'sources': [], 'analyses': []})
        b = self.load(self.root, analysis=path)
        self.assertEqual(b.data['analysis']['status'], 'rejected')
        self.assertAlmostEqual(b.data['cases'][0]['final_score'], 10.5 / 13)

    def test_matching_analysis_keeps_variants_counts_correlations_and_relative_sources(self):
        make_run(self.root, kinds=('complete',))
        from analysis.result_reader import read_source
        from analysis.ablation import analyze_dataset
        dataset = read_source(self.root)
        result = analyze_dataset(dataset)
        analysis = {'schema_version': 'fusion-ablation/1', 'ranking_eligible': False,
                    'sources': [dataset], 'source_files': dataset['source_files'], 'analyses': [result]}
        path = self.root / 'ablation.json'
        write_json(path, analysis)
        b = self.load(self.root, analysis=path)
        self.assertEqual(b.data['analysis']['status'], 'ok')
        self.assertEqual(b.data['analysis']['counts']['complete'], 1)
        cohort = b.data['analysis']['cohorts']['complete_measurements']
        self.assertEqual(len(cohort['variants']), 7)
        self.assertAlmostEqual(cohort['variants'][0]['mean_score'], 10.5 / 13)
        self.assertEqual(cohort['correlations']['pearson']['shape']['mass']['n'], 1)
        self.assertNotIn(str(self.root).replace('\\', '\\\\'), json.dumps(b.data['analysis']))
        analysis['analyses'][0]['coordinate_mode'] = 'strict'
        write_json(path, analysis)
        self.assertEqual(self.load(self.root, analysis=path).data['analysis']['status'], 'rejected')
        analysis['analyses'][0]['coordinate_mode'] = 'geometry'
        analysis['sources'][0]['source_files'][0]['sha256'] = '0' * 64
        write_json(path, analysis)
        b = self.load(self.root, analysis=path)
        self.assertEqual(b.data['analysis']['status'], 'rejected')
        self.assertEqual(b.data['cases'][0]['integrity_status'], 'verified')

    def test_source_json_symlink_escape_is_not_read(self):
        make_run(self.root, kinds=('complete',))
        path = self.root / 'complete/results/shape.json'
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / 'shape.json'
            target.write_bytes(path.read_bytes())
            path.unlink()
            try:
                path.symlink_to(target)
            except OSError:
                self.skipTest('symlink creation unavailable')
            b = self.load(self.root)
            self.assertEqual(b.data['cases'][0]['integrity_status'], 'untrusted')


if __name__ == '__main__':
    unittest.main()

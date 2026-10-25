"""Ablation checks with normalized dict fixtures and hand-derived expectations."""
import copy
import json
import unittest

from analysis import ablation


METRICS = ('shape', 'dimension', 'distance', 'mass', 'face_match', 'section')
WEIGHTS = dict(zip(METRICS, (.3, .25, .15, .1, .1, .1)))


def record(case, scores, baseline, *, coverage=1., participates=True,
           status='ok', score_kind='measured', execution=None, metric_statuses=None):
    measured = dict(zip(METRICS, scores))
    return {
        'case': case, 'source_id': 'run-1',
        'source_protocol_id': 'fusion-step-adapted-v3.0',
        'baseline_score': baseline, 'measured_scores': measured,
        'awarded_scores': {name: (value if value is not None else 0.)
                           if participates else None for name, value in measured.items()},
        'participates': participates, 'validation_passed': participates,
        'status': status, 'score_kind': score_kind,
        'original_weight_coverage': coverage,
        'exclusion_reasons': [] if participates else [status],
        'input_hashes': {}, 'execution': execution or {},
        'metric_statuses': metric_statuses or {},
    }


def dataset(records, weights=None):
    return {'source_id': 'run-1', 'system': 'system-a',
            'source_protocol_id': 'fusion-step-adapted-v3.0',
            'weights': WEIGHTS.copy() if weights is None else weights,
            'records': records, 'source_files': [], 'warnings': ['fixture warning']}


def variants(cohort):
    return {variant['id']: variant for variant in cohort['variants']}


def ranked(variant):
    return {row['case']: row for row in variant['ranking']}


class AblationTests(unittest.TestCase):
    def test_thirteen_variants_have_independent_scores_and_rank_changes(self):
        # Baseline A=.3, B=.25, C=.275. Removing shape leaves A=0, B=.25, C=.125.
        data = dataset([
            record('A', [1, 0, 0, 0, 0, 0], .3),
            record('B', [0, 1, 0, 0, 0, 0], .25),
            record('C', [.5, .5, 0, 0, 0, 0], .275),
        ])
        analysis = ablation.analyze_dataset(data)
        cohort = analysis['cohorts']['complete_measurements']
        result = variants(cohort)
        self.assertEqual(len(result), 13)
        self.assertEqual(result['baseline']['mode'], 'baseline')
        self.assertIsNone(result['baseline']['removed_metric'])
        self.assertAlmostEqual(result['baseline']['mean_score'], .275)
        renormalized = result['without_shape_renormalized']
        fixed = result['without_shape_zero_weight']
        self.assertAlmostEqual(renormalized['mean_score'], .17857142857142858)
        self.assertAlmostEqual(fixed['mean_score'], .125)
        renormalized_rows = ranked(renormalized)
        fixed_rows = ranked(fixed)
        self.assertAlmostEqual(renormalized_rows['B']['score'], .35714285714285715)
        self.assertAlmostEqual(renormalized_rows['B']['delta'], .10714285714285715)
        self.assertEqual(fixed_rows['B']['score'], .25)
        self.assertEqual(fixed_rows['B']['delta'], 0.)
        self.assertEqual(renormalized_rows['B']['baseline_rank'], 3.)
        self.assertEqual(renormalized_rows['B']['rank'], 1.)
        self.assertEqual(renormalized_rows['B']['rank_change'], 2.)
        self.assertEqual(renormalized_rows['A']['rank_change'], -2.)
        self.assertAlmostEqual(renormalized['rank_comparison']['spearman']['value'], -1.)
        self.assertAlmostEqual(renormalized['rank_comparison']['kendall_tau_b']['value'], -1.)

    def test_complete_cohort_remains_fixed_when_removed_metric_is_missing(self):
        data = dataset([
            record('complete', [1, 1, 1, 1, 1, 1], 1.),
            record('partial', [1, 1, 1, 1, 1, None], .9, coverage=.9,
                   score_kind='policy_lower_bound', status='scored_with_unavailable_metrics'),
            record('invalid', [None] * 6, None, coverage=0., participates=False,
                   status='candidate_invalid', score_kind='unscored'),
        ])
        original = copy.deepcopy(data)
        analysis = ablation.analyze_dataset(data)
        self.assertEqual(data, original)
        self.assertEqual(analysis['records'], original['records'])
        self.assertEqual(analysis['warnings'], ['fixture warning'])
        complete = analysis['cohorts']['complete_measurements']
        policy = analysis['cohorts']['policy_awards']
        self.assertEqual(complete['case_ids'], ['complete'])
        self.assertEqual(policy['case_ids'], ['complete', 'partial'])
        for variant in complete['variants']:
            self.assertEqual([row['case'] for row in variant['ranking']], ['complete'])
        result = variants(policy)
        row = ranked(result['without_section_renormalized'])['partial']
        self.assertAlmostEqual(row['score'], 1.)
        self.assertAlmostEqual(row['original_weight_coverage'], .9)
        self.assertAlmostEqual(row['retained_weight_coverage'], 1.)
        self.assertEqual(row['score_kind'], 'policy_lower_bound')
        other = ranked(result['without_shape_zero_weight'])['partial']
        self.assertAlmostEqual(other['score'], .6)
        self.assertAlmostEqual(other['retained_weight_coverage'], 6. / 7.)
        self.assertEqual(analysis['counts']['total'], 3)
        self.assertEqual(analysis['counts']['participating'], 2)
        self.assertEqual(analysis['counts']['complete'], 1)
        self.assertEqual(analysis['counts']['excluded'], 1)
        self.assertEqual(analysis['counts']['unavailable_metric_counts']['section'], 1)
        self.assertEqual(analysis['counts']['unavailable_metric_counts']['shape'], 0)

    def test_policy_correlations_use_only_actual_paired_measurements(self):
        data = dataset([
            record('A', [.2, .8, 0, 0, 0, 0], .26),
            record('B', [.4, .4, 0, 0, 0, 0], .22),
            record('C', [.8, None, 0, 0, 0, 0], .24, coverage=.75,
                   score_kind='policy_lower_bound'),
        ])
        result = ablation.analyze_dataset(data)
        matrix = result['cohorts']['policy_awards']['correlations']
        for method in ('pearson', 'spearman'):
            self.assertEqual(set(matrix[method]), set(METRICS))
            self.assertEqual(matrix[method]['shape']['dimension']['n'], 2)
            self.assertAlmostEqual(matrix[method]['shape']['dimension']['value'], -1.)
            self.assertEqual(matrix[method]['shape']['dimension'],
                             matrix[method]['dimension']['shape'])
            self.assertEqual(matrix[method]['shape']['shape']['n'], 3)
            self.assertIsNone(matrix[method]['distance']['mass']['value'])
            self.assertEqual(matrix[method]['distance']['mass']['reason'], 'constant_input')
        complete = result['cohorts']['complete_measurements']['correlations']
        self.assertEqual(complete['pearson']['shape']['shape']['n'], 2)

    def test_average_tied_ranks_are_used_for_each_variant(self):
        result = ablation.analyze_dataset(dataset([
            record('B', [1, 0, 0, 0, 0, 0], .3),
            record('A', [1, 0, 0, 0, 0, 0], .3),
            record('C', [0, 1, 0, 0, 0, 0], .25),
        ]))
        result = variants(result['cohorts']['complete_measurements'])
        baseline = ranked(result['baseline'])
        self.assertEqual(baseline['A']['rank'], 1.5)
        self.assertEqual(baseline['B']['rank'], 1.5)
        self.assertEqual(baseline['C']['rank'], 3.)
        rows = ranked(result['without_shape_zero_weight'])
        self.assertEqual(rows['A']['rank'], 2.5)
        self.assertEqual(rows['A']['rank_change'], -1.)
        self.assertEqual(rows['C']['rank_change'], 2.)

    def test_failures_count_explicit_execution_and_metric_errors_once(self):
        rows = [
            record('ok', [1] * 6, 1.),
            record('invalid', [None] * 6, None, coverage=0., participates=False,
                   status='candidate_invalid', score_kind='unscored'),
            record('reference', [None] * 6, 0., coverage=0.,
                   status='scored_with_unavailable_metrics', score_kind='policy_lower_bound',
                   metric_statuses={'shape': 'reference_invalid'}),
            record('timeout', [None] * 6, 0., coverage=0., score_kind='policy_lower_bound',
                   execution={'status': 'timed_out', 'timed_out': True, 'returncode': -1},
                   metric_statuses={'shape': 'evaluator_error'}),
            record('metric', [None] * 6, 0., coverage=0., score_kind='policy_lower_bound',
                   metric_statuses={'shape': 'evaluator_error'}),
            record('failed', [None] * 6, None, coverage=0., participates=False,
                   status='evaluator_error', score_kind='unscored'),
        ]
        counts = ablation.analyze_dataset(dataset(rows))['counts']
        self.assertEqual(counts['evaluator_failure_cases'], 3)
        self.assertEqual(counts['evaluator_failure_rate'], .5)

    def test_empty_dataset_has_no_nan_and_explicit_null_aggregates(self):
        result = ablation.analyze_dataset(dataset([]))
        self.assertEqual(result['counts']['total'], 0)
        self.assertIsNone(result['counts']['evaluator_failure_rate'])
        for cohort in result['cohorts'].values():
            self.assertEqual(cohort['case_count'], 0)
            self.assertEqual(len(cohort['variants']), 13)
            self.assertIsNone(cohort['variants'][0]['mean_score'])
            self.assertEqual(cohort['variants'][0]['ranking'], [])
        json.dumps(result, allow_nan=False)

    def test_removing_only_positive_weight_has_explicit_undefined_renormalization(self):
        weights = dict(zip(METRICS, (1., 0., 0., 0., 0., 0.)))
        result = ablation.analyze_dataset(dataset([
            record('A', [.8, 1, 1, 1, 1, 1], .8),
            record('B', [.2, 0, 0, 0, 0, 0], .2),
        ], weights))
        result = variants(result['cohorts']['complete_measurements'])
        invalid = result['without_shape_renormalized']
        self.assertIsNone(invalid['mean_score'])
        for row in invalid['ranking']:
            for key in ('score', 'delta', 'rank', 'rank_change', 'retained_weight_coverage'):
                self.assertIsNone(row[key])
        self.assertEqual(invalid['rank_comparison']['spearman']['reason'], 'no_retained_weight')
        fixed = result['without_shape_zero_weight']
        self.assertEqual(fixed['mean_score'], 0.)
        self.assertEqual([row['rank'] for row in fixed['ranking']], [1.5, 1.5])
        unchanged = ranked(result['without_dimension_renormalized'])
        self.assertEqual(unchanged['A']['score'], .8)
        json.dumps(result, allow_nan=False)

    def test_missing_zero_weight_metric_still_excludes_from_complete_cohort(self):
        weights = dict(zip(METRICS, (1., 0., 0., 0., 0., 0.)))
        result = ablation.analyze_dataset(dataset([
            record('A', [.8, None, 1, 1, 1, 1], .8, score_kind='policy_lower_bound'),
        ], weights))
        self.assertEqual(result['cohorts']['complete_measurements']['case_count'], 0)
        self.assertEqual(result['cohorts']['policy_awards']['case_count'], 1)

    def test_invalid_weight_sets_and_nonfinite_scores_are_rejected(self):
        for bad in (float('nan'), float('inf'), -.3, True):
            data = dataset([record('A', [1] * 6, 1.)])
            data['weights']['shape'] = bad
            with self.subTest(weight=bad), self.assertRaises(ValueError):
                ablation.analyze_dataset(data)
        data = dataset([record('A', [1] * 6, 1.)])
        data['weights']['shape'] = .4
        with self.assertRaises(ValueError):
            ablation.analyze_dataset(data)
        data = dataset([record('A', [float('nan'), 1, 1, 1, 1, 1], 1.)])
        with self.assertRaises(ValueError):
            ablation.analyze_dataset(data)

    def test_duplicate_case_ids_are_rejected(self):
        with self.assertRaises(ValueError):
            ablation.analyze_dataset(dataset([
                record('same', [1] * 6, 1.), record('same', [0] * 6, 0.)]))


if __name__ == '__main__':
    unittest.main()

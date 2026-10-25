import copy
import unittest

from analysis.system_comparison import compare_systems


def source(system, a, b):
    records = [{'case': case, 'input_hashes': {'gt': 'a' * 64, 'annotations': 'b' * 64},
                'participates': True, 'measured_scores': dict(shape=1., dimension=1.)}
               for case in ('one', 'two')]
    data = dict(source_id=system, system=system, source_protocol_id='v3', weights=dict(shape=.5, dimension=.5), records=records)
    variants = [dict(id='baseline', mode='baseline', removed_metric=None,
                     ranking=[dict(case='one', score=a), dict(case='two', score=b)]),
                dict(id='without_shape_renormalized', mode='renormalized', removed_metric='shape',
                     ranking=[dict(case='one', score=1.), dict(case='two', score=1.)])]
    analysis = dict(source_id=system, cohorts={
        'complete_measurements': dict(case_ids=['one', 'two'], variants=variants),
        'policy_awards': dict(case_ids=['one', 'two'], variants=copy.deepcopy(variants))})
    return data, analysis


class SystemComparisonTests(unittest.TestCase):
    def test_matched_systems_compare_means_on_identical_case_set(self):
        left, a = source('A', 1., .6)
        right, b = source('B', .7, .7)
        result = compare_systems([left, right], [a, b])
        self.assertEqual(result['status'], 'available')
        variants = result['cohorts']['complete_measurements']['variants']
        ranks = {row['system']: row for row in variants[0]['ranking']}
        self.assertAlmostEqual(ranks['A']['score'], .8)
        self.assertEqual(ranks['A']['rank'], 1.)
        self.assertEqual(ranks['B']['rank'], 2.)
        self.assertEqual([row['rank'] for row in variants[1]['ranking']], [1.5, 1.5])
        self.assertFalse(result['ranking_eligible'])

    def test_protocol_gt_annotation_and_case_mismatch_prevent_comparison(self):
        for mutation in ('protocol', 'gt', 'annotations', 'case', 'weight'):
            left, a = source('A', 1., .6)
            right, b = source('B', .7, .7)
            if mutation == 'protocol':
                right['source_protocol_id'] = 'legacy'
            elif mutation in ('gt', 'annotations'):
                right['records'][0]['input_hashes'][mutation] = 'c' * 64
            elif mutation == 'case':
                right['records'][0]['case'] = 'three'
            else:
                right['weights'] = dict(shape=.3, dimension=.7)
            with self.subTest(mutation=mutation):
                result = compare_systems([left, right], [a, b])
                self.assertEqual(result['status'], 'not_applicable')
                self.assertTrue(result['reasons'])

    def test_complete_cohort_uses_common_cases_and_policy_refuses_unequal_admission(self):
        left, a = source('A', 1., .6)
        right, b = source('B', .7, .7)
        b['cohorts']['complete_measurements']['case_ids'] = ['one']
        b['cohorts']['policy_awards']['case_ids'] = ['one']
        result = compare_systems([left, right], [a, b])
        full = result['cohorts']['complete_measurements']
        self.assertEqual(full['case_ids'], ['one'])
        self.assertEqual(full['variants'][0]['ranking'][0]['score'], 1.)
        self.assertEqual(result['cohorts']['policy_awards']['status'], 'not_applicable')

    def test_no_system_metadata_is_not_a_model_leaderboard(self):
        left, a = source('A', 1., .6)
        left['system'] = None
        self.assertEqual(compare_systems([left], [a])['status'], 'not_requested')


if __name__ == '__main__':
    unittest.main()

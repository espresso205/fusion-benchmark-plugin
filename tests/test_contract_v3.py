import copy
import unittest

from src.contract import aggregate_case, MODULE_NAMES, refresh_assessment

PROTOCOL = 'fusion-step-adapted-v3.0'
HASHES = {'gt': 'a'*64, 'candidate': 'b'*64, 'annotations': 'c'*64}


def records():
    return {name: {'schema_version': '3.0', 'protocol_id': PROTOCOL, 'case': 'Q01',
        'input_hashes': dict(HASHES), 'status': 'ok', 'score': 1.,
        'quality': {'measurement_valid': True, 'candidate_valid': True},
        'raw': {'gt': {'healthy': True}, 'candidate': {'healthy': True}} if name == 'validity' else {},
        'timing': {'compute_s': .1}} for name in MODULE_NAMES}


class V3ContractTests(unittest.TestCase):
    def aggregate(self, rows):
        return aggregate_case('Q01', rows, HASHES, protocol_id=PROTOCOL)

    def test_validation_passed_receives_fixed_weight_score_despite_measurement_failure(self):
        rows = records()
        rows['section'].update(status='evaluator_error', score=None)
        rows['section']['quality']['measurement_valid'] = False
        out = self.aggregate(rows)
        self.assertTrue(out['participates_in_scoring'])
        self.assertAlmostEqual(out['final_score'], .9)
        self.assertEqual(out['status'], 'scored_with_unavailable_metrics')
        self.assertIsNone(out['metrics']['section']['measured_score'])
        self.assertEqual(out['metrics']['section']['awarded_score'], 0.)
        self.assertEqual(out['metrics']['section']['score_source'], 'unavailable_policy_floor')
        self.assertEqual(out['score_interval'], [out['final_score'], 1.])
        self.assertAlmostEqual(out['measurement_weight_coverage'], .9)

    def test_even_all_measurements_unavailable_keeps_valid_candidate_in_scoring(self):
        rows = records()
        rows = {'validity': rows['validity']}
        out = self.aggregate(rows)
        self.assertTrue(out['participates_in_scoring'])
        self.assertEqual(out['final_score'], 0.)
        self.assertEqual(out['score_interval'], [0., 1.])
        self.assertEqual(out['measurement_weight_coverage'], 0.)
        self.assertTrue(all(out['metrics'][name]['measured_score'] is None for name in MODULE_NAMES[1:]))

    def test_reference_measurement_preflight_does_not_override_candidate_validation(self):
        rows = records()
        rows['validity']['quality'].update(reference_validated=False, reference_status='reference_invalid')
        self.assertEqual(self.aggregate(rows)['final_score'], 1.)

    def test_reference_validation_failure_does_not_erase_completed_candidate_validation(self):
        rows = records()
        rows['validity'].update(status='reference_invalid', score=None)
        rows['validity']['quality']['measurement_valid'] = False
        rows['validity']['raw']['gt'] = {'healthy': False}
        out = self.aggregate(rows)
        self.assertTrue(out.get('validation_passed'))
        self.assertEqual(out['final_score'], 1.)

    def test_confirmed_invalid_candidate_does_not_participate(self):
        rows = records()
        rows['validity'].update(status='candidate_invalid', score=None)
        rows['validity']['quality'].update(measurement_valid=False, candidate_invalid_confirmed=True)
        out = self.aggregate(rows)
        self.assertFalse(out['participates_in_scoring'])
        self.assertIsNone(out['final_score'])
        self.assertEqual(out['status'], 'candidate_invalid')

    def test_unconfirmed_validation_is_not_a_pass(self):
        rows = records()
        rows['validity'].update(status='evaluator_error', score=None)
        out = self.aggregate(rows)
        self.assertFalse(out['participates_in_scoring'])
        self.assertIsNone(out['final_score'])

    def test_stale_evidence_is_not_accepted_as_valid_measurement_or_validation(self):
        for name in ('mass', 'validity'):
            rows = records()
            rows[name]['input_hashes']['candidate'] = 'd'*64
            out = self.aggregate(rows)
            self.assertIsNone(out['final_score'])
            self.assertEqual(out['status'], 'integrity_error')
            self.assertIsNone(out['metrics'][name]['measured_score'])
            if name == 'validity':
                self.assertFalse(out['validation_passed'])

    def test_post_run_integrity_failure_revokes_all_awards(self):
        out = self.aggregate(records())
        out.update(final_score=None, status='integrity_error')
        refresh_assessment(out)
        self.assertTrue(all(v is None for v in out['awarded_module_scores'].values()))
        self.assertTrue(all(m['awarded_score'] is None for m in out['metrics'].values()))

    def test_nonfinite_metric_is_unavailable_not_nan_or_whole_case_null(self):
        rows = records()
        rows['mass']['score'] = float('nan')
        out = self.aggregate(rows)
        self.assertAlmostEqual(out['final_score'], .9)
        self.assertIsNone(out['metrics']['mass']['measured_score'])

    def test_approximate_measurement_preserves_score_and_method(self):
        rows = records()
        rows['mass'].update(score=.75, backend='adaptive_gk')
        rows['mass']['quality']['scored_approximate'] = True
        out = self.aggregate(rows)
        self.assertAlmostEqual(out['final_score'], .975)
        self.assertEqual(out['metrics']['mass']['backend'], 'adaptive_gk')
        self.assertEqual(out['metrics']['mass']['score_source'], 'approximate_measurement')
        self.assertEqual(out['measurement_weight_coverage'], 1.)


if __name__ == '__main__':
    unittest.main()

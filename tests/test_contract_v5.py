import copy
import importlib.util
import unittest

AVAILABLE = importlib.util.find_spec('src.contract_v5') is not None
if AVAILABLE:
    from src import contract_v5 as core
H = {'gt':'a'*64,'candidate':'b'*64,'annotations':'c'*64}

def records():
    out = {}
    for name in ('candidate_basic','reference_basic','candidate_assembly','reference_preflight','pose','shape','dimension','mass'):
        out[name] = dict(schema_version='5.0', protocol_id='fusion-step-adapted-v5.0', case='x',
            coordinate_mode='strict', input_hashes=dict(H), evaluation_input_hashes=dict(H),
            status='pass', measured_score=1. if name in ('shape','dimension','mass') else None, raw={})
    out['reference_preflight']['raw'] = {'acceptance_verified':True}
    return out

class ContractV5Tests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(AVAILABLE, 'v5 contract must exist')
    def agg(self, rows):
        return core.aggregate_case('x', rows, H, coordinate_mode='strict')
    def test_strict_failure_preserves_complete_geometric_measurement(self):
        rows=records(); rows['candidate_assembly']['status']='fail'
        result=self.agg(rows)
        self.assertEqual(result['final_score'],1.)
        self.assertEqual(result['candidate_assembly']['status'],'fail')
        self.assertEqual(result['score_kind'],'complete_geometric_similarity')
        self.assertFalse(result['ranking_eligible'])
    def test_failed_basic_blocks_formal_score_without_fake_zero(self):
        rows=records(); rows['candidate_basic']['status']='fail'
        result=self.agg(rows)
        self.assertIsNone(result['final_score'])
        self.assertEqual(result['metrics']['mass']['measured_score'],1.)
    def test_reference_absent_failed_or_unverified_never_passes(self):
        for state in ('fail','error','timeout','partial','not_run'):
            rows=records(); rows['reference_preflight']['status']=state
            self.assertIsNone(self.agg(rows)['final_score'])
        rows=records(); rows['reference_preflight']['raw']={}
        self.assertIsNone(self.agg(rows)['final_score'])
        rows=records(); del rows['reference_preflight']
        self.assertEqual(self.agg(rows)['reference_preflight']['status'],'not_run')
    def test_missing_metric_is_null_and_separate_lower_bound(self):
        rows=records(); rows['mass'].update(status='timeout',measured_score=None)
        result=self.agg(rows)
        self.assertIsNone(result['final_score'])
        self.assertIsNone(result['metrics']['mass']['measured_score'])
        self.assertAlmostEqual(result['partial_lower_bound'],11/13)
        self.assertAlmostEqual(result['measurement_weight_coverage'],11/13)
    def test_identity_mismatch_revokes_score(self):
        rows=records(); rows['shape']['input_hashes']['gt']='f'*64
        result=self.agg(rows)
        self.assertEqual(result['status'],'integrity_error')
        self.assertIsNone(result['final_score'])
        self.assertIsNone(result['partial_lower_bound'])
    def test_geometry_requires_qualified_derived_identity(self):
        rows=records()
        for row in rows.values(): row['coordinate_mode']='geometry'
        result=core.aggregate_case('x',rows,H)
        self.assertEqual(result['status'],'integrity_error')
    def test_true_zero_is_complete_measurement(self):
        rows=records()
        for n in ('shape','dimension','mass'): rows[n]['measured_score']=0.
        self.assertEqual(self.agg(rows)['final_score'],0.)
    def test_dimension_gt_self_check_failure_blocks_reference(self):
        rows=records(); rows['dimension']['raw']={'gt_self_check_total':2,'gt_self_check_passed':1}
        result=self.agg(rows)
        self.assertIsNone(result['final_score'])
        self.assertEqual(result['reference_preflight']['status'],'fail')
    def test_known_reference_failure_overrides_absent_acceptance(self):
        rows=records();rows.pop('reference_preflight');rows['reference_basic']['status']='fail'
        self.assertEqual(self.agg(rows)['reference_preflight']['status'],'fail')
    def test_empty_probes_expose_reference_failure_without_self_check_counts(self):
        rows=records();rows.pop('reference_preflight')
        rows['dimension'].update(status='error',legacy_status='reference_invalid',measured_score=None,
                                 raw={'error':'empty or duplicate dimension probes'})
        result=self.agg(rows)
        self.assertEqual(result['reference_preflight']['status'],'fail')
        self.assertIsNone(result['final_score'])

if __name__=='__main__': unittest.main()

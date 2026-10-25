import copy
import importlib.util
import unittest

AVAILABLE = importlib.util.find_spec('src.contract_v4') is not None
if AVAILABLE:
    from src import contract_v4 as core

HASHES = {'gt': 'a'*64, 'candidate': 'b'*64, 'annotations': 'c'*64}
DERIVED = 'd'*64


def records(mode='geometry'):
    rows = {}
    for name in ('validity', 'pose', 'shape', 'dimension', 'mass', 'distance', 'face_match', 'section'):
        evaluated = dict(HASHES)
        if mode == 'geometry' and name in ('shape','dimension','distance','face_match','section'):
            evaluated['candidate'] = DERIVED
        rows[name] = {'schema_version':'4.0', 'protocol_id':'fusion-step-adapted-v4.0',
            'case':'case1', 'coordinate_mode':mode, 'input_hashes':dict(HASHES),
            'evaluation_input_hashes':evaluated, 'status':'ok', 'score':1.,
            'quality':{'measurement_valid':True}, 'raw':{}}
    rows['validity']['quality']['candidate_valid'] = True
    rows['validity']['raw'] = {'candidate':{'healthy':True},'gt':{'healthy':True}}
    rows['pose']['raw'] = {'evaluation_candidate_sha256':DERIVED if mode == 'geometry' else HASHES['candidate'],
        'aligned_candidate_sha256':DERIVED, 'coordinate_mode':mode}
    return rows


class ContractV4Tests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(AVAILABLE, 'v4 scoring contract must exist')

    def test_diagnostics_neither_contribute_nor_reduce_coverage(self):
        rows = records()
        for name in ('distance','face_match','section'):
            rows.pop(name)
        out = core.aggregate_case('case1',rows,HASHES)
        self.assertEqual(out['final_score'],1.)
        self.assertEqual(out['measurement_weight_coverage'],1.)
        self.assertEqual(out['score_kind'],'measured')
        self.assertEqual(out['unavailable_metrics'],[])
        self.assertEqual(set(out['diagnostic_unavailable_metrics']),{'distance','face_match','section'})
        self.assertIsNone(out['metrics']['section']['awarded_score'])

    def test_missing_core_retains_null_and_policy_credit(self):
        rows=records(); rows.pop('dimension')
        out=core.aggregate_case('case1',rows,HASHES)
        self.assertAlmostEqual(out['final_score'],8/13)
        self.assertEqual(out['unavailable_metrics'],['dimension'])
        self.assertEqual(out['score_kind'],'policy_lower_bound')
        self.assertIsNone(out['metrics']['dimension']['measured_score'])
        self.assertEqual(out['metrics']['dimension']['awarded_score'],0.)
        self.assertEqual(out['score_interval'][1],1.)

    def test_derived_or_mode_identity_mismatch_revokes_score(self):
        for key,value in [('coordinate_mode','strict'),('evaluation_input_hashes',HASHES)]:
            with self.subTest(key=key):
                rows=records(); rows['shape'][key]=value
                out=core.aggregate_case('case1',rows,HASHES)
                self.assertEqual(out['status'],'integrity_error')
                self.assertIsNone(out['final_score'])

    def test_pose_failure_keeps_mass_and_never_accepts_world_frame_shape(self):
        rows=records(); rows['pose'].update(status='evaluator_error',score=None,raw={})
        rows['pose']['quality']['measurement_valid']=False
        for name in ('shape','dimension','distance','face_match','section'):
            rows.pop(name)
        out=core.aggregate_case('case1',rows,HASHES)
        self.assertAlmostEqual(out['final_score'],2/13)
        self.assertEqual(set(out['unavailable_metrics']),{'shape','dimension'})
        rows['shape']=records()['shape']
        self.assertEqual(core.aggregate_case('case1',rows,HASHES)['status'],'integrity_error')

    def test_strict_pose_failure_does_not_block_original_measurements(self):
        rows=records('strict'); rows['pose'].update(status='evaluator_error',score=None,raw={})
        rows['pose']['quality']['measurement_valid']=False
        out=core.aggregate_case('case1',rows,HASHES,coordinate_mode='strict')
        self.assertEqual(out['final_score'],1.)

    def test_reject_diagnostic_weight_and_foreign_input(self):
        rows=records()
        weights=dict(core.DEFAULT_WEIGHTS); weights['distance']=.1; weights['shape']-=.1
        self.assertEqual(core.aggregate_case('case1',rows,HASHES,weights)['status'],'integrity_error')
        rows['mass']['input_hashes']['gt']='e'*64
        self.assertIsNone(core.aggregate_case('case1',rows,HASHES)['final_score'])

    def test_post_run_failure_revokes_awards_and_keeps_evidence(self):
        out=core.aggregate_case('case1',records(),HASHES)
        out.update(final_score=None,status='integrity_error')
        core.refresh_assessment(out,{'status':'failed'})
        self.assertFalse(out['participates_in_scoring'])
        self.assertTrue(all(v is None for v in out['awarded_module_scores'].values()))
        self.assertEqual(out['scores'][core.PROTOCOL_ID]['coverage']['required_modules'],3)

    def test_invalid_candidate_has_no_score(self):
        rows=records(); rows['validity']['status']='candidate_invalid'
        rows['validity']['quality']['candidate_invalid_confirmed']=True
        out=core.aggregate_case('case1',rows,HASHES)
        self.assertIsNone(out['final_score'])
        self.assertFalse(out['participates_in_scoring'])


if __name__ == '__main__':
    unittest.main()

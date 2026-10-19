import copy
import tempfile
from pathlib import Path
import unittest
from src.contract import aggregate_case, MODULE_NAMES, PROTOCOL_ID, atomic_write_json, failed_case_result

HASHES = {'gt':'a'*64, 'candidate':'b'*64, 'annotations':'c'*64}


def records():
    return {name:{'schema_version':'2.0','protocol_id':PROTOCOL_ID,'case':'Q01',
        'input_hashes':dict(HASHES),'status':'ok','score':1.,
        'quality':{'measurement_valid':True,'reference_validated':True},'raw':{},'timing':{'compute_s':0.1}}
        for name in MODULE_NAMES}


class ContractTests(unittest.TestCase):
    def test_unexpected_case_failure_has_complete_assessment_envelope(self):
        out=failed_case_result('Q01','staging failed')
        self.assertEqual(out['status'],'evaluator_error')
        self.assertEqual(out['execution']['status'],'failed')
        self.assertIsNone(out['quality_status'])
        self.assertEqual(out['geometry']['reference_status'],'unknown')
        self.assertEqual(out['geometry']['candidate_status'],'unknown')
        self.assertEqual(out['scores'][PROTOCOL_ID]['aggregation_status'],'blocked')
        self.assertEqual(out['scores'][PROTOCOL_ID]['coverage'],
                         {'scored_modules':0,'required_modules':7})
        self.assertTrue(out['failure'][0]['recommended_action'])

    def test_complete_valid_results_aggregate(self):
        self.assertEqual(aggregate_case('Q01',records(),HASHES)['final_score'],1.)

    def test_rejected_module_score_is_diagnostic_only(self):
        r=records()
        r['shape']['score']=0.75
        r['shape']['quality']['measurement_valid']=False

        out=aggregate_case('Q01',r,HASHES)

        self.assertIsNone(out['metrics']['shape']['value'])
        self.assertIsNone(out['metrics']['shape']['score'])
        self.assertEqual(out['metrics']['shape']['diagnostic_value'],0.75)

    def test_missing_nonfinite_out_of_range_and_stale_contracts_block(self):
        for mutation in ('missing','nan','inf','negative','too_large','none','protocol','hash','case','quality','schema'):
            with self.subTest(mutation=mutation):
                r = records()
                if mutation == 'missing': del r['shape']
                elif mutation in ('nan','inf','negative','too_large','none'):
                    r['shape']['score'] = {'nan':float('nan'),'inf':float('inf'),'negative':-1.,'too_large':2.,'none':None}[mutation]
                elif mutation == 'protocol': r['shape']['protocol_id']='v1'
                elif mutation == 'hash': r['shape']['input_hashes']['gt']='d'*64
                elif mutation == 'case': r['shape']['case']='Q02'
                elif mutation == 'quality': r['shape']['quality']['measurement_valid']=False
                else: r['shape']['schema_version']='1.0'
                out = aggregate_case('Q01',r,HASHES)
                self.assertIsNone(out['final_score'])
                self.assertEqual(out['status'],'evaluator_error')
                self.assertIsNone(out['metrics']['shape']['quality'])
                self.assertEqual(out['scores'][PROTOCOL_ID]['coverage'],
                                 {'scored_modules':6,'required_modules':7})

    def test_gt_self_check_failure_is_reference_error_not_zero(self):
        r=records()
        r['dimension']['raw']={'gt_self_check_passed':0,'gt_self_check_total':1}
        out=aggregate_case('Q01',r,HASHES)
        self.assertEqual(out['status'],'reference_invalid')
        self.assertIsNone(out['final_score'])

    def test_measurement_failure_is_separate_from_valid_geometry(self):
        r=records()
        r['validity']['raw']={
            'gt':{'healthy':True},
            'candidate':{'healthy':True},
        }
        r['validity']['quality'].update(reference_validated=False, reference_status='reference_invalid')
        r['section'].update(status='reference_invalid', score=None)
        r['section']['quality'].update(measurement_valid=False)

        out=aggregate_case('Q01',r,HASHES)

        self.assertEqual(out['geometry']['reference_status'],'valid')
        self.assertEqual(out['geometry']['candidate_status'],'valid')
        self.assertEqual(out['metrics']['section']['quality'],'measurement_unreliable')
        score=out['scores'][PROTOCOL_ID]
        self.assertEqual(score['aggregation_status'],'blocked')
        self.assertEqual(score['coverage'],{'scored_modules':6,'required_modules':7})
        self.assertIsNone(score['value'])

    def test_multiple_measurement_and_support_issues_are_reported_together(self):
        r=records()
        r['validity']['raw']={'gt':{'healthy':True},'candidate':{'healthy':True}}
        r['validity']['quality'].update(reference_validated=False, reference_status='reference_invalid')
        r['mass'].update(status='reference_invalid', score=None)
        r['mass']['quality'].update(measurement_valid=False)
        r['face_match'].update(status='unsupported', score=None)
        r['face_match']['quality'].update(measurement_valid=False)

        out=aggregate_case('Q01',r,HASHES)

        self.assertEqual(set(out['quality_flags']),{'measurement_unreliable','unsupported_metric'})
        self.assertEqual(out['quality_status'],'unsupported_metric')

    def test_candidate_invalid_does_not_hide_module_error(self):
        r=records()
        r['validity'].update(status='candidate_invalid',score=None)
        r['validity']['quality'].update(measurement_valid=False,candidate_invalid_confirmed=True)
        r['shape'].update(status='evaluator_error',score=None)
        r['shape']['quality']['measurement_valid']=False
        out=aggregate_case('Q01',r,HASHES)
        self.assertEqual(out['status'],'evaluator_error')
        self.assertIsNone(out['final_score'])
        self.assertGreaterEqual(len(out['reasons']),2)

    def test_confirmed_invalid_candidate_with_good_reference_scores_zero(self):
        r=records()
        for row in r.values():
            row.update(status='candidate_invalid',score=None)
            row['quality']['measurement_valid']=False
        r['validity']['quality']['candidate_invalid_confirmed']=True
        out=aggregate_case('Q01',r,HASHES)
        self.assertEqual(out['status'],'candidate_invalid')
        self.assertEqual(out['final_score'],0.)

    def test_missing_weights_are_not_renormalized(self):
        out=aggregate_case('Q01',records(),HASHES,{'shape':1.})
        self.assertIsNone(out['final_score'])

    def test_reference_preflight_exception_is_not_blame_on_reference_geometry(self):
        r=records()
        r['validity']['quality'].update(reference_validated=False,reference_status='evaluator_error')
        out=aggregate_case('Q01',r,HASHES)
        self.assertEqual(out['status'],'evaluator_error')
        self.assertIsNone(out['final_score'])

    def test_atomic_output_refuses_overwrite_and_nan(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'r.json'
            atomic_write_json(p,{'score':1})
            with self.assertRaises(FileExistsError): atomic_write_json(p,{'score':0})
            with self.assertRaises(ValueError): atomic_write_json(Path(d)/'nan.json',{'score':float('nan')})
            self.assertIn('1',p.read_text())

    def test_malformed_validity_container_returns_structured_error(self):
        for malformed in (42, {'quality':'invalid'}):
            r=records();r['validity']=malformed
            out=aggregate_case('Q01',r,HASHES)
            self.assertEqual(out['status'],'evaluator_error')
            self.assertIsNone(out['final_score'])


if __name__ == '__main__': unittest.main()

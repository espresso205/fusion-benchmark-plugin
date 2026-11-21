import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from src.io_utils import sha
from src.contract_v5 import aggregate_case,reference_acceptance
from evaluate_v5 import reports

BASE=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('review_v5_validation',BASE/'fusion_pipeline/v5_validation.py')
validation=importlib.util.module_from_spec(spec);spec.loader.exec_module(validation)

def write(path,data):path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(data),encoding='utf-8')

class PublicationTests(unittest.TestCase):
    def fixture(self,root):
        output=root/'out';output.mkdir();inputs=output/'x/inputs';inputs.mkdir(parents=True)
        for role in ('gt','candidate'):(inputs/(role+'.step')).write_text(role)
        write(inputs/'annotations.json',{'gt_sha256':sha(inputs/'gt.step')})
        hashes={n:sha(inputs/('annotations.json' if n=='annotations' else n+'.step')) for n in ('gt','candidate','annotations')}
        acceptance={'status':'pass','gt_sha256':hashes['gt'],'annotations_sha256':hashes['annotations'],
                    'checks':{n:{'status':'pass'} for n in ('inventory','configuration','dimensions','self_intersection','interference')},'unresolved_issues':[]}
        write(root/'acceptance.json',acceptance);write(inputs/'reference_acceptance.json',acceptance)
        source={'case':'x',**{n:str(inputs/('annotations.json' if n=='annotations' else n+'.step')) for n in hashes},
                'expected_hashes':hashes,'reference_acceptance':str(root/'acceptance.json'),
                'reference_acceptance_sha256':sha(root/'acceptance.json')}
        cases=root/'cases.json';write(cases,{'cases':[source]})
        manifest={'schema_version':'5.0','protocol_id':'fusion-step-adapted-v5.0','coordinate_mode':'strict','cases':[source],
                  'cases_sha256':sha(cases),'weights_sha256':'f'*64}
        write(output/'manifest.json',manifest);digest=sha(output/'manifest.json')
        modules={}
        for name in ('candidate_basic','reference_basic','reference_preflight','candidate_assembly','shape','dimension','mass'):
            data={'schema_version':'5.0','protocol_id':'fusion-step-adapted-v5.0','case':'x','coordinate_mode':'strict',
                  'input_hashes':hashes,'evaluation_input_hashes':hashes,'run_manifest_sha256':digest,'status':'pass','raw':{}}
            if name in ('shape','dimension','mass'):data['measured_score']=1.
            if name=='reference_preflight':data.update(reference_acceptance(acceptance,hashes))
            if name=='candidate_assembly':data['status']='fail'
            modules[name]=data;write(output/'x/results'/f'{name}.json',data)
        result=aggregate_case('x',modules,hashes,coordinate_mode='strict');result.update(run_manifest_sha256=digest,execution={'status':'completed'})
        summary={'schema_version':'5.0','protocol_id':'fusion-step-adapted-v5.0','coordinate_mode':'strict','run_manifest_sha256':digest,
                 'cases':[result],'code_unchanged':True,'original_inputs_unchanged':True,'acceptance_unchanged':True,
                 'batch_mean':1.,'complete_case_ids':['x'],'participating_case_count':1,'total_case_count':1,'ranking_eligible':False,
                 'candidate_failure_rate':0.,'assembly_pass_rate':0.,'measurement_completion_rate':1.,'basic_inconclusive_rate':0.}
        self.publish(output,summary);return output,cases,summary
    def publish(self,output,summary):
        write(output/'x/aggregate.json',summary['cases'][0]);write(output/'summary.json',summary);reports(output,summary)
    def validate(self,output,cases):return validation.validate_publication(output,cases,0,'f'*64)
    def test_real_producer_publication_is_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            output,cases,summary=self.fixture(Path(temp));self.assertEqual(self.validate(output,cases),summary)
    def test_fabricated_assembly_pass_is_rejected_even_with_consistent_reports(self):
        with tempfile.TemporaryDirectory() as temp:
            output,cases,summary=self.fixture(Path(temp))
            summary['cases'][0]['candidate_assembly']={**summary['cases'][0]['candidate_assembly'],'status':'pass'}
            self.publish(output,summary)
            with self.assertRaisesRegex(ValueError,'candidate_assembly'):self.validate(output,cases)
    def test_false_summary_assembly_rate_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            output,cases,summary=self.fixture(Path(temp));summary['assembly_pass_rate']=1.;self.publish(output,summary)
            with self.assertRaisesRegex(ValueError,'assembly_pass_rate'):self.validate(output,cases)
    def test_staged_acceptance_tamper_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            output,cases,summary=self.fixture(Path(temp));write(output/'x/inputs/reference_acceptance.json',{'status':'pass'})
            with self.assertRaisesRegex(ValueError,'acceptance'):self.validate(output,cases)
    def test_acceptance_claim_must_match_frozen_document(self):
        with tempfile.TemporaryDirectory() as temp:
            output,cases,summary=self.fixture(Path(temp));data=summary['cases'][0]['measurements']['reference_preflight']
            data['raw']['acceptance']['checks']['inventory']['note']='fabricated approval'
            write(output/'x/results/reference_preflight.json',data);self.publish(output,summary)
            with self.assertRaisesRegex(ValueError,'acceptance'):self.validate(output,cases)

if __name__=='__main__':unittest.main()

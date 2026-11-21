import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from src.io_utils import sha

BASE=Path(__file__).resolve().parents[1]
PLUGIN=os.environ.get('V5_PLUGIN_SOURCE', str(Path(__file__).resolve().parents[1]))
V5='fusion-step-adapted-v5.0'
def overlay(name):
    spec=importlib.util.spec_from_file_location('fusion_pipeline.'+name,BASE/'fusion_pipeline'/f'{name}.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

@unittest.skipUnless(PLUGIN,'requires staged plugin dependencies')
class ManagementV5Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0,PLUGIN)
        cls.service=overlay('service');cls.study=overlay('capability_study');cls.reporting=overlay('reporting')
    def test_management_catalog_and_capabilities_include_explicit_v5(self):
        tool=next(t for t in self.service.ManagementService.tools if t['name']=='start_run')
        self.assertIn(V5,tool['inputSchema']['properties']['protocol']['enum'])
        result=self.service.ManagementService().call('capabilities',{})
        self.assertIn(V5,result['supported_scoring_protocols'])
        self.assertEqual(result['default_scoring_protocol'],'fusion-step-adapted-v4.0')
    def test_start_run_accepts_v5_without_launching_a_real_process(self):
        from fusion_pipeline import runner,candidate_config
        with tempfile.TemporaryDirectory() as temp:
            service=self.service.ManagementService();service.config={'runs_root':temp,'provider':'codex','model':'test',
                'candidate_selection':{},'cad_backend':'fusion','cad_config_sha256':'a'*64}
            with patch.object(candidate_config,'resolve_candidate_configuration',side_effect=lambda config,*a,**kw:config),\
                 patch.object(runner,'validate_run_configuration',side_effect=lambda config:(config,{})),\
                 patch.object(self.service.subprocess,'Popen') as launch:
                result=service.call('start_run',{'protocol':V5,'coordinate_mode':'geometry','preflight_only':True})
            config=json.loads((Path(temp)/'.service'/result['run_id']/'config.json').read_text())
            self.assertEqual((config['protocol'],config['coordinate_mode']),(V5,'geometry'))
            self.assertIn('--preflight-only',launch.call_args.args[0])
    def test_capability_identity_hashes_v5_weights_and_workers(self):
        from fusion_pipeline import runner,candidate_prompt
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);question=root/'questions/Q01';question.mkdir(parents=True)
            (question/'TASK.md').write_text('box');(question/'drawing.dxf').write_text('0\nSECTION\n2\nHEADER\n0\nENDSEC\n0\nEOF\n')
            gt=root/'gt.step';gt.write_text('test');ann=root/'annotations.json';ann.write_text(json.dumps({'gt_sha256':sha(gt)}))
            private=root/'cases.json';private.write_text(json.dumps({'cases':[{'case':'Q01','gt':str(gt),'annotations':str(ann),
                'expected_hashes':{'gt':sha(gt),'annotations':sha(ann)}}]}))
            evaluator=root/'evaluator';evaluator.mkdir()
            for name in ('evaluate.py','worker.py','weights.json','evaluate_v5.py','worker_v5.py','weights_v5.json','weights_v4.json'):(evaluator/name).write_text('{}')
            config={'questions_root':str(root/'questions'),'private_cases':str(private),'evaluator_directory':str(evaluator),'protocol':V5}
            with patch.object(runner,'source_hashes',return_value={}),patch.object(candidate_prompt,'common_protocol_sha256',return_value='a'*64):
                before=self.study.capture_common_identity(config,['Q01'])
                old_config={**config,'protocol':'fusion-step-adapted-v4.0'}
                old_before=self.study.capture_common_identity(old_config,['Q01'])
                for name in ('evaluate_v5.py','worker_v5.py','weights_v5.json'):
                    self.assertIn(name,before['evaluator_source_files'])
                    (evaluator/name).write_text('{"changed":true}')
                    after=self.study.capture_common_identity(config,['Q01'])
                    self.assertNotEqual(before['evaluator_weights_sha256'],after['evaluator_weights_sha256'])
                    (evaluator/name).write_text('{}')
                acceptance=root/'acceptance.json'
                acceptance.write_text(json.dumps({'status':'fail','gt_sha256':sha(gt),'annotations_sha256':sha(ann)}))
                registry=json.loads(private.read_text());registry['cases'][0].update(reference_acceptance=str(acceptance),reference_acceptance_sha256=sha(acceptance))
                private.write_text(json.dumps(registry))
                failed=self.study.capture_common_identity(config,['Q01'])
                self.assertNotEqual(before['gt_annotations_sha256'],failed['gt_annotations_sha256'])
                self.assertEqual(failed['case_identities'][0]['reference_acceptance_sha256'],sha(acceptance))
                acceptance.write_text(json.dumps({'status':'pass','gt_sha256':sha(gt),'annotations_sha256':sha(ann)}))
                with self.assertRaisesRegex(ValueError,'acceptance.*hash'):
                    self.study.capture_common_identity(config,['Q01'])
                registry['cases'][0]['reference_acceptance_sha256']=sha(acceptance);private.write_text(json.dumps(registry))
                accepted=self.study.capture_common_identity(config,['Q01'])
                self.assertNotEqual(failed['gt_annotations_sha256'],accepted['gt_annotations_sha256'])
                old_after=self.study.capture_common_identity(old_config,['Q01'])
                self.assertEqual(old_before,old_after)
    def test_v5_private_report_returns_existing_static_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);evaluation=root/'evaluation';evaluation.mkdir()
            (evaluation/'summary.json').write_text(json.dumps({'protocol_id':V5,'cases':[{'case':'Q01'}]}))
            (evaluation/'report.html').write_text('<html>v5</html>')
            with patch('src.html_reporting.try_build_report',side_effect=AssertionError('v4 renderer must not run')):
                result=self.reporting.build_private_report(root,{'scoring_protocol':V5,'cases':[]})
            self.assertEqual(result['status'],'completed')
            references=self.reporting.report_references(root)
            self.assertEqual(Path(references['files']['html']),evaluation/'report.html')
            self.assertEqual(references['html_report_generation']['case_count'],1)
    def test_v4_report_keeps_existing_renderer_and_nested_index(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'html-report').mkdir();(root/'html-report/index.html').write_text('v4')
            with patch('src.html_reporting.try_build_report',return_value={'status':'completed','case_count':0}) as render:
                result=self.reporting.build_private_report(root,{'scoring_protocol':'fusion-step-adapted-v4.0','cases':[]})
            self.assertEqual(result['status'],'completed');render.assert_called_once()
            self.assertEqual(Path(self.reporting.report_references(root)['files']['html']),root/'html-report/index.html')
    def test_v3_geometry_stays_rejected_before_dispatch(self):
        service=self.service.ManagementService();service.config={}
        with patch.object(self.service.subprocess,'Popen') as launch:
            with self.assertRaisesRegex(ValueError,'historical protocols require strict'):
                service.call('start_run',{'protocol':'fusion-step-adapted-v3.0','coordinate_mode':'geometry'})
            launch.assert_not_called()
    def test_results_preserve_v5_states_lower_bound_and_summary_denominators(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);run=root/'runs/run';run.mkdir(parents=True)
            identifier='a'*32;job=root/'runs/.service'/identifier;job.mkdir(parents=True)
            (job/'job.json').write_text(json.dumps({'state':'completed','run_path':str(run)}))
            private=root/'private/run/evaluation';private.mkdir(parents=True)
            row={'case':'Q01','status':'reference_pending','final_score':None,'partial_lower_bound':.5,
                 'partial_lower_bound_kind':'diagnostic_missing_credit_floor_not_complete_score','measurement_count':2,
                 'candidate_basic':{'status':'pass','raw':{'private_path':'secret-reference.step'}},'candidate_assembly':{'status':'fail'},
                 'reference_preflight':{'status':'partial'},'ranking_eligible':False,'metrics':{},
                 'execution':{'status':'completed','stage_timeouts':['mass']}}
            summary={'protocol_id':V5,'cases':[row],'batch_mean':None,'complete_case_ids':[],
                     'total_case_count':1,'participating_case_count':0,'assembly_pass_rate':0.,'candidate_failure_rate':0.,
                     'measurement_completion_rate':0.,'basic_inconclusive_rate':0.}
            (private/'summary.json').write_text(json.dumps(summary))
            service=self.service.ManagementService();service.config={'runs_root':str(root/'runs'),'private_runs_root':str(root/'private')}
            result=service.call('results',{'run_id':identifier})
            for field in ('candidate_basic','candidate_assembly','reference_preflight','partial_lower_bound',
                          'partial_lower_bound_kind','measurement_count','ranking_eligible'):
                expected={'status':row[field]['status']} if field in ('candidate_basic','candidate_assembly','reference_preflight') else row[field]
                self.assertEqual(result['scores'][0].get(field),expected,field)
            self.assertNotIn('secret-reference.step',json.dumps(result))
            self.assertEqual(result['scores'][0]['execution']['stage_timeouts'],['mass'])
            for field in ('complete_case_ids','total_case_count','participating_case_count','assembly_pass_rate',
                          'candidate_failure_rate','measurement_completion_rate','basic_inconclusive_rate'):
                self.assertEqual(result.get(field),summary[field],field)

if __name__=='__main__':unittest.main()

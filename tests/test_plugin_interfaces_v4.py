"""Plugin v4 protocol selection and complete source snapshot boundaries."""
from pathlib import Path
import csv
import json
import tempfile
import unittest

from fusion_pipeline import runner


class PluginInterfacesV4Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name in ('evaluate.py','evaluate_v4.py','worker.py','worker_v4.py','weights.json','weights_v3.json','weights_v4.json'):
            (self.root/name).write_text('{}' if name.endswith('.json') else '# fixture', encoding='utf-8')

    def test_v4_snapshot_contains_v4_entrypoints_and_weights(self):
        dest = self.root/'snapshot'
        runner.snapshot_evaluator(self.root, dest)
        for name in ('evaluate_v4.py','worker_v4.py','weights_v4.json'):
            self.assertTrue((dest/name).is_file(), name)

    def test_v4_default_and_explicit_v3_are_selected_without_hidden_alignment(self):
        self.assertTrue(callable(getattr(runner,'scoring_options',None)), 'scoring_options must validate the protocol boundary')
        protocol, mode, weights = runner.scoring_options({}, self.root)
        self.assertEqual((protocol,mode,weights.name), ('fusion-step-adapted-v4.0','geometry','weights_v4.json'))
        self.assertEqual(runner.scoring_options({'protocol':'fusion-step-adapted-v3.0'},self.root)[1], 'strict')
        with self.assertRaisesRegex(ValueError, 'coordinate'):
            runner.scoring_options({'protocol':'fusion-step-adapted-v3.0','coordinate_mode':'geometry'},self.root)
        with self.assertRaisesRegex(ValueError, 'protocol'):
            runner.scoring_options({'protocol':'invented'}, self.root)

    def publication(self):
        output=self.root/'run'
        output.mkdir()
        cases_path=self.root/'cases.json'
        cases=[dict(case='part')]
        runner.atomic(cases_path,dict(cases=cases))
        manifest=dict(protocol_id='fusion-step-adapted-v4.0',schema_version='4.0',coordinate_mode='geometry',
                      cases=cases,cases_sha256=runner.sha(cases_path),weights_sha256='a'*64)
        runner.atomic(output/'manifest.json',manifest)
        digest=runner.sha(output/'manifest.json')
        modules=('shape','dimension','distance','mass','face_match','section')
        row=dict(case='part',protocol_id=manifest['protocol_id'],coordinate_mode='geometry',run_manifest_sha256=digest,
                 participates_in_scoring=True,final_score=1.,status='ok',
                 execution=dict(status='completed',returncode=0,timed_out=False),
                 metrics={n:dict(measured_score=1. if n in ('shape','dimension','mass') else None) for n in modules})
        (output/'part').mkdir()
        runner.atomic(output/'part/aggregate.json',row)
        runner.atomic(output/'summary.json',dict(protocol_id=manifest['protocol_id'],schema_version='4.0',coordinate_mode='geometry',
            run_manifest_sha256=digest,cases=[row],code_unchanged=True,original_inputs_unchanged=True,
            all_cases_aggregatable=True,batch_mean=1.,ranking_eligible=False))
        (output/'report.md').write_text('|part|valid|100.000|measured|100|100|100|—|100|—|—|\n',encoding='utf-8')
        with (output/'comparison.csv').open('w',encoding='utf-8',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=['case','module','new_score'])
            writer.writeheader()
            writer.writerows(dict(case='part',module=n,new_score=row['metrics'][n]['measured_score']) for n in modules)
        return output,cases_path

    def test_v4_publication_validates_scored_results_and_rejects_requested_mode_mismatch(self):
        output,cases=self.publication()
        result=runner.validate_evaluation(output,cases,0,'a'*64,protocol='fusion-step-adapted-v4.0',coordinate_mode='geometry')
        self.assertEqual(result['batch_mean'],1.)
        with self.assertRaisesRegex(ValueError,'coordinate'):
            runner.validate_evaluation(output,cases,0,'a'*64,coordinate_mode='strict')
        summary=json.loads((output/'summary.json').read_text(encoding='utf-8'))
        summary['coordinate_mode']='strict'
        runner.atomic(output/'summary.json',summary)
        with self.assertRaisesRegex(ValueError,'coordinate'):
            runner.validate_evaluation(output,cases,0,'a'*64)

    def test_v4_public_report_discloses_pose_failure_and_diagnostic_timeout(self):
        trial=runner.Trial.__new__(runner.Trial)
        trial.run=self.root
        trial.state=dict(state='completed',scoring_protocol='fusion-step-adapted-v4.0',coordinate_mode='strict',
            cases=[dict(case_id='Q01',state='evaluation_complete',evaluation_status='ok',final_score=1.,
                execution=dict(elapsed_s=2),evaluation_execution=dict(status='timed_out'),
                measurement_weight_coverage=1.,diagnostic_unavailable_metrics=['section'],pose=dict(status='diagnostic_unreliable'))])
        trial.report()
        text=(self.root/'report.md').read_text(encoding='utf-8')
        self.assertIn('diagnostic_unreliable',text)
        self.assertIn('timed_out',text)
        self.assertIn('section',text)
        self.assertIn('100.0%',text)

    def test_v4_diagnostic_timeout_preserves_published_core_score_with_exit_two(self):
        output,cases=self.publication()
        summary=json.loads((output/'summary.json').read_text(encoding='utf-8'))
        summary['cases'][0]['execution']=dict(status='timed_out',timed_out=True,returncode=1)
        runner.atomic(output/'summary.json',summary)
        runner.atomic(output/'part/aggregate.json',summary['cases'][0])
        validated=runner.validate_evaluation(output,cases,2,'a'*64,coordinate_mode='geometry')
        self.assertEqual(validated['cases'][0]['final_score'],1.)
        self.assertEqual(validated['cases'][0]['execution']['status'],'timed_out')


if __name__ == '__main__':
    unittest.main()

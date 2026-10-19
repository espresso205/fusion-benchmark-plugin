"""End-to-end tests exercise the actual CLI, worker, geometry and persisted results."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import time
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from src.io_utils import sha
import evaluate
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox,BRepPrimAPI_MakeSphere
from OCP.BRep import BRep_Builder
from OCP.TopoDS import TopoDS_Compound
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
from OCP.gp import gp_Pnt,gp_Pln,gp_Dir
from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        p = self.root/'box.step'
        writer = STEPControl_Writer()
        writer.Transfer(BRepPrimAPI_MakeBox(10,20,30).Shape(), STEPControl_AsIs)
        writer.Write(str(p))
        ann = self.root/'annotations.json'
        ann.write_text(json.dumps({'gt_sha256': sha(p),
            'items':[{'id':'width','type':'plane_pair','axis':'x','nominal':10,'tol':0.01}],
            'sections':[{'id':'mid','axis':'z','offset':15}]}), encoding='utf-8')
        self.case = {'case':'box', 'gt':str(p), 'candidate':str(p), 'annotations':str(ann),
                     'expected_hashes': {'gt':sha(p),'candidate':sha(p),'annotations':sha(ann)}}
        self.cases = self.root/'cases.json'
        self.cases.write_text(json.dumps({'cases':[self.case]}), encoding='utf-8')
        self.output = self.root/'run'

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(BASE/'evaluate.py'), '--cases', str(self.cases),
            '--output', str(self.output), '--protocol', 'fusion-step-adapted-v2.0', *args], capture_output=True, text=True, timeout=60, cwd=BASE)

    def test_complete_analytic_box_scores_one_and_does_not_write_beside_inputs(self):
        before = {p.name:sha(p) for p in self.root.iterdir() if p.is_file()}
        run = self.run_cli()
        self.assertEqual(run.returncode, 0, run.stdout+run.stderr)
        data = json.loads((self.output/'summary.json').read_text(encoding='utf-8'))
        self.assertAlmostEqual(data['batch_mean'], 1.0, places=7)
        case = data['cases'][0]
        self.assertEqual(case['execution']['status'],'completed')
        self.assertEqual(case['geometry']['reference_status'],'valid')
        self.assertEqual(case['geometry']['candidate_status'],'valid')
        self.assertEqual(case['scores'][case['protocol_id']]['coverage'],
                         {'scored_modules':7,'required_modules':7})
        self.assertIn('valid/valid', (self.output/'report.md').read_text(encoding='utf-8'))
        after = {p.name:sha(p) for p in self.root.iterdir() if p.is_file()}
        self.assertEqual(before, after)

    def test_existing_output_is_refused_and_preserved(self):
        self.output.mkdir()
        marker = self.output/'marker.txt'
        marker.write_text('preserve', encoding='utf-8')
        run = self.run_cli()
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('output already exists', run.stderr)
        self.assertEqual(marker.read_text(), 'preserve')
        self.assertEqual(list(self.output.iterdir()), [marker])

    def test_worker_timeout_is_an_evaluator_failure_not_a_model_zero(self):
        run = self.run_cli('--timeout', '0.001')
        self.assertEqual(run.returncode, 2, run.stdout+run.stderr)
        data = json.loads((self.output/'summary.json').read_text(encoding='utf-8'))
        self.assertEqual(data['cases'][0]['status'], 'evaluator_error')
        self.assertEqual(data['cases'][0]['execution']['status'],'timed_out')
        self.assertIsNone(data['cases'][0]['final_score'])
        self.assertIsNone(data['batch_mean'])

    def test_unexpected_case_exception_publishes_complete_failure_envelope(self):
        args=[str(BASE/'evaluate.py'),'--cases',str(self.cases),'--output',str(self.output), '--protocol', 'fusion-step-adapted-v2.0']
        with patch.object(sys,'argv',args), patch.object(evaluate,'run_case',side_effect=RuntimeError('injected staging failure')):
            code=evaluate.main()

        self.assertEqual(code,2)
        data=json.loads((self.output/'summary.json').read_text(encoding='utf-8'))
        case=data['cases'][0]
        self.assertEqual(case['execution']['status'],'failed')
        self.assertIsNone(case['quality_status'])
        self.assertEqual(case['geometry']['reference_status'],'unknown')
        self.assertEqual(case['scores'][case['protocol_id']]['coverage'],
                         {'scored_modules':0,'required_modules':7})
        self.assertTrue((self.output/'box/aggregate.json').is_file())
        self.assertTrue((self.output/'report.md').is_file())

    def test_unsupported_reference_is_not_hidden_by_invalid_candidate(self):
        compound=TopoDS_Compound();builder=BRep_Builder();builder.MakeCompound(compound)
        builder.Add(compound,BRepPrimAPI_MakeBox(10,20,30).Shape())
        builder.Add(compound,BRepPrimAPI_MakeSphere(gp_Pnt(50,50,50),5).Shape())
        gt=self.root/'compound.step';candidate=self.root/'open_face.step'
        for p,s in [(gt,compound),(candidate,BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(),gp_Dir(0,0,1)),0,10,0,10).Shape())]:
            w=STEPControl_Writer();w.Transfer(s,STEPControl_AsIs);w.Write(str(p))
        ann=Path(self.case['annotations']);data=json.loads(ann.read_text());data['gt_sha256']=sha(gt);ann.write_text(json.dumps(data))
        self.case.update(gt=str(gt),candidate=str(candidate),expected_hashes={'gt':sha(gt),'candidate':sha(candidate),'annotations':sha(ann)})
        self.cases.write_text(json.dumps({'cases':[self.case]}))
        run=self.run_cli()
        self.assertEqual(run.returncode,2,run.stdout+run.stderr)
        out=json.loads((self.output/'box/aggregate.json').read_text())
        self.assertIsNone(out['final_score'])
        pre=json.loads((self.output/'box/reference_preflight.json').read_text())
        self.assertEqual(pre['face_match']['status'],'unsupported')

    def test_source_mutation_invalidates_persisted_case_and_batch(self):
        proc=subprocess.Popen([sys.executable,str(BASE/'evaluate.py'),'--cases',str(self.cases),'--output',str(self.output)],
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,cwd=BASE)
        try:
            deadline=time.monotonic()+20
            while not list(self.output.glob('box/events/*')) and proc.poll() is None and time.monotonic()<deadline:
                time.sleep(0.02)
            self.assertTrue(list(self.output.glob('box/events/*')),'worker did not start')
            Path(self.case['candidate']).write_text('changed during evaluation')
            stdout,stderr=proc.communicate(timeout=60)
            self.assertEqual(proc.returncode,2,stdout+stderr)
            final=json.loads((self.output/'box/aggregate.json').read_text())
            self.assertEqual(final['status'],'evaluator_error')
            self.assertIsNone(final['final_score'])
        finally:
            if proc.poll() is None:
                proc.kill();proc.wait()


if __name__ == '__main__':
    unittest.main()

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

BASE=Path(__file__).resolve().parents[1]


class RoutingV4Tests(unittest.TestCase):
    def test_default_cli_exposes_coordinate_mode(self):
        result=subprocess.run([sys.executable,'-B',str(BASE/'evaluate.py'),'--help'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0)
        self.assertIn('--coordinate-mode',result.stdout)

    def test_explicit_v3_keeps_legacy_help_and_rejects_alignment_flag(self):
        result=subprocess.run([sys.executable,'-B',str(BASE/'evaluate.py'),'--protocol','fusion-step-adapted-v3.0','--help'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0)
        self.assertNotIn('--coordinate-mode',result.stdout)


@unittest.skipUnless(os.environ.get('RUN_GEOMETRY_TESTS')=='1','requires CAD runtime')
class ExecutionV4GeometryTests(unittest.TestCase):
    def test_identical_box_end_to_end_geometry_and_strict(self):
        self.assertTrue((BASE/'evaluate_v4.py').is_file(),'v4 CLI must exist')
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.STEPControl import STEPControl_Writer,STEPControl_AsIs
        from src.io_utils import sha
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            gt=root/'gt.step'
            writer=STEPControl_Writer(); writer.Transfer(BRepPrimAPI_MakeBox(10,20,30).Shape(),STEPControl_AsIs); writer.Write(str(gt))
            ann=root/'ann.json'; ann.write_text(json.dumps({'gt_sha256':sha(gt),'items':[{'id':'z','type':'plane_pair','axis':'z','nominal':30,'tol':.01}],
                'sections':[{'id':'middle','axis':'z','offset':15}]}),encoding='utf-8')
            hashes={'gt':sha(gt),'candidate':sha(gt),'annotations':sha(ann)}
            cases=root/'cases.json'; cases.write_text(json.dumps({'cases':[{'case':'box','candidate':str(gt),'gt':str(gt),'annotations':str(ann),'expected_hashes':hashes}]}),encoding='utf-8')
            for mode in ('geometry','strict'):
                output=root/mode
                run=subprocess.run([sys.executable,'-B',str(BASE/'evaluate.py'),'--cases',str(cases),'--output',str(output),
                    '--coordinate-mode',mode,'--timeout','120'],capture_output=True,text=True,timeout=160)
                self.assertEqual(run.returncode,0,run.stdout+run.stderr)
                summary=json.loads((output/'summary.json').read_text(encoding='utf-8'))
                row=summary['cases'][0]
                self.assertEqual(summary['protocol_id'],'fusion-step-adapted-v4.0')
                self.assertEqual(summary['coordinate_mode'],mode)
                self.assertTrue(summary['original_inputs_unchanged'])
                self.assertEqual(row['measurement_weight_coverage'],1.)
                self.assertGreater(row['final_score'],.94)
                self.assertTrue(row['pose']['raw']['evaluation_candidate_sha256'])
                self.assertIsNone(row['metrics']['distance']['awarded_score'])
                self.assertTrue((output/'report.md').is_file())

    def test_strict_core_results_exist_before_stalled_alignment(self):
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs
        from src.io_utils import sha
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            for name in ('inputs', 'results', 'events', 'diagnostics', 'derived'):
                (folder/name).mkdir()
            gt = folder/'inputs'/'gt.step'
            candidate = folder/'inputs'/'candidate.step'
            writer = STEPControl_Writer()
            writer.Transfer(BRepPrimAPI_MakeBox(10,20,30).Shape(), STEPControl_AsIs)
            writer.Write(str(gt))
            candidate.write_bytes(gt.read_bytes() + b'\n')
            ann = folder/'inputs'/'annotations.json'
            ann.write_text(json.dumps({'gt_sha256':sha(gt),'items':[
                {'id':'z','type':'plane_pair','axis':'z','nominal':30,'tol':.01}],
                'sections':[{'id':'middle','axis':'z','offset':15}]}), encoding='utf-8')
            hashes = {'gt':sha(gt),'candidate':sha(candidate),'annotations':sha(ann)}
            (folder/'job.json').write_text(json.dumps({'case':'box','input_hashes':hashes,
                'run_manifest_sha256':'f'*64,'protocol_id':'fusion-step-adapted-v4.0',
                'coordinate_mode':'strict'}),encoding='utf-8')
            script = (f"import sys,time; sys.path.insert(0,{str(BASE)!r}); "
                      "from src import geometry_v4; "
                      "geometry_v4.align_step=lambda *args,**kwargs: time.sleep(60); "
                      "import worker_v4; worker_v4.main()")
            proc = subprocess.Popen([sys.executable,'-B','-c',script,'--case-dir',str(folder)],
                                    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic()+30
                while not (folder/'events'/'pose_started.json').exists() and time.monotonic()<deadline:
                    if proc.poll() is not None:
                        self.fail(f'worker exited before pose diagnostic: {proc.returncode}')
                    time.sleep(.05)
                self.assertTrue((folder/'events'/'pose_started.json').exists(), 'pose did not start')
                for name in ('mass','shape','dimension'):
                    result = json.loads((folder/'results'/f'{name}.json').read_text(encoding='utf-8'))
                    self.assertEqual(result['status'],'ok',name)
                    self.assertIsNotNone(result['score'],name)
            finally:
                proc.kill()
                proc.wait(timeout=5)


class RunCaseReaderTests(unittest.TestCase):
    def _run_with_diagnostic(self, diagnostic_bytes, *, mode='strict', diagnostic_name='distance'):
        import evaluate_v4
        from src.io_utils import sha
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gt = root/'gt.step'; gt.write_bytes(b'gt bytes')
            candidate = root/'candidate.step'; candidate.write_bytes(b'candidate bytes')
            ann = root/'annotations.json'; ann.write_text('{}',encoding='utf-8')
            hashes = {'gt':sha(gt),'candidate':sha(candidate),'annotations':sha(ann)}
            row = {'case':'case1','gt':gt,'candidate':candidate,'annotations':ann,
                   'expected_hashes':hashes}
            output = root/'output'; output.mkdir()
            manifest_hash = 'f'*64

            class StubWorker:
                returncode = 0
                def __init__(self, command, **kwargs):
                    self.folder = Path(command[-1])
                def wait(self, timeout=None):
                    for name in ('validity','pose','shape','dimension','mass'):
                        raw = {'candidate':{'healthy':True},'gt':{'healthy':True}} if name=='validity' else {}
                        if name=='pose': raw = {'evaluation_candidate_sha256':hashes['candidate']}
                        quality = {'measurement_valid':True}
                        if name=='validity': quality['candidate_valid'] = True
                        data = {'schema_version':'4.0','protocol_id':'fusion-step-adapted-v4.0',
                            'case':'case1','coordinate_mode':mode,'input_hashes':hashes,
                            'evaluation_input_hashes':hashes,'run_manifest_sha256':manifest_hash,
                            'status':'ok','score':1.,'quality':quality,'raw':raw}
                        if name=='pose':
                            data.update(status='diagnostic_unreliable',score=None)
                            quality['measurement_valid'] = False
                        (self.folder/'results'/f'{name}.json').write_text(json.dumps(data),encoding='utf-8')
                    (self.folder/'results'/f'{diagnostic_name}.json').write_bytes(diagnostic_bytes)
                    (self.folder/'worker_complete.json').write_text(json.dumps({
                        'status':'ok','input_hashes_after':hashes}),encoding='utf-8')
                    return 0

            with patch.object(evaluate_v4.subprocess,'Popen',StubWorker):
                return evaluate_v4.run_case(row,output,manifest_hash,None,30,mode)

    def test_malformed_diagnostic_json_keeps_core_awards(self):
        result = self._run_with_diagnostic(b'{malformed json')
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['final_score'],1.)
        self.assertEqual(result['measurement_weight_coverage'],1.)
        self.assertIn('distance',result['diagnostic_unavailable_metrics'])
        self.assertTrue(any(reason.get('module')=='distance' and 'parse' in reason.get('detail','').lower()
                            for reason in result['reasons']))

    def test_non_object_coordinate_diagnostic_keeps_core_awards(self):
        result = self._run_with_diagnostic(b'[]', diagnostic_name='world_shape')
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['final_score'],1.)
        self.assertEqual(result['coordinate_diagnostics']['status'],'diagnostic_unreliable')

    def test_diagnostic_identity_mismatch_still_revokes_core_awards(self):
        data = {'schema_version':'4.0','protocol_id':'fusion-step-adapted-v4.0',
                'case':'case1','coordinate_mode':'geometry','input_hashes':{
                    'gt':'a'*64,'candidate':'b'*64,'annotations':'c'*64},
                'evaluation_input_hashes':{},'run_manifest_sha256':'f'*64,
                'status':'ok','score':1.,'quality':{'measurement_valid':True},'raw':{}}
        result = self._run_with_diagnostic(json.dumps(data).encode())
        self.assertEqual(result['status'],'integrity_error')
        self.assertIsNone(result['final_score'])


if __name__=='__main__': unittest.main()

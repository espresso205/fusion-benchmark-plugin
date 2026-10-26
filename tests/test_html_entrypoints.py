"""An actual evaluation must survive a failed presentation step."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


@unittest.skipUnless(os.environ.get('RUN_GEOMETRY_TESTS')=='1','real geometry integration')
class EntryTests(unittest.TestCase):
    def test_html_exception_does_not_change_successful_cli_score(self):
        import evaluate_v4
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.STEPControl import STEPControl_Writer,STEPControl_AsIs
        from src.io_utils import sha
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); step=root/'box.step'
            writer=STEPControl_Writer(); writer.Transfer(BRepPrimAPI_MakeBox(10,20,30).Shape(),STEPControl_AsIs); writer.Write(str(step))
            ann=root/'annotations.json'
            ann.write_text(json.dumps({'gt_sha256':sha(step),'items':[{'id':'z','type':'plane_pair','axis':'z','nominal':30,'tol':.01}]}),encoding='utf-8')
            cases=root/'cases.json'
            cases.write_text(json.dumps({'cases':[{'case':'box','gt':str(step),'candidate':str(step),'annotations':str(ann),
                'expected_hashes':{'gt':sha(step),'candidate':sha(step),'annotations':sha(ann)}}]}),encoding='utf-8')
            for target in ('build_report','try_build_report'):
                with self.subTest(failing_renderer=target):
                    output=root/target
                    with patch('src.html_reporting.'+target,side_effect=RuntimeError('display failure')):
                        code=evaluate_v4.main(['--cases',str(cases),'--output',str(output),'--timeout','120'])
                    self.assertEqual(code,0)
                    result=json.loads((output/'summary.json').read_text(encoding='utf-8'))
                    self.assertAlmostEqual(result['cases'][0]['final_score'],1.,places=5)
                    self.assertTrue((output/'report.md').is_file())
                    if target=='build_report':
                        status=json.loads((output/'html-report-generation.json').read_text(encoding='utf-8'))
                        self.assertEqual(status['status'],'failed')


if __name__=='__main__': unittest.main()

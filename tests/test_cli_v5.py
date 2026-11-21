import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

BASE=Path(__file__).resolve().parents[1]
class CLITests(unittest.TestCase):
    def test_missing_acceptance_retains_measurements_and_reports_no_formal_total(self):
        self.assertTrue((BASE/'evaluate_v5.py').is_file(),'v5 CLI must exist')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            fixture=subprocess.run([sys.executable,str(BASE/'examples/make_box_fixture.py'),'--output',str(root/'input')],capture_output=True)
            self.assertEqual(fixture.returncode,0,fixture.stderr.decode(errors='replace'))
            proc=subprocess.run([sys.executable,str(BASE/'evaluate_v5.py'),'--cases',str(root/'input/cases.json'),
                '--output',str(root/'out'),'--coordinate-mode','strict','--timeout','180','--stage-timeout','25'],capture_output=True,timeout=200)
            self.assertIn(proc.returncode,(0,2),proc.stderr.decode(errors='replace'))
            summary=json.loads((root/'out/summary.json').read_text(encoding='utf-8'))
            result=summary['cases'][0]
            self.assertEqual(result['candidate_basic']['status'],'pass')
            self.assertEqual(result['reference_preflight']['status'],'not_run')
            self.assertIsNone(result['final_score'])
            self.assertEqual(result['measurement_count'],3)
            self.assertEqual(result['module_scores']['dimension'],1.)
            self.assertEqual(summary['complete_case_ids'],[])
            self.assertIn('not_run',(root/'out/report.md').read_text(encoding='utf-8'))
            spec=importlib.util.spec_from_file_location('cli_v5_validation',BASE/'fusion_pipeline/v5_validation.py')
            validator=importlib.util.module_from_spec(spec);spec.loader.exec_module(validator)
            from src.io_utils import sha
            verified=validator.validate_publication(root/'out',root/'input/cases.json',proc.returncode,sha(BASE/'weights_v5.json'))
            self.assertEqual(verified,summary)

if __name__=='__main__':unittest.main()

import importlib.util
from pathlib import Path
import sys
import tempfile
import time
import unittest

AVAILABLE=importlib.util.find_spec('src.stages_v5') is not None
if AVAILABLE: from src.stages_v5 import run_isolated

class StageTests(unittest.TestCase):
    def setUp(self): self.assertTrue(AVAILABLE,'isolated stage runner must exist')
    def test_hung_stage_is_killed_and_next_stage_completes(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            result=run_isolated([sys.executable,'-c','import time; time.sleep(30)'],folder/'hang.log',.3)
            self.assertEqual(result['status'],'timeout')
            self.assertLess(result['wall_s'],10)
            result=run_isolated([sys.executable,'-c','print(42)'],folder/'next.log',10)
            self.assertEqual(result['status'],'pass')
            self.assertIn('42',(folder/'next.log').read_text())
    def test_crash_is_error_never_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            result=run_isolated([sys.executable,'-c','raise RuntimeError()'],Path(temp)/'x.log',10)
            self.assertEqual(result['status'],'error')
    def test_stage_exception_preserves_completed_strict_progress(self):
        import json
        import worker_v5
        self.assertTrue(hasattr(worker_v5,'failure_result'),'exception must preserve stage progress')
        with tempfile.TemporaryDirectory() as temp:
            progress=Path(temp)/'progress.json'
            progress.write_text(json.dumps({'status':'partial','raw':{'checked_count':1,'total_count':3,
                'issues':[{'entity_id':'first'}],'unchecked_entities':['second','third']}}),encoding='utf-8')
            result=worker_v5.failure_result(RuntimeError('Common failed'),progress)
            self.assertEqual(result['status'],'error')
            self.assertEqual(result['raw']['checked_count'],1)
            self.assertEqual(result['raw']['unchecked_entities'],['second','third'])

if __name__=='__main__': unittest.main()

"""The portable runner must select and preserve the explicit v5 protocol."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PLUGIN = os.environ.get('V5_PLUGIN_SOURCE', str(Path(__file__).resolve().parents[1]))

@unittest.skipUnless(PLUGIN, 'requires staged plugin source')
class PluginV5IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, PLUGIN)
        # Test the review overlay while resolving the rest of the staged
        # plugin's dependencies normally; never mutate the installed plugin.
        path=Path(__file__).resolve().parents[1]/'fusion_pipeline/runner.py'
        spec=importlib.util.spec_from_file_location('fusion_pipeline.runner',path)
        runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
        cls.runner = runner

    def test_runner_identity_covers_validation_helper_import_closure(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            closure=('evaluate_v5.py','src/__init__.py','src/io_utils.py','src/contract.py','src/contract_v5.py','src/stages_v5.py')
            for name in closure:
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('original')
            with patch.object(self.runner,'BASE',root):
                before=self.runner.source_hashes()
                self.assertTrue(set(closure)<=set(before))
                for name in closure:
                    path=root/name;path.write_text('changed')
                    self.assertNotEqual(self.runner.source_hashes()[name],before[name])
                    path.write_text('original')

    def test_explicit_v5_selects_its_weights_and_geometry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'weights_v5.json').write_text('{}')
            protocol, mode, weights = self.runner.scoring_options(
                {'protocol':'fusion-step-adapted-v5.0'}, root)
            self.assertEqual((protocol, mode), ('fusion-step-adapted-v5.0', 'geometry'))
            self.assertEqual(weights.name, 'weights_v5.json')

    def test_snapshot_includes_v5_workers_and_weights(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'source'; source.mkdir()
            names = ('evaluate.py','worker.py','weights.json','evaluate_v5.py','worker_v5.py','weights_v5.json')
            for name in names: (source/name).write_text('{}')
            output = Path(temp) / 'snapshot'
            self.runner.snapshot_evaluator(source,output)
            self.assertTrue((output/'worker_v5.py').is_file())
            self.assertTrue((output/'evaluate_v5.py').is_file())
            self.assertTrue((output/'weights_v5.json').is_file())

    def test_reference_failure_cannot_publish_a_complete_score(self):
        from fusion_pipeline.v5_validation import validate_score_row
        row = self.complete_row()
        row['reference_preflight']['status'] = 'fail'
        with self.assertRaisesRegex(ValueError, 'formal score'):
            validate_score_row(row)

    def test_missing_measurement_cannot_be_converted_to_zero_total(self):
        from fusion_pipeline.v5_validation import validate_score_row
        row = self.complete_row()
        row['metrics']['mass']['measured_score'] = None
        row['metrics']['mass']['status'] = 'timeout'
        with self.assertRaises(ValueError): validate_score_row(row)

    def test_measured_zero_is_a_complete_score(self):
        from fusion_pipeline.v5_validation import validate_score_row
        validate_score_row(self.complete_row())

    @staticmethod
    def complete_row():
        return {'case':'example','candidate_basic':{'status':'pass'},
                'reference_preflight':{'status':'pass','raw':{'acceptance_verified':True}},
                'metrics':{n:{'status':'pass','measured_score':0.0} for n in ('shape','dimension','mass')},
                'final_score':0.0,'partial_lower_bound':0.0,'measurement_weight_coverage':1.0,
                'measurement_count':3,'ranking_eligible':False,'participates_in_scoring':True,
                'score_kind':'complete_geometric_similarity'}

if __name__ == '__main__': unittest.main()

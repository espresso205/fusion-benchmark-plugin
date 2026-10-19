import math
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import dimension_probes as probes
from src.metrics import dimension, mass_properties, directed, mass,validity,StepArtifacts
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs
import numpy as np


class MetricsTests(unittest.TestCase):
    def test_cylinder_just_outside_tolerance_is_not_rounded_into_pass(self):
        row = probes.check_cylinder({'id':'hole','nominal':10.,'tol':0.01,'count':1},
              [{'type':'cylinder','radius':5.00502}])
        self.assertFalse(row['pass'])
        self.assertEqual(row['measured'], 10.01004)

    def test_dimension_gt_self_failure_blocks_score(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'box.step'
            w = STEPControl_Writer()
            w.Transfer(BRepPrimAPI_MakeBox(10,20,30).Shape(), STEPControl_AsIs)
            w.Write(str(p))
            r = dimension(p,p,{'items':[{'id':'width','type':'plane_pair','axis':'x','nominal':11.,'tol':0.01}]})
            self.assertEqual(r['status'], 'reference_invalid')
            self.assertIsNone(r['score'])

    def test_box_mass_and_centroid_inertia_have_analytic_truth(self):
        r = mass_properties(BRepPrimAPI_MakeBox(10,20,30).Shape())
        self.assertAlmostEqual(r['volume_mm3'], 6000., places=8)
        np.testing.assert_allclose(r['centroid_mm'], [5,10,15], atol=1e-12)
        np.testing.assert_allclose(r['principal_moments'], [250000,500000,650000], rtol=1e-12)

    def test_directed_distance_is_in_millimetres(self):
        r = directed(np.array([[0,0,0],[0,0,4]]),np.array([[0,0,1]]))
        self.assertEqual(r['mean'], 2.)
        self.assertEqual(r['max'], 3.)

    def test_q05_unstable_reference_inertia_cannot_be_normal_score(self):
        import json
        if not os.environ.get('FUSION_BENCHMARK_REGRESSION_ROOT'):
            self.skipTest('optional historical regression root not configured')
        case_file = Path(os.environ['FUSION_BENCHMARK_REGRESSION_ROOT']) / 'Q05/cases.json'
        if not case_file.exists():
            self.skipTest('historical regression asset unavailable')
        c = json.loads(case_file.read_text(encoding='utf-8'))['cases'][0]
        r = mass(c['gt'], c['candidate'])
        self.assertEqual(r['status'], 'reference_invalid')
        self.assertIsNone(r['score'])
        self.assertFalse(r['quality']['measurement_valid'])

    def test_q06_spline_bbox_padding_is_not_a_unit_error(self):
        import json
        if not os.environ.get('FUSION_BENCHMARK_REGRESSION_ROOT'):
            self.skipTest('optional historical regression root not configured')
        case_file=Path(os.environ['FUSION_BENCHMARK_REGRESSION_ROOT']) / 'Q06/cases.json'
        if not case_file.exists():self.skipTest('historical regression asset unavailable')
        p=Path(json.loads(case_file.read_text())['cases'][0]['gt'])
        artifact=StepArtifacts(p)
        r=validity(p,p,{'gt':artifact,'candidate':artifact})
        self.assertEqual(r['status'],'ok',r['raw'].get('error'))

    def test_mesh_timeout_on_watertight_brep_is_evaluator_failure(self):
        from unittest.mock import patch
        from dataclasses import dataclass
        from types import SimpleNamespace
        from src import metrics
        # The backend intentionally folds mesh failures into ValidationResult.
        # Inject only that slow backend boundary; run real STEP loading/geometry.
        @dataclass
        class Analysis:
            validation: dict
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'box.step'
            w=STEPControl_Writer();w.Transfer(BRepPrimAPI_MakeBox(10,20,30).Shape(),STEPControl_AsIs);w.Write(str(p))
            for message in ('mesh timeout: tessellation exceeded 120s','mesh exceeds triangle ceiling: 5000001'):
                artifact=SimpleNamespace(analysis=Analysis({'is_valid':False,'is_watertight':True,'topology_errors':[message]}))
                r=validity(p,p,{'gt':artifact,'candidate':artifact})
                self.assertEqual(r['status'],'evaluator_error')
                self.assertIsNone(r['score'])


if __name__ == '__main__':
    unittest.main()

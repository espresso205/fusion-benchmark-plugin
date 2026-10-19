import json
import math
import os
from pathlib import Path
import unittest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox,BRepPrimAPI_MakeCylinder
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.gp import gp_Ax2,gp_Pnt,gp_Dir
from src.section import section_metrics,evaluate_shapes,evaluate


class SectionTests(unittest.TestCase):
    def test_rectangle_exact_area_and_perimeter(self):
        r=section_metrics(BRepPrimAPI_MakeBox(10,20,30).Shape(),'z',15)
        self.assertTrue(r['quality']['measurement_valid'])
        self.assertAlmostEqual(r['area_mm2'],200.,places=8)
        self.assertAlmostEqual(r['perimeter_mm'],60.,places=8)
        self.assertEqual(r['contours'],1)

    def test_annulus_uses_true_curve_area_and_hole_sign(self):
        s=BRepAlgoAPI_Cut(BRepPrimAPI_MakeCylinder(10,20).Shape(),BRepPrimAPI_MakeCylinder(5,20).Shape()).Shape()
        r=section_metrics(s,'z',10)
        self.assertTrue(r['quality']['measurement_valid'])
        self.assertAlmostEqual(r['area_mm2'],75*math.pi,places=6)
        self.assertAlmostEqual(r['perimeter_mm'],30*math.pi,places=6)
        self.assertEqual(r['contours'],2)

    def test_two_holes_have_known_material_area(self):
        s=BRepPrimAPI_MakeBox(20,20,10).Shape()
        for x in (5,15):
            c=BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x,10,0),gp_Dir(0,0,1)),2,10).Shape()
            s=BRepAlgoAPI_Cut(s,c).Shape()
        r=section_metrics(s,'z',5)
        self.assertTrue(r['quality']['measurement_valid'])
        self.assertAlmostEqual(r['area_mm2'],400-8*math.pi,places=6)
        self.assertEqual(r['contours'],3)

    def test_coplanar_reference_cannot_self_certify(self):
        s=BRepPrimAPI_MakeBox(10,20,30).Shape()
        r=evaluate_shapes(s,s,{'sections':[{'id':'bad','axis':'z','offset':0}]})
        self.assertEqual(r['status'],'reference_invalid')
        self.assertIsNone(r['score'])

    def test_empty_reference_requires_explicit_contract(self):
        s=BRepPrimAPI_MakeBox(10,20,30).Shape()
        ann={'sections':[{'id':'outside','axis':'z','offset':40}]}
        self.assertEqual(evaluate_shapes(s,s,ann)['status'],'reference_invalid')
        ann['sections'][0]['expected_empty']=True
        self.assertEqual(evaluate_shapes(s,s,ann)['score'],1.)

    def test_missing_candidate_material_is_detected(self):
        a=BRepPrimAPI_MakeBox(10,20,30).Shape()
        b=BRepPrimAPI_MakeBox(10,20,10).Shape()
        r=evaluate_shapes(a,b,{'sections':[{'id':'mid','axis':'z','offset':15}]})
        self.assertEqual(r['status'],'ok')
        self.assertLess(r['score'],1e-10)

    def test_empty_contract_requires_json_boolean(self):
        s=BRepPrimAPI_MakeBox(10,20,30).Shape()
        for flag in ('false',1,[]):
            r=evaluate_shapes(s,s,{'sections':[{'id':'outside','axis':'z','offset':40,'expected_empty':flag}]})
            self.assertEqual(r['status'],'reference_invalid')
            self.assertIsNone(r['score'])

    def test_q09_reference_does_not_pass_bad_sections(self):
        if not os.environ.get('FUSION_BENCHMARK_REGRESSION_ROOT'):
            self.skipTest('optional historical regression root not configured')
        p=Path(os.environ['FUSION_BENCHMARK_REGRESSION_ROOT']) / 'Q09/cases.json'
        if not p.exists():self.skipTest('historical asset unavailable')
        c=json.loads(p.read_text())['cases'][0]
        ann=json.loads(Path(c['annotations']).read_text())
        r=evaluate(c['gt'],c['gt'],ann)
        self.assertEqual(r['status'],'reference_invalid')
        self.assertIsNone(r['score'])


if __name__=='__main__':unittest.main()

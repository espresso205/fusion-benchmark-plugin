"""Analytic, interference, and measurement-degeneracy regressions for v3."""
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from OCP.BRep import BRep_Builder
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeSolid
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeSphere
from OCP.gp import gp_Pnt, gp_Pln, gp_Dir, gp_Ax2
from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs
from OCP.TopoDS import TopoDS_Compound, TopoDS_Shell
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_FACE

try:
    from src import robust_metrics as robust
except ImportError:
    robust = None


def compound(*shapes):
    out = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(out)
    for shape in shapes:
        builder.Add(out, shape)
    return out


class RobustMetricsTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(robust, 'v3 robust metric module must be implemented')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.box = self.save('box', BRepPrimAPI_MakeBox(10, 20, 30).Shape())

    def save(self, name, shape):
        path = self.root / (name + '.step')
        writer = STEPControl_Writer()
        writer.Transfer(shape, STEPControl_AsIs)
        writer.Write(str(path))
        return path

    def test_box_validation_and_analytic_mass(self):
        checked = robust.validity(self.box, self.box)
        self.assertEqual(checked['status'], 'ok', checked)
        result = robust.mass(self.box, self.box)
        self.assertEqual(result['status'], 'ok', result)
        self.assertAlmostEqual(result['score'], 1, places=7)
        self.assertAlmostEqual(result['raw']['gt']['volume_mm3'], 6000, places=5)
        np.testing.assert_allclose(result['raw']['gt']['principal_moments'], [250000,500000,650000], rtol=1e-6)

    def test_multisolid_positive_interference_is_rejected_but_contact_is_allowed(self):
        a = BRepPrimAPI_MakeBox(10, 10, 10).Shape()
        for name, x, status in [('overlap',5,'candidate_invalid'), ('contact',10,'ok'), ('separate',11,'ok')]:
            with self.subTest(name=name):
                b = BRepPrimAPI_MakeBox(gp_Pnt(x,0,0),10,10,10).Shape()
                path = self.save(name, compound(a,b))
                result = robust.validity(self.box, path)
                self.assertEqual(result['status'], status, result)
                if status != 'ok':
                    self.assertTrue(result['quality']['candidate_invalid_confirmed'])
                    self.assertAlmostEqual(result['raw']['candidate']['interferences'][0]['volume_mm3'],500,places=5)

    def test_nested_solid_is_rejected(self):
        path = self.save('nested',compound(BRepPrimAPI_MakeBox(10,10,10).Shape(),
                                         BRepPrimAPI_MakeBox(gp_Pnt(2,2,2),2,2,2).Shape()))
        result = robust.validity(self.box,path)
        self.assertEqual(result['status'],'candidate_invalid',result)

    def test_geometrically_duplicate_solids_are_rejected(self):
        path=self.save('duplicate',compound(BRepPrimAPI_MakeBox(10,10,10).Shape(),
                                           BRepPrimAPI_MakeBox(10,10,10).Shape()))
        result=robust.validity(self.box,path)
        self.assertEqual(result['status'],'candidate_invalid',result)

    def test_valid_solid_does_not_hide_a_stray_open_face(self):
        face=BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(40,0,0),gp_Dir(1,0,0)),0,10,0,10).Face()
        path=self.save('stray',compound(BRepPrimAPI_MakeBox(10,10,10).Shape(),face))
        result=robust.validity(self.box,path)
        self.assertEqual(result['status'],'candidate_invalid',result)
        self.assertIn('residual_geometry',result['raw']['candidate']['issues'])

    def test_detector_failure_is_not_confirmed_candidate_invalidity(self):
        for error in (RuntimeError('detector failed'), ValueError('invalid detector response')):
            with patch.object(robust,'_self_intersections',side_effect=error):
                result=robust.validity(self.box,self.box)
            self.assertEqual(result['status'],'evaluator_error',result)
            self.assertFalse(result['quality'].get('candidate_invalid_confirmed',False))

    def test_bad_reference_does_not_erase_valid_candidate_geometry(self):
        invalid_reference=self.root/'bad_reference.step'
        invalid_reference.write_text('not a STEP document',encoding='utf-8')
        result=robust.validity(invalid_reference,self.box)
        self.assertEqual(result['status'],'reference_invalid',result)
        self.assertTrue(result['quality']['candidate_valid'])
        self.assertTrue(result['raw']['candidate']['healthy'])

    def test_unreadable_file_is_infrastructure_failure_not_invalid_geometry(self):
        result=robust.validity(self.box,self.root/'missing.step')
        self.assertEqual(result['status'],'evaluator_error',result)
        self.assertFalse(result['quality'].get('candidate_invalid_confirmed',False))

    def test_self_intersection_detector_finds_real_crossing_surfaces(self):
        shell=TopoDS_Shell();builder=BRep_Builder();builder.MakeShell(shell)
        for box in [BRepPrimAPI_MakeBox(10,10,10).Shape(),
                    BRepPrimAPI_MakeBox(gp_Pnt(5,5,5),10,10,10).Shape()]:
            faces=TopExp_Explorer(box,TopAbs_FACE)
            while faces.More():
                builder.Add(shell,faces.Current());faces.Next()
        shape=BRepBuilderAPI_MakeSolid(shell).Solid()
        findings=robust._self_intersections(shape)
        self.assertTrue(findings)
        self.assertTrue(all(f['faulty_subshape_count']>=2 for f in findings))

    def test_sphere_mass_has_analytic_truth_and_size_error_lowers_score(self):
        sphere=self.save('mass_sphere',BRepPrimAPI_MakeSphere(10).Shape())
        larger=self.save('larger_sphere',BRepPrimAPI_MakeSphere(11).Shape())
        result=robust.mass(sphere,larger)
        self.assertEqual(result['status'],'ok',result)
        self.assertAlmostEqual(result['raw']['gt']['volume_mm3'],4000*math.pi/3,delta=0.01)
        np.testing.assert_allclose(result['raw']['gt']['principal_moments'],[160000*math.pi/3]*3,rtol=1e-6)
        self.assertLess(result['score'],0.001)

    def test_generic_sphere_surface_coverage_and_real_shift(self):
        sphere=self.save('sphere',BRepPrimAPI_MakeSphere(10).Shape())
        moved=self.save('moved_sphere',BRepPrimAPI_MakeSphere(gp_Pnt(4,0,0),10).Shape())
        same=robust.face_match(sphere,sphere,{})
        changed=robust.face_match(sphere,moved,{})
        self.assertEqual(same['status'],'ok',same)
        self.assertGreater(same['score'],0.995)
        self.assertLess(changed['score'],0.4)
        self.assertEqual(same['raw']['unassessed_area_fraction'],0)

    def test_surface_normals_preserve_sharp_faces_under_equivalent_reparameterization(self):
        frame=gp_Ax2(gp_Pnt(10,0,0),gp_Dir(0,0,1),gp_Dir(0,1,0))
        equivalent=self.save('equivalent_box',BRepPrimAPI_MakeBox(frame,20,10,30).Shape())
        result=robust.face_match(self.box,equivalent,{})
        self.assertEqual(result['status'],'ok',result)
        self.assertGreater(result['score'],0.999)

    def test_coplanar_section_uses_fixed_two_sided_definition(self):
        ann={'sections':[{'id':'bottom','axis':'z','offset':0}]}
        result=robust.section(self.box,self.box,ann)
        self.assertEqual(result['status'],'ok',result)
        self.assertAlmostEqual(result['score'],1,places=7)
        positions=result['raw']['sections'][0]['positions_mm']
        self.assertLess(positions[0],0)
        self.assertGreater(positions[1],0)
        shorter=self.save('shorter',BRepPrimAPI_MakeBox(5,20,30).Shape())
        changed=robust.section(self.box,shorter,ann)
        self.assertEqual(changed['status'],'ok',changed)
        self.assertLess(changed['score'],0.51)
        self.assertEqual(changed['raw']['sections'][0]['positions_mm'],positions)

    def test_section_mesh_fallback_preserves_plane_and_hole_material(self):
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
        ring=self.save('ring',BRepAlgoAPI_Cut(BRepPrimAPI_MakeCylinder(10,20).Shape(),
                                            BRepPrimAPI_MakeCylinder(5,20).Shape()).Shape())
        ann={'sections':[{'id':'mid','axis':'z','offset':10}]}
        failed={'quality':{'measurement_valid':False,'issues':['injected_kernel_failure']}}
        with patch.object(robust,'_exact_section',return_value=failed):
            result=robust.section(ring,ring,ann)
        self.assertEqual(result['status'],'ok',result)
        self.assertAlmostEqual(result['score'],1,places=7)
        measured=result['raw']['sections'][0]['sides'][0]['gt']['area_mm2']
        self.assertAlmostEqual(measured,75*math.pi,delta=0.2)

    def test_section_kernel_exception_uses_same_plane_mesh_fallback(self):
        ann={'sections':[{'id':'middle','axis':'z','offset':15}]}
        with patch.object(robust,'_exact_section',side_effect=RuntimeError('OCCT operation failed')):
            result=robust.section(self.box,self.box,ann)
        self.assertEqual(result['status'],'ok',result)
        self.assertAlmostEqual(result['raw']['sections'][0]['sides'][0]['gt']['area_mm2'],200,places=6)


if __name__ == '__main__':
    unittest.main()

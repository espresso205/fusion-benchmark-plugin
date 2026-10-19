import math
import unittest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox,BRepPrimAPI_MakeCylinder,BRepPrimAPI_MakeSphere,BRepPrimAPI_MakeTorus
from OCP.gp import gp_Pnt,gp_Trsf,gp_Vec
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from src.face_match import pair_errors,evaluate_shapes


class FaceTests(unittest.TestCase):
    def test_plane_normal_and_offset_joint_sign_are_equivalent(self):
        a={'type':'plane','dir':[0,0,1],'offset':5}
        b={'type':'plane','dir':[0,0,-1],'offset':-5}
        self.assertEqual(pair_errors(a,b,False)['failed'],[])
        b['offset']=5
        self.assertIn('plane_offset_mm',pair_errors(a,b,False)['failed'])

    def test_cylinder_axis_sign_and_axis_origin_do_not_matter(self):
        a={'type':'cylinder','dir':[0,0,1],'loc':[2,3,0],'radius':5}
        b={'type':'cylinder','dir':[0,0,-1],'loc':[2,3,20],'radius':5}
        self.assertEqual(pair_errors(a,b,False)['failed'],[])
        b['radius']=6
        self.assertIn('radius_mm',pair_errors(a,b,False)['failed'])

    def test_identical_box_faces_match(self):
        s=BRepPrimAPI_MakeBox(10,20,30).Shape()
        self.assertEqual(evaluate_shapes(s,s)['score'],1.)

    def test_coaxial_shift_cannot_match_infinite_support_only(self):
        a=BRepPrimAPI_MakeCylinder(5,10).Shape()
        tr=gp_Trsf();tr.SetTranslation(gp_Vec(0,0,3))
        b=BRepBuilderAPI_Transform(a,tr,True).Shape()
        out=evaluate_shapes(a,b)
        self.assertLess(out['score'],1.)
        self.assertGreater(out['raw']['support_only_f1'],out['score'])

    def test_torus_translation_is_detected(self):
        a=BRepPrimAPI_MakeTorus(10,2).Shape()
        tr=gp_Trsf();tr.SetTranslation(gp_Vec(1,0,0))
        b=BRepBuilderAPI_Transform(a,tr,True).Shape()
        self.assertEqual(evaluate_shapes(a,b)['score'],0.)

    def test_sphere_is_explicitly_unsupported_not_none_with_ok(self):
        s=BRepPrimAPI_MakeSphere(5).Shape()
        r=evaluate_shapes(s,s)
        self.assertEqual(r['status'],'unsupported')
        self.assertIsNone(r['score'])
        self.assertEqual(r['raw']['unsupported']['gt']['count'],1)


if __name__=='__main__':unittest.main()

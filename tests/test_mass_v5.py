import importlib.util
import unittest
from unittest.mock import patch
from types import SimpleNamespace
AVAILABLE=importlib.util.find_spec('src.mass_v5') is not None
if AVAILABLE:from src.mass_v5 import measure_instances

class MassTests(unittest.TestCase):
    def test_additional_refinement_reaches_budget_instead_of_discarding_finite_mass(self):
        self.assertTrue(AVAILABLE,'v5 additive mass must exist')
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.BRepGProp import BRepGProp
        from OCP.GProp import GProp_GProps
        shape=BRepPrimAPI_MakeBox(1,1,1).Shape()
        exact=GProp_GProps();BRepGProp.VolumeProperties_s(shape,exact)
        # Controlled integration error: the third round is still outside the
        # frozen tolerance, and only later numerical refinement stabilizes it.
        scales=iter([1.002,1.00002,1.000002,1.0000001,1.00000005])
        calls=[]
        def integration(shape,props,eps,*args):
            calls.append(eps);props.Add(exact,next(scales));return 0.
        with patch('src.mass_v5.BRepGProp',SimpleNamespace(VolumeProperties_s=integration)):
            result=measure_instances([{'id':'part','shape':shape}])
        self.assertTrue(result['converged'])
        self.assertEqual(len(calls),5)
        self.assertLess(result['spread']['inertia_relative'],1e-6)
        self.assertAlmostEqual(result['volume_mm3'],1.00000005)

    def test_finite_refinement_exhaustion_does_not_relax_budget(self):
        self.assertTrue(AVAILABLE,'v5 additive mass must exist')
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.BRepGProp import BRepGProp
        from OCP.GProp import GProp_GProps
        shape=BRepPrimAPI_MakeBox(1,1,1).Shape()
        exact=GProp_GProps();BRepGProp.VolumeProperties_s(shape,exact)
        scales=iter([1.5,1.4,1.3,1.2,1.1])
        def integration(shape,props,eps,*args):props.Add(exact,next(scales));return 0.
        with patch('src.mass_v5.BRepGProp',SimpleNamespace(VolumeProperties_s=integration)):
            result=measure_instances([{'id':'part','shape':shape}])
        self.assertFalse(result['converged'])
        self.assertGreater(result['spread']['inertia_relative'],1e-6)

    def test_additive_mass_preserves_translated_instance_inertia_and_duplicates(self):
        self.assertTrue(AVAILABLE,'v5 additive mass must exist')
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.gp import gp_Pnt
        parts=[{'id':str(i),'shape':BRepPrimAPI_MakeBox(gp_Pnt(x,0,0),1,1,1).Shape()} for i,x in enumerate((0,10))]
        measured=measure_instances(parts)
        self.assertTrue(measured['converged'])
        self.assertAlmostEqual(measured['volume_mm3'],2.)
        self.assertAlmostEqual(measured['centroid_mm'][0],5.5)
        self.assertAlmostEqual(measured['principal_moments'][0],1/3)
        self.assertAlmostEqual(measured['principal_moments'][2],50+1/3)
        repeated=measure_instances([parts[0],parts[0]])
        self.assertAlmostEqual(repeated['volume_mm3'],2.)

if __name__=='__main__':unittest.main()

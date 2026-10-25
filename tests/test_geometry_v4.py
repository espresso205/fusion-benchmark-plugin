"""Behavioral checks for the v4 geometry adapter on analytic STEP solids."""
import hashlib
import math
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.Interface import Interface_Static
from OCP.gp import gp_Trsf, gp_Vec
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

from src import geometry_v4


def write_step(shape, path):
    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_AsIs)
    if int(writer.Write(str(path))) != 1:
        raise RuntimeError('STEP export failed')


def asymmetric_part():
    base = BRepPrimAPI_MakeBox(13, 23, 31).Shape()
    arm = BRepPrimAPI_MakeBox(5, 7, 42).Shape()
    move = gp_Trsf()
    move.SetTranslationPart(gp_Vec(13, 4, 2))
    arm = BRepBuilderAPI_Transform(arm, move, True).Shape()
    fuse = BRepAlgoAPI_Fuse(base, arm)
    fuse.Build()
    return fuse.Shape()


class GeometryV4Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.gt = self.root / 'gt.step'
        self.candidate = self.root / 'candidate.step'

    def test_geometry_pose_recovers_rotation_translation_without_changing_original(self):
        shape = asymmetric_part()
        write_step(shape, self.gt)
        transform = gp_Trsf()
        transform.SetValues(0, -1, 0, 87, 1, 0, 0, -39, 0, 0, 1, 14)
        write_step(BRepBuilderAPI_Transform(shape, transform, True).Shape(), self.candidate)
        original_hash = hashlib.sha256(self.candidate.read_bytes()).hexdigest()

        evaluated, pose = geometry_v4.prepare_pose(
            self.gt, self.candidate, self.root / 'work', 'geometry')

        self.assertEqual(pose['status'], 'ok')
        self.assertNotEqual(evaluated, self.candidate)
        self.assertEqual(hashlib.sha256(self.candidate.read_bytes()).hexdigest(), original_hash)
        self.assertEqual(pose['raw']['candidate_sha256'], original_hash)
        self.assertEqual(pose['raw']['evaluation_candidate_sha256'],
                         hashlib.sha256(evaluated.read_bytes()).hexdigest())
        self.assertEqual(pose['raw']['aligned_candidate_sha256'],
                         pose['raw']['evaluation_candidate_sha256'])
        self.assertAlmostEqual(pose['raw']['rotation_determinant'], 1, places=5)
        self.assertLess(pose['raw']['rotation_orthogonality_error'], 1e-5)
        self.assertEqual(pose['raw']['seed'], 0)
        self.assertGreater(pose['raw']['rotation_angle_deg'], 30)
        self.assertGreater(pose['raw']['translation_norm_mm'], 20)

        aligned = geometry_v4.shape(self.gt, evaluated, self.root / 'shape')
        fixed = geometry_v4.shape(self.gt, self.candidate, self.root / 'fixed')
        self.assertEqual(aligned['status'], 'ok')
        self.assertGreater(aligned['score'], fixed['score'] + .3)
        self.assertGreater(aligned['raw']['volumetric_iou'], .8)
        self.assertGreater(aligned['raw']['surface_f1'], .8)
        self.assertEqual(aligned['raw']['scale_method'], 'AddOptimal(useTriangulation=False)')

    def test_pose_reexports_source_when_upstream_step_drift_exceeds_volume_budget(self):
        write_step(BRepPrimAPI_MakeBox(10, 20, 30).Shape(), self.gt)
        self.candidate.write_bytes(self.gt.read_bytes() + b'\n')
        original_hash = hashlib.sha256(self.candidate.read_bytes()).hexdigest()

        def drifting_alignment(source, target, output, seed):
            self.assertEqual(seed, 0)
            write_step(BRepPrimAPI_MakeBox(10.0002, 20, 30).Shape(), output)
            return SimpleNamespace(rotation=np.eye(3), translation=np.zeros(3),
                                   rmse=0., output_path=Path(output))

        with patch('src.geometry_v4.align_step', side_effect=drifting_alignment):
            evaluated, pose = geometry_v4.prepare_pose(
                self.gt, self.candidate, self.root / 'work', 'geometry')

        self.assertEqual(pose['status'], 'ok', pose['raw'].get('error'))
        self.assertEqual(pose['raw']['export']['precision_mode'], 'GREATEST')
        self.assertLess(pose['raw']['volume_evidence']['relative_change'], 1e-10)
        self.assertEqual(pose['raw']['evaluation_candidate_sha256'],
                         hashlib.sha256(evaluated.read_bytes()).hexdigest())
        self.assertEqual(hashlib.sha256(self.candidate.read_bytes()).hexdigest(), original_hash)
        self.assertFalse((self.root / 'work' / 'candidate_alignment_unverified.step').exists())

    def test_pose_rejects_volume_change_in_controlled_export(self):
        write_step(BRepPrimAPI_MakeBox(10, 20, 30).Shape(), self.gt)
        self.candidate.write_bytes(self.gt.read_bytes() + b'\n')

        def identity_alignment(source, target, output, seed):
            Path(output).write_bytes(self.candidate.read_bytes())
            return SimpleNamespace(rotation=np.eye(3), translation=np.zeros(3),
                                   rmse=0., output_path=Path(output))

        def scaled_export(source, rotation, translation, target):
            write_step(BRepPrimAPI_MakeBox(12, 20, 30).Shape(), target)
            return {'strategy': 'test_scaled_output'}

        with patch('src.geometry_v4.align_step', side_effect=identity_alignment), \
             patch('src.geometry_v4._export_rigid_step', side_effect=scaled_export):
            evaluated, pose = geometry_v4.prepare_pose(
                self.gt, self.candidate, self.root / 'work', 'geometry')
        self.assertIsNone(evaluated)
        self.assertEqual(pose['status'], 'evaluator_error')
        self.assertEqual(pose['raw']['error'], 'alignment export changed solid volume')

    def test_step_writer_settings_restore_when_export_raises(self):
        write_step(BRepPrimAPI_MakeBox(10, 20, 30).Shape(), self.candidate)
        before_mode = Interface_Static.IVal_s('write.precision.mode')

        def failing_export(*args, **kwargs):
            Interface_Static.SetIVal_s('write.precision.mode', 1)
            raise RuntimeError('STEP write failed')

        with patch('build123d.export_step', side_effect=failing_export):
            with self.assertRaisesRegex(RuntimeError, 'STEP write failed'):
                geometry_v4._export_rigid_step(
                    self.candidate, np.eye(3), np.zeros(3), self.root / 'failed.step')
        self.assertEqual(Interface_Static.IVal_s('write.precision.mode'), before_mode)

    def test_pose_restores_writer_settings_changed_by_upstream_alignment(self):
        write_step(BRepPrimAPI_MakeBox(10, 20, 30).Shape(), self.gt)
        self.candidate.write_bytes(self.gt.read_bytes() + b'\n')
        mode_key = 'write.precision.mode'
        value_key = 'write.precision.val'
        curve_key = 'write.surfacecurve.mode'
        original = (Interface_Static.IVal_s(mode_key), Interface_Static.RVal_s(value_key),
                    Interface_Static.IVal_s(curve_key))

        def mutating_alignment(source, target, output, seed):
            Interface_Static.SetIVal_s(mode_key, 0)
            Interface_Static.SetRVal_s(value_key, 0.001)
            Interface_Static.SetIVal_s(curve_key, 1)
            Path(output).write_bytes(self.candidate.read_bytes())
            return SimpleNamespace(rotation=np.eye(3), translation=np.zeros(3),
                                   rmse=0., output_path=Path(output))

        try:
            self.assertTrue(Interface_Static.SetIVal_s(mode_key, 1))
            self.assertTrue(Interface_Static.SetRVal_s(value_key, 1e-7))
            self.assertTrue(Interface_Static.SetIVal_s(curve_key, 0))
            expected = (Interface_Static.IVal_s(mode_key), Interface_Static.RVal_s(value_key),
                        Interface_Static.IVal_s(curve_key))
            self.assertEqual(expected, (1, 1e-7, 0))
            with patch('src.geometry_v4.align_step', side_effect=mutating_alignment):
                evaluated, pose = geometry_v4.prepare_pose(
                    self.gt, self.candidate, self.root / 'work', 'geometry')
            self.assertEqual(pose['status'], 'ok', pose['raw'].get('error'))
            self.assertTrue(evaluated.is_file())
            self.assertEqual((Interface_Static.IVal_s(mode_key),
                              Interface_Static.RVal_s(value_key),
                              Interface_Static.IVal_s(curve_key)), expected)

            def failing_alignment(*args, **kwargs):
                Interface_Static.SetIVal_s(mode_key, 0)
                Interface_Static.SetRVal_s(value_key, 0.001)
                Interface_Static.SetIVal_s(curve_key, 1)
                raise RuntimeError('upstream alignment failed')

            with patch('src.geometry_v4.align_step', side_effect=failing_alignment):
                evaluated, pose = geometry_v4.prepare_pose(
                    self.gt, self.candidate, self.root / 'failed-work', 'geometry')
            self.assertIsNone(evaluated)
            self.assertEqual(pose['status'], 'evaluator_error')
            self.assertEqual((Interface_Static.IVal_s(mode_key),
                              Interface_Static.RVal_s(value_key),
                              Interface_Static.IVal_s(curve_key)), expected)
        finally:
            Interface_Static.SetIVal_s(mode_key, original[0])
            Interface_Static.SetRVal_s(value_key, original[1])
            Interface_Static.SetIVal_s(curve_key, original[2])

    def test_strict_keeps_original_when_alignment_fails(self):
        write_step(BRepPrimAPI_MakeBox(10, 20, 30).Shape(), self.gt)
        write_step(BRepPrimAPI_MakeBox(10, 20, 30).Shape(), self.candidate)
        with patch('src.geometry_v4.align_step', side_effect=RuntimeError('ICP unavailable')):
            evaluated, pose = geometry_v4.prepare_pose(
                self.gt, self.candidate, self.root / 'work', 'strict')
        self.assertEqual(evaluated, self.candidate)
        self.assertEqual(pose['status'], 'diagnostic_unreliable')
        self.assertEqual(pose['raw']['evaluation_candidate_sha256'],
                         hashlib.sha256(self.candidate.read_bytes()).hexdigest())

    def test_identical_bytes_use_bound_identity_derivation(self):
        write_step(BRepPrimAPI_MakeBox(10, 20, 30).Shape(), self.gt)
        self.candidate.write_bytes(self.gt.read_bytes())
        with patch('src.geometry_v4.align_step', side_effect=RuntimeError('must not align')):
            evaluated, pose = geometry_v4.prepare_pose(
                self.gt, self.candidate, self.root / 'work', 'geometry')
        self.assertEqual(pose['status'], 'ok')
        self.assertEqual(evaluated, self.root / 'work' / 'candidate_aligned.step')
        self.assertEqual(pose['raw']['R'], np.eye(3).tolist())
        self.assertEqual(pose['raw']['t'], [0., 0., 0.])
        self.assertTrue(pose['raw']['identity_shortcut'])
        self.assertEqual(pose['raw']['evaluation_candidate_sha256'],
                         hashlib.sha256(evaluated.read_bytes()).hexdigest())

    def test_improper_transform_is_rejected_for_geometry(self):
        write_step(BRepPrimAPI_MakeBox(10, 20, 30).Shape(), self.gt)
        write_step(BRepPrimAPI_MakeBox(10, 20, 30).Shape(), self.candidate)
        class BadAlignment:
            rotation = np.diag([-1., 1., 1.])
            translation = np.zeros(3)
            rmse = 0.
            output_path = self.root / 'work' / 'candidate_aligned.step'
        with patch('src.geometry_v4.align_step', return_value=BadAlignment()):
            evaluated, pose = geometry_v4.prepare_pose(
                self.gt, self.candidate, self.root / 'work', 'geometry')
        self.assertIsNone(evaluated)
        self.assertNotEqual(pose['status'], 'ok')

    def test_pose_alignment_does_not_hide_scaled_part(self):
        write_step(BRepPrimAPI_MakeBox(10, 20, 30).Shape(), self.gt)
        write_step(BRepPrimAPI_MakeBox(12, 24, 36).Shape(), self.candidate)
        evaluated, pose = geometry_v4.prepare_pose(
            self.gt, self.candidate, self.root / 'work', 'geometry')
        self.assertEqual(pose['status'], 'ok')
        self.assertEqual(pose['raw']['evaluation_candidate_sha256'],
                         hashlib.sha256(evaluated.read_bytes()).hexdigest())
        self.assertAlmostEqual(pose['raw']['rotation_determinant'], 1, places=5)
        measured = geometry_v4.shape(self.gt, evaluated, self.root / 'shape')
        self.assertEqual(measured['status'], 'ok')
        self.assertLess(measured['raw']['volumetric_iou'], .7)
        self.assertLess(measured['score'], .9)

    def test_far_origin_box_uses_extent_for_shape_and_distance_scale(self):
        move = gp_Trsf()
        move.SetTranslationPart(gp_Vec(1000, -2000, 3000))
        box = BRepPrimAPI_MakeBox(10, 20, 30).Shape()
        write_step(BRepBuilderAPI_Transform(box, move, True).Shape(), self.gt)

        expected_diagonal = math.sqrt(10**2 + 20**2 + 30**2)
        shape = geometry_v4.shape(self.gt, self.gt, self.root / 'shape')
        distance = geometry_v4.distance(self.gt, self.gt)

        self.assertEqual(shape['status'], 'ok')
        self.assertEqual(distance['status'], 'ok')
        self.assertAlmostEqual(shape['raw']['part_diagonal_mm'], expected_diagonal, places=5)
        self.assertAlmostEqual(distance['raw']['part_diagonal_mm'], expected_diagonal, places=5)
        self.assertAlmostEqual(distance['raw']['tau_mm'], .005 * expected_diagonal, places=5)
        self.assertLess(distance['raw']['symmetric_p99_mm'], .01)

    def test_geometry_discards_unbound_alignment_output(self):
        write_step(BRepPrimAPI_MakeBox(10, 20, 30).Shape(), self.gt)
        write_step(BRepPrimAPI_MakeBox(10, 20, 30).Shape(), self.candidate)
        class WrongOutput:
            rotation = np.eye(3)
            translation = np.zeros(3)
            rmse = 0.
            output_path = self.candidate
        with patch('src.geometry_v4.align_step', return_value=WrongOutput()):
            evaluated, pose = geometry_v4.prepare_pose(
                self.gt, self.candidate, self.root / 'work', 'geometry')
        self.assertIsNone(evaluated)
        self.assertEqual(pose['status'], 'evaluator_error')

    def test_dimension_explains_probe_failure_and_distance_identical_surface(self):
        write_step(BRepPrimAPI_MakeBox(10, 20, 30).Shape(), self.gt)
        write_step(BRepPrimAPI_MakeBox(12, 20, 30).Shape(), self.candidate)
        ann = {'items': [{'id': 'width', 'type': 'plane_pair',
                          'axis': 'x', 'nominal': 10, 'tol': .01}]}
        measured = geometry_v4.dimension(self.gt, self.candidate, ann)
        self.assertEqual(measured['status'], 'ok')
        self.assertEqual(measured['score'], 0)
        self.assertEqual(measured['raw']['items'][0]['failure_type'], 'outside_tolerance')
        self.assertAlmostEqual(measured['raw']['items'][0]['difference_mm'], 2, places=3)

        identical = geometry_v4.distance(self.gt, self.gt)
        self.assertEqual(identical['status'], 'ok')
        self.assertLess(identical['raw']['symmetric_p99_mm'], .01)
        self.assertGreater(identical['score'], .98)
        self.assertEqual(identical['raw']['distance_backend'], 'point_to_mesh_surface')

    def test_dimension_identifies_pattern_radius_and_position_failures(self):
        cylinder = BRepPrimAPI_MakeCylinder(5, 20).Shape()
        write_step(cylinder, self.gt)
        move = gp_Trsf()
        move.SetTranslationPart(gp_Vec(5, 0, 0))
        write_step(BRepBuilderAPI_Transform(cylinder, move, True).Shape(), self.candidate)
        ann = {'items': [
            {'id': 'radius', 'type': 'cylinder', 'nominal': 10, 'tol': .01,
             'count': 1, 'pattern_radius': 0, 'pattern_tol': .1},
            {'id': 'position', 'type': 'cylinder', 'nominal': 10, 'tol': .01,
             'count': 1, 'pattern_positions': [[0, 0]], 'centroid_within': .5},
        ]}
        measured = geometry_v4.dimension(self.gt, self.candidate, ann)
        self.assertEqual(measured['status'], 'ok')
        self.assertEqual(measured['score'], 0)
        items = {item['id']: item for item in measured['raw']['items']}
        self.assertAlmostEqual(items['radius']['diameter_difference_mm'], 0, places=5)
        self.assertEqual(items['radius']['failure_type'], 'pattern_radius_mismatch')
        self.assertAlmostEqual(items['radius']['pattern_radius_difference_mm'][0], 5, places=3)
        self.assertEqual(items['position']['failure_type'], 'pattern_position_mismatch')


if __name__ == '__main__':
    unittest.main()

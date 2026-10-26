"""Display-only behavior: identity, independent coordinates and failure isolation."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET


class DisplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'private.step'
        self.source.write_bytes(b'not a STEP file')

    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('src.display_assets'),
                             'display assets implementation is required')
        from src import display_assets
        return display_assets

    def slot(self, frame='world'):
        return {'path': str(self.source), 'sha256': hashlib.sha256(self.source.read_bytes()).hexdigest(),
                'coordinate_frame': frame}

    def test_mismatched_source_never_generates_a_model_or_leaks_path(self):
        api = self.api()
        slot = self.slot(); slot['sha256'] = '0' * 64
        with patch.object(api, '_run_worker', side_effect=AssertionError('unverified input used')):
            result = api.build_display_assets({'a': {'world': {'candidate': slot}}}, self.root / 'report')
        view = result['a']['views']['world']
        self.assertIsNone(view['candidate'])
        self.assertIn('source_hash_mismatch', view['metadata']['role_errors']['candidate'])
        self.assertNotIn(str(self.source), json.dumps(result))

    def test_missing_scoring_input_is_not_replaced_with_world_input(self):
        api = self.api()
        with patch.object(api, '_run_worker', return_value={'status': 'failed', 'error': 'mesh_failed'}):
            result = api.build_display_assets({'a': {'world': {'candidate': self.slot()},
                                                     'scoring': {}}}, self.root / 'report')
        self.assertIsNone(result['a']['views']['scoring']['candidate'])
        self.assertEqual(result['a']['views']['scoring']['status'], 'unavailable')

    def test_invalid_triangle_indices_and_nonfinite_vertices_are_rejected(self):
        api = self.api()
        good = {'vertices': [0, 0, 0, 1, 0, 0, 0, 1, 0], 'indices': [0, 1, 2]}
        api.validate_mesh(good)
        for indices in ([0, 1, 3], [0, -1, 2], [0, 1.5, 2], [0, 1], [False, 1, 2]):
            with self.subTest(indices=indices), self.assertRaises(ValueError):
                api.validate_mesh({**good, 'indices': indices})
        with self.assertRaises(ValueError):
            api.validate_mesh({**good, 'vertices': [float('nan'), *good['vertices'][1:]]})

    def test_svg_keeps_common_scale_and_world_displacement(self):
        api = self.api()
        reference = {'vertices': [0, 0, 0, 10, 0, 0, 0, 10, 0], 'indices': [0, 1, 2]}
        candidate = {'vertices': [30, 0, 0, 41, 0, 0, 30, 11, 0], 'indices': [0, 1, 2]}
        svg = ET.fromstring(api.svg_preview({'reference': reference, 'candidate': candidate}))
        polygons = svg.findall('.//{http://www.w3.org/2000/svg}polygon')
        by_role = {p.attrib['data-role']: [[float(c) for c in pair.split(',')]
                   for pair in p.attrib['points'].split()] for p in polygons}
        import math
        ref, cand = by_role['reference'], by_role['candidate']
        self.assertAlmostEqual(math.dist(cand[0], cand[1]) / math.dist(ref[0], ref[1]), 1.1, places=4)
        half = float(svg.attrib['viewBox'].split()[2]) / 2
        self.assertTrue(all(p[0] < half for p in cand))
        self.assertTrue(all(p[0] > half for p in ref))
        # Remove only the viewport offset to compare coordinates in the common camera.
        ref_local = [ref[0][0] - half, ref[0][1]]
        self.assertGreater(math.dist(ref_local, cand[0]), 2 * math.dist(ref[0], ref[1]))

    def test_process_timeout_and_failure_have_safe_errors(self):
        api = self.api()
        timeout = api._run_process([sys.executable, '-c', 'import time; time.sleep(10)'], .05)
        self.assertEqual(timeout['error'], 'mesh_timeout')
        failure = api._run_process([sys.executable, '-c', 'raise RuntimeError("private input path")'], 5)
        self.assertEqual(failure['error'], 'mesh_failed')
        self.assertNotIn('private', json.dumps(failure))

    def test_failed_role_preserves_other_role_and_reports_missing_faces(self):
        api = self.api()
        mesh = {'vertices': [0, 0, 0, 10, 0, 0, 0, 10, 0], 'indices': [0, 1, 2],
                'missingFaceCount': 2, 'faceCount': 1, 'sourceTriangleCount': 1,
                'renderTriangleCount': 1, 'truncated': False}
        def worker(source, destination, *, expected_hash, coordinate_frame, timeout):
            Path(destination).write_text(json.dumps(mesh), encoding='utf-8')
            return {'status': 'completed'}
        with patch.object(api, '_run_worker', side_effect=worker):
            result = api.build_display_assets({'a': {'scoring': {'reference': self.slot('reference')}}}, self.root / 'report')
        view = result['a']['views']['scoring']
        self.assertEqual(view['status'], 'partial')
        self.assertIsNone(view['candidate'])
        self.assertEqual(view['metadata']['quality']['reference']['missingFaceCount'], 2)
        self.assertTrue((self.root / 'report' / view['reference']).is_file())
        self.assertTrue((self.root / 'report' / view['thumbnail']).is_file())

    def test_worker_start_failure_downgrades_only_that_role(self):
        api = self.api()
        other = self.root / 'candidate.step'; other.write_bytes(b'another source')
        candidate = {'path': str(other), 'sha256': hashlib.sha256(other.read_bytes()).hexdigest(), 'coordinate_frame': 'world'}
        mesh = {'vertices': [0, 0, 0, 10, 0, 0, 0, 10, 0], 'indices': [0, 1, 2], 'missingFaceCount': 0}
        def worker(source, destination, **kwargs):
            if Path(source) == self.source:
                raise OSError('private startup details')
            Path(destination).write_text(json.dumps(mesh), encoding='utf-8')
            return {'status': 'completed'}
        with patch.object(api, '_run_worker', side_effect=worker):
            result = api.build_display_assets({'a': {'world': {'reference': self.slot(), 'candidate': candidate}}}, self.root / 'report')
        view = result['a']['views']['world']
        self.assertEqual(view['status'], 'partial')
        self.assertIsNone(view['reference'])
        self.assertIsNotNone(view['candidate'])
        self.assertNotIn('private', json.dumps(result))

    def test_timeout_removes_incomplete_mesh_transport_file(self):
        api = self.api()
        def worker(source, destination, **kwargs):
            Path(destination).with_suffix('.tmp').write_text('partial mesh', encoding='utf-8')
            return {'status': 'failed', 'error': 'mesh_timeout'}
        with patch.object(api, '_run_worker', side_effect=worker):
            result = api.build_display_assets({'a': {'world': {'reference': self.slot()}}}, self.root / 'report')
        self.assertIsNone(result['a']['views']['world']['reference'])
        self.assertEqual(list((self.root / 'report' / 'meshes').iterdir()), [])

    def test_text_only_import_does_not_require_cad_dependencies(self):
        self.api()
        result = subprocess.run([sys.executable, '-S', '-c',
            'import sys; import src.display_assets; assert "OCP" not in sys.modules; assert "numpy" not in sys.modules'],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_dense_preview_failure_never_emits_a_large_svg(self):
        api = self.api()
        self.assertTrue(hasattr(api, '_run_preview'), 'isolated raster preview worker is required')
        mesh = {'vertices': [0, 0, 0, 10, 0, 0, 0, 10, 0], 'indices': [0, 1, 2] * 2001}
        def worker(source, destination, **kwargs):
            Path(destination).write_text(json.dumps(mesh), encoding='utf-8')
            return {'status': 'completed'}
        with patch.object(api, '_run_worker', side_effect=worker), patch.object(api, '_run_preview',
                return_value={'status': 'failed', 'error': 'preview_timeout'}):
            result = api.build_display_assets({'a': {'world': {'candidate': self.slot()}}}, self.root / 'report')
        view = result['a']['views']['world']
        self.assertIsNotNone(view['candidate'])
        self.assertIsNone(view['thumbnail'])
        self.assertEqual(view['metadata']['preview']['error'], 'preview_timeout')
        self.assertEqual(list((self.root / 'report' / 'previews').glob('*.svg')), [])

    @unittest.skipUnless(os.environ.get('RUN_GEOMETRY_TESTS') == '1', 'geometry tests disabled')
    def test_png_is_candidate_left_reference_right_with_one_common_camera(self):
        api = self.api()
        self.assertTrue(hasattr(api, '_run_preview'), 'isolated raster preview worker is required')
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from src.display_mesh import display_mesh
        paths = {}
        for role, length in [('reference', 10), ('candidate', 11)]:
            mesh = display_mesh(BRepPrimAPI_MakeBox(length, length, length).Shape())
            path = self.root / (role + '.json'); path.write_text(json.dumps(mesh), encoding='utf-8')
            paths[role] = path
        destination = self.root / 'preview.png'
        result = api._run_preview(paths, destination, timeout=30)
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(destination.read_bytes()[:8], b'\x89PNG\r\n\x1a\n')
        self.assertLess(destination.stat().st_size, 500_000)
        self.assertEqual(result['metadata']['panels'], ['candidate', 'reference'])
        self.assertEqual(result['metadata']['camera']['origin_mm'], [5.5, 5.5, 5.5])
        from vtkmodules.vtkIOImage import vtkPNGReader
        import numpy as np
        reader = vtkPNGReader(); reader.SetFileName(str(destination)); reader.Update()
        output = reader.GetOutput(); width, height, _ = output.GetDimensions()
        pixels = np.frombuffer(memoryview(output.GetPointData().GetScalars()), dtype=np.uint8).reshape(height, width, -1).astype(float)
        orange = (pixels[:, :, 0] > pixels[:, :, 1] * 1.15) & (pixels[:, :, 0] > pixels[:, :, 2] * 1.15)
        blue = (pixels[:, :, 2] > pixels[:, :, 0] * 1.15) & (pixels[:, :, 2] > pixels[:, :, 1] * 1.05)
        self.assertFalse(np.any(orange[:, width//2:]))
        self.assertFalse(np.any(blue[:, :width//2]))
        oy, ox = np.where(orange); by, bx = np.where(blue)
        self.assertGreater(len(ox), 1000); self.assertGreater(len(bx), 1000)
        self.assertAlmostEqual((ox.max()-ox.min())/(bx.max()-bx.min()), 1.1, delta=.025)
        self.assertAlmostEqual((oy.max()-oy.min())/(by.max()-by.min()), 1.1, delta=.025)

    @unittest.skipUnless(os.environ.get('RUN_GEOMETRY_TESTS') == '1', 'geometry tests disabled')
    def test_real_box_mesh_uses_mm_and_preserves_ten_percent_scale_difference(self):
        api = self.api()
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs
        slots = {}
        for role, width in [('reference', 10), ('candidate', 11)]:
            path = self.root / (role + '.step')
            writer = STEPControl_Writer(); writer.Transfer(BRepPrimAPI_MakeBox(width, 20, 30).Shape(), STEPControl_AsIs)
            self.assertEqual(int(writer.Write(str(path))), 1)
            slots[role] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'coordinate_frame': 'world'}
        result = api.build_display_assets({'box': {'world': slots}}, self.root / 'report')
        view = result['box']['views']['world']
        self.assertEqual(view['status'], 'completed', view)
        self.assertEqual((self.root / 'report' / view['thumbnail']).suffix, '.png')
        self.assertEqual(view['metadata']['display_origin_mm'], [5.5, 10.0, 15.0])
        widths = []
        for role in ['reference', 'candidate']:
            mesh = json.loads((self.root / 'report' / view[role]).read_text(encoding='utf-8'))
            self.assertEqual(mesh['units'], 'mm')
            self.assertEqual(mesh['source_sha256'], slots[role]['sha256'])
            self.assertEqual(mesh['missingFaceCount'], 0)
            xs = mesh['vertices'][::3]; widths.append(max(xs) - min(xs))
        self.assertEqual(widths, [10.0, 11.0])


if __name__ == '__main__':
    unittest.main()

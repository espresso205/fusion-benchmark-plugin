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

    def test_four_cameras_share_origin_scale_and_expected_projection(self):
        api = self.api()
        mesh = {'vertices': [0, 0, 0, 4, 0, 0, 0, 2, 2], 'indices': [0, 1, 2]}
        self.assertIn('angle', __import__('inspect').signature(api.preview_camera).parameters,
                      'camera must support standard directions')
        expected = {'iso': [1, -1, 1], 'front': [0, -1, 0], 'top': [0, 0, 1], 'right': [1, 0, 0]}
        cameras = [api.preview_camera({'candidate': mesh}, angle=angle) for angle in expected]
        self.assertTrue(all(c['origin_mm'] == [2, 1, 1] for c in cameras))
        self.assertEqual(len({c['parallel_scale_mm'] for c in cameras}), 1)
        for angle, camera in zip(expected, cameras):
            self.assertEqual(camera['direction'], expected[angle])
            self.assertEqual(camera['up'], [0, 1, 0] if angle == 'top' else [0, 0, 1])
        for angle in ('front', 'top', 'right'):
            svg = ET.fromstring(api.svg_preview({'candidate': mesh}, angle=angle))
            polygon = svg.find('.//{http://www.w3.org/2000/svg}polygon')
            points = [[float(c) for c in pair.split(',')] for pair in polygon.attrib['points'].split()]
            self.assertLess(points[2][1], points[0][1])
            if angle in ('front', 'top'):
                self.assertGreater(points[1][0], points[0][0])
            else:
                self.assertGreater(points[2][0], points[0][0])

    def test_preview_failure_keeps_all_four_static_split_and_overlay_views(self):
        api = self.api()
        mesh = {'vertices': [0, 0, 0, 10, 0, 0, 0, 10, 10], 'indices': [0, 1, 2]}
        def worker(source, destination, **kwargs):
            Path(destination).write_text(json.dumps(mesh), encoding='utf-8')
            return {'status': 'completed'}
        with patch.object(api, '_run_worker', side_effect=worker), patch.object(api, '_run_preview',
                return_value={'status': 'failed', 'error': 'preview_timeout'}):
            result = api.build_display_assets({'a': {'world': {'candidate': self.slot()}}}, self.root / 'report')
        frame = result['a']['views']['world']
        self.assertEqual(set(frame.get('previews', {})), {'iso', 'front', 'top', 'right'})
        for angle, modes in frame['previews'].items():
            self.assertEqual(set(modes), {'split', 'overlay'})
            for url in modes.values():
                self.assertTrue((self.root / 'report' / url).is_file(), (angle, url))
        self.assertEqual(frame['thumbnail'], frame['previews']['iso']['split'])
        self.assertEqual(frame['difference']['status'], 'unavailable')
        self.assertEqual(frame['difference']['scope'], 'whole_model')
        self.assertEqual(frame['metadata']['preview'].get('overlay_kind'), 'ordinary_overlay')

    def test_invalid_interface_identity_never_falls_back_to_whole_model(self):
        api = self.api()
        self.assertIn('interfaces', __import__('inspect').signature(api.build_display_assets).parameters)
        mesh = {'vertices': [0, 0, 0, 10, 0, 0, 0, 10, 10], 'indices': [0, 1, 2]}
        def worker(source, destination, **kwargs):
            Path(destination).write_text(json.dumps(mesh), encoding='utf-8')
            return {'status': 'completed'}
        for interface in ({'gt_sha256': '0'*64, 'regions': [{'path': str(self.source)}]},
                          {'gt_sha256': self.slot()['sha256'], 'regions': [],
                           'error': 'interface_input_unavailable'}):
            with patch.object(api, '_run_worker', side_effect=worker), patch.object(api, '_run_preview',
                    return_value={'status': 'failed', 'error': 'preview_timeout'}):
                result = api.build_display_assets({'a': {'world': {'candidate': self.slot(),
                    'reference': self.slot()}}}, self.root / 'report', interfaces={'a': interface})
            frame = result['a']['views']['world']
            self.assertEqual(frame['difference']['scope'], 'interface')
            self.assertEqual(frame['difference']['status'], 'unavailable')
            self.assertTrue(all(frame['previews'][angle]['overlay'] for angle in ('iso', 'front', 'top', 'right')))
            self.assertNotIn(str(self.source), json.dumps(result))

    def test_difference_worker_rejects_jig_hash_mismatch_and_cleans_layers(self):
        api = self.api()
        self.assertTrue(hasattr(api, '_run_difference'))
        from tests.test_display_comparison import box
        paths = {}
        for role in ('candidate', 'reference'):
            path = self.root / (role + '.json')
            path.write_text(json.dumps(box(0, 2)), encoding='utf-8'); paths[role] = path
        result = api._run_difference(paths, self.root / 'difference', timeout=20,
            interface={'gt_sha256': 'a'*64, 'regions': [{'path': str(self.source),
                'sha256': '0'*64, 'fit_type': 'KIR'}]}, reference_hash='a'*64)
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['scope'], 'interface')
        self.assertEqual(result['reason'], 'interface_source_hash_mismatch')
        self.assertFalse(list(self.root.glob('difference-*.json')))

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

    def test_partial_preview_worker_failure_preserves_completed_pngs(self):
        api = self.api()
        destination = self.root / 'preview.png'
        # The native renderer may terminate after writing its first view.
        # A valid independent 1x1 PNG represents that completed output.
        import base64
        png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=')
        def process(command, timeout):
            destination.write_bytes(png)
            destination.with_name('preview-front-split.tmp').write_bytes(b'partial')
            return {'status': 'failed', 'error': 'mesh_timeout'}
        with patch.object(api, '_run_process', side_effect=process):
            result = api._run_preview({}, destination, timeout=1)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(destination.read_bytes(), png)
        self.assertEqual(result['previews']['iso']['split'], destination)
        self.assertIsNone(result['previews']['front']['split'])
        self.assertFalse(destination.with_name('preview-front-split.tmp').exists())

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
        self.assertEqual(set(result['metadata']['cameras']), {'iso', 'front', 'top', 'right'})
        for modes in api.preview_paths(destination).values():
            for path in modes.values():
                self.assertEqual(path.read_bytes()[:8], b'\x89PNG\r\n\x1a\n')
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
    def test_overlay_reveals_amber_missing_material_inside_matched_shell(self):
        api = self.api()
        import manifold3d as m
        import numpy as np
        from vtkmodules.vtkIOImage import vtkPNGReader
        outer = m.Manifold.cube([6, 6, 6])
        inner = m.Manifold.cube([2, 2, 2]).translate([2, 2, 2])
        paths = {}
        for name, solid in [('candidate', outer-inner), ('reference', outer),
                            ('matched', outer-inner), ('missing', inner)]:
            mesh = solid.to_mesh64()
            path = self.root / (name+'.json')
            path.write_text(json.dumps({'vertices': np.asarray(mesh.vert_properties)[:, :3].ravel().tolist(),
                'indices': np.asarray(mesh.tri_verts).ravel().tolist()}), encoding='utf-8')
            paths[name] = path
        result = api._run_preview({key: paths[key] for key in ('candidate', 'reference')},
            self.root/'overlay.png', timeout=30,
            layer_paths={key: paths[key] for key in ('matched', 'missing')})
        self.assertEqual(result['status'], 'completed', result)
        reader = vtkPNGReader()
        reader.SetFileName(str(self.root/'overlay-front-overlay.png')); reader.Update()
        data = reader.GetOutput()
        pixels = np.frombuffer(memoryview(data.GetPointData().GetScalars()), dtype=np.uint8).reshape(-1, 3).astype(float)
        red, green, blue = pixels.T
        # The blue translucent shell mixes with amber, so recognize its warm
        # hue after compositing instead of expecting the unmixed layer color.
        amber = (red > green*1.05) & (red < green*1.55) & (green > blue*1.5)
        self.assertGreater(int(amber.sum()), 100, 'matched/context must not hide the inner missing volume')

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

    @unittest.skipUnless(os.environ.get('RUN_GEOMETRY_TESTS') == '1', 'geometry tests disabled')
    def test_real_jig_uses_each_existing_frame_without_alignment_or_input_mutation(self):
        api = self.api()
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs
        from OCP.gp import gp_Pnt
        from tests.test_display_comparison import volume
        slots, original = {}, {}
        for role, start in [('reference', 0), ('candidate', 1), ('jig', 0)]:
            path = self.root / (role + '.step')
            writer = STEPControl_Writer()
            writer.Transfer(BRepPrimAPI_MakeBox(gp_Pnt(start, 0, 0), 2, 2, 2).Shape(), STEPControl_AsIs)
            self.assertEqual(int(writer.Write(str(path))), 1)
            original[role] = path.read_bytes()
            slots[role] = {'path': str(path), 'sha256': hashlib.sha256(original[role]).hexdigest(),
                           'coordinate_frame': 'world'}
        result = api.build_display_assets({'a': {
            'world': {'candidate': slots['candidate'], 'reference': slots['reference']},
            'scoring': {'candidate': slots['reference'], 'reference': slots['reference']}}},
            self.root / 'report', interfaces={'a': {'gt_sha256': slots['reference']['sha256'],
                'regions': [{**slots['jig'], 'fit_type': 'KIR'}]}})
        for frame_name, expected in [('world', {'matched': 4, 'extra': 4, 'missing': 4}),
                                      ('scoring', {'matched': 8, 'extra': 0, 'missing': 0})]:
            frame = result['a']['views'][frame_name]
            self.assertEqual(frame['difference']['status'], 'completed', frame['difference'])
            self.assertEqual(frame['difference']['scope'], 'interface')
            actual = {key: volume(json.loads((self.root / 'report' / url).read_text(encoding='utf-8')))
                      if url else 0 for key, url in frame['difference']['layers'].items()}
            self.assertEqual(actual, expected)
            self.assertEqual(frame['metadata']['preview']['overlay_kind'], 'difference')
        for role, data in original.items():
            self.assertEqual(Path(slots[role]['path']).read_bytes(), data)
        self.assertNotIn(str(self.root), json.dumps(result))


if __name__ == '__main__':
    unittest.main()

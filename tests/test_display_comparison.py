"""Display Boolean partitions are physical volumes, never evaluation scores."""
import importlib.util
import unittest


def box(x0, x1, y0=0, y1=2, z0=0, z1=2):
    return {'vertices': [x0,y0,z0, x1,y0,z0, x1,y1,z0, x0,y1,z0,
                         x0,y0,z1, x1,y0,z1, x1,y1,z1, x0,y1,z1],
            'indices': [0,2,1, 0,3,2, 4,5,6, 4,6,7, 0,1,5, 0,5,4,
                        1,2,6, 1,6,5, 2,3,7, 2,7,6, 3,0,4, 3,4,7]}


def volume(mesh):
    if mesh is None:
        return 0
    import numpy as np
    points = np.asarray(mesh['vertices']).reshape(-1, 3)
    points = points - points[0]
    triangles = points[np.asarray(mesh['indices']).reshape(-1, 3)]
    return abs(float(np.einsum('ij,ij->i', triangles[:, 0],
                              np.cross(triangles[:, 1], triangles[:, 2])).sum()) / 6)


class ComparisonTests(unittest.TestCase):
    def test_surface_tolerance_is_local_and_preserves_raw_partitions(self):
        from src.display_comparison import compare_meshes, surface_layers
        import copy
        meshes = {'candidate': box(.002, 2.002), 'reference': box(0, 2)}
        raw = compare_meshes(meshes); frozen = copy.deepcopy(raw)
        surface = surface_layers(meshes, raw['layers'], tolerance_mm=.02)
        self.assertIsNone(surface['extra'])
        self.assertIsNone(surface['missing'])
        self.assertIsNotNone(surface['matched'])
        self.assertEqual(raw, frozen, 'display filtering must preserve exact raw evidence')
        meshes = {'candidate': box(.2, 2.2), 'reference': box(0, 2)}
        raw = compare_meshes(meshes)
        surface = surface_layers(meshes, raw['layers'], tolerance_mm=.02)
        self.assertIsNotNone(surface['extra'])
        self.assertIsNotNone(surface['missing'])

    def test_surface_mode_is_exterior_only_and_raw_retains_enclosed_cavity(self):
        import manifold3d as m
        import numpy as np
        from src.display_comparison import compare_meshes, surface_layers
        outer = m.Manifold.cube([6, 6, 6])
        inner = m.Manifold.cube([2, 2, 2]).translate([2, 2, 2])
        def mesh(solid):
            value = solid.to_mesh64()
            return {'vertices': np.asarray(value.vert_properties)[:, :3].ravel().tolist(),
                    'indices': np.asarray(value.tri_verts).ravel().tolist()}
        meshes = {'candidate': mesh(outer-inner), 'reference': mesh(outer)}
        raw = compare_meshes(meshes)
        self.assertAlmostEqual(volume(raw['layers']['missing']), 8)
        surface = surface_layers(meshes, raw['layers'], tolerance_mm=.02)
        self.assertIsNone(surface['extra'])
        self.assertIsNone(surface['missing'])
        self.assertIsNotNone(surface['matched'])
        self.assertAlmostEqual(volume(raw['layers']['missing']), 8)

    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('src.display_comparison'),
                             'solid display comparison must exist')
        from src.display_comparison import compare_meshes
        return compare_meshes

    def test_overlap_partitions_preserve_world_coordinates_and_volume(self):
        compare = self.api()
        for offset in (0, 1_000_000_000):
            with self.subTest(offset=offset):
                result = compare({'candidate': box(offset+1, offset+3),
                                  'reference': box(offset, offset+2)})
                self.assertEqual(result['status'], 'completed', result)
                self.assertEqual(result['scope'], 'whole_model')
                self.assertEqual({key: volume(mesh) for key, mesh in result['layers'].items()},
                                 {'matched': 4, 'extra': 4, 'missing': 4})
                self.assertEqual(min(result['layers']['matched']['vertices'][::3]), offset+1)

    def test_identical_models_have_empty_extra_and_missing_layers(self):
        result = self.api()({'candidate': box(0, 2), 'reference': box(0, 2)})
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(volume(result['layers']['matched']), 8)
        self.assertIsNone(result['layers']['extra'])
        self.assertIsNone(result['layers']['missing'])

    def test_open_mesh_is_unavailable_instead_of_fabricated_difference(self):
        broken = box(0, 2); broken['indices'] = broken['indices'][:-3]
        result = self.api()({'candidate': broken, 'reference': box(0, 2)})
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['reason'], 'difference_mesh_not_closed')
        self.assertEqual(result['layers'], {'matched': None, 'extra': None, 'missing': None})

    def test_manifold_implicit_volume_change_is_rejected_despite_no_error(self):
        # This folded synthetic box is accepted by Manifold with NoError and
        # twelve triangles, but its import changes volume from 20/3 to 6 mm3.
        # A successful constructor or unchanged triangle count is insufficient.
        folded = box(0, 2); folded['vertices'][:3] = [-2, -1, 3]
        self.assertAlmostEqual(volume(folded), 20/3)
        result = self.api()({'candidate': folded, 'reference': box(0, 2)})
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['reason'], 'difference_mesh_volume_changed')
        self.assertEqual(result['layers'], {'matched': None, 'extra': None, 'missing': None})

    def test_zero_area_faces_are_allowed_when_import_preserves_volume(self):
        candidate = box(0, 2)
        candidate['indices'].extend([0, 0, 1])
        # Independent duplicated face vertices also legitimately weld away.
        candidate['vertices'].extend(candidate['vertices'][:3])
        candidate['indices'][0] = 8
        result = self.api()({'candidate': candidate, 'reference': box(0, 2)})
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(volume(result['layers']['matched']), 8)
        self.assertIsNone(result['layers']['extra'])
        self.assertIsNone(result['layers']['missing'])

    def test_widely_separated_models_keep_stable_volume_without_repositioning(self):
        result = self.api()({'candidate': box(1_000_000_000, 1_000_000_002),
                            'reference': box(0, 2)})
        self.assertEqual(result['status'], 'completed', result)
        self.assertIsNone(result['layers']['matched'])
        self.assertEqual(volume(result['layers']['extra']), 8)
        self.assertEqual(volume(result['layers']['missing']), 8)

    def test_kir_shell_detects_material_outside_region(self):
        result = self.api()({'candidate': box(1, 3), 'reference': box(0, 2)},
                            regions=[{'mesh': box(0, 2), 'fit_type': 'KIR'}])
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(result['scope'], 'interface')
        self.assertEqual({key: volume(mesh) for key, mesh in result['layers'].items()},
                         {'matched': 4, 'extra': 4, 'missing': 4})

    def test_kor_shell_detects_removed_surrounding_material(self):
        result = self.api()({'candidate': box(0, 1), 'reference': box(0, 2)},
                            regions=[{'mesh': box(2, 4), 'fit_type': 'KOR'}])
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual({key: volume(mesh) for key, mesh in result['layers'].items()},
                         {'matched': 8, 'extra': 0, 'missing': 4})


if __name__ == '__main__':
    unittest.main()

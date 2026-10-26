"""Tessellated solid display diagnostics; no scoring imports or measurements."""
import math


# This compares two representations of the same input triangles, not a mesh
# against a BRep. One part per million (or 1e-6 mm3 for very small inputs)
# permits floating-point summation noise, never material-changing repair.
VOLUME_RELATIVE_TOLERANCE = 1e-6
VOLUME_ABSOLUTE_TOLERANCE_MM3 = 1e-6


def unavailable(scope='whole_model', reason='difference_unavailable'):
    return {'status': 'unavailable', 'scope': scope,
            'layers': dict.fromkeys(('matched', 'extra', 'missing')), 'reason': reason}


def compare_meshes(meshes, *, regions=None):
    """Partition solids in their existing frame, using only a shared translation.

    Empty Boolean results are valid and represented by null layers. An open,
    incomplete or invalid input must never look like a successful comparison.
    """
    scope = 'interface' if regions is not None else 'whole_model'
    if not all(role in meshes for role in ('candidate', 'reference')):
        return unavailable(scope, 'difference_requires_both_models')
    try:
        import numpy as np
        import manifold3d as m
        if __package__:
            from .display_assets import validate_mesh, preview_camera
        else:
            from display_assets import validate_mesh, preview_camera
        origin = np.asarray(preview_camera(meshes)['origin_mm'])

        def solid(mesh):
            validate_mesh(mesh)
            if mesh.get('missingFaceCount', 0) or mesh.get('truncated', False):
                raise ValueError('difference_mesh_incomplete')
            source_points = np.asarray(mesh['vertices'], dtype=np.float64).reshape(-1, 3)
            source_triangles = np.asarray(mesh['indices']).reshape(-1, 3)
            # Calculate signed input volume before Manifold can collapse or
            # retriangulate faces. A per-mesh computational origin and fsum
            # avoid cancellation when the two models are far apart; neither
            # changes the shared coordinates used by the Boolean operation.
            local_origin = source_points.min(axis=0) + np.ptp(source_points, axis=0)/2
            local_triangles = (source_points-local_origin)[source_triangles]
            source_volume = math.fsum(np.einsum('ij,ij->i', local_triangles[:, 0],
                np.cross(local_triangles[:, 1], local_triangles[:, 2]))) / 6
            points = source_points - origin
            # OCC emits independent vertices per face. Weld equal transported
            # positions, without moving vertices or filling any missing faces.
            vertices, inverse = np.unique(points, axis=0, return_inverse=True)
            triangles = inverse[source_triangles]
            result = m.Manifold(m.Mesh64(np.ascontiguousarray(vertices),
                                        np.asarray(triangles, dtype=np.uint64)))
            if result.status() != m.Error.NoError or result.is_empty() or result.volume() <= 0:
                raise ValueError('difference_mesh_not_closed')
            imported_volume = result.volume()
            tolerance = max(VOLUME_ABSOLUTE_TOLERANCE_MM3,
                            VOLUME_RELATIVE_TOLERANCE * abs(source_volume))
            if (not math.isfinite(source_volume) or not math.isfinite(imported_volume) or
                    abs(imported_volume-source_volume) > tolerance):
                raise ValueError('difference_mesh_volume_changed')
            return result

        candidate, reference = solid(meshes['candidate']), solid(meshes['reference'])
        if regions is None:
            partitions = {'matched': candidate ^ reference, 'extra': candidate - reference,
                          'missing': reference - candidate}
        else:
            if not regions:
                raise ValueError('interface_input_unavailable')
            partitions = {key: m.Manifold() for key in ('matched', 'extra', 'missing')}
            for region in regions:
                region_solid = solid(region['mesh'])
                points = np.asarray(region['mesh']['vertices']).reshape(-1, 3) - origin
                low, high = points.min(axis=0), points.max(axis=0)
                margin = max(2.0, .2 * float((high-low).max()))
                inflated = m.Manifold.cube((high-low+2*margin).tolist()).translate((low-margin).tolist())
                if region['fit_type'] == 'KOR':
                    shell = (inflated ^ reference) - region_solid
                    layers = {'matched': region_solid - candidate,
                              'extra': region_solid ^ candidate, 'missing': shell - candidate}
                elif region['fit_type'] == 'KIR':
                    shell = (inflated - reference) - region_solid
                    layers = {'matched': region_solid ^ candidate,
                              'extra': shell ^ candidate, 'missing': region_solid - candidate}
                else:
                    raise ValueError('interface_input_unavailable')
                for key, value in layers.items():
                    partitions[key] = partitions[key] + value

        layers = {}
        for key, value in partitions.items():
            if value.status() != m.Error.NoError:
                raise ValueError('difference_boolean_failed')
            if value.is_empty():
                layers[key] = None
                continue
            result = value.to_mesh64()
            mesh = {'vertices': (np.asarray(result.vert_properties)[:, :3] + origin).ravel().tolist(),
                    'indices': np.asarray(result.tri_verts).ravel().tolist(), 'units': 'mm',
                    'approximation': 'tessellated-solid-boolean', 'scope': scope}
            validate_mesh(mesh)
            layers[key] = mesh
        return {'status': 'completed', 'scope': scope, 'layers': layers}
    except ImportError:
        return unavailable(scope, 'difference_dependency_unavailable')
    except ValueError as error:
        reason = str(error)
        allowed = {'difference_mesh_incomplete', 'difference_mesh_not_closed', 'difference_mesh_volume_changed',
                   'interface_input_unavailable', 'difference_boolean_failed'}
        return unavailable(scope, reason if reason in allowed else 'difference_mesh_invalid')
    except Exception:
        return unavailable(scope, 'difference_boolean_failed')

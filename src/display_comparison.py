"""Tessellated solid display diagnostics; no scoring imports or measurements."""
import math


# This compares two representations of the same input triangles, not a mesh
# against a BRep. One part per million (or 1e-6 mm3 for very small inputs)
# permits floating-point summation noise, never material-changing repair.
VOLUME_RELATIVE_TOLERANCE = 1e-6
VOLUME_ABSOLUTE_TOLERANCE_MM3 = 1e-6


def surface_layers(meshes, partitions, *, tolerance_mm):
    """Color local surface differences at the display mesh precision.

    Raw closed Boolean partitions are never changed. The union's exterior
    triangles are split by an interpolated distance field. No component/volume threshold is
    used: a small but spatially distinct defect remains visible. These open
    display surfaces must never be used as volume or scoring evidence.
    """
    import numpy as np
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkPolyData, vtkCellArray
    from vtkmodules.vtkFiltersCore import vtkImplicitPolyDataDistance
    from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray
    tolerance_mm = float(tolerance_mm)
    if not math.isfinite(tolerance_mm) or tolerance_mm < 0:
        raise ValueError('invalid_display_tolerance')
    origin = np.asarray(meshes['reference']['vertices'], dtype=float).reshape(-1, 3).mean(axis=0)
    distances, retained = [], []
    for role in ('reference', 'candidate'):
        mesh = meshes[role]
        points = vtkPoints()
        points.SetData(numpy_to_vtk(np.asarray(mesh['vertices']).reshape(-1, 3)-origin, deep=True))
        indices = np.asarray(mesh['indices'], dtype=np.int64)
        cells = vtkCellArray()
        cells.SetData(numpy_to_vtkIdTypeArray(np.arange(0, len(indices)+1, 3, dtype=np.int64), deep=True),
                      numpy_to_vtkIdTypeArray(indices, deep=True))
        poly = vtkPolyData(); poly.SetPoints(points); poly.SetPolys(cells)
        distance = vtkImplicitPolyDataDistance(); distance.SetInput(poly)
        distances.append(distance); retained.append(poly)
    output = {key: {'vertices': [], 'indices': []} for key in ('matched', 'extra', 'missing')}
    maps = {key: {} for key in output}

    def append(role, polygon):
        if len(polygon) < 3:
            return
        mesh, lookup = output[role], maps[role]
        indices = []
        for point, _ in polygon:
            key = tuple(round(float(value), 12) for value in point)
            if key not in lookup:
                lookup[key] = len(mesh['vertices'])//3
                # Six decimal millimeters are far below the displayed mesh
                # precision; compact transport only, never change raw evidence.
                mesh['vertices'].extend(np.round(point+origin, 6).tolist())
            indices.append(lookup[key])
        for i in range(1, len(indices)-1):
            if len({indices[0], indices[i], indices[i+1]}) == 3:
                mesh['indices'].extend((indices[0], indices[i], indices[i+1]))

    def clip(polygon, positive):
        result = []
        for previous, current in zip(polygon[-1:]+polygon[:-1], polygon):
            p, pv = previous; q, qv = current
            pin, qin = (pv > 0) == positive, (qv > 0) == positive
            if pin != qin:
                fraction = pv/(pv-qv)
                result.append((p+(q-p)*fraction, 0.0))
            if qin:
                result.append(current)
        return result

    sample_cache = {}
    def value(point):
        key = tuple(round(float(v), 12) for v in point)
        if key not in sample_cache:
            sample_cache[key] = max(abs(distance.EvaluateFunction(point)) for distance in distances)-tolerance_mm
        return sample_cache[key]

    def classify(role, triangle, depth=0):
        values = [value(point) for point in triangle]
        midpoints = [(triangle[i]+triangle[(i+1)%3])/2 for i in range(3)]
        middle_values = [value(point) for point in midpoints]
        center = sum(triangle)/3
        center_value = value(center)
        sampled = values+middle_values+[center_value]
        if all(v > 0 for v in sampled):
            append(role, list(zip(triangle, values))); return
        if all(v <= 0 for v in sampled):
            append('matched', list(zip(triangle, values))); return
        error = max([abs(middle_values[i]-(values[i]+values[(i+1)%3])/2) for i in range(3)] +
                    [abs(center_value-sum(values)/3)])
        if depth < 3 and error > max(tolerance_mm*.2, 1e-5):
            a,b,c = triangle; ab,bc,ca = midpoints
            for child in ((a,ab,ca),(ab,b,bc),(ca,bc,c),(ab,bc,ca)):
                classify(role, child, depth+1)
            return
        # The interpolated boundary is now close to the sampled distance field;
        # center samples retain small interior features missed by corners alone.
        for i in range(3):
            polygon = [(triangle[i], values[i]), (triangle[(i+1)%3], values[(i+1)%3]), (center, center_value)]
            append(role, clip(polygon, True)); append('matched', clip(polygon, False))

    for role, mesh in partitions.items():
        if not mesh:
            continue
        points = np.asarray(mesh['vertices']).reshape(-1, 3)-origin
        triangles = np.asarray(mesh['indices']).reshape(-1, 3)
        # The Boolean solids share internal faces. Keep only the exterior of
        # their union in the surface view; rendering those coincident cut faces
        # would produce depth stripes even with fully opaque materials. The raw
        # mode retains enclosed/internal defects for independent inspection.
        epsilon = max(1e-7, float(np.ptp(points, axis=0).max())*1e-10)
        exterior = []
        for triangle in triangles:
            center = points[triangle].mean(axis=0)
            signed = [distance.EvaluateFunction(center) for distance in distances]
            if min(signed) >= -epsilon:
                exterior.append(triangle)
        if role == 'matched':
            for triangle in exterior:
                append('matched', [(points[index], 0.0) for index in triangle])
            continue
        # Each Boolean boundary belongs to one input surface. Distance to the
        # other input measures its local separation. Enclosed differences remain
        # in the unmodified raw partitions, accessible through layer isolation.
        for triangle in exterior:
            classify(role, tuple(points[index] for index in triangle))
    return {key: ({**mesh, 'units': 'mm', 'approximation': 'display-surface-distance',
                   'display_tolerance_mm': tolerance_mm} if mesh['indices'] else None)
            for key, mesh in output.items()}


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

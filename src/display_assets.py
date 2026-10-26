"""Optional display assets; importing this module needs only the standard library.

Meshes are never passed back to the evaluator. All cache entries live inside the
new report output, keyed by input bytes, frame, generator bytes and dependencies.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import tempfile


GENERATOR_VERSION = 'fusion-display/2'
SVG_TRIANGLE_LIMIT = 2000
QUALITY_KEYS = ('sourceTriangleCount', 'renderTriangleCount', 'topologySimplified',
                'truncated', 'displayVersion', 'faceCount', 'missingFaceCount',
                'linearDeflectionMm', 'approximation', 'coordinateDecimals')


def _sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def dependency_versions():
    versions = {'python': sys.version.split()[0]}
    for name in ('cadquery-ocp', 'numpy', 'vtk'):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = 'unavailable'
    return versions


def generator_fingerprint():
    root = Path(__file__).parent
    return {'version': GENERATOR_VERSION, 'dependencies': dependency_versions(),
            'sources': {name: _sha256(root / name) for name in
                        ('display_mesh.py', 'display_worker.py', 'display_assets.py')}}


def validate_mesh(mesh):
    """Reject malformed numbers/indices before SVG projection or GPU allocation."""
    vertices, indices = mesh.get('vertices'), mesh.get('indices')
    if not isinstance(vertices, list) or not isinstance(indices, list):
        raise ValueError('invalid_mesh')
    if not vertices or len(vertices) % 3 or not indices or len(indices) % 3:
        raise ValueError('invalid_mesh')
    if len(vertices) > 9_000_000 or len(indices) > 3_000_000:
        raise ValueError('mesh_too_large')
    if any(type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1e15 for value in vertices):
        raise ValueError('invalid_mesh')
    count = len(vertices) // 3
    if any(type(index) is not int or index < 0 or index >= count for index in indices):
        raise ValueError('invalid_mesh')


def _run_process(command, timeout):
    try:
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {'status': 'failed', 'error': 'mesh_timeout'}
    except OSError:
        return {'status': 'failed', 'error': 'mesh_worker_unavailable'}
    return ({'status': 'completed'} if result.returncode == 0 else
            {'status': 'failed', 'error': 'mesh_failed'})


def _run_worker(source, destination, *, expected_hash, coordinate_frame, timeout):
    # The request file is private and short lived. Its absolute paths are never
    # copied into the report, a manifest or worker error messages.
    with tempfile.TemporaryDirectory(prefix='fusion-display-') as temporary:
        request = Path(temporary) / 'request.json'
        request.write_text(json.dumps({'source': str(source), 'destination': str(destination),
            'expected_hash': expected_hash, 'coordinate_frame': coordinate_frame}), encoding='utf-8')
        result = _run_process([sys.executable, '-B', str(Path(__file__).with_name('display_worker.py')),
                               str(request)], timeout)
    return result


def _run_preview(mesh_paths, destination, *, timeout):
    destination = Path(destination)
    with tempfile.TemporaryDirectory(prefix='fusion-preview-') as temporary:
        request = Path(temporary) / 'request.json'
        metadata_path = Path(temporary) / 'metadata.json'
        request.write_text(json.dumps({'operation': 'preview', 'meshes': {
            role: str(path) for role, path in mesh_paths.items()},
            'destination': str(destination), 'metadata_path': str(metadata_path)}), encoding='utf-8')
        result = _run_process([sys.executable, '-B', str(Path(__file__).with_name('display_worker.py')),
                               str(request)], timeout)
        if result['status'] == 'completed':
            try:
                with destination.open('rb') as stream:
                    if stream.read(8) != b'\x89PNG\r\n\x1a\n':
                        raise ValueError('invalid_png')
                return {'status': 'completed', 'metadata': json.loads(metadata_path.read_text(encoding='utf-8'))}
            except (OSError, ValueError):
                result = {'status': 'failed', 'error': 'preview_invalid'}
        destination.unlink(missing_ok=True)
        destination.with_suffix('.tmp').unlink(missing_ok=True)
        return {'status': 'failed', 'error': result.get('error', 'preview_failed').replace('mesh_', 'preview_')}


def preview_camera(meshes, width=960, height=360):
    """One orthographic camera for both panels; retains size and displacement."""
    minimum = [min(min(mesh['vertices'][axis::3]) for mesh in meshes.values()) for axis in range(3)]
    maximum = [max(max(mesh['vertices'][axis::3]) for mesh in meshes.values()) for axis in range(3)]
    size = [maximum[axis]-minimum[axis] for axis in range(3)]
    diagonal = math.sqrt(sum(value*value for value in size))
    return {'projection': 'orthographic', 'view': 'iso',
            'origin_mm': [minimum[axis]+size[axis]/2 for axis in range(3)],
            'bounds_mm': {'minimum': minimum, 'maximum': maximum},
            'parallel_scale_mm': max(.01, diagonal/2)*1.15*max(1, height/(width/2)),
            'distance_mm': max(1, diagonal*2)}


def svg_preview(meshes, width=960, height=360):
    """Orthographic isometric preview with one common origin, scale and camera.

    All triangles are kept. A depth sort is a display approximation; the SVG is
    explicitly labeled as such and is not a geometry measurement.
    """
    if sum(len(mesh['indices'])//3 for mesh in meshes.values()) > SVG_TRIANGLE_LIMIT:
        raise ValueError('preview_too_dense_for_svg')
    projected = {}
    for role in ('reference', 'candidate'):
        if role not in meshes:
            continue
        mesh = meshes[role]; validate_mesh(mesh)
        vertices = mesh['vertices']
        projected[role] = [( (vertices[i] - vertices[i+1]) / math.sqrt(2),
                            (vertices[i] + vertices[i+1] - 2*vertices[i+2]) / math.sqrt(6),
                            (vertices[i] + vertices[i+1] + vertices[i+2]) / math.sqrt(3))
                           for i in range(0, len(vertices), 3)]
    all_points = [point for points in projected.values() for point in points]
    if not all_points:
        raise ValueError('empty_preview')
    xmin, xmax = min(p[0] for p in all_points), max(p[0] for p in all_points)
    ymin, ymax = min(p[1] for p in all_points), max(p[1] for p in all_points)
    scale = min((width/2-48)/max(xmax-xmin, 1e-9), (height-64)/max(ymax-ymin, 1e-9))
    cx, cy = (xmin+xmax)/2, (ymin+ymax)/2
    polygons = []
    for role, points in projected.items():
        indices = meshes[role]['indices']
        for offset in range(0, len(indices), 3):
            triangle = [points[index] for index in indices[offset:offset+3]]
            panel_center = width/4 if role == 'candidate' else 3*width/4
            xy = ' '.join(f'{(p[0]-cx)*scale+panel_center:.4f},{(p[1]-cy)*scale+height/2-10:.4f}' for p in triangle)
            color = '#258cba' if role == 'reference' else '#ec8a32'
            polygons.append((sum(p[2] for p in triangle),
                f'<polygon data-role="{role}" points="{xy}" fill="{color}" fill-opacity="0.64" stroke="{color}" stroke-width="0.25"/>'))
    polygons.sort(key=lambda item: item[0])
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" '
            'aria-label="左侧橙色生成模型，右侧蓝色标准答案，共享坐标与比例">'
            '<rect width="100%" height="100%" fill="#ffffff"/>' +
            ''.join(item[1] for item in polygons) +
            '</svg>')


def build_display_assets(model_sources, output, *, timeout=60.0, workers=2):
    output = Path(output).resolve()
    (output / 'meshes').mkdir(parents=True, exist_ok=True)
    (output / 'previews').mkdir(parents=True, exist_ok=True)
    timeout = max(.01, float(timeout))
    fingerprint = generator_fingerprint()
    slots, jobs = {}, {}
    for case, views in model_sources.items():
        for view in ('world', 'scoring'):
            for role in ('reference', 'candidate'):
                slot = views.get(view, {}).get(role)
                if not slot:
                    continue
                key = (case, view, role)
                try:
                    expected = slot['sha256']
                    path = Path(slot['path'])
                    if not path.is_absolute() or not re.fullmatch('[a-fA-F0-9]{64}', expected):
                        raise ValueError('source_identity_unavailable')
                    if _sha256(path) != expected.lower():
                        raise ValueError('source_hash_mismatch')
                    frame = slot.get('coordinate_frame', view)
                    # Frames are labels, never paths; callers project only their
                    # known coordinate enum into the browser-facing metadata.
                    if not isinstance(frame, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', frame):
                        raise ValueError('source_frame_invalid')
                    content = {'sha256': expected.lower(), 'coordinate_frame': frame,
                               'generator': fingerprint}
                    cache_key = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
                    slots[key] = {'cache_key': cache_key, 'sha256': expected.lower(), 'frame': frame}
                    jobs[cache_key] = (path, expected.lower(), frame)
                except OSError:
                    slots[key] = {'error': 'source_unavailable'}
                except (KeyError, TypeError, ValueError) as error:
                    safe = str(error) if str(error) in ('source_hash_mismatch', 'source_frame_invalid',
                                                       'source_identity_unavailable') else 'source_identity_unavailable'
                    slots[key] = {'error': safe}

    def build_job(item):
        cache_key, (path, expected, frame) = item
        destination = output / 'meshes' / (cache_key + '.json')
        try:
            result = _run_worker(path, destination, expected_hash=expected,
                                 coordinate_frame=frame, timeout=timeout)
        except Exception:
            result = {'status': 'failed', 'error': 'mesh_worker_unavailable'}
        mesh = None
        if result.get('status') == 'completed':
            try:
                mesh = json.loads(destination.read_text(encoding='utf-8'))
                validate_mesh(mesh)
                # Re-check after subprocess completion before publishing a slot.
                if _sha256(path) != expected:
                    raise ValueError('source_hash_mismatch')
            except (OSError, ValueError, TypeError):
                result = {'status': 'failed', 'error': 'mesh_invalid'}
        if result.get('status') != 'completed':
            destination.unlink(missing_ok=True)
            destination.with_suffix('.tmp').unlink(missing_ok=True)
            return cache_key, {'error': result.get('error', 'mesh_failed')}
        return cache_key, {'mesh': mesh, 'url': destination.relative_to(output).as_posix()}

    with ThreadPoolExecutor(max_workers=max(1, min(4, int(workers)))) as pool:
        generated = dict(pool.map(build_job, jobs.items()))
    displays = {}
    for case in model_sources:
        views = {}
        for view in ('world', 'scoring'):
            row = {'status': 'unavailable', 'reference': None, 'candidate': None,
                   'thumbnail': None, 'metadata': {'units': 'mm', 'coordinate_frame': view,
                   'source_hashes': {}, 'frames': {}, 'quality': {}, 'role_errors': {},
                   'generator': fingerprint}, 'error': None}
            meshes = {}
            for role in ('reference', 'candidate'):
                slot = slots.get((case, view, role), {'error': 'source_unavailable'})
                built = generated.get(slot.get('cache_key'), slot)
                if 'error' in built:
                    row['metadata']['role_errors'][role] = built['error']
                    continue
                meshes[role] = built['mesh']
                row[role] = built['url']
                row['metadata']['source_hashes'][role] = slot['sha256']
                row['metadata']['frames'][role] = slot['frame']
                row['metadata']['quality'][role] = {key: built['mesh'].get(key) for key in QUALITY_KEYS}
            if meshes:
                row['metadata']['display_origin_mm'] = [
                    min(min(mesh['vertices'][axis::3]) for mesh in meshes.values()) / 2 +
                    max(max(mesh['vertices'][axis::3]) for mesh in meshes.values()) / 2
                    for axis in range(3)]
                row['metadata']['display_origin_policy'] = 'shared_translation_only'
                row['status'] = ('completed' if len(meshes) == 2 and
                    all(mesh.get('missingFaceCount', 0) == 0 for mesh in meshes.values()) else 'partial')
                filename = hashlib.sha256(str(case).encode('utf-8')).hexdigest()[:24] + '-' + view + '.png'
                preview = output / 'previews' / filename
                try:
                    preview_result = _run_preview({role: output / row[role] for role in meshes},
                                                  preview, timeout=timeout)
                except Exception:
                    preview_result = {'status': 'failed', 'error': 'preview_worker_unavailable'}
                preview_meta = {'status': preview_result['status'], 'format': None,
                    'panels': ['candidate', 'reference'], 'shared_camera': True,
                    'legend': {'candidate': '生成模型（橙色，左）', 'reference': '标准答案（蓝色，右）'}}
                if preview_result['status'] == 'completed':
                    row['thumbnail'] = preview.relative_to(output).as_posix()
                    preview_meta.update(preview_result['metadata'])
                    preview_meta['format'] = 'png'
                elif sum(len(mesh['indices'])//3 for mesh in meshes.values()) <= SVG_TRIANGLE_LIMIT:
                    preview = preview.with_suffix('.svg')
                    preview.write_text(svg_preview(meshes), encoding='utf-8')
                    row['thumbnail'] = preview.relative_to(output).as_posix()
                    preview_meta.update({'status': 'fallback', 'format': 'svg',
                                         'error': preview_result['error']})
                else:
                    preview_meta['error'] = preview_result['error']
                    row['status'] = 'partial'
                row['metadata']['preview'] = preview_meta
            if row['status'] != 'completed':
                row['error'] = ('preview_incomplete' if meshes else 'display_unavailable')
            views[view] = row
        statuses = [item['status'] for item in views.values()]
        status = 'completed' if all(s == 'completed' for s in statuses) else (
            'unavailable' if all(s == 'unavailable' for s in statuses) else 'partial')
        displays[case] = {'views': views, 'status': status}
    return displays

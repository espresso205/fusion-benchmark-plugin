"""Private, isolated STEP-to-display worker. No evaluator imports or writes."""
import hashlib
import json
from pathlib import Path
import sys


def read_step_mesh(source, expected):
    # Imports remain inside the child operation so plain report generation is
    # usable in a standard-library-only environment.
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_Reader
    from OCP.OSD import OSD_Parallel, OSD_ThreadPool
    if __package__:
        from .display_mesh import display_mesh
        from .display_assets import validate_mesh
    else:
        from display_mesh import display_mesh
        from display_assets import validate_mesh
    source = Path(source)
    with source.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
            raise ValueError('source_hash_mismatch')
    OSD_Parallel.SetUseOcctThreads_s(True)
    pool = OSD_ThreadPool.DefaultPool_s(); pool.Init(2); pool.SetNbDefaultThreadsToLaunch(2)
    reader = STEPControl_Reader()
    Interface_Static.SetCVal_s('xstep.cascade.unit', 'MM')
    if reader.ReadFile(str(source)) != IFSelect_RetDone:
        raise ValueError('step_read_failed')
    reader.SetSystemLengthUnit(1.0)
    if reader.TransferRoots() <= 0:
        raise ValueError('step_transfer_failed')
    shape = reader.OneShape()
    if shape.IsNull():
        raise ValueError('step_empty')
    mesh = display_mesh(shape)
    validate_mesh(mesh)
    with source.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
            raise ValueError('source_hash_mismatch')
    return mesh


def generate(request):
    if __package__:
        from .display_assets import dependency_versions
    else:
        from display_assets import dependency_versions
    expected = request['expected_hash']
    mesh = read_step_mesh(request['source'], expected)
    mesh.update({'units': 'mm', 'source_sha256': expected,
                 'coordinate_frame': request['coordinate_frame'], 'dependencies': dependency_versions()})
    destination = Path(request['destination'])
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(json.dumps(mesh, separators=(',', ':'), allow_nan=False), encoding='utf-8')
    temporary.replace(destination)


def generate_difference(request):
    if __package__:
        from .display_comparison import compare_meshes, unavailable
    else:
        from display_comparison import compare_meshes, unavailable
    interface = request.get('interface')
    scope = 'interface' if interface is not None else 'whole_model'
    try:
        meshes = {role: json.loads(Path(path).read_text(encoding='utf-8'))
                  for role, path in request['meshes'].items()}
        regions = None
        if interface is not None:
            if interface.get('gt_sha256') != request.get('reference_hash'):
                raise ValueError('interface_reference_mismatch')
            if interface.get('error') or not interface.get('regions'):
                raise ValueError('interface_input_unavailable')
            regions = []
            for region in interface['regions']:
                if region.get('fit_type') not in ('KOR', 'KIR') or not Path(region['path']).is_absolute():
                    raise ValueError('interface_input_unavailable')
                try:
                    mesh = read_step_mesh(region['path'], region['sha256'])
                except ValueError as error:
                    if str(error) == 'source_hash_mismatch':
                        raise ValueError('interface_source_hash_mismatch') from None
                    raise ValueError('interface_input_unavailable') from None
                regions.append({'mesh': mesh, 'fit_type': region['fit_type']})
        result = compare_meshes(meshes, regions=regions)
        for key, mesh in result['layers'].items():
            if mesh is not None:
                destination = Path(request['destinations'][key])
                temporary = destination.with_suffix('.tmp')
                temporary.write_text(json.dumps(mesh, separators=(',', ':'), allow_nan=False), encoding='utf-8')
                temporary.replace(destination)
                result['layers'][key] = True
    except Exception as error:
        allowed = {'interface_reference_mismatch', 'interface_input_unavailable', 'interface_source_hash_mismatch'}
        result = unavailable(scope, str(error) if str(error) in allowed else 'difference_worker_failed')
    Path(request['metadata_path']).write_text(json.dumps(result), encoding='utf-8')


def render_preview(request):
    """Load one scene and render four split and four overlay orthographic views."""
    import math
    import numpy as np
    import vtkmodules.vtkRenderingOpenGL2  # Register the native offscreen backend.
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkPolyData, vtkCellArray
    from vtkmodules.vtkFiltersCore import vtkPolyDataNormals
    from vtkmodules.vtkIOImage import vtkPNGWriter
    from vtkmodules.vtkRenderingCore import (vtkActor, vtkCamera, vtkPolyDataMapper,
        vtkRenderer, vtkRenderWindow, vtkWindowToImageFilter)
    from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray
    if __package__:
        from .display_assets import validate_mesh, preview_camera, preview_paths, VIEW_DIRECTIONS
    else:
        from display_assets import validate_mesh, preview_camera, preview_paths, VIEW_DIRECTIONS
    meshes = {}
    for role in ('candidate', 'reference'):
        path = request['meshes'].get(role)
        if path:
            mesh = json.loads(Path(path).read_text(encoding='utf-8'))
            validate_mesh(mesh); meshes[role] = mesh
    layers = {}
    for role, path in request.get('layers', {}).items():
        mesh = json.loads(Path(path).read_text(encoding='utf-8'))
        validate_mesh(mesh); layers[role] = mesh
    width, height = 960, 360
    settings = preview_camera(meshes, width, height)
    camera = vtkCamera(); camera.ParallelProjectionOn()
    distance = settings['distance_mm']
    camera.SetFocalPoint(0, 0, 0)
    camera.SetParallelScale(settings['parallel_scale_mm'])
    camera.SetClippingRange(max(.001, distance/10000), distance*100)
    window = vtkRenderWindow()
    window.SetOffScreenRendering(1); window.SetShowWindow(False)
    window.SetSize(width, height); window.SetMultiSamples(0); window.SetAlphaBitPlanes(1)
    retained, actors = [], {}
    colors = {'candidate': (.925, .541, .196), 'reference': (.145, .549, .729),
              'matched': (.145, .549, .729), 'extra': (.863, .235, .235),
              'missing': (.898, .667, .133)}
    try:
        for role, mesh in {**meshes, **layers}.items():
            vertices = np.asarray(mesh['vertices'], dtype=np.float64).reshape(-1, 3)
            vertices -= np.asarray(settings['origin_mm'])
            points = vtkPoints(); points.SetData(numpy_to_vtk(vertices, deep=True))
            indices = np.asarray(mesh['indices'], dtype=np.int64)
            cells = vtkCellArray(); cells.SetData(
                numpy_to_vtkIdTypeArray(np.arange(0, len(indices)+1, 3, dtype=np.int64), deep=True),
                numpy_to_vtkIdTypeArray(indices, deep=True))
            poly = vtkPolyData(); poly.SetPoints(points); poly.SetPolys(cells)
            normals = vtkPolyDataNormals(); normals.SetInputData(poly)
            normals.SetFeatureAngle(35); normals.ConsistencyOn(); normals.SplittingOn()
            mapper = vtkPolyDataMapper(); mapper.SetInputConnection(normals.GetOutputPort())
            actor = vtkActor(); actor.SetMapper(mapper)
            actor.GetProperty().SetColor(*colors[role])
            actor.GetProperty().SetAmbient(.24); actor.GetProperty().SetDiffuse(.76)
            actor.GetProperty().SetSpecular(.12); actor.GetProperty().SetSpecularPower(20)
            actors[role] = actor; retained.append((poly, normals, mapper, actor))
        renderers = []
        for panel in range(2):
            renderer = vtkRenderer(); renderer.SetViewport(panel/2, 0, (panel+1)/2, 1)
            renderer.SetBackground(1, 1, 1); renderer.SetActiveCamera(camera)
            renderer.SetUseFXAA(True)
            renderers.append(renderer)
        overlay = vtkRenderer(); overlay.SetBackground(1, 1, 1); overlay.SetActiveCamera(camera)
        overlay.SetUseDepthPeeling(True); overlay.SetMaximumNumberOfPeels(100)
        overlay.SetOcclusionRatio(0); overlay.SetUseFXAA(True)
        cameras = {}
        for angle, modes in preview_paths(request['destination']).items():
            current = preview_camera(meshes, width, height, angle=angle); cameras[angle] = current
            direction = current['direction']; norm = math.sqrt(sum(v*v for v in direction))
            camera.SetPosition(*(distance*v/norm for v in direction)); camera.SetViewUp(*current['up'])
            for mode, destination in modes.items():
                for renderer in [*renderers, overlay]:
                    window.RemoveRenderer(renderer); renderer.RemoveAllViewProps()
                for role, actor in actors.items():
                    actor.GetProperty().SetOpacity(1); actor.GetProperty().SetColor(*colors[role])
                if mode == 'split':
                    for renderer, role in zip(renderers, ('candidate', 'reference')):
                        window.AddRenderer(renderer)
                        if role in actors:
                            renderer.AddActor(actors[role])
                else:
                    window.AddRenderer(overlay)
                    for role in (list(layers) + ['candidate'] if layers else list(meshes)):
                        if role in actors:
                            actor = actors[role]
                            opacity = {'candidate': .12, 'matched': .3, 'extra': .85, 'missing': .85}.get(role, .5)
                            actor.GetProperty().SetOpacity(opacity if layers else .5)
                            if layers and role == 'candidate':
                                actor.GetProperty().SetColor(.557, .580, .608)
                            overlay.AddActor(actor)
                window.Render()
                pixels = vtkWindowToImageFilter(); pixels.SetInput(window)
                pixels.SetInputBufferTypeToRGB(); pixels.ReadFrontBufferOff(); pixels.Update()
                temporary = destination.with_suffix('.tmp')
                writer = vtkPNGWriter(); writer.SetFileName(str(temporary)); writer.SetCompressionLevel(9)
                writer.SetInputConnection(pixels.GetOutputPort()); writer.Write()
                temporary.replace(destination)
        metadata = {'width': width, 'height': height, 'panels': ['candidate', 'reference'],
            'camera': settings, 'cameras': cameras,
            'overlay_kind': 'difference' if layers else 'ordinary_overlay',
            'rendered_triangle_counts': {
                role: len(mesh['indices'])//3 for role, mesh in meshes.items()}}
        Path(request['metadata_path']).write_text(json.dumps(metadata), encoding='utf-8')
    finally:
        window.Finalize()


if __name__ == '__main__':
    try:
        request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
        if request.get('operation') == 'preview':
            render_preview(request)
        elif request.get('operation') == 'difference':
            generate_difference(request)
        else:
            generate(request)
    except Exception:
        # Do not expose OCP errors containing private paths. The parent supplies
        # stable public failure labels and preserves the completed score.
        raise SystemExit(1)

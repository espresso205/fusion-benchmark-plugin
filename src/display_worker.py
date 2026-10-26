"""Private, isolated STEP-to-display worker. No evaluator imports or writes."""
import hashlib
import json
from pathlib import Path
import sys


def generate(request):
    # Imports remain inside the child operation so plain report generation is
    # usable in a standard-library-only environment.
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_Reader
    from OCP.OSD import OSD_Parallel, OSD_ThreadPool
    if __package__:
        from .display_mesh import display_mesh
        from .display_assets import dependency_versions, validate_mesh
    else:
        from display_mesh import display_mesh
        from display_assets import dependency_versions, validate_mesh
    source = Path(request['source'])
    expected = request['expected_hash']
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
    mesh.update({'units': 'mm', 'source_sha256': expected,
                 'coordinate_frame': request['coordinate_frame'], 'dependencies': dependency_versions()})
    destination = Path(request['destination'])
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(json.dumps(mesh, separators=(',', ':'), allow_nan=False), encoding='utf-8')
    temporary.replace(destination)


def render_preview(request):
    """Render complete meshes offscreen; both viewports share one camera."""
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
        from .display_assets import validate_mesh, preview_camera
    else:
        from display_assets import validate_mesh, preview_camera
    meshes = {}
    for role in ('candidate', 'reference'):
        path = request['meshes'].get(role)
        if path:
            mesh = json.loads(Path(path).read_text(encoding='utf-8'))
            validate_mesh(mesh); meshes[role] = mesh
    width, height = 960, 360
    settings = preview_camera(meshes, width, height)
    camera = vtkCamera(); camera.ParallelProjectionOn()
    distance = settings['distance_mm']
    camera.SetPosition(distance/math.sqrt(3), -distance/math.sqrt(3), distance/math.sqrt(3))
    camera.SetFocalPoint(0, 0, 0); camera.SetViewUp(0, 0, 1)
    camera.SetParallelScale(settings['parallel_scale_mm'])
    camera.SetClippingRange(max(.001, distance/10000), distance*100)
    window = vtkRenderWindow()
    window.SetOffScreenRendering(1); window.SetShowWindow(False)
    window.SetSize(width, height); window.SetMultiSamples(4)
    retained = []
    try:
        for panel, role in enumerate(('candidate', 'reference')):
            renderer = vtkRenderer(); renderer.SetViewport(panel/2, 0, (panel+1)/2, 1)
            renderer.SetBackground(1, 1, 1); renderer.SetActiveCamera(camera)
            window.AddRenderer(renderer)
            mesh = meshes.get(role)
            if not mesh:
                continue
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
            actor.GetProperty().SetColor(*((.925, .541, .196) if role == 'candidate' else (.145, .549, .729)))
            actor.GetProperty().SetAmbient(.24); actor.GetProperty().SetDiffuse(.76)
            actor.GetProperty().SetSpecular(.12); actor.GetProperty().SetSpecularPower(20)
            renderer.AddActor(actor); retained.append((poly, normals, mapper, actor))
        window.Render()
        pixels = vtkWindowToImageFilter(); pixels.SetInput(window)
        pixels.SetInputBufferTypeToRGB(); pixels.ReadFrontBufferOff(); pixels.Update()
        destination = Path(request['destination']); temporary = destination.with_suffix('.tmp')
        writer = vtkPNGWriter(); writer.SetFileName(str(temporary)); writer.SetCompressionLevel(9)
        writer.SetInputConnection(pixels.GetOutputPort()); writer.Write()
        temporary.replace(destination)
        metadata = {'width': width, 'height': height, 'panels': ['candidate', 'reference'],
            'camera': settings, 'rendered_triangle_counts': {
                role: len(mesh['indices'])//3 for role, mesh in meshes.items()}}
        Path(request['metadata_path']).write_text(json.dumps(metadata), encoding='utf-8')
    finally:
        window.Finalize()


if __name__ == '__main__':
    try:
        request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
        if request.get('operation') == 'preview':
            render_preview(request)
        else:
            generate(request)
    except Exception:
        # Do not expose OCP errors containing private paths. The parent supplies
        # stable public failure labels and preserves the completed score.
        raise SystemExit(1)

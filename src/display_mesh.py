# Adapted from CADViewer; Copyright (c) 2026 earthtojake (MIT).
# License: report_assets/licenses/CADViewer-MIT.txt
"""Display-only tessellation: complete faces on an isolated shape copy.

This module must never supply geometry to measurement or scoring functions.
"""

DISPLAY_VERSION = 'report-complete-faces-v1'


def _simplify(vertices, indices, target=140000):
    """Edge-collapse simplification preserves topology instead of dropping faces."""
    import numpy as np
    if len(indices)//3 <= target:
        return vertices, indices, False
    try:
        from vtkmodules.vtkCommonCore import vtkPoints
        from vtkmodules.vtkCommonDataModel import vtkPolyData, vtkCellArray
        from vtkmodules.vtkFiltersCore import vtkCleanPolyData, vtkDecimatePro, vtkPolyDataNormals
        from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray, vtk_to_numpy
    except ImportError:
        return vertices, indices, False
    points = vtkPoints()
    points.SetData(numpy_to_vtk(np.asarray(vertices).reshape(-1,3),deep=True))
    faces = np.asarray(indices,dtype=np.int64).reshape(-1,3)
    cells = vtkCellArray()
    cells.SetData(numpy_to_vtkIdTypeArray(np.arange(0,len(faces)*3+1,3,dtype=np.int64),deep=True),numpy_to_vtkIdTypeArray(faces.ravel(),deep=True))
    poly = vtkPolyData(); poly.SetPoints(points); poly.SetPolys(cells)
    clean = vtkCleanPolyData(); clean.SetInputData(poly)
    clean.ToleranceIsAbsoluteOn(); clean.SetAbsoluteTolerance(1e-6)
    simplify = vtkDecimatePro(); simplify.SetInputConnection(clean.GetOutputPort())
    simplify.PreserveTopologyOn(); simplify.SplittingOff(); simplify.BoundaryVertexDeletionOff()
    simplify.SetFeatureAngle(35); simplify.SetTargetReduction(1-target/len(faces))
    normals = vtkPolyDataNormals(); normals.SetInputConnection(simplify.GetOutputPort())
    normals.SplittingOn(); normals.SetFeatureAngle(35); normals.ConsistencyOn(); normals.Update()
    result = normals.GetOutput()
    if not result.GetNumberOfPolys():
        return vertices, indices, False
    positions = np.round(vtk_to_numpy(result.GetPoints().GetData()),5).ravel().tolist()
    triangles = vtk_to_numpy(result.GetPolys().GetConnectivityArray()).ravel().tolist()
    return positions, triangles, True


def display_mesh(shape):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
    from OCP.BRepTools import BRepTools
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRep import BRep_Tool
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS
    import numpy as np

    # The source can have extremely fine measurement meshes. Never clean or
    # remesh it in place, as OCC topology holds mutable triangulation caches.
    copied = BRepBuilderAPI_Copy(shape, False, False).Shape()
    box = Bnd_Box()
    BRepBndLib.Add_s(copied, box, False)
    bounds = box.Get()
    diag = sum((bounds[i+3]-bounds[i])**2 for i in range(3))**.5
    deflection = max(.02, diag * .000025)
    for attempt in range(5):
        BRepTools.Clean_s(copied)
        BRepMesh_IncrementalMesh(copied, deflection, False, min(.35 + attempt*.12, .8), True)
        faces, total, missing = [], 0, 0
        explorer = TopExp_Explorer(copied, TopAbs_FACE)
        while explorer.More():
            face = TopoDS.Face_s(explorer.Current())
            location = TopLoc_Location()
            tri = BRep_Tool.Triangulation_s(face, location)
            if tri is not None and tri.NbTriangles():
                faces.append((tri, location.Transformation(), face.Orientation()))
                total += tri.NbTriangles()
            else:
                missing += 1
            explorer.Next()
        if total <= 500000:
            break
        if attempt < 4:
            deflection *= 2
    if total > 1000000:
        raise RuntimeError('Complete display mesh exceeds one million triangles; no disconnected fragments were substituted')
    vertices, indices, offset = [], [], 0
    for tri, transform, orientation in faces:
        points = np.array([tri.Node(i).Coord() for i in range(1, tri.NbNodes()+1)])
        matrix = np.array([[transform.Value(i,j) for j in range(1,5)] for i in range(1,4)])
        points = points @ matrix[:,:3].T + matrix[:,3]
        triangles = np.array([tri.Triangle(i).Get() for i in range(1,tri.NbTriangles()+1)]) - 1
        if orientation == TopAbs_REVERSED:
            triangles = triangles[:, ::-1]
        # Round transport coordinates only. CAD objects and measured numbers
        # are untouched. Shared face vertices keep smooth cylinder normals.
        vertices.extend(np.round(points, 5).ravel().tolist())
        indices.extend((triangles+offset).ravel().tolist())
        offset += len(points)
    vertices, indices, simplified = _simplify(vertices, indices)
    return {'vertices':vertices, 'indices':indices, 'sourceTriangleCount':total,
            'renderTriangleCount':len(indices)//3, 'topologySimplified':simplified,
            'truncated':False, 'displayVersion':DISPLAY_VERSION, 'faceCount':len(faces),
            'missingFaceCount':missing, 'linearDeflectionMm':deflection,
            'approximation':'complete-coarse-cad-faces', 'coordinateDecimals':5}


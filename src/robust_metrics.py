"""V3 measurements: independent solid validation and explicit approximate evidence.

No candidate-based plane search, missing-score imputation, automatic healing,
or changes to v2 are performed here. The caller owns process time limits.
"""
from functools import lru_cache
import hashlib
import math
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
from OCP.BRepCheck import BRepCheck_Analyzer, BRepCheck_Shell, BRepCheck_NoError
from OCP.BRepGProp import BRepGProp
from OCP.BOPAlgo import BOPAlgo_ArgumentAnalyzer, BOPAlgo_SelfIntersect
from OCP.Bnd import Bnd_Box
from OCP.GProp import GProp_GProps
from OCP.TopAbs import TopAbs_SOLID, TopAbs_SHELL, TopAbs_FACE, TopAbs_EDGE, TopAbs_VERTEX
from OCP.TopExp import TopExp
from OCP.TopTools import TopTools_IndexedMapOfShape
from OCP.TopoDS import TopoDS

from .metrics import read_step, mass_properties, result
from .section import section_metrics as _exact_section
from cadgenbench.common.mesh import tessellate_shape
from cadgenbench.eval.sampling import sample_points_and_normals_from_mesh

MASS_RELATIVE_BUDGET = 1e-6
INTERFERENCE_ABSOLUTE_MM3 = 1e-7
INTERFERENCE_RELATIVE = 1e-9
SURFACE_DISTANCE_MM = 0.05
SURFACE_NORMAL_DEGREES = 5.0
SURFACE_SAMPLES = 20000


def _key(path):
    p = Path(path).resolve()
    return str(p), hashlib.sha256(p.read_bytes()).hexdigest()


@lru_cache(maxsize=24)
def _loaded(path, digest):
    return read_step(path)


def _shape(path):
    return _loaded(*_key(path))[0]


def _mapped(shape, kind):
    out = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, kind, out)
    return out


def _bounds(shape):
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box)
    return list(box.Get())


def _diagonal(shape):
    b = _bounds(shape)
    return math.dist(b[:3], b[3:])


def _self_intersections(shape):
    checker = BOPAlgo_ArgumentAnalyzer()
    checker.SetShape1(shape)
    checker.SelfInterMode = True
    checker.Perform()
    if checker.HasErrors():
        raise RuntimeError('OCCT self-intersection checker failed')
    findings = []
    for finding in checker.GetCheckResult():
        if finding.GetCheckStatus() == BOPAlgo_SelfIntersect:
            findings.append({'status': 'self_intersection',
                             'faulty_subshape_count': finding.GetFaultyShapes1().Size(),
                             'faulty_subshapes':[{'type':str(s.ShapeType()),'bbox_mm':_bounds(s)}
                                                for s in finding.GetFaultyShapes1()]})
        else:
            raise RuntimeError('unexpected self-intersection checker result: '+str(finding.GetCheckStatus()))
    return findings


def _validate_shape(shape, units):
    solids = _mapped(shape, TopAbs_SOLID)
    out = {'healthy': False, 'working_unit': 'mm', 'declared_length_units': units,
           'solid_count': solids.Size(), 'issues': [], 'solids': [], 'interferences': [],
           'validation_backend': 'OCCT BRepCheck + BOPAlgo self-intersection + solid Common',
           'interference_tolerance': {'absolute_mm3': INTERFERENCE_ABSOLUTE_MM3,
                                      'relative_to_smaller_solid': INTERFERENCE_RELATIVE}}
    if not solids.Size():
        out['issues'].append('no_solid')
        return out
    for kind in (TopAbs_SHELL, TopAbs_FACE, TopAbs_EDGE, TopAbs_VERTEX):
        all_parts = _mapped(shape, kind)
        owned = TopTools_IndexedMapOfShape()
        for solid in solids:
            TopExp.MapShapes_s(solid, kind, owned)
        if any(not owned.Contains(part) for part in all_parts):
            out['issues'].append('residual_geometry')
            break
    volumes, boxes = [], []
    for index, raw_solid in enumerate(solids):
        solid = TopoDS.Solid_s(raw_solid)
        valid = bool(BRepCheck_Analyzer(solid).IsValid())
        shells = _mapped(solid, TopAbs_SHELL)
        closed = shells.Size() > 0 and all(BRepCheck_Shell(TopoDS.Shell_s(s)).Closed() == BRepCheck_NoError for s in shells)
        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(solid, props)
        volume = props.Mass()
        bounds = _bounds(solid)
        finite = math.isfinite(volume) and volume > 0 and all(map(math.isfinite,bounds))
        row = {'index': index, 'brep_valid': valid, 'closed': closed, 'volume_mm3':volume,'bbox_mm':bounds}
        if not valid or not closed or not finite:
            out['issues'].append('invalid_solid')
        else:
            row['self_intersections'] = _self_intersections(solid)
            if row['self_intersections']:
                out['issues'].append('self_intersection')
        out['solids'].append(row)
        volumes.append(volume)
        boxes.append(bounds)
    if out['issues']:
        return out
    for i in range(solids.Size()):
        for j in range(i):
            if any(min(boxes[i][a+3],boxes[j][a+3]) <= max(boxes[i][a],boxes[j][a]) for a in range(3)):
                continue
            algo = BRepAlgoAPI_Common(solids.FindKey(i+1),solids.FindKey(j+1))
            algo.SetNonDestructive(True)
            algo.Build()
            if not algo.IsDone():
                raise RuntimeError('solid interference Boolean failed')
            props=GProp_GProps()
            BRepGProp.VolumeProperties_s(algo.Shape(),props)
            volume=abs(props.Mass())
            if not math.isfinite(volume):
                raise RuntimeError('nonfinite solid intersection volume')
            tolerance=max(INTERFERENCE_ABSOLUTE_MM3, INTERFERENCE_RELATIVE*min(volumes[i],volumes[j]))
            if volume>tolerance:
                out['interferences'].append({'solid_indices':[j,i],'volume_mm3':volume,'tolerance_mm3':tolerance})
    if out['interferences']:
        out['issues'].append('solid_interference')
    out['healthy']=not out['issues']
    out['brep_valid']=all(s['brep_valid'] for s in out['solids'])
    out['signed_volume_mm3']=sum(volumes)
    return out


def validity(gt, candidate, artifacts=None):
    """Geometry only: metric qualification never invalidates a candidate."""
    raw={}
    candidate_valid=False
    for role,path in [('candidate',candidate),('gt',gt)]:
        try:
            shape,units=_loaded(*_key(path))
        except ValueError as exc:
            raw[role]={'healthy':False,'error':str(exc)}
            return result('reference_invalid' if role=='gt' else 'candidate_invalid',raw=raw,
                          reference_valid=role!='gt',candidate_valid=candidate_valid,
                          candidate_invalid_confirmed=role=='candidate')
        except Exception as exc:
            return result('evaluator_error',raw={**raw,'error':str(exc),'stage':role+'_validation'},
                          candidate_invalid_confirmed=False,validation_inconclusive=True,candidate_valid=candidate_valid)
        try:
            info=_validate_shape(shape,units)
            raw[role]=info
        except Exception as exc:
            return result('evaluator_error',raw={**raw,'error':str(exc),'stage':role+'_validation'},
                          candidate_invalid_confirmed=False,validation_inconclusive=True,candidate_valid=candidate_valid)
        if not info['healthy']:
            return result('reference_invalid' if role=='gt' else 'candidate_invalid',raw=raw,
                          reference_valid=role!='gt',candidate_valid=candidate_valid,
                          candidate_invalid_confirmed=role=='candidate')
        if role=='candidate':candidate_valid=True
    return result(score=1.,raw=raw,reference_valid=True,candidate_valid=True,
                  candidate_invalid_confirmed=False)


def _properties(props, backend, **extra):
    m=props.MatrixOfInertia()
    tensor=np.array([[m.Value(i,j) for j in (1,2,3)] for i in (1,2,3)])
    moments=np.sort(np.linalg.eigvalsh(tensor))
    c=props.CentreOfMass()
    out={'volume_mm3':props.Mass(),'centroid_mm':[c.X(),c.Y(),c.Z()],
         'principal_moments':moments.tolist(),'backend':backend,**extra}
    _check_properties(out)
    return out


def _check_properties(out):
    if not np.isfinite([out['volume_mm3'],*out['centroid_mm'],*out['principal_moments']]).all():
        raise ValueError('nonfinite mass measurement')
    if out['volume_mm3']<=0 or min(out['principal_moments'])<=0:
        raise ValueError('nonpositive mass measurement')


def _spread(a,b):
    return {'volume_relative':abs(a['volume_mm3']-b['volume_mm3'])/b['volume_mm3'],
            'inertia_relative':max(abs(x-y)/y for x,y in zip(a['principal_moments'],b['principal_moments'])),
            'centroid_mm':math.dist(a['centroid_mm'],b['centroid_mm'])}


def _converged(spread, diagonal):
    return (spread['volume_relative']<=MASS_RELATIVE_BUDGET and
            spread['inertia_relative']<=MASS_RELATIVE_BUDGET and
            spread['centroid_mm']<=max(1e-8,diagonal*MASS_RELATIVE_BUDGET))


@lru_cache(maxsize=32)
def _mesh(path,digest,deflection):
    shape=_loaded(path,digest)[0]
    copied=BRepBuilderAPI_Copy(shape,True,False).Shape()
    mesh=tessellate_shape(copied,deflection,angular_deflection_rad=0.1)
    if len(mesh.triangles)>2_000_000:
        raise RuntimeError('v3 mesh triangle budget exceeded')
    return mesh


def _mesh_for(path,deflection):
    return _mesh(*_key(path),float(deflection))


@lru_cache(maxsize=24)
def _mass_measure(path,digest):
    shape=_loaded(path,digest)[0]
    diagonal=_diagonal(shape)
    attempts=[]
    try:
        quick=[mass_properties(shape,eps) for eps in (1e-6,1e-8)]
        spread=_spread(*quick)
        attempts.append({'backend':'OCCT adaptive Gauss','eps':[1e-6,1e-8],'spread':spread})
        if _converged(spread,diagonal):
            return {**quick[-1],'backend':'OCCT adaptive Gauss','converged':True,'spread':spread,'attempts':attempts}
    except Exception as exc:
        attempts.append({'backend':'OCCT adaptive Gauss','error':str(exc)})
    try:
        refined=[]
        for eps in (1e-6,1e-8):
            props=GProp_GProps()
            error=BRepGProp.VolumePropertiesGK_s(shape,props,eps,True,True,True,True,False)
            if not math.isfinite(error) or error<0:
                raise RuntimeError('Gauss-Kronrod integration failed')
            refined.append(_properties(props,'OCCT Gauss-Kronrod',integration_eps=eps,integration_error_estimate=error))
        spread=_spread(*refined)
        attempts.append({'backend':'OCCT Gauss-Kronrod','eps':[1e-6,1e-8],'spread':spread})
        if _converged(spread,diagonal):
            return {**refined[-1],'converged':True,'spread':spread,'attempts':attempts}
    except Exception as exc:
        attempts.append({'backend':'OCCT Gauss-Kronrod','error':str(exc)})
    import trimesh
    previous=None
    for factor in (1e-4,5e-5,2.5e-5,1.25e-5):
        deflection=max(1e-5,diagonal*factor)
        mesh=_mesh(path,digest,deflection)
        # A local origin reduces cancellation for models far from world zero.
        origin=mesh.vertices.mean(axis=0)
        tm=trimesh.Trimesh(vertices=mesh.vertices-origin,faces=mesh.triangles,process=False)
        if not tm.is_volume:
            raise RuntimeError('mass mesh is not a consistently oriented positive volume')
        mp=tm.mass_properties
        current={'volume_mm3':float(mp.volume),'centroid_mm':(mp.center_mass+origin).tolist(),
                 'principal_moments':np.linalg.eigvalsh(mp.inertia).tolist(),
                 'backend':'closed triangular polyhedron integration','deflection_mm':deflection}
        _check_properties(current)
        if previous is not None:
            spread=_spread(previous,current)
            attempts.append({'backend':current['backend'],'deflection_mm':deflection,'spread':spread})
            if _converged(spread,diagonal):
                return {**current,'converged':True,'spread':spread,'attempts':attempts}
        previous=current
    # A finite measured approximation is retained with an explicit unresolved
    # convergence warning, never relabeled as an exact/validated measurement.
    return {**current,'converged':False,'spread':spread,'attempts':attempts}


def mass(gt,candidate):
    try:
        g=_mass_measure(*_key(gt));c=_mass_measure(*_key(candidate))
        rv=abs(c['volume_mm3']-g['volume_mm3'])/g['volume_mm3']
        ri=max(abs(x-y)/y for x,y in zip(c['principal_moments'],g['principal_moments']))
        score=math.exp(-(rv+ri)/0.1)
        converged=g['converged'] and c['converged']
        if not converged:
            return result('evaluator_error',raw={'gt':g,'candidate':c,
                          'score_estimate_diagnostic_only':score,
                          'error':'mass refinement did not meet the frozen numerical budget'},
                          convergence_validated=False,measurement_unreliable=True)
        out=result(score=score,raw={'gt':g,'candidate':c,'volume_relative_error':rv,
                   'principal_moment_relative_error':ri,'scales':{'volume':0.05,'inertia':0.05},
                   'relative_convergence_budget':MASS_RELATIVE_BUDGET},reference_valid=True,
                   scored_approximate=True,convergence_validated=converged)
        out['backend']={'gt':g['backend'],'candidate':c['backend']}
        out['uncertainty']={'kind':'empirical_refinement_difference','gt':g['spread'],'candidate':c['spread'],
                            'rigorous_bound':False,'converged':converged}
        return out
    except Exception as exc:
        return result('evaluator_error',raw={'error':str(exc),'stage':'mass_measurement'})


def face_match(gt,candidate,ann=None):
    try:
        diagonal=_diagonal(_shape(gt))
        deflection=max(1e-5,min(SURFACE_DISTANCE_MM/8,diagonal*2e-5))
        meshes=[_mesh_for(p,deflection) for p in (gt,candidate)]
        samples=[]
        for seed in (0,1):
            directional=[]
            for i in (0,1):
                a,b=meshes[i],meshes[1-i]
                points,normals=sample_points_and_normals_from_mesh(a.vertices,a.triangles,SURFACE_SAMPLES,seed,
                                                                 smooth_normals=False)
                distances,other_normals=_closest_surface(points,b)
                dots=np.abs(np.einsum('ij,ij->i',normals,other_normals))
                hits=(distances<=SURFACE_DISTANCE_MM)&(dots>=math.cos(math.radians(SURFACE_NORMAL_DEGREES)))
                directional.append(float(hits.mean()))
            recall,precision=directional
            f1=2*precision*recall/(precision+recall) if precision+recall else 0.
            samples.append({'seed':seed,'precision':precision,'recall':recall,'f1':f1})
        score=float(np.mean([s['f1'] for s in samples]))
        out=result(score=score,raw={'definition':'bidirectional area-weighted trimmed-surface coverage',
                   'seed_results':samples,'unassessed_area_fraction':0.,'deflection_mm':deflection,
                   'distance_tolerance_mm':SURFACE_DISTANCE_MM,'normal_tolerance_degrees':SURFACE_NORMAL_DEGREES,
                   'samples_per_side_per_seed':SURFACE_SAMPLES,'triangle_counts':[len(m.triangles) for m in meshes],
                   'limitations':['sampled coverage, not exact patch equality','facet-normal error bounded only empirically by tessellation refinement']},
                   scored_approximate=True,reference_valid=True)
        out['backend']='OCCT tessellation + Open3D closest-point surface coverage'
        out['uncertainty']={'kind':'two_seed_empirical_range','score_min':min(s['f1'] for s in samples),
                            'score_max':max(s['f1'] for s in samples),'rigorous_bound':False}
        return out
    except Exception as exc:
        return result('evaluator_error',raw={'error':str(exc),'stage':'surface_coverage'})


def _closest_surface(points,mesh):
    """Use facet normals; vertex smoothing across CAD sharp edges is invalid."""
    import open3d as o3d
    origin=mesh.vertices.mean(axis=0)
    scene=o3d.t.geometry.RaycastingScene()
    scene.add_triangles(o3d.t.geometry.TriangleMesh(
        o3d.core.Tensor((mesh.vertices-origin).astype(np.float32)),
        o3d.core.Tensor(mesh.triangles.astype(np.int32))))
    hit=scene.compute_closest_points(o3d.core.Tensor((points-origin).astype(np.float32)))
    foot=hit['points'].numpy().astype(np.float64)+origin
    triangles=mesh.vertices[mesh.triangles[hit['primitive_ids'].numpy().astype(np.int64)]]
    normals=np.cross(triangles[:,1]-triangles[:,0],triangles[:,2]-triangles[:,0])
    normals/=np.maximum(np.linalg.norm(normals,axis=1,keepdims=True),1e-30)
    return np.linalg.norm(points-foot,axis=1),normals


def build_section_plan(gt,ann):
    sections=ann.get('sections',[])
    if not sections or len({s['id'] for s in sections})!=len(sections):
        raise ValueError('nonempty uniquely identified section annotations required')
    offset=max(1e-3,_diagonal(_shape(gt))*1e-5)
    rows=[]
    for s in sections:
        if s['axis'] not in ('x','y','z') or isinstance(s['offset'],bool) or not math.isfinite(s['offset']):
            raise ValueError('invalid annotated section plane')
        rows.append({'id':s['id'],'axis':s['axis'],'original_offset_mm':s['offset'],
                     'positions_mm':[s['offset']-offset,s['offset']+offset]})
    return {'definition':'two_sided_fixed_offset_score_mean_v1','offset_mm':offset,
            'reference_sha256':_key(gt)[1],'sections':rows}


def _inside(point,polygon):
    x,y=point;inside=False
    for a,b in zip(polygon,np.roll(polygon,-1,axis=0)):
        if (a[1]>y)!=(b[1]>y) and x<(b[0]-a[0])*(y-a[1])/(b[1]-a[1])+a[0]:
            inside=not inside
    return inside


def _mesh_section(path,axis,offset,deflection):
    import trimesh
    mesh=_mesh_for(path,deflection)
    tm=trimesh.Trimesh(vertices=mesh.vertices,faces=mesh.triangles,process=False)
    idx=('x','y','z').index(axis)
    normal=np.eye(3)[idx];origin=normal*offset
    segments=trimesh.intersections.mesh_plane(tm,normal,origin)
    if len(segments)==0:
        return {'area_mm2':0.,'perimeter_mm':0.,'contours':0,'backend':'triangular mesh section',
                'quality':{'measurement_valid':True,'empty':True},'deflection_mm':deflection}
    points=segments.reshape(-1,3);parent=list(range(len(points)))
    def find(i):
        while parent[i]!=i:
            parent[i]=parent[parent[i]];i=parent[i]
        return i
    for a,b in cKDTree(points).query_pairs(1e-6):
        parent[find(a)]=find(b)
    groups={}
    for i in range(len(points)):groups.setdefault(find(i),[]).append(i)
    roots={r:i for i,r in enumerate(groups)}
    nodes=np.array([points[m].mean(axis=0) for m in groups.values()])
    edges=set()
    for i in range(len(segments)):
        a,b=roots[find(2*i)],roots[find(2*i+1)]
        if a!=b:edges.add(tuple(sorted((a,b))))
    adjacency={i:[] for i in range(len(nodes))}
    for a,b in edges:adjacency[a].append(b);adjacency[b].append(a)
    active={n for n,v in adjacency.items() if v}
    if any(len(adjacency[n])!=2 for n in active):
        raise RuntimeError('mesh section is not disjoint closed loops')
    loops=[];remaining=set(active)
    while remaining:
        start=min(remaining);order=[];prev=None;current=start
        while True:
            order.append(current);remaining.discard(current)
            nxt=next(n for n in adjacency[current] if n!=prev)
            prev,current=current,nxt
            if current==start:break
            if current not in remaining:raise RuntimeError('mesh section loop revisits vertex')
        loops.append(nodes[order][:,[i for i in range(3) if i!=idx]])
    areas=[abs(np.sum(p[:,0]*np.roll(p[:,1],-1)-p[:,1]*np.roll(p[:,0],-1)))/2 for p in loops]
    area=0.
    for i,p in enumerate(loops):
        depth=sum(_inside(p[0],other) for j,other in enumerate(loops) if i!=j and areas[j]>areas[i])
        area+=areas[i]*(-1)**depth
    perimeter=sum(float(np.linalg.norm(p-np.roll(p,-1,axis=0),axis=1).sum()) for p in loops)
    if area<0 or not math.isfinite(area+perimeter):raise RuntimeError('invalid mesh section measure')
    return {'area_mm2':area,'perimeter_mm':perimeter,'contours':len(loops),
            'quality':{'measurement_valid':True,'empty':False},'backend':'triangular mesh section',
            'deflection_mm':deflection}


def _measure_section(path,axis,offset,diagonal):
    try:
        exact=_exact_section(_shape(path),axis,offset)
    except Exception as exc:
        exact={'quality':{'measurement_valid':False,'issues':['OCCT section exception: '+str(exc)]}}
    if exact['quality']['measurement_valid']:
        return {**exact,'backend':'OCCT exact section curves'}
    coarse=_mesh_section(path,axis,offset,max(1e-5,diagonal*2e-5))
    fine=_mesh_section(path,axis,offset,max(1e-5,diagonal*1e-5))
    fine['exact_backend_issues']=exact['quality'].get('issues',[])
    fine['refinement_difference']={k:abs(fine[k]-coarse[k]) for k in ('area_mm2','perimeter_mm','contours')}
    if fine['contours']!=coarse['contours'] or any(
        fine['refinement_difference'][k]>max(1e-8,abs(fine[k])*1e-3) for k in ('area_mm2','perimeter_mm')):
        raise RuntimeError('mesh section refinement did not meet the frozen 0.001 relative budget')
    return fine


def _section_score(g,c):
    if g['area_mm2']==0:
        return 1. if c['area_mm2']==0 else 0.
    if c['area_mm2']==0:return 0.
    area=abs(c['area_mm2']-g['area_mm2'])/g['area_mm2']
    perimeter=abs(c['perimeter_mm']-g['perimeter_mm'])/g['perimeter_mm']
    return math.exp(-(area+perimeter)/0.05)*(1. if g['contours']==c['contours'] else 0.5)


def section(gt,candidate,ann):
    try:
        plan=ann.get('v3_section_plan') or build_section_plan(gt,ann)
        expected=build_section_plan(gt,ann)
        if plan!=expected:
            raise ValueError('frozen section plan differs from v3 GT-only definition')
        diagonal=_diagonal(_shape(gt));rows=[]
        for s in plan['sections']:
            sides=[]
            for position in s['positions_mm']:
                g=_measure_section(gt,s['axis'],position,diagonal)
                c=_measure_section(candidate,s['axis'],position,diagonal)
                sides.append({'position_mm':position,'gt':g,'candidate':c,'score':_section_score(g,c)})
            rows.append({**s,'sides':sides,'score':sum(v['score'] for v in sides)/2})
        out=result(score=sum(s['score'] for s in rows)/len(rows),raw={'plan':plan,'sections':rows,
                   'definition':'mean of paired side scores, then mean across annotated sections',
                   'scales':{'area_perimeter_relative':0.05}},reference_valid=True,scored_approximate=True)
        out['backend']='fixed double-sided section; OCCT exact curves with same-plane mesh fallback'
        out['uncertainty']={'kind':'mesh refinement differences retained per side','rigorous_bound':False}
        return out
    except Exception as exc:
        return result('evaluator_error',raw={'error':str(exc),'stage':'section_measurement'})

"""Planar sections: endpoint graph, exact-curve integration, explicit quality gate.

Only degree-two disjoint loops are measured. No greedy branch selection, healing,
fallback plane search or numerical score for a pathological reference section.
"""
import math
from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
from OCP.BRepAdaptor import BRepAdaptor_Curve,BRepAdaptor_Surface
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeWire,BRepBuilderAPI_MakeFace
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepClass import BRepClass_FaceClassifier
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.GeomAbs import GeomAbs_Plane
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_EDGE,TopAbs_FACE,TopAbs_VERTEX,TopAbs_FORWARD,TopAbs_IN,TopAbs_ON
from OCP.TopoDS import TopoDS
from OCP.gp import gp_Pnt,gp_Dir,gp_Pln

TOL_MM=1e-5


def xyz(p):return (p.X(),p.Y(),p.Z())


def section_metrics(shape,axis,offset):
    if axis not in ('x','y','z') or not math.isfinite(offset):
        raise ValueError('invalid section plane')
    idx=('x','y','z').index(axis)
    direction=[0.,0.,0.];direction[idx]=1.
    origin=[0.,0.,0.];origin[idx]=offset
    plane=gp_Pln(gp_Pnt(*origin),gp_Dir(*direction))
    quality={'measurement_valid':False,'boolean_done':False,'coplanar_faces':0,'edge_count':0,
             'vertex_count':0,'duplicate_edges':0,'branch_nodes':0,'open_nodes':0,'node_degrees':[],
             'endpoint_tolerance_mm':TOL_MM,'issues':[]}
    out={'area_mm2':None,'perimeter_mm':None,'contours':0,'open_chains':0,'quality':quality}
    faces=TopExp_Explorer(shape,TopAbs_FACE)
    while faces.More():
        surface=BRepAdaptor_Surface(TopoDS.Face_s(faces.Current()))
        if surface.GetType()==GeomAbs_Plane:
            pl=surface.Plane()
            n=xyz(pl.Axis().Direction())
            if abs(n[idx])>1-1e-10 and abs(xyz(pl.Location())[idx]-offset)<=TOL_MM:
                quality['coplanar_faces']+=1
        faces.Next()
    algo=BRepAlgoAPI_Section(shape,plane,False)
    algo.SetNonDestructive(True)
    algo.Build()
    if not algo.IsDone():
        quality['issues'].append('section_boolean_failed')
        return out
    quality['boolean_done']=True
    ve=TopExp_Explorer(algo.Shape(),TopAbs_VERTEX)
    while ve.More():quality['vertex_count']+=1;ve.Next()
    edges=[];ends=[];midpoints=[];samples=[];perimeter=0.
    ex=TopExp_Explorer(algo.Shape(),TopAbs_EDGE)
    while ex.More():
        edge=TopoDS.Edge_s(ex.Current().Oriented(TopAbs_FORWARD))
        curve=BRepAdaptor_Curve(edge)
        a,b=curve.FirstParameter(),curve.LastParameter()
        pts=[xyz(curve.Value(a+(b-a)*f)) for f in (0.,0.25,0.5,0.75,1.)]
        p=GProp_GProps();BRepGProp.LinearProperties_s(edge,p)
        if not math.isfinite(p.Mass()) or p.Mass()<=TOL_MM:
            quality['issues'].append('degenerate_edge')
        edges.append(edge);ends.extend((pts[0],pts[-1]));midpoints.append(pts[2]);samples.append(pts)
        perimeter+=abs(p.Mass());ex.Next()
    quality['edge_count']=len(edges)
    out['perimeter_mm']=perimeter
    if quality['coplanar_faces']:
        quality['issues'].append('coplanar_cut_ambiguous')
    if not edges:
        if quality['vertex_count']:
            quality['issues'].append('tangent_or_degenerate_intersection')
        out.update(area_mm2=0.,perimeter_mm=0.)
        quality['measurement_valid']=not quality['issues']
        quality['empty']=True
        return out
    quality['empty']=False
    # Explicitly identify duplicates, rather than counting doubled edges as a loop.
    for i,a in enumerate(samples):
        for b in samples[:i]:
            if min(max(math.dist(p,q) for p,q in zip(a,b)),max(math.dist(p,q) for p,q in zip(a,reversed(b))))<=TOL_MM:
                quality['duplicate_edges']+=1
    # Single-link endpoint clustering; reject transitive clusters whose diameter
    # exceeds the tolerance, so clustering cannot silently bridge a large gap.
    parents=list(range(len(ends)))
    def find(i):
        while parents[i]!=i:
            parents[i]=parents[parents[i]];i=parents[i]
        return i
    for i,p in enumerate(ends):
        for j,q in enumerate(ends[:i]):
            if math.dist(p,q)<=TOL_MM:parents[find(i)]=find(j)
    groups={}
    for i in range(len(ends)):groups.setdefault(find(i),[]).append(i)
    for members in groups.values():
        if any(math.dist(ends[i],ends[j])>TOL_MM for i in members for j in members):
            quality['issues'].append('ambiguous_endpoint_cluster')
    nodes={root:n for n,root in enumerate(groups)}
    pairs=[(nodes[find(2*i)],nodes[find(2*i+1)]) for i in range(len(edges))]
    adjacency={n:[] for n in nodes.values()}
    for i,(a,b) in enumerate(pairs):adjacency[a].append(i);adjacency[b].append(i)
    degrees=[len(v) for v in adjacency.values()]
    quality.update(node_degrees=degrees,branch_nodes=sum(d>2 for d in degrees),open_nodes=sum(d<2 for d in degrees))
    out['open_chains']=sum(d==1 for d in degrees)//2
    if quality['duplicate_edges']:quality['issues'].append('duplicate_edges')
    if any(d!=2 for d in degrees):quality['issues'].append('non_manifold_section_graph')
    if quality['issues']:return out
    unused=set(range(len(edges)));loops=[]
    while unused:
        first=min(unused);start=pairs[first][0];node=start;order=[]
        while True:
            options=[e for e in adjacency[node] if e in unused]
            if not options:
                quality['issues'].append('unclosed_loop');return out
            e=min(options);a,b=pairs[e];forward=(node==a)
            order.append((e,forward));unused.remove(e);node=b if forward else a
            if node==start:break
        wire=BRepBuilderAPI_MakeWire()
        for e,forward in order:
            wire.Add(edges[e] if forward else TopoDS.Edge_s(edges[e].Reversed()))
        if not wire.IsDone():
            quality['issues'].append('wire_construction_failed');return out
        builder=BRepBuilderAPI_MakeFace(plane,wire.Wire(),True)
        if not builder.IsDone() or not BRepCheck_Analyzer(builder.Face()).IsValid():
            quality['issues'].append('invalid_loop_face');return out
        face=builder.Face();p=GProp_GProps();BRepGProp.SurfaceProperties_s(face,p)
        area=abs(p.Mass())
        if not math.isfinite(area) or area<=TOL_MM*TOL_MM:
            quality['issues'].append('zero_or_invalid_area');return out
        loops.append((face,area,midpoints[order[0][0]]))
    total=0.
    for i,(face,area,point) in enumerate(loops):
        depth=0
        for j,(outer,outer_area,_) in enumerate(loops):
            if i==j or outer_area<=area:continue
            state=BRepClass_FaceClassifier(outer,gp_Pnt(*point),TOL_MM).State()
            if state==TopAbs_ON:
                quality['issues'].append('touching_loops');return out
            depth+=state==TopAbs_IN
        total+=area*((-1)**depth)
    if total<=TOL_MM*TOL_MM:
        quality['issues'].append('nonpositive_material_area');return out
    out.update(area_mm2=total,contours=len(loops))
    quality.update(measurement_valid=True,closed_loops=len(loops))
    return out


def evaluate_shapes(gt_shape,candidate_shape,annotations):
    sections=annotations.get('sections',[])
    if not sections or len({s['id'] for s in sections})!=len(sections):
        return {'status':'reference_invalid','score':None,'quality':{'measurement_valid':False},'raw':{'error':'empty or duplicate sections'}}
    rows=[];statuses=[]
    for sec in sections:
        if type(sec.get('expected_empty',False)) is not bool:
            return {'status':'reference_invalid','score':None,'quality':{'measurement_valid':False},
                    'raw':{'error':'expected_empty must be a JSON boolean','section_id':sec['id']}}
        gt=section_metrics(gt_shape,sec['axis'],sec['offset'])
        cand=section_metrics(candidate_shape,sec['axis'],sec['offset'])
        row={'id':sec['id'],'axis':sec['axis'],'actual_offset_mm':sec['offset'],'gt':gt,'candidate':cand,'section_score':None}
        empty=gt['quality'].get('empty',False)
        if not gt['quality']['measurement_valid'] or empty!=(sec.get('expected_empty',False) is True):
            status='reference_invalid'
        elif not cand['quality']['measurement_valid']:
            status='evaluator_error'
        else:
            status='ok'
            if empty:
                score=1. if cand['quality'].get('empty') else 0.
            else:
                a=abs(cand['area_mm2']-gt['area_mm2'])/gt['area_mm2']
                p=abs(cand['perimeter_mm']-gt['perimeter_mm'])/gt['perimeter_mm']
                score=math.exp(-(a+p)/0.05)*(1. if cand['contours']==gt['contours'] else 0.5)
                row.update(rel_area_error=a,rel_perimeter_error=p)
            row['section_score']=score
        row['status']=status;statuses.append(status);rows.append(row)
    status='reference_invalid' if 'reference_invalid' in statuses else ('evaluator_error' if 'evaluator_error' in statuses else 'ok')
    return {'status':status,'score':sum(r['section_score'] for r in rows)/len(rows) if status=='ok' else None,
            'quality':{'measurement_valid':status=='ok','reference_valid':not any(s=='reference_invalid' for s in statuses)},
            'raw':{'sections':rows,'scale':'exp(-(relA+relP)/0.05); contour mismatch x0.5',
                   'method':'degree-two endpoint graph; OCCT wire/face area and edge length; nesting by planar face classifier'}}


def evaluate(gt_path,candidate_path,annotations):
    from .metrics import read_step
    return evaluate_shapes(read_step(gt_path)[0],read_step(candidate_path)[0],annotations)

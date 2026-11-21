"""Original-instance assembly evidence. No per-part alignment or implicit fit rules."""
import math
import re
from pathlib import Path
import numpy as np
from scipy.optimize import linear_sum_assignment
from . import metrics
from . import robust_metrics as old
from OCP.BRepCheck import BRepCheck_Analyzer,BRepCheck_Shell,BRepCheck_NoError
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.TopAbs import TopAbs_SOLID,TopAbs_SHELL,TopAbs_FACE,TopAbs_EDGE,TopAbs_VERTEX
from OCP.TopExp import TopExp,TopExp_Explorer
from OCP.TopTools import TopTools_IndexedMapOfShape
from OCP.TopoDS import TopoDS

def _solids(shape):
    # Explorer retains repeated occurrences; indexed maps would collapse coincident references.
    out=[]; ex=TopExp_Explorer(shape,TopAbs_SOLID)
    while ex.More(): out.append(TopoDS.Solid_s(ex.Current()));ex.Next()
    return out

def basic_shape(shape, units, event=lambda *a,**kw:None):
    solids=_solids(shape); issues=[]; rows=[]
    if not solids: issues.append({'type':'no_solid'})
    for kind in (TopAbs_SHELL,TopAbs_FACE,TopAbs_EDGE,TopAbs_VERTEX):
        owned=TopTools_IndexedMapOfShape()
        for solid in solids: TopExp.MapShapes_s(solid,kind,owned)
        if any(not owned.Contains(x) for x in old._mapped(shape,kind)):
            issues.append({'type':'residual_geometry','entity_type':str(kind)})
    for i,solid in enumerate(solids):
        event('basic_solid',phase='start',entity_id=i)
        valid=bool(BRepCheck_Analyzer(solid).IsValid())
        shells=old._mapped(solid,TopAbs_SHELL)
        closed=bool(shells.Size()) and all(BRepCheck_Shell(TopoDS.Shell_s(s)).Closed()==BRepCheck_NoError for s in shells)
        p=GProp_GProps();BRepGProp.VolumeProperties_s(solid,p)
        volume=p.Mass(); bounds=old._bounds(solid)
        finite=math.isfinite(volume) and volume>0 and all(map(math.isfinite,bounds))
        row={'id':f'solid:{i}','brep_valid':valid,'closed':closed,'volume_mm3':volume,'bbox_mm':bounds}
        rows.append(row)
        if not valid or not closed or not finite: issues.append({'type':'invalid_solid',**row})
        event('basic_solid',phase='end',entity_id=i)
    return {'status':'fail' if issues else 'pass','raw':{'issues':issues,'solids':rows,'solid_count':len(solids),
            'declared_length_units':units,'working_unit':'mm','checked_count':len(rows),'total_count':len(solids)}}

def load_instances(path,event=lambda *a,**kw:None):
    """Traverse XCAF occurrences with composed locations; flat solids remain explicit."""
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString,TCollection_AsciiString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.TDF import TDF_LabelSequence,TDF_Label,TDF_Tool
    from OCP.TopLoc import TopLoc_Location
    from OCP.TDataStd import TDataStd_Name
    event('xcaf_import',phase='start',path=str(path))
    doc=TDocStd_Document(TCollection_ExtendedString('v5'))
    reader=STEPCAFControl_Reader()
    if int(reader.ReadFile(str(path)))!=1: raise ValueError('STEP XCAF import failed')
    reader.Reader().SetSystemLengthUnit(1.0)
    if not reader.Transfer(doc): raise ValueError('STEP XCAF transfer failed')
    tool=XCAFDoc_DocumentTool.ShapeTool_s(doc.Main()); roots=TDF_LabelSequence();tool.GetFreeShapes(roots)
    out=[]
    # XCAF may expand several representation modes of a single PRODUCT as
    # components. Source NAUO edges distinguish physical assembly children.
    source=Path(path).read_text(encoding='utf-8',errors='replace')
    entities={key:(kind,body) for key,kind,body in re.findall(r'#(\d+)\s*=\s*(\w+)\s*\((.*?)\);',source,re.S)}
    def step_string(value):
        value=re.sub(r'\\X2\\(.*?)\\X0\\',lambda m:bytes.fromhex(m[1]).decode('utf-16-be'),value)
        return value.replace("''", "'")
    products={}
    for key,(kind,body) in entities.items():
        if kind=='PRODUCT':
            strings=re.findall(r"'((?:''|[^'])*)'",body)
            if len(strings)>1:products[key]=step_string(strings[1])
    physical_parent_names=set()
    for kind,body in entities.values():
        if kind!='NEXT_ASSEMBLY_USAGE_OCCURRENCE':continue
        refs=re.findall(r'#(\d+)',body)
        if not refs:continue
        current=refs[0]
        for _ in range(4):
            if current in products:
                physical_parent_names.add(products[current]);break
            entity=entities.get(current)
            if not entity:break
            links=re.findall(r'#(\d+)',entity[1])
            if not links:break
            current=links[0]
    product_names=set(products.values())
    def label_name(label):
        name=TDataStd_Name()
        return name.Get().ToExtString() if label.FindAttribute(TDataStd_Name.GetID_s(),name) else ''
    def entry(label):
        value=TCollection_AsciiString();TDF_Tool.Entry_s(label,value);return value.ToCString()
    def walk(label,location,path_ids):
        local=tool.GetLocation_s(label); composed=location.Multiplied(local)
        definition=TDF_Label()
        referred=tool.GetReferredShape_s(label,definition)
        target=definition if referred else label
        ids=path_ids+[entry(label)]
        components=TDF_LabelSequence()
        alias_evidence=None
        if tool.IsAssembly_s(target) and tool.GetComponents_s(target,components,False):
            name=label_name(target)
            unique=old._mapped(tool.GetShape_s(target),TopAbs_SOLID)
            # Only collapse aliases inside a source PRODUCT with no physical
            # child relation, never different product occurrences at one pose.
            if components.Length()>1 and unique.Size()==1 and name in product_names and name not in physical_parent_names:
                alias_evidence={'reason':'one source PRODUCT, no outgoing NAUO, one identical BRep',
                                'representation_labels':[entry(components.Value(i)) for i in range(1,components.Length()+1)]}
                event('representation_alias',phase='resolved',product=name,path=ids,**alias_evidence)
            else:
                for i in range(1,components.Length()+1): walk(components.Value(i),composed,ids)
                return
        shape=tool.GetShape_s(target)
        # Definition's location is already included for a free simple root.
        shape=shape.Located(TopLoc_Location()).Moved(composed)
        solids=list(old._mapped(shape,TopAbs_SOLID)) if alias_evidence else _solids(shape)
        if not solids: return
        matrix=composed.Transformation()
        transform=[[matrix.Value(i,j) for j in (1,2,3,4)] for i in (1,2,3)]
        explicit_physical=bool(path_ids) and bool(physical_parent_names)
        groups=[solids] if explicit_physical and len(solids)>1 else [[s] for s in solids]
        for i,bodies in enumerate(groups):
            part_id='/'.join(ids)+(f'/part:{i}' if len(bodies)>1 else f'/solid:{i}')
            out.append({'id':part_id,'definition_id':entry(target),
                        'instance_path':ids,'product_name':label_name(target),'representation_alias':alias_evidence,
                        'inventory_source':'xcaf_physical_product' if explicit_physical else 'flat_solid_fallback',
                        'transform':transform,'shape':shape if len(bodies)>1 else bodies[0],
                        'constituents':[{'id':part_id+f'/body:{j}','shape':body} for j,body in enumerate(bodies)]})
    for i in range(1,roots.Length()+1): walk(roots.Value(i),TopLoc_Location(),[])
    if not out: raise ValueError('XCAF imported no solid instances')
    event('xcaf_import',phase='end',instance_count=len(out))
    return out

def descriptors(instances,rotation=None,translation=None,event=lambda *a,**kw:None):
    result=[]
    R=np.eye(3) if rotation is None else np.asarray(rotation)
    t=np.zeros(3) if translation is None else np.asarray(translation)
    for item in instances:
        event('instance_properties',phase='start',entity_id=item['id'])
        p=GProp_GProps();BRepGProp.VolumeProperties_s(item['shape'],p)
        a=GProp_GProps();BRepGProp.SurfaceProperties_s(item['shape'],a)
        m=p.MatrixOfInertia(); inertia=np.array([[m.Value(i,j) for j in (1,2,3)] for i in (1,2,3)])
        center=p.CentreOfMass();point=R@np.array([center.X(),center.Y(),center.Z()])+t
        result.append({k:v for k,v in item.items() if k not in ('shape','constituents')} |
            {'constituents':[{'id':body['id']} for body in item.get('constituents',[])],
             'volume_mm3':p.Mass(),'area_mm2':a.Mass(),
            'centroid_mm':point.tolist(),'principal_moments':np.linalg.eigvalsh(inertia).tolist(),
            'bbox_mm':old._bounds(item['shape'])})
        event('instance_properties',phase='end',entity_id=item['id'])
    return result

def match_parts(reference,candidate,descriptor_tolerance=.15):
    """Allow explicit unmatched dummies. Geometry descriptors select; position breaks ties only."""
    n,m=len(reference),len(candidate); cost=np.full((n+m,n+m),1e6)
    delta=np.full((n,m),float('inf'))
    def difference(a,b):
        vals=[]
        for k in ('volume_mm3','area_mm2'):
            vals.append(abs(a[k]-b[k])/max(abs(a[k]),abs(b[k]),1e-12))
        vals.extend(abs(x-y)/max(abs(x),abs(y),1e-12) for x,y in zip(a['principal_moments'],b['principal_moments']))
        return max(vals)
    for i,a in enumerate(reference):
        for j,b in enumerate(candidate):
            d=difference(a,b);delta[i,j]=d
            if d<=descriptor_tolerance:
                distance=math.dist(a['centroid_mm'],b['centroid_mm'])
                cost[i,j]=d+1e-6*distance/(1+distance)
    for i in range(n):cost[i,m+i]=1.
    for j in range(m):cost[n+j,j]=1.
    cost[n:,m:]=0
    ri,ci=linear_sum_assignment(cost)
    pairs=[(i,j) for i,j in zip(ri,ci) if i<n and j<m and cost[i,j]<1]
    matches=[{'reference_id':reference[i]['id'],'candidate_id':candidate[j]['id'],
              'descriptor_relative_error':float(delta[i,j]),
              'centroid_error_mm':math.dist(reference[i]['centroid_mm'],candidate[j]['centroid_mm'])} for i,j in pairs]
    duplicates=[]
    for j,b in enumerate(candidate):
        for k in range(j):
            if difference(b,candidate[k])<1e-8 and math.dist(b['centroid_mm'],candidate[k]['centroid_mm'])<1e-7:
                duplicates.append([candidate[k]['id'],b['id']])
    missing=[a['id'] for i,a in enumerate(reference) if i not in {x[0] for x in pairs}]
    extra=[b['id'] for j,b in enumerate(candidate) if j not in {x[1] for x in pairs}]
    return {'matches':matches,'missing':missing,'extra':extra,'duplicate_candidates':duplicates,
            'part_count_coverage':len(pairs)/n if n else 0.,'reference_count':n,'candidate_count':m,
            'descriptor_tolerance':descriptor_tolerance,'matching_method':'global descriptor assignment with unmatched dummies',
            'position_acceptance':'not_run: drawing-bound position/fit tolerances required',
            'single_solid_incompleteness':m==1 and n>1}

def solid_instances(instances):
    """Keep strict body identities separately from physical matching identities."""
    out=[]
    for part in instances:
        bodies=part.get('constituents') or [{'id':part['id'],'shape':part['shape']}]
        for body in bodies:
            out.append({'id':part['id'] if len(bodies)==1 else body['id'],'physical_id':part['id'],'shape':body['shape']})
    return out

def bbox_overlap_pairs(boxes):
    """Exact sweep-and-prune; strict positive overlap retains containment."""
    active=[];pairs=[]
    for i in sorted(range(len(boxes)),key=lambda k:(boxes[k][0],k)):
        box=boxes[i]
        active=[j for j in active if boxes[j][3]>box[0]]
        for j in active:
            if all(min(box[a+3],boxes[j][a+3])>max(box[a],boxes[j][a]) for a in (1,2)):
                pairs.append((max(i,j),min(i,j)))
        active.append(i)
    return sorted(pairs)

def inspect_interference(solids,event=lambda *a,**kw:None,progress=lambda x:None,ids=None,allowances=None,alignment=None,physical_ids=None):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    ids=ids or [f'solid:{i}' for i in range(len(solids))]
    physical_ids=physical_ids or ids
    boxes=[old._bounds(s) for s in solids];volumes=[]
    for s in solids:
        p=GProp_GProps();BRepGProp.VolumeProperties_s(s,p);volumes.append(abs(p.Mass()))
    total=len(solids)*(len(solids)-1)//2;issues=[]
    pairs=bbox_overlap_pairs(boxes);checked=total-len(pairs)
    allowance_pairs={tuple(sorted(r.get('candidate_ids',[]))) for r in (allowances or []) if len(r.get('candidate_ids',[]))==2}
    physical_pairs=sorted({tuple(sorted((physical_ids[i],physical_ids[j]))) for i,j in pairs}&allowance_pairs)
    intersections={}
    skipped=[{'reason':'disjoint_or_touching_exact_bbox_sweep','pair_count':checked}]
    raw={'issues':issues,'allowed_interferences':[],'checked_count':checked,'total_count':total,
         'broad_phase':'exact_bbox_sweep','skipped':skipped,'unchecked_pairs':[[ids[i],ids[j]] for i,j in pairs],
         'pending_interferences':[],'unchecked_physical_pairs':[list(p) for p in physical_pairs]}
    progress({'status':'partial','raw':raw})
    for pair_index,(i,j) in enumerate(pairs):
        pair=[ids[j],ids[i]]
        if any(min(boxes[i][a+3],boxes[j][a+3])<=max(boxes[i][a],boxes[j][a]) for a in range(3)):
            skipped.append({'pair':pair,'reason':'disjoint_or_touching_exact_bbox'})
        else:
            event('solid_common',phase='start',pair=pair)
            algo=BRepAlgoAPI_Common();algo.SetNonDestructive(True)
            from OCP.TopTools import TopTools_ListOfShape
            arg=TopTools_ListOfShape();arg.Append(solids[i]);tool=TopTools_ListOfShape();tool.Append(solids[j])
            algo.SetArguments(arg);algo.SetTools(tool);algo.Build()
            if not algo.IsDone(): raise RuntimeError('Common failed for '+str(pair))
            p=GProp_GProps();BRepGProp.VolumeProperties_s(algo.Shape(),p);volume=abs(p.Mass())
            if not math.isfinite(volume): raise RuntimeError('nonfinite intersection volume')
            tolerance=max(1e-7,1e-9*min(volumes[i],volumes[j]))
            if volume>tolerance:
                intersection=algo.Shape()
                if alignment is not None:
                    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
                    intersection=BRepBuilderAPI_Transform(intersection,alignment,True).Shape()
                issue={'type':'positive_volume_interference','entity_ids':[physical_ids[j],physical_ids[i]],'body_ids':pair,
                       'intersection_volume_mm3':volume,'tolerance_mm3':tolerance,'bbox_mm':old._bounds(intersection)}
                key=tuple(sorted(issue['entity_ids']))
                if key in allowance_pairs:
                    intersections.setdefault(key,[]).append((issue,intersection))
                    raw['pending_interferences'].append(issue)
                else:issues.append(issue)
            event('solid_common',phase='end',pair=pair,intersection_volume_mm3=volume)
        checked+=1;raw.update(checked_count=checked,unchecked_pairs=[[ids[a],ids[b]] for a,b in pairs[pair_index+1:]])
        progress({'status':'partial','raw':raw})
    # A physical-pair cap is consumed once, independently of the number of
    # constituent solids. Exact union avoids double-counting overlapping bodies.
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
    for key in physical_pairs:
        parts=intersections.get(key,[])
        event('physical_pair_allowance',phase='start',pair=list(key))
        if parts:
            intersection=parts[0][1]
            for _,other in parts[1:]:
                merged=BRepAlgoAPI_Fuse(intersection,other);merged.SetNonDestructive(True);merged.Build()
                if not merged.IsDone():raise RuntimeError('physical-pair intersection union failed: '+str(key))
                intersection=merged.Shape()
            props=GProp_GProps();BRepGProp.VolumeProperties_s(intersection,props);volume=abs(props.Mass())
            if not math.isfinite(volume):raise RuntimeError('nonfinite physical-pair union volume')
            issue={**parts[0][0],'entity_ids':list(key),'intersection_volume_mm3':volume,
                   'bbox_mm':old._bounds(intersection),'constituent_intersections':[x for x,_ in parts],
                   'volume_aggregation':'exact_union_of_constituent_intersections',
                   'physical_pair_volume_mm3':volume,'applicable_rule_caps':[
                       {'rule_id':r.get('id'),'maximum_volume_mm3':r.get('maximum_volume_mm3')}
                       for r in (allowances or []) if tuple(sorted(r.get('candidate_ids',[])))==key]}
            allowed=next((rule for rule in (allowances or []) if allowed_interference(issue,rule,intersection)),None)
            if allowed:raw['allowed_interferences'].append({**issue,'rule_id':allowed['id'],'maximum_volume_mm3':allowed['maximum_volume_mm3']})
            else:issues.append(issue)
        raw['pending_interferences']=[x for x in raw['pending_interferences'] if tuple(sorted(x['entity_ids']))!=key]
        raw['unchecked_physical_pairs'].remove(list(key))
        event('physical_pair_allowance',phase='end',pair=list(key))
        progress({'status':'partial','raw':raw})
    return {'status':'fail' if issues else 'pass','raw':raw}

def inspect_self(instances,event=lambda *a,**kw:None,progress=lambda x:None):
    instances=solid_instances(instances)
    raw={'issues':[],'checked_count':0,'total_count':len(instances),'unchecked_entities':[x['id'] for x in instances]}
    progress({'status':'partial','raw':raw})
    for item in instances:
        event('self_intersection',phase='start',entity_id=item['id'])
        findings=old._self_intersections(item['shape'])
        raw['issues'].extend({'entity_id':item['id'],'physical_id':item['physical_id'],**x} for x in findings)
        raw['checked_count']+=1;raw['unchecked_entities'].remove(item['id'])
        event('self_intersection',phase='end',entity_id=item['id'],findings=len(findings))
        progress({'status':'partial','raw':raw})
    return {'status':'fail' if raw['issues'] else 'pass','raw':raw}

def allowed_interference(issue,rule,intersection=None):
    """No type-wide exemption: one pair, finite volume cap and bounded reference region."""
    try:
        maximum=rule['maximum_volume_mm3'];bounds=issue['bbox_mm']
        if not (set(issue['entity_ids'])==set(rule['candidate_ids']) and len(set(rule['candidate_ids']))==2
                and math.isfinite(maximum) and maximum>=0 and issue['intersection_volume_mm3']<=maximum):return False
        if 'allowed_annulus' in rule:
            if intersection is None:return False
            from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
            from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
            from OCP.gp import gp_Ax2,gp_Pnt,gp_Dir
            region=rule['allowed_annulus'];origin=region['axis_origin_mm'];direction=region['axis_direction']
            length=region['length_mm'];inner=region['inner_radius_mm'];outer=region['outer_radius_mm']
            if (len(origin)!=3 or len(direction)!=3 or not all(math.isfinite(x) for x in [*origin,*direction,length,inner,outer])
                or length<=0 or not 0<=inner<outer or math.sqrt(sum(x*x for x in direction))<=0):return False
            axis=gp_Ax2(gp_Pnt(*origin),gp_Dir(*direction));allowed=BRepPrimAPI_MakeCylinder(axis,outer,length).Shape()
            if inner>0:
                cut=BRepAlgoAPI_Cut(allowed,BRepPrimAPI_MakeCylinder(axis,inner,length).Shape());cut.Build()
                if not cut.IsDone():raise RuntimeError('allowed annulus construction failed')
                allowed=cut.Shape()
            residual=BRepAlgoAPI_Cut(intersection,allowed);residual.Build()
            if not residual.IsDone():raise RuntimeError('allowed-region residual Boolean failed')
            props=GProp_GProps();BRepGProp.VolumeProperties_s(residual.Shape(),props)
            outside=abs(props.Mass());tolerance=max(1e-7,1e-9*issue['intersection_volume_mm3'])
            if not math.isfinite(outside):raise RuntimeError('nonfinite allowed-region residual')
            issue['allowed_region_test']={'rule_id':rule.get('id'),'outside_volume_mm3':outside,'tolerance_mm3':tolerance}
            return outside<=tolerance
        region=rule['allowed_bbox_mm']
        return (len(region)==6 and all(math.isfinite(v) for v in region)
                and all(region[a]<=bounds[a] and bounds[a+3]<=region[a+3] for a in range(3)))
    except (KeyError,TypeError,ValueError):return False

def evaluate_fit(instances,matching,rules,event=lambda *a,**kw:None,progress=lambda x:None):
    if not isinstance(rules,dict) or not rules:
        return {'status':'not_run','raw':{'reason':'frozen drawing-bound fit rules absent'}}
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    by_id={x['id']:x['shape'] for x in instances}
    mapping={x['reference_id']:x['candidate_id'] for x in matching.get('matches',[])}
    checks=[];errors=[]
    position_ids=['position:'+x['reference_id'] for x in matching.get('matches',[])] if 'position_tolerance_mm' in rules else []
    rule_ids=[r['id'] for r in rules.get('fits',[])]
    all_ids=position_ids+rule_ids
    if len(set(all_ids))!=len(all_ids):raise ValueError('duplicate fit/position rule IDs')
    def snapshot(status='partial'):
        done={x['id'] for x in checks}|{x['rule_id'] for x in errors}
        return {'status':status,'raw':{'checks':list(checks),'errors':list(errors),'checked_count':len(done),'total_count':len(all_ids),
                'unchecked_rules':[x for x in all_ids if x not in done],
                'rule_scope':'only explicit frozen rules; no inferred connections or tolerances'}}
    progress(snapshot())
    if 'position_tolerance_mm' in rules:
        tolerance=float(rules['position_tolerance_mm'])
        if not math.isfinite(tolerance) or tolerance<0:raise ValueError('invalid frozen position tolerance')
        for pair in matching.get('matches',[]):
            checks.append({'id':'position:'+pair['reference_id'],'type':'position','measured_mm':pair['centroid_error_mm'],
                           'tolerance_mm':tolerance,'status':'pass' if pair['centroid_error_mm']<=tolerance else 'fail'})
            progress(snapshot())
    for rule in rules.get('fits',[]):
        progress(snapshot())
        event('fit_rule',phase='start',rule_id=rule.get('id'))
        try:
            refs=rule['reference_ids']
            if len(refs)!=2 or refs[0]==refs[1]:raise ValueError('fit requires two distinct reference instances')
            if any(x not in mapping for x in refs):
                checks.append({'id':rule['id'],'status':'fail','reason':'required part unmatched'})
                progress(snapshot());event('fit_rule',phase='end',rule_id=rule.get('id'));continue
            a,b=[by_id[mapping[x]] for x in refs]
            kind=rule['type']
            if kind in ('surface_gap','expected_connection','forbidden_connection'):
                distance=BRepExtrema_DistShapeShape(a,b);distance.Perform()
                if not distance.IsDone():raise RuntimeError('exact gap measurement failed')
                value=distance.Value()
                if kind=='surface_gap':
                    low=float(rule['minimum_mm']);high=float(rule['maximum_mm'])
                    if not all(math.isfinite(x) for x in (low,high)) or low<0 or high<low:raise ValueError('invalid frozen gap limits')
                    passed=low<=value<=high
                else:
                    tolerance=float(rule['contact_tolerance_mm'])
                    if not math.isfinite(tolerance) or tolerance<0:raise ValueError('invalid frozen contact tolerance')
                    passed=value<=tolerance if kind=='expected_connection' else value>tolerance
                checks.append({'id':rule['id'],'type':kind,'status':'pass' if passed else 'fail','measured_gap_mm':value,'rule':rule})
            elif kind=='axis_alignment':
                axes=[_cylinder_axis(s,r,rule['selector_tolerance_mm']) for s,r in zip((a,b),rule['cylinder_radii_mm'])]
                (p,u),(q,v)=axes;u=np.asarray(u);v=np.asarray(v);delta=np.asarray(q)-p
                angle=math.degrees(math.acos(min(1.,abs(float(np.dot(u,v))))))
                cross=np.cross(u,v);norm=np.linalg.norm(cross)
                separation=abs(float(np.dot(delta,cross)))/norm if norm>1e-12 else float(np.linalg.norm(np.cross(delta,u)))
                limits=[float(rule['maximum_axis_distance_mm']),float(rule['maximum_angle_degrees'])]
                if not all(math.isfinite(x) and x>=0 for x in limits):raise ValueError('invalid frozen axis tolerances')
                checks.append({'id':rule['id'],'type':kind,'status':'pass' if separation<=limits[0] and angle<=limits[1] else 'fail',
                               'axis_distance_mm':separation,'unsigned_axis_angle_degrees':angle,'rule':rule})
            else:raise ValueError('unsupported fit type: '+str(kind))
        except Exception as exc:errors.append({'rule_id':rule.get('id'),'status':'error','error':str(exc)})
        event('fit_rule',phase='end',rule_id=rule.get('id'))
        progress(snapshot())
    status='partial' if errors and checks else 'error' if errors else 'fail' if any(x['status']=='fail' for x in checks) else 'pass' if checks else 'not_run'
    return snapshot(status)

def _cylinder_axis(shape,radius,tolerance):
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Cylinder
    if not math.isfinite(radius) or radius<=0 or not math.isfinite(tolerance) or tolerance<0:raise ValueError('invalid cylinder selector')
    candidates=[]
    for face in old._mapped(shape,TopAbs_FACE):
        surface=BRepAdaptor_Surface(TopoDS.Face_s(face))
        if surface.GetType()!=GeomAbs_Cylinder:continue
        cylinder=surface.Cylinder()
        if abs(cylinder.Radius()-radius)>tolerance:continue
        axis=cylinder.Axis();p=axis.Location();d=axis.Direction()
        candidates.append((np.array([p.X(),p.Y(),p.Z()]),np.array([d.X(),d.Y(),d.Z()])))
    if not candidates:raise ValueError('cylindrical feature not found')
    p,u=candidates[0]
    if any(np.linalg.norm(np.cross(q-p,u))>tolerance or abs(np.dot(u,v))<1-1e-10 for q,v in candidates[1:]):
        raise ValueError('ambiguous cylindrical feature selector')
    return p,u

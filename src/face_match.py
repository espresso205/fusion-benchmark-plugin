"""Unoriented support matching plus finite-face summaries, not patch coverage.

One-to-one F1 remains sensitive to face splitting. Area/centroid/bounds prevent
obvious same-support/wrong-trim matches but do not prove exact domain equivalence.
"""
import math
import numpy as np
from scipy.optimize import linear_sum_assignment
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_FACE
from OCP.TopoDS import TopoDS
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.GeomAbs import GeomAbs_Plane,GeomAbs_Cylinder,GeomAbs_Torus,GeomAbs_Cone
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib

GATES={'radius_mm':0.05,'major_mm':0.05,'minor_mm':0.05,'axis_angle_deg':0.5,
       'axis_line_mm':0.1,'plane_angle_deg':5.,'plane_offset_mm':0.05,'cone_angle_deg':0.5,
       'center_mm':0.1,'apex_mm':0.1,'area_relative':0.001,'centroid_mm':0.1,'bbox_mm':0.1}
TYPES=('plane','cylinder','torus','cone')


def xyz(p):return [p.X(),p.Y(),p.Z()]


def norm(v):
    a=np.asarray(v,dtype=float);length=np.linalg.norm(a)
    if not math.isfinite(length) or length==0:raise ValueError('invalid support direction')
    return a/length


def unsigned_angle(a,b):
    return math.degrees(math.acos(float(np.clip(abs(norm(a)@norm(b)),0.,1.))))


def axis_origin(p,d):
    p=np.asarray(p);d=norm(d)
    return p-d*(d@p)


def faces_of(shape):
    faces=[];ex=TopExp_Explorer(shape,TopAbs_FACE)
    while ex.More():
        face=TopoDS.Face_s(ex.Current());s=BRepAdaptor_Surface(face);typ=s.GetType()
        f={'type':'unsupported','surface_type':str(typ),'index':len(faces)}
        if typ==GeomAbs_Plane:
            pl=s.Plane();d=xyz(pl.Axis().Direction())
            f.update(type='plane',dir=d,offset=float(np.dot(d,xyz(pl.Location()))))
        elif typ==GeomAbs_Cylinder:
            cy=s.Cylinder();f.update(type='cylinder',radius=cy.Radius(),dir=xyz(cy.Axis().Direction()),loc=xyz(cy.Location()))
        elif typ==GeomAbs_Torus:
            to=s.Torus();f.update(type='torus',major=to.MajorRadius(),minor=to.MinorRadius(),dir=xyz(to.Axis().Direction()),loc=xyz(to.Location()))
        elif typ==GeomAbs_Cone:
            co=s.Cone();f.update(type='cone',angle=abs(math.degrees(co.SemiAngle())),dir=xyz(co.Axis().Direction()),apex=xyz(co.Apex()))
        p=GProp_GProps();BRepGProp.SurfaceProperties_s(face,p)
        box=Bnd_Box();BRepBndLib.AddOptimal_s(face,box)
        f.update(area_mm2=abs(p.Mass()),centroid=xyz(p.CentreOfMass()),bbox=list(box.Get()))
        if not np.isfinite([f['area_mm2'],*f['centroid'],*f['bbox']]).all() or f['area_mm2']<=0:
            raise ValueError('invalid finite face measurements')
        faces.append(f);ex.Next()
    return faces


def pair_errors(a,b,finite=True):
    typ=a['type'];err={}
    if typ!=b['type'] or typ not in TYPES:
        return {'errors':{},'failed':['surface_type'],'cost':None}
    if typ=='plane':
        da,db=norm(a['dir']),norm(b['dir'])
        sign=1. if da@db>=0 else -1.
        err.update(plane_angle_deg=unsigned_angle(da,db),plane_offset_mm=abs(a['offset']-sign*b['offset']))
    else:
        err['axis_angle_deg']=unsigned_angle(a['dir'],b['dir'])
        if typ=='cylinder':
            err.update(radius_mm=abs(a['radius']-b['radius']),
                       axis_line_mm=float(np.linalg.norm(axis_origin(a['loc'],a['dir'])-axis_origin(b['loc'],b['dir']))))
        elif typ=='torus':
            err.update(major_mm=abs(a['major']-b['major']),minor_mm=abs(a['minor']-b['minor']),center_mm=math.dist(a['loc'],b['loc']))
        elif typ=='cone':
            err.update(cone_angle_deg=abs(abs(a['angle'])-abs(b['angle'])),apex_mm=math.dist(a['apex'],b['apex']))
    if finite:
        err.update(area_relative=abs(a['area_mm2']-b['area_mm2'])/max(a['area_mm2'],b['area_mm2']),
                   centroid_mm=math.dist(a['centroid'],b['centroid']),
                   bbox_mm=max(abs(x-y) for x,y in zip(a['bbox'],b['bbox'])))
    failed=[k for k,v in err.items() if not math.isfinite(v) or v>GATES[k]]
    return {'errors':err,'failed':failed,'cost':None if failed else sum(v/GATES[k] for k,v in err.items())}


def match(a,b,finite):
    n,m=len(a),len(b)
    if not n or not m:
        return {'matched':0,'pairs':[],'unmatched_gt':[x['index'] for x in a],
                'unmatched_candidate':[x['index'] for x in b],'nearest_failures':[]}
    details=[[pair_errors(x,y,finite) for y in b] for x in a]
    penalty=(n+m+1)*100.
    cost=np.array([[d['cost'] if d['cost'] is not None else penalty for d in row] for row in details])
    ii,jj=linear_sum_assignment(cost)
    pairs=[(int(i),int(j)) for i,j in zip(ii,jj) if details[i][j]['cost'] is not None]
    gi={i for i,j in pairs};cj={j for i,j in pairs}
    nearest=[]
    for i in range(n):
        if i in gi:continue
        j=min(range(m),key=lambda j:(len(details[i][j]['failed']),sum(details[i][j]['errors'].values())))
        nearest.append({'gt_face':a[i]['index'],'candidate_face':b[j]['index'],**details[i][j]})
    return {'matched':len(pairs),'pairs':[{'gt_face':a[i]['index'],'candidate_face':b[j]['index'],**details[i][j]} for i,j in pairs],
            'unmatched_gt':[a[i]['index'] for i in range(n) if i not in gi],
            'unmatched_candidate':[b[j]['index'] for j in range(m) if j not in cj],'nearest_failures':nearest}


def evaluate_shapes(gt_shape,candidate_shape):
    gt,cand=faces_of(gt_shape),faces_of(candidate_shape)
    unsupported={}
    for role,faces in [('gt',gt),('candidate',cand)]:
        bad=[f for f in faces if f['type']=='unsupported']
        unsupported[role]={'count':len(bad),'area_mm2':sum(f['area_mm2'] for f in bad),
            'area_fraction':sum(f['area_mm2'] for f in bad)/sum(f['area_mm2'] for f in faces) if faces else None,
            'face_indices':[f['index'] for f in bad]}
    if not gt or not cand or any(x['count'] for x in unsupported.values()):
        return {'status':'unsupported','score':None,'quality':{'measurement_valid':False},'raw':{'unsupported':unsupported}}
    groups={};matched=0;support=0
    for typ in TYPES:
        a=[f for f in gt if f['type']==typ];b=[f for f in cand if f['type']==typ]
        detail=match(a,b,True);sm=match(a,b,False)['matched']
        detail.update(gt=len(a),candidate=len(b),support_only_matched=sm,
                      f1=2*detail['matched']/(len(a)+len(b)) if a or b else None)
        groups[typ]=detail;matched+=detail['matched'];support+=sm
    score=2*matched/(len(gt)+len(cand))
    return {'status':'ok','score':score,'quality':{'measurement_valid':True,'patch_coverage_validated':False},
        'raw':{'per_type':groups,'support_only_f1':2*support/(len(gt)+len(cand)),
               'overall':{'matched':matched,'FP':len(cand)-matched,'FN':len(gt)-matched,'f1':score},
               'gates':GATES,'unsupported':unsupported,
               'limitations':['one-to-one face matching remains sensitive to splitting/merging',
                              'finite-domain summaries are necessary gates, not exact patch overlap'],
               'gt_faces':gt,'candidate_faces':cand}}


def evaluate(gt_path,candidate_path,annotations=None):
    from .metrics import read_step
    return evaluate_shapes(read_step(gt_path)[0],read_step(candidate_path)[0])

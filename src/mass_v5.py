"""V5 additive physical-instance mass. Legacy whole-shape mass remains untouched."""
import math
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from . import robust_metrics as legacy
from .assembly_v5 import load_instances

def measure_instances(instances,event=lambda *a,**kw:None):
    previous=None; attempts=[];last=None
    # All rounds remain adaptive; require volume, centroid and inertia convergence.
    for eps in (1e-6,1e-8,1e-10,1e-12,1e-14):
        total=GProp_GProps();per_part=[]
        for item in instances:
            event('mass_instance_gauss',phase='start',entity_id=item['id'],epsilon=eps)
            props=GProp_GProps()
            estimate=BRepGProp.VolumeProperties_s(item['shape'],props,eps,True,False)
            if not math.isfinite(estimate) or estimate<0:raise ValueError('nonfinite/failed adaptive integration')
            legacy._properties(props,'OCCT per-instance adaptive Gauss')
            total.Add(props)
            per_part.append({'id':item['id'],'volume_mm3':props.Mass(),'integration_error_estimate':estimate})
            event('mass_instance_gauss',phase='end',entity_id=item['id'],epsilon=eps)
        last=legacy._properties(total,'OCCT additive physical-instance adaptive Gauss')
        if previous is not None:
            spread=legacy._spread(previous,last)
            bounds=[legacy._bounds(x['shape']) for x in instances]
            diagonal=math.sqrt(sum((max(b[a+3] for b in bounds)-min(b[a] for b in bounds))**2 for a in range(3)))
            converged=legacy._converged(spread,diagonal)
            attempts.append({'epsilon':eps,'spread':spread,'instances':per_part})
            if converged:return {**last,'converged':True,'spread':spread,'attempts':attempts,'physical_instance_count':len(instances)}
        else:attempts.append({'epsilon':eps,'instances':per_part})
        previous=last
    return {**last,'converged':False,'spread':spread,'attempts':attempts,'physical_instance_count':len(instances)}

def mass(gt,candidate,event=lambda *a,**kw:None):
    values={}
    for role,path in (('gt',gt),('candidate',candidate)):
        event('mass_role',phase='start',role=role)
        values[role]=measure_instances(load_instances(path,event),event)
        event('mass_role',phase='end',role=role)
    g,c=values['gt'],values['candidate']
    rv=abs(c['volume_mm3']-g['volume_mm3'])/g['volume_mm3']
    ri=max(abs(x-y)/y for x,y in zip(c['principal_moments'],g['principal_moments']))
    score=math.exp(-(rv+ri)/.1)
    valid=g['converged'] and c['converged']
    return {'status':'pass' if valid else 'error','measured_score':score if valid else None,
            'raw':{**values,'volume_relative_error':rv,'principal_moment_relative_error':ri,
                   'score_estimate_diagnostic_only':None if valid else score,'relative_convergence_budget':1e-6,
                   'error':None if valid else 'per-instance refinement failed frozen convergence budget'},
            'quality':{'scored_approximate':True,'convergence_validated':valid},
            'backend':'v5 additive physical-instance Gauss; no whole-shape GK',
            'uncertainty':{'kind':'empirical_refinement_difference','rigorous_bound':False}}

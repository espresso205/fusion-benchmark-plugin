"""One killable v5 stage. Original v3/v4 algorithms are imported without edits."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback
BASE=Path(__file__).resolve().parent
sys.path.insert(0,str(BASE))
from src.io_utils import sha,strict_json,utc_now
from src.contract import atomic_write_json
from src.contract_v5 import PROTOCOL_ID,SCHEMA_VERSION

def replace_json(path,data):
    fd,temp=tempfile.mkstemp(prefix='.pending-',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(data,f,ensure_ascii=False,allow_nan=False);f.flush();os.fsync(f.fileno())
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)

def failure_result(exc,progress_path):
    raw=strict_json(progress_path).get('raw',{}) if progress_path.exists() else {}
    return {'status':'error','measured_score':None,
            'raw':{**raw,'error':str(exc),'traceback':traceback.format_exc()}}

def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--case-dir',required=True,type=Path)
    ap.add_argument('--stage',required=True);args=ap.parse_args();folder=args.case_dir;stage=args.stage
    job=strict_json(folder/'job.json');start=time.perf_counter()
    paths={n:folder/'inputs'/('annotations.json' if n=='annotations' else n+'.step') for n in ('gt','candidate','annotations')}
    originals={n:sha(p) for n,p in paths.items()}
    evaluated=paths['candidate']
    def event(name,**details):
        with (folder/'events'/f'{stage}.jsonl').open('a',encoding='utf-8') as stream:
            stream.write(json.dumps({'name':name,'elapsed_s':time.perf_counter()-start,'at':utc_now(),**details})+'\n')
            stream.flush();os.fsync(stream.fileno())
    def progress(data):replace_json(folder/'progress'/f'{stage}.json',data)
    def adapt(data):
        status='pass' if data.get('status')=='ok' and data.get('quality',{}).get('measurement_valid') is True else 'error'
        return {**data,'legacy_status':data.get('status'),'status':status,
                'measured_score':data.get('score') if status=='pass' else None}
    try:
        if originals!=job['input_hashes']:raise ValueError('staged input hash mismatch')
        ann=strict_json(paths['annotations'])
        if ann.get('gt_sha256')!=originals['gt']:raise ValueError('annotation GT hash mismatch')
        event('imports',phase='start')
        from src import metrics,robust_metrics as robust,geometry_v4 as geometry,assembly_v5 as assembly
        event('imports',phase='end')
        # Instrument existing calls, preserving their order, arguments and return values.
        def traced(label,fn):
            def call(*a,**kw):
                event(label,phase='start',arguments=[str(x)[:200] for x in a if isinstance(x,(str,Path,float,int))])
                t=time.perf_counter()
                try:return fn(*a,**kw)
                finally:event(label,phase='end',duration_s=time.perf_counter()-t)
            return call
        robust.mass_properties=traced('mass_adaptive_gauss',robust.mass_properties)
        robust._mesh=traced('mesh_refinement',robust._mesh)
        robust._loaded=traced('step_import',robust._loaded)
        original_gprop=robust.BRepGProp
        class GPropProxy:
            VolumePropertiesGK_s=staticmethod(traced('mass_gauss_kronrod',original_gprop.VolumePropertiesGK_s))
            VolumeProperties_s=staticmethod(traced('mass_volume_properties',original_gprop.VolumeProperties_s))
        robust.BRepGProp=GPropProxy
        if stage.endswith('_basic'):
            role='candidate' if stage=='candidate_basic' else 'gt'
            event('step_import',phase='start',role=role)
            try:shape,units=metrics.read_step(paths[role])
            except ValueError as exc:
                data={'status':'fail','raw':{'issues':[{'type':'invalid_step','error':str(exc)}]}}
            else:
                event('step_import',phase='end',role=role);data=assembly.basic_shape(shape,units,event)
        elif stage=='pose':
            chosen,legacy=geometry.prepare_pose(paths['gt'],paths['candidate'],folder/'derived',job['coordinate_mode'])
            data=adapt(legacy)
        elif stage in ('shape','dimension','mass'):
            if stage in ('shape','dimension') and job['coordinate_mode']=='geometry':
                evaluated=folder/'derived/candidate_aligned.step'
                pose=strict_json(folder/'results/pose.json')
                if sha(evaluated)!=pose['raw']['evaluation_candidate_sha256']:raise ValueError('derived hash mismatch')
            event(stage,phase='start')
            if stage=='shape':legacy=geometry.shape(paths['gt'],evaluated,folder/'diagnostics')
            elif stage=='dimension':legacy=geometry.dimension(paths['gt'],evaluated,ann)
            else:
                from src.mass_v5 import mass
                data=mass(paths['gt'],paths['candidate'],event)
            if stage!='mass':data=adapt(legacy)
            event(stage,phase='end')
        elif stage in ('candidate_self_intersection','candidate_interference'):
            instances=assembly.load_instances(paths['candidate'],event)
            bodies=assembly.solid_instances(instances)
            allowances=[];alignment=None
            match_path=folder/'results/part_matching.json'
            if stage=='candidate_interference' and match_path.exists():
                matched=strict_json(match_path).get('raw',{})
                mapping={x['reference_id']:x['candidate_id'] for x in matched.get('matches',[])}
                for rule in ann.get('assembly_v5',{}).get('allowed_interferences',[]):
                    if all(x in mapping for x in rule.get('reference_ids',[])) and len(rule.get('reference_ids',[]))==2:
                        allowances.append({**rule,'candidate_ids':[mapping[x] for x in rule['reference_ids']]})
                if allowances and job['coordinate_mode']=='geometry':
                    pose=strict_json(folder/'results/pose.json')
                    if pose['status']=='pass':
                        from OCP.gp import gp_Trsf
                        R=pose['raw']['R'];t=pose['raw']['t'];alignment=gp_Trsf()
                        alignment.SetValues(*[float(v) for i in range(3) for v in [*R[i],t[i]]])
                    else:allowances=[]
            data=(assembly.inspect_self(instances,event,progress) if stage=='candidate_self_intersection' else
                  assembly.inspect_interference([x['shape'] for x in bodies],event,progress,[x['id'] for x in bodies],
                                                allowances,alignment,[x['physical_id'] for x in bodies]))
        elif stage=='part_matching':
            rotation=translation=None
            if job['coordinate_mode']=='geometry':
                pose=strict_json(folder/'results/pose.json');rotation=pose['raw']['R'];translation=pose['raw']['t']
                evaluated=folder/'derived/candidate_aligned.step'
                if sha(evaluated)!=pose['raw']['evaluation_candidate_sha256']:raise ValueError('derived hash mismatch')
            gt=assembly.descriptors(assembly.load_instances(paths['gt'],event),event=event)
            candidate=assembly.descriptors(assembly.load_instances(paths['candidate'],event),rotation,translation,event)
            matched=assembly.match_parts(gt,candidate)
            data={'status':'fail' if matched['missing'] or matched['extra'] or matched['duplicate_candidates'] else 'pass',
                  'raw':{**matched,'reference_instances':gt,'candidate_instances':candidate}}
        elif stage=='fit':
            rules=ann.get('assembly_v5')
            if rules:
                matched=strict_json(folder/'results/part_matching.json').get('raw',{})
                data=assembly.evaluate_fit(assembly.load_instances(paths['candidate'],event),matched,rules,event,progress)
                if job['coordinate_mode']=='geometry':evaluated=folder/'derived/candidate_aligned.step'
            else:data={'status':'not_run','raw':{'reason':'frozen drawing-bound fit rules absent'}}
        else:raise ValueError('unknown stage')
    except Exception as exc:
        data=failure_result(exc,folder/'progress'/f'{stage}.json')
        if 'hash mismatch' in str(exc):data['raw']['integrity_error']=True
    data.update(schema_version=SCHEMA_VERSION,protocol_id=PROTOCOL_ID,case=job['case'],stage=stage,
                coordinate_mode=job['coordinate_mode'],input_hashes=originals,run_manifest_sha256=job['run_manifest_sha256'],
                evaluation_input_hashes={**originals,'candidate':sha(evaluated)} if evaluated.exists() else None,
                timing={'wall_s':time.perf_counter()-start})
    if {n:sha(p) for n,p in paths.items()}!=originals:
        data.update(status='error',measured_score=None);data['raw']['integrity_error']=True
    atomic_write_json(folder/'results'/f'{stage}.json',data)
    print(stage,data['status'],data.get('measured_score'),flush=True)

if __name__=='__main__':main()

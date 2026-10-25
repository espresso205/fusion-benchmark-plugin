"""V4 isolated worker: admit original geometry, score core, then diagnostics."""
import argparse
import os
from pathlib import Path
import time
import traceback

from src.io_utils import sha, strict_json, utc_now
from src.contract import atomic_write_json
from src.contract_v4 import PROTOCOL_ID, SCHEMA_VERSION


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--case-dir',type=Path,required=True)
    folder=ap.parse_args().case_dir.resolve()
    job=strict_json(folder/'job.json')
    os.environ.pop('CADGENBENCH_MESH_CACHE_DIR',None)
    from src import metrics, robust_metrics as robust, geometry_v4 as geometry
    start=time.perf_counter()
    paths={n:folder/'inputs'/('annotations.json' if n=='annotations' else n+'.step') for n in ('gt','candidate','annotations')}
    originals={n:sha(p) for n,p in paths.items()}
    if originals!=job['input_hashes']: raise ValueError('staged input hash mismatch')
    ann=strict_json(paths['annotations'])
    if ann.get('gt_sha256')!=originals['gt']: raise ValueError('annotation GT hash mismatch')
    mode=job['coordinate_mode']
    if mode not in ('geometry','strict') or job['protocol_id']!=PROTOCOL_ID: raise ValueError('invalid v4 job')
    gt,candidate=paths['gt'],paths['candidate']
    artifacts={'gt':metrics.StepArtifacts(gt),'candidate':metrics.StepArtifacts(candidate)}

    def timed(name,fn):
        atomic_write_json(folder/'events'/f'{name}_started.json',{'stage':name,'started_at':utc_now()})
        t=time.perf_counter()
        try: data=fn()
        except Exception as exc:
            data=metrics.result('evaluator_error',raw={'error':str(exc),'traceback':traceback.format_exc()})
        data['timing']={'compute_s':time.perf_counter()-t}
        return data

    def persist(name,data,evaluated=candidate):
        data.update(schema_version=SCHEMA_VERSION,protocol_id=PROTOCOL_ID,case=job['case'],angle=name,
            coordinate_mode=mode,input_hashes=originals,run_manifest_sha256=job['run_manifest_sha256'],
            evaluation_input_hashes={**originals,'candidate':sha(evaluated)} if evaluated else None)
        atomic_write_json(folder/'results'/f'{name}.json',data)
        print(job['case'],name,data['status'],data.get('score'),flush=True)

    validity=timed('validity',lambda:robust.validity(gt,candidate,artifacts))
    persist('validity',validity)
    admitted=(validity.get('quality',{}).get('candidate_valid') is True
              and validity.get('raw',{}).get('candidate',{}).get('healthy') is True)
    if admitted:
        persist('mass',timed('mass',lambda:robust.mass(gt,candidate)))
        pose_path=candidate if mode=='strict' else None
        def prepare():
            nonlocal pose_path
            pose_path,data=geometry.prepare_pose(gt,candidate,folder/'derived',mode)
            return data
        if mode=='geometry':
            persist('pose',timed('pose',prepare))
        if pose_path is not None:
            evaluated_hash=sha(pose_path)
            for name,fn in (
                ('shape',lambda:geometry.shape(gt,pose_path,folder/'diagnostics')),
                ('dimension',lambda:geometry.dimension(gt,pose_path,ann))):
                persist(name,timed(name,fn),pose_path)
        else:
            evaluated_hash=None
            for name in ('shape','dimension'):
                persist(name,metrics.result('evaluator_error',raw={'error':'rigid alignment unavailable; no fixed-frame fallback'}),None)
        preflight={'case':job['case'],'input_hashes':originals,'coordinate_mode':mode,
            'run_manifest_sha256':job['run_manifest_sha256'],'qualification_policy':'diagnostics do not determine scored coverage'}
        try:
            plan=robust.build_section_plan(gt,ann)
            atomic_write_json(folder/'section_plan.json',plan)
            ann={**ann,'v3_section_plan':plan}
            preflight['section_plan_sha256']=sha(folder/'section_plan.json')
        except Exception as exc: preflight['section_plan_error']=str(exc)
        atomic_write_json(folder/'reference_preflight.json',preflight)
        if mode=='geometry':
            persist('world_shape',timed('world_shape',lambda:geometry.shape(gt,candidate,folder/'diagnostics')))
        else:
            persist('pose',timed('pose',prepare))
            pose_path=candidate
        if pose_path is not None:
            for name,fn in (
                ('distance',lambda:geometry.distance(gt,pose_path)),
                ('face_match',lambda:robust.face_match(gt,pose_path,ann)),
                ('section',lambda:robust.section(gt,pose_path,ann))):
                persist(name,timed(name,fn),pose_path)
            if sha(pose_path)!=evaluated_hash: raise ValueError('evaluated geometry changed during measurement')
    after={n:sha(p) for n,p in paths.items()}
    atomic_write_json(folder/'worker_complete.json',{'status':'ok' if after==originals else 'evaluator_error',
        'input_hashes_after':after,'wall_s':time.perf_counter()-start,'ended_at':utc_now(),
        'cache_policy':'per-case process-local; derived geometry bound to original hashes'})


if __name__=='__main__': main()

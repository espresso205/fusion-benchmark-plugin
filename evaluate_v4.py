"""Versioned geometry scoring with separate coordinate and measurement diagnostics."""
import argparse
from concurrent.futures import ThreadPoolExecutor,as_completed
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from src.io_utils import sha,strict_json,utc_now
from src.contract import atomic_write_json
from src.contract_v4 import (PROTOCOL_ID,SCHEMA_VERSION,DEFAULT_WEIGHTS,SCORED_METRICS,
    DIAGNOSTIC_METRICS,MODULE_NAMES,aggregate_case,refresh_assessment,valid_weights)

BASE=Path(__file__).resolve().parent


def run_case(row,output,manifest_hash,weights,timeout,coordinate_mode):
    from evaluate import terminate_tree
    folder=output/row['case']; folder.mkdir()
    for name in ('inputs','results','events','diagnostics','derived'): (folder/name).mkdir()
    for role in ('gt','candidate','annotations'):
        dest=folder/'inputs'/('annotations.json' if role=='annotations' else role+'.step')
        shutil.copyfile(row[role],dest)
        if sha(dest)!=row['expected_hashes'][role]: raise ValueError('input changed during staging')
    job={'case':row['case'],'input_hashes':row['expected_hashes'],'run_manifest_sha256':manifest_hash,
         'protocol_id':PROTOCOL_ID,'coordinate_mode':coordinate_mode}
    atomic_write_json(folder/'job.json',job)
    start=time.perf_counter()
    options={'start_new_session':True} if os.name!='nt' else {'creationflags':subprocess.CREATE_NO_WINDOW}
    with (folder/'worker.log').open('w',encoding='utf-8') as log:
        proc=subprocess.Popen([sys.executable,'-B',str(BASE/'worker_v4.py'),'--case-dir',str(folder)],
                              stdout=log,stderr=subprocess.STDOUT,cwd=BASE,**options)
        try:
            code=proc.wait(timeout=timeout); execution={'returncode':code,'timed_out':False}
        except subprocess.TimeoutExpired:
            terminate_tree(proc); execution={'returncode':proc.returncode,'timed_out':True}
    complete_path=folder/'worker_complete.json'
    execution.update(wall_s=time.perf_counter()-start,status='timed_out' if execution['timed_out'] else
                     'completed' if execution['returncode']==0 and complete_path.exists() else 'failed')
    modules,errors,diagnostic_read_errors={},[],[]
    for name in MODULE_NAMES:
        path=folder/'results'/f'{name}.json'
        if path.exists():
            try:
                data=strict_json(path)
                if not isinstance(data,dict): raise ValueError('result JSON must be an object')
            except Exception as exc:
                detail=f'{name}: parse/read error: {exc}'
                if name in (*DIAGNOSTIC_METRICS,'world_shape'):
                    diagnostic_read_errors.append((name,detail))
                else:
                    errors.append(detail)
                continue
            if data.get('run_manifest_sha256')!=manifest_hash:
                errors.append(f'{name}: stale run manifest hash')
            else:
                modules[name]=data
    aggregate=aggregate_case(row['case'],modules,row['expected_hashes'],weights,coordinate_mode=coordinate_mode)
    if diagnostic_read_errors:
        aggregate['diagnostic_read_errors']=[{'module':name,'detail':detail}
                                             for name,detail in diagnostic_read_errors]
        aggregate['reasons'].extend({'status':'diagnostic_unavailable','module':name,'detail':detail}
                                    for name,detail in diagnostic_read_errors)
        for name,detail in diagnostic_read_errors:
            if name=='world_shape':
                aggregate['coordinate_diagnostics']={'status':'diagnostic_unreliable','raw':{'error':detail}}
    try:
        for role in ('gt','candidate','annotations'):
            if sha(folder/'inputs'/('annotations.json' if role=='annotations' else role+'.step'))!=row['expected_hashes'][role]:
                raise ValueError('staged input changed')
        pose=modules.get('pose',{})
        raw=pose.get('raw',{})
        # Derived files are local outputs only. The manifest is the trust root;
        # filenames from module JSON never authorize reading arbitrary paths.
        if pose.get('status')=='ok':
            aligned=folder/'derived'/'candidate_aligned.step'
            if sha(aligned)!=raw.get('aligned_candidate_sha256'): raise ValueError('derived aligned geometry hash mismatch')
            expected_eval=sha(aligned) if coordinate_mode=='geometry' else row['expected_hashes']['candidate']
            if raw.get('evaluation_candidate_sha256')!=expected_eval: raise ValueError('pose evaluation identity mismatch')
        if complete_path.exists():
            completion=strict_json(complete_path)
            if completion.get('status')!='ok' or completion.get('input_hashes_after')!=row['expected_hashes']:
                raise ValueError('worker completion integrity failed')
    except Exception as exc: errors.append(str(exc))
    if errors:
        aggregate.update(status='integrity_error',final_score=None)
        aggregate['reasons'].extend({'status':'integrity_error','detail':s} for s in errors)
    if execution['status']!='completed':
        aggregate['reasons'].append({'status':'evaluator_error','detail':'worker timeout/crash/incomplete',**execution})
        if not aggregate.get('participates_in_scoring') and not errors:
            aggregate.update(status='evaluator_error',final_score=None)
    aggregate.update(run_manifest_sha256=manifest_hash,annotation_kind='gt_derived_probe',ranking_eligible=False)
    refresh_assessment(aggregate,execution)
    atomic_write_json(folder/'provisional.json',{**aggregate,'lifecycle':'provisional','requires_batch_integrity_confirmation':True})
    print(row['case'],aggregate['status'],aggregate['final_score'],f"{execution['wall_s']:.2f}s",flush=True)
    return aggregate


def main(argv=None):
    from evaluate import validate_cases,code_inventory,dependency_versions
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--cases',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--weights',type=Path,default=BASE/'weights_v4.json')
    ap.add_argument('--protocol',choices=[PROTOCOL_ID],default=PROTOCOL_ID)
    ap.add_argument('--coordinate-mode',choices=['geometry','strict'],default='geometry')
    ap.add_argument('--timeout',type=float,default=600)
    ap.add_argument('--workers',type=int,choices=range(1,5),default=1)
    args=ap.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout<=0: ap.error('timeout must be positive and finite')
    if args.output.exists(): ap.error('output already exists; refusing overwrite')
    document=strict_json(args.cases); rows=validate_cases(document)
    weights=strict_json(args.weights)
    if weights.get('protocol_id',PROTOCOL_ID)!=PROTOCOL_ID or not valid_weights(weights.get('weights',weights)):
        ap.error('invalid v4 weights or protocol')
    output=args.output.resolve(); output.mkdir(parents=True,exist_ok=False)
    started=time.perf_counter(); code=code_inventory()
    manifest={'schema_version':SCHEMA_VERSION,'protocol_id':PROTOCOL_ID,'started_at':utc_now(),'cases':rows,
        'cases_sha256':sha(args.cases),'weights':weights,'weights_sha256':sha(args.weights),'code_hashes':code,
        'dependencies':dependency_versions(),'annotation_kind':'gt_derived_probe','coordinate_mode':args.coordinate_mode,
        'coordinate_frame':'rigid_aligned_mm' if args.coordinate_mode=='geometry' else 'fixed_world_mm',
        'scored_metrics':list(SCORED_METRICS),'diagnostic_metrics':list(DIAGNOSTIC_METRICS),
        'timeout_per_case_s':args.timeout,'workers':args.workers,'ranking_eligible':False,
        'ranking_reason':'adapted probes and uncalibrated weights; coordinate diagnostics are not engineering datum certification'}
    atomic_write_json(output/'manifest.json',manifest); digest=sha(output/'manifest.json')
    results=[]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs={pool.submit(run_case,row,output,digest,weights,args.timeout,args.coordinate_mode):row for row in rows}
        for future in as_completed(jobs):
            row=jobs[future]
            try: results.append(future.result())
            except Exception as exc:
                failed=aggregate_case(row['case'],{},row['expected_hashes'],weights,coordinate_mode=args.coordinate_mode)
                failed.update(status='evaluator_error',run_manifest_sha256=digest,annotation_kind='gt_derived_probe')
                failed['reasons'].append({'status':'evaluator_error','detail':str(exc)})
                refresh_assessment(failed,{'status':'failed','returncode':None,'timed_out':False})
                results.append(failed)
    results.sort(key=lambda row:row['case'])
    try:
        unchanged=code==code_inventory() and sha(args.weights)==manifest['weights_sha256']
        inputs_unchanged=sha(args.cases)==manifest['cases_sha256'] and all(sha(row[k])==row['expected_hashes'][k] for row in rows for k in ('gt','candidate','annotations'))
    except (OSError,ValueError): unchanged=inputs_unchanged=False
    if not unchanged or not inputs_unchanged:
        for result in results:
            result.update(status='integrity_error',final_score=None)
            result['reasons'].append({'status':'integrity_error','detail':'code, weights, manifest or original inputs changed'})
            refresh_assessment(result)
    for result in results:
        atomic_write_json(output/result['case']/'aggregate.json',result)
    participants=[r for r in results if r.get('participates_in_scoring') and r['final_score'] is not None]
    summary={'schema_version':SCHEMA_VERSION,'protocol_id':PROTOCOL_ID,'coordinate_mode':args.coordinate_mode,
        'scored_metrics':list(SCORED_METRICS),'diagnostic_metrics':list(DIAGNOSTIC_METRICS),'run_manifest_sha256':digest,
        'cases':results,'code_unchanged':unchanged,'original_inputs_unchanged':inputs_unchanged,
        'all_cases_aggregatable':all(r['final_score'] is not None for r in results),'ranking_eligible':False,
        'batch_mean':math.fsum(r['final_score'] for r in participants)/len(participants) if participants else None,
        'participating_case_count':len(participants),'total_case_count':len(results),
        'all_validation_passed_cases_scored':all(not r.get('validation_passed') or r.get('participates_in_scoring') for r in results),
        'batch_mean_scope':'all validation-passed candidates, including core policy lower-bound awards',
        'wall_s':time.perf_counter()-started,'ended_at':utc_now()}
    atomic_write_json(output/'summary.json',summary)
    from src.reporting_v4 import report
    report(output,manifest,summary)
    print('Output:',output,'wall_s:',summary['wall_s'],flush=True)
    return 0 if participants and unchanged and inputs_unchanged and all(r['status'] in ('ok','scored_with_unavailable_metrics','candidate_invalid')
                and r.get('execution',{}).get('status')=='completed' for r in results) else 2


if __name__=='__main__': raise SystemExit(main())

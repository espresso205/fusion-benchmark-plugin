"""V5 isolated-stage evaluator; reference acceptance is required for formal totals."""
import argparse
from concurrent.futures import ThreadPoolExecutor,as_completed
import csv
import html
import math
from pathlib import Path
import shutil
import sys
import time
BASE=Path(__file__).resolve().parent
sys.path.insert(0,str(BASE))
from src.io_utils import sha,strict_json,utc_now
from src.contract import atomic_write_json
from src.contract_v5 import PROTOCOL_ID,SCHEMA_VERSION,STAGES,DEFAULT_WEIGHTS,aggregate_case,reference_acceptance
from src.stages_v5 import run_isolated

def run_case(row,output,manifest_hash,timeout,stage_timeout,mode):
    folder=output/row['case'];folder.mkdir()
    for name in ('inputs','results','events','diagnostics','derived','progress','logs'):(folder/name).mkdir()
    for role in ('gt','candidate','annotations'):
        dest=folder/'inputs'/('annotations.json' if role=='annotations' else role+'.step')
        shutil.copyfile(row[role],dest)
        if sha(dest)!=row['expected_hashes'][role]:raise ValueError('input changed during staging')
    job={'case':row['case'],'input_hashes':row['expected_hashes'],'coordinate_mode':mode,
         'protocol_id':PROTOCOL_ID,'run_manifest_sha256':manifest_hash}
    atomic_write_json(folder/'job.json',job);modules={};start=time.perf_counter();integrity=[]
    def envelope(name,data):
        return {**data,'schema_version':SCHEMA_VERSION,'protocol_id':PROTOCOL_ID,'case':row['case'],'stage':name,
                'coordinate_mode':mode,'input_hashes':row['expected_hashes'],'evaluation_input_hashes':row['expected_hashes'],
                'run_manifest_sha256':manifest_hash}
    acceptance=None
    if row.get('reference_acceptance'):
        acceptance_path=folder/'inputs/reference_acceptance.json';shutil.copyfile(row['reference_acceptance'],acceptance_path)
        if sha(acceptance_path)!=row['reference_acceptance_sha256']:raise ValueError('reference acceptance changed during staging')
        acceptance=strict_json(acceptance_path)
    modules['reference_preflight']=envelope('reference_preflight',reference_acceptance(acceptance,row['expected_hashes']))
    atomic_write_json(folder/'results/reference_preflight.json',modules['reference_preflight'])
    def execute(name,dependency=None):
        remaining=timeout-(time.perf_counter()-start)
        if dependency or remaining<=0:
            data=envelope(name,{'status':'not_run','measured_score':None,'raw':{'reason':dependency or 'case budget exhausted'}})
            atomic_write_json(folder/'results'/f'{name}.json',data);modules[name]=data;return
        outcome=run_isolated([sys.executable,'-B',str(BASE/'worker_v5.py'),'--case-dir',str(folder),'--stage',name],
                             folder/'logs'/f'{name}.log',min(stage_timeout,remaining),BASE)
        path=folder/'results'/f'{name}.json'
        if path.exists():
            data=strict_json(path)
            if data.get('run_manifest_sha256')!=manifest_hash:integrity.append(name+': manifest mismatch')
        else:
            progress=folder/'progress'/f'{name}.json'
            partial=strict_json(progress).get('raw',{}) if progress.exists() else {}
            data=envelope(name,{'status':outcome['status'] if outcome['status']!='pass' else 'error','measured_score':None,
                                'raw':{**partial,'reason':'stage timeout' if outcome['status']=='timeout' else 'stage crashed/no result'}})
            atomic_write_json(path,data)
        data['execution']=outcome;modules[name]=data
        if data.get('raw',{}).get('integrity_error'):integrity.append(name+': worker integrity failure')
        print(row['case'],name,data['status'],f"{outcome['wall_s']:.2f}s",flush=True)
    execute('candidate_basic');execute('reference_basic')
    basic=modules['candidate_basic']['status']=='pass'
    gate=None if basic else 'candidate basic did not pass'
    if mode=='geometry':execute('pose',gate)
    else:
        modules['pose']=envelope('pose',{'status':'not_run','raw':{'reason':'strict world frame requires no alignment'}})
        atomic_write_json(folder/'results/pose.json',modules['pose'])
    aligned=mode=='strict' or modules['pose']['status']=='pass'
    pose_gate=gate or (None if aligned else 'whole-model rigid alignment unavailable')
    # Shape/dimensions precede mass; each is independently killable.
    execute('shape',pose_gate);execute('dimension',pose_gate);execute('mass',gate)
    execute('part_matching',pose_gate)
    execute('candidate_self_intersection',gate);execute('candidate_interference',gate)
    execute('fit',pose_gate)
    checks={n:modules[n]['status'] for n in ('part_matching','candidate_self_intersection','candidate_interference','fit')}
    state='fail' if 'fail' in checks.values() else 'pass' if all(x=='pass' for x in checks.values()) else 'partial'
    modules['candidate_assembly']=envelope('candidate_assembly',{'status':state,'raw':{'checks':checks,
        'completed_count':sum(x in ('pass','fail') for x in checks.values()),'total_count':len(checks),
        'unchecked':[n for n,s in checks.items() if s not in ('pass','fail')]}})
    atomic_write_json(folder/'results/candidate_assembly.json',modules['candidate_assembly'])
    for role in ('gt','candidate','annotations'):
        if sha(folder/'inputs'/('annotations.json' if role=='annotations' else role+'.step'))!=row['expected_hashes'][role]:
            integrity.append('staged input changed: '+role)
    if mode=='geometry' and modules['pose']['status']=='pass':
        pose=modules['pose']['raw'];derived=folder/'derived/candidate_aligned.step'
        if not derived.exists() or sha(derived)!=pose.get('aligned_candidate_sha256') or sha(derived)!=pose.get('evaluation_candidate_sha256'):
            integrity.append('derived geometry identity mismatch')
    result=aggregate_case(row['case'],modules,row['expected_hashes'],coordinate_mode=mode)
    if modules['reference_preflight'].get('raw',{}).get('integrity_error'):integrity.append('reference acceptance mismatch')
    if integrity:
        revoke(result,integrity)
    result.update(run_manifest_sha256=manifest_hash,execution={'status':'completed','wall_s':time.perf_counter()-start,
                  'stage_timeouts':[n for n,r in modules.items() if r['status']=='timeout']})
    return result

def revoke(result,errors):
    result.update(status='integrity_error',final_score=None,partial_lower_bound=None,participates_in_scoring=False,score_kind='unscored')
    result['reasons'].extend({'status':'integrity_error','detail':x} for x in errors)

def reports(output,summary):
    columns=['case','candidate_basic','candidate_assembly','reference_preflight','final_score','partial_lower_bound','coverage','shape','dimension','mass']
    rows=[]
    for r in summary['cases']:
        rows.append([r['case'],r['candidate_basic']['status'],r['candidate_assembly']['status'],r['reference_preflight']['status'],
            r['final_score'],r['partial_lower_bound'],r['measurement_weight_coverage'],*[r['metrics'][n]['measured_score'] for n in ('shape','dimension','mass')]])
    with (output/'comparison.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f);writer.writerow(columns);writer.writerows(rows)
    show=lambda v:'null' if v is None else str(v)
    text='V5 geometric similarity; assembly status is independent. Lower bounds are diagnostic, never complete totals.\n\n'
    text+='| '+' | '.join(columns)+' |\n|'+'|'.join(['---']*len(columns))+'|\n'
    text+='\n'.join('| '+' | '.join(show(v) for v in row)+' |' for row in rows)
    text+='\n\nComplete mean cases: '+str(summary['complete_case_ids'])+'\n'
    (output/'report.md').write_text(text,encoding='utf-8')
    table='<table><tr>'+''.join('<th>'+html.escape(c)+'</th>' for c in columns)+'</tr>'
    table+=''.join('<tr>'+''.join('<td>'+html.escape(show(v))+'</td>' for v in row)+'</tr>' for row in rows)+'</table>'
    (output/'report.html').write_text('<!doctype html><meta charset="utf-8"><title>V5 evaluation</title><h1>V5 geometric similarity</h1><p>Assembly and reference acceptance are separate. null = unmeasured or unscored. Lower bounds are diagnostic.</p>'+table,encoding='utf-8')

def main(argv=None):
    from evaluate import validate_cases,code_inventory,dependency_versions
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--cases',type=Path,required=True);ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--protocol',choices=[PROTOCOL_ID],default=PROTOCOL_ID)
    ap.add_argument('--weights',type=Path,default=BASE/'weights_v5.json')
    ap.add_argument('--coordinate-mode',choices=['geometry','strict'],default='geometry')
    ap.add_argument('--timeout',type=float,default=600);ap.add_argument('--stage-timeout',type=float,default=90)
    ap.add_argument('--workers',type=int,choices=range(1,5),default=1);args=ap.parse_args(argv)
    if any(not math.isfinite(x) or x<=0 for x in (args.timeout,args.stage_timeout)):ap.error('timeouts must be finite positive seconds')
    if args.output.exists():ap.error('output already exists')
    rows=validate_cases(strict_json(args.cases))
    weights=strict_json(args.weights)
    if weights.get('protocol_id')!=PROTOCOL_ID or weights.get('weights')!=DEFAULT_WEIGHTS:ap.error('v5 requires frozen protocol weights')
    for row in rows:
        if row.get('reference_acceptance'):
            path=Path(row['reference_acceptance'])
            if not path.is_absolute() or not path.is_file() or sha(path)!=row.get('reference_acceptance_sha256'):
                raise ValueError('missing absolute reference acceptance or SHA mismatch')
    output=args.output.resolve();output.mkdir(parents=True);start=time.perf_counter();code=code_inventory()
    manifest={'schema_version':SCHEMA_VERSION,'protocol_id':PROTOCOL_ID,'cases':rows,'cases_sha256':sha(args.cases),
              'evaluator_path':str(BASE/'evaluate_v5.py'),'code_hashes':code,'dependencies':dependency_versions(),
              'weights':weights,'weights_sha256':sha(args.weights),
              'coordinate_mode':args.coordinate_mode,'timeout_per_case_s':args.timeout,'stage_timeout_s':args.stage_timeout,
              'started_at':utc_now(),'ranking_eligible':False,'cache_policy':'disabled; no reuse across runs'}
    atomic_write_json(output/'manifest.json',manifest);digest=sha(output/'manifest.json');results=[]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs={pool.submit(run_case,row,output,digest,args.timeout,args.stage_timeout,args.coordinate_mode):row for row in rows}
        for future in as_completed(jobs):
            row=jobs[future]
            try:results.append(future.result())
            except Exception as exc:
                result=aggregate_case(row['case'],{},row['expected_hashes'],coordinate_mode=args.coordinate_mode)
                result.update(status='error',execution={'status':'error','reason':str(exc)});results.append(result)
    results.sort(key=lambda r:r['case'])
    unchanged=code==code_inventory() and sha(args.weights)==manifest['weights_sha256']
    inputs_unchanged=sha(args.cases)==manifest['cases_sha256'] and all(sha(r[k])==r['expected_hashes'][k] for r in rows for k in ('gt','candidate','annotations'))
    acceptance_unchanged=all(not r.get('reference_acceptance') or sha(r['reference_acceptance'])==r['reference_acceptance_sha256'] for r in rows)
    if not unchanged or not inputs_unchanged or not acceptance_unchanged:
        for result in results:revoke(result,['code or original inputs or acceptance changed during run'])
    for result in results:atomic_write_json(output/result['case']/'aggregate.json',result)
    complete=[r for r in results if r['final_score'] is not None];count=len(results)
    summary={'schema_version':SCHEMA_VERSION,'protocol_id':PROTOCOL_ID,'coordinate_mode':args.coordinate_mode,'cases':results,
             'run_manifest_sha256':digest,'code_unchanged':unchanged,'original_inputs_unchanged':inputs_unchanged,
             'acceptance_unchanged':acceptance_unchanged,'ranking_eligible':False,'total_case_count':count,
             'complete_case_ids':[r['case'] for r in complete],'participating_case_count':len(complete),
             'batch_mean':math.fsum(r['final_score'] for r in complete)/len(complete) if complete else None,
             'batch_mean_scope':'only explicitly listed complete_case_ids with accepted reference and basic pass',
             'candidate_failure_rate':sum(r['candidate_basic']['status']=='fail' for r in results)/count,
             'assembly_pass_rate':sum(r['candidate_assembly']['status']=='pass' for r in results)/count,
             'measurement_completion_rate':sum(r['measurement_count']==3 for r in results)/count,
             'basic_inconclusive_rate':sum(r['candidate_basic']['status'] not in ('pass','fail') for r in results)/count,
             'wall_s':time.perf_counter()-start,'ended_at':utc_now()}
    atomic_write_json(output/'summary.json',summary);reports(output,summary)
    print('Output:',output,flush=True)
    return 0 if unchanged and inputs_unchanged and acceptance_unchanged and all(r.get('execution',{}).get('status')=='completed' for r in results) else 2

if __name__=='__main__':raise SystemExit(main())

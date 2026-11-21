"""Verify v5 publication without treating incomplete measurements as scores."""
import csv
import math
from pathlib import Path
import tempfile
from src.io_utils import sha, strict_json

PROTOCOL = 'fusion-step-adapted-v5.0'
WEIGHTS = {'shape':6/13, 'dimension':5/13, 'mass':2/13}

def number(value):
    return type(value) in (int,float) and math.isfinite(value)

def validate_score_row(row):
    metrics=row.get('metrics',{})
    measured={}
    for name in WEIGHTS:
        metric=metrics.get(name,{})
        value=metric.get('measured_score')
        if value is not None:
            if metric.get('status')!='pass' or not number(value) or not 0<=value<=1:
                raise ValueError('invalid v5 measurement: '+name)
            measured[name]=value
    coverage=math.fsum(WEIGHTS[n] for n in measured)
    weighted=math.fsum(WEIGHTS[n]*v for n,v in measured.items())
    if row.get('measurement_count')!=len(measured) or not math.isclose(row.get('measurement_weight_coverage',-1),coverage,abs_tol=1e-12):
        raise ValueError('v5 coverage does not match measurements')
    final=row.get('final_score')
    if final is not None:
        if (len(measured)!=3 or row.get('candidate_basic',{}).get('status')!='pass'
                or row.get('reference_preflight',{}).get('status')!='pass'
                or row.get('reference_preflight',{}).get('raw',{}).get('acceptance_verified') is not True
                or row.get('status')=='integrity_error'):
            raise ValueError('formal score has unqualified inputs or incomplete measurements')
        if not number(final) or not math.isclose(final,weighted,abs_tol=1e-12):
            raise ValueError('v5 formal score differs from measurements')
    lower=row.get('partial_lower_bound')
    if lower is not None and (not number(lower) or not math.isclose(lower,weighted,abs_tol=1e-12)):
        raise ValueError('v5 diagnostic lower bound mismatch')
    if row.get('ranking_eligible') is not False or row.get('participates_in_scoring') is not (final is not None):
        raise ValueError('v5 publication eligibility mismatch')
    expected_kind='complete_geometric_similarity' if final is not None else 'unscored'
    if row.get('score_kind')!=expected_kind: raise ValueError('v5 score kind mismatch')

def validate_publication(output,cases_path,returncode,weights_sha256,*,protocol=None,coordinate_mode=None):
    output=Path(output)
    if returncode not in (0,2): raise ValueError('v5 evaluator did not finish publication')
    for name in ('manifest.json','summary.json','report.md','comparison.csv','report.html'):
        if not (output/name).is_file() or not (output/name).stat().st_size:
            raise ValueError('missing v5 publication: '+name)
    manifest=strict_json(output/'manifest.json');summary=strict_json(output/'summary.json')
    cases=strict_json(cases_path)['cases'];digest=sha(output/'manifest.json')
    for doc in (manifest,summary):
        if doc.get('protocol_id')!=PROTOCOL or doc.get('schema_version')!='5.0':
            raise ValueError('v5 schema/protocol mismatch')
    if protocol not in (None,PROTOCOL): raise ValueError('wrong requested protocol')
    mode=manifest.get('coordinate_mode')
    if mode not in ('geometry','strict') or summary.get('coordinate_mode')!=mode or coordinate_mode not in (None,mode):
        raise ValueError('v5 coordinate mode mismatch')
    if (manifest.get('cases')!=cases or manifest.get('cases_sha256')!=sha(cases_path)
            or manifest.get('weights_sha256')!=weights_sha256 or summary.get('run_manifest_sha256')!=digest):
        raise ValueError('v5 publication identity mismatch')
    if any(summary.get(k) is not True for k in ('code_unchanged','original_inputs_unchanged','acceptance_unchanged')):
        raise ValueError('v5 inputs/code changed during evaluation')
    finals=summary.get('cases',[]);by_id={r['case']:r for r in finals}
    if len(by_id)!=len(finals) or set(by_id)!={r['case'] for r in cases}: raise ValueError('v5 case coverage mismatch')
    for case,row in by_id.items():
        if (strict_json(output/case/'aggregate.json')!=row or row.get('run_manifest_sha256')!=digest
                or row.get('protocol_id')!=PROTOCOL or row.get('coordinate_mode')!=mode):
            raise ValueError('v5 aggregate identity mismatch: '+case)
        validate_score_row(row)
        source=next(r for r in cases if r['case']==case)
        for role in ('candidate','gt','annotations'):
            staged=output/case/'inputs'/('annotations.json' if role=='annotations' else role+'.step')
            if sha(staged)!=source['expected_hashes'][role]: raise ValueError('v5 staged input identity mismatch')
        from src.contract_v5 import aggregate_case,reference_acceptance
        acceptance=None
        if source.get('reference_acceptance'):
            staged=output/case/'inputs/reference_acceptance.json'
            if (sha(staged)!=source.get('reference_acceptance_sha256') or
                    sha(source['reference_acceptance'])!=source.get('reference_acceptance_sha256')):
                raise ValueError('v5 frozen acceptance identity mismatch')
            acceptance=strict_json(staged)
        expected_acceptance=reference_acceptance(acceptance,source['expected_hashes'])
        measured_acceptance=row.get('measurements',{}).get('reference_preflight',{})
        if any(measured_acceptance.get(k)!=v for k,v in expected_acceptance.items()):
            raise ValueError('v5 acceptance evidence does not match frozen document')
        for stage,data in row.get('measurements',{}).items():
            saved=strict_json(output/case/'results'/f'{stage}.json')
            if {k:v for k,v in data.items() if k!='execution'}!={k:v for k,v in saved.items() if k!='execution'}:
                raise ValueError('v5 stage evidence differs from publication: '+stage)
            if saved.get('run_manifest_sha256')!=digest: raise ValueError('v5 stale stage evidence')
        rebuilt=aggregate_case(case,row['measurements'],source['expected_hashes'],coordinate_mode=mode)
        if row.get('status')=='integrity_error':
            raise ValueError('v5 integrity failure cannot be published')
        for field,value in rebuilt.items():
            if row.get(field)!=value: raise ValueError('v5 contract reconstruction mismatch: '+field)
    complete=[r for r in finals if r.get('final_score') is not None]
    mean=math.fsum(r['final_score'] for r in complete)/len(complete) if complete else None
    if (summary.get('batch_mean')!=mean or summary.get('complete_case_ids')!=[r['case'] for r in complete]
            or summary.get('participating_case_count')!=len(complete) or summary.get('ranking_eligible') is not False):
        raise ValueError('v5 complete mean includes incomplete cases')
    count=len(finals)
    expected_rates={'total_case_count':count,
        'candidate_failure_rate':sum(r['candidate_basic']['status']=='fail' for r in finals)/count,
        'assembly_pass_rate':sum(r['candidate_assembly']['status']=='pass' for r in finals)/count,
        'measurement_completion_rate':sum(r['measurement_count']==3 for r in finals)/count,
        'basic_inconclusive_rate':sum(r['candidate_basic']['status'] not in ('pass','fail') for r in finals)/count}
    for field,value in expected_rates.items():
        if summary.get(field)!=value:raise ValueError('v5 summary rate mismatch: '+field)
    with (output/'comparison.csv').open(encoding='utf-8-sig',newline='') as stream: comparisons=list(csv.DictReader(stream))
    if len(comparisons)!=len(finals) or {r['case'] for r in comparisons}!=set(by_id):
        raise ValueError('v5 CSV case coverage mismatch')
    for view in comparisons:
        row=by_id[view['case']]
        for field in ('final_score','partial_lower_bound'):
            if (float(view[field]) if view[field] else None)!=row[field]: raise ValueError('v5 CSV score mismatch')
        for name in WEIGHTS:
            if (float(view[name]) if view[name] else None)!=row['metrics'][name]['measured_score']: raise ValueError('v5 CSV measurement mismatch')
        for field in ('candidate_basic','candidate_assembly','reference_preflight'):
            if view[field]!=row[field]['status']: raise ValueError('v5 CSV check status mismatch')
    success=all(r.get('execution',{}).get('status')=='completed' for r in finals)
    if returncode!=(0 if success else 2): raise ValueError('v5 execution completion mismatch')
    from evaluate_v5 import reports
    with tempfile.TemporaryDirectory() as temp:
        regenerated=Path(temp);reports(regenerated,summary)
        for name in ('comparison.csv','report.md','report.html'):
            if (regenerated/name).read_bytes()!=(output/name).read_bytes():
                raise ValueError('v5 rendered publication differs from JSON: '+name)
    return summary

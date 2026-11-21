"""V5 contract: measurements, assembly acceptance and reference acceptance are distinct."""
import math
import re
from .contract import number

PROTOCOL_ID='fusion-step-adapted-v5.0'
SCHEMA_VERSION='5.0'
STATES=('pass','fail','error','timeout','partial','not_run')
SCORED_METRICS=('shape','dimension','mass')
DEFAULT_WEIGHTS={'shape':6/13,'dimension':5/13,'mass':2/13}
STAGES=('candidate_basic','reference_basic','reference_preflight','pose','shape','dimension','mass',
        'part_matching','candidate_self_intersection','candidate_interference','fit','candidate_assembly')
POSE_DEPENDENT={'shape','dimension','part_matching','fit'}

def aggregate_case(case_id, modules, expected_hashes, *, coordinate_mode='geometry'):
    errors=[]
    if not isinstance(expected_hashes,dict) or set(expected_hashes)!={'gt','candidate','annotations'} or not all(
            isinstance(x,str) and re.fullmatch('[0-9a-f]{64}',x) for x in expected_hashes.values()):
        errors.append('invalid expected input hashes')
    if coordinate_mode not in ('geometry','strict'): errors.append('invalid coordinate mode')
    rows={}
    for name,row in modules.items():
        if name not in STAGES or not isinstance(row,dict):
            errors.append('unknown or malformed stage: '+name); continue
        if (row.get('schema_version')!=SCHEMA_VERSION or row.get('protocol_id')!=PROTOCOL_ID or
            row.get('case')!=case_id or row.get('input_hashes')!=expected_hashes or
            row.get('coordinate_mode')!=coordinate_mode or row.get('status') not in STATES):
            errors.append(name+': identity/schema/status mismatch')
        rows[name]=row
    pose=rows.get('pose',{})
    derived=pose.get('raw',{}).get('evaluation_candidate_sha256')
    for name,row in rows.items():
        if row.get('status')!='pass': continue
        evaluated=expected_hashes
        if name in POSE_DEPENDENT and coordinate_mode=='geometry':
            if pose.get('status')!='pass' or not isinstance(derived,str) or not re.fullmatch('[0-9a-f]{64}',derived):
                errors.append(name+': no qualified whole-model rigid alignment'); continue
            evaluated={**expected_hashes,'candidate':derived}
        if row.get('evaluation_input_hashes')!=evaluated: errors.append(name+': evaluated identity mismatch')
    empty=lambda: {'status':'not_run','raw':{'reason':'stage not run'}}
    basic=rows.get('candidate_basic',empty())
    reference=dict(rows.get('reference_preflight',empty()))
    if reference.get('status')=='pass' and reference.get('raw',{}).get('acceptance_verified') is not True:
        reference={'status':'error','raw':{'reason':'reference acceptance not verified'}}
    dr=rows.get('dimension',{}).get('raw',{})
    if rows.get('dimension',{}).get('legacy_status')=='reference_invalid' or ('gt_self_check_total' in dr and
            (not number(dr['gt_self_check_total']) or dr['gt_self_check_total']<=0 or dr.get('gt_self_check_passed')!=dr['gt_self_check_total'])):
        reference={'status':'fail','raw':{'reason':'GT dimension self check failed','dimension_evidence':dr,'acceptance_evidence':reference}}
    if rows.get('reference_basic',{}).get('status')=='fail':
        reference={'status':'fail','raw':{'reason':'reference basic validation failed',
                   'basic_evidence':rows['reference_basic'],'acceptance_evidence':reference}}
    elif rows.get('reference_basic',{}).get('status')!='pass' and reference.get('status')=='pass':
        reference={'status':rows.get('reference_basic',{}).get('status','not_run'),
                   'raw':{'reason':'reference basic validation did not pass','acceptance_evidence':reference}}
    metrics={}
    for name in SCORED_METRICS:
        row=rows.get(name,empty()); value=row.get('measured_score')
        valid=row.get('status')=='pass' and number(value) and 0<=value<=1
        metrics[name]={**row,'measured_score':float(value) if valid else None,'awarded_score':None,'metric_role':'scored'}
    missing=[n for n in SCORED_METRICS if metrics[n]['measured_score'] is None]
    coverage=math.fsum(DEFAULT_WEIGHTS[n] for n in SCORED_METRICS if n not in missing)
    qualified=not errors and basic.get('status')=='pass' and reference.get('status')=='pass'
    measured=math.fsum(DEFAULT_WEIGHTS[n]*metrics[n]['measured_score'] for n in SCORED_METRICS if n not in missing)
    final=measured if qualified and not missing else None
    result={'schema_version':SCHEMA_VERSION,'protocol_id':PROTOCOL_ID,'case':case_id,'coordinate_mode':coordinate_mode,
        'input_hashes':expected_hashes,'status':'integrity_error' if errors else 'candidate_invalid' if basic.get('status')=='fail'
            else 'reference_pending' if reference.get('status')!='pass' else 'partial' if missing else 'ok' if qualified else 'inconclusive',
        'candidate_basic':basic,'candidate_assembly':rows.get('candidate_assembly',empty()),'reference_preflight':reference,
        'measurements':rows,'metrics':metrics,'module_scores':{n:m['measured_score'] for n,m in metrics.items()},
        'final_score':final,'partial_lower_bound':measured if not errors and basic.get('status')=='pass' else None,
        'partial_lower_bound_kind':'diagnostic_missing_credit_floor_not_complete_score',
        'measurement_weight_coverage':coverage,'measurement_count':3-len(missing),'unavailable_metrics':missing,
        'weights':dict(DEFAULT_WEIGHTS),'scored_metrics':list(SCORED_METRICS),'ranking_eligible':False,
        'validation_passed':basic.get('status')=='pass','participates_in_scoring':final is not None,
        'score_kind':'complete_geometric_similarity' if final is not None else 'unscored',
        'reasons':[{'status':'integrity_error','detail':x} for x in errors]}
    return result

def reference_acceptance(document, hashes):
    if document is None: return {'status':'not_run','raw':{'reason':'frozen reference acceptance absent'}}
    if document.get('gt_sha256')!=hashes['gt'] or document.get('annotations_sha256')!=hashes['annotations']:
        return {'status':'error','raw':{'reason':'reference acceptance input hash mismatch','integrity_error':True}}
    checks=document.get('checks',{})
    complete=all(checks.get(n,{}).get('status')=='pass' for n in
                 ('inventory','configuration','dimensions','self_intersection','interference'))
    status=document.get('status','not_run')
    accepted=status=='pass' and complete and document.get('unresolved_issues')==[]
    return {'status':'pass' if accepted else status if status in STATES and status!='pass' else 'partial',
            'raw':{'acceptance_verified':accepted,'acceptance':document}}

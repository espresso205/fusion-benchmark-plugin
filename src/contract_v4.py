"""V4: three scored modules, separate diagnostics and explicit pose evidence."""
import math
import re

from .contract import number

PROTOCOL_ID = 'fusion-step-adapted-v4.0'
SCHEMA_VERSION = '4.0'
SCORED_METRICS = ('shape', 'dimension', 'mass')
DIAGNOSTIC_METRICS = ('distance', 'face_match', 'section')
DEFAULT_WEIGHTS = {'shape':6/13, 'dimension':5/13, 'distance':0., 'mass':2/13,
                   'face_match':0., 'section':0.}
MODULE_NAMES = ('validity', 'pose', *DEFAULT_WEIGHTS, 'world_shape')
POSE_DEPENDENT = {'shape','dimension',*DIAGNOSTIC_METRICS}


def valid_weights(weights):
    return (isinstance(weights,dict) and set(weights)==set(DEFAULT_WEIGHTS)
        and all(number(v) and (v > 0 if k in SCORED_METRICS else v == 0) for k,v in weights.items())
        and math.isclose(math.fsum(weights.values()),1.,abs_tol=1e-12))


def _hashes(value):
    return (isinstance(value,dict) and set(value)=={'gt','candidate','annotations'}
        and all(isinstance(v,str) and re.fullmatch('[0-9a-f]{64}',v) for v in value.values()))


def aggregate_case(case_id, modules, expected_hashes, weights=None, *, coordinate_mode='geometry'):
    modules = modules if isinstance(modules,dict) else {}
    w = dict(DEFAULT_WEIGHTS) if weights is None else weights
    w = w.get('weights',w) if isinstance(w,dict) else w
    integrity, reasons = [], []
    if not valid_weights(w):
        integrity.append('three positive scored weights and three zero diagnostic weights summing to one required')
        w = dict(DEFAULT_WEIGHTS)
    if not _hashes(expected_hashes):
        integrity.append('invalid expected hashes')
    if coordinate_mode not in ('geometry','strict'):
        integrity.append('invalid coordinate mode')
    invalid_identity=set()
    for name,row in modules.items():
        if name not in MODULE_NAMES or not isinstance(row,dict):
            integrity.append('unknown or malformed module: '+str(name)); invalid_identity.add(name)
        elif (row.get('schema_version')!=SCHEMA_VERSION or row.get('protocol_id')!=PROTOCOL_ID
                or row.get('case')!=case_id or row.get('input_hashes')!=expected_hashes
                or row.get('coordinate_mode')!=coordinate_mode):
            integrity.append(name+': schema/protocol/case/input/mode mismatch'); invalid_identity.add(name)
    pose = modules.get('pose',{})
    pose = pose if isinstance(pose,dict) else {}
    pose_raw = pose.get('raw',{}) if isinstance(pose.get('raw'),dict) else {}
    pose_ok = (pose.get('status')=='ok' and pose.get('quality',{}).get('measurement_valid') is True
               and 'pose' not in invalid_identity)
    evaluated_sha = pose_raw.get('evaluation_candidate_sha256') if coordinate_mode=='geometry' else expected_hashes.get('candidate')
    for name,row in modules.items():
        if not isinstance(row,dict) or name in invalid_identity or row.get('status')!='ok':
            continue
        if name in POSE_DEPENDENT and coordinate_mode=='geometry':
            if not pose_ok or not isinstance(evaluated_sha,str) or not re.fullmatch('[0-9a-f]{64}',evaluated_sha):
                integrity.append(name+': measured geometry without qualified pose'); invalid_identity.add(name); continue
            expected_eval={**expected_hashes,'candidate':evaluated_sha}
        else:
            expected_eval=expected_hashes
        if row.get('evaluation_input_hashes')!=expected_eval:
            integrity.append(name+': evaluated input identity mismatch'); invalid_identity.add(name)
    v = modules.get('validity',{})
    v = v if isinstance(v,dict) else {}
    q = v.get('quality',{}) if isinstance(v.get('quality'),dict) else {}
    vr = v.get('raw',{}) if isinstance(v.get('raw'),dict) else {}
    candidate = vr.get('candidate',{}) if isinstance(vr.get('candidate'),dict) else {}
    reference = vr.get('gt',{}) if isinstance(vr.get('gt'),dict) else {}
    invalid = v.get('status')=='candidate_invalid' and q.get('candidate_invalid_confirmed') is True
    passed = (not invalid and 'validity' not in invalid_identity and q.get('candidate_valid') is True
        and candidate.get('healthy') is True and (v.get('status')=='ok' and v.get('score')==1.
        and q.get('measurement_valid') is True or v.get('status')=='reference_invalid'
        or v.get('status')=='evaluator_error' and vr.get('stage')=='gt_validation'))
    participating = passed and not integrity
    metrics, awards, missing, diagnostic_missing = {}, {}, [], []
    for name in ('validity',*DEFAULT_WEIGHTS):
        row=modules.get(name,{})
        row=row if isinstance(row,dict) else {}
        quality=row.get('quality',{}) if isinstance(row.get('quality'),dict) else {}
        raw=row.get('raw',{}) if isinstance(row.get('raw'),dict) else {}
        value=row.get('score')
        valid=(name not in invalid_identity and row.get('status')=='ok'
               and quality.get('measurement_valid') is True and number(value) and 0<=value<=1)
        if name=='dimension':
            valid=valid and (number(raw.get('gt_self_check_total')) and raw['gt_self_check_total']>0
                            and raw.get('gt_self_check_passed')==raw['gt_self_check_total']) if 'gt_self_check_total' in raw else valid
        measured=float(value) if valid else None
        role='scored' if name in SCORED_METRICS else 'validation' if name=='validity' else 'diagnostic'
        award=(measured if valid else 0.) if participating and role=='scored' else None
        if role=='scored':
            awards[name]=award
            if not valid:
                missing.append(name)
                reasons.append({'module':name,'status':'measurement_unavailable','detail':raw.get('error',row.get('status','missing'))})
        elif role=='diagnostic':
            awards[name]=None
            if not valid: diagnostic_missing.append(name)
        metrics[name]={'status':row.get('status','missing'),'value':measured,'score':measured,
            'measured_score':measured,'awarded_score':award,'metric_role':role,
            'diagnostic_value':float(value) if number(value) and not valid else None,
            'score_source':('approximate_measurement' if quality.get('scored_approximate') else 'measurement') if valid
                else 'unavailable_policy_floor' if role=='scored' else 'diagnostic_unavailable',
            'quality':('approximate' if quality.get('scored_approximate') else 'ok') if valid else 'measurement_unavailable',
            'backend':row.get('backend'),'parameters':row.get('parameters'),'uncertainty':row.get('uncertainty'),
            'timing':row.get('timing'),'unit':'normalized_score_0_to_1','reason_codes':[] if valid else [row.get('status','missing')],
            'evidence':{'result_file':f'results/{name}.json','input_hashes':row.get('input_hashes'),
                        'evaluation_input_hashes':row.get('evaluation_input_hashes'),'run_manifest_sha256':row.get('run_manifest_sha256')}}
    coverage=math.fsum(w[n] for n in SCORED_METRICS if metrics[n]['measured_score'] is not None)
    score=math.fsum(w[n]*awards[n] for n in SCORED_METRICS) if participating else None
    status=('integrity_error' if integrity else 'candidate_invalid' if invalid else 'validation_inconclusive' if not passed
            else 'scored_with_unavailable_metrics' if missing else 'ok')
    reasons.extend({'status':'integrity_error','module':'integrity','detail':x} for x in integrity)
    if not passed: reasons.append({'status':status,'module':'validity','detail':'candidate validation did not pass'})
    out={'schema_version':SCHEMA_VERSION,'protocol_id':PROTOCOL_ID,'case':case_id,'coordinate_mode':coordinate_mode,
        'status':status,'final_score':score,'weights':w,'scored_metrics':list(SCORED_METRICS),
        'diagnostic_metrics':list(DIAGNOSTIC_METRICS),'module_scores':{n:m['measured_score'] for n,m in metrics.items()},
        'awarded_module_scores':awards,'metrics':metrics,'reasons':reasons,'failure':reasons,
        'participates_in_scoring':participating,'validation_passed':passed,'measurement_weight_coverage':coverage,
        'unavailable_metrics':missing,'diagnostic_unavailable_metrics':diagnostic_missing,
        'score_kind':'policy_lower_bound' if participating and missing else 'measured' if participating else 'unscored',
        'score_interval':[score,min(1.,math.fsum([score,*(w[n] for n in missing)]))] if participating else None,
        'score_interval_kind':'missing_credit_range_not_numerical_confidence','ranking_eligible':False,
        'quality_status':'measurement_unavailable' if missing else 'ok','quality_flags':['measurement_unavailable'] if missing else [],
        'geometry':{'candidate_status':'valid' if passed else 'invalid' if invalid else 'unknown',
                    'reference_status':'valid' if reference.get('healthy') is True else 'unknown',
                    'reference_preflight':{'validated':q.get('reference_validated'),'status':q.get('reference_status','unknown')}},
        'pose':pose,'coordinate_diagnostics':modules.get('world_shape',{})}
    return refresh_assessment(out)


def refresh_assessment(result, execution=None):
    score=result.get('final_score')
    if not number(score):
        result.update(participates_in_scoring=False,score_kind='unscored',score_interval=None)
        result['awarded_module_scores']={n:None for n in result.get('awarded_module_scores',{})}
        for m in result.get('metrics',{}).values(): m['awarded_score']=None
    result['scores']={PROTOCOL_ID:{'value':score,'aggregation_status':'scored' if number(score) else 'blocked',
        'score_kind':result.get('score_kind'),'blocked_reasons':result.get('reasons',[]) if not number(score) else [],
        'coverage':{'scored_modules':sum(result.get('metrics',{}).get(n,{}).get('measured_score') is not None for n in SCORED_METRICS),
                    'required_modules':3,'weight':result.get('measurement_weight_coverage',0.)},'rank_eligible':False}}
    if execution is not None: result['execution']=execution
    return result

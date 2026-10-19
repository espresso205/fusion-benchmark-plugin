"""Fail-closed module and aggregation contract; no missing-score imputation."""
import json
import math
import os
from pathlib import Path
import re
import tempfile

PROTOCOL_ID = 'fusion-step-adapted-v2.0'
DEFAULT_WEIGHTS = {'shape':0.30,'dimension':0.25,'distance':0.15,'mass':0.10,'face_match':0.10,'section':0.10}
MODULE_NAMES = ('validity', *DEFAULT_WEIGHTS)
STATUSES = {'ok','evaluator_error','reference_invalid','unsupported','candidate_invalid','candidate_missing','candidate_timeout'}
MEASUREMENT_QUALITIES = {'ok','ok_with_warning','measurement_unreliable','unsupported_metric'}


def atomic_write_json(path, data):
    path = Path(path)
    if path.exists():
        raise FileExistsError(path)
    payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        # Linking an already-complete inode publishes atomically and refuses
        # existing destinations, unlike replace(). Both files are on one volume.
        os.link(temp,path)
    finally:
        Path(temp).unlink(missing_ok=True)


def number(v):
    return not isinstance(v,bool) and isinstance(v,(int,float)) and math.isfinite(v)


def refresh_assessment(result, modules=None, execution=None, expected_hashes=None):
    """Attach independent execution, geometry, measurement, and score states.

    The legacy top-level ``status`` remains stable for existing consumers. New
    consumers should use ``quality_status``, ``geometry``, ``metrics`` and
    ``scores`` so a failed measurement is not mistaken for invalid geometry.
    """
    if result.get('protocol_id') == 'fusion-step-adapted-v3.0':
        from .contract_v3 import refresh_assessment as refresh_v3
        return refresh_v3(result, execution)
    has_module_records = isinstance(modules, dict)
    modules = modules if has_module_records else {}
    metric_rows = result.get('metrics', {}) if not has_module_records and isinstance(result.get('metrics'), dict) else {}
    result_reasons = result.get('reasons') if isinstance(result.get('reasons'), list) else []
    evaluator_error_modules = {
        reason.get('module') for reason in result_reasons
        if isinstance(reason, dict) and reason.get('status') == 'evaluator_error' and reason.get('module')
    }

    def valid_metric_score(name):
        row = modules.get(name)
        if not isinstance(row, dict) or name in evaluator_error_modules:
            return False
        quality = row.get('quality') if isinstance(row.get('quality'), dict) else {}
        raw = row.get('raw') if isinstance(row.get('raw'), dict) else {}
        if (row.get('status') != 'ok' or quality.get('measurement_valid') is not True or
            not number(row.get('score')) or not 0 <= row['score'] <= 1 or
            row.get('schema_version') != '2.0' or row.get('protocol_id') != result.get('protocol_id') or
            row.get('case') != result.get('case')):
            return False
        if expected_hashes is not None and row.get('input_hashes') != expected_hashes:
            return False
        if name == 'dimension' and 'gt_self_check_total' in raw:
            if (not number(raw['gt_self_check_total']) or raw['gt_self_check_total'] <= 0 or
                raw.get('gt_self_check_passed') != raw['gt_self_check_total']):
                return False
        return True

    for name in MODULE_NAMES if has_module_records else ():
        row = modules.get(name)
        if not isinstance(row, dict):
            metric_rows[name] = {
                'status': 'missing', 'value': None, 'score': None, 'diagnostic_value': None,
                'unit': 'normalized_score_0_to_1', 'quality': None,
                'reason_codes': ['missing_module'], 'backend': None,
                'parameters': None, 'uncertainty': None, 'timing': None,
                'evidence': {'result_file': f'results/{name}.json'},
            }
            continue
        status = row.get('status', 'unknown')
        quality = row.get('quality') if isinstance(row.get('quality'), dict) else {}
        raw = row.get('raw') if isinstance(row.get('raw'), dict) else {}
        score = row.get('score') if number(row.get('score')) else None
        dimension_self_check_failed = (
            name == 'dimension' and 'gt_self_check_total' in raw and
            (not number(raw.get('gt_self_check_total')) or raw['gt_self_check_total'] <= 0 or
             raw.get('gt_self_check_passed') != raw['gt_self_check_total'])
        )
        if name in evaluator_error_modules:
            quality_status = None
        elif dimension_self_check_failed:
            quality_status = 'measurement_unreliable'
        elif status == 'unsupported':
            quality_status = 'unsupported_metric'
        elif status == 'reference_invalid' or quality.get('measurement_valid') is False:
            quality_status = 'measurement_unreliable'
        elif status == 'evaluator_error':
            quality_status = None
        elif status in ('candidate_invalid', 'candidate_missing', 'candidate_timeout'):
            quality_status = None
        elif status == 'ok':
            quality_status = 'ok_with_warning' if name == 'validity' and quality.get('reference_validated') is False else 'ok'
        else:
            quality_status = None
        reason_codes = quality.get('reason_codes', [])
        if not isinstance(reason_codes, list):
            reason_codes = []
        if status != 'ok' and status not in reason_codes:
            reason_codes = [*reason_codes, status]
        accepted_score = score if valid_metric_score(name) else None
        metric_rows[name] = {
            'status': status,
            'value': accepted_score,
            'score': accepted_score,
            'diagnostic_value': score if score is not None and accepted_score is None else None,
            'unit': 'normalized_score_0_to_1',
            'quality': quality_status,
            'reason_codes': reason_codes,
            'backend': row.get('backend') or raw.get('backend'),
            'parameters': row.get('parameters') or raw.get('parameters'),
            'uncertainty': row.get('uncertainty') or raw.get('uncertainty'),
            'timing': row.get('timing'),
            'evidence': {
                'result_file': f'results/{name}.json',
                'input_hashes': row.get('input_hashes'),
                'run_manifest_sha256': row.get('run_manifest_sha256'),
            },
        }

    validity = modules.get('validity') if isinstance(modules.get('validity'), dict) else {}
    validity_raw = validity.get('raw') if isinstance(validity.get('raw'), dict) else {}
    validity_quality = validity.get('quality') if isinstance(validity.get('quality'), dict) else {}

    def geometry_state(role):
        shape = validity_raw.get('gt' if role == 'reference' else 'candidate')
        if isinstance(shape, dict) and type(shape.get('healthy')) is bool:
            return 'valid' if shape['healthy'] else 'invalid'
        if role == 'candidate' and validity_quality.get('candidate_invalid_confirmed') is True:
            return 'invalid'
        return 'unknown'

    geometry = result.get('geometry') if not has_module_records and isinstance(result.get('geometry'), dict) else {
        'reference_status': geometry_state('reference'),
        'candidate_status': geometry_state('candidate'),
        'reference_preflight': {
            'validated': validity_quality.get('reference_validated'),
            'status': validity_quality.get('reference_status', 'unknown'),
        },
    }
    metric_qualities = {row.get('quality') for row in metric_rows.values() if isinstance(row, dict)}
    quality_flags = {quality for quality in metric_qualities if quality in MEASUREMENT_QUALITIES and quality not in ('ok', 'ok_with_warning')}
    if geometry.get('reference_preflight', {}).get('validated') is False:
        quality_flags.add('measurement_unreliable')
    if 'unsupported_metric' in quality_flags:
        quality_status = 'unsupported_metric'
    elif 'measurement_unreliable' in quality_flags:
        quality_status = 'measurement_unreliable'
    elif None in metric_qualities:
        quality_status = None
    elif 'ok_with_warning' in metric_qualities:
        quality_status = 'ok_with_warning'
    elif 'ok' in metric_qualities:
        quality_status = 'ok'
    else:
        quality_status = None

    reasons = result_reasons
    failures = []
    owner_by_status = {
        'evaluator_error': 'evaluator_or_infrastructure',
        'reference_invalid': 'reference_measurement_or_contract',
        'unsupported': 'metric_support_boundary',
        'candidate_invalid': 'candidate_geometry',
        'candidate_missing': 'submission',
        'candidate_timeout': 'execution',
    }
    action_by_status = {
        'evaluator_error': 'Inspect evaluator and infrastructure evidence; retry only after addressing a transient cause.',
        'reference_invalid': 'Revalidate the reference measurement or contract before scoring; do not infer invalid reference geometry.',
        'unsupported': 'Keep this metric unscored and update the frozen protocol support contract before another scoring run.',
        'candidate_invalid': 'Repair the candidate geometry and submit a new artifact.',
        'candidate_missing': 'Provide the required candidate artifact and rerun the evaluation.',
        'candidate_timeout': 'Review timeout logs and resource limits before considering a bounded retry.',
    }
    for reason in reasons:
        if not isinstance(reason, dict):
            continue
        status = reason.get('status', 'evaluator_error')
        failures.append({
            'stage': reason.get('module') or 'aggregation',
            'reason_code': status,
            'responsible_party': owner_by_status.get(status, 'evaluator_or_protocol'),
            'retryable': None if status == 'evaluator_error' else False,
            'recommended_action': action_by_status.get(status, 'Review the module evidence and scoring contract.'),
            'detail': reason.get('detail'),
        })

    old_profile = result.get('scores', {}).get(result.get('protocol_id'), {}) if isinstance(result.get('scores'), dict) else {}
    old_coverage = old_profile.get('coverage', {}) if isinstance(old_profile, dict) else {}
    if not has_module_records and isinstance(old_coverage.get('scored_modules'), int):
        scored_modules = old_coverage['scored_modules']
    else:
        scored_modules = sum(1 for name in MODULE_NAMES if valid_metric_score(name))
    score_value = result.get('final_score') if number(result.get('final_score')) else None
    score_record = {
        'value': score_value,
        'aggregation_status': 'scored' if score_value is not None else 'blocked',
        'blocked_reasons': reasons if score_value is None else [],
        'coverage': {'scored_modules': scored_modules, 'required_modules': len(MODULE_NAMES)},
        'rank_eligible': False,
    }
    result.update(
        quality_status=quality_status,
        quality_flags=sorted(quality_flags),
        geometry=geometry,
        metrics=metric_rows,
        scores={result.get('protocol_id', PROTOCOL_ID): score_record},
        failure=failures,
    )
    if execution is not None:
        result['execution'] = execution
    return result


def failed_case_result(case_id, detail, protocol_id=PROTOCOL_ID, weights=None):
    """Create the same assessment envelope for failures outside the worker."""
    result = {
        'schema_version': '3.0' if protocol_id == 'fusion-step-adapted-v3.0' else '2.0',
        'protocol_id': protocol_id,
        'case': case_id,
        'status': 'evaluator_error',
        'final_score': None,
        'module_scores': {name: None for name in MODULE_NAMES},
        'weights': DEFAULT_WEIGHTS.copy() if weights is None else weights,
        'reasons': [{'status': 'evaluator_error', 'module': 'execution', 'detail': str(detail)}],
    }
    return refresh_assessment(result, {}, execution={'status': 'failed'})


def validate_case_ids(case_ids, expected_cases=None):
    reasons, seen = [], set()
    for c in case_ids:
        if not isinstance(c,str) or c in seen:
            reasons.append({'status':'evaluator_error','detail':f'invalid or duplicate case: {c!r}'})
        else: seen.add(c)
    if expected_cases is not None and seen != set(expected_cases):
        reasons.append({'status':'evaluator_error','detail':'case ID set differs from frozen manifest'})
    return reasons


def aggregate_case(case_id, modules, expected_hashes, weights=None, protocol_id=PROTOCOL_ID):
    if protocol_id == 'fusion-step-adapted-v3.0':
        from .contract_v3 import aggregate_case as aggregate_v3
        return aggregate_v3(case_id, modules, expected_hashes, weights)
    reasons=[]
    def error(status, detail, module=None):
        reasons.append({'status':status,'module':module,'detail':detail})
    w=DEFAULT_WEIGHTS.copy() if weights is None else weights
    if isinstance(w,dict) and 'weights' in w:
        w=w['weights']
    if not isinstance(w,dict) or set(w)!=set(DEFAULT_WEIGHTS) or any(not number(v) or v<0 for v in w.values()) or not math.isclose(sum(w.values()),1.,abs_tol=1e-12):
        error('evaluator_error','six finite nonnegative weights summing to one required')
    if not isinstance(expected_hashes,dict) or set(expected_hashes)!={'gt','candidate','annotations'} or any(not isinstance(h,str) or not re.fullmatch('[0-9a-f]{64}',h) for h in expected_hashes.values()):
        error('evaluator_error','invalid expected hashes')
    if not isinstance(modules,dict):
        modules={}
        error('evaluator_error','modules must be a mapping')
    for extra in set(modules)-set(MODULE_NAMES):
        error('evaluator_error','unknown module',extra)
    scores={}
    for name in MODULE_NAMES:
        r=modules.get(name)
        if not isinstance(r,dict):
            error('evaluator_error','missing or malformed module',name)
            scores[name]=None
            continue
        scores[name]=r.get('score') if number(r.get('score')) else None
        if r.get('schema_version')!='2.0' or r.get('protocol_id')!=protocol_id or r.get('case')!=case_id:
            error('evaluator_error','schema/protocol/case mismatch',name)
        if r.get('input_hashes')!=expected_hashes:
            error('evaluator_error','stale input hashes',name)
        status=r.get('status')
        if status not in STATUSES:
            error('evaluator_error','unknown status',name)
        elif status!='ok':
            error(status,'module reported '+status,name)
        quality=r.get('quality')
        if not isinstance(quality,dict) or type(quality.get('measurement_valid')) is not bool:
            error('evaluator_error','measurement validity flag required',name)
        elif status=='ok' and not quality['measurement_valid']:
            error('evaluator_error','ok result has invalid measurement',name)
        score=r.get('score')
        if status=='ok' and (not number(score) or not 0<=score<=1):
            error('evaluator_error','non-finite, absent or out-of-range score',name)
        if status!='ok' and score is not None:
            error('evaluator_error','failed modules must not carry numerical score',name)
        raw=r.get('raw')
        if not isinstance(raw,dict):
            error('evaluator_error','raw object required',name)
        elif name=='dimension' and 'gt_self_check_total' in raw:
            if not number(raw['gt_self_check_total']) or raw['gt_self_check_total']<=0 or raw.get('gt_self_check_passed')!=raw['gt_self_check_total']:
                error('reference_invalid','dimension reference self-check failed',name)
        timing=r.get('timing')
        if not isinstance(timing,dict) or any(not number(v) or v<0 for k,v in timing.items() if k.endswith('_s')):
            error('evaluator_error','invalid timing',name)
    validity=modules.get('validity')
    validity=validity if isinstance(validity,dict) else {}
    vq=validity.get('quality')
    vq=vq if isinstance(vq,dict) else {}
    if vq.get('reference_validated') is not True:
        reference_status=vq.get('reference_status','reference_invalid')
        if reference_status not in ('evaluator_error','reference_invalid','unsupported'):
            reference_status='evaluator_error'
        error(reference_status,'reference measurement preflight did not pass','validity')
    invalid=validity.get('status')=='candidate_invalid'
    if invalid and vq.get('candidate_invalid_confirmed') is not True:
        error('evaluator_error','candidate invalidity has no confirmed evidence','validity')
    if any(r['status']=='candidate_invalid' for r in reasons) and not invalid:
        error('evaluator_error','candidate failure unsupported by validity module')
    if not reasons and scores.get('validity')!=1.:
        error('evaluator_error','validity score must be one for normal aggregation','validity')
    precedence=('evaluator_error','reference_invalid','unsupported','candidate_missing','candidate_timeout','candidate_invalid')
    status=next((s for s in precedence if any(r['status']==s for r in reasons)),'ok')
    score=0. if status=='candidate_invalid' else None
    if status=='ok':
        score=math.fsum(w[k]*scores[k] for k in DEFAULT_WEIGHTS)
    result = {'schema_version':'2.0','protocol_id':protocol_id,'case':case_id,'status':status,
              'final_score':score,'module_scores':scores,'weights':w,'reasons':reasons}
    return refresh_assessment(result, modules, expected_hashes=expected_hashes)

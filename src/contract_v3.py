"""Validation-only admission with fixed-weight, explicitly evidenced awards.

Unavailable measurements earn no credit under the declared policy. They remain
null as measurements, and the total is labelled a lower-bound award, never a
claim that the unavailable geometry was actually measured to score zero.
"""
import math
import re

PROTOCOL_ID = 'fusion-step-adapted-v3.0'
SCHEMA_VERSION = '3.0'


def aggregate_case(case_id, modules, expected_hashes, weights=None):
    from .contract import DEFAULT_WEIGHTS, MODULE_NAMES, number
    w = DEFAULT_WEIGHTS.copy() if weights is None else weights
    if isinstance(w, dict) and 'weights' in w:
        w = w['weights']
    modules = modules if isinstance(modules, dict) else {}
    reasons, integrity = [], []
    if (not isinstance(w, dict) or set(w) != set(DEFAULT_WEIGHTS)
            or any(not number(v) or v < 0 for v in w.values())
            or not math.isclose(sum(w.values()), 1., abs_tol=1e-12)):
        integrity.append('six finite nonnegative weights summing to one required')
        w = DEFAULT_WEIGHTS.copy()
    if (not isinstance(expected_hashes, dict) or set(expected_hashes) != {'gt', 'candidate', 'annotations'}
            or any(not isinstance(h, str) or not re.fullmatch('[0-9a-f]{64}', h) for h in expected_hashes.values())):
        integrity.append('invalid expected hashes')
    invalid_identity = set()
    for name, row in modules.items():
        if name not in MODULE_NAMES:
            integrity.append('unknown module: ' + str(name))
        elif isinstance(row, dict) and (row.get('schema_version') != SCHEMA_VERSION
                or row.get('protocol_id') != PROTOCOL_ID or row.get('case') != case_id
                or row.get('input_hashes') != expected_hashes):
            integrity.append(name + ': schema/protocol/case/input identity mismatch')
            invalid_identity.add(name)
    validity = modules.get('validity')
    validity = validity if isinstance(validity, dict) else {}
    vq = validity.get('quality') if isinstance(validity.get('quality'), dict) else {}
    raw = validity.get('raw') if isinstance(validity.get('raw'), dict) else {}
    candidate = raw.get('candidate') if isinstance(raw.get('candidate'), dict) else {}
    reference = raw.get('gt') if isinstance(raw.get('gt'), dict) else {}
    invalid = validity.get('status') == 'candidate_invalid' and vq.get('candidate_invalid_confirmed') is True
    candidate_evidence = vq.get('candidate_valid') is True and candidate.get('healthy') is True
    passed = ('validity' not in invalid_identity and not invalid and candidate_evidence and (validity.get('status') == 'ok'
              and validity.get('score') == 1. and vq.get('measurement_valid') is True
              or validity.get('status') == 'reference_invalid'
              or validity.get('status') == 'evaluator_error' and raw.get('stage') == 'gt_validation'))
    participating = passed and not integrity
    metrics, awards, missing = {}, {}, []
    for name in MODULE_NAMES:
        row = modules.get(name)
        row = row if isinstance(row, dict) else {}
        quality = row.get('quality') if isinstance(row.get('quality'), dict) else {}
        value = row.get('score')
        valid = (name not in invalid_identity and row.get('status') == 'ok' and quality.get('measurement_valid') is True
                 and number(value) and 0 <= value <= 1)
        dimension_raw = row.get('raw', {})
        if name == 'dimension' and isinstance(dimension_raw, dict) and 'gt_self_check_total' in dimension_raw:
            valid = valid and (number(dimension_raw['gt_self_check_total'])
                              and dimension_raw['gt_self_check_total'] > 0
                              and dimension_raw.get('gt_self_check_passed') == dimension_raw['gt_self_check_total'])
        measured = float(value) if valid else None
        approximate = quality.get('scored_approximate') is True
        awarded = measured if measured is not None else 0.
        if name != 'validity':
            awards[name] = awarded if participating else None
            if not valid:
                missing.append(name)
                reasons.append({'module': name, 'status': 'measurement_unavailable',
                                'detail': row.get('raw', {}).get('error', row.get('status', 'missing'))
                                if isinstance(row.get('raw', {}), dict) else 'malformed measurement'})
        metrics[name] = {
            'status': row.get('status', 'missing'), 'value': measured, 'score': measured,
            'measured_score': measured, 'awarded_score': awarded if participating else None,
            'diagnostic_value': float(value) if number(value) and not valid else None,
            'score_source': ('approximate_measurement' if approximate else 'measurement') if valid else 'unavailable_policy_floor',
            'quality': ('approximate' if approximate else 'ok') if valid else 'measurement_unavailable',
            'backend': row.get('backend'), 'parameters': row.get('parameters'),
            'uncertainty': row.get('uncertainty'), 'timing': row.get('timing'),
            'unit': 'normalized_score_0_to_1', 'reason_codes': [] if valid else [row.get('status', 'missing')],
            'evidence': {'result_file': f'results/{name}.json', 'input_hashes': row.get('input_hashes'),
                         'run_manifest_sha256': row.get('run_manifest_sha256')},
        }
    coverage = math.fsum(w[n] for n in w if metrics[n]['measured_score'] is not None)
    score = math.fsum(w[n]*awards[n] for n in w) if participating else None
    # This policy range concerns missing credit only, not a certified numerical
    # uncertainty bound. Per-backend numerical uncertainty remains separate.
    upper = math.fsum([score, *(w[n] for n in missing)]) if participating else None
    interval = [score, min(1., upper)] if participating else None
    status = ('integrity_error' if integrity else 'candidate_invalid' if invalid else
              'validation_inconclusive' if not passed else
              'scored_with_unavailable_metrics' if missing else 'ok')
    reasons.extend({'status': 'integrity_error', 'module': 'integrity', 'detail': item} for item in integrity)
    if not passed:
        reasons.append({'status': status, 'module': 'validity', 'detail': 'candidate validation did not pass'})
    result = {
        'schema_version': SCHEMA_VERSION, 'protocol_id': PROTOCOL_ID, 'case': case_id,
        'status': status, 'final_score': score, 'module_scores': {n: metrics[n]['measured_score'] for n in MODULE_NAMES},
        'awarded_module_scores': awards, 'weights': w, 'reasons': reasons,
        'participates_in_scoring': participating, 'validation_passed': passed,
        'measurement_weight_coverage': coverage, 'unavailable_metrics': missing,
        'score_kind': 'policy_lower_bound' if participating and missing else 'measured' if participating else 'unscored',
        'score_interval': interval, 'score_interval_kind': 'missing_credit_range_not_numerical_confidence',
        'quality_status': 'measurement_unavailable' if missing else 'ok',
        'quality_flags': ['measurement_unavailable'] if missing else [],
        'geometry': {'candidate_status': 'valid' if passed else 'invalid' if invalid else 'unknown',
                     'reference_status': 'valid' if reference.get('healthy') is True else 'unknown',
                     'reference_preflight': {'validated': vq.get('reference_validated'),
                                             'status': vq.get('reference_status', 'unknown')}},
        'metrics': metrics, 'failure': reasons, 'ranking_eligible': False,
    }
    return refresh_assessment(result)


def refresh_assessment(result, execution=None):
    """Keep post-worker integrity decisions reflected in all score views."""
    from .contract import number
    score = result.get('final_score')
    if not number(score):
        result['participates_in_scoring'] = False
        result['score_kind'] = 'unscored'
        result['score_interval'] = None
        result['awarded_module_scores'] = {name: None for name in result.get('awarded_module_scores', {})}
        for metric in result.get('metrics', {}).values():
            metric['awarded_score'] = None
    result['scores'] = {PROTOCOL_ID: {
        'value': score, 'aggregation_status': 'scored' if number(score) else 'blocked',
        'score_kind': result.get('score_kind'),
        'blocked_reasons': result.get('reasons', []) if not number(score) else [],
        'coverage': {'scored_modules': sum(m.get('measured_score') is not None
                                         for n, m in result.get('metrics', {}).items() if n != 'validity'),
                     'required_modules': 6, 'weight': result.get('measurement_weight_coverage', 0.)},
        'rank_eligible': False,
    }}
    if execution is not None:
        result['execution'] = execution
    return result

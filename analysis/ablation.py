"""Offline fixed-cohort ablation of already validated, normalized score data.

This module imports no evaluator/geometry code and performs no input-file I/O.
Original records and score-kind labels are retained for provenance. Analysis
scores never alter admission, source scores, or official ranking eligibility.
"""
from copy import deepcopy
import math

from .statistics import average_ranks, kendall_tau_b, pearson, spearman


METRICS = ('shape', 'dimension', 'distance', 'mass', 'face_match', 'section')
_FAILURE_STATUSES = frozenset({'evaluator_error', 'worker_error', 'failed',
                               'timed_out', 'timeout', 'crashed'})


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _score(value):
    return _finite_number(value) and 0. <= value <= 1.


def _validate(dataset):
    weights = dataset['weights']
    if (not isinstance(weights, dict) or set(weights) != set(METRICS)
            or any(not _score(value) for value in weights.values())
            or not math.isclose(math.fsum(weights.values()), 1., rel_tol=0., abs_tol=1e-12)):
        raise ValueError('six finite nonnegative weights summing to one required')
    if dataset.get('source_protocol_id') == 'fusion-step-adapted-v4.0':
        if any((weights[name] <= 0 if name in ('shape','dimension','mass') else weights[name] != 0) for name in METRICS):
            raise ValueError('v4 requires three positive scored weights and zero diagnostic weights')
        if dataset.get('coordinate_mode') not in ('geometry','strict'):
            raise ValueError('v4 coordinate_mode must be geometry or strict')
    cases = set()
    required = _scored_metrics(dataset)
    for row in dataset['records']:
        case = row['case']
        if not isinstance(case, str) or not case or case in cases:
            raise ValueError('nonempty unique case ids required')
        cases.add(case)
        if not isinstance(row['participates'], bool):
            raise ValueError(f'{case}: participates must be boolean')
        for field in ('measured_scores', 'awarded_scores'):
            values = row[field]
            if (not isinstance(values, dict) or set(values) != set(METRICS)
                    or any(value is not None and not _score(value) for value in values.values())):
                raise ValueError(f'{case}: {field} must contain six finite scores or nulls')
        if not _score(row['original_weight_coverage']):
            raise ValueError(f'{case}: invalid original weight coverage')
        if row['participates']:
            if not _score(row['baseline_score']) or any(row['awarded_scores'][name] is None for name in required):
                raise ValueError(f'{case}: participating case requires a baseline and all scored awards')
        elif row['baseline_score'] is not None:
            raise ValueError(f'{case}: excluded case must not have a baseline score')


def _status(value):
    return value.get('status') if isinstance(value, dict) else value


def _execution_failure(row):
    execution = row.get('execution', {})
    code = execution.get('returncode')
    return (execution.get('timed_out') is True
            or execution.get('status') in _FAILURE_STATUSES
            or (_finite_number(code) and code != 0))


def _evaluator_failure(row):
    return (_execution_failure(row) or row.get('status') in _FAILURE_STATUSES
            or any(_status(value) in _FAILURE_STATUSES
                   for value in row.get('metric_statuses', {}).values()))


def _correlations(rows):
    observations = {name: [row['measured_scores'][name] for row in rows] for name in METRICS}
    matrices = {}
    for label, function in (('pearson', pearson), ('spearman', spearman)):
        matrix = {name: {} for name in METRICS}
        for i, left in enumerate(METRICS):
            for right in METRICS[i:]:
                stat = function(observations[left], observations[right])
                matrix[left][right] = stat
                matrix[right][left] = dict(stat)
        matrices[label] = matrix
    return matrices


def _variant(rows, weights, baseline_scores, baseline_ranks, removed=None, mode='baseline'):
    retained = [name for name in METRICS if name != removed and weights[name] > 0]
    retained_weight = math.fsum(weights[name] for name in retained)
    undefined = mode == 'renormalized' and retained_weight == 0.
    if mode == 'baseline':
        scores = baseline_scores[:]
    elif undefined:
        scores = [None] * len(rows)
    else:
        # Normalize weights before multiplication to retain precision when the
        # surviving total weight is tiny. zero_weight preserves original weights.
        scale = retained_weight if mode == 'renormalized' else 1.
        scores = [math.fsum((weights[name] / scale) * row['awarded_scores'][name]
                            for name in retained) for row in rows]
    ranks = [None] * len(rows) if undefined else average_ranks(scores)
    ranking = []
    for row, score, rank, baseline, baseline_rank in zip(rows, scores, ranks, baseline_scores, baseline_ranks):
        coverage = (math.fsum(weights[name] for name in retained
                              if row['measured_scores'][name] is not None) / retained_weight
                    if retained_weight else None)
        ranking.append({
            'case': row['case'], 'score': score, 'baseline_score': baseline,
            'delta': score - baseline if score is not None else None,
            'rank': rank, 'baseline_rank': baseline_rank,
            'rank_change': baseline_rank - rank if rank is not None else None,
            'original_weight_coverage': row['original_weight_coverage'],
            'retained_weight_coverage': coverage, 'score_kind': row['score_kind'],
        })
    ranking.sort(key=lambda row: (row['rank'] is None, row['rank'] or 0., row['case']))
    if undefined:
        comparison = {name: {'value': None, 'n': 0, 'reason': 'no_retained_weight'}
                      for name in ('spearman', 'kendall_tau_b')}
    else:
        comparison = {'spearman': spearman(baseline_scores, scores),
                      'kendall_tau_b': kendall_tau_b(baseline_scores, scores)}
    return {
        'id': 'baseline' if removed is None else f'without_{removed}_{mode}',
        'removed_metric': removed, 'mode': mode,
        'mean_score': math.fsum(scores) / len(scores) if scores and not undefined else None,
        'ranking': ranking, 'rank_comparison': comparison,
    }


def _scored_metrics(dataset):
    return tuple(name for name in METRICS if dataset['weights'][name] > 0) if dataset.get('source_protocol_id') == 'fusion-step-adapted-v4.0' else METRICS


def _cohort(rows, weights, scored_metrics=METRICS):
    baseline_scores = [row['baseline_score'] for row in rows]
    baseline_ranks = average_ranks(baseline_scores)
    variants = [_variant(rows, weights, baseline_scores, baseline_ranks)]
    for metric in scored_metrics:
        for mode in ('renormalized', 'zero_weight'):
            variants.append(_variant(rows, weights, baseline_scores, baseline_ranks, metric, mode))
    return {'case_count': len(rows), 'case_ids': [row['case'] for row in rows],
            'variants': variants, 'correlations': _correlations(rows)}


def analyze_dataset(dataset):
    """Return baseline and paired removals in two fixed, versioned cohorts.

    Unavailable-metric counts concern participating cases; excluded cases remain
    in records and in the exclusion count. Evaluator-failure rate uses all source
    cases as denominator and counts each case once. Candidate/reference invalid
    and mere measurement unavailability do not by themselves imply evaluator
    failure; explicit execution or metric error evidence does.

    Coverage retains the source coverage and also reports measured surviving
    weight divided by all surviving weight, for either ablation mode. If all
    surviving weights are zero, this coverage is null; renormalized scores/ranks
    are also null, while zero_weight scores are zero.
    """
    _validate(dataset)
    records = dataset['records']
    weights = dataset['weights']
    scored_metrics = _scored_metrics(dataset)
    participants = [row for row in records if row['participates']]
    complete = [row for row in participants
                if all(row['measured_scores'][name] is not None for name in scored_metrics)]
    failures = sum(_evaluator_failure(row) for row in records)
    counts = {
        'total': len(records), 'participating': len(participants),
        'complete': len(complete), 'excluded': len(records) - len(participants),
        'unavailable_metric_counts': {
            name: sum(row['measured_scores'][name] is None for row in participants) for name in METRICS},
        'evaluator_failure_cases': failures,
        'evaluator_failure_rate': failures / len(records) if records else None,
    }
    return {
        'source_id': dataset['source_id'], 'system': dataset.get('system'),
        'source_protocol_id': dataset['source_protocol_id'], 'weights': dict(weights),
        'coordinate_mode': dataset.get('coordinate_mode'), 'scored_metrics': list(scored_metrics),
        'warnings': list(dataset.get('warnings', [])), 'counts': counts,
        'records': deepcopy(records),
        'cohorts': {
            'complete_measurements': _cohort(complete, weights, scored_metrics),
            'policy_awards': _cohort(participants, weights, scored_metrics),
        },
    }

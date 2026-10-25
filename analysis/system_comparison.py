"""Exploratory system comparisons on explicitly identified, matched inputs."""
import math

from .statistics import average_ranks, kendall_tau_b, spearman


def compare_systems(datasets, analyses):
    selected = [source for source in datasets if source.get('system') is not None]
    result = {'status': 'not_requested', 'ranking_eligible': False, 'reasons': [], 'cohorts': {}}
    if not selected:
        return result
    result['status'] = 'not_applicable'
    if len(selected) < 2:
        result['reasons'] = ['at_least_two_explicit_systems_required']
        return result
    labels = [source['system'] for source in selected]
    if len(set(labels)) != len(labels):
        result['reasons'].append('duplicate_system_identity')
    base = selected[0]
    base_rows = {row['case']: row for row in base['records']}
    for source in selected:
        if source['source_protocol_id'] != base['source_protocol_id']:
            result['reasons'].append('different_protocols')
        if source.get('coordinate_mode') != base.get('coordinate_mode'):
            result['reasons'].append('different_coordinate_modes')
        if source['weights'] != base['weights']:
            result['reasons'].append('different_weights')
        rows = {row['case']: row for row in source['records']}
        if rows.keys() != base_rows.keys():
            result['reasons'].append('different_case_sets')
            continue
        for case, row in rows.items():
            for role in ('gt', 'annotations'):
                digest = row.get('input_hashes', {}).get(role)
                if not digest or digest != base_rows[case].get('input_hashes', {}).get(role):
                    result['reasons'].append('missing_or_different_' + role + '_hash')
    if result['reasons']:
        result['reasons'] = sorted(set(result['reasons']))
        return result
    by_source = {item['source_id']: item for item in analyses}
    result.update(status='available', source_protocol_id=base['source_protocol_id'], systems=labels,
                  weights=base['weights'], declared_identity=True)
    for cohort_name in ('complete_measurements', 'policy_awards'):
        cohorts = [by_source[source['source_id']]['cohorts'][cohort_name] for source in selected]
        case_sets = [set(cohort['case_ids']) for cohort in cohorts]
        if cohort_name == 'policy_awards' and any(cases != case_sets[0] for cases in case_sets):
            result['cohorts'][cohort_name] = {'status': 'not_applicable',
                'reason': 'different_participating_case_sets', 'variants': []}
            continue
        common = sorted(set.intersection(*case_sets))
        if not common:
            result['cohorts'][cohort_name] = {'status': 'not_applicable', 'reason': 'no_common_cases', 'variants': []}
            continue
        variants = []
        base_scores = None
        base_ranks = None
        for template in cohorts[0]['variants']:
            scores = []
            for cohort in cohorts:
                variant = next(item for item in cohort['variants'] if item['id'] == template['id'])
                by_case = {row['case']: row['score'] for row in variant['ranking']}
                values = [by_case[case] for case in common]
                scores.append(math.fsum(values) / len(common) if all(v is not None for v in values) else None)
            ranks = average_ranks(scores) if all(v is not None for v in scores) else [None] * len(scores)
            if template['id'] == 'baseline':
                base_scores, base_ranks = scores, ranks
            variants.append({'id': template['id'], 'mode': template['mode'],
                'removed_metric': template['removed_metric'],
                'ranking': [{'system': label, 'score': score, 'rank': rank,
                    'baseline_score': base_scores[index], 'baseline_rank': base_ranks[index],
                    'delta': score - base_scores[index] if score is not None else None,
                    'rank_change': base_ranks[index] - rank if rank is not None else None}
                    for index, (label, score, rank) in enumerate(zip(labels, scores, ranks))],
                'rank_comparison': {'spearman': spearman(base_scores, scores),
                                    'kendall_tau_b': kendall_tau_b(base_scores, scores)}})
        result['cohorts'][cohort_name] = {'status': 'available', 'case_ids': common,
            'case_count': len(common), 'variants': variants}
    return result

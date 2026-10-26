"""Small complete v4 runs for report tests; no CAD runtime required."""
import hashlib
import json
import math
from pathlib import Path

from src.contract_v4 import DEFAULT_WEIGHTS, PROTOCOL_ID, aggregate_case, refresh_assessment


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_run(root, *, mode='geometry', kinds=('lower', 'complete', 'invalid', 'timeout')):
    root = Path(root)
    root.mkdir(exist_ok=True)
    manifest = {'schema_version': '4.0', 'protocol_id': PROTOCOL_ID,
                'coordinate_mode': mode, 'weights': dict(DEFAULT_WEIGHTS), 'cases': [],
                'ranking_eligible': False, 'code_hashes': {'worker_v4.py': 'a' * 64}}
    for case in kinds:
        folder = root / case
        for name in ('inputs', 'results', 'derived'):
            (folder / name).mkdir(parents=True)
        for name in ('gt.step', 'candidate.step', 'annotations.json'):
            (folder / 'inputs' / name).write_bytes((case + name).encode())
        (folder / 'derived' / 'candidate_aligned.step').write_bytes((case + 'aligned').encode())
        hashes = {role: sha(folder / 'inputs' / (role + '.step' if role != 'annotations' else 'annotations.json'))
                  for role in ('gt', 'candidate', 'annotations')}
        manifest['cases'].append({'case': case, 'expected_hashes': hashes,
                                  'gt': 'C:/private/reference.step', 'candidate': 'C:/private/candidate.step',
                                  'annotations': 'C:/private/annotations.json'})
    write_json(root / 'manifest.json', manifest)
    digest = sha(root / 'manifest.json')
    rows = []
    for record in manifest['cases']:
        case, hashes = record['case'], record['expected_hashes']
        folder = root / case
        aligned = sha(folder / 'derived' / 'candidate_aligned.step')
        modules = {}
        if case != 'timeout':
            for name in ('validity', 'pose', 'shape', 'dimension', 'mass', 'distance', 'face_match', 'section', 'world_shape'):
                evaluated = dict(hashes)
                if mode == 'geometry' and name in ('shape', 'dimension', 'distance', 'face_match', 'section'):
                    evaluated['candidate'] = aligned
                row = {'schema_version': '4.0', 'protocol_id': PROTOCOL_ID, 'case': case,
                       'coordinate_mode': mode, 'angle': name, 'input_hashes': hashes,
                       'evaluation_input_hashes': evaluated, 'run_manifest_sha256': digest,
                       'status': 'ok', 'score': 0.5 if name == 'dimension' else 1.,
                       'quality': {'measurement_valid': True}, 'raw': {}, 'backend': 'fixture',
                       'parameters': {}, 'uncertainty': {}, 'timing': {'wall_s': 0.01}}
                if name == 'validity':
                    row['quality']['candidate_valid'] = case != 'invalid'
                    row['raw'] = {'candidate': {'healthy': case != 'invalid'}, 'gt': {'healthy': True}}
                    if case == 'invalid':
                        row.update(status='candidate_invalid', score=0.)
                        row['quality'].update(candidate_invalid_confirmed=True, measurement_valid=False)
                if name == 'pose':
                    row['raw'] = {'aligned_candidate_sha256': aligned,
                                  'evaluation_candidate_sha256': aligned if mode == 'geometry' else hashes['candidate'],
                                  'rotation_angle_deg': 15., 'translation_mm': [1., 2., 3.]}
                if name == 'shape':
                    row['raw'] = {'volumetric_iou': 1., 'surface_f1': 1., 'topology': 1.,
                                  'private_annotations': {'secret': 'DO NOT EXPORT'}}
                if name == 'dimension':
                    row['raw'] = {'gt_self_check_total': 2, 'gt_self_check_passed': 2, 'passed': 1, 'total': 2,
                                  'items': [{'id': 'plane-z', 'type': 'plane_pair', 'expected': 10., 'measured': 12.,
                                             'difference_mm': 2., 'pass': False, 'failure_type': 'outside_tolerance'}]}
                if name == 'mass' and case == 'lower':
                    row.update(status='diagnostic_unreliable', score=None)
                    row['quality']['measurement_valid'] = False
                    row['raw'] = {'error': 'failed to converge at C:/private/secret.step </script><script>x</script>'}
                modules[name] = row
                write_json(folder / 'results' / (name + '.json'), row)
        aggregate = aggregate_case(case, modules, hashes, coordinate_mode=mode)
        aggregate.update(run_manifest_sha256=digest, annotation_kind='gt_derived_probe')
        execution = {'status': 'timed_out' if case == 'timeout' else 'completed',
                     'timed_out': case == 'timeout', 'returncode': None if case == 'timeout' else 0, 'wall_s': 1.}
        if case == 'timeout':
            aggregate['status'] = 'evaluator_error'
        refresh_assessment(aggregate, execution)
        write_json(folder / 'aggregate.json', aggregate)
        rows.append(aggregate)
    participating = [r for r in rows if r['participates_in_scoring']]
    summary = {'schema_version': '4.0', 'protocol_id': PROTOCOL_ID, 'coordinate_mode': mode,
               'run_manifest_sha256': digest, 'cases': rows, 'code_unchanged': True,
               'original_inputs_unchanged': True, 'ranking_eligible': False,
               'total_case_count': len(rows), 'participating_case_count': len(participating),
               'batch_mean': math.fsum(r['final_score'] for r in participating) / len(participating) if participating else None}
    write_json(root / 'summary.json', summary)
    return summary


def revise_case(root, summary, case, callback):
    row = next(r for r in summary['cases'] if r['case'] == case)
    callback(row)
    write_json(Path(root) / case / 'aggregate.json', row)
    write_json(Path(root) / 'summary.json', summary)

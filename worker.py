"""One isolated process per case; share validated CADGenBench artifacts in-process."""
from pathlib import Path
import argparse
import os
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parent))
from src.io_utils import sha, strict_json, utc_now
from src.contract import atomic_write_json, PROTOCOL_ID, MODULE_NAMES


def run_v3(folder, job, gt, candidate, ann, artifacts, timed):
    from src import metrics, robust_metrics as robust
    protocol = 'fusion-step-adapted-v3.0'

    def persist(name, data):
        data.update(schema_version='3.0', protocol_id=protocol, case=job['case'],
                    angle=name, input_hashes=job['input_hashes'], run_manifest_sha256=job['run_manifest_sha256'])
        atomic_write_json(folder/'results'/f'{name}.json', data)
        print(job['case'], name, data['status'], data.get('score'), flush=True)

    # Publish validation before expensive measurements so a later backend crash
    # cannot erase the independently completed geometry decision.
    validity = timed('validity', lambda: robust.validity(gt, candidate, artifacts))
    persist('validity', validity)
    q = validity.get('quality', {})
    admitted = (q.get('candidate_valid') is True
                and validity.get('raw', {}).get('candidate', {}).get('healthy') is True)
    preflight = {'case': job['case'], 'geometry': validity.get('raw', {}).get('gt'),
                 'input_hashes': job['input_hashes'], 'run_manifest_sha256': job['run_manifest_sha256'],
                 'reference_validated': validity.get('raw', {}).get('gt', {}).get('healthy') is True,
                 'qualification_policy': 'metric qualification does not override candidate validation'}
    if admitted:
        try:
            plan = robust.build_section_plan(gt, ann)
            atomic_write_json(folder/'section_plan.json', plan)
            ann = {**ann, 'v3_section_plan': plan}
            preflight['section_plan_sha256'] = sha(folder/'section_plan.json')
        except Exception as exc:
            preflight['section_plan_error'] = str(exc)
    atomic_write_json(folder/'reference_preflight.json', preflight)
    functions = {
        'dimension': lambda: metrics.dimension(gt, candidate, ann),
        'mass': lambda: robust.mass(gt, candidate),
        'face_match': lambda: robust.face_match(gt, candidate, ann),
        'section': lambda: robust.section(gt, candidate, ann),
        'shape': lambda: metrics.shape(gt, candidate, artifacts, folder/'diagnostics'),
        'distance': lambda: metrics.distance(gt, candidate),
    }
    for name, fn in functions.items():
        if admitted:
            data = timed(name, fn)
        else:
            data = timed(name, lambda: metrics.result('candidate_invalid' if q.get('candidate_invalid_confirmed')
                         else 'evaluator_error', raw={'reason': 'candidate validation did not pass'}))
        persist(name, data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--case-dir', type=Path, required=True)
    args = ap.parse_args()
    folder = args.case_dir.resolve()
    job = strict_json(folder / 'job.json')
    # Never inherit unverified file-stat keyed external caches.
    os.environ.pop('CADGENBENCH_MESH_CACHE_DIR', None)
    start = time.perf_counter()
    t = time.perf_counter()
    from src import metrics, section, face_match
    imports_s = time.perf_counter() - t
    gt, candidate = folder/'inputs/gt.step', folder/'inputs/candidate.step'
    ann_path = folder/'inputs/annotations.json'
    paths = {'gt': gt, 'candidate': candidate, 'annotations': ann_path}
    if {k: sha(v) for k, v in paths.items()} != job['input_hashes']:
        raise ValueError('staged input hash mismatch')
    ann = strict_json(ann_path)
    if ann.get('gt_sha256') != job['input_hashes']['gt']:
        raise ValueError('annotation GT hash mismatch')
    artifacts = {'gt': metrics.StepArtifacts(gt), 'candidate': metrics.StepArtifacts(candidate)}
    reference_artifacts = {'gt': artifacts['gt'], 'candidate': artifacts['gt']}

    def timed(name, fn):
        atomic_write_json(folder/'events'/f'{name}_started.json', {'stage': name, 'started_at': utc_now()})
        st = time.perf_counter()
        try:
            data = fn()
        except Exception as exc:
            data = metrics.result('evaluator_error', raw={'error': str(exc), 'traceback': traceback.format_exc()})
        data['timing'] = {'compute_s': time.perf_counter()-st}
        return data

    if job.get('protocol_id') == 'fusion-step-adapted-v3.0':
        run_v3(folder, job, gt, candidate, ann, artifacts, timed)
        after = {k: sha(v) for k, v in paths.items()}
        atomic_write_json(folder/'worker_complete.json', {'status': 'ok' if after == job['input_hashes'] else 'evaluator_error',
            'input_hashes_after': after, 'imports_s': imports_s, 'wall_s': time.perf_counter()-start, 'ended_at': utc_now(),
            'cache_policy': 'per-case process-local artifacts'})
        return
    preflight = {}
    for name, fn in {
        'validity': lambda: metrics.validity(gt, gt, reference_artifacts),
        'dimension': lambda: metrics.dimension(gt, gt, ann),
        'mass': lambda: metrics.mass(gt, gt),
        'face_match': lambda: face_match.evaluate(gt, gt, ann),
        'section': lambda: section.evaluate(gt, gt, ann)}.items():
        preflight[name] = timed('reference_'+name, fn)
    reference_status = next((s for s in ('evaluator_error','reference_invalid','unsupported')
                             if any(r['status']==s for r in preflight.values())), 'ok')
    reference_valid = all(r['status']=='ok' for r in preflight.values())
    if not reference_valid and reference_status=='ok': reference_status='evaluator_error'
    preflight.update(case=job['case'], reference_validated=reference_valid,
                     status=reference_status,
                     input_hashes=job['input_hashes'], run_manifest_sha256=job['run_manifest_sha256'])
    atomic_write_json(folder/'reference_preflight.json', preflight)
    functions = {
        'validity': lambda: metrics.validity(gt, candidate, artifacts),
        'dimension': lambda: metrics.dimension(gt, candidate, ann),
        'mass': lambda: metrics.mass(gt, candidate),
        'face_match': lambda: face_match.evaluate(gt, candidate, ann),
        'section': lambda: section.evaluate(gt, candidate, ann),
        'shape': lambda: metrics.shape(gt, candidate, artifacts, folder/'diagnostics'),
        'distance': lambda: metrics.distance(gt, candidate)}
    candidate_invalid = False
    for name in ('validity', 'dimension', 'mass', 'face_match', 'section', 'shape', 'distance'):
        if candidate_invalid:
            data = timed(name, lambda: metrics.result('candidate_invalid', raw={'reason': 'candidate validity gate failed'}))
        else:
            data = timed(name, functions[name])
        if name == 'validity':
            candidate_invalid = data['status'] == 'candidate_invalid'
            data.setdefault('quality', {}).update(reference_validated=reference_valid,
                                                  reference_status=reference_status,
                                                  candidate_invalid_confirmed=candidate_invalid)
            if not reference_valid:
                data['raw']['reference_preflight_failed'] = True
        data.setdefault('quality', {}).setdefault('measurement_valid', data['status'] == 'ok')
        data.update(schema_version='2.0', protocol_id=PROTOCOL_ID, case=job['case'],
                    angle=name, input_hashes=job['input_hashes'], run_manifest_sha256=job['run_manifest_sha256'])
        atomic_write_json(folder/'results'/f'{name}.json', data)
        print(job['case'], name, data['status'], data.get('score'), flush=True)
    after = {k: sha(v) for k, v in paths.items()}
    atomic_write_json(folder/'worker_complete.json', {'status': 'ok' if after == job['input_hashes'] else 'evaluator_error',
        'input_hashes_after': after, 'imports_s': imports_s, 'wall_s': time.perf_counter()-start, 'ended_at': utc_now(),
        'cache_policy': 'per-case process-local artifacts; no persistent or candidate-supplied mesh cache'})


if __name__ == '__main__':
    main()

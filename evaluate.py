"""CLI for immutable, versioned shadow scoring. One isolated process per case."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from src.io_utils import sha, strict_json, tree_hashes, utc_now


def validate_cases(document):
    cases = document.get('cases')
    if not isinstance(cases, list) or not cases:
        raise ValueError('cases must be a nonempty list')
    seen = set()
    for row in cases:
        case = row.get('case')
        if not isinstance(case, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', case):
            raise ValueError(f'invalid case identifier: {case!r}')
        if case in seen:
            raise ValueError(f'duplicate case: {case}')
        seen.add(case)
        expected = row.get('expected_hashes')
        if not isinstance(expected, dict) or set(expected) != {'candidate','gt','annotations'}:
            raise ValueError(f'{case}: missing input hashes')
        for role in ('candidate','gt','annotations'):
            digest = expected[role]
            if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
                raise ValueError(f'{case}: invalid {role} hash')
            path = Path(row.get(role, ''))
            if not path.is_absolute() or not path.is_file():
                raise ValueError(f'{case}: missing absolute {role} file')
            if sha(path) != digest:
                raise ValueError(f'{case}: stale {role} hash')
    return cases


def code_inventory():
    out = {p.name: sha(p) for p in sorted(BASE.glob('*.py'))}
    for name in ('src','vendor'):
        out.update({f'{name}/{k}':v for k,v in tree_hashes(BASE/name).items()})
    return out


def dependency_versions():
    versions = {'python': sys.version, 'executable': sys.executable, 'platform': sys.platform}
    for name in ('cadquery-ocp','numpy','scipy','build123d','trimesh','manifold3d'):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = 'not installed'
    return versions


def terminate_tree(proc):
    if os.name == 'nt':
        subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'], capture_output=True, check=False)
    else:
        import signal
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(timeout=10)


def run_case(row, output, manifest_hash, weights, timeout, protocol_id='fusion-step-adapted-v2.0'):
    from src.contract import atomic_write_json, aggregate_case, refresh_assessment, MODULE_NAMES, PROTOCOL_ID
    folder = output/row['case']
    folder.mkdir()
    for name in ('inputs','results','events','diagnostics'):
        (folder/name).mkdir()
    for role in ('gt','candidate','annotations'):
        dest = folder/'inputs'/('annotations.json' if role == 'annotations' else role+'.step')
        shutil.copyfile(row[role], dest)
        if sha(dest) != row['expected_hashes'][role]:
            raise ValueError('input changed during staging')
    job = {'case': row['case'], 'input_hashes': row['expected_hashes'], 'run_manifest_sha256': manifest_hash,
           'protocol_id': protocol_id}
    atomic_write_json(folder/'job.json', job)
    started = time.perf_counter()
    with (folder/'worker.log').open('w', encoding='utf-8') as log:
        options = {'start_new_session': True} if os.name != 'nt' else {'creationflags': subprocess.CREATE_NO_WINDOW}
        proc = subprocess.Popen([sys.executable, '-B', str(BASE/'worker.py'), '--case-dir', str(folder)],
                                stdout=log, stderr=subprocess.STDOUT, cwd=BASE, **options)
        try:
            code = proc.wait(timeout=timeout)
            execution = {'returncode': code, 'timed_out': False}
        except subprocess.TimeoutExpired:
            terminate_tree(proc)
            execution = {'returncode': proc.returncode, 'timed_out': True}
    execution['wall_s'] = time.perf_counter()-started
    modules = {}
    read_errors = []
    for name in MODULE_NAMES:
        path = folder/'results'/f'{name}.json'
        if not path.exists():
            continue
        try:
            data = strict_json(path)
            if data.get('run_manifest_sha256') != manifest_hash:
                raise ValueError('stale run manifest hash')
            modules[name] = data
        except Exception as exc:
            read_errors.append({'module': name, 'status': 'evaluator_error', 'detail': str(exc)})
    aggregate = aggregate_case(row['case'], modules, row['expected_hashes'], weights, protocol_id=protocol_id)
    try:
        staged_unchanged = all(sha(folder/'inputs'/('annotations.json' if role == 'annotations' else role+'.step'))
                               == row['expected_hashes'][role] for role in ('gt', 'candidate', 'annotations'))
    except (OSError, ValueError):
        staged_unchanged = False
    if not staged_unchanged:
        aggregate.update(status='integrity_error' if protocol_id == 'fusion-step-adapted-v3.0' else 'evaluator_error', final_score=None)
        aggregate['reasons'].append({'status': 'integrity_error', 'module': 'staged_inputs',
                                     'detail': 'staged input changed or unavailable after worker exit'})
    if execution['returncode'] != 0 or read_errors or not (folder/'worker_complete.json').exists():
        # A v3 case with a completed validation still participates when a later
        # measurement crashes. Stale evidence remains an integrity failure.
        if protocol_id != 'fusion-step-adapted-v3.0' or not aggregate.get('participates_in_scoring') or read_errors:
            aggregate.update(status='evaluator_error', final_score=None)
        aggregate['reasons'].extend(read_errors)
        aggregate['reasons'].append({'status': 'evaluator_error', 'detail': 'worker timeout/crash/incomplete', **execution})
    else:
        complete = strict_json(folder/'worker_complete.json')
        if complete['status'] != 'ok' or complete['input_hashes_after'] != row['expected_hashes']:
            aggregate.update(status='evaluator_error', final_score=None)
            aggregate['reasons'].append({'status': 'evaluator_error', 'detail': 'input changed during evaluation'})
    execution['status'] = ('timed_out' if execution['timed_out'] else
                           'completed' if execution['returncode'] == 0 and (folder/'worker_complete.json').exists()
                           else 'failed')
    aggregate.update(run_manifest_sha256=manifest_hash,
                     annotation_kind='gt_derived_probe', ranking_eligible=False)
    refresh_assessment(aggregate, modules, execution, expected_hashes=row['expected_hashes'])
    atomic_write_json(folder/'provisional.json', {**aggregate, 'lifecycle':'provisional',
                                                'requires_batch_integrity_confirmation':True})
    print(row['case'], aggregate['status'], aggregate['final_score'], f"{execution['wall_s']:.2f}s", flush=True)
    return aggregate


def legacy_main():
    from src.contract import atomic_write_json, refresh_assessment, failed_case_result, PROTOCOL_ID
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--cases', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True, help='new directory; overwrite/resume deliberately refused')
    ap.add_argument('--weights', type=Path)
    ap.add_argument('--protocol', choices=[PROTOCOL_ID, 'fusion-step-adapted-v3.0'], default='fusion-step-adapted-v3.0')
    ap.add_argument('--timeout', type=float, default=600, help='hard per-case process-tree limit in seconds')
    ap.add_argument('--workers', type=int, choices=range(1,5), default=1)
    args = ap.parse_args()
    protocol = args.protocol
    is_v3 = protocol == 'fusion-step-adapted-v3.0'
    schema = '3.0' if is_v3 else '2.0'
    args.weights = args.weights or BASE/('weights_v3.json' if is_v3 else 'weights.json')
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        ap.error('timeout must be positive and finite')
    if args.output.exists():
        ap.error('output already exists; refusing overwrite')
    doc = strict_json(args.cases)
    rows = validate_cases(doc)
    weights = strict_json(args.weights)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    code = code_inventory()
    manifest = {'schema_version': schema, 'protocol_id': protocol, 'started_at': utc_now(),
        'cases': rows, 'cases_sha256': sha(args.cases), 'weights': weights,
        'weights_sha256': sha(args.weights), 'code_hashes': code, 'dependencies': dependency_versions(),
        'annotation_kind': 'gt_derived_probe', 'annotation_strategy': 'reference-frozen bilateral section plan; same definition for all candidates' if is_v3 else 'unchanged frozen v1 probes; no fallback plane selection',
        'coordinate_frame': 'fixed_world_mm', 'timeout_per_case_s': args.timeout, 'workers': args.workers,
        'boolean_diagnostic_policy': 'legacy Boolean results are diagnostic_unreliable and never gate equivalence',
        'ranking_eligible': False, 'ranking_reason': 'adapted GT probes and uncalibrated metrics; no independent drawing annotations'}
    atomic_write_json(output/'manifest.json', manifest)
    digest = sha(output/'manifest.json')
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = {pool.submit(run_case, row, output, digest, weights, args.timeout, protocol): row for row in rows}
        for future in as_completed(jobs):
            try:
                results.append(future.result())
            except Exception as exc:
                row = jobs[future]
                failed = failed_case_result(row['case'], exc, protocol_id=protocol, weights=weights.get('weights', weights))
                failed.update(run_manifest_sha256=digest, annotation_kind='gt_derived_probe', ranking_eligible=False)
                (output/row['case']).mkdir(parents=True, exist_ok=True)
                results.append(failed)
    results.sort(key=lambda r:r['case'])
    try:
        unchanged = code == code_inventory()
        original_inputs_unchanged = all(sha(row[k]) == row['expected_hashes'][k] for row in rows for k in ('candidate','gt','annotations'))
    except (OSError, ValueError):
        unchanged = False
        original_inputs_unchanged = False
    if not unchanged or not original_inputs_unchanged:
        for r in results:
            r.update(status='evaluator_error', final_score=None)
            r['reasons'].append({'detail': 'code or original input changed during run'})
            refresh_assessment(r)
    # Only publish final per-case results after the same integrity gate used by
    # the summary. Interrupted/failed batches retain provisional records only.
    for r in results:
        (output/r['case']).mkdir(parents=True, exist_ok=True)
        atomic_write_json(output/r['case']/'aggregate.json',r)
    eligible = all(r['final_score'] is not None for r in results)
    participants = [r for r in results if r.get('participates_in_scoring') and r['final_score'] is not None] if is_v3 else results if eligible else []
    all_admitted_scored = all(not r.get('validation_passed') or r.get('participates_in_scoring') for r in results)
    summary = {'schema_version':schema, 'protocol_id': protocol, 'run_manifest_sha256': digest,
        'cases': results, 'code_unchanged': unchanged, 'original_inputs_unchanged': original_inputs_unchanged,
        'all_cases_aggregatable': eligible, 'ranking_eligible': False,
        'batch_mean': math.fsum(r['final_score'] for r in participants)/len(participants) if participants else None,
        'participating_case_count': len(participants), 'total_case_count': len(results),
        'all_validation_passed_cases_scored': all_admitted_scored if is_v3 else eligible,
        'batch_mean_scope': 'all validation-passed candidates, including policy lower-bound awards' if is_v3 else 'all required cases',
        'wall_s': time.perf_counter()-started, 'ended_at': utc_now()}
    atomic_write_json(output/'summary.json', summary)
    from src.reporting import report
    report(output, manifest, summary)
    print('Output:', output, 'wall_s:', summary['wall_s'], flush=True)
    return 0 if eligible or (is_v3 and participants and all_admitted_scored and all(r['status'] in
                        ('ok', 'scored_with_unavailable_metrics', 'candidate_invalid') for r in results)) else 2


def main():
    # Route explicitly: all legacy parsing, formulas and execution stay intact.
    selector = argparse.ArgumentParser(add_help=False)
    selector.add_argument('--protocol', default='fusion-step-adapted-v4.0')
    selected, _ = selector.parse_known_args()
    if selected.protocol == 'fusion-step-adapted-v4.0':
        from evaluate_v4 import main as main_v4
        return main_v4()
    return legacy_main()


if __name__ == '__main__':
    raise SystemExit(main())

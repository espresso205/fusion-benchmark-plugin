"""Offline frozen capability experiments and denominator-preserving analysis.

Only the Python standard library and static profile/capability resolvers are
used. This module never probes a CLI, launches a model, or connects to CAD/MCP.
"""
import copy
import csv
import hashlib
import itertools
import json
import math
from pathlib import Path
import random
import re

from . import capabilities
from .candidate_profiles import (LAUNCH_FIELDS, apply_candidate_selection,
                                 freeze_candidate_profile, resolve_candidate_profile)

PLAN_SCHEMA = 'candidate-capability-experiment/1'
RUN_SCHEMA = 'candidate-capability-run/1'
_GROUP_ID = re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,47}$')
COMMON_DIGEST_FIELDS = ('dataset_sha256', 'input_manifest_sha256', 'gt_annotations_sha256',
                        'common_protocol_sha256', 'plugin_source_sha256', 'evaluator_weights_sha256')
COMMON_IDENTITY_FIELDS = COMMON_DIGEST_FIELDS + ('budget_policy', 'cad_identity', 'coordinate_mode')


def _common_identity_validation(common):
    """Separate draft identity claims from a complete comparable identity."""
    missing = [key for key in COMMON_IDENTITY_FIELDS if key not in common]
    invalid = []
    for key in COMMON_DIGEST_FIELDS:
        if key in common and (not isinstance(common[key], str) or not re.fullmatch('[a-f0-9]{64}', common[key])):
            invalid.append(key)
    if 'budget_policy' in common:
        budget = common['budget_policy']
        if (not isinstance(budget, dict) or not _number(budget.get('seconds')) or budget['seconds'] <= 0 or
                type(budget.get('requests')) is not int or budget['requests'] <= 0):
            invalid.append('budget_policy')
    if 'cad_identity' in common:
        cad = common['cad_identity']
        if (not isinstance(cad, dict) or cad.get('backend') not in ('fusion', 'solidworks') or
                not isinstance(cad.get('version'), str) or not cad['version'].strip()):
            invalid.append('cad_identity')
    if 'coordinate_mode' in common and common['coordinate_mode'] not in ('geometry', 'strict'):
        invalid.append('coordinate_mode')
    return {'status': 'incomplete' if missing or invalid else 'complete',
            'required_fields': list(COMMON_IDENTITY_FIELDS), 'missing_fields': missing, 'invalid_fields': invalid}


def _plan_common_validation(plan):
    validation = _common_identity_validation(plan['common'])
    validation['binding_status'] = plan.get('common_identity_capture', {}).get('status', 'not_captured')
    if validation['binding_status'] != 'captured':
        validation['status'] = 'incomplete'
        validation['invalid_fields'].append('actual_source_input_binding')
    return validation


def _admission_validation(plan):
    """Validate retained maintainer evidence; never infer drawing/GT quality."""
    admission = plan.get('admission')
    if not isinstance(admission, dict) or admission.get('status') != 'passed' or not isinstance(admission.get('cases'), dict):
        return {'status': 'not_verified', 'reasons': ['maintainer_admission_not_passed']}
    identities = {row['case_id']: row for row in plan['common'].get('case_identities', [])}
    reasons = []
    for case_id in plan['case_ids']:
        row, identity = admission['cases'].get(case_id), identities.get(case_id)
        if not isinstance(row, dict) or not identity:
            reasons.append(case_id + ':missing_case_admission_or_captured_identity')
            continue
        for key in ('input_manifest_sha256', 'gt_sha256', 'annotations_sha256'):
            if row.get(key) != identity[key]:
                reasons.append(case_id + ':stale_' + key)
        for check in ('drawing_readability', 'gt_primary_measurability'):
            evidence = row.get(check)
            if (not isinstance(evidence, dict) or evidence.get('status') != 'passed' or
                    not isinstance(evidence.get('evidence_sha256'), str) or
                    not re.fullmatch('[a-f0-9]{64}', evidence['evidence_sha256'])):
                reasons.append(case_id + ':' + check + '_not_verified')
    return {'status': 'not_verified' if reasons else 'passed', 'reasons': reasons}


def capture_common_identity(config, case_ids):
    """Bind actual installed source, ordered public inputs and private references.

    Read-only and process-free. CAD version is a declared identity; the runner
    compares its separately observed CAD identity before candidate dispatch.
    """
    from .input_assets import load_manifest, legacy_manifest
    from .candidate_prompt import common_protocol_sha256
    from .runner import BASE, source_hashes, scoring_options, verify_package
    case_ids = _identifiers(case_ids, 'case_ids')
    if not config.get('questions_root') or not config.get('private_cases'):
        raise ValueError('study_identity_paths_required')
    public_root = Path(config['questions_root'])
    private = _read(config['private_cases'])
    registry_rows = private.get('cases') if isinstance(private, dict) else None
    if (not isinstance(registry_rows, list) or any(not isinstance(row, dict) or not isinstance(row.get('case'), str)
                                                for row in registry_rows)):
        raise ValueError('study_private_registry_invalid')
    references = {row['case']: row for row in registry_rows}
    if len(references) != len(registry_rows) or any(case_id not in references for case_id in case_ids):
        raise ValueError('study_private_registry_invalid')
    cases, inputs, reference_hashes = [], [], []
    for case_id in case_ids:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', case_id):
            raise ValueError('study_case_id_invalid')
        root = public_root / case_id
        manifest = load_manifest(root / 'inputs.json', case_id) if (root / 'inputs.json').exists() else legacy_manifest(case_id, root)
        row, hashes = references[case_id], {}
        for role in ('gt', 'annotations'):
            _, raw = capabilities._read_file(row[role])
            hashes[role] = hashlib.sha256(raw).hexdigest()
            if hashes[role] != row.get('expected_hashes', {}).get(role):
                raise ValueError('study_private_' + role + '_hash_mismatch: ' + case_id)
        if _read(row['annotations']).get('gt_sha256') != hashes['gt']:
            raise ValueError('study_annotation_gt_binding_mismatch: ' + case_id)
        acceptance_identity = {}
        if config.get('protocol') == 'fusion-step-adapted-v5.0':
            hashes['reference_acceptance'] = None
            acceptance_identity['reference_acceptance_sha256'] = None
        if config.get('protocol') == 'fusion-step-adapted-v5.0' and (row.get('reference_acceptance') or row.get('reference_acceptance_sha256')):
            if not row.get('reference_acceptance') or not row.get('reference_acceptance_sha256'):
                raise ValueError('study_reference_acceptance_hash_required: ' + case_id)
            _, raw = capabilities._read_file(row['reference_acceptance'])
            digest = hashlib.sha256(raw).hexdigest()
            if digest != row['reference_acceptance_sha256']:
                raise ValueError('study_reference_acceptance_hash_mismatch: ' + case_id)
            acceptance = json.loads(raw)
            if (acceptance.get('gt_sha256') != hashes['gt'] or
                    acceptance.get('annotations_sha256') != hashes['annotations']):
                raise ValueError('study_reference_acceptance_input_binding_mismatch: ' + case_id)
            hashes['reference_acceptance'] = digest
            acceptance_identity['reference_acceptance_sha256'] = digest
        inputs.append({'case_id': case_id, 'input_manifest': manifest})
        reference_hashes.append({'case_id': case_id, **hashes})
        cases.append({'case_id': case_id, 'input_manifest': manifest, 'input_manifest_sha256': _digest(manifest),
                      'gt_sha256': hashes['gt'], 'annotations_sha256': hashes['annotations'], **acceptance_identity})
    evaluator = Path(config.get('evaluator_directory', BASE)).resolve()
    if config.get('evaluator_zip') and not config.get('evaluator_directory'):
        raise ValueError('study_evaluator_directory_required')
    protocol, coordinate, weights = scoring_options(config, evaluator)
    names = {name for name in ('evaluate.py', 'evaluate_v4.py', 'evaluate_v5.py', 'worker.py', 'worker_v4.py', 'worker_v5.py', 'bootstrap.py',
                               'scheduler.py', 'weights.json', 'weights_v3.json', 'weights_v4.json', 'weights_v5.json',
                               'SCORING_SOURCE_MANIFEST.json', 'PACKAGE_MANIFEST.json') if (evaluator / name).is_file()}
    for folder in ('src', 'scheduler', 'vendor'):
        names.update(path.relative_to(evaluator).as_posix() for path in (evaluator / folder).rglob('*')
                     if path.is_file() and path.suffix in ('.py', '.json') and '__pycache__' not in path.parts)
    if not {'evaluate.py', 'worker.py', 'weights.json'}.issubset(names):
        raise ValueError('study_evaluator_source_incomplete')
    evaluator_files = {}
    for name in sorted(names):
        _, raw = capabilities._read_file(evaluator / name)
        evaluator_files[name] = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    if 'PACKAGE_MANIFEST.json' in evaluator_files:
        verify_package(evaluator, evaluator_files['PACKAGE_MANIFEST.json']['sha256'])
    plugin_files = source_hashes()
    cad = copy.deepcopy(config.get('cad_identity') or {
        'backend': config.get('cad_backend', 'fusion'), 'version': config.get('cad_version')})
    input_sha, private_sha = _digest(inputs), _digest(reference_hashes)
    return {'dataset_sha256': _digest({'input_manifest_sha256': input_sha, 'gt_annotations_sha256': private_sha}),
            'input_manifest_sha256': input_sha, 'gt_annotations_sha256': private_sha,
            'common_protocol_sha256': common_protocol_sha256(), 'plugin_source_sha256': _digest(plugin_files),
            'evaluator_weights_sha256': _digest({'files': evaluator_files, 'protocol': protocol,
                                               'weights_sha256': evaluator_files[weights.name]['sha256']}),
            'budget_policy': {'seconds': 1800, 'requests': 200}, 'cad_identity': cad, 'coordinate_mode': coordinate,
            'scoring_protocol': protocol, 'case_identities': cases,
            'plugin_source_files': plugin_files, 'evaluator_source_files': evaluator_files}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _read(path):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('study_duplicate_json_key: ' + key)
            value[key] = item
        return value
    return json.loads(Path(path).read_text(encoding='utf-8-sig'), object_pairs_hook=unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('study_nonfinite_json')))


def _write(path, value):
    with Path(path).open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n')


def _identifiers(value, field):
    if (not isinstance(value, list) or not value or
            any(not isinstance(item, str) or not item.strip() for item in value) or
            len(value) != len(set(value))):
        raise ValueError('study_invalid_' + field)
    return list(value)


def _inside(root, relative):
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise ValueError('study_snapshot_path_invalid')
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('study_snapshot_path_invalid')
    return path


def build_plan(spec, output_dir):
    """Freeze profile × group × case × repetition and fresh per-cell configs.

    ``base_config`` contains the runnable local configuration; profile definitions
    may be supplied in ``candidate_profiles`` or that base configuration. All
    evidence absent from ``profile_evidence``/``common`` remains unverified.
    """
    if not isinstance(spec, dict) or spec.get('schema') != PLAN_SCHEMA:
        raise ValueError('study_spec_invalid')
    spec = copy.deepcopy(spec)
    experiment = spec.get('experiment_id')
    if not isinstance(experiment, str) or not _GROUP_ID.fullmatch(experiment):
        raise ValueError('study_experiment_id_invalid')
    profile_ids = _identifiers(spec.get('candidate_profile_ids'), 'candidate_profile_ids')
    case_ids = _identifiers(spec.get('case_ids'), 'case_ids')
    repetitions = spec.get('repetitions')
    if type(repetitions) is not int or repetitions < 1:
        raise ValueError('study_repetitions_invalid')
    seed = spec.get('schedule_seed')
    if type(seed) is not int:
        raise ValueError('study_schedule_seed_invalid')
    base = spec.get('base_config')
    if not isinstance(base, dict):
        raise ValueError('study_base_config_required')
    base = copy.deepcopy(base)
    if 'candidate_profiles' in spec:
        base['candidate_profiles'] = spec['candidate_profiles']
    if 'capability_registry' in spec:
        base['capability_registry'] = spec['capability_registry']
    registry = capabilities.load_registry(Path(base['capability_registry']))
    resolved = {identifier: resolve_candidate_profile(base, profile_id=identifier) for identifier in profile_ids}
    raw_groups = spec.get('groups')
    if not isinstance(raw_groups, list) or not raw_groups:
        raise ValueError('study_groups_invalid')
    groups, selections = {}, {}
    for raw in raw_groups:
        if not isinstance(raw, dict) or set(raw) - {'id', 'skills', 'mcp_servers', 'study_kind', 'label'}:
            raise ValueError('study_group_invalid')
        identifier = raw.get('id')
        if not isinstance(identifier, str) or not _GROUP_ID.fullmatch(identifier) or identifier in groups:
            raise ValueError('study_duplicate_or_invalid_group')
        group = copy.deepcopy(raw)
        selection = {'schema': 'candidate-capabilities/1', 'study_kind': group.get('study_kind', 'capability_ablation'),
                     'skill_delivery': spec.get('skill_delivery', 'gateway_read'),
                     'exposure_mode': spec.get('exposure_mode', 'gateway_proxy'),
                     'skills': group.get('skills'), 'mcp_servers': group.get('mcp_servers')}
        selections[identifier] = capabilities.normalize_selection(
            {'candidate_capabilities': selection}, registry, cad_backend=base.get('cad_backend', 'fusion'))
        groups[identifier] = {**group, 'selection': selection,
                              'selection_sha256': selections[identifier]['selection_sha256']}
    for left, right in itertools.combinations(groups.values(), 2):
        for field in ('skills', 'mcp_servers'):
            shared = set(left[field]) & set(right[field])
            if ([item for item in left[field] if item in shared] !=
                    [item for item in right[field] if item in shared]):
                raise ValueError('study_common_capability_order_mismatch')
    common = spec.get('common', {})
    if not isinstance(common, dict):
        raise ValueError('study_common_invalid')
    mode = spec.get('common_identity_mode', 'verify')
    if mode not in ('verify', 'capture', 'draft'):
        raise ValueError('study_common_identity_mode_invalid')
    capture = {'status': 'not_captured', 'mode': mode}
    if mode != 'draft' and base.get('questions_root') and base.get('private_cases'):
        if 'cad_identity' not in base and common.get('cad_identity') is not None:
            base['cad_identity'] = copy.deepcopy(common['cad_identity'])
        actual = capture_common_identity(base, case_ids)
        if mode == 'verify' and common != actual:
            raise ValueError('planned_common_identity_mismatch')
        common = actual
        capture['status'] = 'captured'
    elif mode == 'capture':
        raise ValueError('study_identity_paths_required')
    policy = spec.get('retry_policy', 'retain_all_attempts_no_best_score_selection')
    if policy != 'retain_all_attempts_no_best_score_selection':
        raise ValueError('study_retry_policy_unsupported')
    retry = spec.get('infrastructure_retry', {'max_attempts': 1, 'selection_rule': 'first_attempt'})
    if (not isinstance(retry, dict) or set(retry) != {'max_attempts', 'selection_rule'} or
            type(retry['max_attempts']) is not int or retry['max_attempts'] < 1 or
            retry['selection_rule'] not in ('first_attempt', 'first_non_infrastructure_attempt')):
        raise ValueError('study_retry_policy_invalid')
    # Complete static validation before claiming an output directory.
    _digest(spec)
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=False)
    _write(root / 'study-spec.json', spec)
    plan = {'schema': PLAN_SCHEMA, 'experiment_id': experiment, 'candidate_profile_ids': profile_ids,
            'case_ids': case_ids, 'repetitions': repetitions, 'schedule_seed': seed,
            'execution_mode': 'serial', 'schedule_algorithm': 'block_rotation_seeded/1',
            'retry_policy': policy, 'infrastructure_retry': retry, 'common': common,
            'common_identity_capture': capture,
            'admission': copy.deepcopy(spec.get('admission', {'status': 'not_verified'})),
            'generation_controls': copy.deepcopy(spec.get('generation_controls', {'temperature': None, 'seed': None})),
            'profiles': {}, 'groups': groups, 'cells': [], 'frozen_files': {}}
    plan['common_identity_validation'] = _plan_common_validation(plan)
    plan['admission_validation'] = _admission_validation(plan)
    # Registry definitions stay private; source bytes and definitions are bound
    # separately. Execution rechecks both snapshots and their original sources.
    frozen_registry = {key: registry[key] for key in ('schema', 'skills', 'mcp_servers')}
    _write(root / 'capability-registry.json', frozen_registry)
    for identifier, profile in resolved.items():
        folder = root / 'profiles' / identifier
        snapshot = freeze_candidate_profile(profile, folder)
        executable = Path(profile['cli_executable'])
        plan['profiles'][identifier] = {
            'configuration': profile, 'candidate_profile_sha256': profile['candidate_profile_sha256'],
            'snapshot_path': (folder / 'candidate-profile.json').relative_to(root).as_posix(),
            'executable_sha256': hashlib.sha256(executable.read_bytes()).hexdigest(),
            'verification_status': 'not_verified',
            'evidence': copy.deepcopy(spec.get('profile_evidence', {}).get(identifier, {}))}
    (root / 'groups').mkdir()
    for identifier, group in groups.items():
        manifest = capabilities.freeze_capabilities(selections[identifier], registry, root / 'groups' / identifier)
        group['snapshot_path'] = f'groups/{identifier}/capability-manifest.json'
        group['capability_content_sha256'] = _digest({
            'skills': manifest['skill_content_and_resource_hashes'],
            'servers': manifest['requested_server_definitions']})
    # Within every case/repetition block, rotate a seeded order of all
    # profile/group combinations. Across blocks no arm is systematically first.
    rng = random.Random(seed)
    arms = list(itertools.product(profile_ids, groups))
    rng.shuffle(arms)
    blocks = list(itertools.product(case_ids, range(1, repetitions + 1)))
    rng.shuffle(blocks)
    (root / 'cells').mkdir()
    for block_index, (case_id, repetition) in enumerate(blocks):
        offset = block_index % len(arms)
        for profile_id, group_id in arms[offset:] + arms[:offset]:
            cell_id = 'cell_' + _digest([profile_id, group_id, case_id, repetition])[:24]
            cell = {'cell_id': cell_id, 'candidate_profile_id': profile_id, 'group_id': group_id,
                    'case_id': case_id, 'repetition': repetition, 'sequence': len(plan['cells']) + 1,
                    'candidate_profile_sha256': resolved[profile_id]['candidate_profile_sha256'],
                    'selection_sha256': groups[group_id]['selection_sha256'],
                    'config_path': f'cells/{cell_id}.json'}
            config = apply_candidate_selection(base, {'candidate_profile': profile_id})
            config['candidate_profiles'] = {profile_id: copy.deepcopy(base['candidate_profiles'][profile_id])}
            for legacy in capabilities.LEGACY_SKILL_FIELDS:
                config.pop(legacy, None)
            config['candidate_capabilities'] = copy.deepcopy(groups[group_id]['selection'])
            config['capability_registry'] = str(root / 'capability-registry.json')
            config['case_ids'] = [case_id]
            config['capability_study'] = {'experiment_id': experiment, **{
                key: cell[key] for key in ('cell_id', 'candidate_profile_id', 'group_id', 'case_id', 'repetition')}}
            # Each cell is a fresh run; no previous workspace or session is reused.
            config['run_id'] = experiment + '_' + cell_id
            cell['config_sha256'] = _digest(config)
            _write(root / cell['config_path'], config)
            plan['cells'].append(cell)
    for path in sorted(root.rglob('*')):
        if path.is_file():
            plan['frozen_files'][path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    plan['planned_cell_count'] = len(plan['cells'])
    plan['plan_sha256'] = _digest(plan)
    _write(root / 'experiment-plan.json', plan)
    return plan


def _load_plan(plan_path):
    path = Path(plan_path)
    plan = _read(path)
    if not isinstance(plan, dict) or plan.get('schema') != PLAN_SCHEMA:
        raise ValueError('study_plan_invalid')
    unhashed = {key: value for key, value in plan.items() if key != 'plan_sha256'}
    if _digest(unhashed) != plan.get('plan_sha256'):
        raise ValueError('study_plan_hash_mismatch')
    keys = [(c['candidate_profile_id'], c['group_id'], c['case_id'], c['repetition']) for c in plan['cells']]
    expected = set(itertools.product(plan['candidate_profile_ids'], plan['groups'], plan['case_ids'],
                                     range(1, plan['repetitions'] + 1)))
    if len(keys) != len(set(keys)) or set(keys) != expected or len({c['cell_id'] for c in plan['cells']}) != len(keys):
        raise ValueError('study_plan_cells_invalid')
    for relative, expected_sha in plan['frozen_files'].items():
        target = _inside(path.parent, relative)
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != expected_sha:
            raise ValueError('study_snapshot_mismatch: ' + relative)
    return plan


def validate_plan_execution(plan_path, cell_id, config, *, require_verified=False, observed_launch=None):
    """Reject mutable plan/config/profile/reasoning/capability substitutions.

    Call before starting any candidate or CAD work. This validates static
    identity. ``require_verified`` additionally requires frozen CLI/version/hash
    readiness evidence; ``observed_launch`` binds a fresh probe to that evidence.
    The runner still owns reasoning support/admission checks, leases and stop
    conditions. CLI availability does not establish model or CAD availability.
    """
    plan = _load_plan(plan_path)
    cell = next((item for item in plan['cells'] if item['cell_id'] == cell_id), None)
    if cell is None or _digest(config) != cell['config_sha256']:
        raise ValueError('planned_candidate_configuration_mismatch')
    resolved = resolve_candidate_profile(config)
    if resolved['candidate_profile_sha256'] != cell['candidate_profile_sha256']:
        raise ValueError('planned_candidate_configuration_mismatch')
    evidence = plan['profiles'][cell['candidate_profile_id']]['evidence']
    if require_verified:
        if _plan_common_validation(plan)['status'] != 'complete':
            raise ValueError('study_common_identity_incomplete')
        if capture_common_identity(config, plan['case_ids']) != plan['common']:
            raise ValueError('planned_common_identity_mismatch')
        if _admission_validation(plan)['status'] != 'passed':
            raise ValueError('study_admission_not_verified')
        if plan['common']['cad_identity']['backend'] != config.get('cad_backend', 'fusion'):
            raise ValueError('planned_common_identity_mismatch: CAD backend')
        if (any(evidence.get(key) is not True for key in
                ('configuration_valid', 'cli_available', 'required_flags_present')) or
                not isinstance(evidence.get('cli_version'), str) or not evidence['cli_version'].strip() or
                not isinstance(evidence.get('resolved_executable'), str) or
                not Path(evidence['resolved_executable']).is_absolute() or
                not isinstance(evidence.get('executable_sha256'), str) or
                not re.fullmatch('[a-f0-9]{64}', evidence['executable_sha256'])):
            raise ValueError('study_profile_not_verified')
        executable = Path(evidence['resolved_executable'])
        if not executable.is_file() or hashlib.sha256(executable.read_bytes()).hexdigest() != evidence['executable_sha256']:
            raise ValueError('planned_candidate_configuration_mismatch: installed executable')
    if observed_launch is not None:
        if not isinstance(observed_launch, dict) or any(
                evidence.get(key) is None or observed_launch.get(key) != evidence[key]
                for key in ('cli_version', 'resolved_executable', 'executable_sha256')):
            raise ValueError('planned_candidate_configuration_mismatch: observed launch')
    for profile in plan['profiles'].values():
        binary = Path(profile['configuration']['cli_executable'])
        if not binary.is_file() or hashlib.sha256(binary.read_bytes()).hexdigest() != profile['executable_sha256']:
            raise ValueError('study_snapshot_mismatch: executable')
    for group in plan['groups'].values():
        capabilities.verify_frozen_capabilities(_read(_inside(Path(plan_path).parent, group['snapshot_path'])))
    return copy.deepcopy(cell)


def run_manifest_template(plan_path, cell_id, run_id, attempt_id):
    """Produce portable private evidence identity; unknown measurements are null."""
    plan = _load_plan(plan_path)
    cell = next((item for item in plan['cells'] if item['cell_id'] == cell_id), None)
    if cell is None:
        raise ValueError('study_cell_unknown')
    return {'schema': RUN_SCHEMA, 'experiment_id': plan['experiment_id'], 'plan_sha256': plan['plan_sha256'],
            **{key: cell[key] for key in ('cell_id', 'candidate_profile_id', 'group_id', 'case_id', 'repetition',
                                        'candidate_profile_sha256', 'selection_sha256', 'config_sha256')},
            'run_id': run_id, 'attempt_id': attempt_id, 'attempt_index': 1,
            'frozen_identity': copy.deepcopy(plan['common']),
            'requested_reasoning': copy.deepcopy(plan['profiles'][cell['candidate_profile_id']]['configuration']['reasoning']),
            'outcome': {'native_submitted': False, 'trusted_export': False, 'primary_complete': False,
                        'formal_score': None, 'primary_coverage': None, 'failure_stage': None,
                        'failure_category': None, 'cad_unknown': False},
            'usage': {'actual_skill_reads': None, 'per_server_tool_calls': None, 'controlled_calls': None,
                      'modeling_seconds': None, 'input_tokens': None, 'output_tokens': None,
                      'reasoning_tokens': None, 'cost': None}}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _mean(values):
    values = [value for value in values if _number(value)]
    return sum(values) / len(values) if values else None


def _run_error(plan, cell, run):
    if not isinstance(run, dict) or run.get('schema') != RUN_SCHEMA:
        return 'study_run_schema_invalid'
    if type(run.get('repetition')) is not int:
        return 'planned_candidate_configuration_mismatch'
    for key in ('experiment_id', 'plan_sha256'):
        if run.get(key) != plan[key]:
            return 'planned_experiment_identity_mismatch'
    for key in ('cell_id', 'candidate_profile_id', 'group_id', 'case_id', 'repetition',
                'candidate_profile_sha256', 'selection_sha256', 'config_sha256'):
        if run.get(key) != cell[key]:
            return 'planned_candidate_configuration_mismatch'
    reasoning = plan['profiles'][cell['candidate_profile_id']]['configuration']['reasoning']
    if run.get('requested_reasoning') != reasoning:
        return 'planned_candidate_configuration_mismatch'
    configuration = plan['profiles'][cell['candidate_profile_id']]['configuration']
    aliases = {'requested_model': 'model', 'configured_executable': 'cli_executable',
               'requested_isolation': 'strict_tool_isolation'}
    for evidence_key, configuration_key in {**{key: key for key in LAUNCH_FIELDS}, **aliases}.items():
        if evidence_key in run and run[evidence_key] != configuration.get(configuration_key):
            return 'planned_candidate_configuration_mismatch'
    if run.get('frozen_identity') != plan['common']:
        return 'planned_common_identity_mismatch'
    if (any(not isinstance(run.get(key), str) or not run[key].strip() for key in ('run_id', 'attempt_id'))
            or type(run.get('attempt_index')) is not int or run['attempt_index'] < 1):
        return 'study_attempt_identity_invalid'
    if 'retry' in run:
        retry = run['retry']
        if (not isinstance(retry, dict) or set(retry) != {'reason', 'previous_attempt_id', 'evidence_sha256'} or
                retry.get('reason') != 'infrastructure_failure' or
                not isinstance(retry.get('previous_attempt_id'), str) or not retry['previous_attempt_id'].strip() or
                not isinstance(retry.get('evidence_sha256'), str) or
                not re.fullmatch('[a-f0-9]{64}', retry['evidence_sha256'])):
            return 'study_retry_metadata_invalid'
    outcome = run.get('outcome')
    if not isinstance(outcome, dict) or any(type(outcome.get(key)) is not bool for key in
            ('native_submitted', 'trusted_export', 'primary_complete', 'cad_unknown')):
        return 'study_outcome_invalid'
    if any(outcome.get(key) is not None and not isinstance(outcome[key], str)
           for key in ('failure_stage', 'failure_category')):
        return 'study_outcome_invalid'
    if ('exposure_verification_status' in run and not isinstance(run['exposure_verification_status'], str)):
        return 'study_exposure_evidence_invalid'
    score = outcome.get('formal_score')
    coverage = outcome.get('primary_coverage')
    if ((score is not None and not _number(score)) or
            (coverage is not None and (not _number(coverage) or not 0 <= coverage <= 1)) or
            (outcome['primary_complete'] and
             (score is None or not _number(coverage) or coverage != 1
              or not outcome['native_submitted'] or not outcome['trusted_export'])) or
            (not outcome['primary_complete'] and score is not None) or
            (outcome['trusted_export'] and not outcome['native_submitted'])):
        return 'study_outcome_invalid'
    if not isinstance(run.get('usage', {}), dict):
        return 'study_usage_invalid'
    return None


def _select_attempts(plan, attempts):
    """Return the prespecified attempt; scores never participate in selection."""
    ordered = sorted(attempts, key=lambda item: item['manifest']['attempt_index'])
    count = len(ordered)
    policy = plan['infrastructure_retry']
    if count > policy['max_attempts'] or [r['manifest']['attempt_index'] for r in ordered] != list(range(1, count + 1)):
        return None, 'unexplained_multiple_attempts'
    if len({r['manifest']['attempt_id'] for r in ordered}) != count:
        return None, 'unexplained_multiple_attempts'
    for previous, current in zip(ordered, ordered[1:]):
        prior, run = previous['manifest'], current['manifest']
        retry = run.get('retry', {})
        if (prior['outcome'].get('failure_category') != 'infrastructure_failure' or
                prior['outcome']['native_submitted'] or prior['outcome']['cad_unknown'] or
                retry.get('reason') != 'infrastructure_failure' or
                retry.get('previous_attempt_id') != prior['attempt_id'] or
                not isinstance(retry.get('evidence_sha256'), str) or
                not re.fullmatch('[a-f0-9]{64}', retry['evidence_sha256'])):
            return None, 'unexplained_multiple_attempts'
    if policy['selection_rule'] == 'first_non_infrastructure_attempt':
        return next((r for r in ordered if r['manifest']['outcome'].get('failure_category') != 'infrastructure_failure'), ordered[-1]), None
    return ordered[0], None


def _pairs(left, right):
    left = {(row['case_id'], row['repetition']): row for row in left}
    right = {(row['case_id'], row['repetition']): row for row in right}
    pairs, unpaired = [], []
    identity_complete = all(row['common_identity_complete'] and row['admission_verified']
                            for row in [*left.values(), *right.values()])
    for key in left:
        a, b = left[key], right[key]
        scores = [row['outcome']['formal_score'] for row in (a, b)]
        if all(row['comparable'] and row['outcome']['primary_complete'] and _number(score)
               for row, score in zip((a, b), scores)):
            pairs.append({'case_id': key[0], 'repetition': key[1], 'left_score': scores[0],
                          'right_score': scores[1], 'difference': scores[1] - scores[0]})
        else:
            unpaired.append({'case_id': key[0], 'repetition': key[1],
                             'left_reason': 'common_identity_incomplete' if not a['common_identity_complete'] else
                                            'admission_not_verified' if not a['admission_verified'] else
                                            a['status'] if a['status'] != 'observed' else
                                            a['outcome'].get('failure_category') or 'primary_incomplete',
                             'right_reason': 'common_identity_incomplete' if not b['common_identity_complete'] else
                                             'admission_not_verified' if not b['admission_verified'] else
                                             b['status'] if b['status'] != 'observed' else
                                             b['outcome'].get('failure_category') or 'primary_incomplete'})
    return {'paired_planned_count': len(left), 'quality_pair_count': len(pairs),
            'comparison_eligible': identity_complete,
            'paired_case_count': len({p['case_id'] for p in pairs}), 'quality_pairs': pairs, 'unpaired': unpaired,
            'mean_quality_difference': _mean([p['difference'] for p in pairs]),
            'native_submission_rate_difference':
                (sum(r['outcome']['native_submitted'] for r in right.values()) -
                 sum(r['outcome']['native_submitted'] for r in left.values())) / len(left) if identity_complete else None,
            'interpretation': 'Descriptive quality difference on the common complete subset; selection bias is possible. '
                              'Repetitions are correlated within case; no confidence interval estimated.'}


def _profile_comparison_kind(left, right):
    a, b = left['configuration'], right['configuration']
    differences = {key: {'left': a.get(key), 'right': b.get(key)} for key in LAUNCH_FIELDS if a.get(key) != b.get(key)}
    ea, eb = left['evidence'], right['evidence']
    for key in sorted(set(ea) | set(eb)):
        if ea.get(key) != eb.get(key):
            differences['evidence.' + key] = {'left': ea.get(key), 'right': eb.get(key)}
    # Default/variant modes cannot establish a one-factor intensity experiment.
    requests = [profile['reasoning'] for profile in (a, b)]
    explicit_effort = all(r.get('mode') == 'explicit' and r.get('control') == 'effort' for r in requests)
    required = ('cli_version', 'resolved_executable_sha256', 'observed_model', 'model_connection_identity',
                'observed_isolation', 'generation_controls')
    verified = all(e.get('status') == 'verified' and e.get('reasoning_support_status') == 'verified' and
                   all(e.get(key) is not None for key in required) for e in (ea, eb))
    evidence_equal = ea == eb
    reasoning_only = (set(differences) == {'reasoning'} and explicit_effort and verified and evidence_equal
                      and left['executable_sha256'] == right['executable_sha256'])
    return 'reasoning_only' if reasoning_only else 'system_settings', differences


def _usage_count(value):
    if _number(value):
        return value
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict) and all(_number(item) for item in value.values()):
        return sum(value.values())
    return None


def _stratum(profile_id, group_id, group, rows):
    n = len(rows)
    observed = [row for row in rows if row['status'] == 'observed']
    failures, stages, exposures = {}, {}, {}
    for row in rows:
        exposure = row['evidence'].get('exposure_verification_status', 'not_verified')
        exposures[exposure] = exposures.get(exposure, 0) + 1
        category = row['outcome'].get('failure_category') if row['status'] == 'observed' else row['status']
        if category:
            failures[category] = failures.get(category, 0) + 1
        stage = row['outcome'].get('failure_stage')
        if stage:
            stages[stage] = stages.get(stage, 0) + 1
    reads = [_usage_count(row['usage'].get('actual_skill_reads')) for row in observed]
    if not group['skills']:
        skill_usage = 'not_enabled'
    elif any(value is not None and value > 0 for value in reads):
        skill_usage = 'enabled_used'
    elif len(reads) == n and all(value == 0 for value in reads):
        skill_usage = 'enabled_not_used'
    else:
        skill_usage = 'enabled_usage_unknown'
    usage_keys = ('controlled_calls', 'modeling_seconds', 'input_tokens', 'output_tokens', 'reasoning_tokens', 'cost')
    return {'candidate_profile_id': profile_id, 'group_id': group_id, 'planned_count': n,
            'independent_case_count': len({row['case_id'] for row in rows}), 'observed_count': len(observed),
            'native_submission_rate': sum(row['outcome']['native_submitted'] for row in rows) / n,
            'trusted_export_rate': sum(row['outcome']['trusted_export'] for row in rows) / n,
            'primary_complete_rate': sum(row['outcome']['primary_complete'] for row in rows) / n,
            'cad_unknown_rate': sum(row['outcome']['cad_unknown'] for row in rows) / n,
            'failure_categories': dict(sorted(failures.items())), 'failure_stages': dict(sorted(stages.items())),
            'exposure_verification_counts': exposures,
            'skill_usage_status': skill_usage,
            'per_server_usage_status': {server: (
                'enabled_used' if any(_number(row['usage'].get('per_server_tool_calls', {}).get(server)) and
                                      row['usage']['per_server_tool_calls'][server] > 0
                                      for row in observed if isinstance(row['usage'].get('per_server_tool_calls'), dict))
                else 'enabled_not_used' if len(observed) == n and all(
                    isinstance(row['usage'].get('per_server_tool_calls'), dict) and
                    row['usage']['per_server_tool_calls'].get(server) == 0 for row in observed)
                else 'enabled_usage_unknown') for server in group['mcp_servers']},
            'comparison_eligible': all(row['common_identity_complete'] and row['admission_verified'] for row in rows),
            'mean_formal_score_complete_subset': _mean([row['outcome']['formal_score'] for row in rows
                                                       if row['comparable']]),
            'formal_score_observed_count': sum(row['outcome']['formal_score'] is not None for row in rows),
            'usage_means': {key: _mean([row['usage'].get(key) for row in observed]) for key in usage_keys},
            'usage_observed_counts': {key: sum(_number(row['usage'].get(key)) for row in observed) for key in usage_keys}}


def analyze_plan(plan_path, run_manifest_paths, output_dir):
    """Read frozen evidence, retain every planned denominator, write JSON/CSV/MD.

    Invalid/mismatched evidence is retained with its rejection reason. It cannot
    improve completion or enter a quality pair. Unexplained repeated attempts
    reject the entire cell rather than silently selecting the best score.
    """
    plan = _load_plan(plan_path)
    common_validation = _plan_common_validation(plan)
    identity_complete = common_validation['status'] == 'complete'
    admission_validation = _admission_validation(plan)
    admission_verified = admission_validation['status'] == 'passed'
    by_id = {cell['cell_id']: cell for cell in plan['cells']}
    attempts = {key: [] for key in by_id}
    rejected, invalid_cells = [], set()
    for source in run_manifest_paths:
        source = Path(source)
        try:
            run = _read(source)
        except (ValueError, OSError) as exc:
            rejected.append({'source': str(source), 'reason': 'study_run_unreadable', 'detail': str(exc), 'manifest': None})
            continue
        cell_id = run.get('cell_id') if isinstance(run, dict) else None
        evidence = {'source': str(source), 'manifest': run}
        if not isinstance(cell_id, str) or cell_id not in by_id:
            rejected.append({**evidence, 'reason': 'unplanned_cell'})
            continue
        attempts[cell_id].append(evidence)
        reason = _run_error(plan, by_id[cell_id], run)
        if reason:
            rejected.append({**evidence, 'reason': reason})
            invalid_cells.add(cell_id)
    rows = []
    for cell in plan['cells']:
        cell_attempts = attempts[cell['cell_id']]
        chosen = None
        if cell_attempts and cell['cell_id'] not in invalid_cells:
            chosen, error = _select_attempts(plan, cell_attempts)
            if error:
                rejected.extend({**attempt, 'reason': error} for attempt in cell_attempts)
                invalid_cells.add(cell['cell_id'])
        empty = {'native_submitted': False, 'trusted_export': False, 'primary_complete': False,
                 'formal_score': None, 'primary_coverage': None, 'failure_stage': None,
                 'failure_category': None, 'cad_unknown': False}
        rows.append({**cell, 'status': 'observed' if chosen else 'rejected_run' if cell_attempts else 'missing_run',
                     'common_identity_complete': identity_complete, 'admission_verified': admission_verified,
                     'comparable': bool(chosen) and identity_complete and admission_verified,
                     'comparability_reasons': ([] if identity_complete else ['common_identity_incomplete']) +
                                              ([] if admission_verified else ['admission_not_verified']),
                     'selected_attempt_id': chosen['manifest']['attempt_id'] if chosen else None,
                     'outcome': copy.deepcopy(chosen['manifest']['outcome']) if chosen else empty,
                     'usage': copy.deepcopy(chosen['manifest'].get('usage', {})) if chosen else {},
                     'evidence': {key: copy.deepcopy(chosen['manifest'][key]) for key in (
                         'exposure_verification_status', 'observed_model_evidence', 'observed_reasoning',
                         'reasoning_verification_status', 'observed_isolation_evidence', 'failure_and_cleanup_evidence')
                         if chosen and key in chosen['manifest']},
                     'attempts': copy.deepcopy(cell_attempts)})
    strata, within, across, interactions = [], [], [], []
    cohorts = {}
    for profile_id in plan['candidate_profile_ids']:
        for group_id, group in plan['groups'].items():
            cohort = [row for row in rows if row['candidate_profile_id'] == profile_id and row['group_id'] == group_id]
            cohorts[profile_id, group_id] = cohort
            strata.append(_stratum(profile_id, group_id, group, cohort))
        for left_id, right_id in itertools.combinations(plan['groups'], 2):
            left, right = plan['groups'][left_id], plan['groups'][right_id]
            changed = [key for key in ('skills', 'mcp_servers') if left[key] != right[key]]
            if len(changed) != 1 or len(set(left[changed[0]]) ^ set(right[changed[0]])) != 1:
                continue
            if left['selection']['study_kind'] != right['selection']['study_kind']:
                continue
            within.append({'candidate_profile_id': profile_id, 'contrast': right_id + '-' + left_id,
                           'comparison_kind': 'capability_within_profile', 'factor': changed[0],
                           **_pairs(cohorts[profile_id, left_id], cohorts[profile_id, right_id])})
        if all(key in plan['groups'] for key in ('A', 'B', 'C', 'D')):
            available = {entry['contrast'] for entry in within if entry['candidate_profile_id'] == profile_id}
            if {'B-A', 'C-A', 'D-C', 'D-B'}.issubset(available):
                indexed = {g: {(r['case_id'], r['repetition']): r for r in cohorts[profile_id, g]} for g in 'ABCD'}
                values = []
                for key in indexed['A']:
                    outcomes = [indexed[g][key]['outcome'] for g in 'ABCD']
                    if identity_complete and admission_verified and all(o['primary_complete'] and _number(o['formal_score']) for o in outcomes):
                        a, b, c, d = [o['formal_score'] for o in outcomes]
                        values.append({'case_id': key[0], 'repetition': key[1], 'interaction': (d - c) - (b - a)})
                interactions.append({'candidate_profile_id': profile_id, 'contrast': '(D-C)-(B-A)',
                                     'comparison_eligible': identity_complete and admission_verified,
                                     'planned_count': len(indexed['A']), 'quality_pair_count': len(values),
                                     'values': values, 'mean_quality_interaction': _mean([v['interaction'] for v in values])})
    for left_id, right_id in itertools.combinations(plan['candidate_profile_ids'], 2):
        kind, differences = _profile_comparison_kind(plan['profiles'][left_id], plan['profiles'][right_id])
        if not identity_complete or not admission_verified:
            kind = 'system_settings'
        for group_id in plan['groups']:
            across.append({'left_profile_id': left_id, 'right_profile_id': right_id, 'group_id': group_id,
                           'contrast': right_id + '-' + left_id, 'comparison_kind': kind,
                           'frozen_setting_differences': differences,
                           'attribution_limit': 'System/settings comparison; differences cannot be attributed to CLI alone.'
                                                if kind == 'system_settings' else 'Only verified native effort differs in frozen evidence.',
                           **_pairs(cohorts[left_id, group_id], cohorts[right_id, group_id])})
    result = {'schema': 'candidate-capability-analysis/1', 'experiment_id': plan['experiment_id'],
              'plan_sha256': plan['plan_sha256'], 'planned_cell_count': len(rows),
              'independent_case_count': len(plan['case_ids']), 'repetitions': plan['repetitions'],
              'common_identity_validation': common_validation,
              'admission_validation': admission_validation,
              'cells': rows, 'strata': strata, 'within_profile_comparisons': within,
              'profile_comparisons': across, 'interactions': interactions, 'rejected_runs': rejected,
              'limitations': ['All planned cells remain denominators, including missing and rejected evidence.',
                             'Formal missing scores remain null; policy lower bounds are separate observations.',
                             'Quality pairs describe the common complete subset and may have selection bias.',
                             'Repetitions do not add independent cases; no confidence intervals are estimated.',
                             'A configured capability is distinct from verified exposure and actual use.',
                             'Schedule seed controls order only; model generation randomness may be uncontrolled.']}
    if not identity_complete:
        result['limitations'].insert(0, 'Common identity is incomplete: draft observations remain visible, but quality pairs, '
                                    'score aggregates and main-effect differences are excluded; this plan cannot enter formal execution.')
    if not admission_verified:
        result['limitations'].insert(0, 'Maintainer admission is missing or stale: drawing readability and GT primary measurability '
                                    'have not been established for the captured case bytes. Raw observations and denominators remain; '
                                    'comparisons are ineligible and formal execution is blocked.')
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=False)
    _write(root / 'capability-analysis.json', result)
    columns = ('candidate_profile_id', 'group_id', 'case_id', 'repetition', 'cell_id', 'status',
               'selected_attempt_id', 'native_submitted', 'trusted_export', 'primary_complete',
               'formal_score', 'primary_coverage', 'failure_stage', 'failure_category', 'cad_unknown')
    with (root / 'capability-analysis.csv').open('x', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            data = {**row, **row['outcome']}
            writer.writerow({key: data.get(key) for key in columns})
    lines = ['# Capability experiment analysis', '', f"Experiment: {plan['experiment_id']}", '',
             f"Planned attempts/cells: {len(rows)}; independent cases: {len(plan['case_ids'])}; repetitions: {plan['repetitions']}.", '',
             '| Profile | Group | Planned | Submitted | Trusted export | Primary complete |',
             '|---|---|---:|---:|---:|---:|']
    for row in strata:
        lines.append(f"| {row['candidate_profile_id']} | {row['group_id']} | {row['planned_count']} | "
                     f"{row['native_submission_rate']:.3f} | {row['trusted_export_rate']:.3f} | {row['primary_complete_rate']:.3f} |")
    lines += ['', '## Within-profile quality differences', '', '| Profile | Contrast | Complete pairs / planned | Mean difference |',
              '|---|---|---:|---:|']
    for row in within:
        lines.append(f"| {row['candidate_profile_id']} | {row['contrast']} | {row['quality_pair_count']} / {row['paired_planned_count']} | {row['mean_quality_difference']} |")
    lines += ['', '## Profile comparisons', '', '| Profiles | Group | Kind | Complete pairs / planned | Mean difference |',
              '|---|---|---|---:|---:|']
    for row in across:
        lines.append(f"| {row['contrast']} | {row['group_id']} | {row['comparison_kind']} | "
                     f"{row['quality_pair_count']} / {row['paired_planned_count']} | {row['mean_quality_difference']} |")
    lines += ['', f"Rejected run evidence: {len(rejected)} (full evidence and reasons in JSON).", '', '## Interpretation', '']
    lines += ['- ' + item for item in result['limitations']]
    with (root / 'capability-analysis.md').open('x', encoding='utf-8', newline='\n') as stream:
        stream.write('\n'.join(lines) + '\n')
    return result

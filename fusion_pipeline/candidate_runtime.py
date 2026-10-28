"""Private per-attempt launch and capability evidence owned by the runner.

Static selection is resolved elsewhere. This module binds it to a fresh CLI
probe, immutable source bytes, the actual prompt and observed gateway evidence.
It never connects to CAD or starts a candidate model.
"""
import copy
import hashlib
import json
import math
from pathlib import Path

from src.io_utils import sha, strict_json
from .candidate_profiles import LAUNCH_FIELDS, freeze_candidate_profile, resolve_candidate_profile
from .candidate_config import project_candidate_evidence, public_candidate_selection
from .capabilities import (freeze_capabilities, load_registry, normalize_selection,
                           public_capabilities, verify_frozen_capabilities)
from .candidate_prompt import build_candidate_prompt, common_protocol_sha256
from .reasoning import resolve_reasoning


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def negative_control(config):
    selection = config.get('candidate_capabilities', {})
    return (isinstance(selection, dict) and selection.get('study_kind') == 'negative_control_no_cad'
            and isinstance(selection.get('mcp_servers'), list) and 'cad_native' not in selection['mcp_servers'])


def observe_launch(config, doctor=None):
    from .adapters import probe
    doctor = doctor if doctor is not None else probe(config['provider'], config.get('cli_executable', config.get(config['provider'])))
    if (doctor.get('available') is not True or doctor.get('required_flags_present') is not True
            or not isinstance(doctor.get('version'), str) or not doctor['version'].strip()
            or not isinstance(doctor.get('executable'), str)):
        raise ValueError('candidate_cli_unverified')
    binary = Path(doctor['executable']).resolve(strict=True)
    if not binary.is_file():
        raise ValueError('candidate_executable_unavailable')
    observed = {'resolved_executable': str(binary), 'executable_sha256': sha(binary),
                'cli_version': doctor['version'], 'configured_executable': config.get('cli_executable', config.get(config['provider']))}
    configured = observed['configured_executable']
    observed['configured_executable_sha256'] = sha(configured) if configured else None
    request = config.get('reasoning', {'mode': 'cli_default'})
    if request.get('mode') == 'explicit':
        support = config.get('reasoning_support', {})
        if (not isinstance(support, dict) or any(support.get(key) != observed[key]
                for key in ('resolved_executable', 'executable_sha256', 'cli_version'))):
            raise ValueError('reasoning_support_unverified: installed executable/version binding required')
    resolve_reasoning(dict(config, cli_version=observed['cli_version']), config.get('reasoning_support', {}))
    return observed


def _request_identity(config):
    # Bind the fields actually handed to the adapter AND the selected profile's
    # current definition. A mutable profile ID cannot replace a frozen launch.
    return {**{key: copy.deepcopy(config[key]) for key in (*LAUNCH_FIELDS, 'reasoning_support') if key in config},
            'resolved_profile': resolve_candidate_profile(config),
            'capability_selection': copy.deepcopy(config.get('candidate_capabilities')),
            'capability_registry': config.get('capability_registry')}


def freeze_attempt(config, output_dir, *, contract, observed_launch, case, input_manifest, source_hashes, smoke=False):
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=False)
    registry = load_registry(config['capability_registry'])
    selection = config['candidate_capability_selection']
    manifest = freeze_capabilities(selection, registry, root / 'capabilities')
    profile = freeze_candidate_profile(config, root / 'profile')
    public = public_capabilities(manifest)
    prompt = build_candidate_prompt(case, contract, public)
    if smoke:
        prompt += ('\nThis is an authorized read-only integration smoke. Only discover tools, get_contract, '
                   'read_input(name=TASK.md), and read selected skills. '
                   + ('Query one read-only native CAD status through cad_tools/cad_call. ' if 'cad_native' in public['mcp_servers'] else
                      'Do not contact any CAD service or submit native geometry. ')
                   + 'Do not create, modify, close documents or submit. '
                   'Your entire final response must be the single literal SMOKE_OK, '
                   'with no summary, explanation, table, Markdown fence, or other text.\n')
    launch = {key: copy.deepcopy(config[key]) for key in LAUNCH_FIELDS if key in config}
    launch.update(observed_launch)
    runtime = {**copy.deepcopy(manifest), **copy.deepcopy(observed_launch),
        **{key: contract[key] for key in ('run_id', 'case_id', 'attempt_id')},
        'schema': 'candidate-attempt/1', 'candidate_profile_id': config['candidate_profile_id'],
        'candidate_profile_sha256': profile['candidate_profile_sha256'],
        'resolved_candidate_launch_sha256': digest(launch),
        'candidate_configuration_sha256': digest(_request_identity(config)),
        'profile_manifest': str(root / 'profile/candidate-profile.json'),
        'profile_manifest_sha256': sha(root / 'profile/candidate-profile.json'),
        'capability_manifest': str(root / 'capabilities/capability-manifest.json'),
        'capability_manifest_sha256': sha(root / 'capabilities/capability-manifest.json'),
        'common_protocol_sha256': common_protocol_sha256(),
        'prompt_sha256': hashlib.sha256(prompt.encode('utf-8')).hexdigest(),
        'input_manifest_sha256': digest(input_manifest), 'plugin_commit_and_source_hashes': copy.deepcopy(source_hashes),
        'provider': config['provider'], 'requested_model': config['model'],
        'expected_actual_model': config.get('expected_actual_model'), 'observed_model_evidence': None,
        'requested_reasoning': copy.deepcopy(config.get('reasoning')), 'observed_reasoning': None,
        'reasoning_verification_status': 'not_observed',
        'requested_isolation': config.get('strict_tool_isolation'), 'observed_isolation_evidence': None,
        'budget_policy': {key: contract[key] for key in ('max_tool_calls', 'deadline')},
        'candidate_selection': public_candidate_selection(config), 'smoke': smoke}
    resolved = resolve_reasoning(dict(config, cli_version=observed_launch['cli_version']), config.get('reasoning_support', {}))
    for key in ('resolved_reasoning', 'reasoning_origin', 'reasoning_support_evidence', 'emitted_reasoning_parameters',
                'variant_resolved_options', 'variant_resolved_options_sha256', 'reasoning_token_usage_or_null'):
        runtime[key] = copy.deepcopy(resolved[key])
    runtime['credential_source_without_values'] = copy.deepcopy(profile['credential_source_without_values'])
    runtime['model_connection_identity'] = None
    # The rich contract-bound manifest is written once. Gateway observations
    # remain separate so finalization never rewrites its admission digest.
    frozen = {key: copy.deepcopy(value) for key, value in runtime.items()
              if key not in ('capability_manifest', 'capability_manifest_sha256')}
    frozen['schema'] = manifest['schema']
    manifest_path = root / 'capability-manifest.json'
    with manifest_path.open('x', encoding='utf-8') as stream:
        json.dump(frozen, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    runtime.update(capability_manifest=str(manifest_path), capability_manifest_sha256=sha(manifest_path),
                   attempt_evidence_path=str(root / 'candidate-attempt.json'))
    (root / 'candidate-attempt.json').write_text(json.dumps(runtime, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return runtime, prompt


def verify_attempt(runtime, config, *, source_hashes, prompt=None, input_manifest=None):
    if digest(_request_identity(config)) != runtime['candidate_configuration_sha256']:
        raise ValueError('candidate_configuration_changed')
    if source_hashes != runtime['plugin_commit_and_source_hashes'] or common_protocol_sha256() != runtime['common_protocol_sha256']:
        raise ValueError('candidate_source_changed')
    for path_key, hash_key in (('capability_manifest', 'capability_manifest_sha256'),
                               ('profile_manifest', 'profile_manifest_sha256'),
                               ('resolved_executable', 'executable_sha256'),
                               ('configured_executable', 'configured_executable_sha256')):
        if runtime.get(path_key) and sha(runtime[path_key]) != runtime[hash_key]:
            raise ValueError('candidate_snapshot_or_executable_changed')
    if runtime.get('capability_manifest'):
        manifest = strict_json(runtime['capability_manifest'])
        verify_frozen_capabilities(manifest)
        current = normalize_selection(config, load_registry(config['capability_registry']), cad_backend=config.get('cad_backend', 'fusion'))
        pinned = config['candidate_capability_selection']
        if any(current.get(key) != pinned.get(key) for key in ('selection_sha256', '_registry_definition_sha256', '_source_hashes')):
            raise ValueError('candidate_capability_configuration_changed')
    elif runtime.get('legacy_skill_sha256') and sha(config['cad_skill']) != runtime['legacy_skill_sha256']:
        raise ValueError('public modeling skill changed after freezing')
    if prompt is not None and hashlib.sha256(prompt.encode('utf-8')).hexdigest() != runtime['prompt_sha256']:
        raise ValueError('candidate_prompt_changed')
    if input_manifest is not None and digest(input_manifest) != runtime['input_manifest_sha256']:
        raise ValueError('candidate_input_manifest_changed')
    return True


def freeze_legacy_attempt(config, output_dir, *, contract, observed_launch, prompt, input_manifest, source_hashes):
    """Freeze a selected profile without migrating legacy capabilities/prompt."""
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=False)
    profile = freeze_candidate_profile(config, root / 'profile')
    launch = {key: copy.deepcopy(config[key]) for key in LAUNCH_FIELDS if key in config}
    launch.update(observed_launch)
    runtime = {**copy.deepcopy(observed_launch), 'schema': 'candidate-attempt/1',
        **{key: contract[key] for key in ('run_id', 'case_id', 'attempt_id')},
        'selection_origin': 'legacy_default', 'candidate_profile_id': config['candidate_profile_id'],
        'candidate_profile_sha256': profile['candidate_profile_sha256'],
        'resolved_candidate_launch_sha256': digest(launch),
        'candidate_configuration_sha256': digest(_request_identity(config)),
        'profile_manifest': str(root / 'profile/candidate-profile.json'),
        'profile_manifest_sha256': sha(root / 'profile/candidate-profile.json'),
        'common_protocol_sha256': common_protocol_sha256(),
        'prompt_sha256': hashlib.sha256(prompt.encode('utf-8')).hexdigest(),
        'input_manifest_sha256': digest(input_manifest), 'plugin_commit_and_source_hashes': copy.deepcopy(source_hashes),
        'provider': config['provider'], 'requested_model': config['model'],
        'expected_actual_model': config.get('expected_actual_model'), 'observed_model_evidence': None,
        'requested_reasoning': copy.deepcopy(config.get('reasoning')), 'observed_reasoning': None,
        'reasoning_verification_status': 'not_observed', 'reasoning_token_usage_or_null': None,
        'exposure_verification_status': 'not_recorded_legacy', 'actual_skill_reads': None,
        'per_server_tool_calls': None, 'observed_tool_surface': [],
        'legacy_skill_sha256': sha(config['cad_skill']) if config.get('cad_skill') else None,
        'candidate_selection': public_candidate_selection(config),
        'attempt_evidence_path': str(root / 'candidate-attempt.json')}
    resolved = resolve_reasoning(dict(config, cli_version=observed_launch['cli_version']), config.get('reasoning_support', {}))
    for key in ('resolved_reasoning', 'reasoning_origin', 'reasoning_support_evidence', 'emitted_reasoning_parameters',
                'variant_resolved_options', 'variant_resolved_options_sha256'):
        runtime[key] = copy.deepcopy(resolved[key])
    (root / 'candidate-attempt.json').write_text(json.dumps(runtime, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return runtime


def finish_attempt(runtime, result, audit_dir):
    """Keep configured, exposed and used evidence distinct; unknown stays null."""
    audit = Path(audit_dir)
    observed = strict_json(audit / 'capability-evidence.json') if (audit / 'capability-evidence.json').is_file() else {}
    verification = result.get('session_verification', {})
    for key in ('actual_skill_reads', 'per_server_tool_calls', 'observed_tool_surface',
                'candidate_visible_tool_schema_hash', 'exposure_verification_status',
                'observed_server_identity', 'tools_list_responses', 'cleanup_evidence'):
        if key in observed:
            runtime[key] = copy.deepcopy(observed[key])
    for key in ('requested_reasoning', 'resolved_reasoning', 'observed_reasoning',
                'reasoning_verification_status', 'reasoning_support_evidence',
                'emitted_reasoning_parameters', 'reasoning_token_usage_or_null'):
        if key in verification:
            runtime[key] = copy.deepcopy(verification[key])
    runtime['observed_model_evidence'] = ({'actual_model': verification['actual_model'],
        'verified': verification.get('model_verified') is True, 'source_type': verification.get('model_identity_source')}
        if verification.get('actual_model') is not None else None)
    runtime['failure_and_cleanup_evidence'] = {'classification': result.get('guard_classification'),
        'returncode': result.get('returncode'), 'timed_out': result.get('timed_out'),
        'cancelled': result.get('cancelled'), 'process_cleanup': copy.deepcopy(result.get('process_cleanup')),
        'gateway_cleanup': copy.deepcopy(observed.get('cleanup_evidence'))}
    public = copy.deepcopy(runtime['candidate_selection'])
    public.update({key: runtime[key] for key in ('cli_version', 'observed_model_evidence',
        'observed_reasoning', 'reasoning_verification_status', 'reasoning_token_usage_or_null',
        'exposure_verification_status', 'actual_skill_reads', 'per_server_tool_calls', 'observed_tool_surface')})
    for key in ('resolved_reasoning', 'reasoning_support_evidence', 'emitted_reasoning_parameters'):
        if key in runtime:
            public[key] = runtime[key]
    public['cleanup_verified'] = (result.get('process_cleanup', {}).get('verified_stopped') is True
                                  and (observed.get('cleanup_evidence') or {}).get('verified_stopped') is True)
    runtime['candidate_selection'] = project_candidate_evidence(public)
    return runtime['candidate_selection']


def finalize_study_run(template, row, *, pipeline=None):
    """Derive study completion statistics without editing formal score evidence."""
    result = copy.deepcopy(template)
    if row.get('negative_control_no_cad'):
        result['negative_control_no_cad'] = True
    native = 'native_document' in row
    exported = bool(row.get('trusted_step')) and native
    coverage = row.get('measurement_weight_coverage') if row.get('evaluation_complete') else None
    coverage_is_numeric = type(coverage) in (int, float) and math.isfinite(coverage)
    score = row.get('final_score')
    score_is_numeric = type(score) in (int, float) and math.isfinite(score)
    primary = bool(exported and row.get('evaluation_complete') and coverage_is_numeric and coverage == 1
                   and row.get('score_kind') not in ('policy_lower_bound', 'lower_bound')
                   and score_is_numeric)
    execution = row.get('execution', {})
    reason = row.get('failure_reason') or execution.get('guard_classification')
    cad_evidence = row.get('cad_execution_evidence', {})
    pending_cad = cad_evidence.get('pending_cad')
    cad_unknown = (any(value in ('cad_task_unknown', 'cad_workspace_unconfirmed', 'fusion_task_unknown')
                       for value in (row.get('failure_reason'), execution.get('guard_classification')))
                   or type(pending_cad) is int and pending_cad > 0
                   or bool(cad_evidence.get('pending_fusion'))
                   or cad_evidence.get('cad_workspace_unconfirmed') is True)
    result['outcome'] = dict(native_submitted=native, trusted_export=exported, primary_complete=primary,
        formal_score=score if primary else None, primary_coverage=coverage if coverage_is_numeric else None,
        policy_score_or_lower_bound=score if not primary and score_is_numeric else None,
        failure_stage=row.get('failure_stage'),
        failure_category='negative_control_no_cad' if row.get('negative_control_no_cad') else
                         (reason if reason not in (None, 'clear') else None),
        cad_unknown=cad_unknown)
    if pipeline and pipeline.get('state') == 'blocked' and row.get('state') in ('queued', 'not_started'):
        result['outcome'].update(failure_stage=pipeline.get('failure_stage'), failure_category='candidate_not_dispatched')
        result['pipeline_failure'] = {key: pipeline[key] for key in ('failure_stage', 'error') if key in pipeline}
    public = row.get('candidate_selection', {})
    result['usage'].update(actual_skill_reads=copy.deepcopy(public.get('actual_skill_reads')),
        per_server_tool_calls=copy.deepcopy(public.get('per_server_tool_calls')),
        controlled_calls=execution.get('tool_calls'), modeling_seconds=execution.get('elapsed_s'),
        input_tokens=None, output_tokens=None, reasoning_tokens=public.get('reasoning_token_usage_or_null'), cost=None)
    for key in ('observed_reasoning', 'observed_model_evidence', 'reasoning_verification_status',
                'exposure_verification_status', 'cli_version'):
        if key in public:
            result[key] = copy.deepcopy(public[key])
    return result

"""Whole-profile candidate selection, with no CLI execution or credential reads."""
import copy
import hashlib
import json
from pathlib import Path
import re

from .reasoning import normalize_reasoning

LAUNCH_FIELDS = ('provider', 'model', 'reasoning', 'cli_executable', 'credential_settings',
                 'strict_tool_isolation', 'native_tools_authorization', 'expected_actual_model')
_ID = re.compile(r'^[a-z][a-z0-9_]{0,47}$')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def load_candidate_profiles(path):
    data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if (not isinstance(data, dict) or set(data) != {'schema', 'candidate_profiles'}
            or data['schema'] != 'candidate-profiles/1' or not isinstance(data['candidate_profiles'], dict)):
        raise ValueError('candidate_profiles_invalid')
    for identifier, profile in data['candidate_profiles'].items():
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier) or not isinstance(profile, dict):
            raise ValueError('candidate_profiles_invalid')
    return data


def _normalize(source, *, explicit):
    from .adapters import PROVIDERS, capabilities
    result = {key: copy.deepcopy(source[key]) for key in LAUNCH_FIELDS if key in source}
    provider = result.get('provider', 'opencode' if not explicit else None)
    if provider not in PROVIDERS:
        raise ValueError('candidate_provider_unsupported')
    result['provider'] = provider
    if not isinstance(result.get('model'), str) or not result['model'].strip():
        raise ValueError('candidate_model_required')
    if explicit and 'reasoning' not in result:
        raise ValueError('reasoning_invalid: new profile requires reasoning')
    result['reasoning'] = normalize_reasoning(result.get('reasoning', {'mode': 'cli_default'}))
    if explicit and type(result.get('strict_tool_isolation')) is not bool:
        raise ValueError('candidate_isolation_invalid')
    if 'strict_tool_isolation' in result and type(result['strict_tool_isolation']) is not bool:
        raise ValueError('candidate_isolation_invalid')
    if result.get('strict_tool_isolation') and not capabilities()[provider]['native_tools_disabled']:
        raise ValueError('candidate_isolation_unsupported')
    if explicit and not result['strict_tool_isolation']:
        authorization = result.get('native_tools_authorization')
        if not isinstance(authorization, dict) or any(not isinstance(authorization.get(key), str)
                or not authorization[key].strip() for key in ('source', 'scope')):
            raise ValueError('native_tools_authorization_required')
        result['native_tools_authorization'] = {key: authorization[key] for key in ('source', 'scope')}
    configured = result.get('cli_executable')
    if configured is None and not explicit:
        configured = source.get(provider)
    if explicit or configured is not None:
        if not isinstance(configured, str) or not configured or not Path(configured).is_absolute():
            raise ValueError('candidate_executable_unavailable: absolute path required')
        if not Path(configured).is_file():
            raise ValueError('candidate_executable_unavailable')
        result['cli_executable'] = str(Path(configured).resolve())
    for key in ('credential_settings', 'expected_actual_model'):
        if key in result and result[key] is not None and (not isinstance(result[key], str) or not result[key].strip()):
            raise ValueError('candidate_profile_invalid: ' + key)
    if result.get('credential_settings') is not None and not Path(result['credential_settings']).is_absolute():
        raise ValueError('candidate_profile_invalid: credential_settings must be an absolute reference')
    return result


def resolve_candidate_profile(config, *, profile_id=None, reasoning_override=None):
    if not isinstance(config, dict):
        raise ValueError('candidate_profile_invalid')
    selected = profile_id if profile_id is not None else config.get('candidate_profile')
    explicit = selected is not None
    if explicit:
        if not isinstance(selected, str) or not _ID.fullmatch(selected):
            raise ValueError('candidate_profile_unknown')
        profiles = config.get('candidate_profiles', {})
        if not isinstance(profiles, dict) or selected not in profiles or not isinstance(profiles[selected], dict):
            raise ValueError('candidate_profile_unknown')
        source = profiles[selected]
    else:
        source = config
    result = _normalize(source, explicit=explicit)
    base_digest = _digest(result)
    origin = 'profile' if explicit else 'configured' if 'reasoning' in source else 'legacy_default'
    # Only replay a saved explicit override bound to this same profile and base
    # digest. Scattered top-level reasoning on a base config is never an override.
    if (reasoning_override is None and explicit and config.get('candidate_profile_id') == selected
            and config.get('reasoning_origin') == 'explicit_override' and 'reasoning_override' in config):
        if config.get('base_candidate_profile_sha256') != base_digest:
            raise ValueError('candidate_profile_override_mismatch')
        reasoning_override = config['reasoning_override']
    if reasoning_override is not None:
        result['reasoning'] = normalize_reasoning(reasoning_override)
        origin = 'explicit_override'
    final_digest = _digest(result)
    result.update(candidate_profile_id=selected if explicit else 'legacy_unprofiled',
                  selection_origin='candidate_profile' if explicit else 'legacy_default',
                  reasoning_origin=origin, base_candidate_profile_sha256=base_digest,
                  candidate_profile_sha256=final_digest)
    if reasoning_override is not None:
        result['reasoning_override'] = copy.deepcopy(result['reasoning'])
    return result


def apply_candidate_selection(config, overrides):
    """Derive a fresh management config; overrides contain only selection fields."""
    if not isinstance(overrides, dict):
        raise ValueError('candidate_selection_conflict')
    if (overrides.get('candidate_profile', config.get('candidate_profile')) is not None
            and any(key in overrides for key in ('provider', 'model'))):
        raise ValueError('candidate_selection_conflict')
    target = overrides.get('provider', config.get('provider', 'opencode'))
    if 'provider' in overrides and target != config.get('provider', 'opencode'):
        raise ValueError('candidate_profile_required_for_provider_switch')
    derived = copy.deepcopy(config)
    selected = overrides.get('candidate_profile', config.get('candidate_profile'))
    if selected is not None:
        resolved = resolve_candidate_profile(config, profile_id=selected, reasoning_override=overrides.get('reasoning'))
        for key in (*LAUNCH_FIELDS, 'opencode', 'codex', 'claude', 'reasoning_support', 'cli_version',
                    'reasoning_override', 'reasoning_origin', 'candidate_profile_id', 'selection_origin',
                    'base_candidate_profile_sha256', 'candidate_profile_sha256'):
            derived.pop(key, None)
        derived.update(resolved)
        derived['candidate_profile'] = selected
    else:
        for key in ('provider', 'model', 'reasoning'):
            if key in overrides:
                derived[key] = copy.deepcopy(overrides[key])
        derived.update(resolve_candidate_profile(derived))
    return derived


def freeze_candidate_profile(resolved, output_dir):
    """Create a private immutable snapshot. Authentication files are never copied."""
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=False)
    launch = {key: copy.deepcopy(resolved[key]) for key in LAUNCH_FIELDS if key in resolved}
    metadata_fields = ('candidate_profile_id', 'selection_origin', 'reasoning_origin',
                       'base_candidate_profile_sha256', 'reasoning_override')
    snapshot = {'schema': 'candidate-profile-snapshot/1', **launch,
                **{key: copy.deepcopy(resolved[key]) for key in metadata_fields if key in resolved}}
    snapshot['candidate_profile_sha256'] = _digest(launch)
    snapshot.update(configured_executable=launch.get('cli_executable'), resolved_executable=None,
                    executable_sha256=None, cli_version=None, requested_model=launch['model'],
                    observed_model_evidence=None, model_connection_identity=None,
                    requested_reasoning=launch['reasoning'], observed_reasoning=None,
                    credential_source_without_values={'reference': launch.get('credential_settings'),
                                                      'credential_values_logged': False},
                    requested_isolation=launch.get('strict_tool_isolation'), observed_isolation_evidence=None,
                    configuration_valid=True, cli_available=None, auth_status='not_checked',
                    live_model_status='not_called')
    # The runner binds the probed native executable/version before freezing the
    # final launch. A static configuration hash is not that observed identity.
    snapshot['resolved_candidate_launch_sha256'] = None
    (root / 'candidate-profile.json').write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return snapshot

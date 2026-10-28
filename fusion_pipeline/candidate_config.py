"""Shared static candidate configuration for generator, management and runner.

resolve_candidate_configuration(config, overrides=None, *, require_reasoning_verified=False) returns a deep copied,
validated derived config. Only an explicit management capability override removes
legacy skill fields. Calling it again without overrides preserves that decision.
No CLI, model, CAD, auxiliary server or authentication value is accessed here.
"""
import copy
import json
import re

from .candidate_profiles import apply_candidate_selection
from .capabilities import LEGACY_SKILL_FIELDS, load_registry, normalize_selection
from .reasoning import normalize_reasoning, resolve_reasoning


def project_candidate_evidence(value):
    """Project runtime candidate_selection with nested allowlists, never raw manifests."""
    if not isinstance(value, dict):
        return {}
    scalar_fields = ('candidate_profile_id', 'candidate_profile_sha256', 'provider', 'reasoning_origin',
                     'requested_model', 'reasoning_verification_status', 'reasoning_token_usage_or_null',
                     'exposure_verification_status', 'cli_version', 'support_status')
    result = {key: copy.deepcopy(value[key]) for key in scalar_fields
              if key in value and type(value[key]) in (str, int, float, bool, type(None))}
    supported = value.get('supported_values')
    if supported is None and 'supported_values' in value:
        result['supported_values'] = None
    elif isinstance(supported, list) and all(isinstance(item, str) for item in supported):
        result['supported_values'] = list(supported)
    nested = {
        'requested_reasoning': ('mode', 'control', 'value'),
        'resolved_reasoning': ('mode', 'control', 'value'),
        'observed_reasoning': ('control', 'value'),
        'observed_model_evidence': ('model', 'actual_model', 'requested_model', 'provider', 'verified',
                                    'status', 'source_type', 'evidence_sha256'),
        'reasoning_support_evidence': ('provider', 'model', 'cli_version', 'status', 'evidence_sha256'),
        'capability_selection': ('selection_origin', 'study_kind', 'skill_delivery', 'exposure_mode',
                                 'selection_sha256'),
    }
    for field, allowed in nested.items():
        item = value.get(field)
        if item is None and field in value:
            result[field] = None
        elif isinstance(item, dict):
            result[field] = {key: copy.deepcopy(item[key]) for key in allowed if key in item
                             and type(item[key]) in (str, int, float, bool, type(None))}
    selection = value.get('capability_selection')
    if isinstance(selection, dict):
        for field in ('skills', 'mcp_servers'):
            items = selection.get(field)
            if isinstance(items, list) and all(isinstance(item, str) for item in items):
                result['capability_selection'][field] = list(items)
    evidence = value.get('reasoning_support_evidence')
    if isinstance(evidence, dict) and isinstance(evidence.get('controls'), dict):
        result['reasoning_support_evidence']['controls'] = {
            key: list(items) for key, items in evidence['controls'].items()
            if key in ('effort', 'variant') and isinstance(items, list)
            and all(isinstance(item, str) for item in items)}
    if type(value.get('cleanup_verified')) is bool:
        result['cleanup_verified'] = value['cleanup_verified']
    surface = value.get('observed_tool_surface')
    if isinstance(surface, (list, dict)):
        names = [item.get('name') if isinstance(item, dict) else item for item in surface]
        result['observed_tool_surface'] = [name for name in names if isinstance(name, str)
                                          and re.fullmatch(r'[A-Za-z][A-Za-z0-9_.:-]{0,127}', name)]
    calls = value.get('per_server_tool_calls')
    if isinstance(calls, dict):
        result['per_server_tool_calls'] = {key: count for key, count in calls.items()
            if isinstance(key, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,47}', key)
            and type(count) is int and count >= 0}
    reads = value.get('actual_skill_reads')
    if isinstance(reads, list):
        result['actual_skill_reads'] = []
        for read in reads:
            if not isinstance(read, dict) or not isinstance(read.get('id'), str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,47}', read['id']):
                continue
            item = {'id': read['id']}
            resource = read.get('resource_id')
            if resource is None or isinstance(resource, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,47}', resource):
                item['resource_id'] = resource
            if isinstance(read.get('sha256'), str) and re.fullmatch(r'[a-fA-F0-9]{64}', read['sha256']):
                item['sha256'] = read['sha256']
            if type(read.get('count')) is int and read['count'] >= 0:
                item['count'] = read['count']
            result['actual_skill_reads'].append(item)
    parameters = value.get('emitted_reasoning_parameters')
    if isinstance(parameters, list):
        result['emitted_reasoning_parameters'] = []
        if len(parameters) == 2 and all(isinstance(item, str) for item in parameters):
            flag, argument = parameters
            if flag in ('--effort', '--variant') and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', argument):
                result['emitted_reasoning_parameters'] = list(parameters)
            elif flag == '-c' and argument.startswith('model_reasoning_effort='):
                try:
                    level = json.loads(argument.split('=', 1)[1])
                except ValueError:
                    level = None
                if isinstance(level, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', level):
                    result['emitted_reasoning_parameters'] = ['-c', 'model_reasoning_effort=' + json.dumps(level)]
    return result


def selected_reasoning_support(config):
    """Select maintained evidence by profile ID; legacy evidence never crosses profiles."""
    selected = config.get('candidate_profile')
    if selected is None:
        return copy.deepcopy(config.get('reasoning_support', {}))
    mapping = config.get('candidate_reasoning_support', {})
    if not isinstance(mapping, dict):
        raise ValueError('reasoning_support_unverified')
    return copy.deepcopy(mapping.get(selected, {}))


def public_candidate_selection(config):
    """Allowlisted public selection; no registry/executable/authentication paths."""
    reasoning = config.get('resolved_reasoning', {})
    selection = config.get('candidate_capability_selection', {})
    result = {key: copy.deepcopy(config[key]) for key in
              ('candidate_profile_id', 'candidate_profile_sha256', 'provider', 'reasoning_origin')
              if key in config}
    result.update(requested_model=config.get('model'), observed_model_evidence=None,
                  requested_reasoning=copy.deepcopy(config.get('reasoning')),
                  resolved_reasoning=copy.deepcopy(reasoning.get('resolved')),
                  observed_reasoning=None, reasoning_verification_status='not_observed',
                  support_status=reasoning.get('support_status'),
                  supported_values=copy.deepcopy(reasoning.get('supported_values')),
                  reasoning_token_usage_or_null=None,
                  capability_selection={key: copy.deepcopy(selection[key]) for key in
                    ('selection_origin', 'study_kind', 'skill_delivery', 'exposure_mode',
                     'skills', 'mcp_servers', 'selection_sha256') if key in selection})
    evidence = reasoning.get('support_evidence')
    result['reasoning_support_evidence'] = ({key: copy.deepcopy(evidence[key]) for key in
        ('provider', 'model', 'cli_version', 'status', 'controls', 'evidence_sha256') if key in evidence}
        if isinstance(evidence, dict) else None)
    return project_candidate_evidence(result)


def unverified_reasoning_request(candidate):
    """Keep a valid request inspectable without resolving or emitting parameters."""
    result = resolve_reasoning(dict(candidate, reasoning={'mode': 'cli_default'}), {})
    request = normalize_reasoning(candidate['reasoning'])
    result.update(requested=request, requested_reasoning=copy.deepcopy(request),
                  support_status='not_verified', supported_values=None)
    return result


def resolve_candidate_configuration(config, overrides=None, *, require_reasoning_verified=False):
    """Return a fresh derived config. Formal execution must require verified reasoning."""
    if not isinstance(config, dict) or overrides is not None and not isinstance(overrides, dict):
        raise ValueError('candidate_selection_invalid')
    overrides = overrides or {}
    derived = apply_candidate_selection(config, {key: overrides[key] for key in
        ('candidate_profile', 'provider', 'model', 'reasoning') if key in overrides})
    if derived.get('candidate_profile') is None:
        if 'reasoning' in overrides:
            derived['reasoning_origin'] = 'explicit_override'
            derived['reasoning_override'] = copy.deepcopy(derived['reasoning'])
        elif (not any(key in overrides for key in ('provider', 'model'))
              and config.get('candidate_profile_sha256') == derived['candidate_profile_sha256']
              and 'reasoning_origin' in config):
            derived['reasoning_origin'] = config['reasoning_origin']
    if 'cad_backend' in overrides:
        derived['cad_backend'] = overrides['cad_backend']
    if 'candidate_capabilities' in overrides:
        removed = [key for key in LEGACY_SKILL_FIELDS if key in derived]
        for key in removed:
            derived.pop(key)
        derived['candidate_capabilities'] = copy.deepcopy(overrides['candidate_capabilities'])
        derived['legacy_skill_fields_removed_by_explicit_override'] = removed
    registry = {}
    if 'candidate_capabilities' in derived:
        if not isinstance(derived.get('capability_registry'), str) or not derived['capability_registry']:
            raise ValueError('capability_registry_required')
        registry = load_registry(derived['capability_registry'])
    derived['candidate_capability_selection'] = normalize_selection(
        derived, registry, cad_backend=derived.get('cad_backend', 'fusion'))
    support = selected_reasoning_support(derived)
    if not isinstance(support, dict):
        raise ValueError('reasoning_support_invalid')
    if any(key in support and support[key] is not None and not isinstance(support[key], str)
           for key in ('provider', 'model', 'cli_version', 'status', 'evidence_sha256',
                       'resolved_executable', 'executable_sha256')):
        raise ValueError('reasoning_support_invalid')
    if 'controls' in support and (not isinstance(support['controls'], dict) or any(
            not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values)
            for values in support['controls'].values())):
        raise ValueError('reasoning_support_invalid')
    derived['reasoning_support'] = support
    try:
        resolved = resolve_reasoning(derived, support)
        resolved['support_status'] = 'verified' if derived['reasoning']['mode'] == 'explicit' else 'not_requested'
        resolved['supported_values'] = (copy.deepcopy(support['controls'][derived['reasoning']['control']])
                                        if derived['reasoning']['mode'] == 'explicit' else None)
    except ValueError as error:
        if require_reasoning_verified or str(error) != 'reasoning_support_unverified':
            raise
        resolved = unverified_reasoning_request(derived)
    derived['resolved_reasoning'] = resolved
    derived['candidate_selection'] = public_candidate_selection(derived)
    return derived

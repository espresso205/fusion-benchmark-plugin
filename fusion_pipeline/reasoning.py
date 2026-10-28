"""Validate reasoning requests and map verified controls to native argv.

Support evidence is supplied by the maintenance side, bound to a concrete CLI
version and model. This module never probes a CLI or infers internal budgets.
"""
import copy
import hashlib
import json
import re


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def normalize_reasoning(value):
    if not isinstance(value, dict):
        raise ValueError('reasoning_invalid: object required')
    if value == {'mode': 'cli_default'}:
        return dict(value)
    if (set(value) != {'mode', 'control', 'value'} or value.get('mode') != 'explicit'
            or value.get('control') not in ('effort', 'variant')
            or not isinstance(value.get('value'), str) or not value['value'].strip()
            or value['value'] != value['value'].strip() or len(value['value']) > 128
            or any(ord(character) < 32 for character in value['value'])):
        raise ValueError('reasoning_invalid: expected cli_default or explicit control/value')
    return dict(value)


def _variant_options(options):
    # Only reviewed generation settings: never tools, provider credentials,
    # plugin configuration, permissions, or arbitrary nested provider settings.
    allowed = {'reasoningEffort', 'reasoningSummary', 'textVerbosity', 'verbosity',
               'temperature', 'topP', 'topK', 'maxOutputTokens', 'maxTokens',
               'thinking', 'include', 'reasoningBudget'}
    if not isinstance(options, dict) or not options or set(options) - allowed:
        raise ValueError('reasoning_variant_options_invalid')
    thinking = options.get('thinking')
    if thinking is not None and (not isinstance(thinking, dict)
            or set(thinking) - {'type', 'budgetTokens'}
            or any(type(value) not in (str, int) for value in thinking.values())):
        raise ValueError('reasoning_variant_options_invalid')
    for key, value in options.items():
        if key == 'thinking':
            continue
        if type(value) not in (str, int, float, bool, list):
            raise ValueError('reasoning_variant_options_invalid')
        if isinstance(value, list) and any(not isinstance(item, str) for item in value):
            raise ValueError('reasoning_variant_options_invalid')
    try:
        _digest(options)
    except (TypeError, ValueError):
        raise ValueError('reasoning_variant_options_invalid') from None
    return copy.deepcopy(options)


def resolve_reasoning(candidate, support):
    request = normalize_reasoning(candidate.get('reasoning', {'mode': 'cli_default'}))
    provider = candidate.get('provider')
    if provider not in ('claude', 'codex', 'opencode'):
        raise ValueError('reasoning_unsupported: provider')
    result = {'requested': request, 'resolved': None, 'native_parameters': [],
              'support_evidence': None, 'requested_reasoning': request,
              'resolved_reasoning': None, 'reasoning_origin': candidate.get('reasoning_origin', 'configured'),
              'reasoning_support_evidence': None, 'emitted_reasoning_parameters': [],
              'observed_reasoning': None, 'reasoning_verification_status': 'not_observed',
              'variant_resolved_options': None, 'variant_resolved_options_sha256': None,
              'reasoning_token_usage_or_null': None, 'reasoning_only_comparison': None}
    if request['mode'] == 'cli_default':
        return result
    expected_control = 'variant' if provider == 'opencode' else 'effort'
    if request['control'] != expected_control:
        raise ValueError('reasoning_unsupported: provider control')
    if isinstance(support, dict) and support.get('status') == 'unsupported':
        raise ValueError('reasoning_unsupported')
    if (not isinstance(support, dict) or support.get('status') != 'verified'
            or support.get('provider') != provider or support.get('model') != candidate.get('model')
            or not isinstance(support.get('cli_version'), str) or not support['cli_version'].strip()
            or (candidate.get('cli_version') is not None and candidate['cli_version'] != support['cli_version'])
            or not isinstance(support.get('evidence_sha256'), str)
            or not re.fullmatch('[a-fA-F0-9]{64}', support['evidence_sha256'])
            or not isinstance(support.get('controls'), dict)):
        raise ValueError('reasoning_support_unverified')
    values = support['controls'].get(request['control'])
    if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
        raise ValueError('reasoning_support_unverified')
    if request['value'] not in values:
        raise ValueError('reasoning_unsupported')
    native = {'codex': ['-c', 'model_reasoning_effort=' + json.dumps(request['value'])],
              'claude': ['--effort', request['value']],
              'opencode': ['--variant', request['value']]}[provider]
    # Copy only non-sensitive evidence fields, not arbitrary source documents.
    evidence = {key: copy.deepcopy(support[key]) for key in
                ('provider', 'model', 'cli_version', 'status', 'controls', 'evidence_sha256', 'source', 'support_source')
                if key in support}
    result.update(resolved=dict(request), resolved_reasoning=dict(request), native_parameters=native,
                  emitted_reasoning_parameters=list(native), support_evidence=evidence,
                  reasoning_support_evidence=evidence, reasoning_verification_status='parameter_emitted_only')
    if provider == 'opencode':
        options = _variant_options(support.get('variant_options', {}).get(request['value']))
        digest = _digest(options)
        declared = support.get('variant_options_sha256', {}).get(request['value'])
        if declared is not None and declared != digest:
            raise ValueError('reasoning_variant_options_invalid: digest mismatch')
        result.update(variant_resolved_options=options, variant_resolved_options_sha256=digest,
                      reasoning_only_comparison=not bool(set(options) - {'reasoningEffort', 'thinking', 'reasoningBudget'}))
    else:
        result['reasoning_only_comparison'] = True
    return result


def observe_reasoning(resolved, observed=None, *, source=None):
    """Attach actual metadata only; a successful exit or emitted argv is insufficient."""
    result = copy.deepcopy(resolved)
    if observed is None:
        return result
    if (not isinstance(observed, dict) or set(observed) != {'control', 'value'}
            or observed['control'] not in ('effort', 'variant')
            or not isinstance(observed['value'], str) or not observed['value'] or not source):
        raise ValueError('reasoning_observation_invalid')
    result['observed_reasoning'] = {**observed, 'source': source}
    expected = resolved.get('resolved')
    result['reasoning_verification_status'] = ('observed' if expected is None or
        (observed['control'] == expected['control'] and observed['value'] == expected['value']) else 'mismatch')
    return result

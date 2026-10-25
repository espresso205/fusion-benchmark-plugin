"""Read completed result JSON without opening inputs or invoking evaluators.

The reader validates stored evidence, arithmetic and identity. It does not
recompute geometry, repair historical results, or turn absent measurements into
measured zeros. Unsupported or internally inconsistent sources raise ValueError.
"""
import hashlib
import json
import math
from pathlib import Path
import re

V3_PROTOCOL = 'fusion-step-adapted-v3.0'
V4_PROTOCOL = 'fusion-step-adapted-v4.0'
V4_SCORED = ('shape','dimension','mass')
V4_DIAGNOSTIC = ('distance','face_match','section')
LEGACY_PROTOCOL = 'legacy-20261017'
METRICS = ('shape', 'dimension', 'distance', 'mass', 'face_match', 'section')
MODULES = ('validity', *METRICS)
HASH_ROLES = {'candidate', 'gt', 'annotations'}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _object(value, context):
    _require(isinstance(value, dict), context + ': object required')
    return value


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _score(value, context, nullable=True):
    _require((nullable and value is None) or (_number(value) and 0 <= value <= 1),
             context + ': finite score in [0, 1]' + (' or null' if nullable else '') + ' required')
    return None if value is None else float(value)


def _same_number(actual, expected, context, tolerance=1e-12):
    if actual is None or expected is None:
        _require(actual is expected, context + ': null/value mismatch')
    else:
        _require(_number(actual) and _number(expected)
                 and math.isclose(actual, expected, rel_tol=0., abs_tol=tolerance),
                 context + ': numerical mismatch')


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        _require(key not in out, 'duplicate JSON key: ' + key)
        out[key] = value
    return out


def _float(value):
    result = float(value)
    _require(math.isfinite(result), 'nonfinite JSON number: ' + value)
    return result


def _constant(value):
    raise ValueError('nonfinite JSON constant: ' + value)


class _Source:
    def __init__(self, root):
        self.root = root.resolve()
        self.files = {}
        self.warnings = []

    def read(self, path):
        path = path.resolve()
        _require(path.is_relative_to(self.root), 'source JSON escapes result directory: ' + str(path))
        try:
            raw = path.read_bytes()
            doc = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_pairs,
                             parse_constant=_constant, parse_float=_float)
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError(f'{path}: {exc}') from exc
        digest = hashlib.sha256(raw).hexdigest()
        old = self.files.get(str(path))
        _require(old is None or old == digest, 'source changed during reading: ' + str(path))
        self.files[str(path)] = digest
        return _object(doc, str(path))


def _case_map(rows, context):
    _require(isinstance(rows, list), context + ': cases must be a list')
    result = {}
    for row in rows:
        _object(row, context + ' case')
        case = row.get('case')
        _require(isinstance(case, str) and bool(case) and case not in ('.', '..')
                 and not any(c in case for c in '/\\:\x00') and case == case.strip(),
                 context + ': unsafe or invalid case identity')
        _require(case not in result, context + ': duplicate case ' + case)
        result[case] = row
    return result


def _weights(value, context):
    _object(value, context)
    _require(set(value) == set(METRICS) and all(_number(v) and v >= 0 for v in value.values())
             and math.isclose(math.fsum(value.values()), 1., rel_tol=0., abs_tol=1e-12),
             context + ': six finite nonnegative weights summing to one required')
    return {name: float(value[name]) for name in METRICS}


def _hashes(value, context):
    _require(isinstance(value, dict) and set(value) == HASH_ROLES
             and all(isinstance(v, str) and re.fullmatch('[0-9a-f]{64}', v) for v in value.values()),
             context + ': candidate/gt/annotations SHA256 hashes required')
    return value


def _identity(row, case, digest, context, protocol=V3_PROTOCOL, coordinate_mode=None):
    _require(row.get('schema_version') == ('4.0' if protocol==V4_PROTOCOL else '3.0') and row.get('protocol_id') == protocol,
             context + ': schema/protocol mismatch')
    if protocol==V4_PROTOCOL:
        _require(row.get('coordinate_mode') == coordinate_mode, context + ': coordinate mode mismatch')
    if case is not None:
        _require(row.get('case') == case, context + ': case identity mismatch')
    _require(row.get('run_manifest_sha256') == digest, context + ': manifest hash mismatch')


def _reasons(assessment):
    reasons = [assessment['status']]
    for item in assessment.get('reasons', []):
        if isinstance(item, str):
            reasons.append(item)
        elif isinstance(item, dict):
            reasons.append(': '.join(str(item[key]) for key in ('module', 'status', 'detail')
                                     if item.get(key) is not None))
    return list(dict.fromkeys(reason for reason in reasons if reason))


def _measured_from_module(module, name):
    quality = module.get('quality', {})
    _object(quality, name + ' quality')
    score = module.get('score')
    valid = (module.get('status') == 'ok' and quality.get('measurement_valid') is True
             and _number(score) and 0 <= score <= 1)
    raw = module.get('raw', {})
    _object(raw, name + ' raw')
    if name == 'dimension' and 'gt_self_check_total' in raw:
        total = raw['gt_self_check_total']
        valid = valid and _number(total) and total > 0 and raw.get('gt_self_check_passed') == total
    return float(score) if valid else None


def _validation_from_module(module):
    quality = module.get('quality', {})
    raw = module.get('raw', {})
    candidate = raw.get('candidate', {})
    invalid = module.get('status') == 'candidate_invalid' and quality.get('candidate_invalid_confirmed') is True
    evidence = quality.get('candidate_valid') is True and isinstance(candidate, dict) and candidate.get('healthy') is True
    return bool(not invalid and evidence and (
        module.get('status') == 'ok' and module.get('score') == 1. and quality.get('measurement_valid') is True
        or module.get('status') == 'reference_invalid'
        or module.get('status') == 'evaluator_error' and raw.get('stage') == 'gt_validation'))


def _v3_record(source, case, assessment, expected_hashes, weights, digest, protocol=V3_PROTOCOL, coordinate_mode=None):
    is_v4 = protocol==V4_PROTOCOL
    scored = V4_SCORED if is_v4 else METRICS
    _identity(assessment, case, digest, case, protocol, coordinate_mode)
    aggregate = source.read(source.root / case / 'aggregate.json')
    _require(aggregate == assessment, case + ': summary and final aggregate disagree')
    _require(_weights(assessment.get('weights'), case + ' weights') == weights, case + ': weights mismatch')
    participates = assessment.get('participates_in_scoring')
    _require(type(participates) is bool, case + ': participates_in_scoring boolean required')
    validation = assessment.get('validation_passed')
    _require(validation is None or type(validation) is bool, case + ': validation_passed boolean/null required')
    status = assessment.get('status')
    _require(isinstance(status, str) and status, case + ': status required')
    baseline = _score(assessment.get('final_score'), case + ' final_score')
    _require(participates == (baseline is not None), case + ': participation and final_score disagree')
    _require(not participates or validation is True, case + ': participant has no validation pass')
    details = _object(assessment.get('metrics', {}), case + ' metrics')
    # Outer execution failures can have no assessment of their partial module files.
    unassessed = not details and not participates and status == 'evaluator_error'
    _require(unassessed or set(details) == set(MODULES), case + ': seven metric assessments required')
    source_scores = _object(assessment.get('module_scores', {}), case + ' module_scores')
    source_awards = _object(assessment.get('awarded_module_scores', {}), case + ' awarded_module_scores')
    measured, awards, statuses, module_rows = {}, {}, {}, {}
    pose = assessment.get('pose',{}) if is_v4 else {}
    if is_v4:
        for name,field in (('pose','pose'),('world_shape','coordinate_diagnostics')):
            path = source.root / case / 'results' / (name+'.json')
            if path.exists():
                extra = source.read(path)
                _identity(extra,case,digest,case+'/'+name,protocol,coordinate_mode)
                _require(extra.get('input_hashes')==expected_hashes,case+'/'+name+': input hashes mismatch')
                _require(assessment.get(field)==extra,case+'/'+name+': aggregate evidence mismatch')
            else:
                _require(not assessment.get(field),case+'/'+name+': absent evidence retained in aggregate')
    for name in MODULES:
        context = f'{case}/{name}'
        metric = _object(details.get(name, {}), context + ' metric')
        value = _score(metric.get('measured_score'), context + ' measured_score')
        award = _score(metric.get('awarded_score'), context + ' awarded_score')
        for key in ('value', 'score'):
            if key in metric:
                _same_number(metric[key], value, context + ' ' + key)
        if not unassessed:
            _require(name in source_scores, context + ': module_scores missing entry')
            _same_number(source_scores[name], value, context + ' module_scores')
            expected_award = (value if value is not None else 0.) if participates and (not is_v4 or name in scored) else None
            _same_number(award, expected_award, context + ' award policy')
            if is_v4:
                role = 'scored' if name in scored else 'validation' if name=='validity' else 'diagnostic'
                _require(metric.get('metric_role')==role,context+': metric role mismatch')
        module_path = source.root / case / 'results' / (name + '.json')
        if module_path.exists():
            module = source.read(module_path)
            module_rows[name] = module
            _identity(module, case, digest, context, protocol, coordinate_mode)
            _require(module.get('angle', name) == name, context + ': angle identity mismatch')
            _require(module.get('input_hashes') == expected_hashes, context + ': input hashes mismatch')
            if is_v4 and module.get('status')=='ok':
                evaluated = dict(expected_hashes)
                if coordinate_mode=='geometry' and name in METRICS and name!='mass':
                    _require(pose.get('status')=='ok' and pose.get('quality',{}).get('measurement_valid') is True,
                             context+': measured geometry without qualified pose')
                    evaluated['candidate']=pose.get('raw',{}).get('evaluation_candidate_sha256')
                    _hashes(evaluated,context+' evaluation hashes')
                _require(module.get('evaluation_input_hashes')==evaluated,context+': evaluation input identity mismatch')
            if not unassessed:
                _require(metric.get('status') == module.get('status'), context + ': status mismatch')
                _same_number(value, _measured_from_module(module, name), context + ' measured evidence')
                approximate = module.get('quality', {}).get('scored_approximate') is True
                expected_source = (('approximate_measurement' if approximate else 'measurement')
                                   if value is not None else 'diagnostic_unavailable' if is_v4 and name not in scored else 'unavailable_policy_floor')
                _require(metric.get('score_source') == expected_source, context + ': score_source mismatch')
                diagnostic = module.get('score') if value is None and _number(module.get('score')) else None
                _same_number(metric.get('diagnostic_value'), diagnostic, context + ' diagnostic score')
                for key in ('backend', 'parameters', 'uncertainty', 'timing'):
                    _require(metric.get(key) == module.get(key), context + ': ' + key + ' mismatch')
                evidence = _object(metric.get('evidence'), context + ' evidence')
                _require(evidence.get('input_hashes') == expected_hashes, context + ': evidence input hashes mismatch')
                _require(evidence.get('run_manifest_sha256') == digest, context + ': evidence manifest hash mismatch')
                _require(evidence.get('result_file') == f'results/{name}.json', context + ': evidence path mismatch')
                if is_v4:
                    _require(evidence.get('evaluation_input_hashes') == module.get('evaluation_input_hashes'),
                             context+': evaluation evidence identity mismatch')
                if name == 'validity':
                    _require(validation == _validation_from_module(module), context + ': validation evidence mismatch')
        else:
            _require(value is None and (unassessed or metric.get('status') == 'missing'),
                     context + ': missing published module evidence')
            evidence = metric.get('evidence', {})
            _object(evidence, context + ' evidence')
            _require(evidence.get('input_hashes') is None and evidence.get('run_manifest_sha256') is None,
                     context + ': missing module retains evidence identity')
            if name == 'validity':
                _require(validation is not True, context + ': missing validation evidence')
        statuses[name] = metric.get('status', module_rows.get(name, {}).get('status', 'missing'))
        if name in METRICS:
            measured[name], awards[name] = value, award
            if not unassessed:
                _require(name in source_awards, context + ': awarded_module_scores missing entry')
                _same_number(source_awards[name], award, context + ' awarded_module_scores')
    coverage = math.fsum(weights[n] for n in METRICS if measured[n] is not None)
    if not unassessed:
        _same_number(assessment.get('measurement_weight_coverage'), coverage, case + ' coverage')
        unavailable = [name for name in scored if measured[name] is None]
        _require(assessment.get('unavailable_metrics') == unavailable, case + ': unavailable_metrics mismatch')
        if is_v4:
            _require(assessment.get('diagnostic_unavailable_metrics') == [n for n in V4_DIAGNOSTIC if measured[n] is None],
                     case+': diagnostic_unavailable_metrics mismatch')
    kind = 'policy_lower_bound' if participates and coverage < 1. - 1e-12 else 'measured' if participates else 'unscored'
    # Even zero-weight missing metrics are declared unavailable in the v3 contract.
    if participates and any(measured[n] is None for n in scored):
        kind = 'policy_lower_bound'
    _require(assessment.get('score_kind') == kind, case + ': score_kind mismatch')
    if participates:
        _same_number(baseline, math.fsum(weights[n] * awards[n] for n in scored), case + ' final arithmetic')
        _require(status in ('ok', 'scored_with_unavailable_metrics'), case + ': invalid participant status')
        interval = assessment.get('score_interval')
        _require(isinstance(interval, list) and len(interval) == 2, case + ': score_interval required')
        _same_number(interval[0], baseline, case + ' interval lower')
        upper = min(1., math.fsum([baseline, *(weights[n] for n in scored if measured[n] is None)]))
        _same_number(interval[1], upper, case + ' interval upper')
    else:
        _require(status not in ('ok', 'scored_with_unavailable_metrics'), case + ': unscored case has scored status')
        _require(assessment.get('score_interval') is None, case + ': unscored case has score interval')
    profiles = _object(assessment.get('scores'), case + ' scores')
    profile = _object(profiles.get(protocol), case + ' score profile')
    _same_number(profile.get('value'), baseline, case + ' score profile value')
    _require(profile.get('score_kind') == kind, case + ': score profile kind mismatch')
    _require(profile.get('aggregation_status') == ('scored' if participates else 'blocked'),
             case + ': score profile aggregation status mismatch')
    return dict(case=case, baseline_score=baseline, measured_scores=measured, awarded_scores=awards,
                participates=participates, validation_passed=validation, status=status, score_kind=kind,
                original_weight_coverage=coverage, exclusion_reasons=[] if participates else _reasons(assessment),
                input_hashes=dict(expected_hashes), execution=_object(assessment.get('execution', {}), case + ' execution'),
                metric_statuses=statuses, metric_details=details, source_assessment=assessment,
                **({'coordinate_mode':coordinate_mode} if is_v4 else {}))


def _read_v3(source, summary, protocol=V3_PROTOCOL):
    manifest_path = source.root / 'manifest.json'
    manifest = source.read(manifest_path)
    digest = source.files[str(manifest_path.resolve())]
    is_v4 = protocol==V4_PROTOCOL
    coordinate_mode = manifest.get('coordinate_mode') if is_v4 else None
    if is_v4:
        _require(coordinate_mode in ('geometry','strict'),'manifest coordinate_mode invalid')
    _identity(summary, None, digest, 'summary', protocol, coordinate_mode)
    _require(manifest.get('schema_version') == ('4.0' if is_v4 else '3.0') and manifest.get('protocol_id') == protocol,
             'manifest schema/protocol mismatch')
    config = _object(manifest.get('weights'), 'manifest weights')
    _require(config.get('protocol_id', protocol) == protocol, 'weights protocol mismatch')
    weights = _weights(config.get('weights', config), 'manifest weights')
    if is_v4:
        _require(all(weights[n]>0 for n in V4_SCORED) and all(weights[n]==0 for n in V4_DIAGNOSTIC),
                 'v4 requires three positive scored weights and zero diagnostic weights')
    cases = _case_map(summary.get('cases'), 'summary')
    expected = _case_map(manifest.get('cases'), 'manifest')
    _require(set(cases) == set(expected), 'summary/manifest case identity sets differ')
    records = [_v3_record(source, case, row, _hashes(expected[case].get('expected_hashes'), case), weights, digest, protocol, coordinate_mode)
               for case, row in cases.items()]
    participants = [row for row in records if row['participates']]
    for key, value in (('total_case_count', len(records)), ('participating_case_count', len(participants))):
        if key in summary:
            _require(type(summary[key]) is int and summary[key] == value, 'summary ' + key + ' mismatch')
    if 'batch_mean' in summary:
        expected_mean = math.fsum(r['baseline_score'] for r in participants) / len(participants) if participants else None
        _same_number(summary['batch_mean'], expected_mean, 'summary batch_mean')
    if participants:
        _require(summary.get('code_unchanged') is True and summary.get('original_inputs_unchanged') is True,
                 'summary participants lack completed batch integrity evidence')
    return protocol, weights, records


def _read_legacy(source, final, final_path):
    _require('protocol_id' not in final and 'schema_version' not in final,
             'unrecognized legacy protocol/schema declaration')
    weights = _weights(final.get('weights'), 'legacy final weights')
    # Historical aggregation stops at the first error in the stored weight
    # mapping's insertion order, which need not match our normalized order.
    weight_order = tuple(final['weights'])
    result_dir = final_path.parent
    config_path = source.root / 'weights.json'
    if config_path.exists():
        config = source.read(config_path)
        _require(_weights(config.get('weights'), 'legacy weights') == weights, 'legacy weights disagree')
        weight_order = tuple(config['weights'])
        _require(config.get('gate', {}).get('angle') == 'validity', 'legacy validity gate required')
    else:
        source.warnings.append('Legacy weights.json is absent; weights are taken from final_scores.json.')
    _require(_object(final.get('gate'), 'legacy gate').get('angle') == 'validity', 'legacy validity gate required')
    cases = _case_map(final.get('cases'), 'legacy final')
    input_path = source.root / 'cases.json'
    if input_path.exists():
        input_cases = _case_map(source.read(input_path).get('cases'), 'legacy cases.json')
        _require(set(input_cases) == set(cases), 'legacy manifest/final case identity sets differ')
    else:
        source.warnings.append('Legacy cases.json is absent; case identities are checked across stored result files.')
    angles = {}
    for name in MODULES:
        doc = source.read(result_dir / (name + '.json'))
        _require(doc.get('angle') == name, 'legacy ' + name + ': angle identity mismatch')
        _require('protocol_id' not in doc and 'schema_version' not in doc, 'legacy angle has unexpected version identity')
        angles[name] = _case_map(doc.get('cases'), 'legacy ' + name)
        _require(set(angles[name]) == set(cases), 'legacy ' + name + ': case identity sets differ')
    records = []
    for case, assessment in cases.items():
        details = {name: angles[name][case] for name in MODULES}
        for name, metric in details.items():
            _require(isinstance(metric.get('status'), str), case + '/' + name + ': status required')
            _score(metric.get('score'), case + '/' + name)
            if metric['status'] == 'ok':
                _score(metric.get('score'), case + '/' + name, nullable=False)
        gate = details['validity']
        validation = None if gate['status'] == 'evaluator_error' else gate.get('score') == 1.
        failed = next((name for name in weight_order if details[name]['status'] == 'evaluator_error'), None)
        expected_status = ('evaluator_error(validity)' if gate['status'] == 'evaluator_error' else
                           'invalid_shape' if not validation else 'evaluator_error(' + failed + ')' if failed else 'ok')
        _require(assessment.get('status') == expected_status, case + ': legacy final status disagrees with raw evidence')
        measured = {name: float(details[name]['score']) if details[name]['status'] == 'ok' else None for name in METRICS}
        participates = expected_status == 'ok'
        if participates:
            _require(all(value is not None for value in measured.values()), case + ': legacy scored case has unavailable metric')
        baseline = math.fsum(weights[name] * measured[name] for name in METRICS) if participates else None
        original_final = baseline if participates else 0. if expected_status == 'invalid_shape' else None
        _same_number(assessment.get('final_score'), original_final, case + ' legacy final arithmetic')
        for name in METRICS:
            if name in assessment:
                raw_score = details[name].get('score')
                rounded = (raw_score if expected_status == 'invalid_shape' else round(raw_score, 6)) if _number(raw_score) else None
                _same_number(assessment[name], rounded, case + '/' + name + ' legacy rounded score')
        records.append(dict(case=case, baseline_score=baseline, measured_scores=measured,
                            awarded_scores=dict(measured) if participates else dict.fromkeys(METRICS),
                            participates=participates, validation_passed=validation,
                            status=expected_status, score_kind='measured' if participates else 'unscored',
                            original_weight_coverage=math.fsum(weights[n] for n in METRICS if measured[n] is not None),
                            exclusion_reasons=[] if participates else [expected_status], input_hashes={}, execution={},
                            metric_statuses={n: details[n]['status'] for n in MODULES},
                            metric_details=details, source_assessment=assessment))
    source.warnings.append('Legacy results have no frozen input hashes; protocol legacy-20261017 is kept separate from v3. '
                           'Stored result JSON is validated, but input provenance cannot be established.')
    return LEGACY_PROTOCOL, weights, records


def read_source(path, *, source_id=None, system=None):
    """Return normalized records and SHA256 hashes of every consumed JSON file.

    ``path`` accepts a v3/v4 run directory/summary.json or a legacy root,
    results directory/final_scores.json. Legacy invalid-shape zeros are retained
    in ``source_assessment`` and excluded from analytical baseline scores.
    """
    path = Path(path).resolve()
    if path.is_dir():
        candidates = [path / 'summary.json', path / 'final_scores.json', path / 'results/final_scores.json']
        matches = [item for item in candidates if item.is_file()]
        _require(len(matches) == 1, 'expected exactly one summary.json or final_scores.json in result directory')
        entry = matches[0]
    else:
        entry = path
    _require(entry.name in ('summary.json', 'final_scores.json'), 'input must be a result directory, summary.json or final_scores.json')
    root = entry.parent
    if entry.name == 'final_scores.json' and root.name == 'results':
        root = root.parent
    source = _Source(root)
    doc = source.read(entry)
    protocol = doc.get('protocol_id')
    if protocol == 'fusion-step-adapted-v2.0' or doc.get('schema_version') == '2.0':
        raise ValueError('v2 is unsupported; this analysis reader supports v3, v4 and legacy-20261017 only')
    if entry.name == 'summary.json':
        _require(protocol in (V3_PROTOCOL,V4_PROTOCOL), 'unsupported summary protocol: ' + str(protocol))
        protocol, weights, records = _read_v3(source, doc, protocol)
    else:
        protocol, weights, records = _read_legacy(source, doc, entry)
    source_id = root.name if source_id is None else source_id
    _require(isinstance(source_id, str) and bool(source_id.strip()), 'source_id must be a nonempty string')
    _require(system is None or isinstance(system, str) and bool(system.strip()), 'system must be a nonempty string or null')
    for row in records:
        row.update(source_id=source_id, source_protocol_id=protocol)
    return dict(source_id=source_id, system=system, source_protocol_id=protocol, weights=weights,
                records=records, source_files=[dict(path=p, sha256=h) for p, h in sorted(source.files.items())],
                warnings=source.warnings, **({'coordinate_mode':doc['coordinate_mode']} if protocol==V4_PROTOCOL else {}))

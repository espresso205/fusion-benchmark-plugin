"""Verified, allowlisted browser projection of final v4 results.

This module uses the standard library and frozen arithmetic contract only. It
never evaluates geometry, reads annotation contents, or edits source evidence.
Private model paths live exclusively in ReportBundle.model_sources.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re

from .contract_v4 import (PROTOCOL_ID, DEFAULT_WEIGHTS, SCORED_METRICS,
                          DIAGNOSTIC_METRICS, MODULE_NAMES, aggregate_case,
                          valid_weights)


@dataclass
class ReportBundle:
    data: dict
    model_sources: dict
    source_files: list


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _same(a, b):
    if _number(a) and _number(b):
        return math.isclose(a, b, rel_tol=0., abs_tol=1e-12)
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


def _text(value):
    """External strings are plain text; redact host paths before publication."""
    value = str(value)
    value = re.sub(r'(?:[A-Za-z]:[\\/]|\\\\)[^\r\n]*', '[private path]', value)
    value = re.sub(r'(?<![\w<:])/[A-Za-z_.~][^\s<>]*', '[private path]', value)
    value = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', value)
    return value.replace('<', '‹').replace('>', '›')[:2000]


def _case_id(value):
    _require(isinstance(value, str) and value and value == value.strip()
             and len(value) <= 160 and value not in ('.', '..')
             and not any(c in value for c in '/\\:<>&\"|?*')
             and not any(ord(c) < 32 for c in value)
             and not value.endswith(('.', ' ')), 'unsafe or invalid case identity')
    _require(value.split('.')[0].upper() not in
             {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)),
              *(f'LPT{i}' for i in range(1, 10))}, 'reserved case identity')
    return value


def _case_map(rows, key='case'):
    _require(isinstance(rows, list), 'case list required')
    out = {}
    for row in rows:
        _require(isinstance(row, dict), 'case object required')
        case = _case_id(row.get(key))
        _require(case not in out, 'duplicate case identity')
        out[case] = row
    return out


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        _require(key not in out, 'duplicate JSON key')
        out[key] = value
    return out


def _reject_constant(value):
    raise ValueError('nonfinite JSON number')


def _float(value):
    result = float(value)
    _require(math.isfinite(result), 'nonfinite JSON number')
    return result


def _decode(raw):
    result = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_pairs,
                        parse_constant=_reject_constant, parse_float=_float)
    _require(isinstance(result, dict), 'JSON object required')
    return result


class _Source:
    def __init__(self, root):
        self.root = root.resolve()
        self.files = {}

    def path(self, relative):
        path = self.root / relative
        _require(path.resolve().is_relative_to(self.root), 'source path escapes result directory')
        return path

    def read(self, relative):
        raw = self.path(relative).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        _require(relative not in self.files or self.files[relative] == digest,
                 'source changed while reading')
        self.files[relative] = digest
        return _decode(raw)


def _hash(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _hashes(value):
    return (isinstance(value, dict) and set(value) == {'gt', 'candidate', 'annotations'}
            and all(_hash(v) for v in value.values()))


def _file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _identity(row, case, digest, mode, hashes=None, name=None):
    _require(row.get('schema_version') == '4.0' and row.get('protocol_id') == PROTOCOL_ID,
             'schema or protocol mismatch')
    _require(row.get('coordinate_mode') == mode, 'coordinate mode mismatch')
    _require(row.get('run_manifest_sha256') == digest, 'run manifest identity mismatch')
    if case is not None:
        _require(row.get('case') == case, 'case identity mismatch')
    if hashes is not None:
        _require(row.get('input_hashes') == hashes, 'input identity mismatch')
    if name is not None:
        _require(row.get('angle', name) == name, 'module identity mismatch')


# Schemas are structural allowlists. A scalar never admits arbitrary dictionaries.
def _fields(names, kind='scalar'):
    return {name: kind for name in names.split()}


def project_cad_provenance(value):
    """Project maintainer pipeline provenance; never publish paths or worker claims."""
    schema = {**_fields('cad_backend cad_config_sha256 failure_stage evaluation_source_sha256 export_receipt_sha256 template_selection_receipt_sha256'),
        'cad_identity': _fields('backend version software_version bridge_version adapter_version adapter_sha256 helper_sha256 tools_sha256 source_sha256 server_name pid session_id'),
        'native_document': _fields('schema format document_type bytes sha256 configuration dependency_sha256'),
        'trusted_step_sha256': 'text', 'trusted_report_sha256': 'text',
        'trusted_export': {**_fields('status stage length_unit step_ap body_scope dependency_status template_evidence_count'),
                           'declared_length_units': ['text'], 'warnings': [_fields('name message health phase code bit classification reason')]},
        'cad_capabilities': {**_fields('backend declared_support dependency_discovered available'),
                             **{key: _fields('executed status') for key in ('mcp_handshake', 'cad_connection', 'trusted_export')}}}
    result = _project(value, schema) or {}
    identity = value.get('cad_identity') or {}
    if isinstance(identity, dict):
        shown = result.setdefault('cad_identity', {}) if identity else {}
        instance = identity.get('instance') or {}
        if isinstance(instance, dict):
            shown.update(_project(instance, _fields('pid session_id version')))
        bridge = identity.get('bridge_server', identity.get('server', {})) or {}
        server = bridge.get('serverInfo', {}) if isinstance(bridge, dict) else {}
        if isinstance(server, dict):
            for key, target in (('name', 'server_name'), ('version', 'bridge_version')):
                if isinstance(server.get(key), str):
                    shown[target] = _text(server[key])
    return result


_SPREAD = _fields('volume_relative inertia_relative centroid_mm')
_MASS_SIDE = {**_fields('volume_mm3 backend converged diagonal_mm integration_eps integration_error_estimate deflection_mm'),
              'centroid_mm': ['number'], 'principal_moments': ['number'], 'spread': _SPREAD,
              'attempts': [{**_fields('backend deflection_mm'), 'eps': ['number'], 'spread': _SPREAD}]}
_DIM_ITEM = {**_fields('id type axis pass expected measured difference_mm diameter_difference_mm count expected_count face_count_difference matched_planes fallback_association failure_type reason'),
             **_fields('expected_minor measured_minor expected_major measured_major minor_radius_difference_mm major_radius_difference_mm pattern_centroid_norm_mm pattern_centroid_excess_mm', 'number'),
             'measured_from_planes': ['number'],
             'pattern_radius': {'expected': 'number', 'measured': ['number'], 'pass': 'scalar'},
             'pattern_positions': {'pairwise_measured': ['number'], 'pairwise_expected': ['number'],
                                   'centroid_xy': ['number'], 'pairwise_pass': 'scalar', 'centroid_pass': 'scalar'},
             'pattern_radius_difference_mm': ['number'], 'pattern_pairwise_difference_mm': ['number']}
_SECTION_GEOMETRY = {**_fields('area_mm2 perimeter_mm contours open_chains backend deflection_mm'),
                     'exact_backend_issues': ['text'],
                     'quality': {**_fields('measurement_valid empty boolean_done coplanar_faces edge_count vertex_count duplicate_edges branch_nodes open_nodes endpoint_tolerance_mm closed_loops'),
                                 'issues': ['text'], 'node_degrees': ['number']},
                     'refinement_difference': _fields('area_mm2 perimeter_mm contours')}
_SECTION = {**_fields('id axis original_offset_mm score'), 'positions_mm': ['number'],
            'sides': [{**_fields('position_mm score'), 'gt': _SECTION_GEOMETRY,
                       'candidate': _SECTION_GEOMETRY}]}
_HEALTH = {**_fields('healthy brep_valid solid_count signed_volume_mm3 working_unit validation_backend'),
           'declared_length_units': ['text'], 'issues': ['text'],
           'solids': [{**_fields('index brep_valid closed volume_mm3'), 'bbox_mm': ['number']}],
           'interference_tolerance': _fields('absolute_mm3 relative_to_smaller_solid')}
_SHAPE = {**_fields('volumetric_iou surface_f1 topology part_diagonal_mm mesh_deflection_mm scale_method coordinate_policy'),
          'official_diagnostics': _fields('part_diagonal surface_distance_f1 surface_distance_precision surface_distance_recall surface_distance_threshold surface_distance_mean_chamfer surface_distance_mean_normal_dot volume_intersection volume_union volume_symmetric_difference'),
          'topology_per_axis': _fields('b0 b1 b2')}
_RAW = {
    'validity': {'candidate': _HEALTH, 'gt': _HEALTH, **_fields('stage error')},
    'pose': {**_fields('coordinate_mode gt_sha256 candidate_sha256 aligned_candidate_sha256 evaluation_candidate_sha256 seed identity_shortcut rmse rmse_mm rotation_angle_deg rotation_determinant rotation_orthogonality_error translation_norm_mm'),
             'R': [['number']], 'rotation_matrix': [['number']],
             **{k: ['number'] for k in ('t', 'translation_mm', 'gt_bbox_center_mm', 'candidate_bbox_center_mm', 'bbox_center_delta_mm')},
             'volume_evidence': _fields('aligned_mm3 original_mm3 relative_budget relative_change'),
             'export': _fields('strategy precision_mode')},
    'shape': _SHAPE,
    'world_shape': _SHAPE,
    'dimension': {**_fields('gt_self_check_passed gt_self_check_total annotation_kind passed total interpretation'),
                  'items': [_DIM_ITEM], 'gt_items': [_DIM_ITEM], 'limitations': ['text']},
    'mass': {'gt': _MASS_SIDE, 'candidate': _MASS_SIDE,
             **_fields('volume_relative_error principal_moment_relative_error relative_convergence_budget score_estimate_diagnostic_only'),
             'scales': _fields('volume inertia')},
    'distance': {**_fields('symmetric_p99_mm symmetric_mean_cd1_mm part_diagonal_mm tau_mm samples_per_side seed distance_backend scale_method limitation'),
                 'gt_to_candidate': _fields('max p99 p95 mean'), 'candidate_to_gt': _fields('max p99 p95 mean')},
    'face_match': {**_fields('definition unassessed_area_fraction deflection_mm distance_tolerance_mm normal_tolerance_degrees samples_per_side_per_seed'),
                   'seed_results': [_fields('seed precision recall f1')], 'triangle_counts': ['number'], 'limitations': ['text']},
    'section': {**_fields('definition'), 'sections': [_SECTION], 'scales': _fields('area_perimeter_relative'),
                'plan': {**_fields('definition offset_mm reference_sha256'), 'sections': [_SECTION]}},
}
for _schema in _RAW.values():
    _schema.update(_fields('error reason'))


def _project(value, schema):
    if value is None:
        return None
    if isinstance(schema, dict):
        return {k: _project(value[k], child) for k, child in schema.items() if k in value} if isinstance(value, dict) else {}
    if isinstance(schema, list):
        return [_project(item, schema[0]) for item in value[:10000]] if isinstance(value, list) else []
    if schema == 'number':
        return value if _number(value) else None
    if schema == 'text':
        return _text(value) if isinstance(value, str) else None
    if type(value) is bool or _number(value):
        return value
    return _text(value) if isinstance(value, str) else None


_EXECUTION = _fields('status returncode timed_out wall_s')
_REASON = _fields('module status detail returncode timed_out wall_s')
_CASE = {**_fields('case status final_score score_kind measurement_weight_coverage participates_in_scoring validation_passed'),
         'score_interval': ['number'], 'weights': _fields('shape dimension mass distance face_match section', 'number'),
         'geometry': {**_fields('candidate_status reference_status'), 'reference_preflight': _fields('validated status')},
         'unavailable_metrics': ['text'], 'diagnostic_unavailable_metrics': ['text'],
         'execution': _EXECUTION, 'reasons': [_REASON]}
_METRIC = {**_fields('status value score measured_score awarded_score metric_role diagnostic_value score_source quality backend unit'),
           'reason_codes': ['text'], 'timing': _fields('wall_s elapsed_s'),
           'evidence': {'result_file': 'text', 'input_hashes': _fields('gt candidate annotations', 'text'),
                        'evaluation_input_hashes': _fields('gt candidate annotations', 'text'), 'run_manifest_sha256': 'text'}}


def _raw(name, row):
    out = _project(row.get('raw', {}), _RAW[name])
    if name == 'dimension':
        for item in out.get('items', []):
            if 'type' not in item:
                item['type'] = ('torus' if 'expected_minor' in item or 'expected_major' in item
                                else 'cylinder_diameter' if 'expected_count' in item or 'diameter_difference_mm' in item
                                else 'plane_pair' if 'measured_from_planes' in item else 'probe')
    return out


def _project_case(row, modules):
    out = _project(row, _CASE)
    out['metrics'] = {}
    for name in ('validity', *DEFAULT_WEIGHTS):
        metric = _project(row.get('metrics', {}).get(name, {}), _METRIC)
        metric['raw'] = _raw(name, modules.get(name, {}))
        out['metrics'][name] = metric
    for name, key in (('pose', 'pose'), ('world_shape', 'coordinate_diagnostics')):
        extra = row.get(key, {})
        out[key] = {**_project(extra, _fields('status score backend')), 'raw': _raw(name, extra)}
    out.update(integrity_status='verified', pipeline_state=None, ranking_eligible=False)
    return out


def _untrusted(case, row, reason):
    out = _empty_case(case, 'integrity_error')
    out.update(integrity_status='untrusted', status='integrity_error',
               execution=_project(row.get('execution', {}), _EXECUTION),
               reasons=[{'module': 'integrity', 'status': 'integrity_error', 'detail': _text(reason)}])
    return out


def _empty_case(case, status):
    return {'case': case, 'status': status, 'final_score': None, 'score_kind': 'unscored',
            'score_interval': None, 'measurement_weight_coverage': 0.,
            'participates_in_scoring': False, 'validation_passed': False,
            'geometry': {'candidate_status': 'unknown', 'reference_status': 'unknown'},
            'weights': dict(DEFAULT_WEIGHTS), 'unavailable_metrics': list(SCORED_METRICS),
            'diagnostic_unavailable_metrics': list(DIAGNOSTIC_METRICS), 'execution': {},
            'metrics': {}, 'pose': {}, 'coordinate_diagnostics': {}, 'reasons': [],
            'pipeline_state': None, 'integrity_status': 'not_evaluated', 'ranking_eligible': False}


def _read_case(source, case, row, manifest_case, weights, digest, mode):
    _identity(row, case, digest, mode)
    _require(source.read(case + '/aggregate.json') == row, 'summary and aggregate disagree')
    hashes = manifest_case.get('expected_hashes')
    _require(_hashes(hashes), 'invalid input hash manifest')
    _require(_same(row.get('weights'), weights), 'weights mismatch')
    modules = {}
    disclosed = {r.get('module') for r in row.get('diagnostic_read_errors', [])
                 if isinstance(r, dict) and 'parse/read' in str(r.get('detail', ''))}
    for name in MODULE_NAMES:
        rel = case + '/results/' + name + '.json'
        path = source.path(rel)
        if not path.exists():
            continue
        try:
            module = source.read(rel)
        except (OSError, UnicodeError, ValueError):
            _require(name in (*DIAGNOSTIC_METRICS, 'world_shape') and name in disclosed,
                     'module evidence cannot be read: ' + name)
            continue
        _identity(module, case, digest, mode, hashes, name)
        modules[name] = module
    expected = aggregate_case(case, modules, hashes, weights, coordinate_mode=mode)
    # Reapply only the documented outer-process effects; never recompute geometry.
    if row.get('status') == 'evaluator_error' and not expected['participates_in_scoring']:
        _require(row.get('execution', {}).get('status') != 'completed', 'unexpected execution override')
        expected['status'] = 'evaluator_error'
    fields = ('status', 'final_score', 'score_kind', 'score_interval', 'measurement_weight_coverage',
              'participates_in_scoring', 'validation_passed', 'geometry', 'weights', 'module_scores',
              'awarded_module_scores', 'unavailable_metrics', 'diagnostic_unavailable_metrics', 'metrics')
    for field in fields:
        _require(_same(row.get(field), expected.get(field)), 'final assessment mismatch: ' + field)
    _require(row.get('ranking_eligible') is False, 'unexpected ranking eligibility')
    profile = row.get('scores', {}).get(PROTOCOL_ID, {})
    for field in ('value', 'aggregation_status', 'score_kind', 'coverage', 'rank_eligible'):
        _require(_same(profile.get(field), expected['scores'][PROTOCOL_ID].get(field)), 'score profile mismatch')
    for name, key in (('pose', 'pose'), ('world_shape', 'coordinate_diagnostics')):
        if name == 'world_shape' and name in disclosed and name not in modules:
            _require(row.get(key, {}).get('status') == 'diagnostic_unreliable', 'missing coordinate diagnostic disclosure')
        else:
            _require(row.get(key, {}) == modules.get(name, {}), 'embedded module mismatch: ' + name)
    models = {'world': {}, 'scoring': {}}
    warnings = []
    # Only a streaming digest is computed; annotation text is never parsed or exported.
    annotation = source.path(case + '/inputs/annotations.json')
    if annotation.exists():
        _require(_file_hash(annotation) == hashes['annotations'], 'staged annotation identity mismatch')

    def slot(relative, digest, frame):
        path = source.path(relative)
        if not path.exists():
            warnings.append(case + ': preview source unavailable: ' + relative.split('/')[-1])
            return None
        _require(_hash(digest), 'invalid model identity')
        _require(_file_hash(path) == digest, 'model bytes do not match identity')
        return {'path': str(path.resolve()), 'sha256': digest, 'coordinate_frame': frame}

    for role, name in (('reference', 'gt'), ('candidate', 'candidate')):
        value = slot(case + '/inputs/' + name + '.step', hashes[name], 'fixed_world_mm')
        if value:
            models['world'][role] = value
            if mode == 'strict' or role == 'reference':
                models['scoring'][role] = dict(value)
                if mode == 'geometry':
                    models['scoring'][role]['coordinate_frame'] = 'rigid_aligned_mm'
    pose = modules.get('pose', {})
    if pose.get('status') == 'ok':
        raw = pose.get('raw', {})
        aligned_hash = raw.get('aligned_candidate_sha256')
        _require(_hash(aligned_hash), 'invalid aligned model identity')
        _require(raw.get('evaluation_candidate_sha256') == (aligned_hash if mode == 'geometry' else hashes['candidate']),
                 'pose evaluation identity mismatch')
        aligned = slot(case + '/derived/candidate_aligned.step', aligned_hash, 'rigid_aligned_mm')
        if mode == 'geometry' and aligned:
            models['scoring']['candidate'] = aligned
    return _project_case(row, modules), models, warnings


def _summary(cases):
    participating = [r for r in cases if r['participates_in_scoring']]
    return {'total_case_count': len(cases), 'participating_case_count': len(participating),
            'complete_case_count': sum(r['score_kind'] == 'measured' for r in participating),
            'lower_bound_case_count': sum(r['score_kind'] == 'policy_lower_bound' for r in participating),
            'invalid_case_count': sum(r['geometry'].get('candidate_status') == 'invalid' for r in cases),
            'validation_incomplete_case_count': sum(r['integrity_status'] == 'verified' and r['geometry'].get('candidate_status') == 'unknown' for r in cases),
            'pipeline_unscored_case_count': sum(r['integrity_status'] == 'not_evaluated' for r in cases),
            'integrity_error_case_count': sum(r['integrity_status'] == 'untrusted' for r in cases),
            'batch_mean': math.fsum(r['final_score'] for r in participating) / len(participating) if participating else None,
            'mean_measurement_weight_coverage': math.fsum(r['measurement_weight_coverage'] for r in participating) / len(participating) if participating else None}


def _validate_run(source, summary, cases):
    manifest = source.read('manifest.json')
    mode = manifest.get('coordinate_mode')
    _require(mode in ('geometry', 'strict'), 'invalid coordinate mode')
    digest = source.files['manifest.json']
    _identity(summary, None, digest, mode)
    _require(manifest.get('schema_version') == '4.0' and manifest.get('protocol_id') == PROTOCOL_ID,
             'manifest protocol mismatch')
    config = manifest.get('weights', {})
    _require(isinstance(config, dict) and config.get('protocol_id', PROTOCOL_ID) == PROTOCOL_ID,
             'weights protocol mismatch')
    weights = config.get('weights', config)
    _require(valid_weights(weights), 'invalid v4 weights')
    manifest_cases = _case_map(manifest.get('cases'))
    _require(cases.keys() == manifest_cases.keys(), 'summary and manifest cases disagree')
    participants = [r for r in cases.values() if r.get('participates_in_scoring') is True]
    _require(all(_number(r.get('final_score')) and 0 <= r['final_score'] <= 1 for r in participants), 'invalid summary score')
    expected_mean = math.fsum(r['final_score'] for r in participants) / len(participants) if participants else None
    _require(type(summary.get('total_case_count')) is int and summary['total_case_count'] == len(cases), 'summary case count mismatch')
    _require(type(summary.get('participating_case_count')) is int and summary['participating_case_count'] == len(participants), 'summary participant count mismatch')
    _require(_same(summary.get('batch_mean'), expected_mean), 'summary mean mismatch')
    if participants:
        _require(summary.get('code_unchanged') is True and summary.get('original_inputs_unchanged') is True,
                 'completed batch integrity not confirmed')
    _require(summary.get('ranking_eligible') is False and manifest.get('ranking_eligible') is False,
             'unexpected ranking eligibility')
    return manifest_cases, weights, digest, mode


def load_report(source, *, pipeline=None, analysis=None, run_name=None):
    """Load immutable final evidence. Individual untrusted cases lose public scores.

    Unsafe case IDs, unreadable summaries and unsupported protocols raise
    ValueError. An unreadable/altered manifest quarantines all known cases.
    An incompatible optional analysis rejects only the appendix.
    """
    _require(pipeline is None or isinstance(pipeline, dict), 'pipeline object required')
    pipeline = pipeline or {}
    pipeline_cases = _case_map(pipeline.get('cases', []), 'case_id')
    mode = pipeline.get('coordinate_mode', 'geometry')
    _require(mode in ('geometry', 'strict'), 'invalid pipeline coordinate mode')
    _require(pipeline.get('scoring_protocol', PROTOCOL_ID) == PROTOCOL_ID, 'HTML reports support v4 only')
    warnings, cases, models = [], [], {}
    reader = None
    weights = dict(DEFAULT_WEIGHTS)
    if source is not None:
        path = Path(source)
        root = path if path.is_dir() else path.parent
        _require(path.is_dir() or path.name == 'summary.json', 'source must be evaluation directory or summary.json')
        reader = _Source(root)
        try:
            final = reader.read('summary.json')
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError('final summary cannot be read') from exc
        _require(final.get('protocol_id') == PROTOCOL_ID, 'HTML reports support v4 only')
        raw_cases = _case_map(final.get('cases'))
        mode = final.get('coordinate_mode')
        _require(mode in ('geometry', 'strict'), 'invalid result coordinate mode')
        _require('coordinate_mode' not in pipeline or pipeline['coordinate_mode'] == mode, 'pipeline coordinate mode mismatch')
        try:
            manifest_cases, weights, digest, mode = _validate_run(reader, final, raw_cases)
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            reason = _text(str(exc)) if isinstance(exc, ValueError) else 'run manifest cannot be verified'
            warnings.append('Run evidence cannot be verified: ' + reason)
            cases = [_untrusted(case, row, reason) for case, row in raw_cases.items()]
        else:
            for case, row in raw_cases.items():
                try:
                    projected, private_models, notes = _read_case(reader, case, row, manifest_cases[case], weights, digest, mode)
                    cases.append(projected)
                    models[case] = private_models
                    warnings.extend(notes)
                except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError) as exc:
                    reason = _text(str(exc)) if isinstance(exc, ValueError) else 'case evidence cannot be verified'
                    cases.append(_untrusted(case, row, reason))
                    warnings.append(case + ': ' + reason)
    else:
        _require(bool(pipeline_cases), 'source or pipeline cases required')
    by_id = {r['case']: r for r in cases}
    for case, row in pipeline_cases.items():
        if case not in by_id:
            result = _empty_case(case, 'pipeline_unscored')
            result['execution'] = _project(row.get('execution', {}), _EXECUTION)
            if row.get('error'):
                result['reasons'] = [{'module': 'pipeline', 'status': _text(row.get('state', 'unscored')),
                                      'detail': _text(row['error'])}]
            cases.append(result)
            by_id[case] = result
        by_id[case]['pipeline_state'] = _text(row.get('state', row.get('status', 'unknown')))
        by_id[case].update(project_cad_provenance(row))
    cases.sort(key=lambda row: row['case'])
    public_files = [{'path': name, 'sha256': digest} for name, digest in sorted(reader.files.items())] if reader else []
    appendix = _analysis(analysis, reader, cases, mode, weights) if analysis is not None else {'status': 'not_provided'}
    if appendix['status'] == 'rejected':
        warnings.append('Analysis appendix rejected: ' + appendix['reason'])
    data = {'schema_version': 'fusion-html-report/1', 'protocol_id': PROTOCOL_ID,
            'coordinate_mode': mode, 'run_name': _text(run_name or pipeline.get('run_name') or (reader.root.name if reader else 'pipeline')),
            'generated_at': datetime.now(timezone.utc).isoformat(), 'ranking_eligible': False,
            'summary': _summary(cases), 'cases': cases, 'analysis': appendix,
            'warnings': warnings, 'source_files': public_files}
    data.update(project_cad_provenance(pipeline))
    return ReportBundle(data, models, public_files)


_STATISTIC = _fields('value n reason')
_RANKING = _fields('case score baseline_score delta rank baseline_rank rank_change original_weight_coverage retained_weight_coverage score_kind')
_VARIANT = {**_fields('id removed_metric mode mean_score'), 'ranking': [_RANKING],
            'rank_comparison': {'spearman': _STATISTIC, 'kendall_tau_b': _STATISTIC}}
_CORRELATIONS = {method: {a: {b: _STATISTIC for b in DEFAULT_WEIGHTS} for a in DEFAULT_WEIGHTS}
                 for method in ('pearson', 'spearman')}
_COHORT = {'case_count': 'number', 'case_ids': ['text'], 'variants': [_VARIANT], 'correlations': _CORRELATIONS}
_ANALYSIS_SCHEMA = {**_fields('source_id source_protocol_id coordinate_mode'),
                    'weights': _fields('shape dimension mass distance face_match section', 'number'),
                    'counts': {**_fields('total participating complete excluded evaluator_failure_cases evaluator_failure_rate'),
                               'unavailable_metric_counts': _fields('shape dimension mass distance face_match section', 'number')},
                    'cohorts': {'complete_measurements': _COHORT, 'policy_awards': _COHORT}, 'warnings': ['text']}


def _analysis_source_files(entries, expected):
    """Match relative evidence identity even after moving a complete run folder."""
    _require(isinstance(entries, list), 'analysis source file list required')
    matched = {}
    for entry in entries:
        _require(isinstance(entry, dict) and isinstance(entry.get('path'), str), 'invalid analysis source entry')
        name = entry['path'].replace('\\', '/')
        _require('..' not in PurePosixPath(name).parts, 'unsafe analysis source path')
        candidates = [relative for relative in expected if name == relative or name.endswith('/' + relative)]
        _require(len(candidates) == 1, 'analysis source file does not belong to this run')
        relative = candidates[0]
        _require(relative not in matched and entry.get('sha256') == expected[relative], 'analysis source hash mismatch')
        matched[relative] = entry['sha256']
    _require(matched == expected, 'analysis source evidence set mismatch')
    return [{'path': name, 'sha256': digest} for name, digest in sorted(matched.items())]


def _analysis(analysis, source, cases, mode, weights):
    try:
        _require(source is not None, 'analysis requires a verified evaluation')
        raw = Path(analysis).read_bytes()
        document = _decode(raw)
        _require(document.get('schema_version') == 'fusion-ablation/1' and document.get('ranking_eligible') is False,
                 'unsupported analysis schema or ranking eligibility')
        evaluated = {r['case']: r for r in cases if r['integrity_status'] != 'not_evaluated'}
        _require(evaluated and all(r['integrity_status'] == 'verified' for r in evaluated.values()),
                 'analysis requires every source case to be verified')
        analyses = document.get('analyses')
        sources = document.get('sources')
        _require(isinstance(analyses, list) and isinstance(sources, list), 'analysis sources and analyses required')
        matches = []
        for candidate in analyses:
            if not isinstance(candidate, dict):
                continue
            if candidate.get('source_protocol_id') != PROTOCOL_ID or candidate.get('coordinate_mode') != mode or not _same(candidate.get('weights'), weights):
                continue
            records = _case_map(candidate.get('records', []))
            if records.keys() == evaluated.keys():
                matches.append((candidate, records))
        _require(len(matches) == 1, 'analysis protocol, mode, weights or case identities mismatch')
        result, records = matches[0]
        matching_sources = [s for s in sources if isinstance(s, dict) and s.get('source_id') == result.get('source_id')]
        _require(len(matching_sources) == 1, 'analysis source identity is ambiguous')
        origin = matching_sources[0]
        _require(origin.get('source_protocol_id') == PROTOCOL_ID and _same(origin.get('weights'), weights),
                 'analysis source protocol or weights mismatch')
        source_files = _analysis_source_files(origin.get('source_files'), source.files)
        for case, row in records.items():
            actual = evaluated[case]
            for key, target in (('baseline_score', 'final_score'), ('participates', 'participates_in_scoring'),
                                ('validation_passed', 'validation_passed'), ('score_kind', 'score_kind'),
                                ('status', 'status'), ('original_weight_coverage', 'measurement_weight_coverage')):
                _require(_same(row.get(key), actual.get(target)), 'analysis record differs from final result')
            for metric in DEFAULT_WEIGHTS:
                _require(_same(row.get('measured_scores', {}).get(metric), actual['metrics'][metric].get('measured_score')),
                         'analysis measurement differs from final result')
                _require(_same(row.get('awarded_scores', {}).get(metric), actual['metrics'][metric].get('awarded_score')),
                         'analysis credit differs from final result')
            expected_hashes = actual['metrics'].get('validity', {}).get('evidence', {}).get('input_hashes')
            if expected_hashes:
                _require(row.get('input_hashes') == expected_hashes, 'analysis input identity mismatch')
        participants = {case for case, row in evaluated.items() if row['participates_in_scoring']}
        complete = {case for case in participants if evaluated[case]['score_kind'] == 'measured'}
        counts = result.get('counts', {})
        for key, count in (('total', len(evaluated)), ('participating', len(participants)), ('complete', len(complete)),
                           ('excluded', len(evaluated) - len(participants))):
            _require(type(counts.get(key)) is int and counts[key] == count, 'analysis cohort counts mismatch')
        for name, ids in (('complete_measurements', complete), ('policy_awards', participants)):
            cohort = result.get('cohorts', {}).get(name, {})
            case_ids = cohort.get('case_ids', [])
            _require(isinstance(case_ids, list) and len(case_ids) == len(ids) and set(case_ids) == ids
                     and cohort.get('case_count') == len(ids), 'analysis cohort case identities mismatch')
            for variant in cohort.get('variants', []):
                _require(_case_map(variant.get('ranking', [])).keys() == ids, 'analysis ranking case identities mismatch')
        projected = _project(result, _ANALYSIS_SCHEMA)
        projected.update(status='ok', ranking_eligible=False, source_files=source_files,
                         analysis_sha256=hashlib.sha256(raw).hexdigest())
        return projected
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError) as exc:
        reason = _text(str(exc)) if isinstance(exc, ValueError) else 'analysis source cannot be read'
        return {'status': 'rejected', 'reason': reason}

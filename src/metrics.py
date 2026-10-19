"""Maintained v2 adapters for the unchanged shape/distance/mass score formulas.

All coordinates are millimetres in the fixed world frame. Alignment is diagnostic.
Mass uses OCCT's centroid inertia directly; numerical integration is not exact.
"""
from dataclasses import asdict
from pathlib import Path
import math
import sys
import time

import numpy as np
from scipy.spatial import cKDTree

VENDOR = Path(__file__).resolve().parents[1] / 'vendor'
sys.path.insert(0, str(VENDOR))
from cadgenbench.common.artifacts import StepArtifacts
from cadgenbench.eval.shape_similarity import compare_step_files
from cadgenbench.eval.topo_match import topo_match
from cadgenbench.eval.sampling import sample_surface_points, _load_occ_shape
from OCP.STEPControl import STEPControl_Reader
from OCP.TColStd import TColStd_SequenceOfAsciiString
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_SOLID
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from . import dimension_probes as probes

N_SAMPLES = 50000


def result(status='ok', score=None, raw=None, **quality):
    return {'status': status, 'score': score,
            'quality': {'measurement_valid': status == 'ok', **quality}, 'raw': raw or {}}


def finite_score(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f'invalid score: {value!r}')
    return value


def read_step(path):
    r = STEPControl_Reader()
    if int(r.ReadFile(str(path))) != 1:
        raise ValueError(f'STEP parse failed: {path}')
    units = [TColStd_SequenceOfAsciiString() for _ in range(3)]
    r.FileUnits(*units)
    length_units = [units[0].Value(i).ToCString() for i in range(1, units[0].Length()+1)]
    if not length_units:
        raise ValueError('STEP declares no length unit')
    # OCCT length-unit factor 1 means millimetres. STEP centimetres/metres are
    # unit conversion, never a best-fit rescaling of candidate geometry.
    r.SetSystemLengthUnit(1.0)
    if r.TransferRoots() <= 0:
        raise ValueError('STEP has no transferable root')
    shape = r.OneShape()
    if shape.IsNull():
        raise ValueError('STEP returned a null shape')
    return shape, length_units


def geometry_info(shape, units):
    ex = TopExp_Explorer(shape, TopAbs_SOLID)
    solids = 0
    while ex.More():
        solids += 1
        ex.Next()
    p = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, p)
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box)
    bounds = list(box.Get())
    volume = p.Mass()
    valid = bool(BRepCheck_Analyzer(shape).IsValid())
    healthy = valid and solids > 0 and math.isfinite(volume) and volume > 0 and all(map(math.isfinite, bounds))
    return {'declared_length_units': units, 'working_unit': 'mm', 'solid_count': solids,
            'brep_valid': valid, 'signed_volume_mm3': volume, 'bbox_mm': bounds,
            'healthy': healthy}


def validity(gt, candidate, artifacts):
    raw = {}
    for role, path in [('gt', gt), ('candidate', candidate)]:
        try:
            shape, units = read_step(path)
            info = geometry_info(shape, units)
        except (ValueError, OSError) as exc:
            return result('reference_invalid' if role == 'gt' else 'candidate_invalid',
                          raw={**raw, role: {'error': str(exc)}}, reference_valid=role != 'gt')
        raw[role] = info
        if not info['healthy']:
            return result('reference_invalid' if role == 'gt' else 'candidate_invalid',
                          raw=raw, reference_valid=role != 'gt')
        analysis = asdict(artifacts[role].analysis)
        info['mesh_analysis'] = analysis
        v = analysis['validation']
        if not (v['is_valid'] and v['is_watertight']):
            # A healthy watertight BRep with a failed mesh gate is not confirmed
            # invalid CAD. Mesh timeouts/ceilings are evaluator limitations.
            errors = ' '.join(str(e).lower() for e in v.get('topology_errors', []))
            if v.get('is_watertight') is True or any(s in errors for s in ('timeout','ceiling','file size','file too large')):
                return result('evaluator_error',raw={**raw,'error':'mesh validation failed on independently checked BRep'},
                              reference_valid=role!='gt')
            return result('reference_invalid' if role == 'gt' else 'candidate_invalid',
                          raw=raw, reference_valid=role != 'gt')
        # Independently ensure the dependency imported millimetres too.
        # Use the same bounding algorithm on both independently loaded shapes.
        # CADGenBench's reported Add_s bounds can include a spline control hull;
        # comparing those with AddOptimal_s would invent a unit mismatch (Q06).
        check_box = Bnd_Box()
        BRepBndLib.AddOptimal_s(artifacts[role].wrapped, check_box)
        b = list(check_box.Get())
        info['dependency_optimal_bbox_mm'] = b
        if max(abs(a-c) for a, c in zip(b, info['bbox_mm'])) > 1e-4:
            return result('evaluator_error', raw={**raw, 'error': 'loader coordinate/unit disagreement'})
    return result(score=1.0, raw=raw, reference_valid=True, candidate_valid=True)


def dimension(gt, candidate, annotations):
    items = annotations.get('items', [])
    if not items or len({it['id'] for it in items}) != len(items):
        return result('reference_invalid', raw={'error': 'empty or duplicate dimension probes'}, reference_valid=False)
    unknown = sorted({it['type'] for it in items} - probes.CHECKS.keys())
    if unknown:
        return result('unsupported', raw={'probe_types': unknown})
    gt_faces = probes.load_faces(gt)
    gt_items = [probes.CHECKS[it['type']](it, gt_faces) for it in items]
    n_gt = sum(bool(it['pass']) for it in gt_items)
    raw = {'gt_items': gt_items, 'gt_self_check_passed': n_gt, 'gt_self_check_total': len(items),
           'annotation_kind': 'gt_derived_probe',
           'limitations': ['diameter counts are face counts, not semantic hole counts',
                          'no independent drawing-based position/depth/GD&T certification']}
    if n_gt != len(items):
        return result('reference_invalid', raw=raw, reference_valid=False)
    faces = probes.load_faces(candidate)
    checks = [probes.CHECKS[it['type']](it, faces) for it in items]
    passed = sum(bool(it['pass']) for it in checks)
    raw.update(items=checks, passed=passed, total=len(checks))
    return result(score=passed / len(checks), raw=raw, reference_valid=True)


def mass_properties(shape, eps=None):
    p = GProp_GProps()
    estimated_error = None
    if eps is None:
        BRepGProp.VolumeProperties_s(shape, p)
    else:
        estimated_error = BRepGProp.VolumeProperties_s(shape, p, float(eps), True, False)
    m = p.MatrixOfInertia()
    tensor = np.array([[m.Value(i, j) for j in (1, 2, 3)] for i in (1, 2, 3)])
    moments = np.sort(np.linalg.eigvalsh(tensor))
    c = p.CentreOfMass()
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    bounds = box.Get()
    values = {'volume_mm3': p.Mass(), 'centroid_mm': [c.X(), c.Y(), c.Z()],
              'principal_moments': moments.tolist(),
              'diagonal_mm': math.dist(bounds[:3], bounds[3:]), 'integration_eps': eps,
              'integration_error_estimate': estimated_error}
    if not np.isfinite([p.Mass(), *values['centroid_mm'], *moments, values['diagonal_mm']]).all():
        raise ValueError('non-finite mass properties')
    if p.Mass() <= 0 or np.any(moments <= 0) or values['diagonal_mm'] <= 0:
        raise ValueError('non-positive volume, principal moment or diagonal')
    return values


def mass(gt, candidate):
    gshape, _ = read_step(gt)
    cshape, _ = read_step(candidate)
    try:
        g = mass_properties(gshape)
    except ValueError as exc:
        return result('reference_invalid', raw={'error': str(exc)}, reference_valid=False)
    q = mass_properties(cshape)
    rv = abs(q['volume_mm3'] - g['volume_mm3']) / g['volume_mm3']
    lg, lq = np.array(g['principal_moments']), np.array(q['principal_moments'])
    ri = float(np.max(np.abs(lq-lg) / lg))
    dc = math.dist(g['centroid_mm'], q['centroid_mm'])
    convergence = {role: [mass_properties(s, eps) for eps in (1e-6, 1e-8)]
                   for role, s in [('gt', gshape), ('candidate', cshape)]}
    stability = {}
    for role, original in [('gt', g), ('candidate', q)]:
        samples = [original, *convergence[role]]
        volume = np.array([x['volume_mm3'] for x in samples])
        moments = np.array([x['principal_moments'] for x in samples])
        # Conservative numerical acceptance budget, independent of candidate score.
        # The OCCT volume error estimate is not an inertia convergence guarantee.
        vr = float(np.ptp(volume)/volume[-1])
        ir = float(np.max(np.ptp(moments, axis=0)/moments[-1]))
        stability[role] = {'volume_relative_spread': vr, 'inertia_relative_spread': ir,
                           'relative_budget': 1e-6, 'stable': vr <= 1e-6 and ir <= 1e-6}
    raw = {'gt': g, 'candidate': q, 'volume_relative_error': rv, 'principal_moment_relative_error': ri,
             'centroid_error_mm_diagnostic_only': dc, 'centroid_error_normalized_diagnostic_only': dc/g['diagonal_mm'],
             'convergence_diagnostic': convergence, 'numerical_stability': stability,
             'score_integration': 'legacy default OCCT GProp, accepted only if stability budget passes',
             'legacy_formula_score_diagnostic_only': math.sqrt(math.exp(-rv/0.05)*math.exp(-ri/0.05)),
             'scales': {'volume': 0.05, 'inertia': 0.05}}
    if not stability['gt']['stable']:
        return result('reference_invalid', raw=raw, reference_valid=False,
                      reason='reference mass integration not numerically stable')
    if not stability['candidate']['stable']:
        return result('evaluator_error', raw=raw, reference_valid=True,
                      reason='candidate mass integration not numerically stable; not evidence of invalid candidate')
    return result(score=raw['legacy_formula_score_diagnostic_only'], reference_valid=True, raw=raw)


def directed(a, b):
    d, _ = cKDTree(b).query(a, k=1)
    if not np.isfinite(d).all():
        raise ValueError('non-finite nearest-neighbour distances')
    return {'max': float(d.max()), 'p99': float(np.percentile(d, 99)),
            'p95': float(np.percentile(d, 95)), 'mean': float(d.mean())}


def distance(gt, candidate):
    shape = _load_occ_shape(Path(gt))
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    bounds = box.Get()
    diag = math.dist(bounds[:3], bounds[3:])
    tau = max(1e-6, 0.005 * diag)
    a = sample_surface_points(gt, N_SAMPLES, seed=0)
    b = sample_surface_points(candidate, N_SAMPLES, seed=0)
    ab, ba = directed(a, b), directed(b, a)
    p99 = max(ab['p99'], ba['p99'])
    return result(score=math.exp(-p99/(10*tau)), raw={
        'gt_to_candidate': ab, 'candidate_to_gt': ba, 'symmetric_p99_mm': p99,
        'symmetric_mean_cd1_mm': (ab['mean']+ba['mean'])/2,
        'bidirectional_mean_sum_mm': ab['mean']+ba['mean'], 'part_diagonal_mm': diag,
        'tau_mm': tau, 'samples_per_side': N_SAMPLES, 'seed': 0,
        'sampling_linear_deflection_mm': 0.1, 'sampling_angular_deflection_rad': 0.5,
        'limitation': 'point-to-point sampling has a representation-dependent noise floor; no bias correction applied'})


def shape(gt, candidate, artifacts, workdir):
    t = time.perf_counter()
    fixed = asdict(compare_step_files(candidate, gt, align=False,
                   candidate_artifacts=artifacts['candidate'], gt_artifacts=artifacts['gt']))
    topo = asdict(topo_match(candidate, gt,
                   candidate_artifacts=artifacts['candidate'], gt_artifacts=artifacts['gt']))
    if fixed.get('metric_errors'):
        return result('evaluator_error', raw={'fixed': fixed, 'topology': topo})
    iou = finite_score(fixed['scores']['shape_volume_iou'])
    sf1 = finite_score(fixed['scores']['shape_surface_distance_f1'])
    tp = finite_score(topo['score'])
    fixed_s = time.perf_counter()-t
    t = time.perf_counter()
    try:
        aligned = asdict(compare_step_files(candidate, gt, align=True,
                         aligned_output=Path(workdir)/'candidate_aligned.step', gt_artifacts=artifacts['gt']))
        if aligned.get('metric_errors'):
            raise ValueError(str(aligned['metric_errors']))
        ai = finite_score(aligned['scores']['shape_volume_iou'])
        af = finite_score(aligned['scores']['shape_surface_distance_f1'])
        diag = {'status': 'ok', 'volumetric_iou_aligned': ai, 'surface_f1_aligned': af,
                'alignment_rmse': aligned.get('alignment_rmse'),
                'pose_explained_fraction': (ai-iou)/(1-iou) if iou < 1 else 0.0}
    except Exception as exc:
        diag = {'status': 'diagnostic_unreliable', 'error': str(exc)}
    return result(score=(iou+sf1+tp)/3, raw={
        'volumetric_iou': iou, 'surface_f1': sf1, 'topology': tp,
        'official_diagnostics': fixed['diagnostics'], 'topology_per_axis': topo.get('per_axis_scores'),
        'aligned_diagnostic': diag, 'fixed_compute_s': fixed_s,
        'aligned_diagnostic_s': time.perf_counter()-t})

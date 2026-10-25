"""V4 rigid-pose and geometric measurements over the unchanged upstream metrics."""
from dataclasses import asdict, replace
import hashlib
import math
from pathlib import Path
from shutil import copyfile
from types import SimpleNamespace

import numpy as np
from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.Interface import Interface_Static
from OCP.gp import gp_Trsf

from . import metrics, robust_metrics
from cadgenbench.common.artifacts import StepArtifacts
from cadgenbench.common.measurements import BBox
from cadgenbench.common.mesh import deflection_for_bbox
from cadgenbench.eval.alignment import align_step
from cadgenbench.eval.shape_similarity import compare_step_files
from cadgenbench.eval.topo_match import topo_match
from cadgenbench.eval.sampling import (
    _load_occ_shape, closest_point_distances_and_normals, sample_points_and_normals_from_mesh,
)

DISTANCE_SAMPLES = 20000
SEED = 0


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _optimal_bounds(shape):
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box, False, False)
    bounds = list(box.Get())
    if not all(map(math.isfinite, bounds)):
        raise ValueError('non-finite optimal bounds')
    return bounds


def _scale(gt):
    shape, _ = metrics.read_step(gt)
    bounds = _optimal_bounds(shape)
    diagonal = math.dist(bounds[:3], bounds[3:])
    if diagonal <= 0:
        raise ValueError('non-positive GT diagonal')
    return bounds, diagonal


def _center(bounds):
    return [(bounds[i] + bounds[i + 3]) / 2 for i in range(3)]


def _export_rigid_step(source, rotation, translation, target):
    """Write the checked transform at the source BRep's greatest tolerance."""
    from build123d import Compound, PrecisionMode, export_step

    transform = gp_Trsf()
    transform.SetValues(
        float(rotation[0, 0]), float(rotation[0, 1]), float(rotation[0, 2]), float(translation[0]),
        float(rotation[1, 0]), float(rotation[1, 1]), float(rotation[1, 2]), float(translation[1]),
        float(rotation[2, 0]), float(rotation[2, 1]), float(rotation[2, 2]), float(translation[2]),
    )
    builder = BRepBuilderAPI_Transform(_load_occ_shape(source), transform, True)
    builder.Build()
    if builder.Shape().IsNull():
        raise ValueError('rigid transform produced a null shape')
    keys = ('write.precision.mode', 'write.precision.val', 'write.surfacecurve.mode')
    before = {keys[0]: Interface_Static.IVal_s(keys[0]),
              keys[1]: Interface_Static.RVal_s(keys[1]),
              keys[2]: Interface_Static.IVal_s(keys[2])}
    try:
        export_step(Compound(builder.Shape()), str(target), precision_mode=PrecisionMode.GREATEST)
    finally:
        Interface_Static.SetIVal_s(keys[0], before[keys[0]])
        Interface_Static.SetRVal_s(keys[1], before[keys[1]])
        Interface_Static.SetIVal_s(keys[2], before[keys[2]])
    after = {keys[0]: Interface_Static.IVal_s(keys[0]),
             keys[1]: Interface_Static.RVal_s(keys[1]),
             keys[2]: Interface_Static.IVal_s(keys[2])}
    if after != before:
        raise ValueError('STEP writer settings were not restored')
    return {'strategy': 'source_brep_rigid_transform_greatest_precision',
            'precision_mode': 'GREATEST', 'settings_before': before, 'settings_after': after}


def prepare_pose(gt, candidate, workdir, coordinate_mode):
    """Choose the evaluation STEP and retain the original/derived byte evidence."""
    if coordinate_mode not in ('geometry', 'strict'):
        raise ValueError('coordinate_mode must be geometry or strict')
    writer_keys = ('write.precision.mode', 'write.precision.val', 'write.surfacecurve.mode')
    writer_before = (Interface_Static.IVal_s(writer_keys[0]),
                     Interface_Static.RVal_s(writer_keys[1]),
                     Interface_Static.IVal_s(writer_keys[2]))
    gt, candidate, workdir = Path(gt), Path(candidate), Path(workdir)
    gt_hash, candidate_hash = _sha(gt), _sha(candidate)
    raw = {'coordinate_mode': coordinate_mode, 'gt_sha256': gt_hash,
           'candidate_sha256': candidate_hash, 'seed': SEED}
    chosen = candidate if coordinate_mode == 'strict' else None
    if chosen is not None:
        raw['evaluation_candidate_sha256'] = candidate_hash
    try:
        gt_bounds, _ = _scale(gt)
        candidate_bounds = _optimal_bounds(metrics.read_step(candidate)[0])
        raw['gt_bbox_center_mm'] = _center(gt_bounds)
        raw['candidate_bbox_center_mm'] = _center(candidate_bounds)
        raw['bbox_center_delta_mm'] = [a - b for a, b in zip(raw['candidate_bbox_center_mm'],
                                                                raw['gt_bbox_center_mm'])]
        workdir.mkdir(parents=True, exist_ok=True)
        target = workdir / 'candidate_aligned.step'
        provisional = workdir / 'candidate_alignment_unverified.step'
        if gt_hash == candidate_hash:
            copyfile(candidate, target)
            alignment = SimpleNamespace(rotation=np.eye(3), translation=np.zeros(3),
                                        rmse=0., output_path=target)
            raw['identity_shortcut'] = True
        else:
            alignment = align_step(candidate, gt, output=provisional, seed=SEED)
            raw['identity_shortcut'] = False
        rotation = np.asarray(alignment.rotation, dtype=float)
        translation = np.asarray(alignment.translation, dtype=float)
        determinant = float(np.linalg.det(rotation))
        orthogonality = float(np.linalg.norm(rotation.T @ rotation - np.eye(3), ord='fro'))
        rmse = float(alignment.rmse)
        if (rotation.shape != (3, 3) or translation.shape != (3,)
                or not np.isfinite(rotation).all() or not np.isfinite(translation).all()
                or not math.isfinite(rmse) or abs(determinant - 1) > 1e-5
                or orthogonality > 1e-5):
            raise ValueError('alignment is not a finite proper rigid transform')
        upstream_output = Path(alignment.output_path)
        expected_output = target if raw['identity_shortcut'] else provisional
        if upstream_output.resolve() != expected_output.resolve() or not upstream_output.is_file():
            raise ValueError('alignment output missing or at unexpected path')
        if _sha(gt) != gt_hash or _sha(candidate) != candidate_hash:
            raise ValueError('source STEP changed during alignment')
        if raw['identity_shortcut']:
            raw['export'] = {'strategy': 'byte_identical_copy'}
        else:
            raw['export'] = _export_rigid_step(candidate, rotation, translation, target)
            provisional.unlink(missing_ok=True)
        aligned = target
        if not aligned.is_file():
            raise ValueError('controlled alignment output missing')
        if _sha(gt) != gt_hash or _sha(candidate) != candidate_hash:
            raise ValueError('source STEP changed during controlled export')
        raw.update(R=rotation.tolist(), t=translation.tolist(), rmse=rmse,
                   rotation_matrix=rotation.tolist(), translation_mm=translation.tolist(),
                   rotation_determinant=determinant, rotation_orthogonality_error=orthogonality,
                   rotation_angle_deg=math.degrees(math.acos(max(-1., min(1., (np.trace(rotation)-1)/2)))),
                   translation_norm_mm=float(np.linalg.norm(translation)), rmse_mm=rmse,
                   aligned_candidate_sha256=_sha(aligned))
        # An exported solid must retain volume; this also catches accidental
        # scale/shear in a backend even when its reported matrix looks rigid.
        original_volume = metrics.geometry_info(*metrics.read_step(candidate))['signed_volume_mm3']
        aligned_volume = metrics.geometry_info(*metrics.read_step(aligned))['signed_volume_mm3']
        raw['volume_evidence'] = {'original_mm3': original_volume,
                                  'aligned_mm3': aligned_volume,
                                  'relative_change': abs(aligned_volume-original_volume)/abs(original_volume),
                                  'relative_budget': 1e-5}
        if abs(aligned_volume - original_volume) > max(1e-6, abs(original_volume) * 1e-5):
            raise ValueError('alignment export changed solid volume')
        if coordinate_mode == 'geometry':
            chosen = aligned
            raw['evaluation_candidate_sha256'] = raw['aligned_candidate_sha256']
        return chosen, metrics.result(score=None, raw=raw, reference_valid=True)
    except Exception as exc:
        raw['error'] = str(exc)
        return chosen, metrics.result('diagnostic_unreliable' if coordinate_mode == 'strict'
                                      else 'evaluator_error', raw=raw,
                                      reference_valid=True)
    finally:
        Interface_Static.SetIVal_s(writer_keys[0], writer_before[0])
        Interface_Static.SetRVal_s(writer_keys[1], writer_before[1])
        Interface_Static.SetIVal_s(writer_keys[2], writer_before[2])


def _artifacts(gt, candidate):
    bounds, diagonal = _scale(gt)
    deflection = deflection_for_bbox(diagonal)
    gt_art = StepArtifacts(gt, deflection_override=deflection)
    candidate_art = StepArtifacts(candidate, deflection_override=deflection)
    analysis = gt_art.analysis
    optimal_bbox = BBox(bounds[0], bounds[3], bounds[1], bounds[4], bounds[2], bounds[5])
    gt_art._analysis = replace(analysis,
                               measurements=replace(analysis.measurements,
                                                    bounding_box=optimal_bbox))
    return gt_art, candidate_art, diagonal, deflection


def shape(gt, candidate, workdir):
    """Run unchanged upstream IoU/F1/topology on an already chosen pose."""
    gt_art, candidate_art, diagonal, deflection = _artifacts(gt, candidate)
    fixed = asdict(compare_step_files(candidate, gt, align=False,
                                      candidate_artifacts=candidate_art, gt_artifacts=gt_art))
    topology = asdict(topo_match(candidate, gt,
                                 candidate_artifacts=candidate_art, gt_artifacts=gt_art))
    if fixed.get('metric_errors'):
        return metrics.result('evaluator_error', raw={'fixed': fixed, 'topology': topology})
    iou = metrics.finite_score(fixed['scores']['shape_volume_iou'])
    f1 = metrics.finite_score(fixed['scores']['shape_surface_distance_f1'])
    topo = metrics.finite_score(topology['score'])
    return metrics.result(score=(iou + f1 + topo) / 3, raw={
        'volumetric_iou': iou, 'surface_f1': f1, 'topology': topo,
        'official_diagnostics': fixed['diagnostics'],
        'topology_per_axis': topology.get('per_axis_scores'),
        'part_diagonal_mm': diagonal, 'mesh_deflection_mm': deflection,
        'scale_method': 'AddOptimal(useTriangulation=False)',
        'coordinate_policy': 'selected STEP pose; upstream align=False',
    })


def dimension(gt, candidate, ann):
    """Keep v3 probe decisions and add a plain explanation of each failure."""
    measured = metrics.dimension(gt, candidate, ann)
    items = measured.get('raw', {}).get('items', [])
    annotations = {item['id']: item for item in ann.get('items', [])}
    for item in items:
        original = annotations[item['id']]
        expected = item.get('expected')
        actual = item.get('measured')
        if isinstance(expected, (float, int)) and isinstance(actual, (float, int)):
            item['difference_mm'] = float(actual - expected) if original['type'] != 'cone' else None
            if original['type'] == 'cylinder':
                item['diameter_difference_mm'] = float(actual - expected)
        if original['type'] == 'torus':
            for key in ('minor', 'major'):
                actual_radius = item.get('measured_' + key)
                expected_radius = item.get('expected_' + key)
                if actual_radius is not None and expected_radius is not None:
                    item[key + '_radius_difference_mm'] = float(actual_radius - expected_radius)
        if item['pass']:
            item['failure_type'] = None
        elif actual is None and item.get('measured_minor') is None:
            item['failure_type'] = 'feature_not_found'
        elif item.get('expected_count') is not None and item.get('count') != item['expected_count']:
            item['failure_type'] = 'face_count_mismatch'
            item['face_count_difference'] = item['count'] - item['expected_count']
        elif item.get('count') is not None and item['count'] != original.get('count', item['count']):
            item['failure_type'] = 'face_count_mismatch'
            item['face_count_difference'] = item['count'] - original['count']
        elif item.get('pattern_radius', {}).get('pass') is False:
            pattern = item['pattern_radius']
            item['failure_type'] = 'pattern_radius_mismatch'
            item['pattern_radius_difference_mm'] = [float(value - pattern['expected'])
                                                    for value in pattern['measured']]
        elif (item.get('pattern_positions', {}).get('pairwise_pass') is False
              or item.get('pattern_positions', {}).get('centroid_pass') is False):
            pattern = item['pattern_positions']
            item['failure_type'] = 'pattern_position_mismatch'
            actual_pairs = pattern['pairwise_measured']
            expected_pairs = pattern['pairwise_expected']
            if len(actual_pairs) == len(expected_pairs):
                item['pattern_pairwise_difference_mm'] = [float(a - b) for a, b in
                                                          zip(actual_pairs, expected_pairs)]
            if pattern['centroid_xy'] is not None and 'centroid_within' in original:
                item['pattern_centroid_norm_mm'] = math.hypot(*pattern['centroid_xy'])
                item['pattern_centroid_excess_mm'] = max(
                    0., item['pattern_centroid_norm_mm'] - original['centroid_within'])
        else:
            item['failure_type'] = 'outside_tolerance'
    if items:
        measured['raw']['interpretation'] = ('GT-derived BRep probes; not complete drawing-based '
                                              'position, depth, or GD&T certification')
    return measured


def _distance_summary(points, mesh):
    distances, _ = closest_point_distances_and_normals(points, mesh.vertices, mesh.triangles)
    if not np.isfinite(distances).all():
        raise ValueError('non-finite point-to-surface distances')
    return {'max': float(np.max(distances)), 'p99': float(np.percentile(distances, 99)),
            'p95': float(np.percentile(distances, 95)), 'mean': float(np.mean(distances))}


def distance(gt, candidate):
    """Diagnostic p99 of sampled points to the opposite mesh surface."""
    _, diagonal = _scale(gt)
    deflection = deflection_for_bbox(diagonal)
    gt_mesh = robust_metrics._mesh_for(gt, deflection)
    candidate_mesh = robust_metrics._mesh_for(candidate, deflection)
    gt_points, _ = sample_points_and_normals_from_mesh(
        gt_mesh.vertices, gt_mesh.triangles, DISTANCE_SAMPLES, SEED)
    candidate_points, _ = sample_points_and_normals_from_mesh(
        candidate_mesh.vertices, candidate_mesh.triangles, DISTANCE_SAMPLES, SEED)
    gt_to_candidate = _distance_summary(gt_points, candidate_mesh)
    candidate_to_gt = _distance_summary(candidate_points, gt_mesh)
    p99 = max(gt_to_candidate['p99'], candidate_to_gt['p99'])
    tau = max(1e-6, .005 * diagonal)
    return metrics.result(score=math.exp(-p99 / (10 * tau)), raw={
        'gt_to_candidate': gt_to_candidate, 'candidate_to_gt': candidate_to_gt,
        'symmetric_p99_mm': p99,
        'symmetric_mean_cd1_mm': (gt_to_candidate['mean'] + candidate_to_gt['mean']) / 2,
        'part_diagonal_mm': diagonal, 'tau_mm': tau,
        'samples_per_side': DISTANCE_SAMPLES, 'seed': SEED,
        'distance_backend': 'point_to_mesh_surface',
        'scale_method': 'AddOptimal(useTriangulation=False)',
        'limitation': 'sampled query points and tessellated target surface approximate BRep geometry',
    })

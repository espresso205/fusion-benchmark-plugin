"""Angle: annotation-driven key-dimension pass rate (M08 generalized).

Probes the candidate BRep against GT annotation items (construction-spec nominals,
drawing-style tolerances). Four probe types:
  cylinder   - diameter multiset + count; optional circular pattern radius or
               rectangular pattern positions checked via rotation-invariant pairwise
               axis distances plus a centroid gate (translation-sensitive)
  torus      - fillet/chamfer-blend minor AND major radius
  cone       - semi-angle (chamfer angle)
  plane_pair - distance between parallel planes along an axis; optional 'near'
               centroid filter disambiguates same-distance pairs

No repository ships a comparable BRep dimension scorer; this is a local proposal,
reusing the OCP BRep traversal pattern from metrics_20261013/scripts/additional_checks.py.
"""
import sys, json, math, traceback
from pathlib import Path
import numpy as np

from OCP.STEPControl import STEPControl_Reader
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_FACE
from OCP.TopoDS import TopoDS
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.GeomAbs import GeomAbs_Plane, GeomAbs_Cylinder, GeomAbs_Torus, GeomAbs_Cone
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps

DIR_TOL_DEG = 5.0          # plane parallel-to-axis acceptance (pose-tolerant matching)
COS_DIR = math.cos(math.radians(DIR_TOL_DEG))


def load_faces(path):
    r = STEPControl_Reader()
    assert int(r.ReadFile(str(path))) == 1, path
    r.TransferRoots()
    ex = TopExp_Explorer(r.OneShape(), TopAbs_FACE)
    out = []
    while ex.More():
        f = TopoDS.Face_s(ex.Current())
        s = BRepAdaptor_Surface(f)
        t = s.GetType()
        info = {'type': None}
        if t == GeomAbs_Plane:
            pl = s.Plane(); ax = pl.Axis(); d = ax.Direction(); p = ax.Location()
            info = {'type': 'plane', 'normal': (d.X(), d.Y(), d.Z()), 'point': (p.X(), p.Y(), p.Z())}
        elif t == GeomAbs_Cylinder:
            cy = s.Cylinder(); ax = cy.Axis(); d = ax.Direction(); p = ax.Location()
            info = {'type': 'cylinder', 'radius': cy.Radius(),
                    'axis_dir': (d.X(), d.Y(), d.Z()), 'axis_loc': (p.X(), p.Y(), p.Z())}
        elif t == GeomAbs_Torus:
            to = s.Torus()
            info = {'type': 'torus', 'major': to.MajorRadius(), 'minor': to.MinorRadius()}
        elif t == GeomAbs_Cone:
            co = s.Cone()
            info = {'type': 'cone', 'semi_angle_deg': math.degrees(co.SemiAngle())}
        if info['type']:
            gp = GProp_GProps(); BRepGProp.SurfaceProperties_s(f, gp)
            cm = gp.CentreOfMass()
            info['centroid'] = (cm.X(), cm.Y(), cm.Z())
            out.append(info)
        ex.Next()
    return out


def pairwise_distances(pts):
    return sorted(float(np.linalg.norm(np.array(a) - np.array(b)))
                  for i, a in enumerate(pts) for b in pts[i + 1:])


def multiset_close(a, b, tol):
    a, b = list(a), list(b)
    if len(a) != len(b):
        return False
    for x in a:
        match = min(b, key=lambda y: abs(x - y), default=None)
        if match is None or abs(x - match) > tol:
            return False
        b.remove(match)
    return True


def check_cylinder(item, faces):
    cyls = [f for f in faces if f['type'] == 'cylinder']
    tol = item['tol']
    m = [f for f in cyls if abs(2 * f['radius'] - item['nominal']) <= tol]
    fallback = False
    if not m:
        # keep the diagnostic: associate the nearest-diameter face set instead
        if cyls:
            near = min(cyls, key=lambda f: abs(2 * f['radius'] - item['nominal']))
            dia = 2 * near['radius']
            m = [f for f in cyls if abs(2 * f['radius'] - dia) <= tol]
            fallback = True
        else:
            return {'id': item['id'], 'pass': False, 'expected': item['nominal'],
                    'measured': None, 'reason': 'no cylindrical faces'}
    measured_dia = 2 * sorted(f['radius'] for f in m)[0]
    dia_ok = abs(measured_dia - item['nominal']) <= tol
    count_ok = len(m) == item['count']
    detail = {'id': item['id'], 'pass': bool(dia_ok and count_ok),
              'expected': item['nominal'], 'measured': measured_dia,
              'count': len(m), 'expected_count': item['count'],
              'fallback_association': fallback}
    if 'pattern_radius' in item:
        rs = [math.hypot(f['axis_loc'][0], f['axis_loc'][1]) for f in m]
        ok = all(abs(x - item['pattern_radius']) <= item.get('pattern_tol', 0.1) for x in rs)
        detail['pattern_radius'] = {'expected': item['pattern_radius'],
                                    'measured': [round(x, 4) for x in rs], 'pass': bool(ok)}
        detail['pass'] = bool(detail['pass'] and ok)
    if 'pattern_positions' in item:
        pts = [[f['axis_loc'][0], f['axis_loc'][1]] for f in m]
        got = pairwise_distances(pts)
        want = pairwise_distances(item['pattern_positions'])
        dist_ok = multiset_close(got, want, item.get('pattern_tol', 0.1))
        cen = np.mean(pts, axis=0) if pts else None
        cen_ok = True
        if cen is None:
            cen_ok = False
        elif 'centroid_within' in item:
            cen_ok = float(np.linalg.norm(cen)) <= item['centroid_within']
        detail['pattern_positions'] = {
            'pairwise_measured': got, 'pairwise_expected': want,
            'centroid_xy': [round(float(x), 4) for x in cen] if cen is not None else None,
            'pairwise_pass': bool(dist_ok), 'centroid_pass': bool(cen_ok)}
        detail['pass'] = bool(detail['pass'] and dist_ok and cen_ok)
    return detail


def check_torus(item, faces):
    tori = [f for f in faces if f['type'] == 'torus']
    tol = item['minor_tol']
    m = [f for f in tori if abs(f['minor'] - item['minor_nominal']) <= tol]
    fallback = False
    if not m and tori:
        near = min(tori, key=lambda f: abs(f['minor'] - item['minor_nominal']))
        m = [f for f in tori if abs(f['minor'] - near['minor']) <= tol]
        fallback = True
    if not m:
        return {'id': item['id'], 'pass': False, 'expected_minor': item['minor_nominal'],
                'measured_minor': None, 'reason': 'no torus faces'}
    minor = sorted(f['minor'] for f in m)[0]
    major = sorted(f['major'] for f in m)[0]
    minor_ok = abs(minor - item['minor_nominal']) <= tol
    major_ok = abs(major - item['major_nominal']) <= item['major_tol']
    count_ok = len(m) == item.get('count', 1)
    return {'id': item['id'], 'pass': bool(minor_ok and major_ok and count_ok),
            'expected_minor': item['minor_nominal'], 'measured_minor': minor,
            'expected_major': item['major_nominal'], 'measured_major': major,
            'count': len(m), 'fallback_association': fallback}


def check_cone(item, faces):
    cones = [f for f in faces if f['type'] == 'cone']
    tol = item['tol']
    m = [f for f in cones if abs(f['semi_angle_deg'] - item['nominal']) <= tol]
    if not m and cones:
        near = min(cones, key=lambda f: abs(f['semi_angle_deg'] - item['nominal']))
        m = [f for f in cones if abs(f['semi_angle_deg'] - near['semi_angle_deg']) <= tol]
    if not m:
        return {'id': item['id'], 'pass': False, 'expected': item['nominal'],
                'measured': None, 'reason': 'no cone faces'}
    ang = sorted(f['semi_angle_deg'] for f in m)[0]
    return {'id': item['id'], 'pass': bool(abs(ang - item['nominal']) <= tol
                                           and len(m) == item.get('count', 1)),
            'expected': item['nominal'], 'measured': ang, 'count': len(m)}


AXIS = {'x': np.array([1.0, 0, 0]), 'y': np.array([0, 1.0, 0]), 'z': np.array([0, 0, 1.0])}


def check_plane_pair(item, faces):
    ax = AXIS[item['axis']]
    cand = []
    for f in faces:
        if f['type'] != 'plane':
            continue
        n = np.array(f['normal'])
        if abs(float(n @ ax)) < COS_DIR:
            continue
        c = np.array(f['centroid'])
        if 'near' in item and float(np.linalg.norm(c - np.array(item['near']))) > item['near_radius']:
            continue
        cand.append(float(c @ ax))
    if len(cand) < 2:
        return {'id': item['id'], 'pass': False, 'expected': item['nominal'],
                'measured': None, 'reason': 'fewer than 2 matching parallel planes',
                'matched_planes': len(cand)}
    best, best_pair = None, None
    for i in range(len(cand)):
        for j in range(i + 1, len(cand)):
            d = abs(cand[i] - cand[j])
            if best is None or abs(d - item['nominal']) < abs(best - item['nominal']):
                best, best_pair = d, sorted((round(cand[i], 4), round(cand[j], 4)))
    return {'id': item['id'], 'pass': bool(abs(best - item['nominal']) <= item['tol']),
            'expected': item['nominal'], 'measured': round(best, 4),
            'measured_from_planes': best_pair, 'matched_planes': len(cand)}


CHECKS = {'cylinder': check_cylinder, 'torus': check_torus, 'cone': check_cone,
          'plane_pair': check_plane_pair}


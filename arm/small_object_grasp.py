"""Profiles for the two demo objects, not a general RGB depth estimator.

The white cylinder dimensions were measured by the operator. Its projected
shape can select an orientation-dependent grip when hypotheses are sufficiently
separated. The cap has only a supplied range; neither profile measures depth.
"""
import re

import cv2
import numpy as np

CYLINDER = {
    'name': 'white cylinder', 'length_cm': 2., 'diameter_cm': 1.,
    'grip_height_cm': .5, 'height_hint_cm': 1.5,
    'prompts': ('small white cylinder', 'white cylinder lying on its side',
                'short white cylindrical piece'),
}
CAP = {
    'name': 'black cap', 'height_range_cm': [.5, 1.],
    'grip_height_cm': .5, 'height_hint_cm': .75,
    'prompts': ('small black cap', 'small black plastic cap',
                'small black capsule-shaped cap'),
}


def profile_for(text):
    words = set(re.findall(r'[a-z]+', (text or '').lower()))
    if {'white', 'cylinder'} <= words and 'black' not in words:
        return dict(CYLINDER)
    if {'black', 'cap'} <= words and 'white' not in words:
        return dict(CAP)
    return None


def height_decision(profile, fit=None):
    """Choose a height from accepted geometry, or explicitly label the fallback.

    Fit thresholds are software heuristics, not calibrated probabilities. The
    caller must reject fits inconsistent with the reachable standing-camera aim.
    """
    result = {
        'closing_height_cm': profile['grip_height_cm'],
        'object_height_cm': None,
        'hover_height_hint_cm': profile['height_hint_cm'],
        'depth_measured': False,
        'orientation_accepted': False,
    }
    if profile['name'] == 'black cap':
        result.update(source='cap_height_range_fallback',
                      height_range_cm=profile['height_range_cm'],
                      reason='cap height is only known to be 0.5–1.0 cm; using the 0.5 cm clearance floor, not a measured height')
        return result
    result.update(source='cylinder_pose_uncertain_fallback',
                  height_range_cm=[profile['diameter_cm'], profile['length_cm']],
                  reason='cylinder pose is uncertain or geometry unavailable; using the common 0.5 cm low grip')
    if not fit or fit.get('source') != 'known_cylinder_silhouette':
        return result
    orientation = fit.get('orientation')
    if orientation not in ('upright', 'sideways'):
        return result
    scores = fit.get('orientation_scores', {})
    own, other = scores.get(orientation, 0.), scores.get('sideways' if orientation == 'upright' else 'upright', 0.)
    iou = fit.get('fit_iou', 0.)
    extent = fit.get('mask_extent_px', [0., 0.])
    if (not np.isfinite([own, other, iou]).all() or iou < .7 or own - other < .12
            or fit.get('mask_area_px', 0) < 80 or min(extent) < 6
            or fit.get('touches_frame_edge', False)):
        return result
    height = profile['length_cm'] if orientation == 'upright' else profile['diameter_cm']
    result.update(source='cylinder_pose_and_known_dimensions', orientation=orientation,
                  orientation_accepted=True, object_height_cm=height, hover_height_hint_cm=height,
                  closing_height_cm=max(.5, height / 2),
                  reason=f'{orientation} cylinder shape with known dimensions: {height:g} cm high, grip at half height')
    return result


def detect(img, text, detector, exhaustive=False):
    """A small alias set for the same named object; never a generic-object fallback."""
    profile = profile_for(text)
    if profile is None:
        return detector(img, text, None)
    candidates = []
    prompts = list(dict.fromkeys([text, *profile['prompts']]))
    for prompt in prompts:
        result = detector(img, prompt, None)
        for candidate in result['candidates']:
            c = dict(candidate)
            c['matched_prompt'] = prompt
            mask = np.asarray(c['mask']).astype(bool)
            c['mask'] = mask
            if not mask.any():
                continue
            duplicate = next((k for k in candidates
                if np.count_nonzero(mask & k['mask']) / max(1, np.count_nonzero(mask | k['mask'])) >= .6), None)
            if duplicate is None:
                c['mask'] = mask
                candidates.append(c)
            elif c['target_score'] > duplicate['target_score']:
                duplicate.update(c)
        # A strong same-object match is enough. Weak/background-only results
        # still get the other named-object prompts and the caller's zoom retry.
        if not exhaustive and any(c['target_score'] >= .65 for c in candidates):
            break
    return {'status': 'PROFILE_DETECTION', 'candidates': candidates, 'chosen': None}


def cylinder_points(profile, orientation, yaw=0.):
    radius, length = profile['diameter_cm']/2, profile['length_cm']
    angles = np.linspace(0, 2*np.pi, 32, endpoint=False)
    if orientation == 'upright':
        return np.array([[radius*np.cos(a), radius*np.sin(a), z]
                         for z in (0.,length) for a in angles])
    axis = np.array([np.cos(yaw), np.sin(yaw), 0.])
    across = np.array([-np.sin(yaw), np.cos(yaw), 0.])
    return np.array([axis*s + across*radius*np.cos(a) + np.array([0.,0.,radius*(1+np.sin(a))])
                     for s in (-length/2,length/2) for a in angles])


def project(points, geometry):
    K,R,t,_,up = geometry
    points = np.array(points, dtype=float, copy=True)
    points[:,2] *= up[2]
    camera = points @ R.T + np.asarray(t).reshape(1,3)
    if np.any(camera[:,2] <= 1e-6):
        raise ValueError('Object projection behind camera')
    pixels = camera @ K.T
    return pixels[:,:2] / pixels[:,2:3]


def centre_on_height(pixel, height_cm, geometry):
    K,R,_,C,up = geometry
    d = R.T @ np.linalg.solve(K, np.array([*pixel,1.]))
    if abs(d[2]) < 1e-8:
        raise ValueError('Camera ray parallel to table')
    scale = (height_cm*up[2] - C[2])/d[2]
    if scale <= 0:
        raise ValueError('Object height behind camera')
    return (C + scale*d)[:2]


def aim_from_mask(profile, mask, geometry):
    """Known-cylinder silhouette hypotheses, or low-cap centroid projection.

    Returned coordinates depend on the existing approximate camera calibration.
    height_decision applies stricter evidence checks before changing grip height.
    """
    mask = np.asarray(mask).astype(bool)
    ys,xs = np.nonzero(mask)
    if len(xs) < 12:
        raise ValueError('Too few segmented pixels to fit object centre')
    pixel = np.array([xs.mean(),ys.mean()])
    if profile['name'] == 'black cap':
        xy = centre_on_height(pixel, .5, geometry)
        return {'xy_cm': xy.tolist(), 'orientation': 'low cap; height range 0.5–1.0 cm',
                'fit_iou': None, 'source': 'cap_centroid_height_prior'}
    hypotheses = [('upright',0.)] + [('sideways',a) for a in np.arange(0,np.pi,np.pi/12)]
    margin = max(12, int(max(np.ptp(xs),np.ptp(ys))))
    left,top = max(0,xs.min()-margin), max(0,ys.min()-margin)
    right,bottom = min(mask.shape[1],xs.max()+margin+1), min(mask.shape[0],ys.max()+margin+1)
    observed = mask[top:bottom,left:right]
    best = None
    scores = {'upright':0., 'sideways':0.}
    for orientation,yaw in hypotheses:
        points = cylinder_points(profile,orientation,yaw)
        h = profile['length_cm']/2 if orientation == 'upright' else profile['diameter_cm']/2
        initial = centre_on_height(pixel,h,geometry)
        for dx in (-.3,0.,.3):
            for dy in (-.3,0.,.3):
                xy = initial + [dx,dy]
                projected = project(points + [xy[0],xy[1],0.],geometry)
                hull = cv2.convexHull(np.rint(projected-[left,top]).astype(np.int32))
                rendered = np.zeros(observed.shape,np.uint8)
                cv2.fillConvexPoly(rendered,hull,1)
                overlap = np.count_nonzero(observed & rendered.astype(bool))
                union = np.count_nonzero(observed | rendered.astype(bool))
                iou = overlap/max(1,union)
                scores[orientation] = max(scores[orientation],float(iou))
                if best is None or iou > best['fit_iou']:
                    best = {'xy_cm':xy.tolist(),'orientation':orientation,'fit_iou':float(iou),
                            'source':'known_cylinder_silhouette'}
    if best['fit_iou'] < .45 or abs(scores['upright']-scores['sideways']) < .08:
        # Do not publish an orientation guess as established posture. A midpoint
        # height prior keeps centre projection usable; the low grip fits both poses.
        best = {'xy_cm':centre_on_height(pixel,.75,geometry).tolist(),
                'orientation':'uncertain; low grip valid for both poses', 'fit_iou':best['fit_iou'],
                'source':'cylinder_centre_height_prior'}
    best['orientation_scores'] = scores
    best['mask_area_px'] = int(len(xs))
    best['mask_extent_px'] = [int(np.ptp(xs)+1), int(np.ptp(ys)+1)]
    best['touches_frame_edge'] = bool(xs.min() == 0 or ys.min() == 0
                                    or xs.max() == mask.shape[1]-1 or ys.max() == mask.shape[0]-1)
    return best

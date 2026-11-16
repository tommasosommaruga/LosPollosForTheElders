"""Automatic named-object profiles: geometry and routing, without SAM/hardware."""
import io
import types
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

import cv2
import numpy as np
from PIL import Image

import grab_text as grab
import small_object_grasp as small


def geometry():
    centre = np.array([0.,-35.,45.])
    forward = np.array([0.,25.,0.])-centre
    forward /= np.linalg.norm(forward)
    right = np.cross(forward,[0.,0.,1.]); right /= np.linalg.norm(right)
    down = np.cross(forward,right)
    rotation = np.array([right,down,forward])
    intrinsics = np.array([[900.,0.,640.],[0.,900.,360.],[0.,0.,1.]])
    return intrinsics,rotation,-rotation@centre,centre,np.array([0.,0.,1.])


def cylinder_mask(orientation, yaw=0., xy=(3.,25.)):
    points = small.cylinder_points(small.CYLINDER,orientation,np.radians(yaw)) + [*xy,0.]
    pixels = small.project(points,geometry())
    mask = np.zeros((720,1280),np.uint8)
    cv2.fillConvexPoly(mask,cv2.convexHull(np.rint(pixels).astype(np.int32)),1)
    return mask.astype(bool)


def candidate(mask, score=.8):
    ys,xs = np.nonzero(mask)
    return {'mask':mask,'target_score':score,'box':[xs.min(),ys.min(),xs.max()+1,ys.max()+1]}


class SmallObjectTests(unittest.TestCase):
    def test_profiles_match_names_without_changing_command_for_orientation(self):
        for text in ('small white cylinder','white cylinder lying sideways','pick up the white cylinder'):
            self.assertEqual(small.profile_for(text)['name'],'white cylinder')
        self.assertEqual(small.profile_for('small black cap')['name'],'black cap')
        for text in ('white bottle','black cylinder','small object','white cap'):
            self.assertIsNone(small.profile_for(text))

    def test_orientation_selects_cylinder_height_automatically(self):
        args = types.SimpleNamespace(last_text='small white cylinder',close=None,object_height=None,above=14.)
        with patch.object(grab,'estimate_height',side_effect=AssertionError('upright-only height guess forbidden')):
            for orientation, expected in [('upright',1.),('sideways',.5)]:
                mask = cylinder_mask(orientation)
                fit = small.aim_from_mask(small.CYLINDER,mask,geometry())
                close,above,est = grab.grab_heights(args,mask,(1280,720),aim=fit)
                self.assertEqual(close,expected)
                self.assertEqual(est[0],2*expected)
                self.assertTrue(args.height_decision['orientation_accepted'])
                self.assertFalse(args.height_decision['depth_measured'])
                self.assertEqual(above,14.)
                self.assertEqual(est[1],1.)

    def test_ambiguous_missing_tiny_or_clipped_pose_does_not_raise_grip(self):
        fit = small.aim_from_mask(small.CYLINDER,cylinder_mask('upright'),geometry())
        for rejected in (None, {**fit,'fit_iou':.6},
                         {**fit,'orientation_scores':{'upright':.95,'sideways':.90}},
                         {**fit,'orientation_scores':{'upright':float('nan'),'sideways':.1}},
                         {**fit,'mask_area_px':30}, {**fit,'mask_extent_px':[4,30]},
                         {**fit,'touches_frame_edge':True}):
            with self.subTest(rejected=rejected):
                decision = small.height_decision(small.CYLINDER,rejected)
                self.assertEqual(decision['closing_height_cm'],.5)
                self.assertFalse(decision['orientation_accepted'])
                self.assertIsNone(decision['object_height_cm'])
                self.assertEqual(decision['source'],'cylinder_pose_uncertain_fallback')

    def test_cap_uses_low_profile_instead_of_15mm_fallback(self):
        args = types.SimpleNamespace(last_text='small black cap',close=None,object_height=None,above=14.)
        with patch.object(grab,'estimate_height',side_effect=AssertionError('generic height guess forbidden')):
            close,above,est = grab.grab_heights(args,None,(1280,720))
        self.assertEqual(close,.5)
        self.assertEqual(est[0],.75)  # explicit range midpoint hint, not a measured height
        self.assertIsNone(args.height_decision['object_height_cm'])
        self.assertFalse(args.height_decision['depth_measured'])
        self.assertEqual(args.height_decision['source'],'cap_height_range_fallback')
        self.assertEqual(small.CAP['height_range_cm'],[.5,1.])

    def test_projected_cylinder_centres_across_orientations_and_positions(self):
        for xy in ((3.,25.),(-8.,23.),(7.,30.)):
            for orientation,yaw in [('upright',0),('sideways',0),('sideways',45),('sideways',90),('sideways',135)]:
                with self.subTest(xy=xy,orientation=orientation,yaw=yaw):
                    fit = small.aim_from_mask(small.CYLINDER,cylinder_mask(orientation,yaw,xy),geometry())
                    self.assertLess(np.linalg.norm(np.array(fit['xy_cm'])-xy),.4)
                    self.assertGreater(fit['fit_iou'],.75)
                    decision = small.height_decision(small.CYLINDER,fit)
                    self.assertTrue(decision['orientation_accepted'])
                    self.assertEqual(decision['closing_height_cm'],1. if orientation == 'upright' else .5)

    def test_missing_default_phrase_retries_sideways_phrase(self):
        c = candidate(cylinder_mask('sideways'))
        detector = Mock(side_effect=[{'candidates':[]},{'candidates':[c]}])
        result = small.detect(Image.new('RGB',(1280,720)),'small white cylinder',detector)
        self.assertEqual(detector.call_args_list[1].args[1],'white cylinder lying on its side')
        self.assertEqual(len(result['candidates']),1)

    def test_named_prompt_retries_do_not_fall_back_to_any_small_object(self):
        detector = Mock(return_value={'candidates':[]})
        small.detect(Image.new('RGB',(1280,720)),'small black cap',detector)
        self.assertGreater(detector.call_count,1)
        for call in detector.call_args_list:
            self.assertIn('black',call.args[1])
            self.assertIn('cap',call.args[1])

    def test_duplicate_masks_are_not_separate_objects(self):
        c = candidate(cylinder_mask('upright'),.4)
        detector = Mock(return_value={'candidates':[c]})
        result = small.detect(Image.new('RGB',(1280,720)),'small white cylinder',detector,exhaustive=True)
        self.assertEqual(len(result['candidates']),1)
        self.assertGreater(detector.call_count,1)

    def test_background_match_does_not_prevent_table_zoom_retry(self):
        image = Image.new('RGB',(1280,720)); encoded=io.BytesIO(); image.save(encoded,format='JPEG')
        c = candidate(cylinder_mask('sideways'))
        args = types.SimpleNamespace(arm_url='http://offline-test',close=None,object_height=None,above=14.)
        response = Mock(json=lambda:{'capabilities':{'controller_build':'descent-taper-v1'}},content=encoded.getvalue())
        with patch.object(grab.requests,'get',return_value=response), \
                patch.object(Image.Image,'save'), patch('pathlib.Path.mkdir'), \
                patch.object(grab,'detect_fixed',side_effect=[{'candidates':[dict(c)]},{'candidates':[dict(c)]}]) as detect, \
                patch.object(grab,'in_reach',side_effect=[(None,False),((3.,25.),True)]), \
                patch.object(grab,'camera_geometry',return_value=None), patch.object(grab,'show_view'), \
                patch.object(grab,'go') as go, redirect_stdout(io.StringIO()):
            grab.find_and_grab(args,'small white cylinder')
        self.assertTrue(detect.call_args_list[1].kwargs['force_zoom'])
        self.assertEqual(go.call_args.args[6],.5)

    def test_fit_returns_table_centre_not_lowest_silhouette_pixel(self):
        mask = cylinder_mask('sideways',45)
        fit = small.aim_from_mask(small.CYLINDER,mask,geometry())
        self.assertLess(np.linalg.norm(np.array(fit['xy_cm'])-[3.,25.]),.1)
        low = grab.base_pixel(mask)
        centre_pixel = small.project(np.array([[*fit['xy_cm'],0.]]),geometry())[0]
        self.assertGreater(np.linalg.norm(np.array(low)-centre_pixel),1.)

    def test_request_fits_pose_before_selecting_height_and_rejects_displaced_fit(self):
        image=Image.new('RGB',(1280,720)); encoded=io.BytesIO(); image.save(encoded,format='JPEG')
        response=Mock(json=lambda:{'capabilities':{}},content=encoded.getvalue())
        for orientation, accepted_xy, expected in [('upright',(3.,25.),1.),
                                                   ('sideways',(3.,25.),.5),
                                                   ('upright',(10.,25.),.5)]:
            args=types.SimpleNamespace(arm_url='http://offline-test',close=None,object_height=None,above=14.)
            c=candidate(cylinder_mask(orientation))
            with self.subTest(orientation=orientation,accepted_xy=accepted_xy), \
                    patch.object(grab.requests,'get',return_value=response), \
                    patch.object(Image.Image,'save'), patch('pathlib.Path.mkdir'), \
                    patch.object(grab,'detect_fixed',return_value={'candidates':[c]}), \
                    patch.object(grab,'in_reach',return_value=(accepted_xy,True)), \
                    patch.object(grab,'camera_geometry',return_value=geometry()), \
                    patch.object(grab,'show_view'), patch.object(grab,'go') as go, \
                    redirect_stdout(io.StringIO()):
                grab.find_and_grab(args,'small white cylinder')
                self.assertEqual(go.call_args.args[6],expected)
                if accepted_xy != (3.,25.):
                    self.assertIsNone(go.call_args.kwargs['aim'])
                    self.assertFalse(args.height_decision['orientation_accepted'])

    def test_profile_preview_never_moves_or_uses_wrist(self):
        args=types.SimpleNamespace(last_text='small white cylinder',close=None,object_height=None,
                                   above=14.,side=False,no_feel=False,dry=True)
        fit=small.aim_from_mask(small.CYLINDER,cylinder_mask('sideways'),geometry())
        with patch.object(grab.requests,'post',side_effect=AssertionError('No POST in fitted preview')), \
                patch.object(grab,'hand_correct',side_effect=AssertionError('No wrist movement in preview')), \
                redirect_stdout(io.StringIO()):
            grab.go(args,Image.new('RGB',(1280,720)),600,600,'small white cylinder',.8,
                    close=.5,above=14.,est=(1.5,1.,None),aim=fit)


if __name__ == '__main__':
    unittest.main()

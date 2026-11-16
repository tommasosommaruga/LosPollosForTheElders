"""Wrong-region and identity regressions for wrist SAM outputs (inference mocked)."""
import io
import json
import types
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch, mock_open

import numpy as np
from PIL import Image
import grab_text as grab


def candidate(x, y, score, size=(1280, 720)):
    mask = np.zeros((size[1], size[0]), dtype=bool)
    mask[int(y)-5:int(y)+6, int(x)-5:int(x)+6] = True
    return {'mask': mask, 'target_score': score, 'box': [x-5, y-5, x+6, y+6]}


class WristSelectionTests(unittest.TestCase):
    def test_stale_wrist_frame_is_not_used_for_alignment(self):
        response = {'frames': {'hand': {'jpeg_base64':'not-decoded', 'receive_age_s':3.}}}
        with patch.object(grab.requests,'get',return_value=Mock(json=lambda:response)):
            with self.assertRaisesRegex(RuntimeError, 'stale'):
                grab.hand_frame(types.SimpleNamespace(arm_url='http://offline-test'))

    def test_tiny_cap_match_is_not_discarded_by_large_object_area_cutoff(self):
        mask=np.zeros((720,1280),dtype=bool); mask[423:428,618:623]=True
        img=Image.new('RGB',(1280,720))
        response={'candidates':[{'mask':mask,'box':[618,423,623,428],'target_score':.8}]}
        with patch.object(grab,'hand_frame',return_value=img), \
                patch.object(grab.ts,'decide',return_value=response), patch('time.sleep'):
            point,_,_=grab.find_in_hand(types.SimpleNamespace(),'small black cap',near=(620.,425.),tries=1)
        self.assertEqual(point,(620.,425.))

    def find(self, responses, tries=2):
        img = Image.new('RGB', (1280, 720))
        with patch.object(grab, 'hand_frame', return_value=img), \
                patch.object(grab.ts, 'decide', side_effect=responses) as detect, \
                patch('time.sleep'), redirect_stdout(io.StringIO()):
            result = grab.find_in_hand(types.SimpleNamespace(), 'small bottle', near=(620.,425.), tries=tries)
        return result, detect

    def test_distant_high_score_does_not_suppress_local_bottle(self):
        # Matches the bundle geometry: highlighted upper-right region, bottle below jaws.
        (point, _, _), detect = self.find([{'candidates': [
            candidate(1205, 20, .99), candidate(620, 575, .3)]}])
        self.assertEqual(point, (620., 575.))
        self.assertEqual(detect.call_count, 1)

    def test_no_local_match_never_switches_to_generic_small_white_object(self):
        (point, _, _), detect = self.find([
            {'candidates': [candidate(1205,20,.99)]}, {'candidates': []}])
        self.assertIsNone(point)
        self.assertEqual(detect.call_count, 2)
        for call in detect.call_args_list:
            self.assertEqual(call.args[1:], ('small bottle', None))

    def test_cropped_retry_restores_full_image_mask_and_coordinates(self):
        # Crop around (620,425): left=300, top=105, right=940, bottom=720.
        (point, img, best), detect = self.find([
            {'candidates': []}, {'candidates': [candidate(320,470,.4,(640,615))]}])
        self.assertEqual(point, (620.,575.))
        self.assertEqual(best['mask'].shape, (720,1280))
        self.assertEqual(detect.call_args_list[1].args[0].size, (640,615))

    def align(self, points, text='small bottle', decision=None):
        args = types.SimpleNamespace(no_hand=False, arm_url='http://offline-test',height_decision=decision)
        cal = {'target_px': [620.,425.], 'px_per_cm': [[17.25,0.],[0.,17.25]],
               'at': [0.,25.], 'look_above': 13.}
        img = Image.new('RGB',(1280,720))
        with patch('os.path.exists',return_value=True), \
                patch('builtins.open',mock_open(read_data=json.dumps(cal))), \
                patch.object(grab.requests,'post',return_value=Mock()), \
                patch.object(grab,'goto') as move, patch.object(grab,'save_hand_view'), \
                patch.object(grab,'find_in_hand',side_effect=[(p,img,None) for p in points]), \
                redirect_stdout(io.StringIO()):
            xy = grab.hand_correct(args, text, 0.,25.,4.)
        return xy,args,move

    def test_large_valid_correction_uses_small_steps_and_observes_final_position(self):
        xy,args,move = self.align([(568.25,425.),(602.75,425.),(620.,425.)])
        self.assertEqual(xy,(3.,25.))
        self.assertTrue(args.wrist_alignment_used)
        self.assertEqual([c.args[1:] for c in move.call_args_list],
                         [(0.,25.,17.),(2.,25.,17.),(3.,25.,17.)])

    def test_lost_target_does_not_return_unobserved_corrected_position(self):
        xy,args,move = self.align([(568.25,425.),None])
        self.assertEqual(xy,(0.,25.))
        self.assertFalse(args.wrist_alignment_used)

    def test_no_unverified_extra_move_after_last_look(self):
        xy,args,move = self.align([(602.75,425.)]*5)
        self.assertEqual(xy,(0.,25.))
        self.assertFalse(args.wrist_alignment_used)
        self.assertEqual(move.call_count,5)

    def test_profile_corrections_are_half_centimetre_steps(self):
        xy,args,move = self.align([(594.125,425.),(602.75,425.),(611.375,425.),(620.,425.)],
                                 text='small white cylinder',
                                 decision={'orientation_accepted':True,'orientation':'upright'})
        self.assertEqual(xy,(1.5,25.))
        self.assertTrue(args.wrist_alignment_used)
        self.assertEqual([c.args[1] for c in move.call_args_list],[0.,.5,1.,1.5])

    def test_profile_rejects_wrist_shift_larger_than_two_centimetres(self):
        xy,args,move = self.align([(568.25,425.)],text='small black cap')
        self.assertEqual(xy,(0.,25.))
        self.assertFalse(args.wrist_alignment_used)
        self.assertEqual(move.call_count,1)

    def test_fine_cap_corrects_offset_old_threshold_accepted(self):
        # 0.25 cm: the old 0.3 cm acceptance would have closed at the initial aim.
        xy,args,move = self.align([(615.6875,425.),(620.,425.),(620.,425.)],text='small black cap')
        self.assertEqual(xy,(.25,25.))
        self.assertTrue(args.wrist_alignment_used)
        self.assertEqual(move.call_count,2)  # confirmation does not move again
        self.assertEqual(args.wrist_report['status'],'aligned')
        self.assertEqual(len(args.wrist_report['observations']),3)
        self.assertEqual(args.wrist_report['tolerance_cm'],.15)

    def test_sideways_cylinder_requires_consistent_second_image(self):
        xy,args,move = self.align([(620.,425.),(620.,425.)],text='small white cylinder',
                                 decision={'orientation_accepted':True,'orientation':'sideways'})
        self.assertTrue(args.wrist_alignment_used)
        self.assertEqual(move.call_count,1)
        self.assertEqual(len(args.wrist_report['observations']),2)

    def test_single_good_frame_followed_by_lost_cap_is_not_confirmed(self):
        xy,args,move = self.align([(620.,425.),None],text='small black cap')
        self.assertFalse(args.wrist_alignment_used)
        self.assertEqual(move.call_count,1)
        self.assertEqual(args.wrist_report['status'],'target_lost')

    def test_jitter_inside_tolerance_still_needs_consistent_frames(self):
        # Both are within 0.15 cm, but they disagree by 0.24 cm.
        xy,args,move = self.align([(617.93,425.),(622.07,425.)]*4,text='small black cap')
        self.assertFalse(args.wrist_alignment_used)
        self.assertEqual(move.call_count,1)
        self.assertEqual(args.wrist_report['status'],'alignment_unconfirmed')

    def test_upright_cylinder_and_bottle_keep_existing_acceptance(self):
        for text in ('small white cylinder','small bottle'):
            xy,args,move = self.align([(615.6875,425.)],text=text,
                                     decision={'orientation_accepted':True,'orientation':'upright'})
            self.assertEqual(xy,(0.,25.))
            self.assertTrue(args.wrist_alignment_used)
            self.assertFalse(args.wrist_report['fine_alignment'])
            self.assertEqual(move.call_count,1)

    def test_repeated_frame_cannot_be_used_as_fresh_confirmation(self):
        import base64
        data=io.BytesIO(); Image.new('RGB',(40,30)).save(data,format='JPEG')
        frame={'camera_id':'hand','sequence':5,'receive_age_s':.1,
               'jpeg_base64':base64.b64encode(data.getvalue()).decode()}
        args=types.SimpleNamespace(arm_url='http://offline-test',require_new_hand_frame=True)
        with patch.object(grab.requests,'get',return_value=Mock(json=lambda:{'frames':{'hand':frame}})):
            grab.hand_frame(args)
            with self.assertRaises(grab.RepeatedHandFrame):
                grab.hand_frame(args)
            frame['sequence']=6
            self.assertEqual(grab.hand_frame(args).size,(40,30))

    def test_fine_last_look_does_not_issue_an_unobserved_correction(self):
        xy,args,move = self.align([(616.55,425.)]*8,text='small black cap')
        self.assertFalse(args.wrist_alignment_used)
        self.assertEqual(move.call_count,8)
        self.assertEqual(args.wrist_report['status'],'alignment_unconfirmed')


if __name__ == '__main__':
    unittest.main()

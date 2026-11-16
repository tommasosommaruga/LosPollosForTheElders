"""Both-camera defaults and explicit standing-only comparison. No robot I/O."""
from contextlib import redirect_stdout, redirect_stderr
import io
import unittest
from unittest.mock import Mock, patch

from PIL import Image
import grab_text as grab


class CameraModeTests(unittest.TestCase):
    def arguments(self, *flags):
        with patch.object(grab, 'find_and_grab') as run, redirect_stdout(io.StringIO()):
            grab.main(['--text', 'small white cylinder', *flags])
        args, text = run.call_args.args
        args.last_text = text
        return args

    def test_normal_command_defaults_to_both_cameras(self):
        args = self.arguments()
        self.assertFalse(args.no_hand)
        self.assertIsNone(args.close)
        self.assertFalse(args.no_feel)
        self.assertIsNone(args.reach_out)  # automatic: zero for the two small-object profiles

    def test_wrist_guidance_can_be_explicitly_disabled(self):
        self.assertFalse(self.arguments('--hand').no_hand)
        self.assertTrue(self.arguments('--no-hand').no_hand)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            grab.main(['--text', 'test', '--hand', '--no-hand'])

    def perform_pick(self, args):
        calls = []
        def post(url, json, **kwargs):
            calls.append((url, json))
            return Mock(json=lambda: {'ok': True, 'x': 0., 'y': 25.})
        with patch.object(grab.requests, 'post', side_effect=post), \
                patch.object(grab.requests, 'get', return_value=Mock(json=lambda: {'log':'test'})), \
                redirect_stdout(io.StringIO()):
            close = args.close if args.close is not None else 1.5
            grab.go(args, Image.new('RGB', (1280,720)), 500,500,'test',.8,close=close,above=14)
        return calls

    def test_normal_pick_never_calls_wrist_and_honours_planned_height(self):
        args = self.arguments('--no-hand')
        args.last_text = 'test object'
        with patch.object(grab, 'hand_correct', side_effect=AssertionError('Wrist guidance forbidden')), \
                patch.object(grab, 'find_in_hand', side_effect=AssertionError('Wrist SAM forbidden')), \
                patch.object(grab, 'goto', side_effect=AssertionError('Wrist search movement forbidden')):
            calls = self.perform_pick(args)
        self.assertEqual([url.rsplit('/',1)[1] for url,_ in calls], ['click','grab'])
        self.assertEqual(calls[-1][1], {'size':1280,'height':1.5,'above':14,'method':'top',
                                      'guard':True,'x':0.,'y':26.})

    def test_opt_in_reaches_existing_wrist_path(self):
        args = self.arguments('--hand')
        with patch.object(grab, 'hand_correct', return_value=(0.,25.)) as wrist:
            self.perform_pick(args)
        wrist.assert_called_once_with(args,'small white cylinder',0.,25.,None)

    def test_explicit_height_still_uses_the_friends_bounded_grab_request(self):
        args = self.arguments('--close','1','--no-hand')
        calls = self.perform_pick(args)
        self.assertTrue(calls[-1][1]['guard'])
        self.assertEqual(calls[-1][1]['height'],1.)
        self.assertNotIn('feel_all_the_way',calls[-1][1])

    def test_planned_height_still_adapts_to_estimate_and_marks_unknown_fallback(self):
        args = self.arguments()
        args.last_text = 'test object'
        for estimate, expected in (((2.,1.,None),1.5), ((10.,3.,None),5.), (None,1.5)):
            with self.subTest(estimate=estimate), patch.object(grab, 'estimate_height', return_value=estimate):
                close, above, _ = grab.grab_heights(args, None, (1280,720))
            self.assertEqual(close,expected)
            self.assertGreater(above,close)

    def test_measured_height_overrides_bad_rgb_estimate_for_small_objects(self):
        for height, expected in [(2.,1.), (1.,.5), (.6,.5), (4.,2.)]:
            with self.subTest(height=height):
                args = self.arguments('--object-height',str(height))
                with patch.object(grab,'estimate_height',return_value=(.7,1.,None)):
                    close, above, est = grab.grab_heights(args,None,(1280,720))
                self.assertEqual(close,expected)
                self.assertEqual(est[0],height)
                self.assertGreater(above,close)

    def test_unvalidated_sub_clearance_object_height_refuses_before_motion(self):
        for height in ('.4','0','nan','inf'):
            with self.subTest(height=height), redirect_stderr(io.StringIO()), \
                    patch.object(grab,'find_and_grab') as pick, self.assertRaises(SystemExit):
                grab.main(['--text','small black cap','--object-height',height])
            pick.assert_not_called()


if __name__ == '__main__':
    unittest.main()

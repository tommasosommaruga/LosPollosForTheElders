import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from PIL import Image
import voice_pick as vp


class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.items = json.loads((vp.HERE / 'voice_items.json').read_text())

    def test_alias_and_polite_command(self):
        result = vp.match_item('Could you pick up the white ring, please?', self.items)
        self.assertEqual(result['target'], 'foam ring')

    def test_rejections(self):
        for text, expected in [('pick the red can', 'UNKNOWN'),
                               ('pick the can and the ring', 'AMBIGUOUS_SPEECH'),
                               ("don't pick the can", 'CANCELLED'), ('', 'NO_SPEECH')]:
            with self.subTest(text=text):
                self.assertEqual(vp.match_item(text, self.items)['status'], expected)

    def test_alias_collision(self):
        self.items['other ring'] = {'category': 'ring', 'aliases': ['ring']}
        self.assertEqual(vp.match_item('ring', self.items)['status'], 'AMBIGUOUS_SPEECH')

    def test_selection_and_failure_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            photo = Path(tmp) / 'input.jpg'
            Image.new('RGB', (100, 100), 'white').save(photo)
            out = Path(tmp) / 'out'
            for status in ('GO', 'AMBIGUOUS', 'NOT_FOUND'):
                selector = Mock(return_value={'status': status, 'candidates': [], 'chosen': None})
                report = vp.select_from_text('ring', photo, self.items, out, 'none', selector)
                self.assertEqual(report['status'], 'SELECTED' if status == 'GO' else status)
                self.assertFalse(report['motion_allowed'])
                selector.assert_called_once()
            selector = Mock(side_effect=RuntimeError('test failure'))
            report = vp.select_from_text('ring', photo, self.items, out, 'none', selector)
            self.assertEqual(report['status'], 'CHECK_FAILED')
            selector.reset_mock()
            report = vp.select_from_text('red can', photo, self.items, out, 'none', selector, vocabulary='catalog')
            selector.assert_not_called()
            self.assertEqual(json.loads((out / 'result.json').read_text())['status'], 'UNKNOWN')
            self.assertTrue((out / 'selection.jpg').is_file())

    def test_open_description_preserves_attributes(self):
        for phrase in ['headphone case', 'red can', 'black and white box', 'cup beside the laptop']:
            self.assertEqual(vp.describe_item('Please pick the ' + phrase)['target'], phrase)
        self.assertEqual(vp.describe_item('pick it')['status'], 'NEEDS_DESCRIPTION')
        self.assertEqual(vp.describe_item('cancel')['status'], 'CANCELLED')

    def test_open_target_reaches_selector_without_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            photo = Path(tmp) / 'input.jpg'
            Image.new('RGB', (100, 100), 'white').save(photo)
            selector = Mock(return_value={'status': 'NOT_FOUND', 'candidates': [], 'chosen': None})
            report = vp.select_from_text('pick the headphone case', photo, {}, tmp, 'none', selector)
            self.assertEqual(selector.call_args.args[1:], ('headphone case', None))
            self.assertEqual(report['status'], 'NOT_FOUND')
            self.assertFalse(report['motion_allowed'])


if __name__ == '__main__':
    unittest.main()

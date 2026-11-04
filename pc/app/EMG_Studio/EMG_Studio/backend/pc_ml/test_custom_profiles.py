"""Variable vocabulary coverage, integer round-trip and model/record identity."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
import torch

from .data import prepare, DatasetPreparationError
from .field import run
from .models import make_model
from .quantize import calibrate, export, load_integer
from .replay import replay, StreamingPredictor


class CustomProfileTests(unittest.TestCase):
    def setUp(self):
        torch.set_num_threads(4)

    def profile(self, n):
        return {'version': 1, 'name': 'Synthetic test only', 'classes': [
            {'id': i, 'key': 'rest' if i == 0 else f'test_{i}',
             'name': 'Rest' if i == 0 else f'Action {i}', 'instruction': f'Test definition {i}',
             'image': 'custom.png', 'output': {'id': i, 'name': f'Hand {i}'}}
            for i in range(n)]}

    def recording(self, path, profile):
        n = len(profile['classes'])
        labels = np.concatenate([np.full(600, i) for _ in range(3) for i in range(n)])
        rng = np.random.default_rng(31)
        raw = 1580+rng.normal(size=(len(labels), 8))*(5+labels[:, None]*5)
        np.savez(path, emg=raw.astype(np.float32), labels=labels,
                 timestamps=np.arange(len(labels))/1000, sample_rate=1000.,
                 gestures={c['id']: (c['name'], c['instruction']) for c in profile['classes']},
                 gesture_profile=json.dumps(profile))

    def test_three_class_train_export_stream_and_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = self.profile(3)
            recording = root/'three.npz'
            self.recording(recording, profile)
            report = run(recording, root/'run', epochs=1)
            model, _, manifest = load_integer(root/'run/export')
            self.assertEqual(manifest['class_names'], ['Rest', 'Action 1', 'Action 2'])
            self.assertEqual(manifest['gesture_profile'], profile)
            self.assertEqual(manifest['action_mapping'], [c['output'] for c in profile['classes']])
            self.assertEqual(len(report['evaluation']['test']['integer']['recall']), 3)
            self.assertTrue(report['int64_reference_verified'])
            predictions = StreamingPredictor(root/'run/export').push(np.full((1024, 8), 1580.))
            self.assertTrue(all(0 <= p['stable'] < 3 for p in predictions))
            result = replay(recording, root/'run/export', root/'replay')
            self.assertEqual(len(result['raw_decision']['confusion']), 3)
            # Same IDs and names, but different movement instructions, must not
            # silently be treated as the same labeled test recording.
            changed = copy.deepcopy(profile)
            changed['classes'][1]['instruction'] = 'Different physical movement'
            self.recording(recording, changed)
            with self.assertRaisesRegex(ValueError, '动作含义'):
                replay(recording, root/'run/export', root/'mismatch')

    def test_five_and_sixteen_class_integer_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            x = np.random.default_rng(12).normal(size=(4, 8, 128)).astype(np.float32)
            for n in [5, 16]:
                integer = calibrate(make_model('sequential', n).eval(), 'sequential', x)
                names = [f'Class {i}' for i in range(n)]
                path = Path(directory)/str(n)
                export(integer, path, np.zeros(8, int), 'test', names)
                loaded, _, manifest = load_integer(path)
                self.assertEqual(manifest['num_classes'], n)
                np.testing.assert_array_equal(loaded.predict_logits(x), integer.predict_logits(x))
                self.assertEqual(loaded.predict_logits(x).shape, (4, n))

    def test_five_class_split_and_invalid_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = self.profile(5)
            path = root/'five.npz'
            self.recording(path, profile)
            data, manifest = prepare(root, root/'data', field_recording=path)
            self.assertEqual(len(manifest['class_names']), 5)
            for split in range(3):
                self.assertEqual(set(data['y'][data['split'] == split]), set(range(5)))
            with np.load(path, allow_pickle=True) as z:
                arrays = {key: z[key] for key in z.files}
            arrays['labels'][0] = 99
            np.savez(path, **arrays)
            with self.assertRaisesRegex(DatasetPreparationError, '标签'):
                prepare(root, root/'bad', field_recording=path)


if __name__ == '__main__':
    unittest.main()

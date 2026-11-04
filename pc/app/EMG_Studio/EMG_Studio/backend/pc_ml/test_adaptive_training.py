"""Small acquisitions train without inventing independent test evidence."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from .cnn_fusion import RECIPE, load
from .cnn_fusion_field import adaptive_splits, prepare, run


def recording(path, repetitions=(1, 1, 1), length=1152, gap=False):
    rng = np.random.default_rng(91)
    raw = []; labels = []; timestamps = []; start = 0.
    for rep in range(max(repetitions)):
        for c, count in enumerate(repetitions):
            if rep >= count:
                continue
            signal = rng.normal(1650, 25+20*c, (length, 8))
            signal += 30*np.sin(np.arange(length)[:, None]*(.15+.07*c)+np.arange(8))
            raw.append(signal); labels.extend([c]*length)
            ts = start+np.arange(length)/1000
            if gap:
                ts[length//2:] += .5
            timestamps.extend(ts); start = ts[-1]+.001
            raw.append(np.full((64, 8), 1650.)); labels.extend([-1]*64)
            timestamps.extend(start+np.arange(64)/1000); start += .064
    np.savez(path, emg=np.concatenate(raw), labels=labels, timestamps=timestamps,
             gestures={c: [f'action{c}', 'hold'] for c in range(len(repetitions))}, capture_complete=True)


class AdaptiveTrainingTests(unittest.TestCase):
    def test_one_two_three_and_unequal_repetitions(self):
        for reps, mode in [((1, 1, 1), 'training_only'), ((2, 2, 2), 'two_part'),
                           ((3, 3, 3), 'three_part'), ((1, 2, 3), 'training_only'),
                           ((2, 3, 4), 'two_part')]:
            with self.subTest(reps=reps), tempfile.TemporaryDirectory() as root:
                path = Path(root)/'recording.npz'; recording(path, reps)
                d, meta = prepare(path)
                self.assertEqual(meta['split_mode'], mode)
                self.assertEqual(meta['repetitions'], list(reps))
                self.assertTrue(meta['training_warnings'])  # <32 windows is now a warning.
                train = set(d['group'][(d['y'] >= 0) & (d['split'] != 2)])
                test = set(d['group'][d['split'] == 2])
                self.assertFalse(train & test)
                self.assertEqual(bool(test), mode != 'training_only')
                for c in range(3):
                    self.assertGreaterEqual(np.sum((d['y'] == c) & (d['split'] != 2)), 2)
                    if test:
                        self.assertTrue(np.any((d['y'] == c) & (d['split'] == 2)))

    def test_stream_gap_does_not_become_independent_repetition(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'recording.npz'; recording(path, length=1600, gap=True)
            d, meta = prepare(path)
            self.assertEqual(meta['repetitions'], [1, 1, 1])
            self.assertFalse(meta['independent_evaluation'])
            # No feature or CNN window bridges an actual stream break.
            for edge in meta['capture_edges'][1:-1]:
                self.assertFalse(np.any((d['start'] < edge) & (d['ends'] > edge)))

    def test_missing_class_and_too_little_covariance_data_are_named(self):
        with self.assertRaisesRegex(ValueError, 'missing'):
            adaptive_splits(np.array([0, 0]), np.array([0, 0]), ['rest', 'missing'])
        with self.assertRaisesRegex(ValueError, 'action'):
            adaptive_splits(np.array([0, 0, 1]), np.array([0, 0, 1]), ['rest', 'action'])
        # Too-small first bouts must fall back to all-data fitting, not fail in LDA.
        y = np.array([0, 0, 0, 1, 1, 1]); g = np.array([0, 1, 1, 2, 3, 3])
        split, meta = adaptive_splits(y, g, ['rest', 'action'])
        self.assertFalse(meta['independent_evaluation'])
        np.testing.assert_array_equal(split, 0)

    def test_too_short_and_incomplete_recordings_still_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'recording.npz'; recording(path, length=500)
            with self.assertRaisesRegex(ValueError, '有效数据不足'):
                prepare(path)
            recording(path)
            with np.load(path, allow_pickle=True) as z:
                data = {key: z[key] for key in z.files}
            data['capture_complete'] = False; np.savez(path, **data)
            with self.assertRaisesRegex(ValueError, '中止记录'):
                prepare(path)

    def test_training_and_export_reports_for_one_and_two_repetitions(self):
        for n in (1, 2):
            with self.subTest(repeats=n), tempfile.TemporaryDirectory() as root:
                root = Path(root); path = root/'recording.npz'
                recording(path, (n, n, n))
                with patch.dict(RECIPE, epochs=1):
                    report = run(path, root/'out')
                    head, model, _, manifest = load(root/'out/export')
                self.assertEqual(head.weights_.shape, (3, 304))
                self.assertEqual(tuple(model.fc.weight.shape), (3, 32))
                self.assertTrue(manifest['metadata']['trained_all_bouts'])
                saved = json.loads((root/'out/field_report.json').read_text(encoding='utf8'))
                self.assertEqual(saved['evaluation']['independent'], n == 2)
                self.assertTrue(report['training_warnings'])
                self.assertIn('final', report['a_deployment'], report['a_deployment'])
                self.assertEqual(report['a_deployment']['final']['scope'], 'all_bout_model_self_check')
                if n == 1:
                    self.assertIsNone(saved['evaluation']['test'])
                    self.assertIsNone(saved['acceptance']['passed_offline'])
                    self.assertFalse((root/'out/validation_export').exists())
                else:
                    self.assertIsNotNone(saved['evaluation']['test'])
                    self.assertEqual(report['a_deployment']['heldout']['scope'], 'heldout')


if __name__ == '__main__':
    unittest.main()

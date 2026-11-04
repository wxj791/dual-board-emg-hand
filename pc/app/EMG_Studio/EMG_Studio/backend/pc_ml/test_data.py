"""Regression tests for explicit partial-file selection and empty windows."""
import tempfile
import unittest
from pathlib import Path

import numpy as np

from .data import prepare, DatasetPreparationError, GESTURES, GUARD, WINDOW


class DataTests(unittest.TestCase):
    def recording(self, directory, lengths, name='selected_aborted.npz'):
        labels = np.concatenate([np.full(length, label, dtype=np.int64) for label, length in lengths]) if lengths else np.empty(0, np.int64)
        x = np.random.default_rng(3).normal(1580, 10, (len(labels), 8)).astype(np.float32)
        path = Path(directory)/name
        np.savez(path, emg=x, labels=labels, timestamps=np.arange(len(labels))/1000,
                 sample_rate=1000., gestures={i: (name, '') for i, name in enumerate(GESTURES)})
        return path

    def test_explicit_aborted_filename_with_full_content_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.recording(directory, [(label, GUARD+WINDOW) for _ in range(3) for label in range(7)])
            data, manifest = prepare(path.parent, path.parent/'out', field_recording=path)
            self.assertEqual(len(data['y']), 21)
            for split in range(3):
                np.testing.assert_array_equal(np.bincount(data['y'][data['split'] == split], minlength=7), np.ones(7))
            self.assertIn(str(path), manifest['sources_sha256'])

    def test_short_segments_produce_actionable_error_not_stack_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.recording(directory, [(label, 100) for _ in range(3) for label in range(7)])
            with self.assertRaisesRegex(DatasetPreparationError, '448'):
                prepare(path.parent, path.parent/'out', field_recording=path)

    def test_missing_repetitions_report_missing_partition(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.recording(directory, [(label, GUARD+WINDOW) for label in range(7)])
            with self.assertRaisesRegex(DatasetPreparationError, '验证集'):
                prepare(path.parent, path.parent/'out', field_recording=path)

    def test_empty_recording_reported_before_reduction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.recording(directory, [])
            with self.assertRaisesRegex(DatasetPreparationError, '记录为空'):
                prepare(path.parent, path.parent/'out', field_recording=path)


if __name__ == '__main__':
    unittest.main()

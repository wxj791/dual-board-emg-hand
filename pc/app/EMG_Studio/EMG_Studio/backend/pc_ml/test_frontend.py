"""Regressions for the Q8 frontend contract, dynamic range and streaming."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
import torch

from .data import round_shift, prepare
from .frontend import PrecisionFilter, coefficients, create_filter, frontend_descriptor
from .models import make_model
from .parameter_store import ParameterStore
from .quantize import calibrate, export
from .replay import StreamingPredictor


class FrontendTests(unittest.TestCase):
    def test_constant_signal_residual_and_chunk_invariance(self):
        for kind in ('precision', 'emg'):
            config = frontend_descriptor(kind)
            f = create_filter(config)
            dc = f.process(np.full((2048, 8), 1588.))
            self.assertLess(abs(dc[-1].astype(float)/256).max(), .2)
            raw = np.random.default_rng(12).integers(0, 3301, (511, 8))
            f = create_filter(config)
            chunks = np.concatenate([f.process(x) for x in np.array_split(raw, 23)])
            np.testing.assert_array_equal(chunks, create_filter(config).process(raw))

    def test_inconsistent_frontend_rejected(self):
        config = frontend_descriptor('precision')
        config['state_fraction'] = 0
        with self.assertRaisesRegex(ValueError, 'contract'):
            create_filter(config)
        with self.assertRaises(ValueError):
            coefficients('unknown')

    def test_stream_uses_q8_without_int16_truncation(self):
        torch.set_num_threads(4)
        rng = np.random.default_rng(4)
        raw = rng.integers(0, 3301, (1024, 8))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            x = rng.normal(size=(8, 8, 128)).astype(np.float32)
            integer = calibrate(make_model('sequential').eval(), 'sequential', x)
            shifts = np.full(8, 12)
            descriptor = export(integer, path, shifts, 'test')
            descriptor.update(frontend=frontend_descriptor('precision'), format='EMG-PARAM-2')
            (path/'manifest.json').write_text(json.dumps(descriptor), encoding='utf-8')
            one = StreamingPredictor(path).push(raw)
            predictor = StreamingPredictor(path)
            many = [p for block in np.array_split(raw, 37) for p in predictor.push(block)]
            self.assertEqual(one, many)
            filtered = create_filter(descriptor['frontend']).process(raw)
            self.assertGreater(abs(filtered).max(), 32767)
            q = np.stack([np.clip(round_shift(filtered[320:448, c], 12), -127, 127)
                          for c in range(8)])
            expected = integer.trace(q[None])['logits'][0]
            self.assertEqual(one[0]['prediction'], int(expected.argmax()))
            changed = copy.deepcopy(descriptor)
            changed['frontend'] = frontend_descriptor('emg')
            payload = (path/'parameters.bin').read_bytes()
            store = ParameterStore(descriptor, payload)
            with self.assertRaisesRegex(ValueError, 'topology'):
                store.begin(changed, 2)

    def test_data_cache_distinguishes_frontend_and_keeps_24bit_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            labels = np.repeat(np.tile([0, 1], 3), 448)
            raw = np.random.default_rng(33).integers(0, 3301, (len(labels), 8))
            profile = {'classes': [{'id': 0, 'key': 'rest', 'name': 'Rest'},
                                   {'id': 1, 'key': 'action', 'name': 'Action'}]}
            recording = path/'raw.npz'
            np.savez(recording, emg=raw, labels=labels, timestamps=np.arange(len(labels))/1000,
                     sample_rate=1000, gestures={0: ('Rest',''), 1: ('Action','')},
                     gesture_profile=json.dumps(profile))
            old, _ = prepare(path, path/'data', recording)
            new, manifest = prepare(path, path/'data', recording, frontend='precision')
            self.assertEqual(new['filtered'].dtype, np.int32)
            self.assertGreater(abs(new['filtered']).max(), 32767)
            self.assertNotEqual(old['filtered'].dtype, new['filtered'].dtype)
            cached, same = prepare(path, path/'data', recording, frontend='precision')
            np.testing.assert_array_equal(cached['x'], new['x'])
            self.assertEqual(same['frontend'], manifest['frontend'])


if __name__ == '__main__':
    unittest.main()

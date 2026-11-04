"""Independent arithmetic and stream-contract checks for the hybrid model."""
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
import torch

from .data import round_shift
from .hybrid import HybridNet,IntegerHybrid,calibrate_hybrid,execute_statistics
from .quantize import direct_reference
from .train import seed_all


class HybridTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        torch.set_num_threads(2)
        seed_all(22)
        rng=np.random.default_rng(23)
        cls.x=rng.integers(0,12000,(140,64),dtype=np.int16)
        cls.y=np.repeat(np.arange(7),20)
        cls.wave=rng.integers(-100,101,(2,8,128)).astype(np.float32)/32
        cls.model=HybridNet()
        cls.offset=cls.model.initialize_statistics(cls.x,cls.y)
        cls.model.eval()
        # Nonzero convolution head: exercise the learned path, not only epoch zero.
        with torch.no_grad():cls.model.learned.weight.uniform_(-.4,.4)
        cls.integer=calibrate_hybrid(cls.model,cls.wave,cls.offset,np.zeros(8,dtype=np.int16))

    def test_each_integer_layer_and_combined_head(self):
        q=np.rint(self.wave*32).astype(np.int64)
        trace=self.integer.trace(q,self.x[:2])
        for name,source in [('stem','input'),('conv2','pool1'),('conv3','pool2'),('learned','embedding')]:
            np.testing.assert_array_equal(trace[name],direct_reference(self.integer.layers[name],trace[source]))
        layer=self.integer.layers['statistics']
        expected=[]
        for row in trace['centered']:
            expected.append([int(round_shift(sum(int(a)*int(b) for a,b in zip(row,w))+int(bias),int(s)))
                             for w,bias,s in zip(layer['weights'],layer['bias'],layer['shifts'])])
        np.testing.assert_array_equal(trace['statistics'],expected)
        np.testing.assert_array_equal(trace['logits'],np.asarray(expected)+trace['learned'])

    def test_statistics_signed17_extremes(self):
        rng=np.random.default_rng(77)
        weights=rng.integers(-32767,32768,(7,64),dtype=np.int16)
        bias=rng.integers(-2**31,2**31,7,dtype=np.int64)
        x=np.tile(np.array([-65535,65535],dtype=np.int64),32)[None]
        layer={'weights':weights,'bias':bias,'shifts':np.arange(7)}
        expected=[[int(round_shift(sum(int(a)*int(b) for a,b in zip(x[0],w))+int(b),i))
                   for i,(w,b) in enumerate(zip(weights,bias))]]
        np.testing.assert_array_equal(execute_statistics(layer,x),expected)

    def test_zero_correction_skips_convolution_without_changing_logits(self):
        import copy
        layers=copy.deepcopy(self.integer.layers)
        layers['learned']['weights'][:]=0
        zero=IntegerHybrid(layers,self.integer.offset,self.integer.wave_shifts)
        self.assertFalse(zero.cnn_active)
        q=np.rint(self.wave*32).astype(np.int64)
        expected=zero.trace(q,self.x[:2])['logits']
        # A zero correction must not even need the waveform to make a score.
        np.testing.assert_array_equal(zero.predict_logits(None,self.x[:2]),expected)

    def test_roundtrip_and_hash_rejection(self):
        with tempfile.TemporaryDirectory() as d:
            desc=self.integer.package(d,{'class_names':[str(i) for i in range(7)]})
            self.assertLess(desc['parameter_bytes'],8192)
            loaded,_=IntegerHybrid.load(d)
            q=np.rint(self.wave*32).astype(np.int64)
            np.testing.assert_array_equal(self.integer.trace(q,self.x[:2])['logits'],loaded.trace(q,self.x[:2])['logits'])
            payload=Path(d)/'parameters.bin'
            blob=bytearray(payload.read_bytes());blob[-1]^=1;payload.write_bytes(blob)
            with self.assertRaises(ValueError):IntegerHybrid.load(d)

    def test_real_hybrid_stream_matches_cached_windows(self):
        from .today_live import StreamingFeaturePredictor
        from .frontend import PrecisionFilter
        root=Path(__file__).resolve().parents[1]
        base=root/'artifacts/today_survey/DDW_P2_2610281200'
        package=root/'artifacts/today_hybrid/DDW_P2_2610281200/fold0/export'
        if not package.exists():self.skipTest('Real survey artifact is not available')
        meta=json.loads((base/'data.json').read_text(encoding='utf-8'))
        seg=meta['segments'][0]
        with np.load(meta['path'],allow_pickle=True) as z:raw=z['emg'][seg['start']:seg['stop']]
        p=StreamingFeaturePredictor(package)
        output=p.push(raw)
        p.reset()
        chunks=[r for block in np.array_split(raw,39) for r in p.push(block)]
        self.assertEqual(output,chunks)
        key=f"{p.meta['front']}_{p.width}"
        with np.load(base/'features.npz') as z:
            mask=z[f'{key}_group']==seg['id']
            stats=z[f'{key}_x'][mask];starts=z[f'{key}_start'][mask]-seg['start']
        filtered=PrecisionFilter(p.meta['coefficients_q14']).process(raw)
        wave=np.stack([filtered[s:s+p.width].T for s in starts])
        expected=p.model.predict_logits(wave,stats).argmax(1)
        np.testing.assert_array_equal([r['prediction'] for r in chunks],expected)


if __name__=='__main__':unittest.main()

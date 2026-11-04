"""Independent numeric contracts and date/split invariants for the survey."""
import tempfile
import unittest
from pathlib import Path

import numpy as np

from .fixed_rbf import FixedRbfSVC
from .fixed_linear import FixedLinear
from .frontend import PrecisionFilter
from .today_survey import qlog2,filter_q8,discover,fixed_coefficients,Q8Filter,features


class TodayTests(unittest.TestCase):
    def test_qlog_matches_leading_one_integer_reference(self):
        values=np.r_[np.arange(0,20000),[2**n-1 for n in range(1,48)],
                     [2**n for n in range(48)], [2**n+1 for n in range(1,48)]]
        expected=[]
        for value in values:
            value=max(1,int(value));e=value.bit_length()-1
            expected.append(e*256+((value-(1<<e))<<8)//(1<<e))
        np.testing.assert_array_equal(qlog2(values),expected)

    def test_filter_q8_matches_raw_adapter_engine(self):
        raw=np.random.default_rng(2).integers(0,3301,(1000,8))
        q8=(raw-1650)*256
        for front in ('precision','emg','clean'):
            coeff=fixed_coefficients(front)
            np.testing.assert_array_equal(filter_q8(q8,coeff),PrecisionFilter(coeff).process(raw))
            bank=Q8Filter(coeff)
            chunks=np.concatenate([bank.process(part) for part in np.array_split(q8,23)])
            np.testing.assert_array_equal(chunks,filter_q8(q8,coeff))

    def test_date_and_aborted_filters(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            # Excluded files need not be loadable; prove they never enter NPZ.
            for name in ('DGT_P1_2610272000.npz','emg_aborted_20261028_200000.npz','DGT_P1_2610282000.npz'):
                (root/name).write_bytes(b'0'*16)
            rows=discover(root)
            self.assertTrue(all('excluded' in row for row in rows))

    def test_signed_log_correlation_deterministic_and_bounded(self):
        from .today_covariance import covariance_features
        t=np.tile(np.array([-256,256],dtype=np.int32),128)
        x=np.tile(t,(1,8,1))
        positive=covariance_features(x)
        np.testing.assert_array_equal(positive,np.full((1,28),2048))
        x[:,1]*=-1
        signed=covariance_features(x)
        self.assertEqual(signed[0,0],-2048)
        self.assertTrue(np.all(abs(signed)<=2048))

    def test_fixed_linear_roundtrip_and_separate_int64_scores(self):
        rng=np.random.default_rng(3)
        x=rng.integers(0,10000,(210,64),dtype=np.int16)
        y=np.repeat(np.arange(7),30)
        model=FixedLinear().fit(x,y)
        expected=np.empty((len(x),7),dtype=np.int64)
        for n,row in enumerate(x):
            for c in range(7):
                expected[n,c]=sum((int(v)-int(o))*int(w) for v,o,w in zip(row,model.offset_,model.weights_[c]))+int(model.bias_[c])
        np.testing.assert_array_equal(model.scores(x),expected)
        with tempfile.TemporaryDirectory() as d:
            model.package(d,{})
            loaded,_=FixedLinear.load(d)
            np.testing.assert_array_equal(model.predict(x),loaded.predict(x))

    def test_fixed_rbf_kernel_votes_and_roundtrip(self):
        rng=np.random.default_rng(4)
        x=rng.integers(0,10000,(112,64),dtype=np.int16)
        y=np.repeat(np.arange(7),16)
        model=FixedRbfSVC(representatives=6).fit(x,y)
        q=model.normalize(x[:5])
        reference=[]
        for row in q:
            current=[]
            for support in model.support_:
                distance=sum((int(a)-int(b))**2 for a,b in zip(row,support))
                index=min(4096,(distance+32)//64)
                current.append(int(model.lut_[index]))
            reference.append(current)
        np.testing.assert_array_equal(model.kernel(q),reference)
        votes,scores=model.decision_votes(x[:5])
        for n in range(5):
            pair=0
            starts=np.r_[0,np.cumsum(model.n_support_)]
            for i in range(7):
                for j in range(i+1,7):
                    score=int(model.bias_[pair])
                    for k in range(starts[i],starts[i+1]): score+=int(reference[n][k])*int(model.dual_[j-1,k])
                    for k in range(starts[j],starts[j+1]): score+=int(reference[n][k])*int(model.dual_[i,k])
                    self.assertEqual(score,scores[n,pair]);pair+=1
        with tempfile.TemporaryDirectory() as d:
            m=model.package(d)
            self.assertLess(m['parameter_bytes'],30000)
            loaded,_=FixedRbfSVC.load(d)
            np.testing.assert_array_equal(model.predict(x),loaded.predict(x))

    def test_real_package_stream_chunks_and_training_features_match(self):
        from .today_live import StreamingFeaturePredictor
        root=Path(__file__).resolve().parents[1]
        source=root/'artifacts/today_survey/DDW_P2_2610281200'
        package=root/'artifacts/today_linear/DDW_P2_2610281200/fold2/export'
        if not package.is_dir():
            self.skipTest('Real survey artifact is not present')
        import json
        manifest=json.loads((source/'data.json').read_text(encoding='utf-8'))
        streaming=StreamingFeaturePredictor(package)
        segment=manifest['segments'][0]
        with np.load(manifest['path'],allow_pickle=True) as z:
            raw=z['emg'][segment['start']:segment['stop']]
        one=streaming.push(raw)
        streaming.reset()
        many=[row for part in np.array_split(raw,37) for row in streaming.push(part)]
        self.assertEqual(one,many)
        key=f"{streaming.meta['front']}_{streaming.width}"
        with np.load(source/'features.npz') as z:
            mask=z[f'{key}_group']==segment['id']
            expected=streaming.model.predict(z[f'{key}_x'][mask])
        np.testing.assert_array_equal([r['prediction'] for r in one],expected)

    def test_rbf_and_correlation_stream_match_cached_features(self):
        from .today_live import StreamingFeaturePredictor
        import json
        root=Path(__file__).resolve().parents[1]
        cases=[('today_compact','DDW_P2_2610281200',0,'today_survey'),
               ('today_covariance','DGT_P2_2610282008',1,'today_covariance')]
        for directory,stem,fold,cache in cases:
            package=root/f'artifacts/{directory}/{stem}/fold{fold}/export'
            if not package.is_dir():
                self.skipTest('Real survey artifacts are not present')
            with self.subTest(algorithm=directory):
                meta=json.loads((root/f'artifacts/today_survey/{stem}/data.json').read_text(encoding='utf-8'))
                seg=meta['segments'][0]
                with np.load(meta['path'],allow_pickle=True) as z:
                    raw=z['emg'][seg['start']:seg['stop']]
                streaming=StreamingFeaturePredictor(package)
                output=[r for block in np.array_split(raw,31) for r in streaming.push(block)]
                key=f"{streaming.meta['front']}_{streaming.width}"
                with np.load(root/f'artifacts/{cache}/{stem}/features.npz') as z:
                    x=z[f'{key}_x'][z[f'{key}_group']==seg['id']]
                np.testing.assert_array_equal([r['prediction'] for r in output],streaming.model.predict(x))


if __name__=='__main__':unittest.main()

"""Causal, label-free two-timescale integer EMG envelopes for feature fusion.

Q8 filtered input -> abs nearest-rounded Q4 -> EMA shifts4/6. Both states run
continuously; neither windows nor labels reset them. Four 64-sample mean bins
per 256-sample window retain envelope trajectory. Leading-one log codes and
their across-channel centered values give 128 signed16 envelope features.
"""
import numpy as np
from numba import njit
from .today_survey import qlog2


@njit(cache=True)
def envelopes(signal):
    out=np.empty((len(signal),16),dtype=np.int32)
    state=np.zeros((2,8),dtype=np.int64)
    for n in range(len(signal)):
        for t in range(2):
            shift=4 if t==0 else 6
            for c in range(8):
                v=int(signal[n,c])
                target=(abs(v)+8)>>4
                delta=target-state[t,c]
                step=(abs(delta)+(1<<(shift-1)))>>shift
                state[t,c]+=step if delta>=0 else -step
                out[n,t*8+c]=state[t,c]
    return out


def envelope_features(env,starts):
    env=np.asarray(env,dtype=np.int64)
    cumulative=np.concatenate([np.zeros((1,16),dtype=np.int64),env.cumsum(0)],0)
    encoded=[]
    for t in range(2):
        for b in range(4):
            a=starts+b*64
            total=cumulative[a+64,t*8:t*8+8]-cumulative[a,t*8:t*8+8]
            encoded.append(qlog2((total+32)>>6))
    absolute=np.concatenate(encoded,1)
    blocks=absolute.astype(np.int32).reshape(-1,8,8)
    relative=blocks-(blocks.sum(2,keepdims=True)>>3)
    return np.concatenate([absolute,relative.reshape(-1,64)],1).astype(np.int16)


def independent_check():
    rng=np.random.default_rng(731)
    signal=rng.integers(-8388608,8388608,(1100,8),dtype=np.int32)
    actual=envelopes(signal)
    state=[[0]*8 for _ in range(2)]
    expected=[]
    for sample in signal:
        row=[]
        for t,shift in enumerate([4,6]):
            for c in range(8):
                target=(abs(int(sample[c]))+8)//16
                delta=target-state[t][c]
                step=(abs(delta)+(1<<(shift-1)))//(1<<shift)
                state[t][c]+=step if delta>=0 else -step
                row.append(state[t][c])
        expected.append(row)
    np.testing.assert_array_equal(actual,expected)
    np.testing.assert_array_equal(envelopes(signal[:700]),actual[:700])
    starts=np.asarray([0,32,320,768])
    got=envelope_features(actual,starts)
    rows=[]
    def logcode(value):
        v=max(1,int(value));e=v.bit_length()-1
        return e*256+((v-(1<<e))*256//(1<<e))
    for start in starts:
        row=[]
        for t in range(2):
            for b in range(4):
                for c in range(8):
                    total=sum(int(v) for v in actual[start+b*64:start+(b+1)*64,t*8+c])
                    row.append(logcode((total+32)//64))
        rel=[]
        for i in range(0,64,8):
            mean=sum(row[i:i+8])//8
            rel.extend(v-mean for v in row[i:i+8])
        rows.append(row+rel)
    np.testing.assert_array_equal(got,rows)
    assert np.all(actual>=0) and np.max(actual)<=524288
    return {'scalar_ema_exact':True,'prefix_causality_exact':True,
            'scalar_bin_mean_log_relative_features_exact':True,'test_samples':len(signal)}

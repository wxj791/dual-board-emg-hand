"""Integer, causal input representations for temporal EMG CNN experiments."""
import numpy as np

from .data import round_shift


KINDS = ('raw128', 'raw256_stride2', 'band_abs8', 'band_log_abs8')


def input_spec(kind):
    if kind not in KINDS:
        raise ValueError(kind)
    return {'kind': kind, 'window': 128 if kind == 'raw128' else 256,
            'channels': 32 if kind.startswith('band_') else 8,
            'width': 32 if kind.startswith('band_') else 128,
            'stem_kernel': 3 if kind.startswith('band_') else 5}


def log_q8(values):
    """Same leading-bit/mantissa integer log used by the FPGA features."""
    v = np.maximum(np.asarray(values, dtype=np.int64), 1)
    exponent = np.zeros(v.shape, dtype=np.int64)
    for bit in range(48):
        exponent = np.where(v >= 1 << bit, bit, exponent)
    mantissa = np.where(exponent >= 8, v >> np.maximum(exponent-8, 0),
                        v << np.maximum(8-exponent, 0))
    return (exponent*256 + (mantissa & 255)).astype(np.int32)


def windows(q4, ends, kind):
    spec = input_spec(kind)
    if kind == 'raw128':
        return np.stack([q4[e-128:e, :8].T for e in ends]).astype(np.int32)
    if kind == 'raw256_stride2':
        return np.stack([q4[e-256:e:2, :8].T for e in ends]).astype(np.int32)
    result = np.stack([((abs(q4[e-256:e].astype(np.int64)).T.reshape(32,32,8).sum(-1)+4)>>3)
                       for e in ends]).astype(np.int32)
    return log_q8(result) if kind == 'band_log_abs8' else result


def normalize(values, fit, offsets=None, shifts=None):
    if offsets is None:
        offsets = np.rint(np.median(values[fit], axis=(0,2))).astype(np.int32)
    centered = values.astype(np.int64) - offsets[None,:,None]
    if shifts is None:
        maximum = abs(centered[fit]).max((0,2))
        shifts = np.maximum(0, np.ceil(np.log2(np.maximum(maximum,1)/120))).astype(np.int8)
    q = np.stack([np.clip(round_shift(centered[:,c],int(s)),-127,127)
                  for c,s in enumerate(shifts)],1)
    return q.astype(np.float32)/32, offsets, shifts

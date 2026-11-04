"""Versioned, causal fixed-point frontends shared by training and inference."""
import numpy as np

from .data import COEFFICIENTS, StreamFilter


class PrecisionFilter:
    """24-bit Q8 mV states, Q14 coefficients and signed 48-bit MAC contract.

    Fixed 1650mV subtraction removes the ADC midpoint before scaling. No
    per-window normalization or labels enter this causal frontend. Unlike the
    legacy integer-mV filter, rounding errors are below one ADC mV step.
    """
    def __init__(self, coefficients=COEFFICIENTS):
        self.coefficients = tuple(tuple(int(c) for c in row) for row in coefficients)
        self.state = np.zeros((8, len(self.coefficients), 4), dtype=np.int64)
        self.saturations = 0

    def process(self, raw):
        raw = np.asarray(raw, dtype=np.float64)
        if raw.ndim != 2 or raw.shape[1] != 8 or not np.isfinite(raw).all():
            raise ValueError('Expected finite [samples,8] mV input')
        # Match ADC integer-mV resolution; Q8 improves DSP states, not the ADC.
        values_in = np.clip(np.rint(raw), 0, 3300).astype(np.int64)
        values_in = (values_in-1650)*256
        output = np.empty_like(values_in, dtype=np.int32)
        for channel in range(8):
            values = values_in[:, channel].tolist()
            for section, (b0, b1, b2, a1, a2) in enumerate(self.coefficients):
                x1, x2, y1, y2 = self.state[channel, section].tolist()
                filtered = []
                for x in values:
                    acc = b0*x+b1*x1+b2*x2-a1*y1-a2*y2
                    y = (acc+8192) >> 14 if acc >= 0 else -((-acc+8192) >> 14)
                    clipped = min(2**23-1, max(-2**23, y))
                    self.saturations += clipped != y
                    filtered.append(clipped)
                    x2, x1, y2, y1 = x1, x, y1, clipped
                self.state[channel, section] = x1, x2, y1, y2
                values = filtered
            output[:, channel] = values
        return output


def coefficients(kind):
    if kind == 'precision':
        return COEFFICIENTS
    if kind == 'emg':
        return ((14991, -29982, 14991, -29863, 13716),
                (16282, -30970, 16282, -30970, 16179),
                (13117, 26234, 13117, 25576, 10508))
    raise ValueError(f'Unknown fixed-point frontend: {kind}')


def frontend_descriptor(kind):
    if kind == 'legacy':
        return None
    return {'name': kind, 'sample_rate': 1000, 'raw_offset_mv': 1650,
            'state_fraction': 8, 'state_bits': 24, 'coefficient_fraction': 14,
            'accumulator_bits': 48, 'coefficients_q14': [list(r) for r in coefficients(kind)],
            'raw_input': 'Integer mV, ties-to-even, clipped 0..3300'}


def create_filter(descriptor=None):
    if descriptor is None:
        return StreamFilter()
    expected = frontend_descriptor(descriptor['name'])
    if expected is None or any(descriptor.get(k) != v for k, v in expected.items()):
        raise ValueError('Unsupported or inconsistent fixed-point frontend contract')
    return PrecisionFilter(expected['coefficients_q14'])

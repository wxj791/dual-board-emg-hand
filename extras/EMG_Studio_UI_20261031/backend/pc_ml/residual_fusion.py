"""Integer temporal CNN residual fused with the accepted statistical frontend.

This is an explicit package/runtime, not a change to the existing GUI default.
"""
from pathlib import Path
import json

import numpy as np

from .data import round_shift
from .fixed_linear import FixedLinear
from .fusion import FusionFeatures, FusionDecision, advance
from .quantize import IntegerModel


def load_correction(path):
    path = Path(path)
    meta = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
    with np.load(path / 'integer.npz') as arrays:
        layers = {name: {**desc, **{field: arrays[name+'.'+field].copy()
                   for field in ['weights', 'bias', 'shifts']}} for name, desc in meta['layers'].items()}
        shifts = arrays['wave_shifts'].copy()
    return IntegerModel('sequential', layers, layers['conv3']['output_fraction']), shifts, meta


class ResidualPredictor:
    """Raw PC-order mV -> fused scores and original rest/EMA policy."""
    def __init__(self, package):
        package = Path(package)
        self.linear, self.manifest = FixedLinear.load(package / 'linear')
        self.cnn, self.shifts, self.cnn_manifest = load_correction(package / 'cnn')
        self.scale = self.linear.fraction_ - 8
        threshold = self.cnn_manifest['metadata']['selected']['gate_threshold']
        self.threshold_q = 2**32-1 if threshold is None else int(round(threshold * 2.**self.linear.fraction_))
        if not 0 <= self.scale <= 20:
            raise ValueError('Unsupported score alignment')
        self.reset()

    def reset(self):
        self.frontend = FusionFeatures()
        self.wave_filter = FusionFeatures()
        self.tail = np.empty((0, 8), dtype=np.int64)
        self.count = 0
        self.decision = FusionDecision(len(self.linear.classes_))

    def push(self, raw):
        raw = np.asarray(raw)
        # Let the existing feature frontend validate shape and finite values.
        features, ends = self.frontend.push(raw)
        if not len(raw):
            return []
        q8 = (np.rint(raw).clip(0, 3300).astype(np.int64) - 1650) * 256
        front = self.wave_filter
        signal, _ = advance(q8, front.coeff, front.state, front.env_state)
        q4 = np.sign(signal[:, :8]) * ((abs(signal[:, :8].astype(np.int64)) + 8) >> 4)
        previous = len(self.tail)
        values = np.concatenate([self.tail, q4])
        local_ends = previous + ends - self.count
        self.count += len(raw)
        self.tail = values[-128:].copy()
        if not len(ends):
            return []
        waves = np.stack([values[e-128:e].T for e in local_ends])
        q = np.stack([np.clip(round_shift(waves[:, c], int(s)), -127, 127)
                      for c, s in enumerate(self.shifts)], 1)
        base = self.linear.scores(features)
        corrections = self.cnn.trace(q)['logits'] << self.scale
        top = np.sort(base, axis=1)
        strong = top[:, -1] - top[:, -2] >= self.threshold_q
        corrections[strong] = round_shift(corrections[strong], 2)
        output = []
        for end, b, c in zip(ends, base, corrections):
            fused = b + c
            raw_class, stable, margin = self.decision.step(fused)
            output.append({'sample_end': int(end), 'prediction': raw_class, 'stable': stable,
                           'margin_q8': margin, 'statistics_scores': b, 'cnn_correction': c,
                           'fused_scores': fused, 'cnn_participates': True})
        return output

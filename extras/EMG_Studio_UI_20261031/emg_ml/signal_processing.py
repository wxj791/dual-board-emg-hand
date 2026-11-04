"""Stateful, causal filtering for blocks of eight-channel EMG samples."""

import numpy as np
from scipy import signal


class EMGEnvelope:
    """Display envelope: rectify filtered EMG and apply a causal 1/64 EMA.

    At 1 kHz the time constant is about 64 ms. This display path does not
    replace the selected model's own preprocessing or feature envelopes.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self._state = np.zeros((1, 8), dtype=np.float64)

    def process(self, filtered):
        filtered = np.asarray(filtered, dtype=np.float64)
        if filtered.ndim != 2 or filtered.shape[1] != 8 or not np.isfinite(filtered).all():
            raise ValueError("Envelope input must be finite filtered samples [N, 8]")
        if not len(filtered):
            return filtered.astype(np.float32)
        envelope, self._state = signal.lfilter(
            [1 / 64], [1, -63 / 64], np.abs(filtered), axis=0, zi=self._state
        )
        return envelope.astype(np.float32)


class EMGSignalFilter:
    """20–450 Hz bandpass followed by a 50 Hz notch, at 1000 Hz/channel.

    State persists across blocks; each column has independent filter state.
    Output is signed floating point in the same voltage scale as the input.
    """

    SAMPLE_RATE = 1000.0
    CHANNELS = 8

    def __init__(self):
        bandpass = signal.butter(
            4, (20.0, 450.0), btype="bandpass", fs=self.SAMPLE_RATE, output="sos"
        )
        numerator, denominator = signal.iirnotch(50.0, 30.0, fs=self.SAMPLE_RATE)
        self.sos = np.vstack((bandpass, signal.tf2sos(numerator, denominator)))
        self._initial_state = signal.sosfilt_zi(self.sos)
        self.reset()

    def reset(self):
        self._state = None

    def process(self, samples):
        samples = np.asarray(samples, dtype=np.float64)
        if samples.ndim != 2 or samples.shape[1] != self.CHANNELS:
            raise ValueError("EMG samples must have shape [N, 8]")
        if not np.isfinite(samples).all():
            raise ValueError("EMG samples must contain only finite values")
        if not len(samples):
            return samples.astype(np.float32)
        if self._state is None:
            # Start at the first DC level rather than creating a large bias step.
            self._state = self._initial_state[:, :, None] * samples[0][None, None, :]
        filtered, self._state = signal.sosfilt(
            self.sos, samples, axis=0, zi=self._state
        )
        return filtered.astype(np.float32)

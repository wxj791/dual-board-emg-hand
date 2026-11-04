"""DSP and serial-pipeline checks; no board or serial port is opened."""

import importlib.util
import struct
import sys
import unittest
from pathlib import Path

import numpy as np

EMG_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EMG_DIR))

from signal_processing import EMGSignalFilter, EMGEnvelope


class FilterTests(unittest.TestCase):
    def test_rejects_line_noise_and_preserves_muscle_band(self):
        t = np.arange(6000) / 1000.0
        frequencies = (5, 50, 120, 480)
        samples = 1600 + sum(100 * np.sin(2 * np.pi * f * t) for f in frequencies)
        output = EMGSignalFilter().process(np.repeat(samples[:, None], 8, axis=1))
        tail = output[2000:, 0]
        amplitudes = {
            f: abs(2 * np.mean(tail * np.exp(-2j * np.pi * f * t[2000:])))
            for f in frequencies
        }
        self.assertLess(amplitudes[50], 1)
        self.assertLess(amplitudes[5], 2)
        self.assertLess(amplitudes[480], 5)
        self.assertGreater(amplitudes[120], 95)
        self.assertLess(amplitudes[120], 105)

    def test_arbitrary_block_sizes_match_continuous_processing(self):
        samples = 1600 + np.random.default_rng(42).normal(0, 40, (4000, 8))
        reference = EMGSignalFilter().process(samples)
        processor = EMGSignalFilter()
        blocks = np.split(samples, [1, 17, 18, 345, 1000, 1001, 2871])
        actual = np.concatenate([processor.process(block) for block in blocks])
        np.testing.assert_allclose(actual, reference, rtol=0, atol=1e-5)

    def test_channel_states_are_independent(self):
        samples = np.full((2000, 8), 1600.0)
        samples[100, 3] += 200
        output = EMGSignalFilter().process(samples)
        self.assertGreater(np.max(np.abs(output[:, 3])), 10)
        np.testing.assert_allclose(np.delete(output, 3, axis=1), 0, atol=1e-6)

    def test_dc_start_and_reset_do_not_create_bias_steps(self):
        processor = EMGSignalFilter()
        np.testing.assert_allclose(processor.process(np.full((100, 8), 1600)), 0, atol=1e-6)
        processor.process(np.random.default_rng(7).normal(1600, 30, (200, 8)))
        processor.reset()
        np.testing.assert_allclose(processor.process(np.full((100, 8), 2200)), 0, atol=1e-6)

    def test_invalid_input_cannot_poison_state(self):
        processor = EMGSignalFilter()
        samples = np.ones((30, 8)) * 1600
        processor.process(samples)
        for value in (float("nan"), float("inf"), -float("inf")):
            invalid = samples.copy()
            invalid[2, 5] = value
            with self.assertRaises(ValueError):
                processor.process(invalid)
        np.testing.assert_allclose(processor.process(samples), 0, atol=1e-6)
        self.assertEqual(processor.process(np.empty((0, 8))).shape, (0, 8))


class EnvelopeTests(unittest.TestCase):
    def test_rectifies_smooths_and_retains_block_state(self):
        t = np.arange(2000) / 1000
        samples = np.zeros((2000, 8))
        samples[200:1200, 3] = 100 * np.sin(2 * np.pi * 120 * t[200:1200])
        expected = EMGEnvelope().process(samples)
        processor = EMGEnvelope()
        actual = np.concatenate([processor.process(v) for v in np.split(samples, [1, 203, 487, 1201])])
        np.testing.assert_array_equal(actual, expected)
        self.assertGreater(float(expected[1000, 3]), 60)
        self.assertLess(float(expected[1000, 3]), 70)
        self.assertLess(float(expected[-1, 3]), .001)
        self.assertTrue(np.all(expected >= 0))
        np.testing.assert_array_equal(np.delete(expected, 3, axis=1), 0)
        processor.reset()
        np.testing.assert_array_equal(processor.process(np.zeros((100, 8))), 0)


class FakeSerial:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.is_open = True
        self.on_exhausted = lambda: None

    @property
    def in_waiting(self):
        if self.chunks:
            return len(self.chunks[0])
        self.on_exhausted()
        return 0

    def read(self, count):
        return self.chunks.pop(0)

    def close(self):
        self.is_open = False


class SerialPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "emg_gui_v3_test", EMG_DIR / "emg_studio_gui_V3.0.py"
        )
        cls.gui = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.gui)

    def receive(self, core, payload):
        # Deliberately split both float payloads and frame tails across reads.
        chunks = [payload[i:i + 137] for i in range(0, len(payload), 137)]
        serial = FakeSerial(chunks)
        serial.on_exhausted = lambda: setattr(core, "running", False)
        core.ser = serial
        core.running = True
        core._recv_loop()

    def frame(self, values):
        return struct.pack("<8f", *values) + self.gui.TAIL_BYTES

    def test_diagnostics_and_invalid_frames_are_excluded_and_raw_is_preserved(self):
        core = self.gui.SerialCore()
        self.assertEqual(core.get_snapshot()[0].shape, (0, 8))
        core.start_recording()
        t = np.arange(800) / 1000
        samples = np.column_stack([
            1600 + (ch + 1) * 10 * np.sin(2 * np.pi * 50 * t) for ch in range(8)
        ]).astype(np.float32)
        frames = [self.frame(sample) for sample in samples]
        frames.insert(123, self.frame([-123456, 1000, 100, 100, 100, 0, 0, 0]))
        frames.insert(250, self.frame([float("nan"), 1600, 1600, 1600, 1600, 1600, 1600, 1600]))
        self.receive(core, b"".join(frames))
        raw, filtered, _, total, corrupt = core.get_snapshots()
        expected_raw = samples[:, self.gui.PHYSICAL_CH_MAP]
        expected_filtered = EMGSignalFilter().process(expected_raw)
        self.assertEqual(total, len(samples))
        self.assertEqual(corrupt, 1)
        self.assertEqual(core.get_link_diag()["adc_frames_s"], 1000)
        np.testing.assert_array_equal(raw, expected_raw[-500:])
        np.testing.assert_allclose(filtered, expected_filtered[-500:], atol=1e-5)
        raw3, filtered3, envelope, *_ = core.get_display_snapshots()
        np.testing.assert_array_equal(raw3, raw)
        np.testing.assert_array_equal(filtered3, filtered)
        np.testing.assert_allclose(envelope, EMGEnvelope().process(expected_filtered)[-500:], atol=1e-5)
        recorded = core.stop_recording()
        np.testing.assert_array_equal(np.array([row[1] for row in recorded]), expected_raw)
        self.assertTrue(all(row[2] == 0 for row in recorded))
        np.testing.assert_array_equal(core.get_snapshot()[0], raw)

    def test_transition_periods_still_advance_filter_without_recording(self):
        core = self.gui.SerialCore()
        core.start_recording()
        core.set_label(-1)
        samples = 1600 + np.random.default_rng(11).normal(0, 50, (200, 8))
        # Quantize to exactly the float32 values sent by the protocol.
        samples = samples.astype(np.float32)
        self.receive(core, b"".join(self.frame(row) for row in samples))
        self.assertEqual(core.record_buf, [])
        core.set_label(1)
        self.receive(core, self.frame(np.full(8, 1600)))
        expected = EMGSignalFilter().process(np.vstack((samples[:, self.gui.PHYSICAL_CH_MAP], np.full((1, 8), 1600))))
        raw, filtered, _, total, _ = core.get_snapshots()
        self.assertEqual(total, 201)
        np.testing.assert_allclose(filtered, expected, atol=1e-5)
        self.assertEqual(len(core.stop_recording()), 1)

    def test_long_gap_resets_filter_and_visible_history(self):
        core = self.gui.SerialCore()
        self.receive(core, self.frame(np.full(8, 1600)))
        core.last_data_time -= 1.0
        self.receive(core, self.frame(np.full(8, 2200)))
        raw, filtered, _, total, _ = core.get_snapshots()
        self.assertEqual(total, 2)
        self.assertEqual(raw.shape, (1, 8))
        np.testing.assert_array_equal(raw, 2200)
        np.testing.assert_allclose(filtered, 0, atol=1e-6)
        np.testing.assert_allclose(core.get_display_snapshots()[2], 0, atol=1e-6)


if __name__ == "__main__":
    unittest.main()

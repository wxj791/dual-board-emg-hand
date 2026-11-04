"""Numeric/streaming checks that matter for later RTL and field operation."""
from pathlib import Path
import importlib.util
import json

import numpy as np
import torch

from .data import StreamFilter, filter_exact, round_shift, load_prepared
from .quantize import load_integer, execute, direct_reference
from .replay import StreamingPredictor


def verify(root):
    torch.set_num_threads(4)
    root = Path(root)
    model, shifts, _ = load_integer(root/'field/export')
    checks = []
    project = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('_t03', project/'vectors/t03_generate_vectors.py')
    legacy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(legacy)
    inputs = np.array([legacy.make_input(i) for i in range(256)])
    filters = [[legacy.Biquad(c) for c in legacy.COEFFICIENTS] for _ in range(8)]
    expected = []
    for row in inputs:
        current = []
        for ch, value in enumerate(row):
            for biquad in filters[ch]:
                value = biquad.step(int(value))
            current.append(value)
        expected.append(current)
    assert np.array_equal(filter_exact(inputs), expected)
    checks.append('T03_256_vectors_match_existing_independent_model')
    rng = np.random.default_rng(41)
    raw = rng.integers(0, 3301, size=(2048, 8))
    streaming = StreamFilter()
    parts = [streaming.process(chunk) for chunk in np.array_split(raw, 37)]
    assert np.array_equal(np.concatenate(parts), filter_exact(raw))
    checks.append('stateful_filter_matches_one_block_for_arbitrary_chunks')
    one = StreamingPredictor(root/'field/export').push(raw)
    many_predictor = StreamingPredictor(root/'field/export')
    many = [item for chunk in np.array_split(raw, 37) for item in many_predictor.push(chunk)]
    assert one == many
    assert one[0]['sample_end'] == 448 and len(one) == (len(raw)-448)//32+1
    checks.append('continuous_label_free_prediction_chunk_invariance_and_128_32_schedule')
    filtered = filter_exact(raw)
    expected_input = np.stack([np.clip(round_shift(filtered[320:448, c], int(shifts[c])), -127, 127)
                               for c in range(8)], 0)
    logits = model.trace(expected_input[None])['logits'][0]
    assert int(logits.argmax()) == one[0]['prediction']
    checks.append('first_continuous_window_matches_offline_integer_pipeline')
    with np.load(root/'field/export/golden_layers.npz') as z:
        golden = {k: z[k] for k in z.files}
    for name, layer in model.layers.items():
        source = {'stem': 'input', 'conv2': 'pool1', 'conv3': 'pool2', 'fc': 'features'}[name]
        assert np.array_equal(execute(layer, golden[source]), direct_reference(layer, golden[source]))
    checks.append('20_windows_all_4_layers_match_separate_int64_reference')
    boundaries = round_shift(np.array([-5, -3, -1, 0, 1, 3, 5]), 1)
    assert np.array_equal(boundaries, [-3, -2, -1, 0, 1, 2, 3])
    checks.append('signed_tie_away_from_zero_rounding')
    data, manifest = load_prepared(root/'field/data')
    for segment in manifest['segments']:
        mask = data['group'] == segment['id']
        assert len(np.unique(data['split'][mask])) == 1
        assert np.all(data['start'][mask] >= segment['start']+320)
        assert np.all(data['start'][mask]+128 <= segment['stop'])
    checks.append('all_windows_inside_one_partition_segment_after_startup_guard')
    result = {'checks_passed': checks, 'rtl_equivalence': False,
              'note': 'Numeric and PC stream verification only, no serial hardware opened.'}
    (root/'verification.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))
    return result


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--run', type=Path, default=Path('artifacts/pc_ml'))
    verify(p.parse_args().run)

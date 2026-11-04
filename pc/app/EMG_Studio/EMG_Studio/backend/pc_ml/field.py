"""One recording -> repeat-held-out PC training -> quantized parameter package."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch

from .data import prepare, DatasetPreparationError
from .train import fit, infer, metrics
from .quantize import calibrate, export, load_integer, direct_reference, execute
from .replay import Decision


def stable_evaluation(integer, data, mask, margin):
    logits = integer.predict_logits(data['x'][mask])
    truth, groups = data['y'][mask], data['group'][mask]
    predictions = []
    accepted = []
    previous = None
    for logit, group in zip(logits, groups):
        if group != previous:
            decision = Decision(margin=margin, persistence=3)
        _, _, ok, stable = decision.step(logit)
        predictions.append(stable)
        accepted.append(ok)
        previous = group
    result = metrics(truth, np.array(predictions), logits.shape[1])
    result['accept_fraction'] = float(np.mean(accepted))
    return result


def run(recording, output, epochs=18, threads=4, frontend='precision', kind='sequential'):
    torch.set_num_threads(threads)
    data, manifest = prepare(recording.parent, output/'data', field_recording=recording, frontend=frontend)
    class_names = manifest['class_names']
    num_classes = len(class_names)
    train, validation, test = [data['split'] == i for i in range(3)]
    print('Training-only input shifts:', data['input_shifts'].tolist(), flush=True)
    # Current evidence selected sequential on the previous architecture validation.
    model, details = fit(kind, data['x'][train], data['y'][train],
                         data['x'][validation], data['y'][validation], output,
                         'field_cnn', epochs=epochs, samples=4096, num_classes=num_classes)
    train_indices = np.flatnonzero(train)
    rng = np.random.default_rng(88)
    calibration = data['x'][rng.choice(train_indices, min(2048, len(train_indices)), replace=False)]
    integer = calibrate(model, kind, calibration)
    # Margin selected on validation only; must preserve gestures as well as rest.
    margin_rows = []
    for margin in (0, 32, 64, 128, 256):
        m = stable_evaluation(integer, data, validation, margin)
        margin_rows.append({'margin': margin, 'metrics': m})
    selected = max(margin_rows, key=lambda r: r['metrics']['macro_f1'])
    descriptor = export(integer, output/'export', data['input_shifts'], 'field_cnn', class_names)
    descriptor['gesture_profile'] = manifest.get('gesture_profile')
    descriptor['action_mapping'] = [c.get('output', {'id': 0, 'name': '保持'})
                                    for c in (manifest.get('gesture_profile') or {}).get('classes', [])]
    descriptor['decision'] = {'margin': selected['margin'], 'persistence': 3}
    descriptor['input_channel_order'] = 'Recorder physical order: source PHYSICAL_CH_MAP [3,2,1,0,4,5,6,7]; do not remap recorded files again.'
    descriptor['filter_coefficients_q14'] = manifest['coefficients_q14']
    descriptor['frontend'] = manifest.get('frontend')
    if descriptor['frontend']:
        descriptor['format'] = 'EMG-PARAM-2'
    descriptor['data_sources_sha256'] = manifest['sources_sha256']
    descriptor['input_units'] = ('Recorded integer mV, minus 1650 then Q8; signed24 filter states; FPGA adapter must match.'
                                 if descriptor['frontend'] else 'Recorded mV rounded to signed16 before T03 filter; FPGA adapter must match.')
    (output/'export/manifest.json').write_text(json.dumps(descriptor, ensure_ascii=False, indent=2), encoding='utf-8')
    reloaded, _, _ = load_integer(output/'export')
    vectors = np.rint(calibration[:20]*32).astype(np.int8)
    golden = reloaded.trace(vectors)
    np.savez_compressed(output/'export/golden_layers.npz', **golden)
    # All conv and FC layers checked with separate int64 products and sums.
    for name, layer in integer.layers.items():
        source = {'stem': 'input', 'conv2': 'pool1', 'conv3': 'pool2', 'fc': 'features',
                  'fusion': 'concat', 'temporal': 'fusion',
                  **{f'branches.{i}': 'pool1' for i in range(3)}}[name]
        assert np.array_equal(execute(layer, golden[source]), direct_reference(layer, golden[source])), name
    results = {}
    for name, mask in [('validation', validation), ('test', test)]:
        fp = infer(model, data['x'][mask]).argmax(1)
        ip = integer.predict_logits(data['x'][mask]).argmax(1)
        results[name] = {'float': metrics(data['y'][mask], fp, num_classes), 'integer': metrics(data['y'][mask], ip, num_classes),
                         'agreement': float(np.mean(fp == ip)),
                         'stable': stable_evaluation(integer, data, mask, selected['margin'])}
    ckpt = torch.load(output/'field_cnn.pth', weights_only=True)
    ckpt.update({'input_shifts': data['input_shifts'].tolist(), 'input_scale': 1/32,
                 'window': 128, 'stride': 32, 'class_names': class_names,
                 'gesture_profile': manifest.get('gesture_profile')})
    torch.save(ckpt, output/'field_cnn.pth')
    held_out = results['test']['integer']
    acceptance = {'overall_target': .95, 'per_class_recall_target': .90,
                  'rest_window_false_activation_target': .01,
                  'passed_offline': (held_out['accuracy'] >= .95 and min(held_out['recall']) >= .90
                                     and held_out['rest_false_activation'] <= .01),
                  'weak_classes': [class_names[i] for i, value in enumerate(held_out['recall']) if value < .90],
                  'prospective_online_acceptance': False}
    report = {'recording': str(recording), 'training': details, 'evaluation': results,
              'acceptance': acceptance,
              'validation_margin_sweep': margin_rows, 'selected_margin_q8': selected['margin'],
              'parameters': descriptor, 'int64_reference_verified': True,
              'limitations': manifest['limitations'] + ['Stable metric resets at known label runs; action transitions are not measured.',
                                                       'Windows within a held-out repetition overlap; they are not independent trials.']}
    (output/'field_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Field held-out test:', json.dumps(results['test'], ensure_ascii=False), flush=True)
    return report


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--recording', required=True, type=Path)
    p.add_argument('--output', type=Path, default=Path('artifacts/pc_ml/field'))
    p.add_argument('--epochs', type=int, default=18)
    p.add_argument('--threads', type=int, default=4)
    p.add_argument('--frontend', choices=['legacy', 'precision', 'emg'], default='precision')
    p.add_argument('--kind', choices=['sequential', 'multiscale'], default='sequential')
    a = p.parse_args()
    try:
        run(a.recording.resolve(), a.output.resolve(), a.epochs, a.threads, a.frontend, a.kind)
    except (DatasetPreparationError, FileNotFoundError) as exc:
        print(f'DATA_ERROR: {exc}', flush=True)
        raise SystemExit(2)

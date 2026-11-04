"""Repeat-held-out experiments with interference removed and explicit fixed point.

Isolated from the workstation's active model. Test partitions are evaluated
only after validation selects the frontend/topology. No serial or motor I/O.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch
from scipy import signal

from .data import COEFFICIENTS, load_prepared, round_shift
from .models import profile, make_model
from .frontend import PrecisionFilter, coefficients, frontend_descriptor
from .quantize import calibrate, export, direct_reference, execute
from .train import fit, infer, metrics



def refined_data(base, manifest, kind):
    result = {k: np.copy(v) for k, v in base.items() if k not in ('x', 'filtered', 'input_shifts')}
    filtered = np.empty_like(base['filtered'], dtype=np.int32)
    saturated = 0
    for file in manifest['sources_sha256']:
        with np.load(file, allow_pickle=True) as z:
            raw = z['emg']
        for segment in manifest['segments']:
            if segment['file'] != file:
                continue
            frontend = PrecisionFilter(coefficients(kind))
            samples = frontend.process(raw[segment['start']:segment['stop']])
            saturated += frontend.saturations
            mask = np.flatnonzero(base['group'] == segment['id'])
            for index in mask:
                start = int(base['start'][index])-segment['start']
                filtered[index] = samples[start:start+128].T
    train = result['split'] == 0
    magnitude = np.percentile(abs(filtered[train].astype(np.int64)), 99.5, axis=(0, 2))
    shifts = np.ceil(np.log2(np.maximum(magnitude, 1)/112)).astype(int)
    quantized = np.stack([np.clip(round_shift(filtered[:, ch], int(shifts[ch])), -127, 127)
                          for ch in range(8)], axis=1).astype(np.int8)
    result.update(filtered=filtered, input_shifts=shifts, x=quantized.astype(np.float32)/32)
    descriptor = {'name': kind, 'sample_rate': 1000, 'raw_offset_mv': 1650,
                  'state_fraction': 8, 'state_bits': 24, 'coefficient_fraction': 14,
                  'accumulator_bits': 48, 'coefficients_q14': [list(r) for r in coefficients(kind)],
                  'saturations': saturated, 'input_shifts': shifts.tolist(),
                  'raw_input': 'Integer mV, ties-to-even, clipped 0..3300'}
    return result, descriptor


def interference_audit(manifest):
    rows = []
    for file in manifest['sources_sha256']:
        with np.load(file, allow_pickle=True) as z:
            raw = z['emg']
        for segment in manifest['segments']:
            if segment['file'] != file:
                continue
            x = raw[segment['start']+320:segment['stop']]
            if len(x) < 1000:
                continue
            f, p = signal.welch(x, fs=1000, nperseg=1000, axis=0)
            band = (f >= 20) & (f <= 450)
            line = (f >= 48) & (f <= 52)
            rows.append({'group': segment['id'], 'label': segment['label'],
                         'line_fraction_5_450': float(p[line].sum()/max(p[(f >= 5) & (f <= 450)].sum(), 1e-20)),
                         'line_fraction_20_450': float(p[line].sum()/max(p[band].sum(), 1e-20)),
                         'non_line_power': p[band & ~line].sum(0).tolist()})
    return {'nominal_sample_rate': 1000, 'segments': rows,
            'limitation': 'Host timestamps do not prove sensor timing; residual power is not automatically biological EMG.'}


def run(args):
    torch.set_num_threads(args.threads)
    args.output.mkdir(parents=True, exist_ok=True)
    all_results = []
    for session in args.sessions:
        root = Path(session)
        base, manifest = load_prepared(root/'data')
        out = args.output/root.name
        out.mkdir(parents=True, exist_ok=True)
        train, val, test = [base['split'] == i for i in range(3)]
        candidates = []
        trained = {}
        datasets = {'legacy': base}
        descriptors = {'legacy': None}
        # Same topology isolates DSP precision; multiscale adds an amplitude path.
        configurations = [('legacy', 'sequential'), ('precision', 'sequential'),
                          ('emg', 'sequential'), ('emg', 'multiscale')]
        for front, kind in configurations:
            name = f'{front}_{kind}'
            if front not in datasets:
                datasets[front], descriptors[front] = refined_data(base, manifest, front)
            data = datasets[front]
            if args.reuse_checkpoints and (out/f'{name}.pth').is_file():
                checkpoint = torch.load(out/f'{name}.pth', weights_only=True)
                if checkpoint['kind'] != kind or len(checkpoint['history']) != args.epochs:
                    raise ValueError('Cached experiment configuration differs')
                model = make_model(kind, len(manifest['class_names']))
                model.load_state_dict(checkpoint['state_dict'])
                details = {k: checkpoint[k] for k in ('name', 'kind', 'seed', 'best_epoch', 'history')}
                details.update(profile=profile(model), reused_checkpoint=True)
            else:
                model, details = fit(kind, data['x'][train], data['y'][train],
                                     data['x'][val], data['y'][val], out, name,
                                     epochs=args.epochs, samples=4096,
                                     num_classes=len(manifest['class_names']))
            rng = np.random.default_rng(88)
            indices = np.flatnonzero(train)
            calibration = data['x'][rng.choice(indices, min(2048, len(indices)), replace=False)]
            integer = calibrate(model, kind, calibration)
            validation = metrics(data['y'][val], integer.predict_logits(data['x'][val]).argmax(1),
                                 len(manifest['class_names']))
            row = {'name': name, 'frontend': front, 'kind': kind, 'training': details,
                   'validation_integer': validation, 'frontend_descriptor': descriptors[front]}
            candidates.append(row)
            trained[name] = model, integer, calibration
            print('VALIDATION', name, validation['accuracy'], validation['macro_f1'], flush=True)
        selected = max(candidates, key=lambda row: row['validation_integer']['macro_f1'])
        # Freeze selection before any test evaluation. All test scores below are
        # exploratory ablations; future independent recordings remain required.
        for row in candidates:
            model, integer, calibration = trained[row['name']]
            data = datasets[row['frontend']]
            row['test_integer'] = metrics(data['y'][test], integer.predict_logits(data['x'][test]).argmax(1),
                                          len(manifest['class_names']))
            row['test_float'] = metrics(data['y'][test], infer(model, data['x'][test]).argmax(1),
                                        len(manifest['class_names']))
            descriptor = export(integer, out/row['name']/'export', data['input_shifts'],
                                row['name'], manifest['class_names'])
            descriptor.update(frontend=frontend_descriptor(row['frontend']),
                              filter_coefficients_q14=COEFFICIENTS if row['frontend']=='legacy' else descriptors[row['frontend']]['coefficients_q14'],
                              decision={'margin': 0, 'persistence': 3},
                              gesture_profile=manifest.get('gesture_profile'),
                              data_sources_sha256=manifest['sources_sha256'])
            if descriptor['frontend']:
                descriptor['format'] = 'EMG-PARAM-2'
            (out/row['name']/'export/manifest.json').write_text(json.dumps(descriptor, ensure_ascii=False, indent=2), encoding='utf-8')
            row['parameter_bytes'] = descriptor['payload_bytes']
            vectors = np.rint(calibration[:8]*32).astype(np.int8)
            trace = integer.trace(vectors)
            sources = {'stem': 'input', 'conv2': 'pool1', 'conv3': 'pool2',
                       'fusion': 'concat', 'temporal': 'fusion', 'fc': 'features',
                       **{f'branches.{i}': 'pool1' for i in range(3)}}
            for layer_name, layer in integer.layers.items():
                assert np.array_equal(execute(layer, trace[sources[layer_name]]),
                                      direct_reference(layer, trace[sources[layer_name]])), layer_name
            np.savez_compressed(out/row['name']/'export/golden_layers.npz', **trace)
            print('TEST', row['name'], row['test_integer']['accuracy'], row['test_integer']['recall'], flush=True)
        result = {'source_session': str(root), 'class_names': manifest['class_names'],
                  'recording_sha256': manifest['sources_sha256'], 'selected_on_validation': selected['name'],
                  'candidates': candidates, 'interference': interference_audit(manifest),
                  'limitations': ['Exploratory, same-recording whole-repeat holdouts, not prospective online acceptance.',
                                  'No transitions retained by recorder; rest false activation here is a window fraction, not events/minute.',
                                  'No hardware deployment or routed full-network area claim.',
                                  'Filtering residual noise is not proof of effective muscle information.']}
        (out/'comparison.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        all_results.append(result)
    (args.output/'comparison.json').write_text(json.dumps(all_results, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sessions', type=Path, nargs='+', required=True)
    parser.add_argument('--output', type=Path, default=Path('artifacts/pc_ml/refinement'))
    parser.add_argument('--epochs', type=int, default=18)
    parser.add_argument('--threads', type=int, default=4)
    parser.add_argument('--reuse-checkpoints', action='store_true',
                        help='Resume this unchanged isolated experiment after interruption')
    run(parser.parse_args())

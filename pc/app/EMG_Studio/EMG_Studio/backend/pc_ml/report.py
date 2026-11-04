"""Plots, held-out signal stress checks, and a readable PC experiment report."""
from pathlib import Path
import json
import time

import numpy as np
import torch

from .data import load_prepared
from .models import make_model
from .quantize import load_integer
from .train import metrics


def build(run):
    run = Path(run)
    torch.set_num_threads(4)
    benchmark = json.loads((run/'benchmark.json').read_text(encoding='utf-8'))
    field = json.loads((run/'field/field_report.json').read_text(encoding='utf-8'))
    audit = json.loads((run/'data_audit.json').read_text(encoding='utf-8'))
    integer, _, manifest = load_integer(run/'field/export')
    data, _ = load_prepared(run/'field/data')
    mask = data['split'] == 2
    x, y = data['x'][mask], data['y'][mask]
    stress = {}
    def evaluate(name, values):
        qx = np.clip(np.rint(values*32), -127, 127).astype(np.float32)/32
        stress[name] = metrics(y, integer.predict_logits(qx).argmax(1))
    evaluate('clean', x)
    evaluate('global_gain_0.5', x*.5)
    evaluate('global_gain_1.5', x*1.5)
    noise = np.random.default_rng(17).normal(0, .1, x.shape).astype(np.float32)
    evaluate('post_filter_noise_std_0.1_model_units', x+noise)
    for ch in range(8):
        corrupted = x.copy()
        corrupted[:, ch] = 0
        evaluate(f'channel_{ch+1}_missing', corrupted)
    # Wall time of PC emulator; hardware latency is not inferred from this.
    sample = x[:1]
    for _ in range(5):
        integer.predict_logits(sample)
    durations = []
    for _ in range(30):
        begin = time.perf_counter()
        integer.predict_logits(sample)
        durations.append((time.perf_counter()-begin)*1000)
    (run/'stress.json').write_text(json.dumps({'evaluation': stress,
        'limitation': 'Synthetic perturbations AFTER the filter in quantized model units; not recordings of a new location/person.',
        'pc_float64_integer_emulator_batch1_ms': {'median': float(np.median(durations)), 'p95': float(np.percentile(durations, 95))}},
        indent=2), encoding='utf-8')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 2, figsize=(14, 9))
    names = ['Seq: field', 'Multi: field', 'Old data only', 'Multi: fine-tune']
    scores = [r['evaluation']['field_test']['accuracy']*100 for r in benchmark['results']]
    axes[0, 0].bar(names, scores, color=['#0284c7','#14b8a6','#94a3b8','#6366f1'])
    axes[0, 0].set(ylim=(0, 105), ylabel='Held-out repetition accuracy (%)', title='Exploratory architecture comparison')
    for i, value in enumerate(scores):
        axes[0, 0].text(i, value+1, f'{value:.2f}', ha='center')
    h = field['training']['history']
    axes[0, 1].plot([e['epoch'] for e in h], [e['validation_macro_f1']*100 for e in h], marker='o')
    axes[0, 1].axvline(field['training']['best_epoch'], linestyle='--', color='#f97316', label='Validation-selected epoch')
    axes[0, 1].set(xlabel='Epoch', ylabel='Validation macro-F1 (%)', title='Field-only calibration and training')
    axes[0, 1].legend()
    cm = np.array(field['evaluation']['test']['integer']['confusion'])
    axes[1, 0].imshow(cm, cmap='Blues')
    axes[1, 0].set(xticks=range(7), yticks=range(7), xlabel='Predicted gesture', ylabel='Recorded gesture', title='INT8 held-out repetition confusion')
    for i in range(7):
        for j in range(7):
            if cm[i, j]:
                axes[1, 0].text(j, i, str(cm[i, j]), ha='center', va='center', color='white' if cm[i, j] > cm.max()/2 else 'black')
    stress_names = ['clean', 'global_gain_0.5', 'global_gain_1.5']+[f'channel_{i+1}_missing' for i in range(8)]
    axes[1, 1].barh(range(len(stress_names)), [stress[n]['accuracy']*100 for n in stress_names], color='#0d9488')
    axes[1, 1].set(yticks=range(len(stress_names)), yticklabels=['Clean','Gain x0.5','Gain x1.5']+[f'Missing channel {i+1}' for i in range(8)],
                   xlim=(0, 105), xlabel='Accuracy (%)', title='Synthetic stress: does not prove new-wearing robustness')
    fig.suptitle('EMG FPGA PC study - on-site model adaptation', fontsize=16)
    fig.tight_layout()
    fig.savefig(run/'overview.png', dpi=160)
    plt.close(fig)
    # Useful plain text, with no misleading all-recording replay score.
    lines = ['PC EMG FIELD EXPERIMENT', '', f'Recordings audited: {len(audit)}',
             'Archives / aborted / missing-class files are not blindly pooled.',
             f'Field training time: {field["training"]["train_seconds"]:.2f} s (optimization + validation only)',
             f'INT8 parameter payload: {manifest["payload_bytes"]} bytes',
             f'Weights: {sum(t["bytes"] for t in manifest["tensors"] if t["name"].endswith("weights"))} bytes',
             f'Parameters after BN folding: 4399; MAC/window: {field["training"]["profile"]["macs"]}', '',
             'Held-out third repetition within one recording:']
    for mode in ('float', 'integer', 'stable'):
        m = field['evaluation']['test'][mode]
        lines.append(f'{mode}: accuracy={m["accuracy"]:.4%}; macro-F1={m["macro_f1"]:.6f}; rest false activation={m["rest_false_activation"]:.4%}')
    lines += ['', 'This is an exploratory PC result, not a new-wearing test or an FPGA board test.',
              'Stable result includes startup persistence delay and known-segment resets.',
              'Sample timestamps are host receive timestamps; sensor gaps cannot be recovered.',
              'Further fresh recordings are required for an unbiased prospective acceptance test.']
    (run/'RESULTS.txt').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    print('\n'.join(lines))


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--run', type=Path, default=Path('artifacts/pc_ml'))
    build(p.parse_args().run)

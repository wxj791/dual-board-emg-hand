"""Fixed split, training-only sampling, validation-only selection; CPU friendly."""
from __future__ import annotations

import argparse
import copy
import json
import random
import time
from pathlib import Path

import numpy as np
import torch
from sklearn.metrics import confusion_matrix, f1_score, recall_score
from torch import nn

from .data import prepare, load_prepared
from .models import make_model, profile


def seed_all(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)


def metrics(y, predicted, num_classes=7):
    labels = list(range(num_classes))
    matrix = confusion_matrix(y, predicted, labels=labels)
    return {'accuracy': float(np.mean(y == predicted)),
            'macro_f1': float(f1_score(y, predicted, labels=labels, average='macro', zero_division=0)),
            'recall': recall_score(y, predicted, labels=labels, average=None, zero_division=0).tolist(),
            'rest_false_activation': float(np.mean(predicted[y == 0] != 0)) if np.any(y == 0) else None,
            'confusion': matrix.tolist(), 'windows': len(y)}


@torch.no_grad()
def infer(model, x, batch_size=512):
    model.eval()
    return torch.cat([model(batch) for batch in torch.as_tensor(x).split(batch_size)]).numpy()


def fit(kind, x, y, val_x, val_y, output, name, epochs=12, samples=8192,
        initial=None, lr=.003, seed=42, num_classes=7):
    seed_all(seed)
    model = make_model(kind, num_classes)
    if initial is not None:
        model.load_state_dict(initial)
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=2e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, epochs, eta_min=lr*.08)
    tensor_x = torch.as_tensor(x)
    tensor_y = torch.as_tensor(y)
    # Equal-class minibatch sampling rather than a rest-dominated objective.
    per_class = [np.flatnonzero(y == i) for i in range(num_classes)]
    if any(not len(indices) for indices in per_class):
        raise ValueError('Training requires every gesture class')
    best, best_state, best_epoch = -1, None, None
    history = []
    start_time = time.perf_counter()
    rng = np.random.default_rng(seed)
    print(f'\n{name}: train={len(y)}, validation={len(val_y)}, epochs={epochs}', flush=True)
    for epoch in range(epochs):
        model.train()
        labels = rng.integers(0, num_classes, samples)
        indices = np.array([rng.choice(per_class[i]) for i in labels])
        loss_total = 0.
        for batch in np.array_split(indices, max(1, int(np.ceil(samples/256)))):
            bx, by = tensor_x[batch], tensor_y[batch]
            # Physical-scale disturbances; no arbitrary channel rotation assumed.
            gain = torch.exp(torch.randn(len(bx), 8, 1)*.12)
            global_gain = torch.exp(torch.randn(len(bx), 1, 1)*.12)
            bx = (bx*gain*global_gain + torch.randn_like(bx)*.012).clamp(-127/32, 127/32)
            # Occasionally mask one faulty electrode, keep all other channels.
            drop = torch.rand(len(bx)) < .12
            bx[torch.arange(len(bx))[drop], torch.randint(0, 8, (int(drop.sum()),))] = 0
            opt.zero_grad(set_to_none=True)
            logits = model(bx)
            loss = nn.functional.cross_entropy(logits, by, label_smoothing=.025)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5)
            opt.step()
            loss_total += float(loss.detach())*len(batch)
        sched.step()
        val = metrics(val_y, infer(model, val_x).argmax(1), num_classes)
        record = {'epoch': epoch+1, 'loss': loss_total/samples,
                  'validation_accuracy': val['accuracy'], 'validation_macro_f1': val['macro_f1']}
        history.append(record)
        if val['macro_f1'] > best:
            best = val['macro_f1']
            best_state = copy.deepcopy(model.state_dict())
            best_epoch = epoch+1
        print(f'{name} {epoch+1:02}/{epochs}: loss={record["loss"]:.4f} '
              f'val_acc={val["accuracy"]:.4f} val_F1={val["macro_f1"]:.4f}', flush=True)
    model.load_state_dict(best_state)
    duration = time.perf_counter()-start_time
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    torch.save({'kind': kind, 'state_dict': best_state, 'name': name, 'seed': seed, 'num_classes': num_classes,
                'best_epoch': best_epoch, 'history': history}, output / f'{name}.pth')
    return model, {'name': name, 'kind': kind, 'seed': seed,
                   'best_epoch': best_epoch, 'train_seconds': duration,
                   'history': history, 'profile': profile(model)}


def evaluate_partitions(model, data):
    result = {}
    for name, target in [('source', False), ('field', True)]:
        for label, split in [('validation', 1), ('test', 2)]:
            mask = (data['split'] == split) & (data['target'] == target)
            result[f'{name}_{label}'] = metrics(data['y'][mask], infer(model, data['x'][mask]).argmax(1))
    return result


def benchmark(args):
    torch.set_num_threads(args.threads)
    data, manifest = prepare(args.data_root, args.output / 'data')
    x, y = data['x'], data['y']
    train_source = (data['split'] == 0) & ~data['target']
    val_source = (data['split'] == 1) & ~data['target']
    train_field = (data['split'] == 0) & data['target']
    val_field = (data['split'] == 1) & data['target']
    rows, models = [], {}
    for kind, name in [('sequential', 'sequential_field'), ('multiscale', 'multiscale_field')]:
        model, row = fit(kind, x[train_field], y[train_field], x[val_field], y[val_field],
                         args.output, name, epochs=args.epochs, samples=args.field_samples)
        rows.append(row)
        models[name] = model
    base, row = fit('multiscale', x[train_source], y[train_source], x[val_source], y[val_source],
                    args.output, 'multiscale_foundation', epochs=args.epochs,
                    samples=args.source_samples)
    rows.append(row)
    models[row['name']] = base
    tuned, row = fit('multiscale', x[train_field], y[train_field], x[val_field], y[val_field],
                     args.output, 'multiscale_adapted', epochs=args.epochs,
                     samples=args.field_samples, initial=base.state_dict(), lr=.001)
    rows.append(row)
    models[row['name']] = tuned
    # Select architecture/checkpoint exclusively on field validation, never test.
    field_candidates = [r for r in rows if r['name'] != 'multiscale_foundation']
    selected = max(field_candidates, key=lambda r: max(h['validation_macro_f1'] for h in r['history']))
    for row in rows:
        row['evaluation'] = evaluate_partitions(models[row['name']], data)
    torch.save({'kind': selected['kind'], 'state_dict': models[selected['name']].state_dict(),
                'name': selected['name'], 'input_shifts': data['input_shifts'].tolist(),
                'input_scale': 1/32, 'window': 128, 'stride': 32,
                'data_manifest_sha256': __import__('hashlib').sha256(
                    (args.output/'data/data_manifest.json').read_bytes()).hexdigest()}, args.output/'selected.pth')
    report = {'seed': 42, 'threads': args.threads, 'nominal_sample_rate': 1000,
              'epochs': args.epochs, 'field_samples_per_epoch': args.field_samples,
              'source_samples_per_epoch': args.source_samples,
              'selection': selected['name'], 'selection_rule': 'Max field validation macro-F1',
              'results': rows, 'limitations': manifest['limitations'],
              'frontend_calibration': 'Fixed channel shifts from training partitions, including field train; not a calibration-free zero-shot test.'}
    (args.output/'benchmark.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    for row in rows:
        m = row['evaluation']['field_test']
        print(f'RESULT {row["name"]}: field test acc={m["accuracy"]:.4f}, F1={m["macro_f1"]:.4f}, '
              f'train={row["train_seconds"]:.1f}s', flush=True)
    print('SELECTED ON VALIDATION:', selected['name'], flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-root', type=Path, default=Path('E:/FPGA/FPGA2026(5)/emg_ml/data/raw'))
    parser.add_argument('--output', type=Path, default=Path('artifacts/pc_ml'))
    parser.add_argument('--epochs', type=int, default=14)
    parser.add_argument('--source-samples', type=int, default=8192)
    parser.add_argument('--field-samples', type=int, default=4096)
    parser.add_argument('--threads', type=int, default=4)
    benchmark(parser.parse_args())


if __name__ == '__main__':
    main()

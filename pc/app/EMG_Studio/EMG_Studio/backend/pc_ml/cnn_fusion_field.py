"""Fixed reusable training recipe for PC statistics + temporal CNN preview."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import time

import numpy as np
import torch
from torch import nn
from threadpoolctl import threadpool_limits

from .cnn_fusion import RECIPE, TITLE, RUNTIME, make_model, normalize, wave_q4, logits, fuse, export, load
from .fusion import FusionFeatures, advance, fit_fusion
from .fusion_field import prepare as original_prepare, smooth
from .train import metrics


def dump(path, value): path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')


def adaptive_splits(y, group, names):
    """Hold out whole acquired bouts only when every class can support it."""
    k = len(names); valid = y >= 0
    windows = np.bincount(y[valid], minlength=k)
    missing = [f'{names[c]}（{windows[c]}个有效窗口）' for c in range(k) if windows[c] < 2]
    if missing:
        raise ValueError('以下动作有效数据不足以拟合统计模型：'+'、'.join(missing)+
                         '；需至少2个有效窗口，请补采或检查断流（保留滤波预热及256点分析窗）。')
    bouts = [np.unique(group[y == c]) for c in range(k)]
    counts = np.array([len(ids) for ids in bouts])
    split = np.full_like(y, -1)
    mode = 'three_part' if min(counts) >= 3 else 'two_part' if min(counts) >= 2 else 'training_only'
    for ids in bouts:
        ntr = max(1, len(ids)//2); nva = max(1, (len(ids)-ntr)//2)
        for rep, gid in enumerate(ids):
            if mode == 'three_part':
                part = 0 if rep < ntr else 1 if rep < ntr+nva else 2
            elif mode == 'two_part':
                part = 0 if rep < len(ids)-1 else 2
            else:
                part = 0
            split[valid & (group == gid)] = part
    warnings = []
    # Tiny first repetitions can leave too little data for LDA covariance fitting.
    train_counts = np.bincount(y[valid & (split != 2)], minlength=k)
    if np.any(train_counts < 2):
        split[valid] = 0; mode = 'training_only'
        warnings.append('独立留出后训练数据不足，已改用全部有效数据训练。')
    independent = mode != 'training_only'
    if not independent:
        warnings.append('部分动作仅有一次有效重复，或无法独立留出；未进行独立评估。')
    elif mode == 'two_part':
        warnings.append('按完整重复划分训练和测试；部分动作仅两次，没有单独验证集。')
    for part in np.unique(split[valid]):
        n = np.bincount(y[split == part], minlength=k)
        small = [f'{names[c]}（{n[c]}个）' for c in range(k) if n[c] < 32]
        if small:
            warnings.append(('训练', '验证', '测试')[part]+'数据偏少：'+'、'.join(small)+'；建议补采，仍继续训练。')
    return split, {'split_mode': mode, 'independent_evaluation': independent,
                   'training_warnings': warnings, 'windows_per_class': windows.tolist()}


def prepare(source):
    d, meta = original_prepare(source, require_partitions=False)
    with np.load(source, allow_pickle=True) as z: raw, labels = z['emg'], z['labels']
    profile = copy.deepcopy(meta.get('gesture_profile'))
    removed = []  # Train every actual class; never silently drop gestures.
    kept = [i for i in range(len(meta['class_names'])) if i not in removed]
    if len(kept) < 2: raise ValueError('至少保留静息和一种动作')
    edges = np.r_[0, np.flatnonzero(labels[1:] != labels[:-1])+1]
    physical = np.searchsorted(edges, np.arange(len(labels)), side='right')-1
    bouts = {}
    for gid, seg in enumerate(meta['segments']):
        if seg['label'] in kept and np.any(d['group'] == gid):
            physical_id = int(physical[seg['start']])
            bouts.setdefault(physical_id, {'label': kept.index(seg['label']), 'chunks': []})['chunks'].append((gid, seg))
    k = len(kept); counts = np.bincount([b['label'] for b in bouts.values()], minlength=k)
    y = np.full_like(d['y'], -1); split = y.copy(); group = y.copy(); ordinal = np.zeros(k, dtype=int)
    records = []
    for gid, b in bouts.items():
        c = b['label']; rep = int(ordinal[c]); ordinal[c] += 1
        for old_gid, seg in b['chunks']:
            mask = d['group'] == old_gid; y[mask] = c; group[mask] = gid
        records.append({'group': gid, 'label': c, 'repetition': rep,
                        'chunks': [s for _, s in b['chunks']]})
    split, plan = adaptive_splits(y, group, [meta['class_names'][i] for i in kept])
    for record in records:
        record['split'] = int(split[group == record['group']][0])
    if profile:
        profile['classes'] = [profile['classes'][i] for i in kept]
        for c, row in enumerate(profile['classes']): row['id'] = c
        if removed: profile['name'] = '前臂七类（去掉翻掌）'
    meta.update(original_class_names=meta['class_names'], class_names=[meta['class_names'][i] for i in kept],
                gesture_profile=profile, removed_original_ids=removed, retained_original_ids=kept,
                segments=records, repetitions=counts.tolist(), grouping='label_bout_across_timestamp_gaps',
                removed_gesture_rejection_validated=False, **plan)
    d.update(y=y, split=split, group=group)
    signal=np.empty((len(raw),32),dtype=np.int32)
    capture_edges=meta['capture_edges']
    for a,b in zip(capture_edges[:-1],capture_edges[1:]):
        front = FusionFeatures()
        signal[a:b], _ = advance((np.rint(raw[a:b]).clip(0, 3300).astype(np.int64)-1650)*256,
                                front.coeff, front.state, front.env_state)
    q4 = wave_q4(signal); ends = d['start']+256
    d['waves'] = np.stack([q4[e-256:e].T for e in ends]); d['ends'] = ends
    return d, meta


def fit(d, mask):
    _, head, _ = fit_fusion(d['x'][mask], d['y'][mask], d['group'][mask])
    base = head.scores(d['x']).astype(np.float64)/2.**head.fraction_
    base -= base.mean(1, keepdims=True)
    maximum = abs(d['waves'][mask]).max((0, 2))
    shifts = np.maximum(0, np.ceil(np.log2(np.maximum(1, maximum)/120))).astype(np.int8)
    x = normalize(d['waves'], shifts); k = head.weights_.shape[0]
    torch.manual_seed(RECIPE['seed']); rng = np.random.default_rng(RECIPE['seed'])
    model = make_model(k)
    optimizer = torch.optim.AdamW(model.parameters(), lr=RECIPE['learning_rate'], weight_decay=RECIPE['weight_decay'])
    weights = torch.tensor(1/np.maximum(1, np.bincount(d['y'][mask], minlength=k)), dtype=torch.float32)
    weights /= weights.mean(); ce = nn.CrossEntropyLoss(weight=weights)
    tx = torch.tensor(x); ty = torch.tensor(np.maximum(0, d['y']), dtype=torch.long)
    tb = torch.tensor(base, dtype=torch.float32); ids = np.flatnonzero(mask)
    for epoch in range(RECIPE['epochs']):
        model.train(); order = rng.permutation(ids); total = 0.
        for first in range(0, len(order), RECIPE['batch_size']):
            b = order[first:first+RECIPE['batch_size']]
            gain = torch.exp(torch.randn(len(b), 8, 1)*RECIPE['gain_sigma'])
            optimizer.zero_grad(set_to_none=True); cnn = model(tx[b]*gain)
            loss = ce(tb[b]+cnn, ty[b])+RECIPE['auxiliary_weight']*ce(cnn, ty[b])+.002*cnn.square().mean()
            if not torch.isfinite(loss): raise ValueError('CNN训练出现无效损失')
            loss.backward(); torch.nn.utils.clip_grad_norm_(model.parameters(), 5); optimizer.step()
            total += float(loss.detach())*len(b)
        print(f'CNN {epoch+1}/{RECIPE["epochs"]} loss={total/len(ids):.5f}', flush=True)
    return head, model.eval(), shifts


def evaluate(d, head, model, shifts, mask, k):
    base = head.scores(d['x']); cnn = logits(model, normalize(d['waves'], shifts))
    combined = fuse(base, cnn, head.fraction_)
    return {'statistics': metrics(d['y'][mask], smooth(base)[mask], k),
            'raw': metrics(d['y'][mask], combined[mask].argmax(1), k),
            'stable': metrics(d['y'][mask], smooth(combined)[mask], k)}


def run(source, output):
    source = Path(source); output = Path(output); output.mkdir(parents=True, exist_ok=True)
    begin = time.perf_counter(); torch.set_num_threads(2)
    d, meta = prepare(source); k = len(meta['class_names'])
    dump(output/'data.json', meta)
    np.savez_compressed(output/'prepared.npz', **d)
    fit_mask = (d['split'] == 0) | (d['split'] == 1); test = d['split'] == 2
    independent = meta['independent_evaluation']; heldout = None
    for warning in meta['training_warnings']:
        print('数据提示：'+warning, flush=True)
    with threadpool_limits(limits=2):
        print('固定配方：clean滤波，256点，统计＋三层CNN，按本次佩戴重新训练。', flush=True)
        from .gesture_tasks_export import export_current
        deployment = {}
        if independent:
            head, model, shifts = fit(d, fit_mask)
            heldout = evaluate(d, head, model, shifts, test, k)
            export(output/'validation_export', head, model, shifts, {**meta, 'trained_all_bouts': False})
            try:
                deployment['heldout'] = export_current(output/'validation_export', d, output/'a_validation')['verification']
            except (ValueError, OverflowError) as e:
                deployment['heldout_error'] = str(e)
        valid = d['y'] >= 0
        print(('留出完成；' if independent else '未进行独立评估；')+'使用本次全部有效重复生成试用权重。', flush=True)
        head, model, shifts = fit(d, valid)
        descriptor = export(output/'export', head, model, shifts, {**meta, 'trained_all_bouts': True})
        try:
            deployment['final'] = export_current(output/'export', d, output/'a_deployment')['verification']
        except (ValueError, OverflowError) as e:
            deployment['final_error'] = str(e)
        reloaded, cnn2, shifts2, _ = load(output/'export')
        np.testing.assert_array_equal(head.scores(d['x']), reloaded.scores(d['x']))
        np.testing.assert_array_equal(logits(model, normalize(d['waves'][:16], shifts)),
                                      logits(cnn2, normalize(d['waves'][:16], shifts2)))
        calibration = evaluate(d, head, model, shifts, valid, k)['raw']
    test_metrics = ({'cnn_float_raw': heldout['raw'], 'stable': heldout['stable'],
                     'statistics_stable': heldout['statistics']} if heldout is not None else None)
    report = {'recording': str(source), 'preset': RUNTIME, 'model_title': TITLE,
        'class_names': meta['class_names'], 'recipe': RECIPE, 'parameters': descriptor,
        'evaluation': {'independent': independent, 'mode': meta['split_mode'], 'test': test_metrics},
        'training_warnings': meta['training_warnings'],
        'calibration_fit': calibration, 'final_trained_all_bouts': True, 'a_deployment': deployment,
        'prospective_online_acceptance': False, 'timing_seconds': time.perf_counter()-begin,
        'acceptance': {'evaluated': independent,
                       'passed_offline': (heldout['stable']['accuracy'] >= .95 and
                       min(heldout['stable']['recall']) >= .9 and heldout['stable']['rest_false_activation'] <= .01)
                       if independent else None,
                       'weak_classes': [meta['class_names'][c] for c, value in enumerate(heldout['stable']['recall']) if value < .9]
                       if independent else []},
        'limitations': meta['training_warnings']+['固定配方来自已研究记录，留出成绩（如有）是开发证据。',
                       'export使用全数据，不能把训练自检当实时精度。', '电脑浮点CNN试用，尚未部署FPGA。']}
    dump(output/'field_report.json', report)
    print(json.dumps({'heldout': heldout['stable']['accuracy'] if independent else None, 'calibration': calibration['accuracy'],
                      'seconds': report['timing_seconds']}, ensure_ascii=False), flush=True)
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--recording', type=Path, required=True); p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    try: run(a.recording.resolve(), a.output.resolve())
    except (ValueError, FileNotFoundError) as e:
        print('DATA_ERROR: '+str(e), flush=True); raise SystemExit(2)


if __name__ == '__main__': main()

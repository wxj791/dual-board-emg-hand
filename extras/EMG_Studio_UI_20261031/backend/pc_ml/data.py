from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

COEFFICIENTS = ((16131, -16131, 0, -15877, 0),
                (16135, -30690, 16135, -30690, 15885),
                (3384, 6769, 3384, -6054, 3208))
GESTURES = ['静息', '握拳', '张开手掌', '点赞', '剪刀手', '数字一', '数字六']
WINDOW = 128
STRIDE = 32
GUARD = 320


class DatasetPreparationError(ValueError):
    """Actionable recording validation failure, safe to show in the GUI."""


def round_shift(x, shift):
    """Signed round-to-nearest, ties away from zero. Negative shift = left."""
    x = np.asarray(x, dtype=np.int64)
    if shift <= 0:
        return x << -shift
    return np.sign(x) * ((np.abs(x) + (1 << (shift - 1))) >> shift)


class StreamFilter:
    """T03 Q14 biquads with zero reset, per-section signed16 saturation."""
    def __init__(self):
        self.state = [[[0, 0, 0, 0] for _ in COEFFICIENTS] for _ in range(8)]

    def process(self, raw):
        raw = np.clip(np.rint(raw), -32768, 32767).astype(np.int64)
        if raw.ndim != 2 or raw.shape[1] != 8:
            raise ValueError('Expected [samples,8]')
        output = np.empty_like(raw, dtype=np.int16)
        for channel in range(8):
            values = raw[:, channel].tolist()
            for section, (b0, b1, b2, a1, a2) in enumerate(COEFFICIENTS):
                x1, x2, y1, y2 = self.state[channel][section]
                filtered = []
                for x in values:
                    acc = b0*x + b1*x1 + b2*x2 - a1*y1 - a2*y2
                    y = (acc + 8192) >> 14 if acc >= 0 else -((-acc + 8192) >> 14)
                    y = min(32767, max(-32768, y))
                    filtered.append(y)
                    x2, x1, y2, y1 = x1, x, y1, y
                self.state[channel][section] = [x1, x2, y1, y2]
                values = filtered
            output[:, channel] = values
        return output


def filter_exact(raw):
    return StreamFilter().process(raw)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare(root: Path, out: Path, field_recording: Path | None = None, frontend='legacy'):
    """Freeze groups BEFORE windows; no state or samples cross a split."""
    from .frontend import create_filter, frontend_descriptor
    frontend_config = frontend_descriptor(frontend)
    out.mkdir(parents=True, exist_ok=True)
    paths = [Path(field_recording)] if field_recording is not None else sorted([*root.glob('DGT/*.npz'), *root.glob('DGTV2/*.npz')])
    # Auto-discovery excludes partial files, but an explicit user selection must
    # be judged by its contents. A stop after a full round can also be named
    # 'aborted' by older recorder versions.
    if field_recording is None:
        paths = [p for p in paths if not any(t in p.name.lower() for t in ('abort', 'checkpoint'))]
    if not paths:
        raise DatasetPreparationError('没有找到可训练的肌电记录，请先按动作方案采集数据或选择一份npz文件。')
    signatures = {str(p): sha256(p) for p in paths}
    cache = out / 'dataset.npz'
    manifest_path = out / 'data_manifest.json'
    if cache.exists() and manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if manifest.get('sources_sha256') == signatures and manifest.get('version') == 2 and manifest.get('field_only', False) == (field_recording is not None) and manifest.get('frontend') == frontend_config:
            print('Using matching prepared dataset cache.', flush=True)
            return load_prepared(out)
    xs, ys, domains, splits, targets, groups, starts = [], [], [], [], [], [], []
    records, segments = [], []
    class_names = list(GESTURES)
    gesture_profile = None
    # Fixed in advance, not selected from model results.
    validation_ids = {15, 17}
    test_ids = {18, 19}
    for domain, path in enumerate(paths):
        with np.load(path, allow_pickle=True) as z:
            raw = z['emg'].astype(np.float32)
            labels = z['labels'].astype(np.int64)
            timestamps = z['timestamps'].astype(np.float64)
            stored_rate = float(z['sample_rate'])
            gesture_meta = z['gestures'].item()
            profile = json.loads(str(z['gesture_profile'].item())) if 'gesture_profile' in z else None
        if raw.ndim != 2 or raw.shape[1] != 8 or not np.isfinite(raw).all():
            raise DatasetPreparationError(f'{path.name}：肌电数据应为有限数值的[N,8]数组。')
        if not len(raw):
            raise DatasetPreparationError(f'{path.name}：记录为空，没有收到有效采样。请先检查串口与实时接收速率。')
        if labels.shape != (len(raw),) or timestamps.shape != (len(raw),) or not np.isfinite(timestamps).all():
            raise DatasetPreparationError(f'{path.name}：标签或时间戳长度与肌电采样数不一致。')
        if profile is not None:
            classes = profile.get('classes', [])
            if not 2 <= len(classes) <= 16 or [c.get('id') for c in classes] != list(range(len(classes))):
                raise DatasetPreparationError(f'{path.name}：动作方案需含静息及1～15个动作，编号应从0连续排列。')
            if classes[0].get('key') != 'rest':
                raise DatasetPreparationError(f'{path.name}：动作0必须是静息。')
            names = [c.get('name', '').strip() for c in classes]
            if any(not name for name in names) or len(set(names)) != len(names):
                raise DatasetPreparationError(f'{path.name}：动作名称不能为空或重复。')
            if field_recording is None and names != GESTURES:
                raise DatasetPreparationError('批量旧数据基准仅支持原七类；自定义方案请明确选择一份现场记录。')
            class_names, gesture_profile = names, profile
        num_classes = len(class_names)
        for label in range(num_classes):
            name = gesture_meta.get(label, [''])[0]
            if name != class_names[label]:
                raise DatasetPreparationError(f'{path.name}：动作{label}名称为“{name}”，与记录中的动作方案不一致。')
        if np.any((labels < -1) | (labels >= num_classes)):
            raise DatasetPreparationError(f'{path.name}：存在超出动作方案范围的标签。')
        target = field_recording is not None or path.parent.name == 'DGTV2'
        number = 0 if field_recording is not None else int(path.stem.split('_P')[1].split('_')[0])
        fixed_split = 1 if number in validation_ids else (2 if number in test_ids else 0)
        boundaries = np.r_[0, np.flatnonzero(labels[1:] != labels[:-1])+1, len(raw)]
        counts = {label: 0 for label in range(num_classes)}
        n_before = len(ys)
        for run, (a, b) in enumerate(zip(boundaries[:-1], boundaries[1:])):
            label = int(labels[a])
            if label not in counts:
                continue
            repetition = counts[label]
            counts[label] += 1
            split = (repetition % 3) if target else fixed_split
            # Recorded transitions were omitted by the original recorder.
            # Reset on every label run and on host gaps >250ms. Drop startup.
            gap_indices = np.flatnonzero(np.diff(timestamps[a:b]) > .25) + a + 1
            chunks = np.r_[a, gap_indices, b]
            for ca, cb in zip(chunks[:-1], chunks[1:]):
                if cb-ca < GUARD+WINDOW:
                    continue
                filtered = create_filter(frontend_config).process(raw[ca:cb])
                gid = len(segments)
                segments.append({'id': gid, 'file': str(path), 'domain': domain,
                                 'label': label, 'repetition': repetition, 'split': int(split),
                                 'start': int(ca), 'stop': int(cb), 'is_target': target})
                for start in range(GUARD, len(filtered)-WINDOW+1, STRIDE):
                    xs.append(filtered[start:start+WINDOW].T)
                    ys.append(label)
                    domains.append(domain)
                    splits.append(split)
                    targets.append(target)
                    groups.append(gid)
                    starts.append(int(ca+start))
        records.append({'file': str(path), 'domain': domain, 'is_target': target,
                        'stored_receive_rate': stored_rate, 'samples': len(raw),
                        'timestamp_unique_fraction': len(np.unique(timestamps))/len(raw),
                        'channel_std': raw.std(0).tolist(),
                        'rail_fraction_0_3300': ((raw <= 0) | (raw >= 3300)).mean(0).tolist(),
                        'windows': len(ys)-n_before})
        print(f'Prepared {path.name}: {len(ys)-n_before} windows', flush=True)
    if not xs:
        total = sum(record['samples'] for record in records)
        raise DatasetPreparationError(
            f'所选记录共{total}点，但没有连续有效段达到{GUARD+WINDOW}点'
            f'（滤波预热{GUARD}点+窗口{WINDOW}点）。请完整采集每动作至少3次，'
            '每次保持5秒，并检查接收速率和长时间断流；不会将不足的数据强行随机切分。')
    arrays = dict(filtered=np.stack(xs).astype(np.int32 if frontend_config else np.int16), y=np.array(ys),
                  domain=np.array(domains), split=np.array(splits),
                  target=np.array(targets), group=np.array(groups), start=np.array(starts))
    train = arrays['split'] == 0
    # Validate coverage before training-only calibration, which also requires a
    # nonempty training partition. Never shrink the guard to manufacture data.
    for split in range(3):
        for target in ((True,) if field_recording is not None else (False, True)):
            mask = (arrays['split'] == split) & (arrays['target'] == target)
            present = np.bincount(arrays['y'][mask], minlength=num_classes)
            missing = [class_names[i] for i in range(num_classes) if present[i] == 0]
            if missing:
                split_name = ('训练', '验证', '测试')[split]
                raise DatasetPreparationError(
                    f'{split_name}集缺少有效动作：{"、".join(missing)}。'
                    f'需要方案中的{num_classes}类各至少3个完整重复，每段滤波预热后仍需128点；'
                    '请检查是否提前停止、漏选动作或采集期间断流。')
    # Training-only fixed channel scaling. Input unit is 1/32; no runtime division.
    magnitude = np.percentile(np.abs(arrays['filtered'][train].astype(np.int32)), 99.5, axis=(0, 2))
    shifts = np.maximum(0, np.ceil(np.log2(np.maximum(magnitude, 1)/112))).astype(int)
    arrays['input_shifts'] = shifts
    # Exact frontend quantization used for float training as well.
    q = np.stack([np.clip(round_shift(arrays['filtered'][:, c], int(shifts[c])), -127, 127)
                  for c in range(8)], axis=1).astype(np.int8)
    arrays['x'] = q.astype(np.float32)/32
    # Check half-open raw intervals don't overlap across partitions of a file.
    for file in signatures:
        segs = sorted([s for s in segments if s['file'] == file], key=lambda s: s['start'])
        for previous, current in zip(segs, segs[1:]):
            assert previous['stop'] <= current['start']
    np.savez_compressed(cache, **arrays)
    manifest = {'version': 2, 'field_only': field_recording is not None,
                'class_names': class_names, 'gesture_profile': gesture_profile,
                'sources_sha256': signatures, 'records': records,
                'segments': segments, 'window': WINDOW, 'stride': STRIDE, 'guard': GUARD,
                'coefficients_q14': frontend_config['coefficients_q14'] if frontend_config else COEFFICIENTS,
                'frontend': frontend_config, 'input_shifts': shifts.tolist(),
                'source_validation_P': sorted(validation_ids), 'source_test_P': sorted(test_ids),
                'target_split': 'Per-label complete repetitions modulo 3: train=0, val=1, test=2',
                'limitations': ['P numbers have no verified subject/placement meaning.',
                               'Nominal 1000 Hz; host timestamps cannot reconstruct lost sensor samples.',
                               'DGTV2 target test is a held-out repetition in one recording, not a new wearing.',
                               'Reset filter per run/chunk; discard 320 startup samples; no adjacent cross-split windows.']}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return arrays, manifest


def load_prepared(out):
    with np.load(out / 'dataset.npz') as z:
        arrays = {key: z[key] for key in z.files}
    return arrays, json.loads((out / 'data_manifest.json').read_text(encoding='utf-8'))

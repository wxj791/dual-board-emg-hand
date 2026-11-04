"""Inspect all recordings, including incomplete and archived files, read-only."""
from pathlib import Path
import json
import numpy as np
from .data import sha256


def audit(root, output):
    rows = []
    hashes = {}
    for path in sorted(Path(root).rglob('*.npz')):
        digest = sha256(path)
        with np.load(path, allow_pickle=True) as z:
            if 'emg' not in z or 'labels' not in z:
                rows.append({'file': str(path), 'excluded': 'No EMG or labels'})
                continue
            x, y = z['emg'], z['labels']
            labels, counts = np.unique(y, return_counts=True)
            rail = ((x <= 0) | (x >= 3300)).mean(0)
            std = x.std(0)
            rates = []
            if 'timestamps' in z:
                ts = z['timestamps']
                boundaries = np.r_[0, np.flatnonzero(y[1:] != y[:-1])+1, len(y)]
                for a, b in zip(boundaries[:-1], boundaries[1:]):
                    duration = ts[b-1]-ts[a]
                    if duration > .5:
                        rates.append((b-a)/duration)
            reasons = []
            if path.parent.name == 'OLD':
                reasons.append('Archived/incomplete: not pooled automatically')
            if 'abort' in path.stem.lower():
                reasons.append('Aborted recording')
            if not set(range(7)).issubset(set(labels.tolist())):
                reasons.append('Incomplete seven-class coverage')
            if max(rail) > .01:
                reasons.append('One or more channels have >1% rail samples')
            if min(std) < 5:
                reasons.append('One or more channels have std <5 mV')
            if digest in hashes:
                reasons.append('Byte-identical duplicate')
            rows.append({'file': str(path), 'sha256': digest, 'samples': len(x),
                         'labels': dict(zip(labels.tolist(), counts.tolist())),
                         'stored_receive_rate': float(z.get('sample_rate', 0)),
                         'within_label_receive_rate_min_median_max': np.percentile(rates, [0, 50, 100]).tolist() if rates else None,
                         'rate_note': 'Stored whole-recording rate can include omitted preparation phases; within-label values use host timestamps and do not prove a sensor clock.',
                         'std': std.tolist(), 'rail_fraction': rail.tolist(),
                         'findings': reasons, 'duplicate_of': hashes.get(digest)})
        hashes.setdefault(digest, str(path))
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Audited {len(rows)} recordings -> {output}', flush=True)
    return rows


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, default=Path('E:/FPGA/FPGA2026(5)/emg_ml/data/raw'))
    p.add_argument('--output', type=Path, default=Path('artifacts/pc_ml/data_audit.json'))
    a = p.parse_args()
    audit(a.root, a.output)

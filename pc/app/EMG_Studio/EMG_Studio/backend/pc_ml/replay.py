"""Replay recorded EMG through the exported FPGA numeric pipeline."""
from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path

import numpy as np

from .data import filter_exact, round_shift, GESTURES, StreamFilter
from .quantize import load_integer
from .frontend import create_filter
from .train import metrics


class Decision:
    """Reject low margin; three consecutive accepted classes before switching."""
    def __init__(self, margin=64, persistence=3):
        self.margin = margin
        self.persistence = persistence
        self.candidate = -1
        self.count = 0
        self.output = 0

    def step(self, logits):
        order = np.argsort(logits)
        best = int(order[-1])
        margin = int(logits[order[-1]]-logits[order[-2]])
        accepted = margin >= self.margin
        next_class = best if accepted else 0
        if self.candidate != next_class:
            self.candidate, self.count = next_class, 1
        else:
            self.count += 1
        if self.count >= self.persistence:
            self.output = next_class
        return best, margin, accepted, self.output


class StreamingPredictor:
    """Continuous, label-free PC preview of the integer inference chain."""
    def __init__(self, package):
        self.model, self.shifts, self.manifest = load_integer(package)
        self.reset()

    def reset(self):
        self.filter = create_filter(self.manifest.get('frontend'))
        self.decision = Decision(**self.manifest.get('decision', {}))
        self.pending = np.empty((0, 8), dtype=np.int16)
        self.count = 0

    def push(self, raw):
        filtered = self.filter.process(raw)
        self.pending = np.concatenate((self.pending, filtered), axis=0)
        ends = []
        windows = []
        # count records how many input samples have already been emitted/consumed.
        # Keep only the last 128-sample overlap plus not-yet-consumed samples.
        for index in range(len(filtered)):
            absolute = self.count+index+1
            if absolute >= 448 and (absolute-448) % 32 == 0:
                end = len(self.pending)-len(filtered)+index+1
                windows.append(self.pending[end-128:end].T)
                ends.append(absolute)
        self.count += len(filtered)
        self.pending = self.pending[-128:].copy()
        if not windows:
            return []
        q = np.stack(windows).astype(np.int64)
        for channel, shift in enumerate(self.shifts):
            q[:, channel] = np.clip(round_shift(q[:, channel], int(shift)), -127, 127)
        logits = self.model.predict_logits(q.astype(np.float32)/32)
        results = []
        for end, logit in zip(ends, logits):
            prediction, margin, accepted, stable = self.decision.step(logit)
            results.append({'sample_end': end, 'prediction': prediction, 'margin_q8': margin,
                            'accepted': accepted, 'stable': stable})
        return results


def replay(recording, package, output):
    descriptor=json.loads((Path(package)/'manifest.json').read_text(encoding='utf-8'))
    if descriptor.get('format')=='EMG-CNN-FUSION-PC-1':
        return replay_cnn_fusion(recording,package,output)
    if descriptor.get('metadata',{}).get('runtime')=='parallel_fusion_v1':
        return replay_fusion(recording,package,output)
    if descriptor['format']=='EMG-HYBRID-INT-1':
        return replay_hybrid(recording,package,output)
    integer, shifts, manifest = load_integer(package)
    with np.load(recording, allow_pickle=True) as z:
        raw, labels, ts = z['emg'], z['labels'], z['timestamps']
        recorded_names = [row[0] for _, row in sorted(z['gestures'].item().items())]
        recorded_profile = json.loads(str(z['gesture_profile'].item())) if 'gesture_profile' in z else None
    if recorded_names != manifest['class_names']:
        raise ValueError('记录与参数包的动作方案不同，不能按编号直接回放比较。')
    if manifest.get('gesture_profile') and recorded_profile:
        recorded_identity = [(c['name'], c['instruction'], c['key']) for c in recorded_profile['classes']]
        model_identity = [(c['name'], c['instruction'], c['key']) for c in manifest['gesture_profile']['classes']]
        if recorded_identity != model_identity:
            raise ValueError('记录与参数包的动作含义不同，请选择同一方案的数据。')
    num_classes = len(manifest['class_names'])
    boundaries = np.unique(np.r_[0, np.flatnonzero(labels[1:] != labels[:-1])+1,
                                 np.flatnonzero(np.diff(ts) > .25)+1, len(raw)])
    rows = []
    begin = time.perf_counter()
    for segment, (a, b) in enumerate(zip(boundaries[:-1], boundaries[1:])):
        if b-a < 448 or labels[a] < 0:
            continue
        # Same segmented frontend as training; recorded transition gaps unavailable.
        filtered = create_filter(manifest.get('frontend')).process(raw[a:b])
        q = np.stack([np.clip(round_shift(filtered[:, ch], int(shifts[ch])), -127, 127)
                      for ch in range(8)], 1)
        starts = list(range(320, len(q)-127, 32))
        if not starts:
            continue
        windows = np.stack([q[s:s+128].T for s in starts])
        logits = integer.predict_logits(windows.astype(np.float32)/32)
        decision = Decision(**manifest.get('decision', {}))
        for start, logit in zip(starts, logits):
            pred, margin, accepted, stable = decision.step(logit)
            rows.append({'segment': segment, 'sample_end': int(a+start+127),
                         'truth': int(labels[a+start+127]), 'prediction': pred,
                         'margin_q8': margin, 'accepted': accepted, 'stable': stable})
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with (output/'replay.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ['segment'])
        writer.writeheader()
        writer.writerows(rows)
    if not rows:
        raise ValueError('No complete stable windows in recording')
    y = np.array([r['truth'] for r in rows])
    pred = np.array([r['prediction'] for r in rows])
    stable = np.array([r['stable'] for r in rows])
    result = {'recording': str(recording), 'package': str(package),
              'class_names': manifest['class_names'],
              'raw_decision': metrics(y, pred, num_classes), 'stable_decision': metrics(y, stable, num_classes),
              'seconds': time.perf_counter()-begin,
              'limitation': 'Offline all-recording demonstration, includes training windows; use field_report held-out metrics for accuracy claims. Labels/gaps define segment resets; not a transition/online continuity test.'}
    (output/'replay.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(14, 4))
    ax.step(np.arange(len(rows)), y, where='post', label='Recorded gesture', linewidth=2, alpha=.65)
    ax.scatter(np.arange(len(rows)), stable, s=3, label='INT8 stable output', color='darkorange')
    ax.set(xlabel='Replay window (gaps omitted)', ylabel='Gesture ID', yticks=range(num_classes))
    ax.legend(loc='upper right')
    ax.grid(alpha=.2)
    fig.tight_layout()
    fig.savefig(output/'replay.png', dpi=160)
    plt.close(fig)
    return result


def replay_fusion(recording,package,output):
    from .fusion import StreamingFusionPredictor
    p=StreamingFusionPredictor(package)
    with np.load(recording,allow_pickle=True) as z:
        raw,y=z['emg'],z['labels']
        recorded=[r[0] for _,r in sorted(z['gestures'].item().items())]
        profile=json.loads(str(z['gesture_profile'].item())) if 'gesture_profile' in z else None
    if recorded!=p.meta['class_names']:raise ValueError('记录与融合参数包动作名称不同')
    if profile and p.meta.get('gesture_profile'):
        identity=lambda v:[(c['key'],c['name'],c['instruction']) for c in v['classes']]
        if identity(profile)!=identity(p.meta['gesture_profile']):raise ValueError('记录与融合参数包动作含义不同')
    rows=[]
    for i in range(0,len(raw),8192):rows.extend(p.push(raw[i:i+8192]))
    if not rows:raise ValueError('没有完整回放窗口')
    for row in rows:row['truth']=int(y[row['sample_end']-1])
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    with (output/'replay.csv').open('w',newline='',encoding='utf-8-sig') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    result={'recording':str(recording),'package':str(package),'class_names':recorded,'windows':len(rows),
        'continuous_no_label_resets':True,'limitation':'全记录回放包含训练片段和过渡窗口，不能作为独立准确率。'}
    (output/'replay.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,ax=plt.subplots(figsize=(14,4))
    ax.step([r['sample_end']/1000 for r in rows],[r['truth'] for r in rows],label='Recorded label',alpha=.6)
    ax.scatter([r['sample_end']/1000 for r in rows],[r['stable'] for r in rows],s=3,label='Fusion output')
    ax.set(xlabel='Stored samples / nominal 1000 Hz (gaps omitted)',ylabel='Gesture ID',yticks=range(len(recorded)))
    ax.legend();fig.tight_layout();fig.savefig(output/'replay.png',dpi=150);plt.close(fig)
    return result


def replay_hybrid(recording,package,output):
    from .today_live import StreamingFeaturePredictor
    p=StreamingFeaturePredictor(package)
    names=p.meta['class_names']
    with np.load(recording,allow_pickle=True) as z:
        raw,y,ts=z['emg'],z['labels'],z['timestamps']
        recorded=[r[0] for _,r in sorted(z['gestures'].item().items())]
    if names!=recorded:raise ValueError('记录与模型动作方案不一致')
    edges=np.unique(np.r_[0,np.flatnonzero(y[1:]!=y[:-1])+1,np.flatnonzero(np.diff(ts)>.25)+1,len(y)])
    rows=[]
    for group,(a,b) in enumerate(zip(edges[:-1],edges[1:])):
        if y[a]<0:continue
        p.reset()
        for row in p.push(raw[a:b]):
            rows.append({**row,'segment':group,'sample_end':int(a+row['sample_end']),
                         'truth':int(y[a+row['sample_end']-1])})
    if not rows:raise ValueError('没有完整回放窗口')
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    truth=np.array([r['truth'] for r in rows]);pred=np.array([r['prediction'] for r in rows]);stable=np.array([r['stable'] for r in rows])
    result={'recording':str(recording),'class_names':names,'raw_decision':metrics(truth,pred,len(names)),
            'stable_decision':metrics(truth,stable,len(names)),
            'limitation':'Includes training intervals; segmented replay is not independent or continuous live acceptance.'}
    (output/'replay.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    with (output/'replay.csv').open('w',newline='',encoding='utf-8-sig') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,ax=plt.subplots(figsize=(14,4));ax.step(np.arange(len(rows)),truth,label='Recorded gesture');ax.scatter(np.arange(len(rows)),stable,s=3,label='Unified integer output')
    ax.legend();ax.set(xlabel='Replay window (gaps omitted)',ylabel='Gesture ID',yticks=range(len(names)));fig.tight_layout();fig.savefig(output/'replay.png',dpi=150);plt.close(fig)
    return result


def replay_cnn_fusion(recording, package, output):
    from .cnn_fusion import StreamingCNNFusionPredictor
    from .cnn_fusion_field import prepare
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    p=StreamingCNNFusionPredictor(package)
    d,meta=prepare(Path(recording))
    if meta['class_names']!=p.manifest['class_names'] or meta.get('gesture_profile')!=p.manifest.get('gesture_profile'):
        raise ValueError('记录与CNN参数包的动作定义不同')
    with np.load(recording,allow_pickle=True) as z: raw=z['emg']
    rows=[]
    for i in range(0,len(raw),512):rows.extend(p.push(raw[i:i+512]))
    np.testing.assert_array_equal([r['sample_end'] for r in rows],d['ends'])
    valid=d['y']>=0
    stable=np.array([r['stable'] for r in rows]);truth=d['y']
    result={'replay_contains_training_samples':True,'independent_acceptance':False,
            'metrics':metrics(truth[valid],stable[valid],len(meta['class_names'])),
            'runtime':'cnn_fusion_clean256_v1','continuous_no_label_resets':True}
    (output/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,ax=plt.subplots(figsize=(14,4))
    ax.step(d['ends'][valid]/1000,truth[valid],label='Recorded label',alpha=.6)
    ax.scatter(d['ends'][valid]/1000,stable[valid],s=3,label='Statistics + CNN')
    ax.set(xlabel='Nominal sample time / s',ylabel='Gesture ID',yticks=range(len(meta['class_names'])))
    ax.legend();fig.tight_layout();fig.savefig(output/'replay.png',dpi=130);plt.close(fig)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--recording', required=True, type=Path)
    parser.add_argument('--package', type=Path, default=Path('artifacts/pc_ml/field/export'))
    parser.add_argument('--output', type=Path, default=Path('artifacts/pc_ml/field/replay'))
    args = parser.parse_args()
    __import__('torch').set_num_threads(4)
    print(json.dumps(replay(args.recording, args.package, args.output), ensure_ascii=False, indent=2))

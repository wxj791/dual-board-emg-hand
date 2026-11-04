"""Independent per-recording algorithm survey, restricted to 2026-10-28.

Whole acquired bouts are split before windows. No pretrained model, ensemble
across recordings, test-selected configuration, or unfiltered input is used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import time
import warnings

import numpy as np
from scipy import signal
from sklearn.discriminant_analysis import LinearDiscriminantAnalysis, QuadraticDiscriminantAnalysis
from sklearn.ensemble import ExtraTreesClassifier, RandomForestClassifier, HistGradientBoostingClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC
from sklearn.pipeline import make_pipeline
from sklearn.linear_model import LogisticRegression

from .frontend import PrecisionFilter, coefficients
from .train import metrics

DATE = '261028'
VERSION = 1


def dump(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                   default=lambda v: v.item() if isinstance(v, np.generic) else v.tolist()), encoding='utf-8')


def discover(root):
    rows = []
    seen = {}
    for path in sorted(root.rglob('*.npz')):
        name = path.name.lower()
        row = {'path': str(path), 'relative': str(path.relative_to(root)), 'bytes': path.stat().st_size}
        if DATE not in name and '20261028' not in name:
            row['excluded'] = 'Not 2026-10-28'
        elif 'abort' in name or 'checkpoint' in name:
            row['excluded'] = 'Aborted/checkpoint filename'
        elif row['bytes'] < 100*1024:
            row['excluded'] = 'Smaller than 100 KiB'
        else:
            with np.load(path, allow_pickle=True) as z:
                x, y, ts = z['emg'], z['labels'], z['timestamps']
                digest = hashlib.sha256(x.tobytes()+y.tobytes()+ts.tobytes()).hexdigest()
                row.update(content_sha256=digest, samples=len(x))
                if digest in seen:
                    row['excluded'] = 'Duplicate content: '+seen[digest]
                else:
                    seen[digest] = str(path)
        rows.append(row)
    return rows


def fixed_coefficients(front):
    if front != 'clean':
        return coefficients(front)
    # Wider rejection bands guard against 50 Hz and its first three harmonics.
    # Every biquad uses int16 Q14 coefficients and the same 24-bit state engine.
    sections = list(coefficients('emg')[:1])
    for frequency in (50, 100, 150, 200):
        b, a = signal.iirnotch(frequency, 12, fs=1000)
        sections.append(tuple(int(v) for v in np.rint(np.r_[b, a[1:]]*16384)))
    sections.append(coefficients('emg')[-1])
    return tuple(sections)


def subband_coefficients():
    sections = []
    for low, high in ((20, 90), (90, 200), (200, 450)):
        sos = signal.butter(1, (low, high), btype='bandpass', fs=1000, output='sos')
        sections.append(tuple(tuple(int(v) for v in np.rint(r[[0,1,2,4,5]]*16384)) for r in sos))
    return sections


def qlog2(values):
    """Leading-one plus 8-bit linear mantissa; no hardware logarithm required.

    Input nonnegative integer sums; output signed16 in 1/256 octave units.
    Python float is used only to locate the leading bit; the encoded result
    is reproduced by integer bit_length and shifts in the independent test.
    """
    v = np.maximum(1, np.asarray(values, dtype=np.int64))
    exponent = np.floor(np.log2(v)).astype(np.int64)
    base = np.left_shift(np.ones_like(exponent), exponent)
    # Floating log2 can round (2**n-1) up to n; enforce the integer interval.
    exponent -= v < base
    base = np.left_shift(np.ones_like(exponent), exponent)
    mantissa = np.left_shift(v-base, 8)//base
    return (exponent*256+mantissa).astype(np.int16)


def features(windows, subbands=None):
    """64 signed16 features: MAV, energy, WL, lag-difference and 3 band energies.

    The eighth group measures temporal envelope variability via half-window
    energy difference. All reductions are integer sums/abs/products; the
    power-of-two window length is stored in the package.
    """
    x = np.asarray(windows, dtype=np.int64)
    # Q8 state -> Q4 mV, nearest ties away from zero, to keep products compact.
    x = np.sign(x)*((abs(x)+8) >> 4)
    energy = (x*x).sum(-1)
    rows = [qlog2(abs(x).sum(-1)), qlog2(energy),
            qlog2(abs(np.diff(x, axis=-1)).sum(-1)),
            qlog2(abs(x[..., 4:]-x[..., :-4]).sum(-1))]
    half = x.shape[-1]//2
    rows.append(qlog2(abs((x[..., :half]**2).sum(-1)-(x[..., half:]**2).sum(-1))))
    for band in subbands:
        b = np.asarray(band, dtype=np.int64)
        b = np.sign(b)*((abs(b)+8) >> 4)
        rows.append(qlog2((b*b).sum(-1)))
    return np.concatenate(rows, axis=1).astype(np.int16)


def prepare_record(path, out):
    cache = out/'features.npz'
    manifest_path = out/'data.json'
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if cache.exists() and manifest_path.exists():
        m = json.loads(manifest_path.read_text(encoding='utf-8'))
        if m.get('version') == VERSION and m['sha256'] == digest:
            with np.load(cache) as z:
                return {k: z[k] for k in z.files}, m
    out.mkdir(parents=True, exist_ok=True)
    with np.load(path, allow_pickle=True) as z:
        raw, y, ts = z['emg'], z['labels'], z['timestamps']
        gestures = z['gestures'].item()
        profile = json.loads(str(z['gesture_profile'].item())) if 'gesture_profile' in z else None
    if raw.shape != (len(y), 8) or not np.isfinite(raw).all():
        raise ValueError('Invalid samples')
    names = [gestures[k][0] for k in sorted(gestures)]
    edges = np.unique(np.r_[0, np.flatnonzero(y[1:] != y[:-1])+1,
                            np.flatnonzero(np.diff(ts) > .25)+1, len(y)])
    # Detect host gaps BEFORE assigning repeats, including same-label bouts
    # separated by omitted rest/preparation. Never split one bout across folds.
    segments = []
    repetitions = np.zeros(len(names), dtype=int)
    arrays = {}
    powers = []
    saturation_counts = {front: 0 for front in ('precision', 'emg', 'clean')}
    for a, b in zip(edges[:-1], edges[1:]):
        label = int(y[a])
        if label < 0 or b-a < 320+256:
            continue
        if label >= len(names):
            raise ValueError('Out-of-range class ID')
        gid = len(segments)
        repetition = int(repetitions[label])
        repetitions[label] += 1
        segments.append({'id': gid, 'label': label, 'repetition': repetition,
                         'fold': repetition % 3, 'start': int(a), 'stop': int(b)})
        chunk = raw[a:b]
        f, p = signal.welch(chunk[320:], fs=1000, nperseg=min(1000,len(chunk)-320), axis=0)
        broadband = (f>=20)&(f<=450)
        lines = np.zeros(len(f), dtype=bool)
        for hz in (50,100,150,200):
            lines |= abs(f-hz)<=2
        powers.append({'group':gid, 'label':label,
                       'line_power_fraction':float(p[lines].sum()/max(p[broadband].sum(),1e-20)),
                       'non_line_power_per_channel':p[broadband & ~lines].sum(0).tolist(),
                       'rail_fraction':np.mean((chunk<=0)|(chunk>=3300),axis=0).tolist(),
                       'receive_rate':float((b-a-1)/max(ts[b-1]-ts[a],1e-9))})
        for front in ('precision', 'emg', 'clean'):
            filt = PrecisionFilter(fixed_coefficients(front))
            samples = filt.process(chunk)
            saturation_counts[front] += int(filt.saturations)
            # Filterbank operates on the already mains-rejected samples. Its
            # engine consumes Q8 directly; remove only its input adapter scale.
            bands = []
            for coeff in subband_coefficients():
                bands.append(filter_q8(samples, coeff))
            for width in (128, 256):
                starts = np.arange(320, len(chunk)-width+1, 32)
                if not len(starts):
                    continue
                windows = np.stack([samples[s:s+width].T for s in starts])
                band_windows = [np.stack([band[s:s+width].T for s in starts]) for band in bands]
                key = f'{front}_{width}'
                block = features(windows, band_windows)
                entry = arrays.setdefault(key, {'x':[], 'y':[], 'fold':[], 'group':[], 'start':[]})
                entry['x'].append(block)
                for name, value in [('y', label), ('fold', repetition%3), ('group',gid)]:
                    entry[name].append(np.full(len(starts),value,dtype=np.int32))
                entry['start'].append(starts+a)
    flat = {f'{key}_{field}':np.concatenate(values) for key, entry in arrays.items() for field,values in entry.items()}
    manifest = {'version':VERSION, 'path':str(path), 'sha256':digest, 'class_names':names,
                'gesture_profile':profile, 'repetitions':repetitions.tolist(), 'segments':segments,
                'guard':320, 'stride':32, 'sample_rate_nominal':1000, 'signal_quality':powers,
                'saturations':saturation_counts, 'frontends':{f:fixed_coefficients(f) for f in saturation_counts},
                'subband_coefficients_q14':subband_coefficients(),
                'filter_note':'All stages signed24 Q8, Q14 coefficients, INT48 MAC, ties away from zero.'}
    np.savez_compressed(cache, **flat)
    dump(manifest_path,manifest)
    return flat,manifest


class Q8Filter:
    """Stateful biquad bank; input already signed24 Q8, no ADC adapter."""
    def __init__(self,coeff):
        self.coeff=coeff
        self.state=np.zeros((len(coeff),8,4),dtype=np.int64)

    def process(self,samples):
        current = np.asarray(samples,dtype=np.int64)
        for section,(b0,b1,b2,a1,a2) in enumerate(self.coeff):
            result = np.empty_like(current)
            for ch in range(8):
                x1,x2,y1,y2=self.state[section,ch].tolist()
                vals=[]
                for x in current[:,ch].tolist():
                    acc=b0*x+b1*x1+b2*x2-a1*y1-a2*y2
                    value=(acc+8192)>>14 if acc>=0 else -((-acc+8192)>>14)
                    value=min(2**23-1,max(-2**23,value))
                    vals.append(value)
                    x2,x1,y2,y1=x1,x,y1,value
                self.state[section,ch]=x1,x2,y1,y2
                result[:,ch]=vals
            current=result
        return current.astype(np.int32)


def filter_q8(samples,coeff):
    return Q8Filter(coeff).process(samples)


def candidates():
    # Fixed before looking at today's held-out scores. Every record selects on
    # validation only. Hardware claims for floating classifiers require a later
    # quantized implementation; threshold forests already consume int16 data.
    return [
        ('lda', lambda:make_pipeline(StandardScaler(),LinearDiscriminantAnalysis(solver='lsqr',shrinkage='auto'))),
        ('qda', lambda:make_pipeline(StandardScaler(),QuadraticDiscriminantAnalysis(reg_param=.2))),
        ('linear', lambda:make_pipeline(StandardScaler(),LogisticRegression(C=.1,max_iter=500,class_weight='balanced'))),
        ('svm_c1', lambda:make_pipeline(StandardScaler(),SVC(C=1,gamma='scale',class_weight='balanced'))),
        ('svm_c10', lambda:make_pipeline(StandardScaler(),SVC(C=10,gamma='scale',class_weight='balanced'))),
        ('forest', lambda:RandomForestClassifier(n_estimators=24,max_depth=6,min_samples_leaf=3,max_features=.75,class_weight='balanced',random_state=42,n_jobs=4)),
        ('extra', lambda:ExtraTreesClassifier(n_estimators=32,max_depth=7,min_samples_leaf=2,max_features=.75,class_weight='balanced',random_state=42,n_jobs=4)),
        ('boost', lambda:HistGradientBoostingClassifier(max_iter=40,max_leaf_nodes=7,l2_regularization=2,early_stopping=False,random_state=42))]


def validation_score(m):
    # Safety first, without maximizing rest at the cost of refusing gestures.
    return (min(m['recall']),m['macro_f1'],m['accuracy'])


def run_record(path,out):
    data,manifest=prepare_record(path,out)
    classes=len(manifest['class_names'])
    all_rows=[]
    selected=[]
    failed_candidates=[]
    from joblib import dump as save_model
    for test_fold in range(3):
        train_fold=(test_fold+1)%3
        val_fold=(test_fold+2)%3
        fitted={}
        candidates_rows=[]
        for front in ('precision','emg','clean'):
            for width in (128,256):
                key=f'{front}_{width}'
                x,y,fold=[data[f'{key}_{k}'] for k in ('x','y','fold')]
                train,val,test=[fold==i for i in (train_fold,val_fold,test_fold)]
                if any(len(np.unique(y[m]))!=classes for m in (train,val,test)):
                    continue
                for name,factory in candidates():
                    begin=time.perf_counter()
                    model=factory()
                    try:
                        with warnings.catch_warnings():
                            warnings.simplefilter('ignore')
                            model.fit(x[train],y[train])
                    except (ValueError,np.linalg.LinAlgError) as exc:
                        failed_candidates.append({'front':front,'window':width,'algorithm':name,
                                                  'test_fold':test_fold,'failure':str(exc)})
                        continue
                    validation=metrics(y[val],model.predict(x[val]),classes)
                    row={'front':front,'window':width,'algorithm':name,'test_fold':test_fold,
                         'validation':validation,'fit_seconds':time.perf_counter()-begin}
                    candidates_rows.append(row)
                    fitted[(front,width,name)]=model
        if not candidates_rows:
            selected.append({'test_fold':test_fold,'unavailable':'Missing full class coverage in one or more acquired-bout partitions'})
            continue
        best=max(candidates_rows,key=lambda r:validation_score(r['validation']))
        # Refit on BOTH remaining complete repetitions after freezing the
        # hyperparameters; final held-out repetition is still untouched.
        selected_key=(best['front'],best['window'],best['algorithm'])
        for row in candidates_rows:
            key=f"{row['front']}_{row['window']}"
            x,y,fold=[data[f'{key}_{k}'] for k in ('x','y','fold')]
            model=next(factory for name,factory in candidates() if name==row['algorithm'])()
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                model.fit(x[fold!=test_fold],y[fold!=test_fold])
            pred=model.predict(x[fold==test_fold])
            row['test']=metrics(y[fold==test_fold],pred,classes)
            row['passed']=row['test']['accuracy']>=.95 and min(row['test']['recall'])>=.90 and row['test']['rest_false_activation']<=.01
            if (row['front'],row['window'],row['algorithm'])==selected_key:
                save_model(model,out/f'selected_fold{test_fold}.joblib')
                selected.append(dict(row))
            all_rows.append(row)
        print('SELECTED',path.name,'fold',test_fold,selected_key,
              'test',selected[-1]['test']['accuracy'],'min',min(selected[-1]['test']['recall']),flush=True)
        dump(out/'progress.json',{'selected':selected,'rows':all_rows})
    result={'path':str(path),'class_names':manifest['class_names'],'data_sha256':manifest['sha256'],
            'selected':selected,'candidates':all_rows,'failed_candidates':failed_candidates,
            'selection_rule':'Max validation minimum class recall, then macro F1, then accuracy; refit two folds, test third.',
            'whole_bout_split':True,'cross_recording_training':False,
            'passed_all_folds':len(selected)==3 and all(row.get('passed',False) for row in selected),
            'limitations':['Three outer folds share training data; exploratory repeated-bout estimates, not prospective online acceptance.',
                           'Candidate test scores are ablations; never select a model from test scores.',
                           'No transition labels retained; class0 rest-window false activation is not events per minute.',
                           'Subband/noise power is not proof of biological signal.']}
    dump(out/'report.json',result)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path('E:/FPGA/FPGA2026(5)/emg_ml/data/raw'))
    p.add_argument('--output',type=Path,default=Path('artifacts/today_survey'))
    p.add_argument('--prepare-only',action='store_true')
    p.add_argument('--resume',action='store_true')
    p.add_argument('--only',nargs='*')
    args=p.parse_args()
    import os
    # Keep sklearn OpenMP work bounded on the workstation.
    from threadpoolctl import threadpool_limits
    with threadpool_limits(limits=4):
        args.output.mkdir(parents=True,exist_ok=True)
        inventory=discover(args.root)
        dump(args.output/'inventory.json',inventory)
        results=[]
        for row in inventory:
            if 'excluded' in row:
                continue
            path=Path(row['path'])
            if args.only and path.stem not in args.only:
                continue
            out=args.output/path.stem
            print('RECORD',path,flush=True)
            if args.prepare_only:
                _,manifest=prepare_record(path,out)
                print('PREPARED',path.name,'repetitions',manifest['repetitions'],flush=True)
            elif args.resume and (out/'report.json').exists():
                results.append(json.loads((out/'report.json').read_text(encoding='utf-8')))
            else:
                results.append(run_record(path,out))
            dump(args.output/'reports.json',results)


if __name__=='__main__':
    main()

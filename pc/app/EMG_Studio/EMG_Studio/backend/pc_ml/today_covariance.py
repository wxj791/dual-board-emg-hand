"""Add integer cross-channel correlation features; no model/data fusion."""
import argparse
import json
from pathlib import Path

import numpy as np
from threadpoolctl import threadpool_limits
from sklearn.discriminant_analysis import LinearDiscriminantAnalysis,QuadraticDiscriminantAnalysis
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC

from .fixed_linear import FixedLinear
from .frontend import PrecisionFilter
from .today_survey import discover,dump,filter_q8,fixed_coefficients,subband_coefficients,qlog2,validation_score
from .train import metrics


def covariance_features(windows):
    x=np.asarray(windows,dtype=np.int64)
    x=np.sign(x)*((abs(x)+8)>>4)
    # Uncentred cross-energy after causal highpass: no label or future mean.
    covariance=x@x.swapaxes(1,2)
    energy=np.diagonal(covariance,axis1=1,axis2=2)
    log_energy=qlog2(energy).astype(np.int32)
    rows=[]
    for a in range(8):
        for b in range(a+1,8):
            xy=covariance[:,a,b]
            # Approximate signed correlation from fixed log codes. No inverse,
            # sqrt or floating correlation is used in inference.
            magnitude=2048+qlog2(abs(xy)).astype(np.int32)-((log_energy[:,a]+log_energy[:,b])>>1)
            rows.append(np.sign(xy)*np.clip(magnitude,0,2048))
    return np.stack(rows,axis=1).astype(np.int16)


def prepare(path,base,out):
    out.mkdir(parents=True,exist_ok=True)
    cache=out/'features.npz'
    if cache.exists():
        with np.load(cache) as z:return {k:z[k] for k in z.files}
    manifest=json.loads((base/'data.json').read_text(encoding='utf-8'))
    with np.load(base/'features.npz') as z:original={k:z[k] for k in z.files}
    with np.load(path,allow_pickle=True) as z:raw=z['emg']
    blocks={}
    for front in ('precision','emg','clean'):
        for segment in manifest['segments']:
            x=PrecisionFilter(fixed_coefficients(front)).process(raw[segment['start']:segment['stop']])
            sources=[x]+[filter_q8(x,c) for c in subband_coefficients()]
            for width in (128,256):
                starts=np.arange(320,len(x)-width+1,32)
                if not len(starts):continue
                cov=[]
                for source in sources:
                    windows=np.stack([source[s:s+width].T for s in starts])
                    cov.append(covariance_features(windows))
                blocks.setdefault(f'{front}_{width}',[]).append(np.concatenate(cov,axis=1))
    for key,values in blocks.items():
        cov=np.concatenate(values)
        assert len(cov)==len(original[f'{key}_x'])
        original[f'{key}_x']=np.concatenate((original[f'{key}_x'],cov),axis=1)
    np.savez_compressed(cache,**original)
    return original


def run_record(path,base,out):
    data=prepare(path,base,out)
    manifest=json.loads((base/'data.json').read_text(encoding='utf-8'))
    classes=len(manifest['class_names']);selections=[];all_rows=[]
    factories={'lda':lambda:make_pipeline(StandardScaler(),LinearDiscriminantAnalysis(solver='lsqr',shrinkage='auto')),
               'qda':lambda:make_pipeline(StandardScaler(),QuadraticDiscriminantAnalysis(solver='eigen',shrinkage=.2)),
               'svm':lambda:make_pipeline(StandardScaler(),SVC(C=10,gamma='scale',class_weight='balanced'))}
    for test_fold in range(3):
        rows=[]
        for front in ('precision','emg','clean'):
            for width in (128,256):
                key=f'{front}_{width}'
                x,y,fold=[data[f'{key}_{k}'] for k in ('x','y','fold')]
                train=fold==(test_fold+1)%3;val=fold==(test_fold+2)%3
                if any(len(np.unique(y[m]))!=classes for m in (train,val,fold==test_fold)):continue
                for name,factory in factories.items():
                    model=factory().fit(x[train],y[train])
                    m=metrics(y[val],model.predict(x[val]),classes)
                    rows.append({'front':front,'window':width,'algorithm':name,'test_fold':test_fold,'validation':m})
        if not rows:
            selections.append({'test_fold':test_fold,'unavailable':'Insufficient acquired-bout class coverage'});continue
        best=max(rows,key=lambda r:validation_score(r['validation']))
        key=f"{best['front']}_{best['window']}"
        x,y,fold=[data[f'{key}_{k}'] for k in ('x','y','fold')]
        model=factories[best['algorithm']]().fit(x[fold!=test_fold],y[fold!=test_fold])
        pred=model.predict(x[fold==test_fold])
        best['test']=metrics(y[fold==test_fold],pred,classes)
        best['passed']=best['test']['accuracy']>=.95 and min(best['test']['recall'])>=.90 and best['test']['rest_false_activation']<=.01
        if best['algorithm']=='lda':
            integer=FixedLinear().from_pipeline(model,x[fold!=test_fold])
            ip=integer.predict(x[fold==test_fold])
            best['test_integer']=metrics(y[fold==test_fold],ip,classes)
            best['integer_float_agreement']=float(np.mean(ip==pred))
            meta={'recording_sha256':manifest['sha256'],'class_names':manifest['class_names'],
                  'front':best['front'],'window':best['window'],'stride':32,
                  'coefficients_q14':manifest['frontends'][best['front']],
                  'subband_coefficients_q14':manifest['subband_coefficients_q14'],
                  'feature_format':'64 qlog features plus 4x28 signed log-correlation features',
                  'test_fold':test_fold}
            desc=integer.package(out/f'fold{test_fold}/export',meta)
            best['parameter_bytes']=desc['parameter_bytes']
        from joblib import dump as save
        save(model,out/f'fold{test_fold}.joblib')
        selections.append(dict(best));all_rows.extend(rows)
        print('COVARIANCE',path.name,test_fold,best['algorithm'],best['test']['accuracy'],min(best['test']['recall']),flush=True)
        dump(out/'progress.json',{'selected':selections,'validation_candidates':all_rows})
    result={'path':str(path),'class_names':manifest['class_names'],'feature_count':176,'selected':selections,
            'validation_candidates':all_rows,'cross_recording_training':False,
            'passed_all_folds':len(selections)==3 and all(r.get('passed',False) for r in selections),
            'selection_rule':'Validation minimum recall then macro F1; refit two acquired-bout folds, test third.'}
    dump(out/'report.json',result)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path('E:/FPGA/FPGA2026(5)/emg_ml/data/raw'))
    p.add_argument('--base',type=Path,default=Path('artifacts/today_survey'))
    p.add_argument('--output',type=Path,default=Path('artifacts/today_covariance'))
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=True);reports=[]
    with threadpool_limits(limits=2):
        for row in discover(args.root):
            if 'excluded' in row:continue
            path=Path(row['path']);out=args.output/path.stem
            if not (args.base/path.stem/'features.npz').exists():continue
            if (out/'report.json').exists():r=json.loads((out/'report.json').read_text(encoding='utf-8'))
            else:r=run_record(path,args.base/path.stem,out)
            reports.append(r);dump(args.output/'reports.json',reports)


if __name__=='__main__':main()

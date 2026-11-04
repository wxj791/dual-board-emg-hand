"""End-to-end integer LDA-score comparison, with validation-only selection."""
import argparse
import json
from pathlib import Path

import numpy as np
from threadpoolctl import threadpool_limits

from .fixed_linear import FixedLinear
from .today_survey import discover,dump,validation_score
from .train import metrics


def run_record(path,base,out):
    out.mkdir(parents=True,exist_ok=True)
    manifest=json.loads((base/'data.json').read_text(encoding='utf-8'))
    with np.load(base/'features.npz') as z: data={k:z[k] for k in z.files}
    classes=len(manifest['class_names'])
    selected=[];candidates=[]
    for test_fold in range(3):
        rows=[]
        for front in ('precision','emg','clean'):
            for width in (128,256):
                key=f'{front}_{width}'
                x,y,fold=[data[f'{key}_{k}'] for k in ('x','y','fold')]
                train=fold==(test_fold+1)%3;val=fold==(test_fold+2)%3
                if any(len(np.unique(y[m]))!=classes for m in (train,val,fold==test_fold)):continue
                model=FixedLinear().fit(x[train],y[train])
                rows.append({'front':front,'window':width,'validation_integer':metrics(y[val],model.predict(x[val]),classes),
                             'test_fold':test_fold})
        if not rows:
            selected.append({'test_fold':test_fold,'unavailable':'Missing full class repetitions'});continue
        best=max(rows,key=lambda r:validation_score(r['validation_integer']))
        key=f"{best['front']}_{best['window']}"
        x,y,fold=[data[f'{key}_{k}'] for k in ('x','y','fold')]
        model=FixedLinear().fit(x[fold!=test_fold],y[fold!=test_fold])
        pred=model.predict(x[fold==test_fold])
        best['test_integer']=metrics(y[fold==test_fold],pred,classes)
        best['passed']=best['test_integer']['accuracy']>=.95 and min(best['test_integer']['recall'])>=.90 and best['test_integer']['rest_false_activation']<=.01
        metadata={'recording_sha256':manifest['sha256'],'class_names':manifest['class_names'],
                  'front':best['front'],'window':best['window'],'stride':32,
                  'coefficients_q14':manifest['frontends'][best['front']],
                  'subband_coefficients_q14':manifest['subband_coefficients_q14'],
                  'feature_format':'64 INT16 qlog2 sums, group-major channel-minor','test_fold':test_fold}
        desc=model.package(out/f'fold{test_fold}/export',metadata)
        best['parameters']={k:desc[k] for k in ('parameter_bytes','score_bound')}
        reloaded,_=FixedLinear.load(out/f'fold{test_fold}/export')
        assert np.array_equal(pred,reloaded.predict(x[fold==test_fold]))
        fp=model.float_model_.predict(x[fold==test_fold])
        best['integer_float_agreement']=float(np.mean(fp==pred))
        np.savez_compressed(out/f'fold{test_fold}/golden.npz',features=x[fold==test_fold][:32],scores=model.scores(x[fold==test_fold][:32]),prediction=pred[:32])
        selected.append(dict(best));candidates.extend(rows)
        print('LINEAR',path.name,test_fold,best['test_integer']['accuracy'],min(best['test_integer']['recall']),flush=True)
    report={'path':str(path),'class_names':manifest['class_names'],'selected':selected,'validation_candidates':candidates,
            'cross_recording_training':False,'passed_all_folds':len(selected)==3 and all(v.get('passed',False) for v in selected),
            'selection_rule':'Validation integer minimum recall then macro F1; refit on two complete repetitions, test third.'}
    dump(out/'report.json',report)
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path('E:/FPGA/FPGA2026(5)/emg_ml/data/raw'))
    p.add_argument('--base',type=Path,default=Path('artifacts/today_survey'))
    p.add_argument('--output',type=Path,default=Path('artifacts/today_linear'))
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    reports=[]
    with threadpool_limits(limits=2):
        for row in discover(args.root):
            if 'excluded' in row:continue
            path=Path(row['path']);out=args.output/path.stem
            if not (args.base/path.stem/'features.npz').is_file():
                print('PENDING_PREPARATION',path.name,flush=True);continue
            if (out/'report.json').is_file(): report=json.loads((out/'report.json').read_text(encoding='utf-8'))
            else: report=run_record(path,args.base/path.stem,out)
            reports.append(report);dump(args.output/'reports.json',reports)


if __name__=='__main__':main()

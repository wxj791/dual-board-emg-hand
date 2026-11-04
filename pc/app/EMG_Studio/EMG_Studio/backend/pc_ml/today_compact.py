"""Validate compact integer RBF candidates on today's prepared recordings."""
import argparse
import json
from pathlib import Path

import numpy as np
from threadpoolctl import threadpool_limits

from .fixed_rbf import FixedRbfSVC
from .today_survey import discover,dump,validation_score
from .train import metrics


def run_record(path,base,out):
    out.mkdir(parents=True,exist_ok=True)
    manifest=json.loads((base/'data.json').read_text(encoding='utf-8'))
    with np.load(base/'features.npz') as z:
        data={k:z[k] for k in z.files}
    classes=len(manifest['class_names'])
    selections=[]
    all_rows=[]
    for test_fold in range(3):
        rows=[]
        for front in ('precision','emg','clean'):
            for width in (128,256):
                key=f'{front}_{width}'
                x,y,fold=[data[f'{key}_{k}'] for k in ('x','y','fold')]
                train=fold==(test_fold+1)%3
                val=fold==(test_fold+2)%3
                if any(len(np.unique(y[m]))!=classes for m in (train,val,fold==test_fold)):
                    continue
                for representatives,C in ((24,1),(24,10),(48,10)):
                    model=FixedRbfSVC(representatives=representatives,C=C).fit(x[train],y[train])
                    m=metrics(y[val],model.predict(x[val]),classes)
                    row={'front':front,'window':width,'representatives_per_class':representatives,'C':C,
                         'validation':m,'test_fold':test_fold}
                    rows.append(row)
        if not rows:
            selections.append({'test_fold':test_fold,'unavailable':'Insufficient complete class repetitions'})
            continue
        selected=max(rows,key=lambda r:validation_score(r['validation']))
        # Test ONLY the frozen winner. No test-based choice among compact models.
        key=f"{selected['front']}_{selected['window']}"
        x,y,fold=[data[f'{key}_{k}'] for k in ('x','y','fold')]
        model=FixedRbfSVC(representatives=selected['representatives_per_class'],C=selected['C']).fit(x[fold!=test_fold],y[fold!=test_fold])
        pred=model.predict(x[fold==test_fold])
        selected['test']=metrics(y[fold==test_fold],pred,classes)
        selected['passed']=selected['test']['accuracy']>=.95 and min(selected['test']['recall'])>=.90 and selected['test']['rest_false_activation']<=.01
        meta={'recording_sha256':manifest['sha256'],'class_names':manifest['class_names'],
              'front':selected['front'],'window':selected['window'],'stride':32,
              'coefficients_q14':manifest['frontends'][selected['front']],
              'subband_coefficients_q14':manifest['subband_coefficients_q14'],
              'feature_format':'64 INT16 qlog2 sums, group-major channel-minor',
              'test_fold':test_fold,'not_for_online_acceptance':True}
        package=model.package(out/f'fold{test_fold}/export',meta)
        selected['parameters']={k:package[k] for k in ('parameter_bytes','support_vectors','score_bound','distance_max')}
        loaded,_=FixedRbfSVC.load(out/f'fold{test_fold}/export')
        assert np.array_equal(loaded.predict(x[fold==test_fold]),pred)
        fp=model.float_model_.predict(model.normalize(x[fold==test_fold]).astype(float))
        selected['integer_float_agreement']=float(np.mean(fp==pred))
        np.savez_compressed(out/f'fold{test_fold}/golden.npz',features=x[fold==test_fold][:32],
                            normalized=model.normalize(x[fold==test_fold][:32]),
                            prediction=pred[:32])
        selections.append(dict(selected))
        all_rows.extend(rows)
        print('COMPACT',path.name,test_fold,'acc',selected['test']['accuracy'],
              'min',min(selected['test']['recall']),'bytes',package['parameter_bytes'],flush=True)
        dump(out/'progress.json',{'selected':selections,'validation_candidates':all_rows})
    result={'path':str(path),'class_names':manifest['class_names'],'selected':selections,
            'validation_candidates':all_rows,'cross_recording_training':False,
            'selection_rule':'Validation minimum class recall then F1; retrain two acquired-bout folds, test third.',
            'passed_all_folds':len(selections)==3 and all(r.get('passed',False) for r in selections),
            'scope':'Integer RBF classifier executed, exported, reloaded. Full FPGA pipeline not integrated.'}
    dump(out/'report.json',result)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path('E:/FPGA/FPGA2026(5)/emg_ml/data/raw'))
    p.add_argument('--base',type=Path,default=Path('artifacts/today_survey'))
    p.add_argument('--output',type=Path,default=Path('artifacts/today_compact'))
    p.add_argument('--only',nargs='*')
    args=p.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    reports=[]
    with threadpool_limits(limits=2):
        for row in discover(args.root):
            if 'excluded' in row:
                continue
            path=Path(row['path'])
            if args.only and path.stem not in args.only:
                continue
            if not (args.base/path.stem/'features.npz').is_file():
                print('PENDING_PREPARATION',path.name,flush=True)
                continue
            out=args.output/path.stem
            if (out/'report.json').is_file():
                result=json.loads((out/'report.json').read_text(encoding='utf-8'))
            else:
                result=run_record(path,args.base/path.stem,out)
            reports.append(result)
            dump(args.output/'reports.json',reports)


if __name__=='__main__':
    main()

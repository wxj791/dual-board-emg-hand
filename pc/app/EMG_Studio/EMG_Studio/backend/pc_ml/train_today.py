"""One today's recording -> independent integer model -> explicit acceptance.

No model mixing or automatic activation. A package can be previewed even when
it fails acceptance, but its status never claims the gestures are reliable.
"""
import argparse
import json
from pathlib import Path

import numpy as np

from .today_survey import prepare_record,dump,validation_score
from .today_linear import run_record as run_linear
from .today_compact import run_record as run_compact
from .fixed_linear import FixedLinear
from .fixed_rbf import FixedRbfSVC


def export_final(source,base,output,report,manifest,algorithm):
    """Select from validation only, fit all bouts, mark prospective status."""
    if algorithm=='hybrid':
        from .today_hybrid import export_final as export_hybrid
        return export_hybrid(source,base,output,report,manifest)
    rows=report['validation_candidates']
    grouped={}
    for row in rows:
        key=(row['front'],row['window'])
        if algorithm=='compact':key+=(row['representatives_per_class'],row['C'])
        grouped.setdefault(key,[]).append(row)
    # Use minimum validation recall across rotations; never test metrics.
    complete={key:items for key,items in grouped.items()
              if {r['test_fold'] for r in items}=={0,1,2}}
    if not complete:return None
    key=max(complete,key=lambda k:min(validation_score(r.get('validation_integer',r.get('validation')))
                                     for r in complete[k]))
    with np.load(base/'features.npz') as z:
        x,y=[z[f'{key[0]}_{key[1]}_{field}'] for field in ('x','y')]
    model=FixedLinear() if algorithm=='linear' else FixedRbfSVC(representatives=key[2],C=key[3])
    model.fit(x,y)
    metadata={'recording_sha256':manifest['sha256'],'class_names':manifest['class_names'],
              'front':key[0],'window':key[1],'stride':32,
              'coefficients_q14':manifest['frontends'][key[0]],
              'subband_coefficients_q14':manifest['subband_coefficients_q14'],
              'feature_format':'64 INT16 qlog2 sums, group-major channel-minor',
              'trained_all_bouts':True,'prospective_live_verified':False,
              'cross_validation_passed':report['passed_all_folds'],
              'selection_rule':'Maximum worst validation min-recall/F1/accuracy across rotations; no test scores.'}
    descriptor=model.package(output/'final_model',metadata)
    return {'path':str(output/'final_model'),'front':key[0],'window':key[1],
            'parameter_bytes':descriptor['parameter_bytes'],'trained_all_bouts':True,
            'prospective_live_verified':False}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--recording',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--algorithm',choices=['linear','compact','hybrid'],default='linear')
    args=p.parse_args()
    source=args.recording.resolve()
    if '261028' not in source.name and '20261028' not in source.name:
        p.error('本轮只使用2026-10-28记录，不能混入旧日期数据。')
    if source.stat().st_size<102400 or 'abort' in source.name.lower() or 'checkpoint' in source.name.lower():
        p.error('本轮排除中止记录和小于100KiB的文件。')
    args.output.mkdir(parents=True,exist_ok=True)
    base=args.output/'data'
    _,manifest=prepare_record(source,base)
    run=run_linear if args.algorithm=='linear' else run_compact
    if args.algorithm=='hybrid':
        from .today_hybrid import run_record as run_hybrid
        import torch
        torch.set_num_threads(2)
        run=lambda source,base,out:run_hybrid(source,base,out,epochs=12)
    from threadpoolctl import threadpool_limits
    with threadpool_limits(limits=4):
        report=run(source,base,args.output/'models')
        final=export_final(source,base,args.output,report,manifest,args.algorithm)
    rows=[]
    for selected in report['selected']:
        m=selected.get('test_integer',selected.get('test'))
        if m is None:
            rows.append({'fold':selected['test_fold'],'passed':False,'reason':'动作重复不足，不能独立验收'})
            continue
        weak=[{'name':manifest['class_names'][i],'recall':v} for i,v in enumerate(m['recall']) if v<.9]
        rows.append({'fold':selected['test_fold'],'passed':selected['passed'],
                     'accuracy':m['accuracy'],'weak_actions':weak,
                     'rest_false_activation':m['rest_false_activation']})
    acceptance={'passed_offline_all_repetitions':report['passed_all_folds'],
                'prospective_live_verified':False,'folds':rows,
                'final_model':final,
                'target':{'accuracy':.95,'each_action_recall':.9,'rest_window_false_activation':.01},
                'activation':'No automatic default-package, serial or actuator change.'}
    dump(args.output/'acceptance.json',acceptance)
    print(json.dumps(acceptance,ensure_ascii=False,indent=2))
    if not report['passed_all_folds']:
        print('未达标：查看weak_actions并补采这些动作及静息；不能以训练集高分代替验收。')


if __name__=='__main__':main()

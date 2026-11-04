"""Fixed clean/128 preset: one click, held-out acceptance, then all-data refit.

No architecture/frontend sweep in field use. Numba compiles the exact integer
filter loops; training and deployed arithmetic keep the existing Q8 contract.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time

import numpy as np
import torch
from threadpoolctl import threadpool_limits

from .hybrid import calibrate_hybrid,IntegerHybrid
from .today_hybrid import fit,infer
from .today_survey import fixed_coefficients,subband_coefficients,features,validation_score,dump,discover
from .train import metrics
from .replay import Decision

try:
    from numba import njit
except ImportError:
    def njit(**kwargs):return lambda f:f


@njit(cache=True)
def filterbank_q8(q8,coeff):
    states=np.zeros((9,8,4),dtype=np.int64)
    out=np.empty((len(q8),32),dtype=np.int32)
    for n in range(len(q8)):
        for ch in range(8):
            current=q8[n,ch]
            main=0
            for section in range(9):
                if section>=6:current=main
                s=states[section,ch]
                c=coeff[section]
                acc=c[0]*current+c[1]*s[0]+c[2]*s[1]-c[3]*s[2]-c[4]*s[3]
                value=(acc+8192)>>14 if acc>=0 else -((-acc+8192)>>14)
                value=min(8388607,max(-8388608,value))
                s[1]=s[0];s[0]=current;s[3]=s[2];s[2]=value
                current=value
                if section==5:
                    main=value;out[n,ch]=value
                elif section>=6:out[n,(section-5)*8+ch]=value
    return out


def prepare(source):
    with np.load(source,allow_pickle=True) as z:
        raw=np.asarray(z['emg']);labels=np.asarray(z['labels']);ts=np.asarray(z['timestamps'])
        gestures=z['gestures'].item()
        profile=json.loads(str(z['gesture_profile'].item())) if 'gesture_profile' in z else None
        if 'capture_complete' in z and not bool(z['capture_complete'].item()):raise ValueError('中止记录不能用于一键训练')
    if raw.shape!=(len(labels),8) or len(ts)!=len(raw) or not np.isfinite(raw).all():raise ValueError('原始数据应为有限的8通道记录')
    names=[row[0] for _,row in sorted(gestures.items())]
    if len(names)!=7:raise ValueError('当前FPGA固定七类：静息＋六个动作')
    coeff=np.asarray(list(fixed_coefficients('clean'))+[c[0] for c in subband_coefficients()],dtype=np.int64)
    edges=np.unique(np.r_[0,np.flatnonzero(labels[1:]!=labels[:-1])+1,np.flatnonzero(np.diff(ts)>.25)+1,len(labels)])
    bouts=[]
    for a,b in zip(edges[:-1],edges[1:]):
        label=int(labels[a])
        if label<0 or b-a<576:continue
        if label>=7:raise ValueError('动作编号越界')
        bouts.append({'start':int(a),'stop':int(b),'label':label})
    counts=np.bincount([s['label'] for s in bouts],minlength=7)
    if min(counts)<3:raise ValueError('每个动作及静息至少需要三个完整重复；请补采缺失动作')
    ordinal=np.zeros(7,dtype=int)
    xs=[];waves=[];ys=[];splits=[];groups=[];starts=[]
    for group,seg in enumerate(bouts):
        a,b,c=seg['start'],seg['stop'],seg['label']
        rep=int(ordinal[c]);ordinal[c]+=1
        # Chronological per-class complete bouts: first half train, next
        # quarter validate, last quarter test (3 reps -> 1/1/1).
        ntrain=max(1,int(counts[c])//2);nval=max(1,(int(counts[c])-ntrain)//2)
        split=0 if rep<ntrain else (1 if rep<ntrain+nval else 2)
        seg.update(repetition=rep,split=split)
        q8=(np.rint(raw[a:b]).clip(0,3300).astype(np.int64)-1650)*256
        signal=filterbank_q8(q8,coeff)
        st=np.arange(320,len(signal)-127,32)
        windows=[np.stack([signal[s:s+128,band*8:band*8+8].T for s in st]) for band in range(4)]
        xs.append(features(windows[0],windows[1:]));waves.append(windows[0])
        ys.append(np.full(len(st),c));splits.append(np.full(len(st),split));groups.append(np.full(len(st),group));starts.append(st+a)
    data={k:np.concatenate(v) for k,v in [('x',xs),('wave',waves),('y',ys),('split',splits),('group',groups),('start',starts)]}
    meta={'path':str(source),'sha256':hashlib.sha256(Path(source).read_bytes()).hexdigest(),
          'class_names':names,'gesture_profile':profile,'segments':bouts,'repetitions':counts.tolist(),
          'front':'clean','window':128,'stride':32,'coefficients_q14':fixed_coefficients('clean'),
          'subband_coefficients_q14':subband_coefficients(),'guard':320,
          'split_rule':'Chronological whole bouts per class: first half train, next quarter validation, last quarter test.'}
    return data,meta


def run(source,output,epochs=12,threads=2):
    source=Path(source);output=Path(output);output.mkdir(parents=True,exist_ok=True)
    if 'abort' in source.name.lower() or 'checkpoint' in source.name.lower() or source.stat().st_size<102400:
        raise ValueError('排除中止、checkpoint和小文件')
    torch.set_num_threads(threads)
    begin=time.perf_counter()
    print('固定FPGA配置：抗工频前端＋128点窗；自动训练和验收，无需调参。',flush=True)
    data,meta=prepare(source)
    prepared=time.perf_counter()
    dump(output/'data.json',meta)
    np.savez_compressed(output/'prepared.npz',**data)
    x,w,y,split=[data[k] for k in ('x','wave','y','split')]
    train,val,test=[split==i for i in range(3)]
    with threadpool_limits(limits=threads):
        model,integer,epoch,initial,nx=fit(w,x,y,train,val,epochs)
        current=metrics(y[val],integer.predict_logits(w[val],x[val]).argmax(1),7)
        initial_model=type(model)();initial_model.load_state_dict(initial);initial_model.eval()
        cal=nx[np.flatnonzero(train)[::max(1,int(train.sum())//512)]]
        baseline=calibrate_hybrid(initial_model,cal,integer.offset,integer.wave_shifts)
        base_metrics=metrics(y[val],baseline.predict_logits(w[val],x[val]).argmax(1),7)
        if validation_score(base_metrics)>=validation_score(current):integer=baseline;model=initial_model;epoch=0;current=base_metrics
        # Validation chooses the epoch; refit all non-test bouts before the
        # final held-out evaluation, just as the research evaluator does.
        model,integer,_,_,nx=fit(w,x,y,train|val,None,epoch)
        # Test is evaluated before fitting the final model to all bouts.
        held_logits=integer.predict_logits(w[test],x[test])
        held=metrics(y[test],held_logits.argmax(1),7)
        stable=stable_metrics(y[test],held_logits,data['group'][test])
        float_scores=infer(model,nx[test],((x[test].astype(np.int64)-integer.offset)/256).astype(np.float32))
        fp=metrics(y[test],float_scores.argmax(1),7)
        metadata={**meta,'best_epoch':epoch,'trained_all_bouts':False,'prospective_live_verified':False,
                  'acceptance_training_scope':'training plus validation bouts; last test bouts excluded'}
        integer.package(output/'validation_export',metadata)
        tested=time.perf_counter()
        print(f'独立测试 {held["accuracy"]:.2%}，最差动作 {min(held["recall"]):.2%}；现在用本记录全部数据生成最终模型。',flush=True)
        _,final,_,_,_=fit(w,x,y,np.ones(len(y),dtype=bool),None,epoch)
    passed=held['accuracy']>=.95 and min(held['recall'])>=.9 and held['rest_false_activation']<=.01
    metadata.update(trained_all_bouts=True,held_out_passed=passed,
                    acceptance_note='Held-out score belongs to validation_export, before this all-bout refit.')
    descriptor=final.package(output/'export',metadata)
    # GUI compatibility fields describe the actual model, not a different
    # topology. The canonical executable format remains EMG-HYBRID-INT-1.
    profile=meta['gesture_profile']
    descriptor.update(class_names=meta['class_names'],gesture_profile=profile,
                      action_mapping=[r['output'] for r in profile['classes']] if profile else [],
                      payload_bytes=descriptor['parameter_bytes'],decision={'margin':0,'persistence':3},
                      frontend={'name':'clean'},data_sources_sha256={str(source):meta['sha256']})
    dump(output/'export/manifest.json',descriptor)
    reloaded,_=IntegerHybrid.load(output/'export')
    np.testing.assert_array_equal(final.predict_logits(w[:4],x[:4]),reloaded.predict_logits(w[:4],x[:4]))
    np.savez_compressed(output/'export/golden.npz',wave=w[:4],features=x[:4],logits=final.predict_logits(w[:4],x[:4]))
    from scripts.hybrid_memory import export_memory
    export_memory(output/'export',output/'export/fpga')
    acceptance={'passed_offline':passed,'overall_target':.95,'per_class_recall_target':.9,
                'rest_window_false_activation_target':.01,
                'weak_classes':[meta['class_names'][c] for c,r in enumerate(held['recall']) if r<.9],
                'prospective_online_acceptance':False,'tested_package':'validation_export',
                'final_package':'export','final_trained_all_bouts':True}
    end=time.perf_counter()
    report={'recording':str(source),'preset':'hybrid_clean128_v2','class_names':meta['class_names'],
            'training':{'best_epoch':epoch,'manual_parameters_required':False,'refit_train_validation_before_test':True},
            'evaluation':{'validation':{'integer':current},'test':{'float':fp,'integer':held,'stable':stable}},
            'stable_metric_note':'Three-consecutive-window decision, reset at each held-out bout. Not continuous live acceptance.',
            'acceptance':acceptance,'parameters':descriptor,'final_trained_all_bouts':True,
            'timing_seconds':{'prepare':prepared-begin,'train_validate_test':tested-prepared,
                              'refit_export':end-tested,'total':end-begin},
            'limitations':['Repeated-bout offline estimate; new continuous capture required.',
                           'Final model uses all samples; its training score is not independent acceptance.']}
    dump(output/'field_report.json',report);dump(output/'acceptance.json',acceptance)
    print(json.dumps({'accuracy':held['accuracy'],'minimum_recall':min(held['recall']),
                      'rest_false_activation':held['rest_false_activation'],'passed':passed,
                      'weak_classes':acceptance['weak_classes'],'seconds':report['timing_seconds'],
                      'final_all_data':True,'bytes':descriptor['parameter_bytes']},ensure_ascii=False),flush=True)
    return report


def stable_metrics(y,logits,groups):
    predictions=[];decision=None;previous=None
    for truth,scores,group in zip(y,logits,groups):
        if decision is None or group!=previous:decision=Decision(margin=0,persistence=3)
        predictions.append(decision.step(scores)[3]);previous=group
    return metrics(y,np.asarray(predictions),7)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--recording',type=Path)
    p.add_argument('--output',type=Path,default=Path('artifacts/fast_field'))
    p.add_argument('--all-today',action='store_true')
    a=p.parse_args()
    try:
        if a.all_today:
            rows=[]
            for row in discover(Path('E:/FPGA/FPGA2026(5)/emg_ml/data/raw')):
                if 'excluded' in row:continue
                source=Path(row['path']);dest=a.output/source.stem
                rows.append(run(source,dest));dump(a.output/'reports.json',rows)
        elif a.recording:run(a.recording.resolve(),a.output.resolve())
        else:p.error('Specify --recording or --all-today')
    except (ValueError,FileNotFoundError) as exc:
        print('DATA_ERROR: '+str(exc),flush=True);raise SystemExit(2)


if __name__=='__main__':main()

"""One recording -> continuous fusion holdout -> all-bout field trial model."""
import argparse
import hashlib
import json
from pathlib import Path
import time

import joblib
import numpy as np
from threadpoolctl import threadpool_limits

from .fusion import extract,fit_fusion,package,load,FusionDecision,TITLE,MIN_CLASSES,MAX_CLASSES
from .train import metrics


def dump(path,value):Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')


def prepare(source, require_partitions=True):
    with np.load(source,allow_pickle=True) as z:
        raw=np.asarray(z['emg']);labels=np.asarray(z['labels']);ts=np.asarray(z['timestamps'])
        gestures=z['gestures'].item()
        profile=json.loads(str(z['gesture_profile'].item())) if 'gesture_profile' in z else None
        if 'capture_complete' in z and not bool(z['capture_complete'].item()):raise ValueError('中止记录不能用于一键训练')
        from .gesture_tasks_recording import stream_edges
        capture_edges=stream_edges(z,raw,labels,ts)
    if raw.shape!=(len(labels),8) or len(ts)!=len(raw) or not np.isfinite(raw).all() or not np.isfinite(ts).all():
        raise ValueError('需要有限的8通道原始数据、逐样本时间和标签')
    names=[r[0] for _,r in sorted(gestures.items())]
    k=len(names)
    if not MIN_CLASSES<=k<=MAX_CLASSES or sorted(gestures)!=list(range(k)):
        raise ValueError('融合训练支持2～32类（含静息），动作编号须从0连续排列')
    if profile and names!=[c['name'] for c in profile['classes']]:raise ValueError('文件动作方案与标签名称不一致')
    if profile and (profile['classes'][0]['key']!='rest' or [c['id'] for c in profile['classes']]!=list(range(k))):
        raise ValueError('动作方案必须以0号静息开始，类别编号连续')
    if len(set(names))!=k:raise ValueError('动作名称不能重复')
    if labels.ndim!=1 or not np.isfinite(labels).all() or np.any(labels!=np.floor(labels)) or np.any((labels< -1)|(labels>=k)):
        raise ValueError('标签须为-1（转换）或动作方案中的整数编号')
    edges=np.unique(np.r_[capture_edges,np.flatnonzero(labels[1:]!=labels[:-1])+1])
    segments=[]
    for a,b in zip(edges[:-1],edges[1:]):
        c=int(labels[a])
        if c<0 or b-a<576:continue
        if c>=k:raise ValueError('动作编号越界')
        segments.append({'start':int(a),'stop':int(b),'label':c})
    counts=np.bincount([s['label'] for s in segments],minlength=k)
    if require_partitions and min(counts)<3:
        missing='、'.join(f'{names[c]}（{counts[c]}次）' for c in range(k) if counts[c]<3)
        raise ValueError('每个动作及静息至少需要三个完整重复；不足：'+missing)
    ordinal=np.zeros(k,dtype=int)
    for seg in segments:
        c=seg['label'];rep=int(ordinal[c]);ordinal[c]+=1
        ntrain=max(1,int(counts[c])//2);nval=max(1,(int(counts[c])-ntrain)//2)
        seg.update(repetition=rep,split=0 if rep<ntrain else 1 if rep<ntrain+nval else 2)
    from .gesture_tasks_recording import extract_segments
    x,ends=extract_segments(raw,capture_edges);starts=ends-256
    y=np.full(len(x),-1,dtype=np.int64);split=y.copy();groups=y.copy()
    for g,seg in enumerate(segments):
        m=(starts>=seg['start']+320)&(ends<=seg['stop'])
        y[m]=seg['label'];split[m]=seg['split'];groups[m]=g
    for part in range(3) if require_partitions else ():
        absent=[names[c] for c in range(k) if not np.any((y==c)&(split==part))]
        if absent:raise ValueError('完整重复过短，隔离分区缺少有效窗口：'+'、'.join(absent)+'；请增加保持时间')
    data={'x':x,'y':y,'split':split,'group':groups,'start':starts}
    meta={'path':str(source),'sha256':hashlib.sha256(Path(source).read_bytes()).hexdigest(),
        'class_names':names,'gesture_profile':profile,'segments':segments,'repetitions':counts.tolist(),
        'continuous_filter_and_envelope':True,'no_label_state_resets':True,'capture_edges':capture_edges.tolist()}
    return data,meta


def smooth(scores):
    d=FusionDecision(scores.shape[1]);return np.asarray([d.step(s)[1] for s in scores])


def run(source,output):
    source=Path(source);output=Path(output);output.mkdir(parents=True,exist_ok=True)
    if any(w in source.name.lower() for w in ['abort','checkpoint']):raise ValueError('请选择完整采集文件')
    begin=time.perf_counter()
    print('并行融合：连续滤波、快慢包络、通道协同；本文件独立训练，固定规则。',flush=True)
    data,meta=prepare(source);dump(output/'data.json',meta);np.savez_compressed(output/'prepared.npz',**data)
    x,y,split,g=[data[k] for k in ['x','y','split','group']]
    k=len(meta['class_names'])
    fit=(split==0)|(split==1);test=split==2;valid=g>=0
    assert not set(g[fit])&set(g[test])
    with threadpool_limits(limits=2):
        model,head,scales=fit_fusion(x[fit],y[fit],g[fit])
        metadata={**meta,'trained_all_bouts':False,'calibration_margin_scales':scales,'prospective_live_verified':False}
        package(head,output/'validation_export',metadata)
        loaded,_=load(output/'validation_export');scores=loaded.scores(x)
        np.testing.assert_array_equal(scores,head.scores(x))
        raw=metrics(y[test],scores[test].argmax(1),k)
        stable=metrics(y[test],smooth(scores)[test],k)
        fp=metrics(y[test],model.predict(x[test]),k)
        np.savez_compressed(output/'heldout_predictions.npz',y=y,heldout=test,scores=scores,
                            prediction=scores.argmax(1),stable=smooth(scores),start=data['start'])
        passed=stable['accuracy']>=.95 and min(stable['recall'])>=.9 and stable['rest_false_activation']<=.01
        print(f'完整重复留出：原始{raw["accuracy"]:.2%}、平滑{stable["accuracy"]:.2%}；用全部本轮数据生成试用参数。',flush=True)
        final_model,final,scales=fit_fusion(x[valid],y[valid],g[valid])
        descriptor=package(final,output/'export',{**meta,'trained_all_bouts':True,
            'calibration_margin_scales':scales,'held_out_passed':passed,'prospective_live_verified':False,
            'acceptance_note':'留出分数属于validation_export；export使用本次全部完整段重训'})
        joblib.dump(final_model,output/'final_model.joblib')
        reloaded,_=load(output/'export');np.testing.assert_array_equal(reloaded.scores(x),final.scores(x))
        fit_metrics=metrics(y[valid],reloaded.predict(x[valid]),k)
    acceptance={'passed_offline':passed,'overall_target':.95,'per_class_recall_target':.9,
        'rest_window_false_activation_target':.01,'decision':'continuous_ewma_shift2_raw_rest',
        'weak_classes':[meta['class_names'][c] for c,v in enumerate(stable['recall']) if v<.9],
        'prospective_online_acceptance':False,'tested_package':'validation_export','final_package':'export'}
    report={'recording':str(source),'preset':'parallel_fusion_v1','model_title':TITLE,
        'class_names':meta['class_names'],'parameters':descriptor,
        'evaluation':{'test':{'float':fp,'integer':raw,'stable':stable}},
        'calibration_fit':fit_metrics,'acceptance':acceptance,'final_trained_all_bouts':True,
        'timing_seconds':{'total':time.perf_counter()-begin},
        'stable_metric_note':'与实时一致的连续EWMA；不按标签或段边界重置',
        'limitations':['留出完整段，但记录已用于研究；仍需新的实测数据。',
            '全数据参数包的训练自检不是实时准确率；过渡缺失影响延迟验收。',
            '电脑执行，完整融合算法尚未部署FPGA。']}
    dump(output/'field_report.json',report);dump(output/'acceptance.json',acceptance)
    print(json.dumps({'raw_accuracy':raw['accuracy'],'stable_accuracy':stable['accuracy'],
        'rest_false_activation':stable['rest_false_activation'],'passed':passed,
        'seconds':report['timing_seconds']['total'],'bytes':descriptor['parameter_bytes']},ensure_ascii=False),flush=True)
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--recording',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    try:run(a.recording.resolve(),a.output.resolve())
    except (ValueError,FileNotFoundError) as exc:print('DATA_ERROR: '+str(exc),flush=True);raise SystemExit(2)


if __name__=='__main__':main()

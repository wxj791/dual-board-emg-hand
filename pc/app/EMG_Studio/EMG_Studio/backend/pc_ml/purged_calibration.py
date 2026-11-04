"""Diagnose known-posture calibration with disjoint guarded time blocks.

This complements whole-bout testing; it does not replace new-bout/online tests.
"""
import json
from pathlib import Path

import numpy as np
import torch
from threadpoolctl import threadpool_limits

from .hybrid import IntegerHybrid
from .robust_linear import pipeline,integer_head
from .train import metrics


def run(directory):
    with np.load(directory/'prepared.npz') as z:d={k:z[k] for k in z.files}
    meta=json.loads((directory/'data.json').read_text(encoding='utf-8'))
    x,w,y,starts,groups=[d[k] for k in ['x','wave','y','start','group']]
    split=np.full(len(y),-1)
    for group,segment in enumerate(meta['segments']):
        length=segment['stop']-segment['start'];a=segment['start']+320
        cut1=a+int((length-320)*.5);cut2=a+int((length-320)*.75)
        belongs=groups==group
        first=belongs&(starts+128<=cut1)
        second=belongs&(starts>=cut1+128)&(starts+128<=cut2)
        last=belongs&(starts>=cut2+128)
        if not (first.any() and second.any() and last.any()):continue
        split[first]=0;split[second]=1;split[last]=2
        used=np.flatnonzero(belongs&(split>=0))
        for i in [0,1]:
            left=used[split[used]==i];right=used[split[used]==i+1]
            assert len(left) and len(right)
            assert starts[right].min()-(starts[left].max()+128)>=128
    train=split==0;val=split==1;test=split==2
    base,_=IntegerHybrid.load(directory/'export')
    results={}
    for config in [{'kind':'lda','shrinkage':'auto','uniform':False},{'kind':'logistic','C':100}]:
        fitted=pipeline(config).fit(x[train|val],y[train|val])
        model=integer_head(base,fitted,x[train|val])
        pred=model.predict_logits(w[test],x[test]).argmax(1)
        results[config['kind']]=metrics(y[test],pred,7)
    result={'recording':directory.name,'results':results,
            'windows':{'training':int(train.sum()),'validation':int(val.sum()),'test':int(test.sum()),'purged':int((split<0).sum())},
            'raw_guard_samples':128,'no_window_overlap_across_partitions':True,
            'meaning':'Within acquired bouts, every acquired posture represented in training. Complements whole-bout testing; not prospective online acceptance.'}
    out=Path('artifacts/purged_calibration')/directory.name;out.mkdir(parents=True,exist_ok=True)
    np.savez_compressed(out/'partition.npz',start=starts,group=groups,split=split)
    (out/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(directory.name,[(k,m['accuracy'],min(m['recall']),m['rest_false_activation']) for k,m in results.items()],flush=True)
    return result


if __name__=='__main__':
    torch.set_num_threads(2)
    with threadpool_limits(limits=2):
        rows=[run(p) for p in sorted(Path('artifacts/fast_field').iterdir()) if (p/'prepared.npz').exists()]
    Path('artifacts/purged_calibration/reports.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')

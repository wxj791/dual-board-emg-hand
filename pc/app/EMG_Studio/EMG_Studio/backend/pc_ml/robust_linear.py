"""Validation-only regularization study in the existing 64x7 hardware head."""
import argparse
import copy
import json
from pathlib import Path
import time

import numpy as np
from sklearn.discriminant_analysis import LinearDiscriminantAnalysis
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits
import torch

from .hybrid import IntegerHybrid,quantize_statistics
from .today_survey import validation_score
from .train import metrics


def pipeline(config):
    if config['kind']=='lda':
        classifier=LinearDiscriminantAnalysis(solver='lsqr',shrinkage=config['shrinkage'],
            priors=np.ones(7)/7 if config['uniform'] else None)
    else:classifier=LogisticRegression(C=config['C'],class_weight='balanced',max_iter=1200,tol=1e-5)
    return make_pipeline(StandardScaler(),classifier)


def integer_head(base,fitted,x):
    scaler,classifier=[p for _,p in fitted.steps]
    weights=classifier.coef_/scaler.scale_[None]
    offset=np.rint(x.mean(0)).astype(np.int16)
    bias=classifier.intercept_+weights@(offset.astype(float)-scaler.mean_)
    scale=max(1.,float(np.std((x-offset)@weights.T+bias)))
    layer=torch.nn.Linear(64,7)
    with torch.no_grad():
        layer.weight.copy_(torch.tensor(weights*256/scale,dtype=torch.float32))
        layer.bias.copy_(torch.tensor(bias/scale,dtype=torch.float32))
    layers=copy.deepcopy(base.layers)
    layers['statistics']=quantize_statistics(layer)
    layers['learned']['weights'][:]=0;layers['learned']['bias'][:]=0
    return IntegerHybrid(layers,offset,base.wave_shifts)


def run(directory,out):
    out.mkdir(parents=True,exist_ok=True);start=time.perf_counter()
    with np.load(directory/'prepared.npz') as z:d={k:z[k] for k in z.files}
    x,y,w,split=[d[k] for k in ['x','y','wave','split']]
    train,val,test=[split==i for i in range(3)]
    base,descriptor=IntegerHybrid.load(directory/'export')
    configs=[{'kind':'lda','shrinkage':s,'uniform':u} for s in ['auto',.001,.01,.03,.1,.3,.6,.9] for u in [False,True]]
    configs += [{'kind':'logistic','C':c} for c in [.001,.01,.1,1,10,100]]
    candidates=[]
    for config in configs:
        fitted=pipeline(config).fit(x[train],y[train])
        model=integer_head(base,fitted,x[train])
        m=metrics(y[val],model.predict_logits(w[val],x[val]).argmax(1),7)
        candidates.append({'configuration':config,'validation':m})
    winner=max(candidates,key=lambda row:validation_score(row['validation']))
    mask=train|val
    fitted=pipeline(winner['configuration']).fit(x[mask],y[mask])
    model=integer_head(base,fitted,x[mask])
    held=metrics(y[test],model.predict_logits(w[test],x[test]).argmax(1),7)
    model.package(out/'validation_export',{**descriptor['metadata'],'head_study':winner['configuration'],
        'trained_all_bouts':False,'exploratory_retest':True})
    fitted=pipeline(winner['configuration']).fit(x,y)
    final=integer_head(base,fitted,x)
    final.package(out/'export',{**descriptor['metadata'],'head_study':winner['configuration'],'trained_all_bouts':True})
    training=metrics(y,final.predict_logits(w,x).argmax(1),7)
    report={'recording':directory.name,'selected':winner,'candidates':candidates,'test':held,'all_data_training_fit':training,
        'seconds':time.perf_counter()-start,'topology_unchanged':True,
        'limitation':'Exploratory comparison on already inspected recordings; requires fresh capture for acceptance.'}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'recording':directory.name,'configuration':winner['configuration'],'validation':winner['validation']['accuracy'],
        'test':held['accuracy'],'worst':min(held['recall']),'rest':held['rest_false_activation'],
        'training_fit':training['accuracy'],'training_worst':min(training['recall'])},ensure_ascii=False),flush=True)
    return report


def main():
    p=argparse.ArgumentParser();p.add_argument('--all',action='store_true');args=p.parse_args()
    torch.set_num_threads(2)
    root=Path('artifacts/fast_field');out=Path('artifacts/robust_linear');out.mkdir(exist_ok=True)
    directories=sorted(p for p in root.iterdir() if (p/'prepared.npz').exists()) if args.all else [root/'DDW_P2_2610281200']
    with threadpool_limits(limits=2):
        reports=[run(d,out/d.name) for d in directories]
    (out/'reports.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()

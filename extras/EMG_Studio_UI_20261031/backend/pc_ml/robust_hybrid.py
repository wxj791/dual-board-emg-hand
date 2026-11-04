"""Train the same FPGA topology with stronger, consistent short-session fitting."""
import copy
import json
from pathlib import Path
import time

import numpy as np
import torch
from torch import nn
from threadpoolctl import threadpool_limits

from .hybrid import HybridNet,calibrate_hybrid
from .today_hybrid import infer
from .today_neural import normalize_wave
from .today_survey import validation_score
from .train import seed_all,metrics
from .robust_linear import pipeline


def fit(w,x,y,train,val,config,epochs=32):
    seed_all(42);model=HybridNet()
    offset=model.initialize_statistics(x[train],y[train])
    if config['uniform']:
        fitted=pipeline({'kind':'lda','shrinkage':'auto','uniform':True}).fit(x[train],y[train])
        scaler,lda=[p for _,p in fitted.steps]
        weights=lda.coef_/scaler.scale_[None]
        bias=lda.intercept_+weights@(offset.astype(float)-scaler.mean_)
        scale=max(1.,float(np.std((x[train]-offset)@weights.T+bias)))
        with torch.no_grad():
            model.statistics.weight.copy_(torch.tensor(weights*256/scale,dtype=torch.float32))
            model.statistics.bias.copy_(torch.tensor(bias/scale,dtype=torch.float32))
    nx,_,shifts=normalize_wave(w,train)
    fx=((x.astype(np.int64)-offset)/256).astype(np.float32)
    model.eval();best_state=copy.deepcopy(model.state_dict());best_epoch=0
    best=validation_score(metrics(y[val],infer(model,nx[val],fx[val]).argmax(1),7)) if val is not None else None
    optimizer=torch.optim.AdamW([
        {'params':model.statistics.parameters(),'lr':config['stat_lr'],'weight_decay':0},
        {'params':[p for name,p in model.named_parameters() if not name.startswith('statistics.')],
         'lr':.003,'weight_decay':.001}])
    scheduler=torch.optim.lr_scheduler.CosineAnnealingLR(optimizer,max(1,epochs),eta_min=.00005)
    ids_by_class=[np.flatnonzero(train&(y==c)) for c in range(7)]
    rng=np.random.default_rng(42);tw=torch.as_tensor(nx);tx=torch.as_tensor(fx);ty=torch.as_tensor(y,dtype=torch.long)
    for epoch in range(epochs):
        model.train();labels=rng.integers(0,7,2048);ids=np.array([rng.choice(ids_by_class[c]) for c in labels])
        for batch in np.array_split(ids,8):
            bw=tw[batch].clone();bx=tx[batch].clone()
            if config['gain']:
                log_gain=torch.randn(len(batch),8)*config['gain']
                bw=(bw*torch.exp(log_gain[:,:,None])).clamp(-127/32,127/32)
                # Both paths describe the same gain-augmented physical signal.
                # qlog units /256 are approximate log2 amplitude or energy.
                delta=log_gain/np.log(2)
                for group in range(8):bx[:,group*8:group*8+8]+=delta*(1 if group in (0,2,3) else 2)
            optimizer.zero_grad(set_to_none=True)
            loss=nn.functional.cross_entropy(model(bw,bx),ty[batch],label_smoothing=.01)
            loss=loss+.005*model.learned.weight.square().mean()
            loss.backward();nn.utils.clip_grad_norm_(model.parameters(),5);optimizer.step()
        scheduler.step()
        if val is not None:
            score=validation_score(metrics(y[val],infer(model,nx[val],fx[val]).argmax(1),7))
            if score>best:best=score;best_epoch=epoch+1;best_state=copy.deepcopy(model.state_dict())
    if val is not None:model.load_state_dict(best_state)
    else:best_epoch=epochs
    model.eval();cal=nx[np.flatnonzero(train)[::max(1,int(train.sum())//512)]]
    integer=calibrate_hybrid(model,cal,offset,shifts)
    return model,integer,best_epoch,nx


def main():
    torch.set_num_threads(2)
    source=Path('artifacts/fast_field/DDW_P2_2610281200')
    out=Path('artifacts/robust_hybrid/DDW_P2_2610281200');out.mkdir(parents=True,exist_ok=True)
    with np.load(source/'prepared.npz') as z:d={k:z[k] for k in z.files}
    meta=json.loads((source/'data.json').read_text(encoding='utf-8'))
    w,x,y,split=[d[k] for k in ['wave','x','y','split']];train,val,test=[split==i for i in range(3)]
    configs=[{'uniform':True,'stat_lr':.001,'gain':0},
             {'uniform':True,'stat_lr':.003,'gain':.08},
             {'uniform':True,'stat_lr':.0003,'gain':.15},
             {'uniform':False,'stat_lr':.003,'gain':.08}]
    rows=[];start=time.perf_counter()
    with threadpool_limits(limits=2):
        for config in configs:
            model,integer,epoch,_=fit(w,x,y,train,val,config)
            m=metrics(y[val],integer.predict_logits(w[val],x[val]).argmax(1),7)
            rows.append({'configuration':config,'epoch':epoch,'validation':m,'cnn_active':integer.cnn_active})
            print('ROBUST_VALIDATION',config,epoch,m['accuracy'],min(m['recall']),flush=True)
            (out/'progress.json').write_text(json.dumps(rows,indent=2),encoding='utf-8')
        winner=max(rows,key=lambda r:validation_score(r['validation']))
        config=winner['configuration'];epoch=winner['epoch']
        _,integer,_,_=fit(w,x,y,train|val,None,config,epoch)
        held=metrics(y[test],integer.predict_logits(w[test],x[test]).argmax(1),7)
        integer.package(out/'validation_export',{**meta,'best_epoch':epoch,'training_recipe':config,'trained_all_bouts':False,'exploratory_retest':True})
        _,final,_,_=fit(w,x,y,np.ones(len(y),dtype=bool),None,config,epoch)
        descriptor=final.package(out/'export',{**meta,'best_epoch':epoch,'training_recipe':config,'trained_all_bouts':True,'exploratory_retest':True})
        training=metrics(y,final.predict_logits(w,x).argmax(1),7)
        from scripts.hybrid_memory import export_memory
        export_memory(out/'export',out/'export/fpga')
    report={'recording':source.name,'selected':winner,'candidates':rows,'test':held,'all_data_training_fit':training,
            'parameter_bytes':descriptor['parameter_bytes'],'topology_unchanged':True,'seconds':time.perf_counter()-start,
            'limitation':'Exploratory study on a previously inspected recording; new online validation required.'}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False),flush=True)


if __name__=='__main__':main()

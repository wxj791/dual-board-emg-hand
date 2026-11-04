"""Independent INT8 neural candidates on today's recordings, whole-bout folds."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

import numpy as np
import torch
from torch import nn

from .data import round_shift
from .frontend import PrecisionFilter
from .models import make_model
from .quantize import calibrate,export,qlayer,execute,direct_reference,fraction
from .today_survey import discover,dump,validation_score,fixed_coefficients
from .train import seed_all,infer,metrics


class FeatureNet(nn.Module):
    def __init__(self,classes):
        super().__init__()
        self.layers=nn.Sequential(nn.Linear(64,64),nn.ReLU(),nn.Linear(64,32),nn.ReLU(),nn.Linear(32,classes))

    def forward(self,x):
        return self.layers(x)


def fit(model,x,y,vx,vy,classes,epochs=24,seed=42):
    seed_all(seed)
    opt=torch.optim.AdamW(model.parameters(),lr=.003,weight_decay=.001)
    sched=torch.optim.lr_scheduler.CosineAnnealingLR(opt,epochs,eta_min=.0002)
    perclass=[np.flatnonzero(y==i) for i in range(classes)]
    if any(not len(k) for k in perclass):
        raise ValueError('Missing class in training')
    rng=np.random.default_rng(seed)
    tx=torch.as_tensor(x);ty=torch.as_tensor(y,dtype=torch.long)
    best=(-1,-1,-1);best_state=None;best_epoch=1
    for epoch in range(epochs):
        model.train()
        labels=rng.integers(0,classes,2048)
        indices=np.array([rng.choice(perclass[k]) for k in labels])
        for ids in np.array_split(indices,8):
            bx=tx[ids]
            # Mild feature noise only; fixed subject/placement is the objective.
            if bx.ndim==2:
                bx=(bx+torch.randn_like(bx)*.025).clamp(-127/32,127/32)
            else:
                bx=(bx*torch.exp(torch.randn(len(bx),8,1)*.07)+torch.randn_like(bx)*.006).clamp(-127/32,127/32)
            opt.zero_grad(set_to_none=True)
            loss=nn.functional.cross_entropy(model(bx),ty[ids],label_smoothing=.02)
            loss.backward();torch.nn.utils.clip_grad_norm_(model.parameters(),5);opt.step()
        sched.step()
        if vx is None:
            continue
        m=metrics(vy,infer(model,vx).argmax(1),classes)
        score=validation_score(m)
        if score>best:
            best=score;best_epoch=epoch+1;best_state=copy.deepcopy(model.state_dict())
    if best_state is not None:
        model.load_state_dict(best_state)
    model.eval()
    return model,best_epoch


def normalize_features(x,mask):
    offset=np.rint(x[mask].mean(0)).astype(np.int16)
    # Shift-only feature normalization. One input unit corresponds to 1/32.
    magnitude=np.percentile(abs(x[mask].astype(np.int64)-offset),99.5,axis=0)
    shifts=np.ceil(np.log2(np.maximum(magnitude,1)/112)).astype(int)
    q=np.stack([np.clip(round_shift(x[:,i].astype(np.int64)-int(offset[i]),int(shifts[i])),-127,127)
                for i in range(64)],axis=1).astype(np.int8)
    return q.astype(np.float32)/32,offset,shifts


def normalize_wave(x,mask):
    magnitude=np.percentile(abs(x[mask].astype(np.int64)),99.5,axis=(0,2))
    shifts=np.ceil(np.log2(np.maximum(magnitude,1)/112)).astype(int)
    q=np.stack([np.clip(round_shift(x[:,i],int(shifts[i])),-127,127) for i in range(8)],axis=1).astype(np.int8)
    return q.astype(np.float32)/32,None,shifts


def calibrate_feature(model,calibration):
    fractions={}
    value=torch.as_tensor(calibration)
    with torch.no_grad():
        for index,layer in enumerate(model.layers):
            value=layer(value)
            if index in (1,3):
                fractions[index]=fraction(float(value.abs().max())*1.05)
    layers=[]
    for index,fi,fo in ((0,5,fractions[1]),(2,fractions[1],fractions[3]),(4,fractions[3],8)):
        mod=model.layers[index]
        layers.append(qlayer(f'fc{index}',mod.weight.detach().numpy(),mod.bias.detach().numpy(),fi,fo,
                             relu=index!=4,clamp=index!=4))
    return layers


def feature_predict(layers,x):
    value=np.rint(x*32).astype(np.int64)
    for layer in layers:
        value=execute(layer,value)
    return value


def export_feature(layers,offset,shifts,out,metadata):
    out.mkdir(parents=True,exist_ok=True)
    arrays={'offset':offset.astype('<i2'),'input_shifts':shifts.astype('<i2')}
    for i,layer in enumerate(layers):
        for name in ('weights','bias','shifts'):
            arrays[f'layer{i}_{name}']=layer[name]
    np.savez(out/'parameters.npz',**arrays)
    payload=bytearray();tensors=[]
    for name,v in arrays.items():
        v=np.asarray(v,dtype=v.dtype.newbyteorder('<'))
        tensors.append({'name':name,'dtype':v.dtype.str,'shape':list(v.shape),'offset':len(payload),'bytes':v.nbytes})
        payload.extend(v.tobytes())
    import hashlib
    (out/'parameters.bin').write_bytes(payload)
    descriptor={'format':'EMG-FEATURE-MLP-INT-1','tensors':tensors,'parameter_bytes':len(payload),
                'layers':[ {k:v for k,v in l.items() if k not in ('weights','bias','shifts')} for l in layers],
                'sha256':hashlib.sha256(payload).hexdigest(),'metadata':metadata,
                'scope':'Integer feature MLP; full FPGA integration not implemented'}
    dump(out/'manifest.json',descriptor)
    return descriptor


def waves(path,manifest,front):
    with np.load(path,allow_pickle=True) as z: raw=z['emg']
    arrays={128:[],256:[]};ys={128:[],256:[]};folds={128:[],256:[]}
    for segment in manifest['segments']:
        x=PrecisionFilter(fixed_coefficients(front)).process(raw[segment['start']:segment['stop']])
        for width in (128,256):
            starts=np.arange(320,len(x)-width+1,32)
            if not len(starts): continue
            arrays[width].append(np.stack([x[s:s+width].T for s in starts]))
            ys[width].extend([segment['label']]*len(starts))
            folds[width].extend([segment['fold']]*len(starts))
    return {w:(np.concatenate(arrays[w]),np.array(ys[w]),np.array(folds[w])) for w in arrays}


def run_record(path,base,out,epochs):
    out.mkdir(parents=True,exist_ok=True)
    manifest=json.loads((base/'data.json').read_text(encoding='utf-8'))
    with np.load(base/'features.npz') as z: data={k:z[k] for k in z.files}
    classes=len(manifest['class_names'])
    wave_cache={}
    candidates=[];selected=[]
    for test_fold in range(3):
        rows=[];fitted={}
        configurations=[(front,width,'features') for front in ('precision','emg','clean') for width in (128,256)]
        configurations += [('precision',128,'sequential'),('clean',128,'sequential'),('clean',256,'multiscale')]
        for front,width,kind in configurations:
            key=f'{front}_{width}'
            if kind=='features':
                x,y,fold=[data[f'{key}_{k}'] for k in ('x','y','fold')]
                normalizer=normalize_features
            else:
                if front not in wave_cache:
                    wave_cache[front]=waves(path,manifest,front)
                x,y,fold=wave_cache[front][width]
                normalizer=normalize_wave
            train=fold==(test_fold+1)%3;val=fold==(test_fold+2)%3
            if any(len(np.unique(y[m]))!=classes for m in (train,val,fold==test_fold)): continue
            nx,offset,shifts=normalizer(x,train)
            seed_all(42)
            model=FeatureNet(classes) if kind=='features' else make_model(kind,classes)
            model,best_epoch=fit(model,nx[train],y[train],nx[val],y[val],classes,epochs)
            cal=nx[np.flatnonzero(train)[::max(1,int(train.sum())//512)]]
            integer=calibrate_feature(model,cal) if kind=='features' else calibrate(model,kind,cal)
            vp=feature_predict(integer,nx[val]).argmax(1) if kind=='features' else integer.predict_logits(nx[val]).argmax(1)
            row={'front':front,'window':width,'kind':kind,'best_epoch':best_epoch,
                 'test_fold':test_fold,'validation_integer':metrics(y[val],vp,classes)}
            rows.append(row)
            fitted[(front,width,kind)]=(x,y,fold,normalizer)
            print('NEURAL_VAL',path.name,test_fold,front,width,kind,round(row['validation_integer']['accuracy'],4),flush=True)
        if not rows:
            selected.append({'test_fold':test_fold,'unavailable':'Incomplete class repetitions'})
            continue
        best=max(rows,key=lambda r:validation_score(r['validation_integer']))
        # Refit only selected architecture, on two complete acquired-bout folds.
        x,y,fold,normalizer=fitted[(best['front'],best['window'],best['kind'])]
        train=fold!=test_fold;test=fold==test_fold
        nx,offset,shifts=normalizer(x,train)
        seed_all(42)
        model=FeatureNet(classes) if best['kind']=='features' else make_model(best['kind'],classes)
        # Epoch count fixed by validation fit; no best-epoch selection on test.
        model,_=fit(model,nx[train],y[train],None,None,classes,best['best_epoch'])
        cal=nx[np.flatnonzero(train)[::max(1,int(train.sum())//1024)]]
        integer=calibrate_feature(model,cal) if best['kind']=='features' else calibrate(model,best['kind'],cal)
        logits=feature_predict(integer,nx[test]) if best['kind']=='features' else integer.predict_logits(nx[test])
        pred=logits.argmax(1)
        best['test_integer']=metrics(y[test],pred,classes)
        best['passed']=best['test_integer']['accuracy']>=.95 and min(best['test_integer']['recall'])>=.90 and best['test_integer']['rest_false_activation']<=.01
        meta={'recording_sha256':manifest['sha256'],'class_names':manifest['class_names'],
              'front':best['front'],'coefficients_q14':manifest['frontends'][best['front']],
              'window':best['window'],'stride':32,'test_fold':test_fold}
        dest=out/f'fold{test_fold}/export'
        if best['kind']=='features':
            descriptor=export_feature(integer,offset,shifts,dest,meta)
            best['parameter_bytes']=descriptor['parameter_bytes']
            value=np.rint(nx[test][:8]*32).astype(np.int64)
            for layer in integer:
                assert np.array_equal(execute(layer,value),direct_reference(layer,value))
                value=execute(layer,value)
        else:
            descriptor=export(integer,dest,shifts,'today_neural',manifest['class_names'])
            descriptor.update(window=best['window'],metadata=meta,
                              status='Experimental numeric package; frontend contract differs; not for existing workstation loader.')
            dump(dest/'manifest.json',descriptor)
            best['parameter_bytes']=descriptor['payload_bytes']
        torch.save({'state_dict':model.state_dict(),'candidate':best},out/f'fold{test_fold}/model.pth')
        selected.append(dict(best));candidates.extend(rows)
        dump(out/'progress.json',{'selected':selected,'candidates':candidates})
        print('NEURAL_TEST',path.name,test_fold,best['kind'],best['test_integer']['accuracy'],min(best['test_integer']['recall']),flush=True)
    result={'path':str(path),'class_names':manifest['class_names'],'selected':selected,'candidates':candidates,
            'cross_recording_training':False,'passed_all_folds':len(selected)==3 and all(r.get('passed',False) for r in selected),
            'selection_rule':'Validation integer minimum class recall then F1; refit selected architecture for validation-selected epochs on two folds.'}
    dump(out/'report.json',result)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path('E:/FPGA/FPGA2026(5)/emg_ml/data/raw'))
    p.add_argument('--base',type=Path,default=Path('artifacts/today_survey'))
    p.add_argument('--output',type=Path,default=Path('artifacts/today_neural'))
    p.add_argument('--epochs',type=int,default=18)
    args=p.parse_args()
    torch.set_num_threads(2)
    args.output.mkdir(parents=True,exist_ok=True)
    reports=[]
    for row in discover(args.root):
        if 'excluded' in row: continue
        path=Path(row['path']);out=args.output/path.stem
        if not (args.base/path.stem/'features.npz').is_file():
            print('PENDING_PREPARATION',path.name,flush=True);continue
        if (out/'report.json').is_file():
            result=json.loads((out/'report.json').read_text(encoding='utf-8'))
        else:
            result=run_record(path,args.base/path.stem,out,args.epochs)
        reports.append(result)
        dump(args.output/'reports.json',reports)


if __name__=='__main__': main()

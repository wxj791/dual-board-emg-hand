"""Today's independent, whole-bout evaluation of one statistics/CNN model."""
import argparse
import copy
import json
from pathlib import Path
import time

import numpy as np
import torch
from torch import nn
from threadpoolctl import threadpool_limits

from .hybrid import HybridNet,IntegerHybrid,calibrate_hybrid
from .quantize import execute,direct_reference
from .today_neural import waves,normalize_wave
from .today_survey import discover,dump,validation_score
from .train import seed_all,metrics


@torch.no_grad()
def infer(model,wave,statistics):
    model.eval()
    return np.concatenate([model(torch.as_tensor(wave[i:i+256]),torch.as_tensor(statistics[i:i+256])).numpy()
                           for i in range(0,len(wave),256)])


def fit(wave,features,y,train,val,epochs=12,seed=42):
    seed_all(seed)
    model=HybridNet(len(np.unique(y)))
    offset=model.initialize_statistics(features[train],y[train])
    nx,_,shifts=normalize_wave(wave,train)
    fx=((features.astype(np.int64)-offset)/256).astype(np.float32)
    initial=copy.deepcopy(model.state_dict())
    best_state=initial;best_epoch=0
    best=validation_score(metrics(y[val],infer(model,nx[val],fx[val]).argmax(1),7)) if val is not None else None
    head=[model.statistics.weight,model.statistics.bias]
    convolution=[p for name,p in model.named_parameters() if not name.startswith('statistics.')]
    opt=torch.optim.AdamW([{'params':head,'lr':.0001,'weight_decay':0},
                           {'params':convolution,'lr':.002,'weight_decay':.001}])
    scheduler=torch.optim.lr_scheduler.CosineAnnealingLR(opt,max(1,epochs),eta_min=.00005)
    perclass=[np.flatnonzero(train&(y==c)) for c in range(7)]
    if any(not len(ids) for ids in perclass):raise ValueError('Missing training class')
    rng=np.random.default_rng(seed)
    tx,tf,ty=torch.as_tensor(nx),torch.as_tensor(fx),torch.as_tensor(y,dtype=torch.long)
    for epoch in range(epochs):
        model.train()
        labels=rng.integers(0,7,1536)
        ids=np.array([rng.choice(perclass[c]) for c in labels])
        for batch in np.array_split(ids,6):
            # Both paths retain the same physical window; no mismatched gain augmentation.
            opt.zero_grad(set_to_none=True)
            loss=nn.functional.cross_entropy(model(tx[batch],tf[batch]),ty[batch],label_smoothing=.01)
            loss=loss+.01*model.learned.weight.square().mean()
            loss.backward();nn.utils.clip_grad_norm_(model.parameters(),3);opt.step()
        scheduler.step()
        if val is not None:
            score=validation_score(metrics(y[val],infer(model,nx[val],fx[val]).argmax(1),7))
            if score>best:best=score;best_epoch=epoch+1;best_state=copy.deepcopy(model.state_dict())
    if val is None:best_epoch=epochs
    else:model.load_state_dict(best_state)
    model.eval()
    cal=nx[np.flatnonzero(train)[::max(1,int(train.sum())//512)]]
    integer=calibrate_hybrid(model,cal,offset,shifts)
    return model,integer,best_epoch,initial,nx


def run_record(source,base,out,epochs):
    out.mkdir(parents=True,exist_ok=True)
    meta=json.loads((base/'data.json').read_text(encoding='utf-8'))
    with np.load(base/'features.npz') as z:data={k:z[k] for k in z.files}
    wave_cache={}
    # Fixed before evaluating this experiment: 128ms fronts + one 256ms clean front.
    configurations=[('precision',128),('emg',128),('clean',128),('clean',256)]
    candidates=[];selected=[]
    for fold in range(3):
        rows=[]
        for front,width in configurations:
            if front not in wave_cache:wave_cache[front]=waves(source,meta,front)
            wave,y,folds=wave_cache[front][width]
            key=f'{front}_{width}'
            assert np.array_equal(y,data[f'{key}_y']) and np.array_equal(folds,data[f'{key}_fold'])
            features=data[f'{key}_x']
            train=folds==(fold+1)%3;val=folds==(fold+2)%3
            begin=time.perf_counter()
            model,integer,epoch,initial,nx=fit(wave,features,y,train,val,epochs)
            learned=metrics(y[val],integer.predict_logits(wave[val],features[val]).argmax(1),7)
            model.load_state_dict(initial);model.eval()
            cal=nx[np.flatnonzero(train)[::max(1,int(train.sum())//512)]]
            baseline=calibrate_hybrid(model,cal,integer.offset,integer.wave_shifts)
            initial_score=metrics(y[val],baseline.predict_logits(wave[val],features[val]).argmax(1),7)
            # Epoch-zero is an explicit validation candidate, never chosen on test.
            if validation_score(initial_score)>=validation_score(learned):
                epoch=0;validation=initial_score
            else:validation=learned
            row={'front':front,'window':width,'test_fold':fold,'best_epoch':epoch,
                 'validation_integer':validation,'epoch_zero_validation':initial_score,
                 'trained_validation':learned,'fit_seconds':time.perf_counter()-begin}
            rows.append(row)
            print('HYBRID_VAL',source.name,fold,front,width,epoch,round(validation['accuracy'],4),flush=True)
        winner=max(rows,key=lambda r:validation_score(r['validation_integer']))
        front,width=winner['front'],winner['window']
        wave,y,folds=wave_cache[front][width];features=data[f'{front}_{width}_x']
        train=folds!=fold;test=folds==fold
        model,integer,_,_,_=fit(wave,features,y,train,None,winner['best_epoch'])
        prediction=integer.predict_logits(wave[test],features[test]).argmax(1)
        m=metrics(y[test],prediction,7)
        winner['test_integer']=m
        winner['passed']=m['accuracy']>=.95 and min(m['recall'])>=.9 and m['rest_false_activation']<=.01
        dest=out/f'fold{fold}/export'
        metadata={'recording_sha256':meta['sha256'],'class_names':meta['class_names'],
                  'front':front,'window':width,'stride':32,'test_fold':fold,
                  'coefficients_q14':meta['frontends'][front],
                  'subband_coefficients_q14':meta['subband_coefficients_q14'],
                  'best_epoch':winner['best_epoch'],'prospective_live_verified':False,
                  'feature_format':'64 INT16 qlog features + 32 learned temporal features; one affine head.'}
        descriptor=integer.package(dest,metadata)
        winner['parameter_bytes']=descriptor['parameter_bytes']
        loaded,_=IntegerHybrid.load(dest)
        assert np.array_equal(prediction,loaded.predict_logits(wave[test],features[test]).argmax(1))
        q=integer.normalize_wave(wave[test][:1]);trace=integer.trace(q,features[test][:1])
        for name,inp in [('stem','input'),('conv2','pool1'),('conv3','pool2'),('learned','embedding')]:
            assert np.array_equal(execute(integer.layers[name],trace[inp]),direct_reference(integer.layers[name],trace[inp]))
        centered=trace['centered'];layer=integer.layers['statistics']
        from .data import round_shift
        score=np.array([[round_shift(sum(int(a)*int(b) for a,b in zip(centered[0],w))+int(bias),int(s))
                         for w,bias,s in zip(layer['weights'],layer['bias'],layer['shifts'])]])
        assert np.array_equal(score,trace['statistics'])
        np.savez_compressed(dest/'golden.npz',wave=wave[test][:4],features=features[test][:4],
                            logits=integer.predict_logits(wave[test][:4],features[test][:4]))
        torch.save(model.state_dict(),out/f'fold{fold}/model.pth')
        selected.append(dict(winner));candidates.extend(rows)
        dump(out/'progress.json',{'selected':selected,'candidates':candidates})
        print('HYBRID_TEST',source.name,fold,winner['best_epoch'],m['accuracy'],min(m['recall']),flush=True)
    report={'path':str(source),'class_names':meta['class_names'],'data_sha256':meta['sha256'],
            'selected':selected,'candidates':candidates,'cross_recording_training':False,
            'passed_all_folds':all(r['passed'] for r in selected),
            'selection_rule':'Validation integer min recall/F1/accuracy; epoch-zero allowed; refit other two folds.',
            'limitations':['Exploratory same-day offline study; requires new prospective recordings.',
                           'CNN contributes no correction when validation selects epoch zero.',
                           'Full FPGA inference pipeline not yet implemented.']}
    dump(out/'report.json',report)
    return report


def export_final(source,base,out,report,meta):
    """Freeze configuration from validation, train all bouts, require new test."""
    grouped={}
    for row in report['candidates']:
        grouped.setdefault((row['front'],row['window']),[]).append(row)
    complete={k:v for k,v in grouped.items() if {r['test_fold'] for r in v}=={0,1,2}}
    if not complete:return None
    front,width=max(complete,key=lambda k:min(validation_score(r['validation_integer']) for r in complete[k]))
    epochs=int(np.median([r['best_epoch'] for r in complete[(front,width)]]))
    wave,y,folds=waves(source,meta,front)[width]
    with np.load(base/'features.npz') as z:features=z[f'{front}_{width}_x']
    model,integer,_,_,_=fit(wave,features,y,np.ones(len(y),dtype=bool),None,epochs)
    metadata={'recording_sha256':meta['sha256'],'class_names':meta['class_names'],
              'front':front,'window':width,'stride':32,'trained_all_bouts':True,
              'coefficients_q14':meta['frontends'][front],
              'subband_coefficients_q14':meta['subband_coefficients_q14'],
              'best_epoch':epochs,'prospective_live_verified':False,
              'cross_validation_passed':report['passed_all_folds'],
              'selection_rule':'Worst validation min recall/F1/accuracy; median validation epoch; no test scores.'}
    descriptor=integer.package(out/'final_model',metadata)
    return {'path':str(out/'final_model'),'front':front,'window':width,'epochs':epochs,
            'cnn_correction_active':integer.cnn_active,
            'parameter_bytes':descriptor['parameter_bytes'],'trained_all_bouts':True,
            'prospective_live_verified':False}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path('E:/FPGA/FPGA2026(5)/emg_ml/data/raw'))
    p.add_argument('--base',type=Path,default=Path('artifacts/today_survey'))
    p.add_argument('--output',type=Path,default=Path('artifacts/today_hybrid'))
    p.add_argument('--epochs',type=int,default=12)
    p.add_argument('--only',nargs='*')
    p.add_argument('--export-final',action='store_true',help='Also refit/export one final model using all bouts, without new accuracy claims.')
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    torch.set_num_threads(2)
    reports=[]
    with threadpool_limits(limits=2):
        for row in discover(args.root):
            if 'excluded' in row:continue
            source=Path(row['path'])
            if args.only and source.stem not in args.only:continue
            out=args.output/source.stem
            if (out/'report.json').exists():report=json.loads((out/'report.json').read_text(encoding='utf-8'))
            else:report=run_record(source,args.base/source.stem,out,args.epochs)
            if args.export_final:
                meta=json.loads((args.base/source.stem/'data.json').read_text(encoding='utf-8'))
                final=export_final(source,args.base/source.stem,out,report,meta)
                dump(out/'acceptance.json',{'passed_offline_all_repetitions':report['passed_all_folds'],
                                          'prospective_live_verified':False,'final_model':final,
                                          'target':{'accuracy':.95,'each_action_recall':.9,'rest_window_false_activation':.01}})
            reports.append(report);dump(args.output/'reports.json',reports)


if __name__=='__main__':main()

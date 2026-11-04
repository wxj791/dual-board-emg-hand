"""One statistics-initialized CNN, mixed INT16/INT8 affine output head.

The two affine terms are the partitioned implementation of one concatenated
feature head. There is one argmax, no voting between independently selected
models. Learned convolution corrections start at zero for short collections.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import torch
from torch import nn

from .data import round_shift
from .fixed_linear import FixedLinear
from .models import CBR
from .quantize import execute,fold,fraction,gap,pool,qlayer


class HybridNet(nn.Module):
    def __init__(self,classes=7):
        super().__init__()
        self.stem=CBR(8,16,5)
        self.conv2=CBR(16,24,3)
        self.conv3=CBR(24,32,3)
        self.statistics=nn.Linear(64,classes)
        self.learned=nn.Linear(32,classes,bias=False)
        nn.init.zeros_(self.learned.weight)

    def embedding(self,wave):
        x=nn.functional.max_pool1d(self.stem(wave),2)
        x=nn.functional.max_pool1d(self.conv2(x),2)
        return self.conv3(x).mean(-1)

    def forward(self,wave,statistics):
        return self.statistics(statistics)+self.learned(self.embedding(wave))

    def initialize_statistics(self,x,y):
        baseline=FixedLinear().fit(x,y)
        scaler,lda=[v for _,v in baseline.float_model_.steps]
        weights=lda.coef_/scaler.scale_[None]
        offset=np.rint(x.mean(0)).astype(np.int16)
        bias=lda.intercept_+weights@(offset.astype(float)-scaler.mean_)
        scores=(x.astype(float)-offset)@weights.T+bias
        scale=max(1.,float(np.std(scores)))
        with torch.no_grad():
            self.statistics.weight.copy_(torch.tensor(weights*256/scale,dtype=torch.float32))
            self.statistics.bias.copy_(torch.tensor(bias/scale,dtype=torch.float32))
        return offset


def quantize_statistics(module):
    weights=module.weight.detach().numpy()
    bias=module.bias.detach().numpy()
    wf=np.array([int(np.clip(np.floor(np.log2(32767/max(abs(w).max(),1e-20))),-16,32)) for w in weights])
    qw=np.rint(weights*np.exp2(wf[:,None])).clip(-32767,32767).astype(np.int16)
    qb=np.rint(bias*np.exp2(8+wf)).astype(np.int64)
    bound=np.abs(qb)+np.abs(qw.astype(np.int64)).sum(1)*65535
    if np.any(bound>=2**47):raise OverflowError('Hybrid statistics INT48 bound')
    return {'name':'statistics','weights':qw,'bias':qb,'shifts':wf,
            'input_fraction':8,'output_fraction':8,'accumulator_bits':48,
            'worst_case_accumulator':int(bound.max()),'relu':False,'clamp':False}


def execute_statistics(layer,centered):
    raw=np.asarray(centered,dtype=np.int64)@layer['weights'].astype(np.int64).T+layer['bias']
    if np.any(abs(raw)>=2**47):raise OverflowError('Hybrid statistics runtime INT48 bound')
    return np.stack([round_shift(raw[:,c],int(s)) for c,s in enumerate(layer['shifts'])],axis=1)


class IntegerHybrid:
    def __init__(self,layers,offset,wave_shifts):
        self.layers=layers
        self.offset=np.asarray(offset,dtype=np.int16)
        self.wave_shifts=np.asarray(wave_shifts,dtype=np.int16)
        self.cnn_active=bool(np.any(layers['learned']['weights']))

    def normalize_wave(self,wave):
        wave=np.asarray(wave,dtype=np.int64)
        return np.stack([np.clip(round_shift(wave[:,c],int(s)),-127,127)
                         for c,s in enumerate(self.wave_shifts)],axis=1)

    def trace(self,q,statistics):
        t={'input':np.asarray(q,dtype=np.int64),'centered':np.asarray(statistics,dtype=np.int64)-self.offset}
        t['stem']=execute(self.layers['stem'],t['input'])
        t['pool1']=pool(t['stem'])
        t['conv2']=execute(self.layers['conv2'],t['pool1'])
        t['pool2']=pool(t['conv2'])
        t['conv3']=execute(self.layers['conv3'],t['pool2'])
        t['embedding']=gap(t['conv3'])
        t['statistics']=execute_statistics(self.layers['statistics'],t['centered'])
        t['learned']=execute(self.layers['learned'],t['embedding'])
        t['logits']=t['statistics']+t['learned']
        return t

    def predict_logits(self,wave,statistics,batch=256,normalized=False):
        if not self.cnn_active:
            # Validation may explicitly reject the correction. Do not spend
            # runtime on a mathematically zero convolution output path.
            return execute_statistics(self.layers['statistics'],np.asarray(statistics,dtype=np.int64)-self.offset)
        q=np.asarray(wave,dtype=np.int64) if normalized else self.normalize_wave(wave)
        return np.concatenate([self.trace(q[i:i+batch],statistics[i:i+batch])['logits']
                               for i in range(0,len(q),batch)])

    def package(self,path,metadata):
        path=Path(path);path.mkdir(parents=True,exist_ok=True)
        arrays={'offset':self.offset,'wave_shifts':self.wave_shifts}
        topology=[]
        for name,layer in self.layers.items():
            topology.append({k:(v.tolist() if isinstance(v,np.ndarray) else v)
                             for k,v in layer.items() if k not in ('weights','bias','shifts')})
            for field in ('weights','bias','shifts'):
                arrays[f'{name}.{field}']=np.asarray(layer[field])
        payload=bytearray();tensors=[]
        for name,array in arrays.items():
            v=np.asarray(array,dtype=array.dtype.newbyteorder('<'))
            tensors.append({'name':name,'dtype':v.dtype.str,'shape':list(v.shape),'bytes':v.nbytes,'offset':len(payload)})
            payload.extend(v.tobytes())
        (path/'parameters.bin').write_bytes(payload)
        m={'format':'EMG-HYBRID-INT-1','parameter_bytes':len(payload),
           'cnn_correction_active':self.cnn_active,
           'sha256':hashlib.sha256(payload).hexdigest(),'tensors':tensors,'layers':topology,
           'metadata':metadata,'logit_fraction':8,
           'rounding':'Nearest, ties away from zero; conv INT8/INT32, statistics INT17/INT16/INT48.',
           'scope':'PC executable integer model; full feature/CNN scheduling and board integration pending.'}
        (path/'manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2),encoding='utf-8')
        return m

    @classmethod
    def load(cls,path):
        path=Path(path)
        m=json.loads((path/'manifest.json').read_text(encoding='utf-8'))
        payload=(path/'parameters.bin').read_bytes()
        if m['format']!='EMG-HYBRID-INT-1' or hashlib.sha256(payload).hexdigest()!=m['sha256']:
            raise ValueError('Invalid hybrid package')
        arrays={t['name']:np.frombuffer(payload,dtype=t['dtype'],count=t['bytes']//np.dtype(t['dtype']).itemsize,
                                      offset=t['offset']).reshape(t['shape']).copy() for t in m['tensors']}
        layers={}
        for descriptor in m['layers']:
            layer=dict(descriptor)
            for field in ('weights','bias','shifts'):layer[field]=arrays[f"{layer['name']}.{field}"]
            layers[layer['name']]=layer
        return cls(layers,arrays['offset'],arrays['wave_shifts']),m


@torch.no_grad()
def calibrate_hybrid(model,wave,offset,wave_shifts):
    model.eval();value=torch.as_tensor(wave);layers={};fi=5
    for name in ('stem','conv2','conv3'):
        block=getattr(model,name)
        value=block(value)
        fo=fraction(float(value.abs().max())*1.1)
        w,b=fold(block)
        layers[name]=qlayer(name,w,b,fi,fo)
        fi=fo
        if name!='conv3':value=nn.functional.max_pool1d(value,2)
    layers['statistics']=quantize_statistics(model.statistics)
    layers['learned']=qlayer('learned',model.learned.weight.detach().numpy(),
                              np.zeros(model.learned.out_features),fi,8,relu=False,clamp=False)
    return IntegerHybrid(layers,offset,wave_shifts)

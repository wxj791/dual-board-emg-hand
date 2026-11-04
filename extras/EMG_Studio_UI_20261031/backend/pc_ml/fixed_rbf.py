"""Compact fixed-point RBF SVM with shared kernels and multiclass vote lanes.

Training runs on PC; every inference operation is integer. The memory image
contains support vectors, coefficients, bias, normalizer and an exponential
lookup table. No sklearn/libsvm is needed to execute an exported model.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.cluster import KMeans
from sklearn.svm import SVC

from .data import round_shift


class FixedRbfSVC(ClassifierMixin, BaseEstimator):
    def __init__(self, representatives=24, C=10):
        self.representatives=representatives
        self.C=C

    def normalize(self,x):
        v=(np.asarray(x,dtype=np.int64)-self.offset_)*self.gain_
        return np.clip(round_shift(v,16),-127,127).astype(np.int8)

    def fit(self,x,y):
        x=np.asarray(x,dtype=np.int16)
        y=np.asarray(y)
        self.classes_=np.unique(y)
        self.n_features_in_=x.shape[1]
        self.offset_=np.rint(x.mean(0)).astype(np.int16)
        self.gain_=np.rint(16*65536/np.maximum(x.std(0),1)).astype(np.int32)
        q=self.normalize(x)
        centres=[]
        labels=[]
        for label in self.classes_:
            unique=np.unique(q[y==label],axis=0)
            count=min(self.representatives,len(unique))
            if count<len(unique):
                km=KMeans(n_clusters=count,n_init=1,random_state=42,max_iter=50).fit(unique.astype(float))
                # Centroid codes are valid synthetic feature representatives;
                # they are not claimed to be reconstructed EMG windows.
                representatives=np.clip(np.rint(km.cluster_centers_),-127,127).astype(np.int8)
            else:
                representatives=unique
            centres.append(representatives)
            labels.extend([label]*len(representatives))
        model=SVC(C=self.C,gamma=1/(self.n_features_in_*256),decision_function_shape='ovo')
        model.fit(np.concatenate(centres).astype(float),np.asarray(labels))
        self.support_=np.rint(model.support_vectors_).astype(np.int8)
        self.n_support_=model.n_support_.astype(np.int16)
        # C<=10 maps signed dual coefficients to INT16 Q11.
        self.dual_=np.rint(model.dual_coef_*2048).astype(np.int16)
        self.bias_=np.rint(model.intercept_*2048*32768).astype(np.int64)
        self.lut_=np.rint(np.exp(-np.arange(4097)/256)*32768).clip(0,32767).astype(np.int16)
        self.float_model_=model
        # Absolute bound for every LUT value and dual coefficient.
        self.accumulator_bound_=int(abs(self.dual_.astype(np.int64)).sum()*32767+abs(self.bias_).max())
        if self.accumulator_bound_>=2**47:
            raise OverflowError('RBF INT48 score bound exceeded')
        return self

    def kernel(self,q):
        q=np.asarray(q,dtype=np.int64)
        s=self.support_.astype(np.int64)
        distance=(q*q).sum(1)[:,None]+(s*s).sum(1)[None]-2*q@s.T
        # gamma = 1/(F*256), LUT x step=1/256 => index=distance/F.
        index=(distance+self.n_features_in_//2)//self.n_features_in_
        return self.lut_[np.clip(index,0,4096)].astype(np.int64)

    def decision_votes(self,x):
        q=self.normalize(x)
        votes=np.zeros((len(q),len(self.classes_)),dtype=np.int16)
        starts=np.r_[0,np.cumsum(self.n_support_)]
        score_rows=[]
        for begin in range(0,len(q),256):
            k=self.kernel(q[begin:begin+256])
            pair=0
            scores=[]
            for i in range(len(self.classes_)):
                for j in range(i+1,len(self.classes_)):
                    score=k[:,starts[i]:starts[i+1]]@self.dual_[j-1,starts[i]:starts[i+1]].astype(np.int64)
                    score+=k[:,starts[j]:starts[j+1]]@self.dual_[i,starts[j]:starts[j+1]].astype(np.int64)
                    score+=self.bias_[pair]
                    votes[begin:begin+len(k),i]+=score>0
                    votes[begin:begin+len(k),j]+=score<=0
                    scores.append(score)
                    pair+=1
            score_rows.append(np.stack(scores,axis=1))
        return votes,np.concatenate(score_rows)

    def predict(self,x):
        votes,_=self.decision_votes(x)
        return self.classes_[votes.argmax(1)]

    def package(self,path,metadata=None):
        path=Path(path)
        path.mkdir(parents=True,exist_ok=True)
        fields={'offset':self.offset_,'gain':self.gain_,'classes':self.classes_.astype(np.int16),
                'support':self.support_,'n_support':self.n_support_,'dual':self.dual_,
                'bias':self.bias_,'exp_lut':self.lut_}
        payload=bytearray()
        tensors=[]
        for name,array in fields.items():
            dtype=np.dtype(array.dtype).newbyteorder('<')
            value=np.asarray(array,dtype=dtype)
            tensors.append({'name':name,'shape':list(value.shape),'dtype':dtype.str,
                            'offset':len(payload),'bytes':value.nbytes})
            payload.extend(value.tobytes())
        (path/'parameters.bin').write_bytes(payload)
        manifest={'format':'EMG-RBF-INT-1','features':self.n_features_in_,
                  'normalizer_shift':16,'kernel_fraction':15,'dual_fraction':11,
                  'support_vectors':len(self.support_),'parameter_bytes':len(payload),
                  'sha256':hashlib.sha256(payload).hexdigest(),'tensors':tensors,
                  'score_accumulator_bits':48,'score_bound':self.accumulator_bound_,
                  'distance_accumulator_bits':24,'distance_max':self.n_features_in_*254**2,
                  'exponent_lut_entries':4097,'ties':'Lowest class index for vote ties',
                  'metadata':metadata or {},
                  'scope':'Executable integer inference; full FPGA integration not implemented.'}
        (path/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        return manifest

    @classmethod
    def load(cls,path):
        path=Path(path)
        m=json.loads((path/'manifest.json').read_text(encoding='utf-8'))
        payload=(path/'parameters.bin').read_bytes()
        if m['format']!='EMG-RBF-INT-1' or hashlib.sha256(payload).hexdigest()!=m['sha256']:
            raise ValueError('Invalid RBF package')
        model=cls()
        names={'offset':'offset_','gain':'gain_','classes':'classes_','support':'support_',
               'n_support':'n_support_','dual':'dual_','bias':'bias_','exp_lut':'lut_'}
        for t in m['tensors']:
            dtype=np.dtype(t['dtype'])
            array=np.frombuffer(payload,dtype=dtype,offset=t['offset'],count=t['bytes']//dtype.itemsize).reshape(t['shape']).copy()
            setattr(model,names[t['name']],array)
        model.n_features_in_=m['features']
        model.accumulator_bound_=m['score_bound']
        return model,m

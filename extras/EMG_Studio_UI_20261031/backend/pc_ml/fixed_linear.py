"""INT16 feature / INT16 weight / INT48 score linear discriminant inference."""
import hashlib
import json
from pathlib import Path

import numpy as np
from sklearn.discriminant_analysis import LinearDiscriminantAnalysis
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline


class FixedLinear:
    def fit(self,x,y):
        self.float_model_=make_pipeline(StandardScaler(),LinearDiscriminantAnalysis(solver='lsqr',shrinkage='auto'))
        self.float_model_.fit(x,y)
        return self.from_pipeline(self.float_model_,x)

    def from_pipeline(self,pipeline,x):
        self.float_model_=pipeline
        scaler,model=pipeline.steps[0][1],pipeline.steps[-1][1]
        weights=model.coef_/scaler.scale_[None]
        self.offset_=np.rint(np.asarray(x).mean(0)).astype(np.int16)
        bias=model.intercept_+weights@(self.offset_.astype(float)-scaler.mean_)
        self.fraction_=int(np.floor(np.log2(32767/max(abs(weights).max(),1e-20))))
        self.weights_=np.rint(weights*2.**self.fraction_).clip(-32767,32767).astype(np.int16)
        self.bias_=np.rint(bias*2.**self.fraction_).astype(np.int64)
        self.classes_=model.classes_.astype(np.int16)
        bound=np.abs(self.bias_)+abs(self.weights_.astype(np.int64)).sum(1)*65535
        self.bound_=int(bound.max())
        if self.bound_>=2**47: raise OverflowError('Linear INT48 bound')
        return self

    def scores(self,x):
        return (np.asarray(x,dtype=np.int64)-self.offset_)@self.weights_.astype(np.int64).T+self.bias_

    def predict(self,x):
        scores=self.scores(x)
        if len(self.classes_)==2 and scores.shape[1]==1:
            return self.classes_[(scores[:,0]>0).astype(int)]
        return self.classes_[scores.argmax(1)]

    def package(self,path,metadata):
        path=Path(path);path.mkdir(parents=True,exist_ok=True)
        fields={'offset':self.offset_,'weights':self.weights_,'bias':self.bias_,'classes':self.classes_}
        payload=bytearray();tensors=[]
        for name,v in fields.items():
            v=np.asarray(v,dtype=v.dtype.newbyteorder('<'))
            tensors.append({'name':name,'dtype':v.dtype.str,'shape':list(v.shape),'bytes':v.nbytes,'offset':len(payload)})
            payload.extend(v.tobytes())
        (path/'parameters.bin').write_bytes(payload)
        m={'format':'EMG-LINEAR-INT-1','fraction':self.fraction_,'parameter_bytes':len(payload),
           'score_accumulator_bits':48,'score_bound':self.bound_,'tensors':tensors,
           'sha256':hashlib.sha256(payload).hexdigest(),'metadata':metadata,
           'scope':'Executable integer classifier, full FPGA integration not implemented'}
        (path/'manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2),encoding='utf-8')
        return m

    @classmethod
    def load(cls,path):
        path=Path(path)
        m=json.loads((path/'manifest.json').read_text(encoding='utf-8'))
        payload=(path/'parameters.bin').read_bytes()
        if m['format']!='EMG-LINEAR-INT-1' or hashlib.sha256(payload).hexdigest()!=m['sha256']:
            raise ValueError('Invalid linear package')
        model=cls()
        for t in m['tensors']:
            dtype=np.dtype(t['dtype'])
            v=np.frombuffer(payload,dtype=dtype,offset=t['offset'],count=t['bytes']//dtype.itemsize).reshape(t['shape']).copy()
            setattr(model,t['name']+'_',v)
        model.fraction_=m['fraction'];model.bound_=m['score_bound']
        return model,m

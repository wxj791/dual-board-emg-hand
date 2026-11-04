"""Label-free PC preview of exported fixed linear/RBF EMG algorithms.

This module opens no serial port and sends no actuator commands. The existing
receiver can feed raw blocks to StreamingFeaturePredictor.push().
"""
import argparse
import json
from pathlib import Path

import numpy as np

from .fixed_linear import FixedLinear
from .fixed_rbf import FixedRbfSVC
from .frontend import PrecisionFilter
from .replay import Decision
from .today_survey import features,Q8Filter


class StreamingFeaturePredictor:
    def __init__(self,package):
        package=Path(package)
        manifest=json.loads((package/'manifest.json').read_text(encoding='utf-8'))
        if manifest['format']=='EMG-LINEAR-INT-1':
            self.model,self.manifest=FixedLinear.load(package)
        elif manifest['format']=='EMG-RBF-INT-1':
            self.model,self.manifest=FixedRbfSVC.load(package)
        elif manifest['format']=='EMG-HYBRID-INT-1':
            from .hybrid import IntegerHybrid
            self.model,self.manifest=IntegerHybrid.load(package)
        else:
            raise ValueError('Expected integer linear, compact RBF or hybrid package')
        self.meta=self.manifest['metadata']
        self.width=int(self.meta['window'])
        self.stride=int(self.meta['stride'])
        if self.width not in (128,256) or self.stride!=32:
            raise ValueError('Unsupported streaming window contract')
        self.reset()

    def reset(self):
        self.frontend=PrecisionFilter(self.meta['coefficients_q14'])
        self.bands=[Q8Filter(c) for c in self.meta['subband_coefficients_q14']]
        self.pending=[np.empty((0,8),dtype=np.int32) for _ in range(4)]
        self.count=0
        self.decision=Decision(margin=0,persistence=3)

    def push(self,raw):
        samples=self.frontend.process(raw)
        blocks=[samples]+[band.process(samples) for band in self.bands]
        kept=[np.concatenate((old,block)) for old,block in zip(self.pending,blocks)]
        starts=[];ends=[]
        for i in range(len(samples)):
            absolute=self.count+i+1
            if absolute>=320+self.width and (absolute-320-self.width)%self.stride==0:
                end=len(kept[0])-len(samples)+i+1
                starts.append(end-self.width);ends.append(absolute)
        self.pending=[v[-self.width:].copy() for v in kept]
        self.count+=len(samples)
        if not starts:return []
        windows=[np.stack([v[s:s+self.width].T for s in starts]) for v in kept]
        encoded=features(windows[0],windows[1:])
        if self.manifest['format']=='EMG-HYBRID-INT-1':
            scores=self.model.predict_logits(windows[0],encoded)
            predicted=scores.argmax(1)
        else:
            predicted=self.predict_features(encoded,windows)
            scores=np.zeros((len(predicted),len(self.meta['class_names'])),dtype=np.int64)
            scores[np.arange(len(predicted)),predicted]=1
        output=[]
        for end,pred,logits in zip(ends,predicted,scores):
            # Persistence is a UI preview only, not a validated reject gate.
            _,margin,accepted,stable=self.decision.step(logits)
            output.append({'sample_end':end,'prediction':int(pred),'stable':int(stable),
                           'margin_q8':margin,'accepted':accepted,
                           'name':self.meta['class_names'][int(stable)],
                           'acceptance_validated':False})
        return output

    def predict_features(self,encoded,windows):
        feature_count=self.model.weights_.shape[1] if hasattr(self.model,'weights_') else self.model.n_features_in_
        if feature_count==176:
            from .today_covariance import covariance_features
            encoded=np.concatenate([encoded]+[covariance_features(w) for w in windows],axis=1)
        elif feature_count!=64:
            raise ValueError('Unsupported integer feature shape')
        return self.model.predict(encoded)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--package',type=Path,required=True)
    p.add_argument('--recording',type=Path,required=True)
    p.add_argument('--threads',type=int,default=2)
    args=p.parse_args()
    if args.threads<1:p.error('--threads must be positive')
    import torch
    torch.set_num_threads(args.threads)
    predictor=StreamingFeaturePredictor(args.package)
    with np.load(args.recording,allow_pickle=True) as z:
        raw,labels,timestamps=z['emg'],z['labels'],z['timestamps']
    # Recorded transitions were omitted: report resets, not a continuous live test.
    edges=np.unique(np.r_[0,np.flatnonzero(labels[1:]!=labels[:-1])+1,
                          np.flatnonzero(np.diff(timestamps)>.25)+1,len(labels)])
    count=0
    for a,b in zip(edges[:-1],edges[1:]):
        predictor.reset()
        results=predictor.push(raw[a:b]);count+=len(results)
        if results:print(json.dumps({'start':int(a),'last':results[-1],'windows':len(results)},ensure_ascii=False))
    print('Preview windows:',count,'(includes training intervals; not an independent accuracy result)')


if __name__=='__main__':main()

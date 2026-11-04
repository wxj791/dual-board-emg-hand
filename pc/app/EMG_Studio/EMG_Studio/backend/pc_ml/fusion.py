"""Field runtime for frozen statistics/envelope/spatial equal-score fusion.

This module never reads gesture labels during inference. Input samples are raw
physical-order integer mV, exactly as stored by the existing workstation.
"""
from pathlib import Path
import json

import numpy as np
from numba import njit
from sklearn.discriminant_analysis import LinearDiscriminantAnalysis
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .fixed_linear import FixedLinear
from .parallel_envelope import envelope_features
from .today_covariance import covariance_features
from .today_survey import features, fixed_coefficients, subband_coefficients

RUNTIME='parallel_fusion_v1'
TITLE='并行融合（波形＋包络＋通道）'
MIN_CLASSES=2
MAX_CLASSES=32


class FusionAffine(LinearDiscriminantAnalysis):
    """Folded class scores, including two explicit scores for binary profiles."""
    def decision_function(self,x):
        return np.asarray(x)@self.coef_.T+self.intercept_

    def predict(self,x):
        return self.classes_[self.decision_function(x).argmax(1)]


@njit(cache=True)
def advance(raw_q8,coeff,state,env_state):
    signal=np.empty((len(raw_q8),32),dtype=np.int32)
    env=np.empty((len(raw_q8),16),dtype=np.int32)
    for n in range(len(raw_q8)):
        for ch in range(8):
            current=raw_q8[n,ch];main=0
            for section in range(9):
                if section>=6:current=main
                s=state[section,ch];c=coeff[section]
                acc=c[0]*current+c[1]*s[0]+c[2]*s[1]-c[3]*s[2]-c[4]*s[3]
                v=(acc+8192)>>14 if acc>=0 else -((-acc+8192)>>14)
                v=min(8388607,max(-8388608,v))
                s[1]=s[0];s[0]=current;s[3]=s[2];s[2]=v;current=v
                if section==5:main=v;signal[n,ch]=v
                elif section>=6:signal[n,(section-5)*8+ch]=v
            target=(abs(main)+8)>>4
            for t in range(2):
                shift=4 if t==0 else 6
                delta=target-env_state[t,ch]
                step=(abs(delta)+(1<<(shift-1)))>>shift
                env_state[t,ch]+=step if delta>=0 else -step
                env[n,t*8+ch]=env_state[t,ch]
    return signal,env


class FusionFeatures:
    width=256
    stride=32
    guard=320

    def __init__(self):
        self.coeff=np.asarray(list(fixed_coefficients('clean'))+[c[0] for c in subband_coefficients()],dtype=np.int64)
        self.reset()

    def reset(self):
        self.state=np.zeros((9,8,4),dtype=np.int64)
        self.env_state=np.zeros((2,8),dtype=np.int64)
        self.signal_tail=np.empty((0,32),dtype=np.int32)
        self.env_tail=np.empty((0,16),dtype=np.int32)
        self.count=0

    def push(self,raw):
        raw=np.asarray(raw)
        if raw.ndim!=2 or raw.shape[1]!=8 or not np.isfinite(raw).all():
            raise ValueError('融合输入须为有限的 [样本数,8] 原始毫伏数据')
        if not len(raw):return np.empty((0,304),dtype=np.int16),np.empty(0,dtype=np.int64)
        q8=(np.rint(raw).clip(0,3300).astype(np.int64)-1650)*256
        signal,env=advance(q8,self.coeff,self.state,self.env_state)
        tail=len(self.signal_tail)
        signal=np.concatenate([self.signal_tail,signal],0)
        env=np.concatenate([self.env_tail,env],0)
        first=self.guard+self.width
        if self.count>=first:first += ((self.count-first)//self.stride+1)*self.stride
        ends=np.arange(first,self.count+len(raw)+1,self.stride,dtype=np.int64)
        starts=tail+ends-self.count-self.width
        self.count+=len(raw)
        self.signal_tail=signal[-self.width:].copy()
        self.env_tail=env[-self.width:].copy()
        if not len(starts):return np.empty((0,304),dtype=np.int16),ends
        windows=[np.stack([signal[s:s+self.width,b*8:b*8+8].T for s in starts]) for b in range(4)]
        stat=features(windows[0],windows[1:])
        spatial=np.concatenate([covariance_features(w) for w in windows],1)
        envelope=envelope_features(env,starts)
        return np.concatenate([stat,envelope,spatial],1),ends


def extract(raw,chunk_size=8192):
    frontend=FusionFeatures();xs=[];ends=[]
    for i in range(0,len(raw),chunk_size):
        x,e=frontend.push(raw[i:i+chunk_size])
        xs.append(x);ends.append(e)
    return (np.concatenate(xs) if xs else np.empty((0,304),dtype=np.int16),
            np.concatenate(ends) if ends else np.empty(0,dtype=np.int64))


class FusionDecision:
    def __init__(self,num_classes=7):
        if not MIN_CLASSES<=num_classes<=MAX_CLASSES:raise ValueError('融合模型支持2～32类（含静息）')
        self.state=np.zeros(num_classes,dtype=np.int64)

    def step(self,score):
        score=np.asarray(score,dtype=np.int64)
        if score.shape!=self.state.shape:raise ValueError('融合决策分数数量与类别数不一致')
        raw=int(score.argmax())
        if raw==0:self.state=score.copy();stable=0
        else:
            delta=score-self.state
            self.state+=np.sign(delta)*((abs(delta)+2)>>2)
            stable=int(self.state.argmax())
        top=np.sort(self.state)
        margin=int(top[-1]-top[-2])
        scale=max(1,int(abs(self.state).max()))
        return raw,stable,int(min(256,margin*256//scale))


def validate_manifest(manifest):
    meta=manifest.get('metadata',{})
    expected={'runtime':RUNTIME,'front':'clean','window':256,'stride':32,
              'guard':320,'feature_dimensions':304,'decision_policy':'raw_rest_else_ewma_shift2'}
    if any(meta.get(k)!=v for k,v in expected.items()):raise ValueError('不支持的融合模型处理配置')
    if meta.get('coefficients_q14')!=[list(c) for c in fixed_coefficients('clean')]:raise ValueError('融合主滤波系数不匹配')
    if meta.get('subband_coefficients_q14')!=[[list(c) for c in rows] for rows in subband_coefficients()]:raise ValueError('融合子带系数不匹配')
    names=meta.get('class_names',[])
    if not MIN_CLASSES<=len(names)<=MAX_CLASSES:raise ValueError('融合模型支持2～32类（含静息）')
    if len(set(names))!=len(names):raise ValueError('融合模型动作名称不能重复')
    profile=meta.get('gesture_profile')
    if profile:
        classes=profile.get('classes',[])
        if ([c['id'] for c in classes]!=list(range(len(names))) or
                [c['name'] for c in classes]!=names or classes[0]['key']!='rest'):
            raise ValueError('融合模型动作方案、静息或类别顺序不一致')


def load(package):
    head,manifest=FixedLinear.load(package)
    validate_manifest(manifest)
    k=len(manifest['metadata']['class_names'])
    if head.weights_.shape!=(k,304) or not np.array_equal(head.classes_,np.arange(k)):
        raise ValueError('融合分类头形状或类别顺序不匹配')
    return head,manifest


class StreamingFusionPredictor:
    def __init__(self,package):
        self.model,self.manifest=load(package)
        self.meta=self.manifest['metadata']
        self.reset()

    def reset(self):
        self.frontend=FusionFeatures();self.decision=FusionDecision(len(self.meta['class_names']))

    def push(self,raw):
        x,ends=self.frontend.push(raw)
        scores=self.model.scores(x)
        output=[]
        for end,score in zip(ends,scores):
            pred,stable,margin=self.decision.step(score)
            output.append({'sample_end':int(end),'prediction':pred,'stable':stable,
                'margin_q8':margin,'accepted':True,'name':self.meta['class_names'][stable],
                'acceptance_validated':False})
        return output


def fit_fusion(x,y,groups):
    """Frozen three experts, train-only score scaling, one folded affine head."""
    classes=np.unique(y);k=len(classes)
    if not MIN_CLASSES<=k<=MAX_CLASSES or not np.array_equal(classes,np.arange(k)):
        raise ValueError('融合训练需要连续编号的2～32类（0为静息）')
    indices=[np.arange(64),np.arange(64,192),np.r_[np.arange(64),np.arange(192,304)]]
    weights=np.zeros((k,304));bias=np.zeros(k);scales=[]
    for ids in indices:
        scaler=StandardScaler().fit(x[:,ids]);z=scaler.transform(x[:,ids])
        lda=LinearDiscriminantAnalysis(solver='lsqr',shrinkage='auto',priors=np.ones(k)/k).fit(z,y)
        nuisance=np.zeros((len(ids),len(ids)))
        for c in range(k):
            centers=np.stack([z[(y==c)&(groups==g)].mean(0) for g in np.unique(groups[y==c])])
            centers-=centers.mean(0);nuisance+=centers.T@centers/len(centers)/k
        lda.coef_=np.linalg.solve(lda.covariance_+3*nuisance,lda.means_.T).T
        lda.intercept_=-.5*(lda.coef_*lda.means_).sum(1)+np.log(lda.priors_)
        s=z@lda.coef_.T+lda.intercept_;top=np.sort(s,axis=1)
        scale=max(1.,float(np.median(top[:,-1]-top[:,-2])));scales.append(scale)
        w=lda.coef_/scaler.scale_[None]/scale/3
        weights[:,ids]+=w;bias+=lda.intercept_/scale/3-w@scaler.mean_
    scaler=StandardScaler().fit(x)
    lda=FusionAffine(solver='lsqr')
    lda.coef_=weights*scaler.scale_[None];lda.intercept_=bias+weights@scaler.mean_
    lda.classes_=np.arange(k);lda.n_features_in_=304
    folded=make_pipeline(scaler,lda)
    return folded,FixedLinear().from_pipeline(folded,x),scales


def package(head,directory,metadata):
    meta={**metadata,'runtime':RUNTIME,'model_title':TITLE,'front':'clean','window':256,'stride':32,
        'guard':320,'feature_dimensions':304,'feature_kind':'equal_score304',
        'decision_policy':'raw_rest_else_ewma_shift2',
        'coefficients_q14':[list(c) for c in fixed_coefficients('clean')],
        'subband_coefficients_q14':[[list(c) for c in rows] for rows in subband_coefficients()],
        'complete_fpga_deployment':False}
    desc=head.package(directory,meta)
    profile=meta.get('gesture_profile')
    desc.update(class_names=meta['class_names'],gesture_profile=profile,
        action_mapping=[c['output'] for c in profile['classes']] if profile else [],
        payload_bytes=desc['parameter_bytes'],model_title=TITLE,frontend={'name':'clean'})
    (Path(directory)/'manifest.json').write_text(json.dumps(desc,ensure_ascii=False,indent=2),encoding='utf-8')
    return desc

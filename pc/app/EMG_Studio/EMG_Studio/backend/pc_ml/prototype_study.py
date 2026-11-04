"""Single max-affine prototype classifier; exploratory FPGA head candidate."""
import json
from pathlib import Path
import time

import numpy as np
from sklearn.cluster import KMeans
from sklearn.discriminant_analysis import LinearDiscriminantAnalysis
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from .today_survey import validation_score
from .train import metrics


def fit(x,y,components,shrinkage,space):
    scaler=StandardScaler().fit(x);z=scaler.transform(x)
    lda=LinearDiscriminantAnalysis(solver='lsqr',shrinkage=shrinkage,priors=np.ones(7)/7).fit(z,y)
    covariance=lda.covariance_ if space=='within' else np.eye(z.shape[1])
    inverse=np.linalg.pinv(covariance,hermitian=True)
    values,vectors=np.linalg.eigh(inverse)
    whiten=vectors@np.diag(np.sqrt(np.maximum(values,0)))@vectors.T
    embedded=z@whiten
    centers=[]
    for c in range(7):centers.append(KMeans(components,random_state=42,n_init=5).fit(embedded[y==c]).cluster_centers_)
    centers=np.asarray(centers)
    weights=(centers@whiten.T)/scaler.scale_
    bias=-.5*(centers**2).sum(-1)-weights@scaler.mean_
    return weights,bias


def predict(x,weights,bias):
    scores=np.einsum('nf,ckf->nck',x,weights)+bias
    return scores.max(-1).argmax(-1)


def run(directory):
    with np.load(directory/'prepared.npz') as z:d={k:z[k] for k in z.files}
    x,y,split=[d[k] for k in ['x','y','split']];train,val,test=[split==i for i in range(3)]
    candidates=[]
    for space in ['within','diagonal']:
        for shrinkage in ['auto',.1,.5]:
            for components in [1,2,3]:
                w,b=fit(x[train],y[train],components,shrinkage,space)
                m=metrics(y[val],predict(x[val],w,b),7)
                candidates.append({'components':components,'shrinkage':shrinkage,'space':space,'validation':m})
    winner=max(candidates,key=lambda r:validation_score(r['validation']))
    kwargs={k:winner[k] for k in ['components','shrinkage','space']}
    w,b=fit(x[train|val],y[train|val],**kwargs)
    held=metrics(y[test],predict(x[test],w,b),7)
    fw,fb=fit(x,y,**kwargs)
    training=metrics(y,predict(x,fw,fb),7)
    report={'recording':directory.name,'winner':winner,'test':held,'all_data_training':training,
            'candidates':candidates,'scope':'Float screening only; max-affine head not yet implemented or quantized in RTL.'}
    out=Path('artifacts/prototype_study')/directory.name;out.mkdir(parents=True,exist_ok=True)
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(directory.name,kwargs,'validation',winner['validation']['accuracy'],'test',held['accuracy'],'worst',min(held['recall']),
          'fit',training['accuracy'],'fitworst',min(training['recall']),flush=True)


if __name__=='__main__':
    with threadpool_limits(limits=2):run(Path('artifacts/fast_field/DDW_P2_2610281200'))

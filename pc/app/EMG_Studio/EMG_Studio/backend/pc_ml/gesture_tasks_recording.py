"""Validate recorded compact metadata before training; never bridge stream segments."""
import numpy as np

def stream_edges(z,raw,labels,timestamps):
    n=len(raw)
    edges=np.r_[0,np.flatnonzero((np.diff(timestamps)>.25)|(np.diff(timestamps)<0))+1,n]
    if 'capture_schema' not in z:return np.unique(edges)
    required=('capture_complete','sample_rate','labels_source','channels_already_reordered',
              'sample_seq','timestamp_us','generation','epoch','flags','segment_id','frame_seq','break_mask')
    if any(key not in z for key in required):raise ValueError('紧凑记录缺少必要元数据')
    if str(np.asarray(z['capture_schema']).item())!='A_RAW_43_V1':raise ValueError('未知采集schema')
    if ('capture_complete' not in z or not bool(np.asarray(z['capture_complete']).item()) or
        float(z['sample_rate'])!=1000 or str(np.asarray(z['labels_source']).item())!='pc_collection_schedule' or
        not bool(np.asarray(z['channels_already_reordered']).item())):raise ValueError('紧凑记录未完成或采样/标签来源无效')
    keys=('sample_seq','timestamp_us','generation','epoch','flags','segment_id','frame_seq','break_mask')
    a={key:np.asarray(z[key]) for key in keys}
    if any(v.shape!=(n,) or not np.issubdtype(v.dtype,np.integer) for v in a.values()):
        raise ValueError('紧凑记录逐点元数据长度/类型不匹配')
    for key,bits in (('sample_seq',32),('timestamp_us',32),('generation',16),('epoch',16),('frame_seq',16),('break_mask',11)):
        if np.any((a[key]<0)|(a[key]>=(1<<bits))):raise ValueError('紧凑记录元数据超范围')
    if np.any((a['flags']<0)|(a['flags']>7)) or np.any((raw<0)|(raw>3300)) or np.any(raw!=np.rint(raw)):
        raise ValueError('紧凑记录采样字段无效')
    if np.any(labels[(a['flags']&6)!=0]!=-1):raise ValueError('warmup/replay不能具有训练标签')
    changes=np.flatnonzero(a['segment_id'][1:]!=a['segment_id'][:-1])+1
    if np.any(np.diff(a['segment_id'].astype(np.int64))<0):raise ValueError('segment_id不能倒退')
    same=a['segment_id'][1:]==a['segment_id'][:-1]
    good=(((a['sample_seq'][1:].astype(np.uint64)-a['sample_seq'][:-1].astype(np.uint64))&0xffffffff)==1)
    delta=(a['timestamp_us'][1:].astype(np.uint64)-a['timestamp_us'][:-1].astype(np.uint64))&0xffffffff
    good &= (delta<=250000) & (a['generation'][1:]==a['generation'][:-1]) & (a['epoch'][1:]==a['epoch'][:-1])
    good &= ((a['flags'][1:]&6)==(a['flags'][:-1]&6)) & ((a['flags'][1:]&1)==0) & (a['break_mask'][1:]==0)
    if np.any(same & ~good):raise ValueError('采样缺号/版本/标记变化没有分段，拒绝跨段训练')
    return np.unique(np.r_[edges,changes])

def extract_segments(raw,edges):
    from .fusion import extract
    xs=[];ends=[]
    for a,b in zip(edges[:-1],edges[1:]):
        x,e=extract(raw[a:b]);xs.append(x);ends.append(e+a)
    return (np.concatenate(xs) if xs else np.empty((0,304),dtype=np.int16),
            np.concatenate(ends) if ends else np.empty(0,dtype=np.int64))

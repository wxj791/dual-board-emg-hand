"""Current-session CNN -> frozen A v2 banks. Never substitutes baseline weights."""
from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path
import struct
import zlib
import numpy as np
import torch
from .data import round_shift
from .models import SequentialCNN
from .quantize import calibrate, direct_reference
from .cnn_fusion import load, normalize, logits, fuse

META = '{{"format":"EMG-A-AB-V2-1","classes":{count:5d},"architecture":"clean-cnn256-stat304-q23-q8","stride":32}}'
FUSION_GATE_Q23 = 2097152
MAX_SCORE_SCALE_SHIFT = 21  # Keep the integer fusion threshold nonzero.

def metadata(count):
    if type(count) is not int or not 2 <= count <= 32: raise ValueError('A supports 2..32 classes')
    return META.format(count=count).encode('ascii')

def transaction_crc(meta, banks):
    crc = zlib.crc32(meta)
    for b, values in enumerate(banks):
        for a,v in enumerate(values): crc = zlib.crc32(struct.pack('<BII',b,a,int(v)),crc)
    return crc

def signed(v, bits): return v-(1<<bits) if v & (1<<(bits-1)) else v

def validate_banks(banks, k):
    metadata(k)
    if len(banks) != 8 or list(map(len,banks)) != [304*k,304,2*k,1280,160,32*k,2*k,k]:
        raise ValueError('A bank shape mismatch')
    if any(type(v) is not int or not 0 <= v <= 0xffffffff for bank in banks for v in bank):
        raise ValueError('invalid u32 word')
    if any(v>65535 for b in (0,1,7) for v in banks[b]) or any(v>255 for v in banks[5]):
        raise ValueError('invalid weight width')
    cfg = banks[4]
    gate=cfg[153]
    if (any(v>31 for v in cfg[1:144:2]+cfg[144:152]+banks[6][1::2]) or
        cfg[152] != 15 or not 0 < gate <= FUSION_GATE_Q23 or gate & (gate-1) or
        any(cfg[154:])): raise ValueError('invalid scale/config')
    for c in range(k):
        if banks[2][c*2+1]>65535: raise ValueError('signed48 bias width')
        bias = signed(banks[2][c*2] | banks[2][c*2+1]<<32,48)
        bound = abs(bias)+sum(abs(signed(w,16))*65535 for w in banks[0][304*c:304*(c+1)])
        fc = abs(signed(banks[6][2*c],32))+sum(abs(signed(w,8))*127 for w in banks[5][32*c:32*(c+1)])
        shift = banks[6][2*c+1]; fc = (fc+(1<<(shift-1) if shift else 0))>>shift
        if fc >= 1<<23 or bound+(fc<<15) >= 1<<47: raise OverflowError('A score accumulator bound')
    for offset,cout,terms in ((0,16,40),(40,24,48),(88,32,72)):
        cfg_start = {0:0,40:32,88:80}[offset]
        for oc in range(cout):
            total = abs(signed(cfg[cfg_start+oc*2],32))
            total += 127*sum(abs(signed((banks[3][(offset+t)*8+oc//4]>>(8*(oc%4)))&255,8)) for t in range(terms))
            if total >= 1<<31: raise OverflowError('A convolution accumulator bound')

def commands(package, chunk=128):
    k=package['active_classes']; banks=package['banks']; validate_banks(banks,k)
    meta=metadata(k)
    if package['transaction_crc32'] != f'{transaction_crc(meta,banks):08x}': raise ValueError('model CRC mismatch')
    if not 1 <= chunk <= 252: raise ValueError('invalid chunk')
    yield 0x102,struct.pack('<I',len(meta))+meta
    for b,values in enumerate(banks):
        for a in range(0,len(values),chunk):
            batch=values[a:a+chunk]
            yield 0x103,struct.pack('<BIH',b,a,len(batch))+struct.pack('<'+'I'*len(batch),*batch)
    yield 0x104,struct.pack('<I',transaction_crc(meta,banks))

def canonical_statistics(head):
    """Fit Q23 without clipping by removing a score common to every class.

    A shared per-feature coefficient cancels in all pairwise score differences,
    including the fusion gate. Only use this representation when the original
    Q23 weights overflow, preserving already valid deployment bytes.
    """
    k=len(head.classes_); metadata(k)
    if head.weights_.shape != (k,304): raise ValueError('expected explicit per-class 304-feature head')
    if any(not np.issubdtype(a.dtype,np.integer) for a in (head.weights_,head.bias_,head.offset_)):
        raise ValueError('统计参数必须为整数')
    if np.any((head.offset_<-32768)|(head.offset_>32767)): raise ValueError('feature offset outside signed16')
    # Prove the target range using Python integers before any int64 shift, so
    # an incompatible source fraction cannot wrap around and appear legal.
    def rescale(array,limit,center=False):
        shift=int(head.fraction_)-23; values=[]
        for value in array.reshape(-1):
            v=int(value)
            v=(v<<-shift) if shift<=0 else (1 if v>=0 else -1)*((abs(v)+(1<<(shift-1)))>>shift)
            values.append(v)
        offsets=[0]*304
        if center and any(abs(v)>=limit for v in values):
            for column in range(304):
                group=values[column::304]
                offsets[column]=(min(group)+max(group))//2
            values=[v-offsets[i%304] for i,v in enumerate(values)]
        if any(abs(v)>=limit for v in values):
            raise OverflowError('本轮统计头超出A固定Q23范围；禁止复用默认模型')
        return np.array(values,dtype=np.int64).reshape(array.shape),offsets
    qhead_w,common = rescale(head.weights_,32768,center=True)
    qhead_b,_ = rescale(head.bias_,1<<47)
    if np.any(abs(qhead_w)>32767) or np.any(abs(qhead_b)>=1<<47):
        raise OverflowError('本轮统计头无法无溢出转换为A固定Q23；需要重新训练/校准，禁止使用默认模型')
    return qhead_w,qhead_b,common


def deployment_statistics(head):
    """Use the smallest common score attenuation fitting existing A Q23 banks.

    The caller must also scale CNN logits and the UART-programmable gate.
    Fraction changes here reinterpret a copy; the source PC model is untouched.
    No coefficient is clipped and already compatible exports keep their bytes.
    """
    trial=copy.copy(head)
    for shift in range(MAX_SCORE_SCALE_SHIFT+1):
        trial.fraction_=int(head.fraction_)+shift
        try:
            w,b,common=canonical_statistics(trial)
        except OverflowError:
            continue
        return w,b,common,shift
    raise OverflowError('统计模型无法在A现有Q23格式及融合阈值范围内表示，请重新训练或校准')


def quantize_current(head, model, shifts, calibration):
    k=len(head.classes_)
    qhead_w,qhead_b,_,score_shift=deployment_statistics(head)
    adapted=SequentialCNN(k).eval()
    for name,index in (('stem',0),('conv2',2),('conv3',4)):
        block=getattr(adapted,name); source=model.blocks[index]
        block.conv.load_state_dict(source[0].state_dict()); block.bn.load_state_dict(source[1].state_dict())
    adapted.fc.load_state_dict(model.fc.state_dict())
    x=normalize(calibration,shifts)
    with torch.no_grad():
        np.testing.assert_allclose(logits(model,x[:4]),logits(adapted,x[:4]),atol=1e-6,rtol=1e-5)
        # Preserve the statistics/CNN ratio while fitting fixed Q23 weight banks.
        # A's residual left shift stays 15; FC output and gate share attenuation.
        factor=.5/(1<<score_shift)
        adapted.fc.weight.mul_(factor); adapted.fc.bias.mul_(factor)
    integer=calibrate(adapted,'sequential',x)
    for layer in integer.layers.values():
        if np.any((layer['shifts']<0)|(layer['shifts']>31)): raise ValueError('A shift outside 0..31')
    trace=integer.trace(np.rint(x[:1]*32).astype(np.int64))
    for name,inp in (('stem','input'),('conv2','pool1'),('conv3','pool2'),('fc','features')):
        np.testing.assert_array_equal(trace[name if name!='fc' else 'logits'],direct_reference(integer.layers[name],trace[inp]))
    words=np.zeros((160,8),dtype=np.uint32); pos=0; cfg=[0]*160; ci=0
    for name in ('stem','conv2','conv3'):
        layer=integer.layers[name]; w=layer['weights'].reshape(len(layer['weights']),-1)
        for term in range(w.shape[1]):
            for oc in range(len(w)): words[pos,oc//4] |= np.uint32((int(w[oc,term])&255)<<(8*(oc%4)))
            pos+=1
        for b,s in zip(layer['bias'],layer['shifts']): cfg[ci:ci+2]=[int(b)&0xffffffff,int(s)]; ci+=2
    cfg[144:152]=list(map(int,shifts)); cfg[152:154]=[15,FUSION_GATE_Q23>>score_shift]
    fc=integer.layers['fc']
    banks=[(qhead_w.reshape(-1)&65535).tolist(),(head.offset_.astype(np.int64)&65535).tolist(),
           sum([[int(b)&0xffffffff,(int(b)>>32)&65535] for b in qhead_b],[]),
           [int(v) for v in words.reshape(-1)],cfg,(fc['weights'].reshape(-1).astype(np.int64)&255).tolist(),
           sum([[int(b)&0xffffffff,int(s)] for b,s in zip(fc['bias'],fc['shifts'])],[]),[0]*k]
    validate_banks(banks,k)
    return banks,integer,qhead_w,qhead_b

def export_current(package, prepared, output):
    package=Path(package); output=Path(output)
    head,model,shifts,m=load(package); d=prepared
    valid=d['y']>=0; train=valid & (d['split']!=2)
    ids=np.flatnonzero(train)
    if not len(ids): raise ValueError('no calibration training windows')
    ids=ids[np.linspace(0,len(ids)-1,min(512,len(ids)),dtype=int)]
    banks,integer,w,b=quantize_current(head,model,shifts,d['waves'][ids])
    _,_,common,score_shift=deployment_statistics(head)
    heldout=np.flatnonzero(valid & (d['split']==2))
    if not len(heldout) and m['metadata'].get('trained_all_bouts'):
        # A training-only model can still be exported; this is explicitly a self-check.
        heldout=np.flatnonzero(valid)
    if not len(heldout): raise ValueError('no held-out windows')
    # Validation-export holds out test bouts; final export is explicitly an all-bout self-check.
    raw=d['waves'][heldout]; x=normalize(raw,shifts)
    base=(d['x'][heldout].astype(np.int64)-head.offset_)@w.T+b
    cnn=integer.predict_logits(x,batch=32)
    strong=np.sort(base,axis=1)[:,-1]-np.sort(base,axis=1)[:,-2]>=banks[4][153]
    scores=base+(cnn<<np.where(strong,13,15)[:,None])
    float_scores=fuse(head.scores(d['x'][heldout]),logits(model,x),head.fraction_)
    y=d['y'][heldout]
    verification={'scope':'heldout' if not m['metadata'].get('trained_all_bouts') else 'all_bout_model_self_check',
        'statistical_common_score_removed':any(common),
        'score_scale_shift':score_shift,'score_scale_divisor':1<<score_shift,
        'fusion_gate_q23':banks[4][153],
        'statistical_weight_max':int(abs(w).max()),
        'score_scaling_note':'统计分数、CNN输出及融合阈值等比缩放；A固定左移15位及参数位宽保持原状',
        'windows':len(y),'pc_float_accuracy':float(np.mean(float_scores.argmax(1)==y)),
        'a_quantized_accuracy':float(np.mean(scores.argmax(1)==y)),
        'prediction_agreement':float(np.mean(scores.argmax(1)==float_scores.argmax(1))),
        'direct_int64_reference':'all four layers passed on first calibration window',
        'hardware_verified':False,'source_sha256':m['sha256']}
    doc={'format':'EMG-A-AB-V2-1','active_classes':len(m['class_names']),'class_names':m['class_names'],
         'source_sha256':m['sha256'],'source_package':str(package.resolve()),'flash_persistent':False,
         'banks':banks,'transaction_crc32':f'{transaction_crc(metadata(len(m["class_names"])),banks):08x}',
         'verification':verification}
    doc['a_model_path']=str((output/'a_model.json').resolve())
    output.mkdir(parents=True,exist_ok=False)
    text=json.dumps(doc,ensure_ascii=False,indent=2)+'\n'
    (output/'a_model.json').write_text(text,encoding='utf8')
    (output/'verification.json').write_text(json.dumps(verification,indent=2)+'\n',encoding='utf8')
    return doc

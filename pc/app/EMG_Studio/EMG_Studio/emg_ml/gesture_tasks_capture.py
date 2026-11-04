"""Compact A raw stream segmentation and lossless recording metadata."""
import struct
import time
import numpy as np

BREAK_REASONS=('capture_start','sample_gap','generation','epoch','flags','discontinuity',
               'timestamp_gap','host_gap','outer_gap','crc_or_schema','format_change')

class RawTimeline:
    def __init__(self):
        self.previous=None; self.last_frame=None; self.last_errors=0
        self.pending=set(); self.segment=-1; self.time=0.; self.start_time=time.time()
    def observe(self,seq,errors):
        if self.last_frame is not None and seq!=(self.last_frame+1)&65535:self.pending.add('outer_gap')
        if errors!=self.last_errors:self.pending.add('crc_or_schema')
        self.last_frame=seq;self.last_errors=errors
    def raw(self,p,frame_seq,stamp,now):
        if len(p)!=28:raise ValueError('0x43 length must be 28')
        version,flags,channels,reserved,seq,generation,epoch=struct.unpack_from('<BBBBIHH',p)
        values=struct.unpack_from('<8H',p,12)
        if (version,channels,reserved)!=(1,8,0) or flags&~7 or any(v>3300 for v in values):
            raise ValueError('invalid 0x43 fields')
        reasons=set(self.pending);self.pending.clear();prev=self.previous
        if prev is None:reasons.add('capture_start')
        else:
            if seq!=(prev['seq']+1)&0xffffffff:reasons.add('sample_gap')
            if generation!=prev['generation']:reasons.add('generation')
            if epoch!=prev['epoch']:reasons.add('epoch')
            if flags&6!=prev['flags']&6:reasons.add('flags')
            delta=(stamp-prev['stamp'])&0xffffffff
            if delta>250000:reasons.add('timestamp_gap')
            if now-prev['now']>.25:reasons.add('host_gap')
            self.time+=delta/1e6 if 0<delta<=250000 else .001
        if flags&1:reasons.add('discontinuity')
        if reasons:self.segment+=1
        self.previous=dict(seq=seq,generation=generation,epoch=epoch,flags=flags,stamp=stamp,now=now)
        meta=dict(sample_seq=seq,timestamp_us=stamp,generation=generation,epoch=epoch,flags=flags,
                  segment_id=self.segment,frame_seq=frame_seq,
                  break_mask=sum(1<<i for i,name in enumerate(BREAK_REASONS) if name in reasons))
        return np.asarray(values,dtype=np.float32),self.start_time+self.time,meta,bool(reasons)

def recording_arrays(records,complete=True):
    """Original three fields retained; compact samples additionally carry metadata."""
    ts=np.asarray([r[0] for r in records],dtype=np.float64)
    raw=np.asarray([r[1] for r in records],dtype=np.float32)
    labels=np.asarray([r[2] for r in records],dtype=np.int32)
    out=dict(emg=raw,labels=labels,timestamps=ts,
             sample_rate=len(ts)/(ts[-1]-ts[0]) if len(ts)>1 and ts[-1]>ts[0] else 1000.,
             capture_complete=bool(complete))
    if records and len(records[0])==4:
        if any(len(r)!=4 for r in records):raise ValueError('mixed recording schemas')
        for key,dtype in (('sample_seq','uint32'),('timestamp_us','uint32'),('generation','uint16'),
                          ('epoch','uint16'),('flags','uint8'),('segment_id','uint64'),
                          ('frame_seq','uint16'),('break_mask','uint16')):
            out[key]=np.asarray([r[3][key] for r in records],dtype=dtype)
        out.update(sample_rate=1000.,capture_schema='A_RAW_43_V1',channels_already_reordered=True,
                   labels_source='pc_collection_schedule',break_reason_names=np.asarray(BREAK_REASONS))
    return out

"""Offline inspection of actual A USB bytes; no serial or board dependency."""
import argparse
import hashlib
import json
import struct
from pathlib import Path

from a_ab_v2_cli import frame, parse_frames


def decode_classification(data):
    if len(data)!=32 or data[0]!=1 or data[1]&~3 or data[18:20]!=bytes(2) or data[28:32]!=bytes(4):
        raise ValueError('invalid 0x41 schema')
    cid,hint,margin,seq,generation,count,epoch=struct.unpack_from('<HHHIHHH',data,2)
    valid=data[1]&3==3
    if margin>256 or (valid and (not 2<=count<=32 or cid>=count)):
        raise ValueError('invalid 0x41 class/count/margin')
    return dict(valid=valid,class_id=cid if valid else None,reported_class_id=cid,
                action_hint=hint,margin_q8=margin,sample_seq=seq,generation=generation,
                class_count=count,epoch=epoch)


def inspect_capture(raw):
    frames,tail=parse_frames(raw)
    if tail or b''.join(frame(f['type'],f['payload'],f['seq'],f['timestamp']) for f in frames)!=raw:
        raise ValueError('capture has truncation, noise, or unsupported frame envelope')
    segments=[];results=[];statuses=[];counts={};gaps=[];pending_break=[]
    last_frame=None;previous=None
    for f in frames:
        kind=f['type'];data=f['payload'];counts[f'{kind:02x}']=counts.get(f'{kind:02x}',0)+1
        if last_frame is not None and f['seq']!=(last_frame+1)&65535:
            gaps.append(dict(previous=last_frame,current=f['seq']))
            pending_break.append('outer_sequence_gap')
        last_frame=f['seq']
        if kind==0x41:
            results.append(decode_classification(data));continue
        if kind==0x51:
            if len(data)<10:raise ValueError('short 0x51 response')
            version,target,rid,op,status,size=struct.unpack_from('<BBHHHH',data)
            if version!=1 or target not in (1,2) or size!=len(data)-10:raise ValueError('invalid 0x51 header')
            if target==1 and op==2 and status==0:
                statuses.append(json.loads(data[10:]))
            continue
        if kind!=0x43:continue
        if len(data)!=28:raise ValueError('0x43 payload must contain 28 bytes')
        version,flags,channels,reserved,seq,generation,epoch=struct.unpack_from('<BBBBIHH',data)
        values=struct.unpack_from('<8H',data,12)
        if (version,channels,reserved)!=(1,8,0) or flags&~7 or any(v>3300 for v in values):
            raise ValueError('invalid 0x43 fields or raw mV')
        reasons=list(dict.fromkeys(pending_break));pending_break=[]
        if previous is None:reasons.append('capture_start')
        else:
            if seq!=(previous['seq']+1)&0xffffffff:reasons.append('sample_sequence_gap')
            if generation!=previous['generation']:reasons.append('generation_change')
            if epoch!=previous['epoch']:reasons.append('epoch_change')
            if (flags&6)!=(previous['flags']&6):reasons.append('warmup_or_replay_change')
            if (f['timestamp']-previous['timestamp'])&0xffffffff>250000:reasons.append('timestamp_break')
        if flags&1:reasons.append('discontinuity_flag')
        if reasons:
            segments.append(dict(index=len(segments),start_reasons=reasons,generation=generation,epoch=epoch,
                                 warmup=bool(flags&2),replay=bool(flags&4),
                                 eligible_for_labeling=not bool(flags&6),samples=[]))
        segments[-1]['samples'].append(dict(sample_seq=seq,flags=flags,timestamp=f['timestamp'],raw_mv=values))
        previous=dict(seq=seq,generation=generation,epoch=epoch,flags=flags,timestamp=f['timestamp'])
    transitions=[];last=None
    for result in results:
        if not result['valid']:
            last=None;continue
        identity=(result['generation'],result['epoch'],result['class_id'])
        if identity!=last:
            transitions.append({k:result[k] for k in ['sample_seq','class_id','generation','epoch']})
            last=identity
    summary=dict(frames=len(frames),frame_counts=counts,raw_samples=sum(len(s['samples']) for s in segments),
                 outer_sequence_gaps=gaps,classifications=results,stable_transitions=transitions,status_snapshots=statuses,
                 segments=[dict({k:v for k,v in s.items() if k!='samples'},count=len(s['samples']),
                                first_sample_seq=s['samples'][0]['sample_seq'],last_sample_seq=s['samples'][-1]['sample_seq']) for s in segments],
                 nominal_sample_rate_hz=1000,channels_already_reordered=True,labels_inferred=False,
                 no_interpolation=True,hardware_io=False,pc_ui_integration=False)
    return summary,segments


def export_capture(source,dest):
    import numpy as np
    source=Path(source);dest=Path(dest)
    if dest.exists():raise ValueError('output already exists; choose a new directory')
    raw=source.read_bytes();summary,segments=inspect_capture(raw)
    if not segments:raise ValueError('capture contains no raw samples to export')
    dest.mkdir(parents=True)
    files={}
    for segment in segments:
        # Preserve warmup/replay separately, without inventing labels or
        # connecting valid samples across excluded portions of a stream.
        name=f"segment_{segment['index']:04d}.npz";samples=segment['samples']
        np.savez_compressed(dest/name,emg=np.asarray([s['raw_mv'] for s in samples],dtype=np.uint16),
                            sample_seq=np.asarray([s['sample_seq'] for s in samples],dtype=np.uint32),
                            timestamp_us=np.asarray([s['timestamp'] for s in samples],dtype=np.uint32),
                            flags=np.asarray([s['flags'] for s in samples],dtype=np.uint8),
                            generation=np.uint16(segment['generation']),epoch=np.uint16(segment['epoch']),
                            sample_rate_hz=np.uint16(1000))
        files[name]=hashlib.sha256((dest/name).read_bytes()).hexdigest()
    summary.update(source_sha256=hashlib.sha256(raw).hexdigest(),files_sha256=files,
                   note='Unlabeled raw segments; classification outputs are never training labels. Warmup/replay segments require exclusion. No full PC collection/training claim.')
    (dest/'manifest.json').write_text(json.dumps(summary,indent=2),encoding='utf8')
    return summary


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('file',type=Path);p.add_argument('--out',type=Path)
    a=p.parse_args()
    report=export_capture(a.file,a.out) if a.out else inspect_capture(a.file.read_bytes())[0]
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()

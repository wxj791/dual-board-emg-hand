"""A framed telemetry reader for the existing collection/display core."""
import struct
import time
import numpy as np
from gesture_tasks_protocol import Parser
from gesture_tasks_capture import RawTimeline

def clean_frontend():
    import sys
    from pathlib import Path
    backend=str(Path(__file__).resolve().parents[1]/'backend')
    if backend not in sys.path:sys.path.insert(0,backend)
    from pc_ml.fusion import FusionFeatures,advance
    front=FusionFeatures()
    advance(np.empty((0,8),dtype=np.int64),front.coeff,front.state,front.env_state)
    return front,advance

def receive_loop(core):
    parser=Parser(); previous=None; timeline=RawTimeline(); front=None
    last_type=None; schema_errors=0
    try:
        while core.running:
            frames=parser.feed(core.ser.read(max(1,min(core.ser.in_waiting,16384))),detailed=True)
            for kind,p,frame_seq,stamp,errors in frames:
                timeline.observe(frame_seq,errors)
                if kind in (0x51,0x52): core.board_session.receive(kind,p); continue
                if kind==0x41:
                    valid=len(p)==32 and p[0]==1 and not p[1]&~3 and p[18:20]==b'\0\0' and p[28:32]==bytes(4)
                    if valid:
                        cid=int.from_bytes(p[2:4],'little'); count=int.from_bytes(p[14:16],'little')
                        valid=int.from_bytes(p[6:8],'little')<=256 and (p[1]!=3 or (2<=count<=32 and cid<count))
                    if valid:
                        core.board_classification=bytes(p); core.board_classification_time=time.monotonic()
                    else:
                        core.board_classification=None;core.board_classification_time=0
                    continue
                if kind==0x43:
                    if front is None:front,advance=clean_frontend()
                    now=time.monotonic()
                    if last_type is not None and last_type!=kind:timeline.pending.add('format_change')
                    try:raw,sample_time,meta,reset=timeline.raw(p,frame_seq,stamp,now)
                    except ValueError:
                        timeline.pending.add('crc_or_schema');schema_errors+=1;continue
                    if reset:
                        front.reset();core.board_stream_breaks+=1
                    signal,env=advance((raw[None].astype(np.int64)-1650)*256,front.coeff,front.state,front.env_state)
                    filtered=signal[0,:8]/256.;envelope=env[0,8:]/16.
                    core.board_raw_type=kind;core.board_raw_time=now;last_type=kind
                    core.last_data_time=now
                    # Warmup/replay never enter the online model or training labels.
                    if not meta['flags']&6:core.publish_field_samples(raw[None],reset=reset)
                    with core.disp_lock:
                        if reset:core.disp_count=0
                        for buf,point in ((core.disp_buf,raw),(core.filtered_buf,filtered),(core.envelope_buf,envelope)):
                            buf[:-1]=buf[1:];buf[-1]=point
                        core.disp_count=min(500,core.disp_count+1)
                    core.total_received+=1
                    with core.record_lock:
                        if core.is_recording:
                            label=core.current_label if not meta['flags']&6 else -1
                            core.record_buf.append((sample_time,tuple(raw),label,meta))
                    continue
                if kind!=0x40 or len(p)!=104 or p[:3]!=b'\x01\x0f\x08': continue
                timeline.pending.add('format_change');last_type=kind
                flags=p[3]; seq,rate,generation,epoch,reserved=struct.unpack_from('<IHHHH',p,4)
                if flags & ~7 or reserved or rate!=1000: continue
                raw=np.array(struct.unpack_from('<8H',p,16),dtype=np.float32)
                if np.any(raw>3300): continue
                filtered=np.array([int.from_bytes(p[i:i+3],'little',signed=True)/256 for i in range(32,56,3)],dtype=np.float32)
                envelope=np.array([int.from_bytes(p[i:i+3],'little')/16 for i in range(56,80,3)],dtype=np.float32)
                if any(int.from_bytes(p[i:i+3],'little')>=1<<20 for i in range(56,104,3)):continue
                now=time.monotonic()
                core.board_raw_type=kind;core.board_raw_time=now
                reset=bool(flags&1) or previous is None or previous!=(epoch,generation,(seq-1)&0xffffffff) or (core.last_data_time is not None and now-core.last_data_time>.25)
                if reset: core.board_stream_breaks+=1
                previous=(epoch,generation,seq); core.last_data_time=now
                with core.disp_lock:
                    if reset: core.disp_count=0
                    for buf,point in ((core.disp_buf,raw),(core.filtered_buf,filtered),(core.envelope_buf,envelope)):
                        buf[:-1]=buf[1:]; buf[-1]=point
                    core.disp_count=min(500,core.disp_count+1)
                core.total_received+=1
                # 0x40 remains a display format, never mixed into a compact
                # collection or presented as a complete 1kHz training source.
            core.corrupt_frames=parser.errors+schema_errors
            core.board_session.expire()
            now=time.time(); dt=now-core.last_stat_time
            if dt>=.3:
                core.instant_sps=(core.total_received-core.last_stat_count)/dt
                core.last_stat_time=now; core.last_stat_count=core.total_received
    finally:
        core.running=False; core.board_session.reset(False)

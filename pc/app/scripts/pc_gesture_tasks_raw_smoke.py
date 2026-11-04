"""A byte fixture + compact capture -> manually scheduled synthetic training -> A export."""
from pathlib import Path
from datetime import datetime
import importlib.util
import hashlib
import json
import sys
import time
import unittest
from unittest.mock import patch
import numpy as np
ROOT=Path(__file__).resolve().parents[1];STUDIO=ROOT/'EMG_Studio/EMG_Studio'
sys.path[:0]=[str(STUDIO/'emg_ml'),str(STUDIO/'backend'),str(STUDIO/'emg_ml/tests')]
from test_gesture_tasks_raw import consume,packet,studio
from gesture_tasks_capture import recording_arrays
from gesture_tasks_protocol import Parser
from pc_ml.cnn_fusion_field import run

def main():
    out=ROOT/'artifacts/pc_gesture_tasks'/datetime.now().strftime('raw43_%Y%m%d_%H%M%S_%f')
    out.mkdir(parents=True,exist_ok=False)
    fixture=ROOT/'artifacts/a_gesture_tasks/board32_dram_pc_transition/a_to_pc.bin'
    if not fixture.is_file():fixture=ROOT/'evidence/raw43/a_output_fixture.bin'
    if not fixture.is_file():raise SystemExit('A fixture missing; no substitute hardware claim')
    wire=fixture.read_bytes();core,records,published=consume(wire,label=-1)
    arrays=recording_arrays(records)
    frames=Parser().feed(wire)
    raw=[p for kind,p in frames if kind==0x43]
    expected=np.array([np.frombuffer(p,dtype='<u2',offset=12) for p in raw])
    np.testing.assert_array_equal(arrays['emg'],expected)
    assert len(frames)==1267 and len(records)==1216 and len(published)==896 and not core.corrupt_frames
    classifications=[p for kind,p in frames if kind==0x41 and p[1]==3]
    transitions=[];last=None
    for p in classifications:
        cid=int.from_bytes(p[2:4],'little')
        if cid!=last:transitions.append({'class_id':cid,'sample_seq':int.from_bytes(p[8:12],'little')});last=cid
    np.savez_compressed(out/'a_fixture_unlabeled.npz',**arrays)
    # Save a PC-owned read-only fixture copy so the standalone package can repeat the consumer check.
    (out/'a_output_fixture.bin').write_bytes(wire)
    report={'a_source_sha256':hashlib.sha256(wire).hexdigest(),'frames':len(frames),
        'raw_samples':len(records),'valid_classifications':len(classifications),'transitions':transitions,
        'warmup_samples':int(np.sum((arrays['flags']&2)!=0)),'eligible_samples':len(published),
        'segments':len(np.unique(arrays['segment_id'])),'no_inferred_labels':bool(np.all(arrays['labels']==-1)),
        'raw_and_sequence_exact':True,'hardware_io':False,'fixture_is_synthetic':True}
    (out/'a_consumer.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    # Independent artificial collection schedule. No A classifications are present.
    rng=np.random.default_rng(1031);n=1920
    labels=np.concatenate([np.full(n,c,dtype=np.int16) for rep in range(3) for c in range(2)])
    t=np.arange(len(labels))/1000
    raw=1650+rng.normal(0,3,(len(labels),8))
    for c in range(8):raw[:,c]+=(3+labels*(90+12*c))*np.sin(2*np.pi*(43+c*7)*t)
    raw=np.rint(raw).clip(0,3300).astype(np.uint16)
    core=studio.SerialCore();core.board_protocol=True;core.running=True
    core.board_raw_type=0x43;core.board_raw_time=time.monotonic();core.start_recording()
    class ScheduledSource:
        index=0;is_open=True
        @property
        def in_waiting(self):return 42 if self.index<len(raw) else 0
        def read(self,size):
            if self.index==len(raw):core.running=False;return b''
            i=self.index;self.index+=1
            core.set_label(int(labels[i]))  # PC collection prompts, never model predictions.
            return packet(i,values=raw[i],flags=1 if i==0 else 0)
    core.ser=ScheduledSource();core.board_session.reset(True)
    with patch.object(studio.serial,'Serial',side_effect=AssertionError('offline test cannot open serial')):
        core._recv_loop()
    recorded=core.stop_recording();saved=recording_arrays(recorded)
    np.testing.assert_array_equal(saved['labels'],labels);np.testing.assert_array_equal(saved['emg'],raw)
    profile={'version':1,'name':'SYNTHETIC PC-SCHEDULED RAW43','classes':[
        {'id':i,'key':'rest' if i==0 else 'custom','name':'rest' if i==0 else 'synthetic_action',
         'instruction':'offline generated sample','image':'custom.png','output':{'id':0,'name':'hold'}} for i in range(2)]}
    source=out/'synthetic_raw43_recording.npz'
    np.savez_compressed(source,**saved,gestures={c['id']:(c['name'],c['instruction']) for c in profile['classes']},gesture_profile=json.dumps(profile))
    report=run(source,out/'training')
    assert 'heldout' in report['a_deployment'] and 'final' in report['a_deployment'],report['a_deployment']
    print(json.dumps({'a_consumer':json.loads((out/'a_consumer.json').read_text()),
        'training_samples':len(recorded),'deployment':report['a_deployment'],'output':str(out)},indent=2),flush=True)
    return 0
if __name__=='__main__':raise SystemExit(main())

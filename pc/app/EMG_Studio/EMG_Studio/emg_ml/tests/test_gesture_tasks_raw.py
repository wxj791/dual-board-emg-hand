"""Command-only compact stream, annotated recording and training boundaries."""
import importlib.util
import json
from pathlib import Path
import struct
import sys
import tempfile
import time
import unittest
import numpy as np
EMG=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(EMG),str(EMG.parent/'backend')]
spec=importlib.util.spec_from_file_location('raw_studio',EMG/'emg_studio_gui_V3.0.py')
studio=importlib.util.module_from_spec(spec);spec.loader.exec_module(studio)
from gesture_tasks_protocol import frame,Parser
from gesture_tasks_capture import RawTimeline,recording_arrays

def packet(seq,outer=None,stamp=None,flags=0,generation=3,epoch=4,values=None):
    p=struct.pack('<BBBBIHH8H',1,flags,8,0,seq&0xffffffff,generation,epoch,*(values if values is not None else range(1500,1508)))
    raw=bytearray(frame(0x43,p,seq if outer is None else outer))
    raw[8:12]=struct.pack('<I',(seq*1000 if stamp is None else stamp)&0xffffffff)
    import binascii
    raw[-2:]=struct.pack('<H',binascii.crc_hqx(raw[2:-2],65535))
    return bytes(raw)

def consume(wire,label=1,fragment=17):
    core=studio.SerialCore();core.board_protocol=True;core.running=True
    core.board_raw_type=0x43;core.board_raw_time=time.monotonic()
    core.set_label(label);core.start_recording();core.enable_field_stream(True)
    published=[];original=core.publish_field_samples
    core.publish_field_samples=lambda raw,reset=False:(published.append((raw.copy(),reset)),original(raw,reset))
    class Port:
        offset=0;is_open=True
        @property
        def in_waiting(self):return len(wire)-self.offset
        def read(self,count):
            if self.offset==len(wire):core.running=False;return b''
            part=wire[self.offset:self.offset+min(fragment,count)];self.offset+=len(part);return part
    core.ser=Port();core.board_session.reset(True);core._recv_loop()
    return core,core.stop_recording(),published

class CompactTests(unittest.TestCase):
    def test_real_capture_from_a_is_exact_and_never_auto_labeled(self):
        root=EMG.parents[2]
        path=root/'artifacts/a_gesture_tasks/board32_dram_pc_transition/a_to_pc.bin'
        if not path.is_file():path=root/'evidence/raw43/a_output_fixture.bin'
        if not path.is_file():self.skipTest('A fixture not included; see PC-owned consumer evidence')
        wire=path.read_bytes();parser=Parser();frames=parser.feed(wire)
        self.assertEqual(len(frames),1267)
        core,records,published=consume(wire,label=6)
        raw=[p for kind,p in frames if kind==0x43]
        self.assertEqual(core.total_received,1216);self.assertEqual(len(records),1216)
        self.assertEqual(core.corrupt_frames,0)
        arrays=recording_arrays(records)
        np.testing.assert_array_equal(arrays['emg'],np.array([struct.unpack_from('<8H',p,12) for p in raw]))
        np.testing.assert_array_equal(arrays['sample_seq'],[struct.unpack_from('<I',p,4)[0] for p in raw])
        self.assertEqual(sum(arrays['labels']==-1),320);self.assertEqual(sum(arrays['labels']==6),896)
        self.assertEqual(len(published),896);self.assertEqual(len(np.unique(arrays['segment_id'])),2)
        self.assertEqual(int.from_bytes(core.board_classification[2:4],'little'),1)
        self.assertTrue(arrays['channels_already_reordered']);self.assertEqual(arrays['sample_rate'],1000)
        self.assertNotIn(1,np.unique(arrays['labels']))  # A class 1 never becomes the collection label.

    def test_faults_segment_and_corrupt_sample_is_not_filled(self):
        bad=bytearray(packet(2));bad[-1]^=1
        wire=packet(0,flags=2)+packet(1)+bad+packet(3)+packet(4,generation=5)+packet(5,generation=5,epoch=6)+packet(6,flags=4)+packet(7,flags=1)
        core,records,published=consume(wire)
        a=recording_arrays(records)
        self.assertEqual(core.total_received,7);self.assertEqual(core.corrupt_frames,1)
        self.assertEqual(list(a['sample_seq']),[0,1,3,4,5,6,7])
        self.assertEqual(len(np.unique(a['segment_id'])),7)
        self.assertEqual(list(a['labels']),[-1,1,1,1,1,-1,1])
        self.assertTrue(all(reset for _,reset in published))

    def test_roundovers_are_continuous_and_host_gaps_break(self):
        timeline=RawTimeline()
        def take(s,f,t,now):
            timeline.observe(f,0)
            return timeline.raw(Parser().feed(packet(s,outer=f,stamp=t))[0][1],f,t,now)
        self.assertTrue(take(0xffffffff,65535,0xffffff00,1.)[-1])
        self.assertFalse(take(0,0,744,1.001)[-1])
        self.assertTrue(take(1,1,1744,2.)[-1])
        self.assertEqual(timeline.segment,1)

    def test_clean_frontend_and_invalid_classification(self):
        from pc_ml.fusion import FusionFeatures,advance
        wire=b''.join(packet(i) for i in range(8))
        invalid=bytearray(32);invalid[0:2]=b'\x01\x03';struct.pack_into('<H',invalid,2,32);struct.pack_into('<H',invalid,14,2)
        wire+=frame(0x41,bytes(invalid),8)
        core,records,_=consume(wire)
        self.assertIsNone(core.board_classification)
        front=FusionFeatures();raw=np.array([r[1] for r in records],dtype=np.int64)
        signal,env=advance((raw-1650)*256,front.coeff,front.state,front.env_state)
        _,filtered,envelope,*_=core.get_display_snapshots()
        np.testing.assert_array_equal(filtered,signal[:,:8]/256.)
        np.testing.assert_array_equal(envelope,env[:,8:]/16.)

    def test_npz_metadata_and_training_reset_at_every_segment(self):
        from pc_ml.gesture_tasks_recording import stream_edges,extract_segments
        from pc_ml.fusion import extract
        records=[]
        rng=np.random.default_rng(19)
        raw=rng.integers(1400,1700,(1600,8)).astype(np.float32)
        for i,row in enumerate(raw):
            records.append((i/1000,tuple(row),1,dict(sample_seq=i if i<800 else i+3,
                timestamp_us=i*1000,generation=1,epoch=2,flags=0,segment_id=i//800,
                frame_seq=i,break_mask=1 if i in (0,800) else 0)))
        a=recording_arrays(records)
        edges=stream_edges(a,a['emg'],a['labels'],a['timestamps'])
        np.testing.assert_array_equal(edges,[0,800,1600])
        x,ends=extract_segments(raw,edges)
        first,e0=extract(raw[:800]);second,e1=extract(raw[800:])
        np.testing.assert_array_equal(x,np.concatenate([first,second]))
        np.testing.assert_array_equal(ends,np.r_[e0,e1+800])
        self.assertTrue(all(not start<800<end for start,end in zip(ends-256,ends)))
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'capture.npz';np.savez_compressed(path,**a)
            with np.load(path,allow_pickle=False) as z:
                for key in a:np.testing.assert_array_equal(z[key],a[key])
        a['segment_id'][:]=0
        with self.assertRaisesRegex(ValueError,'没有分段'):stream_edges(a,a['emg'],a['labels'],a['timestamps'])
        a['segment_id'][800:]=1;a['flags'][0]=2
        with self.assertRaisesRegex(ValueError,'不能具有训练标签'):stream_edges(a,a['emg'],a['labels'],a['timestamps'])

if __name__=='__main__':unittest.main()

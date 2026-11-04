"""Offline fault injection. Never opens a serial port or drives an actuator."""
import copy
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import zlib
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from gesture_tasks_protocol import Binding, Parser, Session, frame, rows_bytes, map_commands, task_state, range_command
from gesture_tasks_deploy import Deployment, capture_and_verify

def state_bytes(**kw):
    d=dict(schema=1,display_enabled=1,mechanical_armed=0,teach_mode=0,generation=4,class_count=2,
           map_revision=0,mapping_valid=1,motion_state=0,active_mask=0,retired_mask=0,
           pose_valid_mask=511,flash_ready=1,page=1,range_mv=200,reserved=0,fault_flags=0)
    d.update(kw)
    return struct.pack('<BBBBHHIBBBBHBBHHI',*d.values())

def response(target,rid,op,status=0,data=b''):
    return struct.pack('<BBHHHH',1,target,rid,op,status,len(data))+data

class MockBoards:
    def __init__(self, model=None):
        self.session=Session(self.write); self.session.reset(True); self.sent=[]
        self.gen=4; self.k=2; self.rev=0; self.rows={}; self.stage={}; self.flash_failure=False
        self.teach=1; self.armed=0; self.fail=None; self.bad_read=False; self.model=model
        self.bank_words=[[] for _ in range(8)]; self.meta=b''; self.no_b=False
        self.crc='12345678';self.state_override={};self.results=0
    def write(self,wire):
        kind,p=Parser().feed(wire)[0]
        ver,target,rid,op,size=struct.unpack_from('<BBHHH',p); data=p[8:]
        assert len(data)==size and kind==0x50
        self.sent.append((target,op,data))
        if target==2 and self.no_b:return
        status=0; result=b''
        if self.fail==(target,op):status=7
        elif op==1: result=json.dumps({'capacity':32,'flash':False}).encode()
        elif target==1:
            if op==2:
                self.results=(self.results+1)&0xffffffff
                result=json.dumps({'valid':1,'loading':0,'model_crc32_valid':1,'model_crc32_hex':self.crc,
                                        'generation_hex':f'{self.gen:04x}','active_hex':f'{self.k:04x}',
                                        'results_hex':f'{self.results:08x}',**self.state_override}).encode()
            elif op==0x102: self.meta=data[4:]; self.k=json.loads(self.meta)['classes']; self.bank_words=[[] for _ in range(8)]
            elif op==0x103:
                bank,addr,count=struct.unpack_from('<BIH',data)
                assert addr==len(self.bank_words[bank]); self.bank_words[bank].extend(struct.unpack_from('<'+'I'*count,data,7))
            elif op==0x104:
                crc=zlib.crc32(self.meta)
                for bank,values in enumerate(self.bank_words):
                    for addr,v in enumerate(values):crc=zlib.crc32(bytes([bank])+addr.to_bytes(4,'little')+v.to_bytes(4,'little'),crc)
                assert data==crc.to_bytes(4,'little'); self.gen=(self.gen+1)&65535;self.crc=f'{crc:08x}'
        elif op==0x604: result=state_bytes(generation=self.gen,class_count=self.k,map_revision=self.rev,teach_mode=self.teach,mechanical_armed=self.armed)
        elif op==0x600: self.stage={}; self.stage_gen,self.stage_k,self.stage_rev=struct.unpack('<HHI',data)
        elif op==0x601: self.stage[int.from_bytes(data[:2],'little')]=data
        elif op==0x602:
            revision,crc=struct.unpack('<II',data)
            assert sorted(self.stage)==list(range(self.stage_k))
            assert self.stage_gen==self.gen and self.stage_k==self.k and revision==self.stage_rev
            assert zlib.crc32(b''.join(self.stage[i] for i in sorted(self.stage)))==crc
            self.rows=copy.deepcopy(self.stage); self.rev=revision; self.armed=0
        elif op==0x603: result=self.rows[int.from_bytes(data,'little')]; result=b'bad' if self.bad_read else result
        elif op==0x300: self.armed=0; self.teach=0
        elif op==0x301: self.armed=1
        elif op==0x605: self.teach=data[0]; self.armed=0
        elif op==0x304 and self.flash_failure:status=7
        elif op==0x30a: result=b'\x01'+struct.pack('<5H',100,200,300,400,500)+b'\x01\x01'
        if (op in (0x300,0x304,0x305,0x605,0x606) and target==2 and not status
                and not (op==0x605 and data==b'\x00')):
            self.session.receive(0x51,response(target,rid,op,1))
            self.session.receive(0x52,response(target,rid,op,0,result))
        else:self.session.receive(0x51,response(target,rid,op,status,result))

class WireTests(unittest.TestCase):
    def test_teach_exit_finishes_on_recorded_ack_but_enter_waits_for_final(self):
        s=Session(lambda _:None);s.reset(True)
        pending=s.send(2,0x605,b'\x00',rid=41)
        # Real reply captured at 11:56:35.042: synchronous success, no event.
        s.receive(0x51,bytes.fromhex('01022900050600000000'))
        self.assertTrue(pending.event.is_set());self.assertEqual(pending.phase,'complete')
        entered=s.send(2,0x605,b'\x01')
        s.receive(0x51,response(2,entered.rid,0x605,1))
        self.assertFalse(entered.event.is_set());self.assertEqual(entered.phase,'accepted')
        s.receive(0x52,response(2,entered.rid,0x605))
        self.assertTrue(entered.event.is_set());self.assertEqual(entered.phase,'complete')

    def test_crc_golden_and_all_disabled_rows(self):
        rows=[Binding(0),Binding(1,1,True,8),Binding(2,2,True)]
        self.assertEqual(rows_bytes(rows).hex(),'000000000000000001000101080000000200020100000000')
        self.assertEqual(zlib.crc32(rows_bytes(rows)),0x79b19a7a)
        for k in (2,7,14,32):
            commands=list(map_commands(0x1234,0x12345678,[Binding(i) for i in range(k)]))
            self.assertEqual(len(commands),k+2)
            self.assertEqual(commands[0][1],struct.pack('<HHI',0x1234,k,0x12345678))
            self.assertEqual(commands[-1][1],struct.pack('<II',0x12345678,zlib.crc32(rows_bytes([Binding(i) for i in range(k)]))))
    def test_invalid_bindings_and_ranges(self):
        for r in (Binding(0,1,True,0),Binding(1,1,True,9),Binding(1,2,True,1),Binding(1,7),Binding(1,0,True),Binding(32)):
            with self.assertRaises(ValueError):r.pack()
        for k in (1,33):
            with self.assertRaises(ValueError):rows_bytes([Binding(i) for i in range(k)])
        with self.assertRaises(ValueError):range_command(4,255,200)
        with self.assertRaises(ValueError):range_command(1,8,200)
        with self.assertRaises(ValueError):range_command(1,255,201)
        self.assertEqual(range_command(1,255,200),b'\x01\xff\xc8\x00')
    def test_fragmentation_crc_recovery_and_bounds(self):
        parser=Parser(); good=frame(0x51,response(2,17,0x604,data=state_bytes()))
        bad=bytearray(good); bad[-1]^=1
        result=[]
        for b in b'garbage'+bad+good:result+=parser.feed(bytes([b]))
        self.assertEqual(result,[(0x51,response(2,17,0x604,data=state_bytes()))]);self.assertEqual(parser.errors,1)
        self.assertLess(len(parser.buffer),14)
        self.assertEqual(task_state(state_bytes())['range_mv'],200)
        with self.assertRaises(ValueError):task_state(state_bytes(motion_state=2,active_mask=2))
    def test_interleaved_targets_duplicate_ids_async_and_timeout(self):
        now=[0.]; s=Session(lambda w:None,lambda:now[0]);s.reset(True)
        a=s.send(1,2,rid=12); b=s.send(2,0x304,rid=13)
        s.receive(0x51,response(2,12,2));self.assertFalse(a.event.is_set())
        s.receive(0x51,response(1,12,2,data=b'A'));self.assertEqual(a.data,b'A')
        s.receive(0x51,response(2,13,0x304,1));self.assertEqual(b.phase,'accepted')
        s.receive(0x51,response(2,13,0x304,0));self.assertFalse(b.event.is_set())
        with self.assertRaises(ValueError):s.send(2,0x304,rid=13)
        s.receive(0x52,response(2,13,0x304,0,b'final'));self.assertEqual(b.data,b'final')
        p=s.send(2,0x606,timeout=2);now[0]=3;s.expire();self.assertEqual(p.phase,'unknown')
        s.receive(0x52,response(2,p.rid,p.opcode));self.assertEqual(p.phase,'unknown')
        q=s.send(2,0x305);epoch=s.epoch;s.reset(False)
        self.assertEqual(q.phase,'unknown');self.assertNotEqual(epoch,s.epoch)
    def test_flash_failure_is_not_saved(self):
        m=MockBoards();m.flash_failure=True
        with self.assertRaisesRegex(RuntimeError,'失败'):capture_and_verify(m.session,0)
        self.assertNotIn(0x30a,[op for _,op,_ in m.sent])
        m.flash_failure=False
        self.assertTrue(capture_and_verify(m.session,8)['valid'])

    def test_incompatible_pose_query_is_rejected(self):
        from gesture_tasks_deploy import query_pose
        class S:
            def request(self,*args):return bytes(16)
        with self.assertRaisesRegex(ValueError,'POSE_QUERY'):query_pose(S(),0)

class DeploymentTests(unittest.TestCase):
    def test_frozen_nonzero_classifications_do_not_allow_upload_or_arm(self):
        from unittest.mock import patch
        from gesture_tasks_deploy import ArmPreconditionError
        with tempfile.TemporaryDirectory() as root,patch('gesture_tasks_deploy.time.sleep'):
            m=MockBoards();d=Deployment(m.session,Path(root)/'s');d.select(self.model(2))
            m.crc=d.model['transaction_crc32'];m.state_override={'results_hex':'00001d29'}
            with patch('gesture_tasks_deploy.time.monotonic',side_effect=[0,10]):
                with self.assertRaisesRegex(ValueError,'计数未增长'):d.upload()
            self.assertFalse(any(op in (0x600,0x301) for _,op,_ in m.sent))
            m.state_override={};d.upload();m.sent.clear()
            m.state_override={'results_hex':'00001d29'}
            with patch('gesture_tasks_deploy.time.monotonic',side_effect=[0,10]):
                with self.assertRaises(ArmPreconditionError) as caught:d.arm()
            self.assertTrue(caught.exception.preserve_verified_bindings)
            self.assertTrue(d.verified);self.assertFalse(any(op==0x301 for _,op,_ in m.sent))
            m.state_override={};m.results=0x1d29
            d.arm()
            self.assertEqual(sum(op==0x301 for _,op,_ in m.sent),1)

    def test_counter_wrap_is_live_but_backward_counter_is_not(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root,patch('gesture_tasks_deploy.time.sleep'):
            m=MockBoards();d=Deployment(m.session,Path(root)/'s');d.select(self.model(2))
            m.crc=d.model['transaction_crc32'];m.results=0xfffffffe
            d.wait_for_classification(m.gen)
            self.assertEqual(m.results,0)
            original=m.session.request;values=iter(('00000100','00000001'))
            def backward(*args,**kwargs):
                m.state_override['results_hex']=next(values)
                return original(*args,**kwargs)
            with patch.object(m.session,'request',side_effect=backward):
                with self.assertRaisesRegex(ValueError,'倒退'):d.wait_for_classification(m.gen)

    def test_arm_rejection_is_not_repeated_and_identity_failures_invalidate(self):
        from unittest.mock import patch
        from gesture_tasks_deploy import ArmPreconditionError
        from gesture_tasks_protocol import Pending,BoardRequestError
        with tempfile.TemporaryDirectory() as root,patch('gesture_tasks_deploy.time.sleep'):
            m=MockBoards();d=Deployment(m.session,Path(root)/'s');d.select(self.model(2));d.upload()
            original=m.session.request
            def rejected(target,op,*args,**kwargs):
                if (target,op)==(2,0x301):
                    pending=Pending(2,999,op,False,0);pending.phase='failed';pending.status=5
                    raise BoardRequestError(pending)
                return original(target,op,*args,**kwargs)
            with patch.object(m.session,'request',side_effect=rejected) as request:
                with self.assertRaises(ArmPreconditionError) as caught:d.arm()
                self.assertIsInstance(caught.exception.__cause__,BoardRequestError)
                self.assertEqual(sum(c.args[:2]==(2,0x301) for c in request.call_args_list),1)
            self.assertTrue(d.verified);self.assertFalse(m.armed)
            m.bad_read=True
            with self.assertRaises(RuntimeError):d.arm()
            self.assertFalse(d.verified)

    def test_arm_teach_motion_connection_and_unknown_outcomes(self):
        from unittest.mock import patch
        from gesture_tasks_deploy import ArmPreconditionError
        from gesture_tasks_protocol import Pending,BoardRequestError
        with tempfile.TemporaryDirectory() as root,patch('gesture_tasks_deploy.time.sleep'):
            m=MockBoards();d=Deployment(m.session,Path(root)/'s');d.select(self.model(2));d.upload();m.sent.clear()
            m.teach=1
            with self.assertRaisesRegex(ArmPreconditionError,'退出示教'):d.arm()
            self.assertTrue(d.verified);self.assertFalse(any(op==0x301 for _,op,_ in m.sent))
            m.teach=0;original=m.session.request
            def changed(*args,**kwargs):
                data=original(*args,**kwargs)
                if args[:2]==(1,2):m.session.reset(True)
                return data
            with patch.object(m.session,'request',side_effect=changed):
                with self.assertRaises(ConnectionError):d.arm()
            self.assertFalse(d.verified)
            d.upload()
            def unknown(target,op,*args,**kwargs):
                if (target,op)==(2,0x301):
                    pending=Pending(2,999,op,False,0);pending.phase='unknown';pending.error='timeout'
                    raise BoardRequestError(pending)
                return original(target,op,*args,**kwargs)
            with patch.object(m.session,'request',side_effect=unknown) as request:
                with self.assertRaises(BoardRequestError):d.arm()
                self.assertEqual(sum(c.args[:2]==(2,0x301) for c in request.call_args_list),1)
            self.assertFalse(d.verified)

    def test_waits_for_first_classification_and_retries_only_rejected_begin(self):
        from unittest.mock import patch
        class DelayedBoards(MockBoards):
            def __init__(self):
                super().__init__();self.polls=0;self.begin_attempts=0
            def write(self,wire):
                _,p=Parser().feed(wire)[0]
                _,target,rid,op,_=struct.unpack_from('<BBHHH',p)
                if target==1 and op==2 and self.gen==5:
                    self.polls+=1
                    self.state_override['results_hex']=f'{max(0,self.polls-3):08x}'
                if target==2 and op==0x600:
                    self.begin_attempts+=1
                    assert self.polls>=4
                    if self.begin_attempts==1:
                        self.sent.append((target,op,p[8:]))
                        self.session.receive(0x51,response(target,rid,op,8));return
                super().write(wire)
        with tempfile.TemporaryDirectory() as root,patch('gesture_tasks_deploy.time.sleep'):
            m=DelayedBoards();d=Deployment(m.session,Path(root)/'s');d.select(self.model(2));d.upload()
            self.assertTrue(d.verified);self.assertEqual(m.begin_attempts,2)
            self.assertEqual(sum(op==0x104 for _,op,_ in m.sent),1)
            self.assertEqual(sum(op==0x602 for _,op,_ in m.sent),1)
            self.assertFalse(m.armed)

    def test_failed_b_sync_can_resume_without_rewriting_a_and_unknown_is_not_retried(self):
        from unittest.mock import patch
        class RejectingBoards(MockBoards):
            reject=True
            def write(self,wire):
                _,p=Parser().feed(wire)[0]
                _,target,rid,op,_=struct.unpack_from('<BBHHH',p)
                if self.reject and target==2 and op==0x600:
                    self.sent.append((target,op,p[8:]))
                    self.session.receive(0x51,response(target,rid,op,8));return
                super().write(wire)
        with tempfile.TemporaryDirectory() as root,patch('gesture_tasks_deploy.time.sleep'):
            m=RejectingBoards();d=Deployment(m.session,Path(root)/'s');d.select(self.model(2))
            with self.assertRaisesRegex(ValueError,'B尚未确认'):d.upload()
            self.assertTrue(d.a_verified);self.assertFalse(d.verified)
            self.assertEqual(sum(op==0x600 for _,op,_ in m.sent),3)
            m.reject=False;m.sent.clear();d.upload()
            self.assertTrue(d.verified)
            self.assertFalse(any(target==1 and op in (0x102,0x103,0x104) for target,op,_ in m.sent))
            self.assertFalse(m.armed)
            m.reject=True;m.sent.clear()
            original=m.session.request
            from gesture_tasks_protocol import Pending,BoardRequestError
            def unknown(target,op,*args,**kwargs):
                if target==2 and op==0x600:
                    pending=Pending(2,999,op,False,0);pending.phase='unknown';pending.error='timeout'
                    raise BoardRequestError(pending)
                return original(target,op,*args,**kwargs)
            with patch.object(m.session,'request',side_effect=unknown) as request:
                with self.assertRaises(BoardRequestError):d.upload()
                self.assertEqual(sum(c.args[:2]==(2,0x600) for c in request.call_args_list),1)

    def test_no_classification_or_model_changed_cannot_submit_b(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root,patch('gesture_tasks_deploy.time.sleep'):
            m=MockBoards();d=Deployment(m.session,Path(root)/'s');d.select(self.model(2))
            m.crc=d.model['transaction_crc32'];m.state_override={'results_hex':'00000000'}
            with patch('gesture_tasks_deploy.time.monotonic',side_effect=[0,10]):
                with self.assertRaisesRegex(ValueError,'尚未恢复识别'):d.upload()
            self.assertFalse(any(op==0x600 for _,op,_ in m.sent))
            m.state_override={'results_hex':'bad'}
            with self.assertRaisesRegex(ValueError,'无法确认'):d.wait_for_classification(m.gen)
            m.state_override={'generation_hex':'0008'}
            with self.assertRaisesRegex(ValueError,'发生变化'):d.wait_for_classification(m.gen)

    def test_crc_identity_missing_malformed_or_loading_blocks_model_write(self):
        with tempfile.TemporaryDirectory() as root:
            for index,override in enumerate(({'model_crc32_hex':None},{'model_crc32_hex':'ABCDEF01'},
                {'model_crc32_valid':True},{'loading':1},{'valid':0},{'model_crc32_valid':0})):
                m=MockBoards();m.state_override=override;d=Deployment(m.session,Path(root)/str(index));d.select(self.model(2))
                with self.assertRaises(ValueError):d.upload()
                self.assertNotIn((1,0x102),[(t,o) for t,o,_ in m.sent])

    def test_wrong_committed_model_crc_prevents_b_commit_and_arm(self):
        with tempfile.TemporaryDirectory() as root:
            m=MockBoards();m.state_override={'model_crc32_hex':'00000000'}
            d=Deployment(m.session,Path(root)/'s');d.select(self.model(2))
            with self.assertRaisesRegex(RuntimeError,'CRC'):d.upload()
            self.assertNotIn((2,0x600),[(t,o) for t,o,_ in m.sent])
            self.assertFalse(d.verified)

    def model(self,k):
        sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'backend'))
        from pc_ml.gesture_tasks_export import metadata,transaction_crc
        banks=[[0]*(304*k),[0]*304,[0]*(2*k),[0]*1280,[0]*160,[0]*(32*k),[0]*(2*k),[0]*k]
        banks[4][152:154]=[15,2097152]
        return {'active_classes':k,'class_names':[f'class{i}' for i in range(k)],'source_sha256':str(k)*64,'banks':banks,
                'transaction_crc32':f'{transaction_crc(metadata(k),banks):08x}'}
    def test_deployment_order_readback_explicit_arm_and_restart(self):
        with tempfile.TemporaryDirectory() as root:
            for k in (2,7,14,32):
                mock=MockBoards();d=Deployment(mock.session,Path(root)/str(k));d.select(self.model(k))
                d.edit([Binding(0)]+[Binding(i,4,True) for i in range(1,k)])
                d.upload();self.assertTrue(d.verified);self.assertFalse(mock.armed)
                ops=[(t,op) for t,op,_ in mock.sent]
                self.assertLess(ops.index((2,0x300)),ops.index((1,0x102)))
                self.assertLess(ops.index((1,0x104)),ops.index((2,0x600)))
                d.arm();self.assertTrue(mock.armed)
                mock.session.reset(True)
                with self.assertRaises(RuntimeError):d.arm()
                reopened=Deployment(mock.session,Path(root)/str(k));reopened.select(self.model(k))
                self.assertFalse(reopened.verified)
    def test_error_readback_and_power_mismatch_cannot_arm(self):
        with tempfile.TemporaryDirectory() as root:
            m=MockBoards();d=Deployment(m.session,Path(root)/'s');d.select(self.model(2))
            m.fail=(1,0x104)
            with self.assertRaises(RuntimeError):d.upload()
            self.assertNotIn((2,0x600),[(t,o) for t,o,_ in m.sent]);self.assertFalse(d.verified)
            m.fail=None;m.bad_read=True
            with self.assertRaises(RuntimeError):d.upload()
            with self.assertRaises(RuntimeError):d.arm()
            m.bad_read=False;d.upload();m.gen=0
            with self.assertRaises(RuntimeError):d.arm()
    def test_deleted_classes_clear_mapping_and_rest_stays_disabled(self):
        with tempfile.TemporaryDirectory() as root:
            d=Deployment(MockBoards().session,Path(root)/'s');d.select(self.model(14))
            d.edit([Binding(0)]+[Binding(i,1,True,8) for i in range(1,14)])
            d.select(self.model(2));self.assertEqual(d.rows,[Binding(0),Binding(1)])

    def test_no_b_return_prevents_a_model_update(self):
        with tempfile.TemporaryDirectory() as root:
            m=MockBoards();m.no_b=True;now=[0]
            def clock():now[0]+=6;return now[0]
            m.session.clock=clock
            d=Deployment(m.session,Path(root)/'s');d.select(self.model(2))
            with self.assertRaisesRegex(RuntimeError,'未知'):d.upload()
            self.assertNotIn((1,0x102),[(t,o) for t,o,_ in m.sent])

    def test_local_crc_mismatch_cannot_restore_bindings(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'s';d=Deployment(MockBoards().session,path);d.select(self.model(7))
            saved=json.loads(path.read_text(encoding='utf8'));saved['bindings'][1]={'class_id':1,'kind':4,'enabled':True,'argument':0}
            path.write_text(json.dumps(saved),encoding='utf8')
            with self.assertRaisesRegex(ValueError,'CRC'):Deployment(MockBoards().session,path).select(self.model(7))

if __name__=='__main__':unittest.main()

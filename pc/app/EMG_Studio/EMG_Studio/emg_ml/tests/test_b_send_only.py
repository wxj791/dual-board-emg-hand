"""Send-only B regressions. No serial ports or physical actuators."""
import ast
import json
from pathlib import Path
import struct
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from gesture_tasks_protocol import Session, Parser, BoardRequestError, Pending
from gesture_tasks_deploy import Deployment
import test_gesture_tasks as fixtures

class BSendOnlyTests(unittest.TestCase):
    def test_no_reply_no_pending_no_timeout_and_same_wire_format(self):
        writes=[];s=Session(writes.append);s.reset(True)
        result=s.send_only(0x201,bytes([1]))
        self.assertTrue(result['sent_only']);self.assertEqual(s.pending,{})
        s.expire();self.assertEqual(len(writes),1)
        kind,p=Parser().feed(writes[0])[0]
        self.assertEqual(kind,0x50)
        self.assertEqual(struct.unpack_from('<BBHHH',p),(1,2,result['request_id'],0x201,1))
        self.assertEqual(p[8:],bytes([1]))

    def test_write_failure_is_reported_without_retry(self):
        calls=[]
        def fail(wire):calls.append(wire);raise OSError('write failed')
        s=Session(fail);s.reset(True)
        with self.assertRaises(BoardRequestError):s.send_only(0x300)
        self.assertEqual(len(calls),1);self.assertEqual(s.pending,{})

    def test_ignoring_b_does_not_hide_a_reply(self):
        s=Session(lambda _:None);s.reset(True);s.ignore_b_replies=True
        a=s.send(1,2)
        s.receive(0x51,fixtures.response(2,a.rid,2))
        self.assertFalse(a.event.is_set())
        s.receive(0x51,fixtures.response(1,a.rid,2,data=b'A'))
        self.assertTrue(a.event.is_set());self.assertEqual(a.data,b'A')

    def test_upload_confirms_commit_without_b_status_or_row_readback(self):
        with tempfile.TemporaryDirectory() as tmp:
            mock=fixtures.MockBoards()
            d=Deployment(mock.session,Path(tmp)/'state.json',b_send_only=True)
            d.select(fixtures.DeploymentTests().model(2))
            with patch('gesture_tasks_protocol.time.sleep'):
                result=d.upload();armed=d.arm()
            self.assertTrue(result['b_commit_confirmed']);self.assertEqual(armed,b'')
            bops=[op for target,op,data in mock.sent if target==2]
            self.assertEqual(bops,[0x300,0x600,0x601,0x601,0x602,0x301])
            self.assertFalse(d.verified);self.assertTrue(d.a_verified)
            self.assertEqual(json.loads((Path(tmp)/'state.json').read_text(encoding='utf8'))['deployment_status'],'b_commit_confirmed')
            mock.session.reset(True)
            with self.assertRaises(RuntimeError):d.arm()

    def test_busy_commit_is_not_repeated_or_claimed_as_uploaded(self):
        class BusyBoards(fixtures.MockBoards):
            rejects=3
            def write(self,wire):
                _,p=Parser().feed(wire)[0]
                _,target,rid,op,_=struct.unpack_from('<BBHHH',p)
                if target==2 and op==0x602 and self.rejects:
                    self.rejects-=1;self.sent.append((target,op,p[8:]))
                    self.session.receive(0x51,fixtures.response(target,rid,op,4));return
                super().write(wire)
        with tempfile.TemporaryDirectory() as tmp,patch('gesture_tasks_protocol.time.sleep'):
            mock=BusyBoards();d=Deployment(mock.session,Path(tmp)/'state',b_send_only=True)
            d.select(fixtures.DeploymentTests().model(2))
            with self.assertRaises(BoardRequestError):d.upload()
            self.assertFalse(d.b_mapping_sent);self.assertEqual(len(mock.rows),0)
            self.assertEqual(sum(op==0x602 for _,op,_ in mock.sent),1)
            self.assertFalse(any(op in (0x603,0x604,0x301) for _,op,_ in mock.sent))
            mock.fail=(2,0x602)
            with self.assertRaises(BoardRequestError):d.upload()
            self.assertFalse(d.b_mapping_sent)

    def test_motion_is_never_repeated_after_acceptance_or_unknown_or_reconnect(self):
        for phase,status,accepted in (('unknown',None,False),('failed',7,False),
                                      ('accepted',4,True),('failed',4,True)):
            s=Session(lambda _:None);s.reset(True)
            p=Pending(2,1,0x305,True,0);p.phase=phase;p.status=status;p.accepted=accepted;p.error='injected'
            with patch.object(s,'request',side_effect=BoardRequestError(p)) as request:
                with self.assertRaises(BoardRequestError):s.request_b_when_idle(0x305,b'\x00\x00')
                self.assertEqual(request.call_count,1)
        s=Session(lambda _:None);s.reset(True)
        def reconnect(*_):s.reset(True)
        p=Pending(2,1,0x305,True,0);p.phase='failed';p.status=4;p.error='busy'
        s._last_b_send=__import__('time').monotonic()
        with patch.object(s,'request',side_effect=BoardRequestError(p)) as request,patch('gesture_tasks_protocol.time.sleep',side_effect=reconnect):
            with self.assertRaises(ConnectionError):s.request_b_when_idle(0x305)
            self.assertEqual(request.call_count,0)

    def test_arm_is_sent_once_for_success_and_rejection(self):
        s=Session(lambda _:None);s.reset(True)
        p=Pending(2,1,0x301,False,0);p.phase='failed';p.status=5;p.error='not ready'
        with patch('gesture_tasks_protocol.time.sleep'),patch.object(s,'request',return_value=b'') as request:
            self.assertEqual(s.request_b_when_idle(0x301,b'\x01\x00'),b'')
            self.assertEqual(request.call_count,1)
        with patch('gesture_tasks_protocol.time.sleep'),patch.object(s,'request',side_effect=BoardRequestError(p)) as request:
            with self.assertRaises(BoardRequestError):s.request_b_when_idle(0x301,b'\x01\x00')
            self.assertEqual(request.call_count,1)

    def test_stop_does_not_wait_for_previous_send_gap(self):
        s=Session(lambda _:None);s.reset(True)
        s._last_b_send=__import__('time').monotonic()
        with patch('gesture_tasks_protocol.time.sleep') as sleep,patch.object(s,'request',return_value=b'') as request:
            s.request_b_when_idle(0x300)
            sleep.assert_not_called();request.assert_called_once_with(2,0x300,b'',timeout=15)

    def test_display_stays_direct_and_servo_reports_board_results(self):
        tree=ast.parse((Path(__file__).resolve().parents[1]/'gesture_tasks_ui.py').read_text(encoding='utf8'))
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='GestureTasksPanel')
        methods=[n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name in ('query','command','teach_mode')]
        started=[];sent=[]
        class Thread:
            def __init__(self,**kw):self.kw=kw
            def start(self):started.append(self.kw['args'][0])
        ns={'threading':types.SimpleNamespace(Thread=Thread),'time':types.SimpleNamespace(monotonic=lambda:10),
            'task_state':fixtures.task_state}
        exec(compile(ast.Module(body=methods,type_ignores=[]),'<UI methods>','exec'),ns)
        requests=[]
        def request(*args,**kwargs):
            requests.append(args)
            return fixtures.state_bytes() if args[1]==0x604 else b''
        session=types.SimpleNamespace(connected=True,epoch=1,send_only=lambda *args:sent.append(args),request=request,
                                     request_b_when_idle=lambda op,data,**kw:request(2,op,data,**kw))
        obj=types.SimpleNamespace(deploy=types.SimpleNamespace(session=session),busy=0,querying=set(),last_query={},query_generation=0,query_endpoint=lambda *args:None)
        ns['query'](obj);self.assertEqual(started,[1])
        obj.command=lambda *args:ns['command'](obj,*args)
        ns['command'](obj,0x201,bytes([1]))
        self.assertEqual(sent,[(0x201,bytes([1]))]);self.assertEqual(requests,[])
        result=ns['teach_mode'](obj,True)
        self.assertEqual(requests,[(2,0x605,bytes([1]))])
        self.assertEqual(result,{'command_completed':True,'opcode':0x605})

    def test_record_completes_teach_save_exit_without_motion_and_stops_on_failure(self):
        tree=ast.parse((Path(__file__).resolve().parents[1]/'gesture_tasks_ui.py').read_text(encoding='utf8'))
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='GestureTasksPanel')
        method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='record_pose')
        calls=[]
        session=types.SimpleNamespace(epoch=1)
        def request(op,data,**kw):calls.append((op,data))
        session.request_b_when_idle=request
        pose={'valid':True,'positions':[100,200,300,400,500],'flash_available':True,'flash_initialized':True}
        ns={'time':types.SimpleNamespace(sleep=lambda _:None),'struct':struct,
            'query_pose':lambda s,slot:pose}
        exec(compile(ast.Module(body=[method],type_ignores=[]),'<record method>','exec'),ns)
        obj=types.SimpleNamespace(deploy=types.SimpleNamespace(session=session))
        self.assertEqual(ns['record_pose'](obj,0),pose)
        self.assertEqual(calls,[(0x605,b'\x01'),(0x304,b'\x00\x00'),(0x605,b'\x00')])
        calls.clear()
        def failed(op,data,**kw):
            calls.append((op,data))
            if op==0x304:raise RuntimeError('flash failure')
        session.request_b_when_idle=failed
        with self.assertRaisesRegex(RuntimeError,'flash failure'):ns['record_pose'](obj,0)
        self.assertEqual(calls,[(0x605,b'\x01'),(0x304,b'\x00\x00')])

    def test_record_real_session_handles_synchronous_teach_exit(self):
        from gesture_tasks_deploy import query_pose
        tree=ast.parse((Path(__file__).resolve().parents[1]/'gesture_tasks_ui.py').read_text(encoding='utf8'))
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='GestureTasksPanel')
        method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='record_pose')
        ns={'time':types.SimpleNamespace(sleep=lambda _:None),'struct':struct,'query_pose':query_pose}
        exec(compile(ast.Module(body=[method],type_ignores=[]),'<record wire integration>','exec'),ns)
        mock=fixtures.MockBoards()
        obj=types.SimpleNamespace(deploy=types.SimpleNamespace(session=mock.session))
        pose=ns['record_pose'](obj,0)
        self.assertTrue(pose['valid']);self.assertEqual(pose['positions'],[100,200,300,400,500])
        self.assertEqual(mock.teach,0)
        self.assertEqual(mock.sent,[(2,0x605,b'\x01'),(2,0x304,b'\x00\x00'),
                                    (2,0x605,b'\x00'),(2,0x30a,b'\x00\x00')])

if __name__=='__main__':unittest.main()

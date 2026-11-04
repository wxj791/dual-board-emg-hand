"""Offline communication tracing and read-only bridge diagnosis."""
import ast
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from gesture_tasks_protocol import Session

class CommunicationDiagnostics(unittest.TestCase):
    def test_write_trace_is_not_board_ack_and_does_not_retry(self):
        writes=[];s=Session(writes.append);s.reset(True)
        with patch('gesture_tasks_protocol.wire_trace') as trace:
            pending=s.send(2,0x604)
        self.assertEqual(len(writes),1)
        self.assertEqual(pending.phase,'sent')
        self.assertFalse(pending.event.is_set())
        self.assertEqual([c.args[0] for c in trace.call_args_list],['board_tx_attempt','board_tx_written'])

    def test_write_failure_never_claims_written(self):
        def fail(_):raise OSError('port failure')
        s=Session(fail);s.reset(True)
        with patch('gesture_tasks_protocol.wire_trace') as trace:p=s.send(2,0x201,b'\x01')
        self.assertEqual(p.phase,'unknown')
        self.assertEqual([c.args[0] for c in trace.call_args_list],['board_tx_attempt'])

    def test_bridge_read_is_rate_limited_and_decodes_actual_bit_positions(self):
        module=ast.parse((Path(__file__).resolve().parents[1]/'gesture_tasks_ui.py').read_text(encoding='utf8'))
        method=next(n for c in module.body if isinstance(c,ast.ClassDef) for n in c.body if isinstance(n,ast.FunctionDef) and n.name=='query_bridge_after_timeout')
        clock=[100.0];calls=[];records=[]
        value=(7<<224)|(4<<96)|(3<<64)|(2<<48)|(1<<36)|(9<<32)|0x31544741
        def request(target,op,data,**kw):
            calls.append((target,op,data,kw));return json.dumps({'bridge_v1_hex':f'{value:064x}'}).encode()
        ns={'json':json,'time':types.SimpleNamespace(monotonic=lambda:clock[0]),'diagnostic':lambda *a,**kw:records.append((a,kw))}
        exec(compile(ast.Module(body=[method],type_ignores=[]),'<isolated UI method>','exec'),ns)
        obj=types.SimpleNamespace(deploy=types.SimpleNamespace(session=types.SimpleNamespace(epoch=5,request=request)))
        fn=ns['query_bridge_after_timeout'];fn(obj,5);fn(obj,5)
        self.assertEqual(calls,[(1,2,b'\x02',{'timeout':2})])
        fields=records[0][1];self.assertEqual(fields['b_return_errors'],7);self.assertEqual(fields['queue_state'],9)
        self.assertEqual(fields['cancelled'],2);self.assertTrue(fields['motion_pending'])
        self.assertEqual(fields['usb_stream_drops'],3);self.assertEqual(fields['usb_stream_queued'],4)
        clock[0]+=31;obj.deploy.session.epoch=6;fn(obj,5)
        self.assertEqual(len(records),1)  # An old epoch cannot publish new-session diagnostics.

if __name__=='__main__':unittest.main()

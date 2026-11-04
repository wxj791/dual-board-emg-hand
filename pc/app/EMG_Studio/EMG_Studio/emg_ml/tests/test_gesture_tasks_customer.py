"""Response identity and diagnostic regression without devices or Tk windows."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from gesture_config import preset, validate_profile, select_profile
from gesture_tasks_protocol import Binding, BoardRequestError, Pending
from gesture_tasks_customer import (model_actions, reconcile_actions, response_binding,
    response_value, response_text, friendly_error, diagnostic, log_path)


class CustomerTests(unittest.TestCase):
    def test_button_command_finishes_without_extra_status_request(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from gesture_tasks_ui import GestureTasksPanel
        session=Mock()
        session.request.side_effect=AssertionError('button must not add a status query')
        panel=object.__new__(GestureTasksPanel)
        panel.deploy=SimpleNamespace(session=session)
        for opcode,data in ((0x301,b'\x01\x00'),(0x300,b''),(0x605,b'\x01'),(0x605,b'\x00'),(0x305,b'\x00\x00')):
            session.reset_mock()
            result=panel.command(opcode,data)
            session.request_b_when_idle.assert_called_once_with(opcode,data,timeout=15)
            session.request.assert_not_called()
            self.assertEqual(result,{'command_completed':True,'opcode':opcode})
        session.request_b_when_idle.side_effect=TimeoutError('completion not received')
        with self.assertRaises(TimeoutError):panel.command(0x300)

    def test_retraining_reorder_matches_action_identity(self):
        original=preset('基础三类 · 握拳张掌')
        original['classes'][1]['response']=response_value('回放已示教姿态 9')
        original['classes'][2]['response']=response_value('显示下一页')
        reordered=copy.deepcopy(original['classes'])
        reordered[1],reordered[2]=reordered[2],reordered[1]
        for i,r in enumerate(reordered):r['id']=i;r.pop('response',None)
        matched=reconcile_actions(reordered,original['classes'])
        self.assertEqual(response_binding(1,matched[1]['response']),Binding(1,4,True,0))
        self.assertEqual(response_binding(2,matched[2]['response']),Binding(2,1,True,8))
        self.assertEqual(response_binding(0,matched[0]['response']),Binding(0))

    def test_deleted_and_new_actions_do_not_inherit_numeric_positions(self):
        old=preset('基础三类 · 握拳张掌')['classes']
        old[1]['response']={'kind':2,'enabled':True,'argument':0}
        new=preset('腕部三类 · 掌屈背伸')['classes']
        matched=reconcile_actions(new,old)
        self.assertEqual([response_binding(i,r.get('response')) for i,r in enumerate(matched)],
                         [Binding(0),Binding(1),Binding(2)])

    def test_saved_collision_response_is_retired(self):
        from gesture_tasks_customer import RESPONSE_OPTIONS
        self.assertNotIn((2,0), RESPONSE_OPTIONS.values())
        self.assertEqual(response_binding(1,{'kind':2,'enabled':True,'argument':0}), Binding(1))

    def test_legacy_preview_outputs_never_become_pose_slots(self):
        p=preset('基础三类 · 握拳张掌')
        actions=model_actions([r['name'] for r in p['classes']],p)
        self.assertEqual([response_binding(i,r.get('response')) for i,r in enumerate(actions)],
                         [Binding(0),Binding(1),Binding(2)])

    def test_response_roundtrip_and_subset_keep_profile_identity(self):
        p=preset('基础三类 · 握拳张掌')
        p['classes'][2]['response']=response_value('回放已示教姿态 3')
        result=validate_profile(json.loads(json.dumps(select_profile(p,[2]))))
        self.assertEqual(result['classes'][1]['key'],'open')
        self.assertEqual(response_text(result['classes'][1]['response']),'回放已示教姿态 3')
        result['classes'][0]['response']=response_value('显示下一页')
        with self.assertRaises(ValueError):validate_profile(result)

    def test_ambiguous_custom_actions_are_rejected(self):
        with self.assertRaises(ValueError):
            model_actions(['rest','same','same'])

    def test_friendly_errors_and_timestamped_traceback_log(self):
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,EMG_GESTURE_TASKS_STATE=str(Path(folder)/'session.json')):
            self.assertIn('未连接A板',friendly_error(ConnectionError('wire detail')))
            for target,name in ((1,'A板'),(2,'B板')):
                p=Pending(target,27,0x604,False,0);p.error='timeout opcode=0604';p.phase='unknown'
                error=BoardRequestError(p)
                self.assertIn(name+'未响应',friendly_error(error,'上传'))
                diagnostic('upload_failed',error,target=target)
            self.assertIn('模型上传未完成',friendly_error(RuntimeError('CRC generation mismatch'),'上传'))
            self.assertIn('尚未上传A板',friendly_error(OverflowError('Q23 overflow'),'上传'))
            self.assertNotIn('检查连接',friendly_error(OverflowError('Q23 overflow'),'上传'))
            p=Pending(2,1,0x600,False,0);p.phase='failed';p.status=8;p.error='status=8'
            self.assertIn('B尚未确认新模型',friendly_error(BoardRequestError(p),'上传'))
            p.opcode=0x305;p.status=5
            self.assertIn('退出示教',friendly_error(BoardRequestError(p),'回放'))
            records=[json.loads(line) for line in log_path().read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len(records),2)
            self.assertTrue(all(r['time'] and r['step']=='upload_failed' and r['exception'] and r['traceback'] for r in records))

if __name__=='__main__':unittest.main()

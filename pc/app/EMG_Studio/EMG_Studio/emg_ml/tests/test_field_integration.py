"""Original workstation integration, without serial devices or actuators."""
import importlib.util
import os
from pathlib import Path
import queue
import sys
import time
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import torch

EMG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EMG))
spec = importlib.util.spec_from_file_location('studio_under_test', EMG/'emg_studio_gui_V3.0.py')
studio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(studio)
from field_training import backend_path


class TestReceiver(unittest.TestCase):
    def test_framed_telemetry_does_not_remap_or_join_discontinuities(self):
        import struct
        from gesture_tasks_protocol import frame
        core=studio.SerialCore();core.board_protocol=True;core.running=True
        def wave(seq):
            raw=[100+c+seq for c in range(8)]
            p=struct.pack('<BBBBIHHHH',1,15,8,0,seq,1000,4,9,0)+struct.pack('<8H',*raw)
            p+=b''.join((-256*(c+1)).to_bytes(3,'little',signed=True) for c in range(8))
            p+=b''.join((16*c).to_bytes(3,'little') for c in range(8))*2
            return frame(0x40,p)
        wire=wave(0)+wave(1)+frame(0x51,struct.pack('<BBHHHH',1,2,1,2,0,1)+b'X')+wave(5)
        class Port:
            offset=0;is_open=True
            @property
            def in_waiting(self):return len(wire)-self.offset
            def write(self,data):return len(data)
            def read(self,count):
                if self.offset==len(wire):core.running=False;return b''
                part=wire[self.offset:self.offset+min(count,17)];self.offset+=len(part);return part
        core.ser=Port();core.board_session.reset(True);core.enable_field_stream(True)
        pending=core.board_session.send(1,2)
        with self.assertRaises(RuntimeError):core.start_recording()
        core._recv_loop()
        self.assertEqual(pending.phase,'unknown')  # Wrong target cannot complete A request.
        self.assertEqual(core.total_received,3);self.assertEqual(core.disp_count,1)
        raw,filtered,env,*_=core.get_display_snapshots()
        np.testing.assert_array_equal(raw[0],np.arange(105,113))
        np.testing.assert_array_equal(filtered[0],-np.arange(1,9))
        np.testing.assert_array_equal(env[0],np.arange(8))
        self.assertTrue(core.field_stream.empty())  # 0x40 is display-only, not complete training input.
        self.assertEqual(core.board_stream_breaks,2)

    def test_stopping_after_completed_round_cannot_save_same_buffer_twice(self):
        core = studio.SerialCore()
        core.start_recording()
        core.record_buf.append((1.0, tuple(range(8)), 0))
        self.assertEqual(len(core.stop_recording()), 1)
        self.assertEqual(core.stop_recording(), [])

    def test_raw_copy_queue_overflow_and_generation(self):
        core = studio.SerialCore()
        core.field_stream = queue.Queue(maxsize=2)
        original = np.arange(80, dtype=np.float32).reshape(10, 8)
        generation = core.enable_field_stream(True)
        core.publish_field_samples(original)
        original[:] = -1
        gen, reset, samples = core.field_stream.get_nowait()
        self.assertEqual(gen, generation)
        self.assertTrue(reset)
        self.assertGreaterEqual(samples.min(), 0)
        for _ in range(3):
            core.publish_field_samples(original)
        self.assertEqual(core.field_stream_overflows, 1)
        self.assertTrue(core.field_stream.get_nowait()[1])
        core.publish_field_samples(original)
        core.enable_field_stream(False)
        self.assertTrue(core.field_stream.empty())
        core.publish_field_samples(original)
        self.assertTrue(core.field_stream.empty())

    def test_decoder_preserves_recording_and_queues_physical_raw(self):
        import struct
        core = studio.SerialCore()
        expected = np.arange(600*8, dtype=np.float32).reshape(600, 8)/3
        stream = b''.join(struct.pack('<8f', *row)+studio.TAIL_BYTES for row in expected)
        class Port:
            def __init__(self):
                self.offset = 0

            @property
            def in_waiting(self):
                if self.offset == len(stream):
                    core.running = False
                    return 0
                return min(3600, len(stream)-self.offset)

            def read(self, count):
                part = stream[self.offset:self.offset+count]
                self.offset += count
                return part
        core.ser = Port()
        core.running = True
        core.enable_field_stream(True)
        core.set_label(2)
        core.start_recording()
        core._recv_loop()
        expected = expected[:, studio.PHYSICAL_CH_MAP]
        chunks = []
        while not core.field_stream.empty():
            chunks.append(core.field_stream.get_nowait()[2])
        np.testing.assert_allclose(np.concatenate(chunks), expected)
        record = core.stop_recording()
        np.testing.assert_allclose(np.array([r[1] for r in record]), expected)
        self.assertTrue(all(r[2] == 2 for r in record))
        self.assertEqual(core.total_received, 600)
        self.assertEqual(core.corrupt_frames, 0)
        raw, filtered, *_ = core.get_snapshots()
        np.testing.assert_allclose(raw, expected[-500:])
        independently_filtered = studio.EMGSignalFilter().process(expected)
        np.testing.assert_allclose(filtered, independently_filtered[-500:], rtol=1e-5, atol=1e-4)


class TestEmbeddedUI(unittest.TestCase):
    def test_training_only_result_load_and_completion_have_no_fake_accuracy(self):
        import json
        import shutil
        from field_training import format_report_metrics
        field = self.app.field_training
        report = {'recording': 'small.npz', 'evaluation': {'test': None, 'independent': False},
                  'acceptance': {'passed_offline': None, 'evaluated': False, 'weak_classes': []},
                  'timing_seconds': 1.5}
        run_dir = Path(self.settings_dir.name)/'small_training'
        shutil.copytree(backend_path()/'artifacts/cnn_fusion_pc_trial_20261029/export', run_dir/'export')
        (run_dir/'field_report.json').write_text(json.dumps(report), encoding='utf8')
        self.assertIn('未独立评估', format_report_metrics(report))
        self.assertNotIn('%', format_report_metrics(report))
        self.assertTrue(field.load_package(run_dir/'export'))
        field.events.put(('done', ('train', 0, run_dir)))
        with patch.object(self.app.gesture_tasks, 'on_trained') as on_trained:
            field.poll()
        on_trained.assert_called_once()
        self.assertIn('未独立评估', field.result.cget('text'))
        self.assertIn('训练完成', field.status.get())
        self.assertIn('未独立评估', field.status.get())
        self.assertNotIn('验收未通过', field.status.get())

    def test_arm_precondition_keeps_readback_but_other_errors_invalidate(self):
        from gesture_tasks_deploy import ArmPreconditionError
        panel=self.app.gesture_tasks
        panel.deploy.verified=True
        panel.report_error('启用',ArmPreconditionError('实时分类计数未增长；恢复后再次启用'))
        self.assertTrue(panel.deploy.verified)
        self.assertIn('实时分类计数未增长',panel.status.get())
        panel.report_error('启用',RuntimeError('readback mismatch'))
        self.assertFalse(panel.deploy.verified)
        panel.deploy.verified=True
        panel.report_error('上传',ArmPreconditionError('precondition'))
        self.assertFalse(panel.deploy.verified)

    def test_gesture_task_editor_uses_actual_classes_and_preserves_rest(self):
        from gesture_tasks_protocol import Binding
        from gesture_tasks_customer import model_actions,action_identity
        panel=self.app.gesture_tasks
        model={'active_classes':32,'class_names':[f'test{i}' for i in range(32)],'source_sha256':'f'*64}
        panel.actions=model_actions(model['class_names'])
        model['action_identities']=[action_identity(r) for r in panel.actions]
        panel.model=model;panel.deploy.select(model)
        dialog=panel.edit_responses()
        self.assertEqual(len(dialog.action_combo['values']),32)
        self.assertEqual(str(dialog.response_combo['state']),'disabled')
        dialog.action_combo.current(31);dialog.select_action()
        dialog.response.set('回放已示教姿态 9');dialog.change_response();dialog.save()
        self.assertEqual(panel.deploy.rows[0],Binding(0))
        self.assertEqual(panel.deploy.rows[31],Binding(31,1,True,8))
        model={'active_classes':2,'class_names':['rest','test'],'source_sha256':'e'*64}
        panel.deploy.select(model)
        self.assertEqual(panel.deploy.rows,[Binding(0),Binding(1)])

    @classmethod
    def setUpClass(cls):
        torch.set_num_threads(4)

    def setUp(self):
        self.settings_dir = tempfile.TemporaryDirectory()
        self.settings_path = Path(self.settings_dir.name)/'filtered_cnn_default.json'
        self.settings_patch = patch('field_training.DEFAULT_PACKAGE_FILE', self.settings_path)
        self.settings_patch.start()
        self.task_settings_patch=patch.dict(os.environ,EMG_GESTURE_TASKS_STATE=str(Path(self.settings_dir.name)/'gesture_tasks_session.json'))
        self.task_settings_patch.start()
        self.profile_patch = patch('gesture_config.PROFILE_FILE', Path(self.settings_dir.name)/'gesture_profile.json')
        self.profile_patch.start()
        self.ports = patch.object(studio.list_ports, 'comports', return_value=[])
        self.ports.start()
        self.app = studio.EMGStudioGUI()
        self.app.withdraw()
        # ZIP extraction normalizes mtimes, so "latest historical model" is
        # not a stable fixture. Legacy checks explicitly select their model.
        self.app._load_trained_model('DGTV2.0.pth')

    def tearDown(self):
        self.app.core.running = False
        self.app.field_training.close()
        # Each test owns a Tk interpreter. Cancel its queued callbacks before
        # creating the next one, including collection-image callbacks.
        for timer in self.app.tk.call('after', 'info'):
            self.app.after_cancel(timer)
        self.app.update_idletasks()
        self.app.destroy()
        self.ports.stop()
        self.settings_patch.stop()
        self.task_settings_patch.stop()
        self.profile_patch.stop()
        self.settings_dir.cleanup()

    def test_original_ui_and_saved_recording_handoff(self):
        app = self.app
        self.assertEqual(len(app.notebook.tabs()), 5)
        self.assertFalse(app.core.board_session.connected)
        self.assertIsNone(app.core.ser)
        self.assertEqual(app.notebook.tab(app.gesture_tasks.tab,'text'), '机械手')
        self.assertEqual(app.active_model_type, 'mlp_56')
        self.assertEqual(app.waveform_mode.get(), 'raw')
        self.assertEqual(len(app.gesture_vars), 6)
        self.assertTrue(all(var.get() for var in app.gesture_vars.values()))
        app.field_training.recording_saved('example_complete.npz', True)
        app.field_training.poll()
        self.assertEqual(app.field_training.source.get(), 'example_complete.npz')
        app.field_training.recording_saved('example_aborted.npz', False)
        app.field_training.poll()
        self.assertEqual(app.field_training.source.get(), 'example_complete.npz')
        app.waveform_mode.set('filtered')
        app._on_waveform_mode_changed()
        self.assertEqual(app.waveform_range.get(), '200')

    def test_independent_query_keeps_a_sampling_when_b_times_out(self):
        from test_gesture_tasks import MockBoards
        app=self.app;panel=app.gesture_tasks;mock=MockBoards();mock.no_b=True
        now=[0]
        def clock():now[0]+=6;return now[0]
        mock.session.clock=clock
        app.core.board_session=mock.session;panel.deploy.session=mock.session
        app.core.board_protocol=True;app.core.last_data_time=time.monotonic()
        panel.endpoint_epoch=mock.session.epoch
        panel.query(targets=(1,2))
        self.assertEqual(panel.busy,0)
        deadline=time.monotonic()+2
        while time.monotonic()<deadline and (panel.querying or panel.endpoint[1] is None):
            panel.poll();time.sleep(.02)
        self.assertIsNotNone(panel.endpoint[1])
        self.assertIsNotNone(panel.endpoint_error[2])
        self.assertIn('正在接收采样',panel.a_text.get())
        self.assertIn('B板：未响应',panel.board_text.get())
        self.assertFalse(panel.deploy.verified)
        self.assertFalse(any(op in (0x300,0x301,0x102) for _,op,_ in mock.sent))

    def test_display_clicks_write_once_without_b_reply_or_busy_gate(self):
        import struct
        from gesture_tasks_protocol import Parser,Session
        app=self.app;panel=app.gesture_tasks;writes=[];clock=[0.]
        session=Session(writes.append,clock=lambda:clock[0]);session.reset(True)
        panel.deploy.session=session;panel.endpoint_epoch=session.epoch
        panel.endpoint_error[2]=TimeoutError('B absent')
        panel.busy=1  # Even a waiting foreground operation must not swallow clicks.
        with patch.object(session,'request',side_effect=AssertionError('no blocking request')), \
             patch.object(panel,'command',side_effect=AssertionError('no status readback')):
            panel.send_display('下一页',0x201,b'\x01')
            panel.send_display('上一页',0x201,b'\xff')
            panel.page.set('滤波');panel.channel.set('全部');panel.mv.set('200')
            panel.set_range()
        self.assertEqual(panel.busy,1)
        self.assertEqual(len(writes),3)
        payloads=[Parser().feed(wire)[0][1] for wire in writes]
        self.assertEqual([struct.unpack_from('<BBHHH',p)[1::2] for p in payloads],[(2,0x201),(2,0x201),(2,0x204)])
        self.assertEqual([p[8:] for p in payloads],[b'\x01',b'\xff',struct.pack('<BBH',1,255,200)])
        self.assertIn('未等待B核对',panel.status.get())
        self.assertNotIn('已完成',panel.status.get())
        self.assertFalse(panel.deploy.verified)
        clock[0]=3.;panel.poll_display()
        self.assertEqual(panel.display_pending,{})
        self.assertEqual(len(writes),3)  # No automatic resend after missing ACK.
        panel.send_display('下一页',0x201,b'\x01')
        self.assertEqual(len(writes),4)

    def test_display_rejection_failure_and_disconnect_are_not_success(self):
        import struct
        from gesture_tasks_protocol import Session
        panel=self.app.gesture_tasks;writes=[]
        session=Session(writes.append);session.reset(True);panel.deploy.session=session
        panel.send_display('下一页',0x201,b'\x01')
        rid=panel.last_display_id
        session.receive(0x51,struct.pack('<BBHHHH',1,2,rid,0x201,6,0))
        panel.poll_display();self.assertIn('被板端拒绝',panel.status.get())
        self.assertFalse(panel.deploy.verified)
        session.reset(False);panel.send_display('下一页',0x201,b'\x01')
        self.assertIn('未连接',panel.status.get());self.assertEqual(len(writes),1)
        def fail(_):raise OSError('write failed')
        session.writer=fail;session.reset(True)
        panel.send_display('下一页',0x201,b'\x01')
        self.assertNotIn('命令已发送',panel.status.get())
        self.assertEqual(panel.display_pending,{})
        panel.send_display('启用',0x301,b'\x01')
        self.assertEqual(len(writes),1)  # Mechanical commands cannot use the bypass.

    def test_poll_automatically_queries_only_a_and_drops_old_display_session(self):
        from gesture_tasks_protocol import Session
        app=self.app;panel=app.gesture_tasks;writes=[]
        session=Session(writes.append);session.reset(True)
        app.core.board_session=session;panel.deploy.session=session
        panel.endpoint_epoch=session.epoch;panel.last_query={1:0,2:0}
        with patch.object(panel,'query') as query:
            panel.poll()
        query.assert_called_once_with(targets=(1,))
        panel.send_display('下一页',0x201,b'\x01')
        session.reset(True);panel.poll_display()
        self.assertEqual(panel.display_pending,{})
        self.assertEqual(len(writes),1)

    def test_a_classification_owns_customer_result_and_checks_identity(self):
        import struct
        from gesture_tasks_customer import model_actions,action_identity
        app=self.app;panel=app.gesture_tasks;core=app.core
        core.board_protocol=True;core.board_session.reset(True)
        panel.actions=model_actions(['静息','张开手掌'])
        model={'class_names':['静息','张开手掌'],'active_classes':2,'source_sha256':'f'*64,
               'transaction_crc32':'12345678','action_identities':[action_identity(a) for a in panel.actions]}
        panel.model=model;panel.deploy.select(model)
        self.assertTrue(panel.verify_a_identity({'valid':1,'loading':0,'model_crc32_valid':1,
            'model_crc32_hex':'12345678','generation_hex':'0004','active_hex':'0002'}))
        raw=bytearray(32);raw[0]=1;raw[1]=3
        struct.pack_into('<H',raw,2,1);struct.pack_into('<HH',raw,12,4,2)
        core.board_classification=bytes(raw);core.board_classification_time=time.monotonic()
        panel.recognition_enabled=True;panel.render_recognition()
        self.assertEqual(app.lbl_pred_gesture.cget('text'),'【张开手掌】')
        app.field_training.live_generation=99;app.infer_active_var.set(True)
        app.field_training.events.put(('prediction',(99,{'stable':0,'margin_q8':10})))
        with patch.object(app.field_training,'stop_live'):
            app.field_training.poll()
        self.assertEqual(app.lbl_pred_gesture.cget('text'),'【张开手掌】')
        panel.deploy.verified=True
        self.assertFalse(panel.verify_a_identity({'valid':1,'loading':0,'model_crc32_valid':1,
            'model_crc32_hex':'abcdef01','generation_hex':'0004','active_hex':'0002'}))
        self.assertFalse(panel.deploy.verified)
        panel.verify_a_identity({'valid':1,'loading':0,'model_crc32_valid':1,
            'model_crc32_hex':'12345678','generation_hex':'0004','active_hex':'0002'})
        struct.pack_into('<H',raw,12,5);core.board_classification=bytes(raw)
        panel.render_recognition()
        self.assertFalse(panel.deploy.a_verified)
        self.assertNotEqual(app.lbl_pred_gesture.cget('text'),'【张开手掌】')
        app.field_training.live_generation=None

    def test_customer_pages_hide_wire_diagnostics_and_fit_requested_layout(self):
        import tkinter as tk
        from tkinter import ttk
        from ui_style import FG,PLOT,PRIMARY
        panel=self.app.gesture_tasks;self.app.update_idletasks()
        texts=[]
        def visit(widget):
            if 'text' in widget.keys():texts.append(str(widget.cget('text')))
            for child in widget.winfo_children():visit(child)
        visit(panel.tab)
        for word in ('0x43','CRC','generation','epoch','opcode','ARM','TASK_STATE','0204'):
            self.assertFalse(any(word in text for text in texts),word)
        self.assertFalse(any(isinstance(w,ttk.Treeview) for w in panel.bind_tab.winfo_children()))
        for page in (panel.bind_tab,panel.teach_tab,panel.display_tab):
            self.assertLessEqual(page.winfo_reqwidth(),500)
            self.assertLessEqual(page.winfo_reqheight(),600)
        style=ttk.Style(self.app)
        self.assertEqual(style.lookup('Treeview','foreground'),FG)
        self.assertEqual(style.lookup('Treeview','fieldbackground'),PLOT)
        self.assertEqual(style.lookup('Treeview','background',('selected',)),PRIMARY)
        self.assertFalse(self.app.winfo_ismapped())

    def test_upload_button_exports_once_then_runs_existing_verified_deployment(self):
        import copy
        from test_gesture_tasks import MockBoards,DeploymentTests
        from gesture_tasks_customer import model_actions,action_identity,response_value
        app=self.app;panel=app.gesture_tasks;mock=MockBoards()
        panel.deploy.session=mock.session;app.core.board_session=mock.session;panel.endpoint_epoch=mock.session.epoch
        full=DeploymentTests().model(2)
        actions=model_actions(full['class_names']);actions[1]['response']=response_value('显示下一页')
        full['action_identities']=[action_identity(a) for a in actions]
        panel.actions=actions;panel.model={k:v for k,v in full.items() if k!='banks'}
        panel.deploy.select(panel.model)
        from gesture_tasks_customer import response_binding
        panel.deploy.edit([response_binding(i,r.get('response')) for i,r in enumerate(actions)])
        app.field_training.package=Path(self.settings_dir.name)/'new/export'
        with patch.object(panel,'sync_model'),patch.object(panel,'export_model',return_value=full) as export, \
             patch('gesture_tasks_ui.require_export_inputs'):
            panel.start_upload()
            deadline=time.monotonic()+4
            while panel.busy and time.monotonic()<deadline:
                panel.poll();time.sleep(.01)
            export.assert_called_once()
        self.assertFalse(panel.deploy.verified)
        self.assertTrue(panel.deploy.b_mapping_sent)
        self.assertTrue(panel.deploy.a_verified)
        self.assertIn('动作绑定已提交',panel.status.get())
        self.assertEqual(mock.rows,mock.stage)
        self.assertFalse(mock.armed)
        self.assertNotIn((2,0x301),[(t,o) for t,o,_ in mock.sent])

    def test_check_existing_factory_a_displays_actions_without_b_or_model_write(self):
        import shutil,json
        from test_gesture_tasks import MockBoards
        from gesture_tasks_materials import readonly_identities
        project=EMG.parents[2]
        sys.path.insert(0,str(project/'scripts'))
        from pc_gesture_tasks_default_materials import prepare_default
        app=self.app;field=app.field_training;panel=app.gesture_tasks;mock=MockBoards()
        mock.no_b=True;mock.gen=0;mock.k=7;mock.crc='fc91830a'
        app.core.board_session=mock.session;panel.deploy.session=mock.session
        app.core.board_protocol=True;panel.endpoint_epoch=mock.session.epoch
        portable=Path(self.settings_dir.name)/'portable'
        prepare_default(portable)
        source=backend_path()/'artifacts/cnn_fusion_pc_trial_20261029/export'
        shutil.copytree(source,portable/'export')
        self.assertTrue(field.load_package(portable/'export'))
        # No prepared.npz exists in this portable copy: checking an existing
        # identity requires neither calibration data nor B responses.
        self.assertFalse((portable/'prepared.npz').exists())
        panel.start_check_a()
        deadline=time.monotonic()+5
        with patch.object(panel,'query'):
            while panel.busy and time.monotonic()<deadline:panel.poll();time.sleep(.01)
        self.assertTrue(panel.deploy.a_verified)
        self.assertEqual(panel.deploy.a_generation,0)
        self.assertFalse(panel.deploy.verified)
        self.assertEqual([(target,op) for target,op,_ in mock.sent],[(1,2)])
        self.assertIn('A模型已核对',panel.status.get())
        import struct
        raw=bytearray(32);raw[0]=1;raw[1]=3
        struct.pack_into('<H',raw,2,3);struct.pack_into('<HH',raw,12,0,7)
        app.core.board_classification=bytes(raw);app.core.board_classification_time=time.monotonic()
        panel.render_recognition()
        self.assertEqual(app.lbl_pred_gesture.cget('text'),'【掌屈】')
        self.assertEqual(panel.deploy.model['transaction_crc32'],'eb3518c0')
        panel.start_arm();self.assertFalse(mock.armed)
        self.assertEqual([(target,op) for target,op,_ in mock.sent],[(1,2)])
        mock.crc='00000000'
        panel.start_check_a()
        with patch.object(panel,'query'):
            while panel.busy and time.monotonic()<deadline:panel.poll();time.sleep(.01)
        self.assertFalse(panel.deploy.a_verified)
        self.assertIn('不匹配',panel.status.get())
        mock.crc='fc91830a';mock.gen=9
        struct.pack_into('<H',raw,12,9)
        app.core.board_classification=bytes(raw);app.core.board_classification_time=time.monotonic()
        panel.deploy.generation=0;panel.deploy.verified=True
        panel.start_check_a()
        with patch.object(panel,'query'):
            while panel.busy and time.monotonic()<deadline:panel.poll();time.sleep(.01)
        self.assertTrue(panel.deploy.a_verified)
        self.assertEqual(panel.deploy.a_generation,9)
        self.assertFalse(panel.deploy.verified)
        self.assertEqual([(target,op) for target,op,_ in mock.sent],[(1,2)]*3)
        # An A-only identity check must not replace an active PC display or
        # stop its worker, including when B is unavailable.
        app.recognition_source.set('PC识别');panel.change_recognition_source()
        app.core.running=True;app.core.board_raw_type=0x43
        app.core.board_raw_time=app.core.last_data_time=time.monotonic()
        field.save_trial.set(False);app._toggle_recognition()
        pc_generation=field.live_generation
        self.assertIsNotNone(pc_generation)
        card=app.lbl_pred_gesture.cget('text')
        panel.start_check_a()
        with patch.object(panel,'query'):
            while panel.busy and time.monotonic()<deadline:panel.poll();time.sleep(.01)
        self.assertTrue(panel.deploy.a_verified)
        self.assertFalse(panel.recognition_enabled)
        self.assertEqual(field.live_generation,pc_generation)
        self.assertEqual(app.lbl_pred_gesture.cget('text'),card)
        self.assertIn('识别来源：PC',app.lbl_pred_conf.cget('text'))
        self.assertEqual([(target,op) for target,op,_ in mock.sent],[(1,2)]*4)
        mock.session.reset(True)
        with patch.object(panel,'query'):panel.poll()
        self.assertFalse(panel.deploy.a_verified)

    def test_a_check_reports_class_mismatch_before_local_export(self):
        from test_gesture_tasks import MockBoards
        app=self.app;field=app.field_training;panel=app.gesture_tasks;mock=MockBoards()
        mock.k=7;mock.crc='fc91830a';mock.no_b=True
        app.core.board_session=mock.session;panel.deploy.session=mock.session
        app.core.board_protocol=True;panel.endpoint_epoch=mock.session.epoch
        field.package=Path(self.settings_dir.name)/'no_materials/export'
        field.parameter_sha256='2'*64;field.class_names=['rest']+[f'action{i}' for i in range(1,10)]
        with patch.object(panel,'export_model',side_effect=AssertionError('Mismatch needs no export')) as export,patch.object(panel,'query'):
            panel.start_check_a()
            deadline=time.monotonic()+2
            while panel.busy and time.monotonic()<deadline:panel.poll();time.sleep(.01)
            export.assert_not_called()
        self.assertFalse(panel.deploy.a_verified)
        self.assertIn('7类',panel.status.get());self.assertIn('10类',panel.status.get())
        self.assertIn('PC识别',panel.status.get())
        self.assertEqual([(t,op) for t,op,_ in mock.sent],[(1,2)])

    def test_retraining_sync_preserves_named_responses_and_new_action_is_off(self):
        import copy
        from gesture_config import preset,action
        from gesture_tasks_customer import response_value
        from gesture_tasks_protocol import Binding
        app=self.app;field=app.field_training;panel=app.gesture_tasks
        original=preset('基础三类 · 握拳张掌')
        original['classes'][1]['response']=response_value('回放已示教姿态 9')
        original['classes'][2]['response']=response_value('显示下一页')
        app.gesture_profile=original
        trained=copy.deepcopy(original)
        trained['classes'][1],trained['classes'][2]=trained['classes'][2],trained['classes'][1]
        trained['classes'].append(action('wrist_flexion',3))
        for i,row in enumerate(trained['classes']):row['id']=i
        field.package=Path(self.settings_dir.name)/'new/export'
        field.parameter_sha256='1'*64;field.gesture_profile=trained
        field.class_names=[r['name'] for r in trained['classes']]
        panel.on_trained()
        self.assertEqual(panel.deploy.rows,[Binding(0),Binding(1,4,True),Binding(2,1,True,8),Binding(3)])
        self.assertFalse(panel.deploy.verified)
        self.assertIn('请上传',panel.status.get())
        panel.auto_upload.set(True)
        with patch.object(panel,'start_upload') as upload:
            panel.on_trained();app.update()
            upload.assert_called_once()
        panel.auto_upload.set(False)
        panel.save_responses(panel.actions)
        panel.sync_model(force=True)
        self.assertEqual(panel.deploy.rows[2],Binding(2,1,True,8))
        self.assertEqual([r['name'] for r in app.gesture_profile['classes']],field.class_names)

    def test_integer_results_use_original_inference_card_and_old_model_can_return(self):
        app = self.app
        panel = app.field_training
        backend = backend_path()
        self.assertTrue(panel.load_package(backend/'artifacts/cnn_fusion_pc_trial_20261029/export'))
        app.core.running = True
        app.core.last_data_time = time.monotonic()
        old_model = app.cnn_model
        with patch.object(old_model, 'forward', side_effect=AssertionError('Old inference must be bypassed')):
            panel.toggle_live()
            self.assertEqual(app.active_model_type, 'field_int8')
            data = np.random.default_rng(7).normal(1580, 30, (1024, 8)).astype(np.float32)
            app.core.publish_field_samples(data, reset=True)
            deadline = time.monotonic()+5
            while time.monotonic() < deadline and '【' not in app.lbl_pred_gesture.cget('text'):
                app.core.last_data_time = time.monotonic()
                app.update()
                time.sleep(.02)
            self.assertIn('【', app.lbl_pred_gesture.cget('text'))
            from pc_ml.package_api import predictor as StreamingPredictor
            expected = StreamingPredictor(panel.package).push(data)[-1]
            expected_name = panel.app_gestures()[expected['stable']][0]
            self.assertEqual(app.lbl_pred_gesture.cget('text'), f'【{expected_name}】')
            self.assertIn(f'{expected["margin_q8"]/256:.2f}', app.lbl_pred_conf.cget('text'))
            self.assertIn('非概率', app.lbl_pred_conf.cget('text'))
            self.assertTrue(all(label.cget('text') == '--' for label in app.prob_labels))
        app._load_trained_model('DGTV2.0.pth')
        self.assertIsNone(panel.live_generation)
        self.assertFalse(app.core.field_stream_enabled)
        self.assertEqual(app.active_model_type, 'mlp_56')
        self.assertFalse(self.settings_path.exists())

    def test_framed_pc_prediction_and_source_switch_isolate_results_without_commands(self):
        import struct
        from test_gesture_tasks import MockBoards
        from gesture_tasks_customer import response_value
        from pc_ml.package_api import predictor as StreamingPredictor
        app=self.app;field=app.field_training;panel=app.gesture_tasks;core=app.core
        mock=MockBoards();core.board_session=mock.session;panel.deploy.session=mock.session
        core.board_protocol=True;core.running=True
        self.assertTrue(field.load_package(backend_path()/'artifacts/cnn_fusion_pc_trial_20261029/export'))
        data=np.random.default_rng(7).normal(1580,30,(1024,8)).astype(np.float32)
        expected=StreamingPredictor(field.package).push(data)[-1]
        panel.sync_model()
        if expected['stable']:
            panel.actions[expected['stable']]['response']=response_value('显示下一页')
        app.recognition_source.set('PC识别');panel.change_recognition_source()
        core.board_raw_type=0x43;core.board_raw_time=time.monotonic();core.last_data_time=time.monotonic()
        app._toggle_recognition()
        generation=field.live_generation
        self.assertIsNotNone(generation)
        core.publish_field_samples(data,reset=True)
        with patch.object(panel,'query'):
            deadline=time.monotonic()+5
            while time.monotonic()<deadline and '【' not in app.lbl_pred_gesture.cget('text'):
                core.last_data_time=core.board_raw_time=time.monotonic()
                app.update();time.sleep(.02)
        self.assertEqual(app.lbl_pred_gesture.cget('text'),f'【{field.class_names[expected["stable"]]}】')
        self.assertIn('识别来源：PC',app.lbl_pred_conf.cget('text'))
        self.assertIn(f'{expected["margin_q8"]/256:.2f}',app.lbl_pred_conf.cget('text'))
        self.assertIn('PC预览',app.lbl_pred_output.cget('text'))
        if expected['stable']:self.assertIn('显示下一页',app.lbl_pred_output.cget('text'))
        # Fresh A telemetry cannot take ownership of the PC result card.
        raw=bytearray(32);raw[0]=1;raw[1]=3
        struct.pack_into('<H',raw,2,(expected['stable']+1)%7);struct.pack_into('<HH',raw,12,0,7)
        core.board_classification=bytes(raw);core.board_classification_time=time.monotonic()
        panel.recognition_enabled=True;panel.render_recognition()
        self.assertEqual(app.lbl_pred_gesture.cget('text'),f'【{field.class_names[expected["stable"]]}】')
        app.recognition_source.set('A板识别');panel.change_recognition_source()
        self.assertIsNone(field.live_generation);self.assertFalse(core.field_stream_enabled)
        self.assertFalse(field.live_worker.is_alive())
        app._toggle_recognition();a_text=app.lbl_pred_gesture.cget('text')
        field.events.put(('prediction',(generation,expected)));field.poll()
        self.assertEqual(app.lbl_pred_gesture.cget('text'),a_text)
        self.assertIn('识别来源：A板',app.lbl_pred_conf.cget('text'))
        # Switching back starts fresh; an old generation cannot populate it.
        app.recognition_source.set('PC识别');panel.change_recognition_source()
        core.board_raw_time=time.monotonic();app._toggle_recognition()
        self.assertNotEqual(field.live_generation,generation)
        field.events.put(('prediction',(generation,expected)));field.poll()
        self.assertEqual(app.lbl_pred_gesture.cget('text'),'等待预热')
        app._toggle_recognition();self.assertIsNone(field.live_generation)
        self.assertEqual(mock.sent,[])

    def test_pc_source_requires_raw_and_clears_result_on_gap_disconnect_and_new_session(self):
        app=self.app;field=app.field_training;panel=app.gesture_tasks;core=app.core
        core.board_protocol=True
        self.assertTrue(field.load_package(backend_path()/'artifacts/cnn_fusion_pc_trial_20261029/export'))
        field.save_trial.set(False)
        app.recognition_source.set('PC识别');panel.change_recognition_source()
        app._toggle_recognition()
        self.assertEqual(app.lbl_pred_gesture.cget('text'),'未连接A板')
        core.running=True;core.board_session.reset(True)
        core.last_data_time=time.monotonic();core.board_raw_type=0x40
        app._toggle_recognition()
        self.assertIsNone(field.live_generation)
        self.assertIn('等待A板完整原始采样',app.lbl_pred_gesture.cget('text'))
        self.assertNotIn('0x',app.lbl_pred_gesture.cget('text'))
        core.board_raw_type=0x43;core.board_raw_time=time.monotonic()
        app._toggle_recognition();self.assertIsNotNone(field.live_generation)
        core.board_raw_time=time.monotonic()-1
        field.poll()
        self.assertEqual(app.lbl_pred_gesture.cget('text'),'等待新数据')
        self.assertEqual(app.lbl_pred_output.cget('text'),'')
        # The stop button must work even when full raw data has stopped.
        app._toggle_recognition();self.assertIsNone(field.live_generation)
        core.board_raw_time=time.monotonic();app._toggle_recognition()
        old_generation=field.live_generation
        core.board_session.reset(True)
        field.events.put(('prediction',(old_generation,{'stable':1,'margin_q8':200})))
        field.poll()
        self.assertIsNone(field.live_generation)
        self.assertIn('连接已断开',app.lbl_pred_gesture.cget('text'))
        self.assertNotIn('【',app.lbl_pred_gesture.cget('text'))
        core.board_raw_time=time.monotonic();app._toggle_recognition()
        core.running=False;field.poll()
        self.assertIsNone(field.live_generation)

    def test_preferred_filtered_model_survives_restart_without_raw_fallback(self):
        import json
        app = self.app
        panel = app.field_training
        package = backend_path()/'artifacts/cnn_fusion_pc_trial_20261029/export'
        self.settings_path.write_text(json.dumps({'package': str(package)}))
        panel.restore_default()
        self.assertEqual(app.active_model_type, 'field_waiting')
        self.assertFalse(app.infer_active_var.get())
        self.assertEqual(panel.package, package)
        app.core.running = True
        with patch.object(app.cnn_model, 'forward', side_effect=AssertionError('No raw fallback')):
            panel.resume_default()
            self.assertEqual(app.active_model_type, 'field_int8')
            self.assertTrue(app.infer_active_var.get())
            self.assertTrue(self.settings_path.exists())

    def test_missing_preferred_package_disables_legacy_inference(self):
        import json
        self.settings_path.write_text(json.dumps({'package': str(Path(self.settings_dir.name)/'missing')}))
        self.app.field_training.restore_default()
        self.assertEqual(self.app.active_model_type, 'field_waiting')
        self.assertFalse(self.app.infer_active_var.get())
        self.assertIsNone(self.app.field_training.package)

    def test_profile_dialog_custom_names_pictures_mapping_and_persistence(self):
        import json
        from gesture_dialog import GestureProfileDialog
        from gesture_config import load_profile, select_profile, validate_profile
        dialog = GestureProfileDialog(self.app)
        dialog.withdraw()
        dialog.preset_name.set('腕部五类 · 优先试验')
        dialog.use_preset()
        self.assertEqual(len(dialog.rows), 5)
        self.assertTrue(all(row.get('photo') for row in dialog.rows))
        dialog.rows[3]['name'].set('向掌侧弯腕')
        dialog.rows[3]['output'].set('3: 两指捏取')
        dialog.apply()
        profile = load_profile()
        self.assertEqual(profile['classes'][3]['name'], '向掌侧弯腕')
        self.assertEqual(profile['classes'][3]['output']['id'], 3)
        self.assertEqual(len(self.app.gesture_vars), 4)
        selected = select_profile(profile, [2, 4])
        self.assertEqual([c['id'] for c in selected['classes']], [0, 1, 2])
        self.assertEqual([c['key'] for c in selected['classes']], ['rest', 'open', 'wrist_extension'])
        selected['classes'][1]['name'] = selected['classes'][2]['name']
        with self.assertRaisesRegex(ValueError, '重复'):
            validate_profile(selected)

    def test_full_gesture_catalog_and_all_presets_have_images(self):
        from gesture_config import GROUPS, NINAPRO_IDS, PRESETS, action, image_path, preset, validate_profile
        keys = [key for group in GROUPS.values() for key in group]
        self.assertEqual(len(keys), 50)
        self.assertEqual(len(set(keys)), 50)
        self.assertTrue(set(NINAPRO_IDS).issubset(keys))
        for key in keys:
            self.assertTrue(image_path(action(key, 1)).is_file(), key)
        for name in PRESETS:
            validate_profile(preset(name))

    def test_picture_cards_select_remove_map_and_reindex_actions(self):
        from gesture_dialog import GestureProfileDialog
        from gesture_config import validate_profile
        dialog = GestureProfileDialog(self.app)
        dialog.withdraw()
        try:
            dialog.clear_selection()
            for key in ['wrist_flexion', 'tip_pinch', 'radial_deviation']:
                dialog.toggle_action(key)
            dialog.focus_row(dialog.rows[2])
            dialog.detail_output.set('回放已示教姿态 4')
            dialog.change_output()
            dialog.toggle_action('wrist_flexion')
            profile = validate_profile(dialog.collect())
            self.assertEqual([c['id'] for c in profile['classes']], [0, 1, 2])
            self.assertEqual([c['key'] for c in profile['classes']], ['rest', 'tip_pinch', 'radial_deviation'])
            self.assertEqual(profile['classes'][1]['response'], {'kind':1,'enabled':True,'argument':3})
            dialog.search.set('捏取')
            dialog.choose_category('常用抓握')
            dialog.update_idletasks()
            self.assertEqual(dialog.detail_name.get(), '掌屈')
            # Product labels explain the task, without editor annotations or
            # exposing numeric command formats to the person collecting EMG.
            labels = []
            def read_labels(widget):
                if 'text' in widget.keys():
                    labels.append(str(widget.cget('text')))
                for child in widget.winfo_children():
                    read_labels(child)
            read_labels(dialog)
            self.assertFalse(any('可改' in text or '动作码' in text or '可替换' in text for text in labels))
        finally:
            dialog.destroy()

    def test_collection_saves_subset_identity_and_output_mapping(self):
        import json
        from gesture_config import preset, select_profile
        app = self.app
        profile = select_profile(preset('腕部五类 · 优先试验'), [2, 4])
        app.apply_gesture_profile(profile)
        core = app.core
        clock = iter(np.arange(10000, dtype=float))
        original_set_label = core.set_label
        sample_offset = [0]
        def collect_samples(label):
            original_set_label(label)
            if label >= 0:
                for i in range(600):
                    core.record_buf.append(((sample_offset[0]+i)/1000, tuple([1580.]*8), label))
                sample_offset[0] += 600
        target = Path(self.settings_dir.name)/'subset.npz'
        with patch.object(core, 'set_label', side_effect=collect_samples), \
             patch.object(app, '_get_next_session_filename', return_value=(str(target.parent), target.name, 1)), \
             patch.object(studio.time, 'time', side_effect=lambda: next(clock)), \
             patch.object(studio.time, 'sleep'), patch.object(studio.messagebox, 'showinfo'):
            app._run_collection_worker('test', [1, 2], 4, 3, 3, 0, 1, profile, False)
        with np.load(target, allow_pickle=True) as z:
            self.assertEqual(set(z['labels']), {0, 1, 2})
            labels = z['labels']
            starts = np.r_[True, labels[1:] != labels[:-1]]
            self.assertEqual(labels[starts & (labels > 0)].tolist(), [1, 2, 2, 1, 1, 2, 2, 1])
            self.assertEqual(z['gestures'].item()[1][0], '张开手掌')
            saved = json.loads(z['gesture_profile'].item())
            self.assertEqual(saved, profile)
            self.assertEqual(saved['classes'][2]['output']['id'], 4)

    def test_collection_profile_does_not_rename_an_existing_model(self):
        from gesture_config import preset
        app = self.app
        app.apply_gesture_profile(preset('腕部五类 · 优先试验'))
        panel = app.field_training
        self.assertTrue(panel.load_package(backend_path()/'artifacts/cnn_fusion_pc_trial_20261029/export'))
        self.assertEqual(len(panel.class_names), 7)
        self.assertEqual(panel.app_gestures()[3][0], '掌屈')
        self.assertEqual(app.gesture_profile['classes'][3]['name'], '掌屈')

    def test_custom_model_live_names_and_independent_mapping_roundtrip(self):
        import copy
        import hashlib
        import json
        from gesture_config import preset
        backend = backend_path()
        if str(backend) not in sys.path:
            sys.path.insert(0, str(backend))
        from pc_ml.models import make_model
        from pc_ml.quantize import calibrate, export
        app = self.app
        panel = app.field_training
        profile = preset('基础三类 · 握拳张掌')
        data = np.random.default_rng(44).normal(size=(4, 8, 128)).astype(np.float32)
        integer = calibrate(make_model('sequential', 3).eval(), 'sequential', data)
        package = Path(self.settings_dir.name)/'custom_export'
        descriptor = export(integer, package, np.zeros(8, int), 'test', [c['name'] for c in profile['classes']])
        descriptor.update(gesture_profile=profile, action_mapping=[c['output'] for c in profile['classes']])
        (package/'manifest.json').write_text(json.dumps(descriptor, ensure_ascii=False), encoding='utf-8')
        weights_before = (package/'parameters.bin').read_bytes()
        self.assertTrue(panel.load_package(package))
        app.apply_gesture_profile(preset('腕部五类 · 优先试验'))
        app.core.running = True
        app.core.last_data_time = time.monotonic()
        panel.toggle_live()
        panel.events.put(('prediction', (panel.live_generation, {'stable': 2, 'margin_q8': 512})))
        panel.poll()
        self.assertEqual(len(app.prob_bars), 3)
        self.assertEqual(app.lbl_pred_gesture.cget('text'), '【张开手掌】')
        self.assertIn('五指张开', app.lbl_pred_output.cget('text'))
        mapping = copy.deepcopy(panel.action_mapping)
        mapping[2] = {'id': 9, 'name': '我的抓握方式'}
        panel.save_mapping(mapping)
        self.assertEqual((package/'parameters.bin').read_bytes(), weights_before)
        self.assertTrue(panel.load_package(package))
        self.assertEqual(panel.action_mapping[2]['name'], '我的抓握方式')

    def test_field_model_owns_recognition_button_and_hides_legacy_selector(self):
        panel = self.app.field_training
        self.assertTrue(panel.load_package(backend_path()/'artifacts/cnn_fusion_pc_trial_20261029/export'))
        self.assertEqual(self.app.active_model_type, 'field_waiting')
        self.assertEqual(self.app.model_select_row.winfo_manager(), '')
        with patch.object(panel, 'toggle_live') as toggle:
            self.app._toggle_recognition()
            toggle.assert_called_once()

    def test_rotation_selection_policy_preserves_archived_profile(self):
        import copy
        from gesture_dialog import GestureProfileDialog
        from gesture_config import preset, action, filter_supported_profile, EXCLUDED_ACTION_KEYS
        archive = preset()
        for key in EXCLUDED_ACTION_KEYS:
            row = action('custom', len(archive['classes']))
            row.update(key=key, name='翻掌向上')
            archive['classes'].append(row)
        original = copy.deepcopy(archive)
        filtered = filter_supported_profile(archive)
        self.assertEqual(archive, original)
        self.assertFalse(EXCLUDED_ACTION_KEYS.intersection(r['key'] for r in filtered['classes']))
        dialog = GestureProfileDialog(self.app)
        try:
            dialog.load(archive)
            for key in EXCLUDED_ACTION_KEYS:
                dialog.toggle_action(key)
            self.assertEqual(len(dialog.collect()['classes']), 7)
            dialog.clear_selection()
            for key in ('wrist_flexion', 'radial_deviation', 'index_flexion'):
                dialog.toggle_action(key)
            self.assertEqual([r['id'] for r in dialog.collect()['classes']], [0,1,2,3])
        finally:
            dialog.destroy()

    def test_gallery_pagination_keeps_all_finger_and_wrist_actions_reachable(self):
        from gesture_dialog import GestureProfileDialog
        dialog = GestureProfileDialog(self.app)
        try:
            dialog.update_idletasks()
            dialog.choose_category('全部')
            visited = set()
            for index in range(dialog._page_count):
                visited.update(entry[0] for entry in dialog.visible_entries)
                dialog.change_page(1)
            self.assertEqual(visited, {entry[0] for entry in dialog.catalog()})
            self.assertEqual(len(visited), 50)
            dialog.search.set('捏取')
            self.assertTrue(all('捏取' in entry[1]['name'] or '捏取' in entry[1]['instruction']
                                for entry in dialog.visible_entries))
        finally:
            dialog.destroy()

    def test_compact_pages_keep_active_controls_visible_without_scrollbars(self):
        from gesture_config import action
        app = self.app
        self.assertTrue(app.field_training.load_package(backend_path()/'artifacts/cnn_fusion_pc_trial_20261029/export'))
        app.deiconify()
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        for width,height in ((1280,720),(1366,768),(1920,1080)):
            app.geometry(f'{width}x{height}')
            for index in range(4):
                app.notebook.select(index)
                app.show_collection_gesture(action('radial_deviation',1))
                app.lbl_action.configure(text='向拇指侧偏腕')
                app.lbl_stage.configure(text='P1 · 12/24 · 录制')
                app.lbl_countdown.configure(text='3.0 s')
                app.lbl_next_action.configure(text='下一步：静息 → 向小指侧偏腕')
                app.update()
                for widget in descendants(app):
                    if not widget.winfo_ismapped():
                        continue
                    self.assertNotIn('Scrollbar', widget.winfo_class())
                    parent=widget.master
                    self.assertGreaterEqual(widget.winfo_x(), -2, str(widget))
                    self.assertGreaterEqual(widget.winfo_y(), -2, str(widget))
                    self.assertLessEqual(widget.winfo_x()+widget.winfo_width(), parent.winfo_width()+3, str(widget))
                    self.assertLessEqual(widget.winfo_y()+widget.winfo_height(), parent.winfo_height()+3, str(widget))

    def test_removed_training_block_is_not_visible(self):
        self.app.notebook.select(3)
        self.app.deiconify();self.app.update()
        def labels(widget):
            for child in widget.winfo_children():
                if 'text' in child.keys() and child.winfo_ismapped():yield str(child.cget('text'))
                yield from labels(child)
        texts=list(labels(self.app.field_training.tab))
        for name in ('选择参数包','动作映射','查看报告','模型回放'):
            self.assertNotIn(name,texts)
        self.assertFalse(hasattr(self.app.field_training,'model_label_widget'))

    def test_waveform_replay_does_not_feed_recording_or_inference(self):
        from waveform_replay import WaveformReplay
        from signal_processing import EMGSignalFilter
        app=self.app
        raw=np.random.default_rng(123).normal(1580,8,(1000,8)).astype(np.float32)
        path=Path(self.settings_dir.name)/'wave.npz'
        np.savez_compressed(path,emg=raw,sample_rate=1000.)
        replay=WaveformReplay(app)
        try:
            replay.load(path,offset=600)
            display,filtered,*_=app.core.get_display_snapshots()
            np.testing.assert_array_equal(display,raw[100:600])
            expected=EMGSignalFilter().process(raw)[100:600]
            np.testing.assert_array_equal(filtered,expected)
            self.assertFalse(app.core.running)
            self.assertFalse(app.core.is_recording)
            self.assertFalse(app.core.field_stream_enabled)
            self.assertTrue(app.core.field_stream.empty())
            self.assertEqual(app.core.record_buf,[])
            self.assertEqual(app.core.total_received,0)
            self.assertEqual(app.lbl_link_status.cget('text'),'离线回放')
            app.core.running=True
            with self.assertRaises(ValueError):replay.load(path)
            app.core.running=False
        finally:
            replay.stop()
        self.assertEqual(app.core.disp_count,0)
        self.assertEqual(app.lbl_link_status.cget('text'),'未连接')

    def test_collection_guide_follows_collection_across_tabs(self):
        app=self.app
        app.deiconify();app.notebook.select(1);app.update()
        self.assertTrue(app.collection_guide.winfo_ismapped())
        self.assertLess(app.collection_guide.winfo_rooty(),app.canvas.winfo_rooty())
        self.assertLess(app.btn_start.winfo_rooty(),app.entry_subject.winfo_rooty())
        app.notebook.select(0);app._update_collection_focus();app.update()
        self.assertFalse(app.collection_guide.winfo_ismapped())
        app.core.is_recording=True
        app._update_collection_focus();app.update()
        self.assertTrue(app.collection_guide.winfo_ismapped())
        app.core.is_recording=False


if __name__ == '__main__':
    unittest.main()

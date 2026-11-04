"""Original workstation integration, without serial devices or actuators."""
import importlib.util
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
    @classmethod
    def setUpClass(cls):
        torch.set_num_threads(4)

    def setUp(self):
        self.settings_dir = tempfile.TemporaryDirectory()
        self.settings_path = Path(self.settings_dir.name)/'filtered_cnn_default.json'
        self.settings_patch = patch('field_training.DEFAULT_PACKAGE_FILE', self.settings_path)
        self.settings_patch.start()
        self.profile_patch = patch('gesture_config.PROFILE_FILE', Path(self.settings_dir.name)/'gesture_profile.json')
        self.profile_patch.start()
        self.ports = patch.object(studio.list_ports, 'comports', return_value=[])
        self.ports.start()
        self.app = studio.EMGStudioGUI()
        self.app.withdraw()

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
        self.profile_patch.stop()
        self.settings_dir.cleanup()

    def test_original_ui_and_saved_recording_handoff(self):
        app = self.app
        self.assertEqual(len(app.notebook.tabs()), 4)
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
            dialog.detail_output.set('三指抓握')
            dialog.change_output()
            dialog.toggle_action('wrist_flexion')
            profile = validate_profile(dialog.collect())
            self.assertEqual([c['id'] for c in profile['classes']], [0, 1, 2])
            self.assertEqual([c['key'] for c in profile['classes']], ['rest', 'tip_pinch', 'radial_deviation'])
            self.assertEqual(profile['classes'][1]['output']['id'], 4)
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

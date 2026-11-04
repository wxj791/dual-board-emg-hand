"""Field CNN controls embedded in the existing EMG Studio V3 workstation.

Tk is touched only on the main thread. Training runs in a child process;
integer inference consumes an ordered raw queue in a dedicated worker.
"""
from __future__ import annotations

from datetime import datetime
from collections import deque
from ui_style import PANEL, PLOT, FG, MUTED, BODY, button, label, check, short, open_path
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

DEFAULT_PACKAGE_FILE = Path(__file__).resolve().parent/'models/filtered_cnn_default.json'
FUSION_MODE = '统计融合'
LEGACY_MODE = '旧版统计＋CNN'
CNN_FUSION_MODE = '统计＋时序CNN'


def preferred_package_file():
    return Path(os.environ['EMG_FIELD_SETTINGS_FILE']) if os.environ.get('EMG_FIELD_SETTINGS_FILE') else DEFAULT_PACKAGE_FILE


def format_report_metrics(report):
    metric = report['evaluation']['test']['stable']
    return (f'留出准确率  {metric["accuracy"]:.2%}\n'
            f'最低召回  {min(metric["recall"]):.2%}  ·  静息误触发  {metric["rest_false_activation"]:.2%}')


def backend_path():
    configured = os.environ.get('EMG_FPGA_PROJECT')
    candidates = ([Path(configured)] if configured else []) + [
        Path(__file__).resolve().parents[2]/'Projects/emg_hand_fpga',
        Path('E:/FPGA/Projects/emg_hand_fpga')]
    for path in candidates:
        if (path/'pc_ml/field.py').is_file():
            return path.resolve()
    raise FileNotFoundError('未找到现场训练后端。请将 emg_hand_fpga 放在 FPGA/Projects 下，或设置 EMG_FPGA_PROJECT。')


class FieldTrainingPanel:
    def __init__(self, app):
        self.app = app
        self.events = queue.Queue()
        self.worker = None
        self.process = None
        self.run_dir = None
        self.live_generation = None
        self.live_worker = None
        self.closed = False
        self.live_stop = threading.Event()
        self._saved_names = {}
        self._capture_readback = False
        self.source = tk.StringVar()
        self.model_text = tk.StringVar(value='尚未载入现场 CNN 参数')
        self.status = tk.StringVar(value='等待采集或选择记录')
        self.auto_train = tk.BooleanVar(value=False)
        self.training_mode = tk.StringVar(value=CNN_FUSION_MODE if os.environ.get('EMG_FIELD_TRAINING_MODE')=='cnn_fusion' else FUSION_MODE if os.environ.get('EMG_FIELD_TRAINING_MODE')=='fusion' else LEGACY_MODE)
        self.save_trial = tk.BooleanVar(value=True)
        self.trial_dir = None
        self.model_label = '现场模型'
        self.package = None
        self.class_names = ['静息', '握拳', '张开手掌', '点赞', '剪刀手', '数字一', '数字六']
        self.gesture_profile = None
        self.action_mapping = []
        self.parameter_sha256 = None
        self.prefer_filtered = False
        self.task_data_error = None
        self.tab = tk.Frame(app.notebook, bg=PANEL, padx=12, pady=10)
        app.notebook.add(self.tab, text='现场训练')
        self._build()
        # A toggle within the original inference page makes the active engine clear.
        self.legacy_model_text = app.lbl_model_file.cget('text')
        self.restore_default()
        app.after(100, self.poll)

    def restore_default(self):
        """An unavailable preferred model must not silently enable the old model."""
        override = os.environ.get('EMG_FIELD_DEFAULT_PACKAGE')
        preferred = preferred_package_file()
        if not override and not preferred.is_file():
            return
        self.prefer_filtered = True
        self.app.infer_active_var.set(False)
        self.app.active_model_type = 'field_waiting'
        self.app.lbl_model_file.configure(text='当前模型: 现场模型（等待参数及串口）')
        try:
            saved = json.loads(preferred.read_text(encoding='utf-8')) if preferred.is_file() else {'package': override}
            if self.load_package(Path(saved['package'])):
                self.app.lbl_model_file.configure(text=f'{self.model_label} · {len(self.class_names)}类')
                self.app.waveform_mode.set('filtered')
                self.app._on_waveform_mode_changed()
                self.app.waveform_range.set('100')
                self.status.set('模型已就绪，等待连接')
        except Exception as exc:
            self.status.set(f'默认现场模型不可用：{exc}。请重新选择参数包。')

    def clear_default(self):
        self.prefer_filtered = False
        preferred = preferred_package_file()
        if preferred.exists():
            preferred.unlink()

    def resume_default(self):
        if self.prefer_filtered and self.package is not None and self.live_generation is None:
            self.toggle_live()

    @property
    def busy(self):
        return self.worker is not None and self.worker.is_alive()

    def _build(self):
        tab = self.tab
        self.log_lines = deque(maxlen=1500)
        self.log_path = Path(__file__).resolve().parents[1]/'runtime/ui_logs'/f'studio_{datetime.now():%Y%m%d_%H%M%S}.log'
        self.display_status = tk.StringVar()
        self.status.trace_add('write', lambda *args: self._sync_status())
        self._sync_status()
        ttk.Combobox(tab, textvariable=self.training_mode,
                     values=[CNN_FUSION_MODE, FUSION_MODE, LEGACY_MODE],
                     state='readonly').pack(fill='x', pady=(0, 8))
        row = tk.Frame(tab, bg=PANEL); row.pack(fill='x', pady=(0, 5))
        self.source_display = tk.StringVar()
        def source_changed(*args):
            self.source_display.set(self.source.get().replace(chr(92),'/').rsplit('/',1)[-1] if self.source.get() else '选择采集记录')
        self.source.trace_add('write', source_changed); source_changed()
        ttk.Entry(row, textvariable=self.source_display, state='readonly').pack(side='left', fill='x', expand=True)
        button(row, '选记录', self.choose).pack(side='left', padx=(8, 0))
        check(tab, '保存后自动训练', self.auto_train).pack(anchor='w', pady=(0, 5))
        self.train_button = button(tab, '开始训练', self.train, primary=True)
        self.train_button.pack(fill='x', pady=(0, 6))
        self.status_label = label(tab, variable=self.display_status, muted=True,
                                  anchor='w', justify='left', wraplength=520, height=1)
        self.status_label.pack(fill='x', pady=(0, 3))
        self.result = label(tab, '尚无评估结果', anchor='w', justify='left', wraplength=520, height=2)
        self.result.pack(fill='x', pady=(0, 8))
        ttk.Separator(tab).pack(fill='x', pady=(0, 8))
        self.live_button = button(tab, '开始识别', self.toggle_live, primary=True)
        self.live_button.pack(fill='x', pady=(8, 5))
        check(tab, '保存识别记录', self.save_trial).pack(anchor='w', pady=(0, 4))
        files = tk.Frame(tab, bg=PANEL); files.pack(fill='x', side='bottom', pady=(4, 0))
        for c in range(2): files.columnconfigure(c, weight=1, uniform='files')
        button(files, '运行日志', self.open_log).grid(row=0, column=0, sticky='ew', padx=(0, 5))
        button(files, '试用数据', self.open_trial).grid(row=0, column=1, sticky='ew', padx=(5, 0))
        def resize(event):
            width = max(280, event.width-28)
            for widget in (self.status_label, self.result):
                widget.configure(wraplength=width)
        tab.bind('<Configure>', resize)

    def _sync_status(self):
        text = self.status.get()
        self.display_status.set(short(text, 30))
        self._append_log(text+'\n')

    def _append_log(self, text):
        self.log_lines.append(text)
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open('a', encoding='utf-8') as handle:
                handle.write(text)
        except OSError:
            pass

    def open_log(self):
        if not self.log_path.is_file():
            self._append_log(''.join(self.log_lines) or '暂无运行日志\n')
        try:
            open_path(self.log_path)
        except OSError as exc:
            messagebox.showerror('日志无法打开', str(exc), parent=self.app)

    def recording_saved(self, path, complete):
        """Called from the original collection worker, without touching Tk."""
        self.events.put(('saved', (str(path), bool(complete))))

    def choose(self):
        root = Path(__file__).resolve().parent/'data/raw'
        path = filedialog.askopenfilename(parent=self.app, initialdir=root,
                                          filetypes=[('肌电记录', '*.npz')])
        if path:
            self.source.set(path)
            self.status.set('记录已选择')

    def train(self, automatic=False):
        if self.busy:
            return
        if self.app.core.is_recording:
            if not automatic:
                messagebox.showinfo('正在采集', '请等本轮保存后再训练，或勾选保存后自动训练。', parent=self.app)
            return
        if self.app.paradigm_thread and self.app.paradigm_thread.is_alive():
            # A live worker may still be counting down (before recording) or may
            # be paused waiting for the next round. Detect the latter explicitly.
            if not self.app.collection_paused.is_set():
                if not automatic:
                    messagebox.showinfo('采集准备中', '请等待本轮采集结束并保存。', parent=self.app)
                return
        path = Path(self.source.get())
        if not path.is_file():
            if not automatic:
                messagebox.showinfo('先选记录', '先完成一轮采集，或选择已有 .npz 文件。', parent=self.app)
            return
        try:
            backend = backend_path()
        except Exception as exc:
            self.status.set(str(exc))
            return
        self.stop_live()
        self._capture_readback = self.app.infer_active_var.get()
        self.app.infer_active_var.set(False)
        self.run_dir = backend/'artifacts/pc_ml/sessions'/datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        self.train_button.configure(state='disabled')
        self.app.btn_start.configure(state='disabled')
        self.status.set('训练中…')
        module = ('pc_ml.cnn_fusion_field' if self.training_mode.get()==CNN_FUSION_MODE else
                  'pc_ml.fusion_field' if self.training_mode.get()==FUSION_MODE else 'pc_ml.fast_field')
        self.launch(['-m', module, '--recording', str(path.resolve()), '--output', str(self.run_dir)], 'train', backend)

    def launch(self, arguments, action, backend):
        if self.busy:
            return
        # Capture inputs on Tk thread; worker does not call Tk methods.
        run_dir = self.run_dir
        self.task_data_error = None
        def work():
            try:
                process = subprocess.Popen([sys.executable, '-X', 'utf8', '-u', *arguments], cwd=backend,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding='utf-8', errors='replace',
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self.process = process
                for line in process.stdout:
                    self.events.put(('log', line))
                code = process.wait()
            except Exception as exc:
                self.events.put(('log', f'{exc}\n'))
                code = 1
            self.events.put(('done', (action, code, run_dir)))
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def choose_package(self):
        if self.busy:
            self.status.set('请等待当前训练或回放完成，再切换参数包。')
            return
        directory = filedialog.askdirectory(parent=self.app, title='选择包含 manifest.json 和 parameters.bin 的 export 文件夹')
        if directory:
            self.load_package(Path(directory))

    def load_package(self, path):
        try:
            backend = backend_path()
            if str(backend) not in sys.path:
                sys.path.insert(0, str(backend))
            from pc_ml.package_api import load_manifest
            manifest = load_manifest(path)
            names = manifest['class_names']
            profile = manifest.get('gesture_profile')
            if profile:
                from gesture_config import validate_profile
                profile = validate_profile(profile)
                if names != [row['name'] for row in profile['classes']]:
                    raise ValueError('参数包类别顺序与其动作方案不一致')
            mapping = manifest.get('action_mapping', [])
            if mapping and len(mapping) != len(names):
                raise ValueError('参数包机械手映射数量与类别数不一致')
            if profile and mapping != [row['output'] for row in profile['classes']]:
                raise ValueError('参数包机械手映射与动作方案不一致')
            from gesture_config import validate_mapping
            override = Path(path)/'hand_mapping.json'
            if override.is_file():
                saved_mapping = json.loads(override.read_text(encoding='utf-8'))
                if saved_mapping['parameters_sha256'] != manifest['sha256'] or saved_mapping['class_names'] != names:
                    raise ValueError('独立动作映射属于另一份模型，请核对hand_mapping.json')
                mapping = saved_mapping['mapping']
            if mapping:
                mapping = validate_mapping(mapping, len(names))
            self.stop_live()
            self.package = Path(path)
            front=(manifest.get('frontend') or {}).get('name')
            self.filter_description = ('20Hz高通 + 工频及谐波陷波 + 450Hz低通' if front=='clean'
                                       else '20Hz高通 + 50Hz陷波 + 450Hz低通' if front=='emg'
                                       else '5Hz高通 + 50Hz陷波 + 200Hz低通')
            self.class_names = list(names)
            self.gesture_profile = profile
            self.action_mapping = mapping
            self.parameter_sha256 = manifest['sha256']
            report_path = self.package.parent/'field_report.json'
            if report_path.is_file():
                report = json.loads(report_path.read_text(encoding='utf-8'))
                self.source.set(report['recording'])
                m = report['evaluation']['test']
                self.result.configure(text=format_report_metrics(report))
            if manifest.get('metadata',{}).get('runtime')=='cnn_fusion_clean256_v1':
                algorithm=CNN_FUSION_MODE
                self.training_mode.set(CNN_FUSION_MODE)
            elif manifest.get('metadata',{}).get('runtime')=='parallel_fusion_v1':
                algorithm=FUSION_MODE
                self.training_mode.set(FUSION_MODE)
            else:
                algorithm=('特征＋CNN整数模型' if manifest.get('cnn_correction_active') else '整数特征模型（CNN未启用）') if manifest['format']=='EMG-HYBRID-INT-1' else '现场CNN'
            self.model_label=algorithm
            self.model_text.set(f'{algorithm} · {len(names)}类')
            self.app.set_inference_classes(names, show_scores=False)
            self.app.active_model_type = 'field_waiting'
            self.app.infer_active_var.set(False)
            self.app.infer_toggle.configure(text='开始识别')
            self.app.model_select_row.pack_forget()
            self.app.lbl_model_file.configure(text=f'{algorithm} · {len(names)}类', fg=FG)
            self.status.set('模型已就绪')
            return True
        except Exception as exc:
            self.status.set(f'参数载入失败：{exc}')
            return False

    def app_gestures(self):
        return {i: (name,) for i, name in enumerate(self.class_names)}

    def save_mapping(self, mapping):
        from gesture_config import validate_mapping
        if self.package is None:
            raise ValueError('请先训练或载入模型，再配置它的机械手输出。')
        mapping = validate_mapping(mapping, len(self.class_names))
        saved = {'version': 1, 'parameters_sha256': self.parameter_sha256,
                 'class_names': self.class_names, 'mapping': mapping}
        path = self.package/'hand_mapping.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(saved, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        temporary.replace(path)
        self.action_mapping = mapping
        self.status.set('动作映射已保存 · 电脑预览')

    def edit_mapping(self):
        if self.busy:
            self.status.set('请等待当前任务结束')
            return
        if self.package is None:
            self.status.set('请先训练或选择参数包')
            return
        from gesture_config import OUTPUT_ACTIONS
        dialog = tk.Toplevel(self.app)
        dialog.title('动作映射')
        columns = 2 if len(self.class_names)>8 else 1
        rows_count = (len(self.class_names)+columns-1)//columns
        dialog.geometry(f'{1080 if columns==2 else 640}x{max(300, rows_count*56+132)}')
        dialog.configure(bg=PANEL); dialog.transient(self.app); dialog.grab_set()
        label(dialog, '输入动作 → 机械手响应', size=18, bold=True).pack(pady=12)
        rows = tk.Frame(dialog, bg=PANEL); rows.pack(fill='both', expand=True, padx=16)
        variables = []
        for i, name in enumerate(self.class_names):
            output = self.action_mapping[i] if self.action_mapping else {'id':0, 'name':OUTPUT_ACTIONS[0]}
            variable = tk.StringVar(value=output['name']); variables.append(variable)
            base = (i//rows_count)*2; row = i%rows_count
            label(rows, short(name, 12), anchor='w').grid(row=row, column=base, sticky='w', padx=(0,10), pady=6)
            names = list(OUTPUT_ACTIONS.values())
            if output['name'] not in names: names.append(output['name'])
            ttk.Combobox(rows, textvariable=variable, values=names, width=19,
                         state='disabled' if i==0 else 'readonly').grid(row=row, column=base+1, sticky='ew', padx=(0,16), pady=6)
        for col in range(columns): rows.columnconfigure(col*2+1, weight=1)
        def save():
            try:
                mapping = []
                for i, variable in enumerate(variables):
                    name = variable.get()
                    code = next((j for j,text in OUTPUT_ACTIONS.items() if text==name), None)
                    if code is None and self.action_mapping and name==self.action_mapping[i]['name']:
                        code = self.action_mapping[i]['id']
                    if code is None: raise ValueError('请选择机械手响应')
                    mapping.append({'id':code, 'name':name})
                self.save_mapping(mapping); dialog.destroy()
            except (ValueError,OSError) as exc:
                messagebox.showerror('映射无法保存', str(exc), parent=dialog)
        button(dialog, '保存映射', save, primary=True).pack(pady=12)

    def toggle_live(self):
        if self.live_generation is not None:
            self.stop_live()
            return
        if self.busy:
            messagebox.showinfo('任务运行中', '请等待训练或回放完成。', parent=self.app)
            return
        if self.live_worker and self.live_worker.is_alive():
            self.status.set('上一轮识别正在退出，请稍后重试。')
            return
        if not self.app.core.running:
            messagebox.showinfo('先连接手环', '使用顶部原有串口工具连接手环接收端。', parent=self.app)
            return
        if self.package is None:
            messagebox.showinfo('先训练或载入', '训练完成后自动就绪，也可以选择已有参数包。', parent=self.app)
            return
        try:
            backend = backend_path()
            if str(backend) not in sys.path:
                sys.path.insert(0, str(backend))
            from pc_ml.package_api import predictor as StreamingPredictor
            import torch
            torch.set_num_threads(2)
            predictor = StreamingPredictor(self.package)
            trial = None
            if self.save_trial.get():
                from pc_ml.trial_log import TrialLog
                from pc_ml.package_api import load_manifest
                trial_root = Path(os.environ.get('EMG_LIVE_TRIAL_ROOT', str(backend/'artifacts/pc_ml/live_trials')))
                self.trial_dir = trial_root/datetime.now().strftime('%Y%m%d_%H%M%S_%f')
                trial = TrialLog(self.trial_dir, self.package, load_manifest(self.package))
        except Exception as exc:
            self.status.set(f'无法启动识别：{exc}')
            return
        generation = self.app.core.enable_field_stream(True)
        self.live_generation = generation
        self.live_stop = threading.Event()
        stop_event = self.live_stop
        self.legacy_model_text = self.app.lbl_model_file.cget('text')
        self.legacy_model_type = getattr(self.app, 'active_model_type', '')
        self.legacy_names = list(self.app.inference_names)
        self.app.set_inference_classes(self.class_names, show_scores=False)
        self.app.active_model_type = 'field_int8'
        self.app.infer_active_var.set(True)
        self.app.lbl_model_file.configure(text=f'{self.model_label} · {len(self.class_names)}类 · 电脑执行')
        self.live_button.configure(text='停止识别')
        self.app.infer_toggle.configure(text='停止识别')
        self.app.lbl_pred_gesture.configure(text='等待预热', fg='#F9E2AF')
        self.app.lbl_pred_conf.configure(text='分类间隔：--（不是概率）')
        self.app.lbl_pred_output.configure(text='机械手响应：等待识别 · 电脑预览')
        self._clear_bars()
        self.status.set('识别中 · 正在预热')
        def work():
            error = None
            try:
                while not stop_event.is_set():
                    try:
                        gen, reset, raw = self.app.core.field_stream.get(timeout=.1)
                    except queue.Empty:
                        continue
                    if gen != generation:
                        continue
                    if reset:
                        predictor.reset()
                    if trial:trial.append(raw, reset)
                    result = predictor.push(raw)
                    if trial:trial.append_predictions(result)
                    if result:
                        self.events.put(('prediction', (generation, result[-1])))
            except Exception as exc:
                error = str(exc)
                self.events.put(('live_error', (generation, error)))
            finally:
                if trial:
                    try:
                        trial.close(error)
                        self.events.put(('trial_saved', str(trial.directory)))
                    except Exception as exc:
                        self.events.put(('log', f'试用数据整理失败，原始分块仍在 {trial.directory}：{exc}\n'))
        self.live_worker = threading.Thread(target=work, daemon=True)
        self.live_worker.start()
        self.prefer_filtered = True
        try:
            preferred = preferred_package_file()
            preferred.parent.mkdir(parents=True, exist_ok=True)
            preferred.write_text(json.dumps({'package': str(self.package.resolve())}, indent=2)+'\n', encoding='utf-8')
        except OSError as exc:
            self.status.set(f'现场识别已启动，但无法保存默认模型：{exc}')
        # Existing inference card is the single place to see live predictions.
        self.app.notebook.select(2)

    def _clear_bars(self):
        for bar, label in zip(self.app.prob_bars, self.app.prob_labels):
            bar['value'] = 0
            label.configure(text='--')

    def stop_live(self):
        if self.live_generation is None:
            return
        self.live_stop.set()
        self.app.core.enable_field_stream(False)
        if self.live_worker and self.live_worker.is_alive():
            self.live_worker.join(timeout=2)
        self.live_generation = None
        self.live_button.configure(text='开始识别')
        self.app.infer_toggle.configure(text='开始识别')
        self.app.infer_active_var.set(False)
        self.app.active_model_type = self.legacy_model_type
        self.app.lbl_model_file.configure(text=self.legacy_model_text)
        self.app.lbl_pred_gesture.configure(text='识别已停止', fg='#A6ADC8')
        self.app.lbl_pred_conf.configure(text='分类间隔：--')
        self.app.lbl_pred_output.configure(text='')
        self.app.set_inference_classes(self.legacy_names, show_scores=not self.legacy_model_type.startswith('field_'))
        self._clear_bars()
        self.status.set('识别已停止')

    def open_trial(self):
        if self.trial_dir and self.trial_dir.is_dir():
            open_path(self.trial_dir)
        else:
            self.status.set('开启“保存实时试用信号和识别结果”后，开始一次实时识别即可生成数据。')

    def open_report(self):
        directory = self.run_dir or (self.package.parent if self.package else None)
        if directory and directory.exists():
            open_path(directory)
        else:
            self.status.set('训练完成后，这里可打开本轮参数和报告。')

    def replay(self):
        if self.busy:
            return
        source = Path(self.source.get())
        if not source.is_file() or self.package is None:
            self.status.set('先选择记录，并训练或载入参数包。')
            return
        try:
            backend = backend_path()
        except Exception as exc:
            self.status.set(str(exc))
            return
        directory = self.package.parent
        self.replay_output = directory/'replay/replay.png'
        self.status.set('正在生成模型回放图。回放包含训练片段，不能代替独立测试。')
        self.launch(['-m', 'pc_ml.replay', '--recording', str(source.resolve()), '--package', str(self.package),
                     '--output', str(directory/'replay')], 'replay', backend)

    def poll(self):
        if self.closed:
            return
        for _ in range(100):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'saved':
                path, complete = value
                if complete:
                    self.source.set(path)
                    self.status.set(f'已接入本轮记录：{Path(path).name}')
                    if self.auto_train.get():
                        self._saved_names[path] = False
                else:
                    self.status.set('中止记录已保存')
            elif kind == 'log':
                if value.startswith('DATA_ERROR: '):
                    self.task_data_error = value[len('DATA_ERROR: '):].strip()
                self._append_log(value)
            elif kind == 'trial_saved':
                self.trial_dir = Path(value)
                self._append_log(f'试用数据已保存：{value}\n')
            elif kind == 'done':
                action, code, run_dir = value
                self.train_button.configure(state='normal')
                if not (self.app.paradigm_thread and self.app.paradigm_thread.is_alive()):
                    self.app.btn_start.configure(state='normal')
                if code:
                    self.status.set(self.task_data_error or '操作失败，请看日志。原始记录和旧模型均保留。')
                    if action == 'train':
                        self.app.infer_active_var.set(self._capture_readback)
                elif action == 'train':
                    report = json.loads((run_dir/'field_report.json').read_text(encoding='utf-8'))
                    m = report['evaluation']['test']
                    self.load_package(run_dir/'export')
                    self.result.configure(text=format_report_metrics(report))
                    acceptance=report.get('acceptance',{})
                    weak='、'.join(acceptance.get('weak_classes',[]))
                    timing=report.get('timing_seconds',{})
                    elapsed=timing.get('total') if isinstance(timing,dict) else timing
                    duration=f'，用时{elapsed:.1f}秒' if elapsed is not None else ''
                    verdict=('留出验收通过；请再采一轮验证实时效果。' if acceptance.get('passed_offline')
                             else '留出验收未通过。'+(f'薄弱动作：{weak}。' if weak else '请检查记录质量。'))
                    self.status.set('训练完成'+duration+'。'+verdict)
                else:
                    self.status.set('模型回放已生成')
                    open_path(self.replay_output)
            elif kind == 'prediction':
                generation, prediction = value
                if generation != self.live_generation or not self.app.infer_active_var.get():
                    continue
                name = self.app_gestures()[prediction['stable']][0]
                self.app.lbl_pred_gesture.configure(text=f'【{name}】', fg='#89B4FA')
                self.app.lbl_pred_conf.configure(text=f'分类间隔: {prediction["margin_q8"]/256:.2f}（非概率）')
                if self.action_mapping:
                    output = self.action_mapping[prediction['stable']]
                    self.app.lbl_pred_output.configure(text=f'机械手响应：{output["name"]} · 电脑预览')
                else:
                    self.app.lbl_pred_output.configure(text='此旧参数包没有机械手动作映射')
            elif kind == 'live_error':
                generation, message = value
                if generation == self.live_generation:
                    self.stop_live()
                    self.status.set(f'现场识别失败：{message}')
        if self.auto_train.get() and not self.busy and not self.app.core.is_recording:
            thread = self.app.paradigm_thread
            if not thread or not thread.is_alive() or self.app.collection_paused.is_set():
                for path, attempted in list(self._saved_names.items()):
                    if not attempted:
                        self._saved_names[path] = True
                        self.source.set(path)
                        self.train(automatic=True)
                        break
        if self.live_generation is not None:
            if not self.app.core.running or not self.app.infer_active_var.get():
                self.stop_live()
            elif self.app.core.last_data_time is not None and __import__('time').monotonic()-self.app.core.last_data_time > .5:
                self.app.lbl_pred_gesture.configure(text='等待新数据', fg='#F9E2AF')
                self.app.lbl_pred_conf.configure(text='数据中断，恢复后重新预热')
        self.app.after(100, self.poll)

    def close(self):
        self.closed = True
        self.stop_live()

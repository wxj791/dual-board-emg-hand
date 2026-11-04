"""Field CNN controls embedded in the existing EMG Studio V3 workstation.

Tk is touched only on the main thread. Training runs in a child process;
integer inference consumes an ordered raw queue in a dedicated worker.
"""
from __future__ import annotations

from datetime import datetime
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
FUSION_MODE = '并行融合（波形＋包络＋通道）'
LEGACY_MODE = '原统计＋CNN'


def preferred_package_file():
    return Path(os.environ['EMG_FIELD_SETTINGS_FILE']) if os.environ.get('EMG_FIELD_SETTINGS_FILE') else DEFAULT_PACKAGE_FILE


def format_report_metrics(report):
    m=report['evaluation']['test']
    if report.get('preset')=='parallel_fusion_v1':
        text=(f'完整重复留出：原始 {m["integer"]["accuracy"]:.2%} · 平滑 {m["stable"]["accuracy"]:.2%}\n'
              f'平滑最差动作 {min(m["stable"]["recall"]):.2%} · 静息误触发 {m["stable"]["rest_false_activation"]:.2%}')
        calibration=report.get('calibration_fit')
        if calibration:text+=f'\n本次采集训练自检 {calibration["accuracy"]:.2%}（非实时验收）'
        return text
    text=(f'留出重复测试：浮点 {m["float"]["accuracy"]:.2%} · 整数 {m["integer"]["accuracy"]:.2%}\n'
          f'静息误触发 {m["integer"]["rest_false_activation"]:.2%} · 稳定输出 {m["stable"]["accuracy"]:.2%}')
    calibration=report.get('calibration_fit')
    if calibration:
        text+=f'\n本次采集训练自检 {calibration["accuracy"]:.2%}（非实时验收）'
    return text


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
        self.status = tk.StringVar(value='先在“数据集录制”中选好动作，跟随图片完成采集。')
        self.auto_train = tk.BooleanVar(value=False)
        self.training_mode = tk.StringVar(value=FUSION_MODE if os.environ.get('EMG_FIELD_TRAINING_MODE')=='fusion' else LEGACY_MODE)
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
        self.tab = tk.Frame(app.notebook, bg='#2A2A3E', padx=10, pady=8)
        app.notebook.add(self.tab, text=' 现场训练 ')
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
                self.app.lbl_model_file.configure(text=f'当前模型: {self.model_label}（待连接）\n{self.package.parent.name}')
                self.app.waveform_mode.set('filtered')
                self.app._on_waveform_mode_changed()
                self.app.waveform_range.set('100')
                self.status.set(f'已载入{self.model_label}；连接手环后自动启动。可重新采集并训练本次佩戴。')
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
        def label(parent, text=None, variable=None, color='#CDD6F4', size=9):
            widget = tk.Label(parent, text=text, textvariable=variable, bg='#2A2A3E', fg=color,
                              font=('微软雅黑', size), anchor='w', justify='left', wraplength=395)
            widget.pack(fill='x', pady=3)
            return widget
        def button(parent, text, command, color='#3E4259'):
            widget = tk.Button(parent, text=text, command=command, bg=color, fg='#FFFFFF',
                               relief='flat', font=('微软雅黑', 9), padx=6, pady=4)
            widget.pack(fill='x', pady=3)
            return widget
        label(tab, '采集动作 → 开始训练 → 测试识别', color='#89B4FA', size=11)
        label(tab, '按图完成一轮采集，点击训练即可；无需调参。训练后会自动检查薄弱动作。')
        ttk.Combobox(tab, textvariable=self.training_mode, values=[FUSION_MODE, LEGACY_MODE],
                     state='readonly').pack(fill='x', pady=3)
        row = tk.Frame(tab, bg='#2A2A3E')
        row.pack(fill='x', pady=3)
        ttk.Entry(row, textvariable=self.source).pack(side='left', fill='x', expand=True)
        tk.Button(row, text='选记录', command=self.choose, bg='#3E4259', fg='#FFFFFF', relief='flat').pack(side='left', padx=(5, 0))
        tk.Checkbutton(tab, text='完整一轮保存后自动训练（等待采集退出/暂停）', variable=self.auto_train,
                       fg='#A6E3A1', bg='#2A2A3E', selectcolor='#181825', activebackground='#2A2A3E',
                       font=('微软雅黑', 9)).pack(anchor='w', pady=3)
        self.train_button = button(tab, '开始训练', self.train, '#166534')
        label(tab, variable=self.status, color='#F9E2AF')
        self.result = label(tab, '训练 / 验证 / 测试按完整动作重复划分。')
        separator = ttk.Separator(tab)
        separator.pack(fill='x', pady=6)
        label(tab, variable=self.model_text, color='#89B4FA')
        row2 = tk.Frame(tab, bg='#2A2A3E')
        row2.pack(fill='x')
        for text, callback in [('选参数包', self.choose_package), ('查看报告', self.open_report),
                               ('整数回放', self.replay), ('动作映射', self.edit_mapping)]:
            tk.Button(row2, text=text, command=callback, bg='#3E4259', fg='#FFFFFF', relief='flat',
                      font=('微软雅黑', 9), padx=6).pack(side='left', padx=(0, 6), pady=3)
        self.live_button = button(tab, '开启现场实时识别', self.toggle_live, '#1D4ED8')
        tk.Checkbutton(tab, text='保存实时试用信号和识别结果', variable=self.save_trial,
                       fg='#A6E3A1', bg='#2A2A3E', selectcolor='#181825',
                       activebackground='#2A2A3E', font=('微软雅黑', 9)).pack(anchor='w')
        button(tab, '打开试用数据', self.open_trial)
        label(tab, '训练完成后打开实时识别，在“模型实时验证”中观察结果。', size=8)
        label(tab, '运行方式：电脑预览', color='#A6ADC8', size=8)
        box = tk.Frame(tab, bg='#2A2A3E')
        box.pack(fill='both', expand=True, pady=(6, 0))
        self.log = tk.Text(box, height=6, width=1, bg='#181825', fg='#CDD6F4', insertbackground='white',
                           wrap='word', font=('Consolas', 8), state='disabled')
        scroll = ttk.Scrollbar(box, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')

    def recording_saved(self, path, complete):
        """Called from the original collection worker, without touching Tk."""
        self.events.put(('saved', (str(path), bool(complete))))

    def choose(self):
        root = Path(__file__).resolve().parent/'data/raw'
        path = filedialog.askopenfilename(parent=self.app, initialdir=root,
                                          filetypes=[('肌电记录', '*.npz')])
        if path:
            self.source.set(path)
            self.status.set('已选择记录；训练按文件中保存的动作方案进行，每类至少三个完整重复。')

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
        self.status.set('训练中：窗口继续接收波形，训练完成后可使用本轮模型识别。')
        module = 'pc_ml.fusion_field' if self.training_mode.get()==FUSION_MODE else 'pc_ml.fast_field'
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
            if manifest.get('metadata',{}).get('runtime')=='parallel_fusion_v1':
                algorithm=FUSION_MODE
                self.training_mode.set(FUSION_MODE)
            else:
                algorithm=('特征＋CNN整数模型' if manifest.get('cnn_correction_active') else '整数特征模型（CNN未启用）') if manifest['format']=='EMG-HYBRID-INT-1' else '现场CNN'
            self.model_label=algorithm
            self.model_text.set(f'{algorithm}参数已就绪 · {len(names)}类 · {manifest["payload_bytes"]} 字节\n'
                                f'{" / ".join(names[:5])}{" …" if len(names)>5 else ""}\n{self.package.parent.name}')
            self.status.set('参数已校验。点击开启现场实时识别，结果会进入原实时验证页。')
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
        self.status.set('机械手响应方案已保存。运行方式：电脑预览。')

    def edit_mapping(self):
        if self.busy:
            self.status.set('请等待当前训练或回放完成，再配置动作映射。')
            return
        if self.package is None:
            self.status.set('请先训练或载入参数包，再配置模型的动作映射。')
            return
        from gesture_config import OUTPUT_ACTIONS
        dialog = tk.Toplevel(self.app)
        dialog.title('动作与机械手响应')
        dialog.geometry('650x560')
        dialog.transient(self.app)
        dialog.configure(bg='#2A2A3E')
        dialog.grab_set()
        tk.Label(dialog, text='你的动作 → 机械手响应', bg='#2A2A3E', fg='#89B4FA',
                 font=('微软雅黑', 14, 'bold')).pack(pady=12)
        tk.Label(dialog, text='为每个动作选择一种机械手响应。\n运行方式：电脑预览',
                 bg='#2A2A3E', fg='#CDD6F4', justify='left').pack(pady=6)
        shell = tk.Frame(dialog, bg='#2A2A3E')
        shell.pack(fill='both', expand=True, padx=16)
        canvas = tk.Canvas(shell, bg='#2A2A3E', highlightthickness=0)
        scroll = ttk.Scrollbar(shell, command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        rows = tk.Frame(canvas, bg='#2A2A3E')
        window = canvas.create_window((0, 0), window=rows, anchor='nw')
        rows.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda e: canvas.itemconfigure(window, width=e.width))
        variables = []
        for i, name in enumerate(self.class_names):
            output = self.action_mapping[i] if self.action_mapping else {'id': 0, 'name': OUTPUT_ACTIONS[0]}
            var = tk.StringVar(value=output['name'])
            variables.append(var)
            tk.Label(rows, text=name, bg='#2A2A3E', fg='#CDD6F4', anchor='w').grid(
                row=i, column=0, sticky='w', padx=10, pady=8)
            names = list(OUTPUT_ACTIONS.values())
            if output['name'] not in names:
                names.append(output['name'])
            ttk.Combobox(rows, textvariable=var, values=names,
                         state='disabled' if i == 0 else 'readonly', width=35).grid(row=i, column=1, padx=10, pady=8)
        def save():
            try:
                mapping = []
                for i, var in enumerate(variables):
                    name = var.get()
                    code = next((j for j, text in OUTPUT_ACTIONS.items() if name == text), None)
                    if code is None and self.action_mapping and name == self.action_mapping[i]['name']:
                        code = self.action_mapping[i]['id']
                    if code is None:
                        raise ValueError('请为每个动作选择机械手响应。')
                    mapping.append({'id': code, 'name': name})
                self.save_mapping(mapping)
                dialog.destroy()
            except (ValueError, OSError) as exc:
                messagebox.showerror('映射无法保存', str(exc), parent=dialog)
        ttk.Button(dialog, text='保存动作映射', command=save).pack(pady=14)

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
        self.app.set_inference_classes(self.class_names)
        self.app.active_model_type = 'field_int8'
        self.app.infer_active_var.set(True)
        self.app.lbl_model_file.configure(text=f'当前模型: {self.model_label}（PC执行）\n{self.filter_description}\n{self.package.parent.name}', fg='#A6E3A1', wraplength=390)
        self.live_button.configure(text='停止现场实时识别')
        self.app.lbl_pred_gesture.configure(text='等待预热', fg='#F9E2AF')
        self.app.lbl_pred_conf.configure(text='分类间隔：--（不是概率）')
        self.app.lbl_pred_output.configure(text='机械手响应：等待识别 · 电脑预览')
        self._clear_bars()
        self.status.set('已开启现场识别。等待约半秒预热。'+('本次试用数据会自动保存。' if trial else ''))
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
        self.live_button.configure(text='开启现场实时识别')
        self.app.infer_active_var.set(False)
        self.app.active_model_type = self.legacy_model_type
        self.app.lbl_model_file.configure(text=self.legacy_model_text)
        self.app.lbl_pred_gesture.configure(text='识别已停止', fg='#A6ADC8')
        self.app.lbl_pred_conf.configure(text='选择旧模型或重新开启现场识别')
        self.app.lbl_pred_output.configure(text='')
        self.app.set_inference_classes(self.legacy_names)
        self._clear_bars()
        self.status.set('现场识别已停止。可打开试用数据，或切换模型继续对比。')

    def open_trial(self):
        if self.trial_dir and self.trial_dir.is_dir():
            os.startfile(self.trial_dir)
        else:
            self.status.set('开启“保存实时试用信号和识别结果”后，开始一次实时识别即可生成数据。')

    def open_report(self):
        directory = self.run_dir or (self.package.parent if self.package else None)
        if directory and directory.exists():
            os.startfile(directory)
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
        self.status.set('正在生成整数模型回放图。回放包含训练片段，不能代替独立测试。')
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
                    self.status.set('已保留中止记录；现场训练默认使用完整一轮，也可手动选择文件。')
            elif kind == 'log':
                if value.startswith('DATA_ERROR: '):
                    self.task_data_error = value[len('DATA_ERROR: '):].strip()
                self.log.configure(state='normal')
                self.log.insert('end', value)
                if int(self.log.index('end-1c').split('.')[0]) > 600:
                    self.log.delete('1.0', '100.0')
                self.log.see('end')
                self.log.configure(state='disabled')
            elif kind == 'trial_saved':
                self.trial_dir = Path(value)
                self.log.configure(state='normal')
                self.log.insert('end', f'试用数据已保存：{value}\n')
                self.log.configure(state='disabled')
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
                    elapsed=report.get('timing_seconds',{}).get('total')
                    duration=f'，用时{elapsed:.1f}秒' if elapsed is not None else ''
                    verdict=('留出验收通过；请再采一轮验证实时效果。' if acceptance.get('passed_offline')
                             else '留出验收未通过。'+(f'薄弱动作：{weak}。' if weak else '请检查记录质量。'))
                    self.status.set('训练完成'+duration+'。'+verdict)
                else:
                    self.status.set('整数回放已完成；回放图不作为独立测试准确率。')
                    os.startfile(self.replay_output)
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

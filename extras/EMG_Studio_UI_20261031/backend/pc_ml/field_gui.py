"""On-site capture/training console. Opens serial ports only on user clicks."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import numpy as np
from serial.tools import list_ports

from .data import GESTURES

ROOT = Path(__file__).resolve().parents[1]


class FieldApp:
    def __init__(self, root, legacy_root=None):
        self.root = root
        self.legacy_root = legacy_root or Path('E:/FPGA/FPGA2026(5)/emg_ml')
        self.core = None
        self.events = queue.Queue()
        self.worker = None
        self.process = None
        self.collecting = False
        self.capture_job = None
        self.live = None
        self.live_seen = 0
        self.live_rx_time = None
        self.recording = tk.StringVar()
        self.subject = tk.StringVar(value='现场佩戴1')
        self.port = tk.StringVar()
        self.status = tk.StringVar(value='先采集三轮数据，或选择已有记录，再训练并导出参数。')
        self.prompt = tk.StringVar(value='七种动作 · 每种三次 · 约 2 分 20 秒')
        self.run_dir = None
        root.title('肌电机械手 · 现场采集与 FPGA 模型训练')
        root.geometry('1040x760')
        root.minsize(900, 650)
        frame = ttk.Frame(root, padding=16)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='现场佩戴，现场训练', font=('Microsoft YaHei UI', 20, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='PC 负责训练；固定网络导出整数参数，供 FPGA 推理。当前可完成 PC 回放验证。').pack(anchor='w', pady=(5, 14))
        top = ttk.Frame(frame)
        top.pack(fill='x')
        ttk.Label(top, text='手环接收端口').pack(side='left')
        self.combo = ttk.Combobox(top, textvariable=self.port, width=12)
        self.combo.pack(side='left', padx=8)
        ttk.Button(top, text='刷新', command=self.refresh).pack(side='left')
        ttk.Button(top, text='连接手环', command=self.connect).pack(side='left', padx=8)
        ttk.Button(top, text='断开', command=self.disconnect).pack(side='left')
        ttk.Label(top, text='佩戴记录名').pack(side='left', padx=(20, 5))
        ttk.Entry(top, textvariable=self.subject, width=18).pack(side='left')
        self.rate = ttk.Label(frame, text='未连接；采集使用 921600 波特率。')
        self.rate.pack(anchor='w', pady=8)
        self.canvas = tk.Canvas(frame, height=145, background='#111827', highlightthickness=0)
        self.canvas.pack(fill='x')
        ttk.Label(frame, textvariable=self.prompt, font=('Microsoft YaHei UI', 17, 'bold')).pack(anchor='w', pady=10)
        collect = ttk.Frame(frame)
        collect.pack(fill='x')
        self.collect_button = ttk.Button(collect, text='1  开始三轮引导采集', command=self.collect)
        self.collect_button.pack(side='left')
        ttk.Button(collect, text='停止采集', command=self.abort).pack(side='left', padx=8)
        file_row = ttk.Frame(frame)
        file_row.pack(fill='x', pady=12)
        ttk.Label(file_row, text='原始数据').pack(side='left')
        ttk.Entry(file_row, textvariable=self.recording).pack(side='left', fill='x', expand=True, padx=8)
        ttk.Button(file_row, text='选择 .npz', command=self.choose).pack(side='left')
        actions = ttk.Frame(frame)
        actions.pack(fill='x')
        self.train_button = ttk.Button(actions, text='2  现场训练并导出', command=self.train)
        self.train_button.pack(side='left')
        ttk.Button(actions, text='3  整数模型回放', command=self.replay).pack(side='left', padx=8)
        ttk.Button(actions, text='查看参数与报告', command=self.open_results).pack(side='left')
        ttk.Button(actions, text='查看 PC 验证结果', command=self.show_demo).pack(side='left', padx=8)
        ttk.Button(actions, text='PC 实时识别 开/关', command=self.toggle_live).pack(side='left')
        ttk.Label(frame, textvariable=self.status, wraplength=970).pack(anchor='w', pady=10)
        log_frame = ttk.Frame(frame)
        log_frame.pack(fill='both', expand=True)
        self.log = tk.Text(log_frame, height=9, wrap='word', font=('Consolas', 10))
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        self.refresh()
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(100, self.tick)

    def refresh(self):
        ports = [p.device for p in list_ports.comports()]
        self.combo['values'] = ports
        if ports and not self.port.get():
            self.port.set(ports[0])

    def connect(self):
        try:
            if self.core is None:
                # Reuse the proven decoder and physical channel map. Its display
                # filter is independent; training consumes the saved raw samples.
                sys.path.insert(0, str(self.legacy_root))
                import importlib.util
                spec = importlib.util.spec_from_file_location('_emg_legacy_decoder', self.legacy_root/'emg_studio_gui_V3.0.py')
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                self.core = module.SerialCore()
            self.abort()
            self.core.connect(self.port.get(), 921600)
            self.status.set('接收已连接。固定好手环后开始采集。')
        except Exception as exc:
            messagebox.showerror('连接失败', str(exc))

    def disconnect(self):
        self.abort()
        self.live = None
        if self.core:
            self.core.disconnect()

    def collect(self):
        if not self.core or not self.core.running:
            messagebox.showinfo('先连接手环', '选择接收端串口并点击“连接手环”。')
            return
        if self.collecting or (self.worker and self.worker.is_alive()):
            return
        self.collecting = True
        self.collect_button['state'] = 'disabled'
        self.core.set_label(-1)
        self.core.start_recording()
        self.schedule = [(repeat, gesture) for repeat in range(3) for gesture in range(7)]
        self.stage_index = 0
        self.prepare_stage()

    def prepare_stage(self):
        if not self.collecting:
            return
        if self.stage_index == len(self.schedule):
            self.finish_capture()
            return
        repeat, gesture = self.schedule[self.stage_index]
        self.core.set_label(-1)
        self.prompt.set(f'第 {repeat+1}/3 轮 · 准备：{GESTURES[gesture]}')
        self.capture_job = self.root.after(1500, self.hold_stage)

    def hold_stage(self):
        if not self.collecting:
            return
        repeat, gesture = self.schedule[self.stage_index]
        self.core.set_label(gesture)
        self.deadline = time.monotonic()+5
        self.hold_tick()

    def hold_tick(self):
        if not self.collecting:
            return
        repeat, gesture = self.schedule[self.stage_index]
        remaining = max(0, self.deadline-time.monotonic())
        self.prompt.set(f'第 {repeat+1}/3 轮 · 保持 {GESTURES[gesture]} · {remaining:.1f} 秒')
        if remaining > 0:
            self.capture_job = self.root.after(100, self.hold_tick)
        else:
            self.stage_index += 1
            self.prepare_stage()

    def finish_capture(self, aborted=False):
        if self.capture_job:
            self.root.after_cancel(self.capture_job)
            self.capture_job = None
        self.collecting = False
        self.collect_button['state'] = 'normal'
        data = self.core.stop_recording()
        self.core.set_label(0)
        if not data:
            self.status.set('没有采集到有效数据。')
            return
        directory = ROOT/'artifacts/pc_ml/recordings'
        directory.mkdir(parents=True, exist_ok=True)
        safe = ''.join(c for c in self.subject.get() if c.isalnum() or c in '_-')[:50] or 'session'
        path = directory/f'{safe}_{datetime.now():%Y%m%d_%H%M%S_%f}{"_aborted" if aborted else ""}.npz'
        ts = np.array([r[0] for r in data])
        raw = np.array([r[1] for r in data], dtype=np.float32)
        labels = np.array([r[2] for r in data], dtype=np.int64)
        gestures = {i: (name, '') for i, name in enumerate(GESTURES)}
        np.savez_compressed(path, emg=raw, labels=labels, timestamps=ts,
                            sample_rate=len(ts)/max(ts[-1]-ts[0], 1), gestures=gestures,
                            nominal_sample_rate=1000, channel_map=[3, 2, 1, 0, 4, 5, 6, 7],
                            capture_complete=not aborted)
        self.recording.set(str(path))
        self.prompt.set('已保存原始数据' if not aborted else '已停止，部分数据已保存')
        self.status.set(f'数据：{path.name}。完整三轮可训练；中止数据可能缺少类别或验证重复。')

    def abort(self):
        if self.collecting:
            self.finish_capture(aborted=True)

    def choose(self):
        path = filedialog.askopenfilename(filetypes=[('肌电记录', '*.npz')])
        if path:
            self.recording.set(path)

    def launch(self, args, finished):
        if self.worker and self.worker.is_alive():
            return
        self.train_button['state'] = 'disabled'
        def work():
            try:
                self.process = subprocess.Popen([sys.executable, '-X', 'utf8', '-u', *args],
                                                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                text=True, encoding='utf-8', errors='replace',
                                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                for line in self.process.stdout:
                    self.events.put(('log', line))
                code = self.process.wait()
                self.events.put(('done', (code, finished)))
            except Exception as exc:
                self.events.put(('log', f'{exc}\n'))
                self.events.put(('done', (1, finished)))
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def train(self):
        if self.collecting:
            messagebox.showinfo('采集中', '请完成采集后再训练。')
            return
        path = Path(self.recording.get())
        if not path.is_file():
            messagebox.showinfo('选择数据', '先采集或选择一份包含完整三轮动作的 .npz。')
            return
        self.run_dir = ROOT/'artifacts/pc_ml/sessions'/datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        self.status.set('正在训练。完整动作重复分别用于训练、验证和测试。')
        self.launch(['-m', 'pc_ml.field', '--recording', str(path), '--output', str(self.run_dir)], 'train')

    def replay(self):
        run = self.run_dir or ROOT/'artifacts/pc_ml/field'
        if not (run/'export/manifest.json').exists():
            messagebox.showinfo('先训练', '训练成功后可回放整数模型。')
            return
        source = Path(self.recording.get()) if self.recording.get() else Path(json.loads((run/'field_report.json').read_text(encoding='utf-8'))['recording'])
        self.launch(['-m', 'pc_ml.replay', '--recording', str(source), '--package', str(run/'export'), '--output', str(run/'replay')], 'replay')

    def open_results(self):
        path = self.run_dir or ROOT/'artifacts/pc_ml/field'
        if path.exists():
            os.startfile(path)

    def show_demo(self):
        path = ROOT/'artifacts/pc_ml/overview.png'
        if path.exists():
            os.startfile(path)
        else:
            self.status.set('验证图尚未生成；可在报告目录查看 JSON 结果。')

    def toggle_live(self):
        if self.live is not None:
            self.live = None
            self.status.set('PC 实时识别已关闭。')
            return
        if not self.core or not self.core.running:
            messagebox.showinfo('先连接手环', '连接接收端串口后开启实时识别。')
            return
        try:
            from .replay import StreamingPredictor
            import torch
            torch.set_num_threads(4)
            run = self.run_dir or ROOT/'artifacts/pc_ml/field'
            self.live = StreamingPredictor(run/'export')
            self.live_seen = self.core.total_received
            self.live_rx_time = None
            self.status.set('PC 整数模型实时识别已开启，请先静置半秒。此结果尚未由 FPGA 计算。')
        except Exception as exc:
            self.live = None
            messagebox.showerror('模型加载失败', str(exc))

    def tick(self):
        while not self.events.empty():
            kind, value = self.events.get_nowait()
            if kind == 'log':
                self.log.insert('end', value)
                self.log.see('end')
            else:
                code, action = value
                self.train_button['state'] = 'normal'
                if code:
                    self.status.set('操作失败，请查看日志。原始数据已保留。')
                elif action == 'train':
                    report = json.loads((self.run_dir/'field_report.json').read_text(encoding='utf-8'))
                    m = report['evaluation']['test']
                    self.status.set(f'训练完成：留出重复测试 浮点 {m["float"]["accuracy"]:.2%}，整数 {m["integer"]["accuracy"]:.2%}；参数包 {report["parameters"]["payload_bytes"]} 字节。')
                else:
                    run = self.run_dir or ROOT/'artifacts/pc_ml/field'
                    os.startfile(run/'replay/replay.png')
                    self.status.set('回放图已生成。回放包含训练片段，准确率请看独立测试报告。')
        if self.core and self.core.running:
            raw, _, rate, total, corrupt = self.core.get_snapshots()
            self.rate['text'] = f'有效接收 {rate:.0f} 点/秒 · 累计 {total} · 坏帧 {corrupt}'
            self.canvas.delete('all')
            width, height = max(self.canvas.winfo_width(), 100), 145
            for ch in range(8):
                baseline = (ch+.5)*height/8
                values = np.clip((raw[:, ch]-np.median(raw[:, ch]))/250, -1, 1)*7
                points = [v for i, val in enumerate(values) for v in (i*width/max(len(values)-1, 1), baseline-val)]
                if len(points) >= 4:
                    self.canvas.create_line(*points, fill=('#38bdf8','#34d399','#fbbf24','#c084fc')[ch%4])
            if self.live is not None and total > self.live_seen:
                received = total-self.live_seen
                latest_time = self.core.last_data_time
                if received > len(raw) or (self.live_rx_time is not None and latest_time-self.live_rx_time > .25):
                    self.live.reset()
                predictions = self.live.push(raw[-min(received, len(raw)):])
                self.live_seen, self.live_rx_time = total, latest_time
                if predictions:
                    last = predictions[-1]
                    self.status.set(f'PC 整数模型：{GESTURES[last["stable"]]} · 分类间隔 {last["margin_q8"]/256:.2f} · 接收 {rate:.0f} 点/秒')
        self.root.after(100, self.tick)

    def close(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo('训练运行中', '请等待训练或回放结束后关闭窗口。')
            return
        self.disconnect()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--smoke', action='store_true')
    args = parser.parse_args()
    root = tk.Tk()
    app = FieldApp(root)
    if args.smoke:
        root.after(500, app.close)
    root.mainloop()


if __name__ == '__main__':
    main()

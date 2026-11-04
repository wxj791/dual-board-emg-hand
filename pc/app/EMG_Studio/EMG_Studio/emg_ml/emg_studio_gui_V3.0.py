#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
emg_studio_gui.py
All-in-One 8-Channel sEMG Studio & Diagnostic Workstation

Features:
1. Signal Link Health Test (Real-time packet rate Sps, corrupt frame detection).
2. 8-Channel Oscilloscope Waveform Display (Canvas-based high FPS).
3. Contact Status & Amplitude Monitor (0~3300mV bar indicators per channel).
4. Automated Gesture Collection & Paradigm Countdown (Configurable repeats, hold/rest times).
5. Dataset Export (.npz archive saved into emg_ml/data/raw/).
"""

import os
import sys
import time
import glob
import re
import json
import struct
import threading
import queue
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox
import numpy as np
import serial
from serial.tools import list_ports
import torch
import torch.nn as nn
from signal_processing import EMGSignalFilter, EMGEnvelope
from gesture_tasks_capture import recording_arrays
from gesture_config import (gesture_dict, load_profile, photo_image, preset, save_profile,
                            select_profile, validate_profile, filter_supported_profile)

GESTURE_DICT = {
    0: ("静息",     "【静息/放松】手臂完全放松平放，不要发力"),
    1: ("握拳",     "【握拳】五指紧握成拳，稳稳保持发力"),
    2: ("张开手掌", "【张开手掌】五指伸直撑开平摊并保持"),
    3: ("点赞",     "【点赞】大拇指向上竖起，其余四指握紧"),
    4: ("剪刀手",   "【剪刀手/耶】伸出食指和中指成V字，其余指握紧"),
    5: ("数字一",   "【数字一】只伸出食指，其余指握紧"),
    6: ("数字六",   "【数字六】拇指和小指伸出成六，其余指握紧")
}

# 1D-CNN 网络结构 (兼容基础 CNN)
class EMG1DCNN(nn.Module):
    def __init__(self, in_channels=8, num_classes=5):
        super(EMG1DCNN, self).__init__()
        self.conv1 = nn.Conv1d(in_channels, 32, kernel_size=7, stride=1, padding=3)
        self.bn1 = nn.BatchNorm1d(32)
        self.relu1 = nn.ReLU()
        self.pool1 = nn.MaxPool1d(2)

        self.conv2 = nn.Conv1d(32, 64, kernel_size=5, stride=1, padding=2)
        self.bn2 = nn.BatchNorm1d(64)
        self.relu2 = nn.ReLU()
        self.pool2 = nn.MaxPool1d(2)

        self.gap = nn.AdaptiveAvgPool1d(1)
        self.classifier = nn.Sequential(
            nn.Dropout(0.3),
            nn.Linear(64, num_classes)
        )

    def forward(self, x):
        x = self.pool1(self.relu1(self.bn1(self.conv1(x))))
        x = self.pool2(self.relu2(self.bn2(self.conv2(x))))
        x = self.gap(x).squeeze(-1)
        out = self.classifier(x)
        return out

# 1. 56维高阶时域生物力学特征提取 (FPGA 友好型特征算子)
def extract_single_window_features(seg_raw):
    """
    输入: seg_raw [8, 120] 原始通道数据 (无需Z-score，保留真实毫伏幅度)
    输出: 56维特征向量 [1, 56]
    包含: MAV, RMS, WL, ZC, SSC, WAMP, LOG_VAR
    """
    x = seg_raw[np.newaxis, :, :]  # [1, 8, 120]
    L = x.shape[2]
    centered = x - np.mean(x, axis=2, keepdims=True)

    mav = np.mean(np.abs(centered), axis=2)
    rms = np.sqrt(np.mean(centered**2, axis=2) + 1e-6)
    diff = np.diff(centered, axis=2)
    wl = np.sum(np.abs(diff), axis=2) / L
    zc_cond = (centered[:, :, :-1] * centered[:, :, 1:] < 0) & (np.abs(diff) >= 15.0)
    zc = np.sum(zc_cond, axis=2).astype(np.float32) / L
    diff_prod = diff[:, :, :-1] * diff[:, :, 1:]
    diff_change = np.abs(diff[:, :, :-1] - diff[:, :, 1:])
    ssc_cond = (diff_prod < 0) & (diff_change >= 15.0)
    ssc = np.sum(ssc_cond, axis=2).astype(np.float32) / (L - 1)
    wamp = np.sum(np.abs(diff) >= 20.0, axis=2).astype(np.float32) / (L - 1)
    var = np.var(centered, axis=2)
    log_var = np.log(var + 1e-5)

    feats = np.concatenate([mav, rms, wl, zc, ssc, wamp, log_var], axis=1) # [1, 56]
    return feats.astype(np.float32)

# 2. FPGA最佳拟合冠军架构: SuperFeatureMLP (7分类)
class SuperFeatureMLP(nn.Module):
    def __init__(self, in_dim=56, num_classes=7):
        super().__init__()
        self.fc1 = nn.Linear(in_dim, 96)
        self.relu1 = nn.ReLU()
        self.fc2 = nn.Linear(96, 96)
        self.relu2 = nn.ReLU()
        self.fc3 = nn.Linear(96, 48)
        self.relu3 = nn.ReLU()
        self.out = nn.Linear(48, num_classes)
        self.drop = nn.Dropout(0.15)

    def forward(self, x):
        h1 = self.relu1(self.fc1(x))
        h2 = self.relu2(self.fc2(h1)) + h1
        h3 = self.drop(self.relu3(self.fc3(h2)))
        return self.out(h3)

# 3. ResNet1D-Light (备用卷积)
class ResBlock1D(nn.Module):
    def __init__(self, channels):
        super().__init__()
        self.conv1 = nn.Conv1d(channels, channels, kernel_size=3, padding=1)
        self.relu = nn.ReLU()
        self.conv2 = nn.Conv1d(channels, channels, kernel_size=3, padding=1)
    def forward(self, x):
        return self.relu(x + self.conv2(self.relu(self.conv1(x))))

class ResNet1DLight(nn.Module):
    def __init__(self, in_channels=8, num_classes=7):
        super().__init__()
        self.prep = nn.Sequential(
            nn.Conv1d(in_channels, 32, kernel_size=5, padding=2),
            nn.ReLU(),
            nn.MaxPool1d(2)
        )
        self.res1 = ResBlock1D(32)
        self.down = nn.Sequential(
            nn.Conv1d(32, 64, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.MaxPool1d(2)
        )
        self.res2 = ResBlock1D(64)
        self.gap = nn.AdaptiveAvgPool1d(1)
        self.fc = nn.Linear(64, num_classes)

    def forward(self, x):
        x = self.prep(x)
        x = self.res1(x)
        x = self.down(x)
        x = self.res2(x)
        x = self.gap(x).squeeze(-1)
        return self.fc(x)

# 物理圆周通道重排映射:
# 1号板=CH4(索引3), 2号板=CH3(索引2), 3号板=CH2(索引1), 4号板=CH1(索引0)
# 5号板=CH5(索引4), 6号板=CH6(索引5), 7号板=CH7(索引6), 8号板=CH8(索引7)
PHYSICAL_CH_MAP = [3, 2, 1, 0, 4, 5, 6, 7]
PHYSICAL_CH_NAMES = ["1号板(CH4)", "2号板(CH3)", "3号板(CH2)", "4号板(CH1)", "5号板(CH5)", "6号板(CH6)", "7号板(CH7)", "8号板(CH8)"]

TAIL_BYTES = b'\x00\x00\x80\x7F'
BYTES_PER_SAMPLE = 36
DIAG_MAGIC = -123456.0


class SerialCore:
    """Universal high-performance serial receiver for 921600 baud JustFloat stream"""
    def __init__(self):
        self.ser = None
        from gesture_tasks_protocol import Session
        self.board_session = Session(self._board_write)
        self.board_protocol = os.environ.get('EMG_BOARD_PROTOCOL') == '1'
        self.board_stream_breaks = 0
        self.board_classification = None
        self.board_classification_time = 0
        self.board_raw_type = None
        self.board_raw_time = 0
        self.running = False
        self.thread = None
        
        # Display waveform buffer: 500 samples x 8 channels
        self.disp_buf = np.zeros((500, 8), dtype=np.float32)
        self.filtered_buf = np.zeros_like(self.disp_buf)
        self.envelope_buf = np.zeros_like(self.disp_buf)
        self.disp_count = 0
        self.disp_lock = threading.Lock()
        self.signal_filter = EMGSignalFilter()
        self.signal_envelope = EMGEnvelope()
        self.last_data_time = None
        # Independent raw stream for the optional field CNN worker. Display and
        # recording always retain their original data path.
        self.field_stream = queue.Queue(maxsize=256)
        self.field_stream_lock = threading.Lock()
        self.field_stream_enabled = False
        self.field_stream_generation = 0
        self.field_stream_reset = True
        self.field_stream_overflows = 0
        
        # Dataset recording buffer
        self.record_buf = []
        self.record_lock = threading.Lock()
        
        # Link statistics
        self.total_received = 0
        self.corrupt_frames = 0
        self.instant_sps = 0.0
        self.current_label = 0
        self.is_recording = False
        self.last_stat_time = time.time()
        self.last_stat_count = 0

        # Link diagnostic values injected once per second by receiver firmware.
        self.diag_lock = threading.Lock()
        self.link_diag = {
            "adc_frames_s": 0.0,
            "tx_generated_pkt_s": 0.0,
            "tx_espnow_ok_s": 0.0,
            "rx_packets_s": 0.0,
            "wireless_gap_s": 0.0,
            "tx_queue_drop_s": 0.0,
            "rx_queue_overflow_s": 0.0,
        }

    def connect(self, port, baud=921600):
        self.disconnect()
        self.ser = serial.Serial(port, baud, timeout=0.1, write_timeout=1)
        self.ser.reset_input_buffer()
        self.board_session.reset(self.board_protocol)
        self.board_stream_breaks = 0
        self.board_classification = None
        self.board_classification_time = 0
        self.board_raw_type = None
        self.board_raw_time = 0
        self.signal_filter.reset()
        self.signal_envelope.reset()
        self.last_data_time = None
        with self.field_stream_lock:
            self.field_stream_reset = True
        with self.disp_lock:
            self.disp_buf.fill(0)
            self.filtered_buf.fill(0)
            self.envelope_buf.fill(0)
            self.disp_count = 0
        with self.diag_lock:
            self.link_diag = dict.fromkeys(self.link_diag, 0.0)
        self.running = True
        self.total_received = 0
        self.corrupt_frames = 0
        self.instant_sps = 0.0
        self.last_stat_time = time.time()
        self.last_stat_count = 0
        self.thread = threading.Thread(target=self._recv_loop, daemon=True)
        self.thread.start()

    def disconnect(self):
        self.running = False
        self.board_session.reset(False)
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=0.5)
        if self.ser and self.ser.is_open:
            self.ser.close()

    def start_recording(self):
        if self.board_protocol and not self.compact_raw_ready():
            raise RuntimeError('等待A的0x43紧凑原始采样；仅有0x40/分类不能开始1kHz训练采集。')
        with self.record_lock:
            self.record_buf.clear()
            self.is_recording = True

    def stop_recording(self):
        with self.record_lock:
            self.is_recording = False
            result = list(self.record_buf)
            self.record_buf.clear()
            return result

    def set_label(self, label):
        with self.record_lock:self.current_label = label

    def compact_raw_ready(self):
        return self.board_raw_type==0x43 and time.monotonic()-self.board_raw_time<.5

    def enable_field_stream(self, enabled):
        with self.field_stream_lock:
            self.field_stream_enabled = enabled
            self.field_stream_generation += 1
            self.field_stream_reset = True
            while True:
                try:
                    self.field_stream.get_nowait()
                except queue.Empty:
                    break
            return self.field_stream_generation

    def publish_field_samples(self, samples, reset=False):
        """Nonblocking, ordered raw blocks; any queue loss forces CNN reset."""
        with self.field_stream_lock:
            if not self.field_stream_enabled:
                return
            reset = reset or self.field_stream_reset
            if self.field_stream.full():
                self.field_stream_overflows += 1
                while True:
                    try:
                        self.field_stream.get_nowait()
                    except queue.Empty:
                        break
                reset = True
            self.field_stream.put_nowait((self.field_stream_generation, reset, samples.copy()))
            self.field_stream_reset = False

    def _board_write(self, data):
        if not self.running or not self.ser or not self.ser.is_open:
            raise ConnectionError('A串口未连接')
        if self.ser.write(data) != len(data):
            raise IOError('串口未完整写入')

    def _recv_loop(self):
        if self.board_protocol:
            from gesture_tasks_stream import receive_loop
            receive_loop(self)
            return
        raw_stream = bytearray()
        while self.running:
            try:
                waiting = self.ser.in_waiting
                if waiting > 0:
                    raw_stream.extend(self.ser.read(waiting))
                    parsed = []

                    while len(raw_stream) >= BYTES_PER_SAMPLE:
                        tail_idx = raw_stream.find(TAIL_BYTES)
                        if tail_idx == -1:
                            if len(raw_stream) > 2048:
                                del raw_stream[:-3]
                                self.corrupt_frames += 1
                            break

                        if tail_idx >= 32:
                            payload = raw_stream[tail_idx - 32:tail_idx]
                            raw_floats = struct.unpack('<8f', payload)

                            if not np.isfinite(raw_floats).all():
                                self.corrupt_frames += 1
                                del raw_stream[:tail_idx + 4]
                                continue

                            # Special diagnostic frame: consume it, but do NOT count,
                            # display, label, or save it as an EMG sample.
                            if abs(raw_floats[0] - DIAG_MAGIC) < 0.5:
                                with self.diag_lock:
                                    self.link_diag["adc_frames_s"] = float(raw_floats[1])
                                    self.link_diag["tx_generated_pkt_s"] = float(raw_floats[2])
                                    self.link_diag["tx_espnow_ok_s"] = float(raw_floats[3])
                                    self.link_diag["rx_packets_s"] = float(raw_floats[4])
                                    self.link_diag["wireless_gap_s"] = float(raw_floats[5])
                                    self.link_diag["tx_queue_drop_s"] = float(raw_floats[6])
                                    self.link_diag["rx_queue_overflow_s"] = float(raw_floats[7])
                                del raw_stream[:tail_idx + 4]
                                continue

                            # 按照物理环形拓扑重新排序
                            remapped = tuple(raw_floats[i] for i in PHYSICAL_CH_MAP)
                            parsed.append(remapped)
                            del raw_stream[:tail_idx + 4]
                        else:
                            del raw_stream[:tail_idx + 4]
                            self.corrupt_frames += 1

                    if parsed:
                        now_ts = time.time()
                        now_mono = time.monotonic()
                        reset_after_gap = (
                            self.last_data_time is not None
                            and now_mono - self.last_data_time > 0.25
                        )
                        if reset_after_gap:
                            self.signal_filter.reset()
                            self.signal_envelope.reset()
                        self.last_data_time = now_mono
                        arr = np.array(parsed, dtype=np.float32)
                        self.publish_field_samples(arr, reset=reset_after_gap)
                        filtered = self.signal_filter.process(arr)
                        envelope = self.signal_envelope.process(filtered)
                        self.total_received += len(parsed)

                        # Filter each new sample once, independent of GUI mode.
                        with self.disp_lock:
                            if reset_after_gap:
                                self.disp_count = 0
                            n_new = len(arr)
                            if n_new >= 500:
                                self.disp_buf[:] = arr[-500:]
                                self.filtered_buf[:] = filtered[-500:]
                                self.envelope_buf[:] = envelope[-500:]
                            else:
                                self.disp_buf = np.roll(self.disp_buf, -n_new, axis=0)
                                self.disp_buf[-n_new:] = arr
                                self.filtered_buf = np.roll(self.filtered_buf, -n_new, axis=0)
                                self.filtered_buf[-n_new:] = filtered
                                self.envelope_buf = np.roll(self.envelope_buf, -n_new, axis=0)
                                self.envelope_buf[-n_new:] = envelope
                            self.disp_count = min(500, self.disp_count + n_new)

                        # Append to training dataset if active.
                        # label == -1 means transition/stabilization period: display it,
                        # but deliberately DO NOT save it into the training dataset.
                        if self.is_recording and self.current_label >= 0:
                            lbl = self.current_label
                            with self.record_lock:
                                for pt in parsed:
                                    self.record_buf.append((now_ts, pt, lbl))
                else:
                    time.sleep(0.002)

                # Update instant sample rate (Sps) every 0.3s
                now = time.time()
                if now - self.last_stat_time >= 0.3:
                    dt = now - self.last_stat_time
                    dc = self.total_received - self.last_stat_count
                    self.instant_sps = dc / dt if dt > 0 else 0.0
                    self.last_stat_time = now
                    self.last_stat_count = self.total_received

            except Exception as e:
                print(f"Serial worker error: {e}")
                self.running = False
                break

    def get_snapshot(self):
        raw, _, sps, total, corrupt = self.get_snapshots()
        return raw, sps, total, corrupt

    def get_snapshots(self):
        """Return aligned raw/filtered samples; old models always use raw."""
        raw, filtered, _, sps, total, corrupt = self.get_display_snapshots()
        return raw, filtered, sps, total, corrupt

    def get_display_snapshots(self):
        """Copy the three aligned waveform views under one receiver lock."""
        with self.disp_lock:
            start = len(self.disp_buf) - self.disp_count
            raw = self.disp_buf[start:].copy()
            filtered = self.filtered_buf[start:].copy()
            envelope = self.envelope_buf[start:].copy()
        return raw, filtered, envelope, self.instant_sps, self.total_received, self.corrupt_frames

    def get_link_diag(self):
        with self.diag_lock:
            return dict(self.link_diag)


class EMGStudioGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("肌电工作站 - 8通道表面肌电一体化调试与采集系统")
        width = min(1600, self.winfo_screenwidth()-50)
        height = min(920, self.winfo_screenheight()-85)
        self.geometry(f"{width}x{height}")
        self.minsize(1280, 720)
        self.configure(bg="#1E1E2E")
        
        self.core = SerialCore()
        self.paradigm_thread = None
        self.stop_requested = False
        self.space_event = threading.Event()
        self.collection_paused = threading.Event()
        try:
            self.gesture_profile = load_profile()
        except (ValueError, OSError) as exc:
            self.gesture_profile = preset()
            self.after(500, lambda error=str(exc): messagebox.showwarning('动作方案未恢复', error, parent=self))
        self.collection_profile = None
        self.inference_names = [v[0] for v in GESTURE_DICT.values()]

        self._build_ui()
        self._refresh_ports()
        self.protocol("WM_DELETE_WINDOW", self._close_app)
        
        # 绑定空格键全局触发下一轮采集
        self.bind("<space>", self._on_space_pressed)

        # Periodic GUI update timer (~30 FPS)
        self.after(33, self._gui_tick)

    def _build_ui(self):
        from workstation_ui import build_workstation
        build_workstation(self, PHYSICAL_CH_NAMES)

    def _toggle_waveform_replay(self):
        from waveform_replay import WaveformReplay
        if not hasattr(self,'wave_replay'):
            self.wave_replay=WaveformReplay(self)
        self.wave_replay.toggle()

    def _toggle_recognition(self):
        if self.core.board_protocol and hasattr(self, 'gesture_tasks'):
            return self.gesture_tasks.toggle_recognition()
        panel = self.field_training
        if panel.prefer_filtered or getattr(self, 'active_model_type', '').startswith('field_'):
            panel.toggle_live()
        else:
            enabled = not self.infer_active_var.get()
            self.infer_active_var.set(enabled)
            self.infer_toggle.configure(text='暂停识别' if enabled else '开始识别')

    def _rebuild_gesture_choices(self):
        from ui_style import label, SURFACE
        for widget in self.gesture_choices.winfo_children():
            widget.destroy()
        classes=self.gesture_profile['classes']
        self.gesture_vars={row['id']:tk.BooleanVar(value=True) for row in classes[1:]}
        self.profile_summary.set(f'训练动作 · {len(classes)}类')
        columns=2 if len(classes)<=10 else 3
        for column in range(3):
            self.gesture_choices.columnconfigure(column,weight=1 if column<columns else 0,
                uniform='gesture_names' if column<columns else '')
        self.gesture_name_labels=[]
        for index,row in enumerate(classes):
            cell=tk.Frame(self.gesture_choices,bg=SURFACE,padx=10,pady=5)
            cell.grid(row=index//columns,column=index%columns,sticky='ew',
                      padx=(0,10) if index%columns<columns-1 else 0,pady=(0,10))
            name=label(cell,row['name'],anchor='w',justify='left',wraplength=200)
            name.pack(fill='x')
            cell.bind('<Configure>',lambda e,w=name:w.configure(wraplength=max(90,e.width-20)))
            self.gesture_name_labels.append(name)

    def open_gesture_profile(self):
        if self.core.is_recording or (self.paradigm_thread and self.paradigm_thread.is_alive()):
            messagebox.showinfo('正在采集', '请先停止本轮采集，再修改动作方案。', parent=self)
            return
        if hasattr(self, 'field_training') and self.field_training.busy:
            messagebox.showinfo('任务运行中', '请等待本轮训练或回放结束，再修改动作方案。', parent=self)
            return
        from gesture_dialog import GestureProfileDialog
        GestureProfileDialog(self)

    def apply_gesture_profile(self, profile, persist=False):
        if self.core.is_recording or (self.paradigm_thread and self.paradigm_thread.is_alive()):
            raise ValueError('采集期间不能修改动作方案，请先停止采集。')
        if hasattr(self, 'gesture_tasks') and self.gesture_tasks.busy:
            raise ValueError('请等待上传或机械手操作结束后修改动作。')
        profile = validate_profile(filter_supported_profile(profile))
        if persist:
            save_profile(profile)
        self.gesture_profile = profile
        if hasattr(self, 'gesture_tasks'):
            self.gesture_tasks.profile_changed()
        self._rebuild_gesture_choices()
        self.show_collection_gesture(profile['classes'][0])
        if hasattr(self, 'field_training'):
            self.field_training.status.set('动作已更新')

    def show_collection_gesture(self, row):
        self._collection_photo = photo_image(row, 96, self)
        self.lbl_action_image.configure(image=self._collection_photo or '')
        self.lbl_action_instruction.configure(text=row['instruction'])

    def set_inference_classes(self, names, show_scores=True):
        from ui_style import PANEL, label, short
        self.inference_names = list(names)
        for widget in self.prob_box.winfo_children():
            widget.destroy()
        self.prob_bars, self.prob_labels = [], []
        label(self.prob_box, '类别分布' if show_scores else '模型类别',
              size=16, bold=True).grid(row=0, column=0, columnspan=4, sticky='w', pady=(0, 8))
        columns = 4 if len(names)>12 else 3 if len(names)>8 else 2
        for column in range(4):
            self.prob_box.columnconfigure(column, weight=1 if column<columns else 0, uniform='classes' if column<columns else '')
        for i, name in enumerate(names):
            row = tk.Frame(self.prob_box, bg=PANEL, padx=5, pady=2)
            row.grid(row=i//columns+1, column=i%columns, sticky='ew', padx=(0, 8), pady=2)
            head = tk.Frame(row, bg=PANEL); head.pack(fill='x')
            label(head, short(name, 7 if columns>2 else 10), anchor='w').pack(side='left')
            score = label(head, '--', muted=True)
            bar = ttk.Progressbar(row, maximum=100, value=0)
            if show_scores:
                score.pack(side='right')
                bar.pack(fill='x', pady=(3, 1))
            self.prob_bars.append(bar)
            self.prob_labels.append(score)

    def _close_app(self):
        if hasattr(self,'wave_replay'):
            self.wave_replay.stop()
        if self.field_training.busy:
            messagebox.showinfo("训练运行中", "训练或回放正在运行，请完成后关闭；结果会保存在本轮目录。")
            return
        self.stop_requested = True
        self.space_event.set()
        if self.core.is_recording:
            self._abort_collection()
        self.core.disconnect()
        self.field_training.close()
        self._finish_close()

    def _finish_close(self):
        live_worker = getattr(getattr(self, 'field_training', None), 'live_worker', None)
        if (self.paradigm_thread and self.paradigm_thread.is_alive()) or (live_worker and live_worker.is_alive()):
            self.after(100, self._finish_close)
        else:
            self.destroy()

    def _refresh_ports(self):
        ports = list(list_ports.comports())
        self.port_map.clear()
        display_names = []
        for p in ports:
            # 例如: "COM3 (USB-SERIAL CH340)" 或 "COM4 (Silicon Labs CP210x)"
            desc = f"{p.device} ({p.description})" if p.description and p.description != p.device else p.device
            self.port_map[desc] = p.device
            display_names.append(desc)

        self.cb_ports["values"] = display_names
        if display_names:
            self.cb_ports.current(0)
        else:
            self.cb_ports.set("无可用串口")

    def _toggle_connect(self):
        if hasattr(self,'wave_replay'):
            self.wave_replay.stop()
        if not self.core.running:
            selected_str = self.cb_ports.get()
            port = self.port_map.get(selected_str, selected_str)
            if not port or "无" in port:
                messagebox.showwarning("提示", "请先选择一个有效的串口！")
                return
            baud = int(self.entry_baud.get().strip())
            try:
                self.core.connect(port, baud)
                self.btn_connect.configure(text="断开串口", bg="#D20F39")
                self.lbl_link_status.configure(text=f"链路状态: 🟢 已连接 ({port})", fg="#A6E3A1")
                self.field_training.resume_default()
            except Exception as e:
                from gesture_tasks_customer import diagnostic
                diagnostic('connect',e)
                messagebox.showerror("连接失败", "无法连接A板，请检查串口和连接线；详情见诊断日志。")
        else:
            if self.core.is_recording:
                self._abort_collection()
            self.core.disconnect()
            self.field_training.stop_live()
            self.btn_connect.configure(text="打开串口", bg="#28A745")
            self.lbl_link_status.configure(text="链路状态: 🔴 未连接", fg="#FFAA00")
            self.lbl_sps.configure(text="采样率: 0 Sps")

    def _start_collection(self):
        if self.core.board_protocol and not self.core.board_session.connected:
            messagebox.showinfo('未连接A板', '请先连接A板，再开始采集。')
            return
        if self.core.board_protocol and not self.core.compact_raw_ready():
            from gesture_tasks_customer import diagnostic
            diagnostic('collection_not_ready',raw_type=self.core.board_raw_type)
            messagebox.showinfo('等待采样', '尚未收到可用于训练的完整采样，请稍候；详情见诊断日志。')
            return
        if self.field_training.busy:
            messagebox.showinfo("训练运行中", "请等待本轮训练或回放结束，再开始新采集。")
            return
        if not self.core.running:
            messagebox.showwarning("提示", "请先连接串口再开始采集！")
            return
        
        gestures = [gid for gid, var in self.gesture_vars.items() if var.get()]
        if not gestures:
            messagebox.showwarning("提示", "请至少勾选一个手势动作！")
            return
        # Unchecked actions are removed and IDs compacted in this recording's
        # snapshot. The worker never reads a mutable editor/global dictionary.
        self.collection_profile = select_profile(self.gesture_profile, gestures)
        gestures = list(range(1, len(self.collection_profile['classes'])))

        try:
            repeats = int(self.entry_repeats.get().strip())
            hold_sec = float(self.entry_hold.get().strip())
            rest_sec = float(self.entry_rest.get().strip())
            transition_sec = float(self.entry_transition.get().strip())
            subject = self.entry_subject.get().strip() or "user01"
            p_text = self.entry_p_idx.get().strip()
            start_p = int(p_text) if p_text.isdigit() else 1
        except Exception:
            messagebox.showerror("参数错误", "请输入合法的数值格式！")
            return

        if repeats < 1 or hold_sec <= 0 or rest_sec <= 0 or transition_sec < 0:
            messagebox.showerror("参数错误", "重复次数需>=1，动作/静息时间需>0，切换间隙需>=0。")
            return

        self.btn_start.configure(state="disabled")
        self.btn_abort.configure(state="normal")
        self.stop_requested = False
        self.collection_paused.clear()

        self.paradigm_thread = threading.Thread(
            target=self._run_collection_worker,
            args=(subject, gestures, repeats, hold_sec, rest_sec, transition_sec, start_p,
                  self.collection_profile, self.continuous_mode_var.get()),
            daemon=True
        )
        self.paradigm_thread.start()

    def _on_space_pressed(self, event=None):
        """用户按下空格键触发下一轮采集"""
        if self.field_training.busy:
            return
        self.space_event.set()

    def _abort_collection(self):
        self.stop_requested = True
        self.space_event.set()  # 解除可能正在等待的空格阻塞
        partial_data = self.core.stop_recording()
        self.btn_start.configure(state="normal")
        self.btn_abort.configure(state="disabled")
        self.lbl_stage.configure(text="已停止并保存", fg="#F38BA8")
        self.lbl_action.configure(text="--", fg="#A6ADC8")
        self.lbl_countdown.configure(text="")

        # 将已录制好的半截数据立即落盘，避免白录
        if partial_data:
            ts = np.array([x[0] for x in partial_data], dtype=np.float64)
            emg = np.array([x[1] for x in partial_data], dtype=np.float32)
            lbl = np.array([x[2] for x in partial_data], dtype=np.int32)
            dur = ts[-1] - ts[0] if len(ts) > 1 else 1.0
            fs = len(ts) / dur
            out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "raw")
            os.makedirs(out_dir, exist_ok=True)
            subj = self.entry_subject.get().strip() or "user01"
            fname = f"emg_{subj}_aborted_{datetime.now().strftime('%Y%m%d_%H%M%S')}.npz"
            np.savez_compressed(
                os.path.join(out_dir, fname),
                **recording_arrays(partial_data,complete=False),
                gestures=gesture_dict(self.collection_profile or self.gesture_profile),
                gesture_profile=json.dumps(self.collection_profile or self.gesture_profile, ensure_ascii=False)
            )
            self.field_training.recording_saved(os.path.join(out_dir, fname), complete=False)

    def _get_next_session_filename(self, subject, target_p=None):
        """
        根据受试者目录获取指定序号或下一个自增序号文件名:
        例如 DGT 人名 -> 保存至 data/raw/DGT/
        自动生成: DGT_P{target_p}_2610241636.npz
        """
        raw_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "raw")
        subj_dir = os.path.join(raw_root, subject)
        os.makedirs(subj_dir, exist_ok=True)

        if target_p is None:
            existing = glob.glob(os.path.join(subj_dir, f"{subject}_P*.npz"))
            max_p = 0
            import re
            pattern = re.compile(rf"^{re.escape(subject)}_P(\d+)_")
            for f in existing:
                base = os.path.basename(f)
                m = pattern.match(base)
                if m:
                    try:
                        num = int(m.group(1))
                        if num > max_p:
                            max_p = num
                    except ValueError:
                        pass
            next_p = max_p + 1
        else:
            next_p = int(target_p)

        time_str = datetime.now().strftime("%y%m%d%H%M") # 年月日时分，如 2610241636
        fname = f"{subject}_P{next_p}_{time_str}.npz"
        return subj_dir, fname, next_p

    def _run_collection_worker(self, subject, gestures, repeats, hold_sec, rest_sec, transition_sec=0.8, start_p=1,
                               profile=None, continuous_mode=None):
        profile = profile or self.collection_profile or self.gesture_profile
        gesture_info = gesture_dict(profile)
        if continuous_mode is None:
            continuous_mode = self.continuous_mode_var.get()
        cycle_count = 0
        current_p = int(start_p)

        while not self.stop_requested:
            cycle_count += 1

            # 1. 准备倒计时（此阶段不录制，避免准备动作污染静息标签） (首次3秒倒计时，后续循环为1.5秒平滑过渡)
            first_gname = gesture_info[gestures[0]][0] if gestures else "--"
            self.lbl_stage.configure(text=f"P{current_p} · 准备", fg="#F9E2AF")
            self.lbl_action.configure(text="准备就绪", fg="#FAB387")
            self.lbl_next_action.configure(text="下一步：静息", fg="#A6E3A1")
            prep_secs = 3 if cycle_count == 1 else 2
            for c in range(prep_secs, 0, -1):
                if self.stop_requested: break
                self.lbl_countdown.configure(text=f"{c}")
                time.sleep(1)
            if self.stop_requested: break

            # 准备结束后才真正开始记录。切换阶段使用 label=-1 丢弃。
            try:self.core.start_recording()
            except RuntimeError as exc:
                self.after(0,self.field_training.status.set,str(exc))
                break

            # 2. 纯净交替循环：切换(丢弃) -> 静息 -> 切换(丢弃) -> 发力 ...
            # 全程统一标签 0: 静息/放松
            schedule = []
            # 每轮走完所有动作，再进入下一次重复；轮换起点，避免
            # 连续采同一动作时把缓慢漂移当成动作特征。
            for r in range(1, repeats + 1):
                offset = (r - 1) % len(gestures) if gestures else 0
                order = gestures[offset:] + gestures[:offset]
                for gid in order:
                    schedule.append((gid, gesture_info[gid][0], r))

            total_actions = len(schedule)

            for i, (gid, gname, r) in enumerate(schedule):
                if self.stop_requested: break
                done = i + 1

                # ----------------------------------------------------------
                # A0. 从上一个状态切换到“放松”：不保存
                #     这段时间专门吸收松手、调整姿势、电极微动等瞬态。
                # ----------------------------------------------------------
                self.core.set_label(-1)
                self.lbl_stage.configure(text=f"P{current_p} · {done}/{total_actions} · 切换", fg="#F9E2AF")
                self.lbl_action.configure(text="放松手掌", fg="#F9E2AF")
                self.lbl_next_action.configure(text=f"下一步：{gname}", fg="#FAB387")
                t_gap = time.time()
                while time.time() - t_gap < transition_sec:
                    if self.stop_requested: break
                    rem = transition_sec - (time.time() - t_gap)
                    self.lbl_countdown.configure(text=f"{rem:.1f} s")
                    time.sleep(0.05)
                if self.stop_requested: break

                # ----------------------------------------------------------
                # A1. 稳定静息期 (标签 0)
                # ----------------------------------------------------------
                self.core.set_label(0)
                self.after(0, self.show_collection_gesture, profile['classes'][0])
                self.lbl_stage.configure(text=f"P{current_p} · {done}/{total_actions} · 静息", fg="#A6E3A1")
                self.lbl_action.configure(text="静息放松", fg="#A6E3A1")
                self.lbl_next_action.configure(text=f"下一步：{gname} · {r}/{repeats}", fg="#FAB387")

                t_rest = time.time()
                while time.time() - t_rest < rest_sec:
                    if self.stop_requested: break
                    rem = rest_sec - (time.time() - t_rest)
                    self.lbl_countdown.configure(text=f"{rem:.1f} s")
                    time.sleep(0.05)
                if self.stop_requested: break

                # ----------------------------------------------------------
                # B0. 从静息切换到动作：不保存
                #     提示一出现就开始做动作，给手指/肌肉 transition_sec 秒完成动作。
                # ----------------------------------------------------------
                self.core.set_label(-1)
                self.lbl_stage.configure(text=f"P{current_p} · {done}/{total_actions} · 切换", fg="#F9E2AF")
                self.after(0, self.show_collection_gesture, profile['classes'][gid])
                self.lbl_action.configure(text=f"{gname}", fg="#FAB387")
                self.lbl_next_action.configure(text="保持稳定，等待录制", fg="#A6ADC8")
                t_gap = time.time()
                while time.time() - t_gap < transition_sec:
                    if self.stop_requested: break
                    rem = transition_sec - (time.time() - t_gap)
                    self.lbl_countdown.configure(text=f"{rem:.1f} s")
                    time.sleep(0.05)
                if self.stop_requested: break

                # ----------------------------------------------------------
                # B1. 稳定动作期 (标签 gid)
                # ----------------------------------------------------------
                self.core.set_label(gid)
                if i + 1 < len(schedule):
                    next_desc = f"静息 → {schedule[i+1][1]}"
                else:
                    next_desc = "静息 → 保存"

                self.lbl_stage.configure(text=f"P{current_p} · {done}/{total_actions} · 录制", fg="#F38BA8")
                self.lbl_action.configure(text=f"{gname}", fg="#F38BA8")
                self.lbl_next_action.configure(text=f"下一步：{next_desc}", fg="#A6ADC8")

                t_hold = time.time()
                while time.time() - t_hold < hold_sec:
                    if self.stop_requested: break
                    rem = hold_sec - (time.time() - t_hold)
                    self.lbl_countdown.configure(text=f"{rem:.1f} s")
                    time.sleep(0.05)
                if self.stop_requested: break

            # --------------------------------------------------------------
            # C. 最后一个发力动作完成后的尾部收尾静息 (保证最后一个动作后也有静息)
            # --------------------------------------------------------------
            if not self.stop_requested:
                # 最后一个动作松开的过程同样丢弃，避免污染静息类。
                self.core.set_label(-1)
                self.lbl_stage.configure(text=f"P{current_p} · 收尾切换", fg="#F9E2AF")
                self.lbl_action.configure(text="静息放松", fg="#F9E2AF")
                self.lbl_next_action.configure(text="下一步：收尾静息", fg="#A6E3A1")
                t_gap = time.time()
                while time.time() - t_gap < transition_sec:
                    if self.stop_requested: break
                    rem = transition_sec - (time.time() - t_gap)
                    self.lbl_countdown.configure(text=f"{rem:.1f} s")
                    time.sleep(0.05)

                if not self.stop_requested:
                    self.core.set_label(0)
                    self.lbl_stage.configure(text=f"P{current_p} · 收尾静息", fg="#A6E3A1")
                    self.lbl_action.configure(text="静息放松", fg="#A6E3A1")
                    self.lbl_next_action.configure(text="本轮结束后保存", fg="#A6E3A1")
                    t_tail = time.time()
                    while time.time() - t_tail < rest_sec:
                        if self.stop_requested: break
                        rem = rest_sec - (time.time() - t_tail)
                        self.lbl_countdown.configure(text=f"{rem:.1f} s")
                        time.sleep(0.05)

            # 停止并保存本轮采集数据 (精确按指定的 current_p 命名)
            raw_list = self.core.stop_recording()
            saved_p_idx = current_p
            if raw_list and len(raw_list) > 100:
                ts = np.array([x[0] for x in raw_list], dtype=np.float64)
                emg = np.array([x[1] for x in raw_list], dtype=np.float32)
                lbl = np.array([x[2] for x in raw_list], dtype=np.int32)
                dur = ts[-1] - ts[0] if len(ts) > 1 else 1.0
                fs = len(ts) / dur

                subj_dir, fname, p_idx = self._get_next_session_filename(subject, target_p=current_p)
                out_path = os.path.join(subj_dir, fname)

                np.savez_compressed(
                    out_path,
                    **recording_arrays(raw_list,complete=not self.stop_requested),
                    gestures=gesture_info,
                    gesture_profile=json.dumps(profile, ensure_ascii=False)
                )
                print(f"[采集落盘] 成功保存第 {p_idx} 组数据 -> {out_path} (共 {len(lbl)} 点)")
                self.field_training.recording_saved(out_path, complete=not self.stop_requested)

            # 如果非连续采集模式，或者用户点了中止，则跳出循环
            if not continuous_mode or self.stop_requested:
                break

            # ----------------------------------------------------------
            # 连续采集：当前 P 已存盘，界面自增到下一个 P，并等待空格键
            # ----------------------------------------------------------
            next_p_idx = current_p + 1
            # 自动同步界面输入框中的 P 序号为下一个值
            self.entry_p_idx.delete(0, tk.END)
            self.entry_p_idx.insert(0, str(next_p_idx))

            self.lbl_stage.configure(
                text=f"P{saved_p_idx} · 已保存", 
                fg="#A6E3A1"
            )
            self.lbl_action.configure(text="空格键继续", fg="#F9E2AF")
            self.lbl_countdown.configure(text=f"等待 P{next_p_idx}")
            self.lbl_next_action.configure(
                text=f"下一轮：P{next_p_idx}", 
                fg="#89B4FA"
            )

            self.space_event.clear()
            self.collection_paused.set()
            # 循环等待空格事件或中止信号
            while not self.stop_requested and not self.space_event.is_set():
                time.sleep(0.05)

            self.collection_paused.clear()

            if self.stop_requested:
                break

            # 空格按下后，流转到下一个 P 序号
            current_p = next_p_idx

        self.btn_start.configure(state="normal")
        self.btn_abort.configure(state="disabled")
        self.lbl_countdown.configure(text="")
        if not self.stop_requested:
            self.lbl_stage.configure(text="采集完成", fg="#A6E3A1")
            self.lbl_action.configure(text="全部就绪", fg="#A6E3A1")
            messagebox.showinfo("完成", f"恭喜！受试者 【{subject}】 的手势采集已全部完成并归档存盘！")
        else:
            self.lbl_stage.configure(text="采集已停止并保存", fg="#F38BA8")
            self.lbl_action.configure(text="已停止", fg="#A6ADC8")

    def _refresh_model_list(self):
        """扫描 models 目录下所有 .pth 模型并按修改时间降序排列，默认选中最新"""
        models_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
        if not os.path.exists(models_dir):
            self.combo_model["values"] = []
            self.lbl_model_file.configure(text="当前模型: 🔴 未找到 models 文件夹", fg="#FFAA00")
            return

        pth_files = [f for f in os.listdir(models_dir) if f.endswith(".pth")]
        # 按修改时间从最新到最旧排序
        pth_files.sort(key=lambda f: os.path.getmtime(os.path.join(models_dir, f)), reverse=True)

        self.combo_model["values"] = pth_files
        if pth_files:
            current_choice = self.combo_model_var.get()
            if not current_choice or current_choice not in pth_files:
                self.combo_model.current(0)
                target = pth_files[0]
            else:
                target = current_choice
            self._load_trained_model(selected_file=target)
        else:
            self.combo_model_var.set("")
            self.lbl_model_file.configure(text="当前模型: 🔴 未找到 .pth 权重文件", fg="#FFAA00")

    def _load_trained_model(self, selected_file=None):
        if hasattr(self, 'field_training'):
            self.field_training.stop_live()
            self.field_training.clear_default()
        if selected_file:
            self.model_select_row.pack(fill="x", before=self.infer_toggle)
            self.infer_toggle.configure(text="暂停识别" if self.infer_active_var.get() else "开始识别")
        models_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
        if not selected_file:
            self._refresh_model_list()
            return

        load_target = os.path.join(models_dir, selected_file)
        if not os.path.exists(load_target):
            self.lbl_model_file.configure(text=f"文件不存在: {selected_file}", fg="#F38BA8")
            return
        self.set_inference_classes([v[0] for v in GESTURE_DICT.values()])
        self.lbl_pred_output.configure(text='')

        try:
            ckpt = torch.load(load_target, map_location=self.device, weights_only=False)
            if isinstance(ckpt, dict) and 'scaler_mean' in ckpt:
                # MLP 格式 (56维特征)
                self.cnn_model = SuperFeatureMLP(in_dim=56, num_classes=7).to(self.device)
                self.cnn_model.load_state_dict(ckpt['state_dict'])
                self.cnn_model.eval()
                self.scaler_mean = np.array(ckpt['scaler_mean'], dtype=np.float32)
                self.scaler_scale = np.array(ckpt['scaler_scale'], dtype=np.float32)
                self.active_model_type = "mlp_56"
                acc_info = f" (Acc: {ckpt.get('acc', 0.0)*100:.1f}%)" if 'acc' in ckpt else ""
                self.lbl_model_file.configure(
                    text=f"{selected_file} · 7类 · 原始信号", 
                    fg="#A6E3A1"
                )
                print(f"-> 成功载入模型: {selected_file} (SuperFeatureMLP)")
                return
            elif isinstance(ckpt, dict) and any('conv' in k or 'layer' in k for k in ckpt.keys()):
                # ResNet 或 CNN
                # 检查是否包含 ResNet 特征
                if any('conv1.weight' in k and ckpt[k].shape[0] == 32 for k in ckpt.keys()) and 'layer1.0.conv1.weight' in ckpt:
                    self.cnn_model = ResNet1DLight(in_channels=8, num_classes=7).to(self.device)
                    self.cnn_model.load_state_dict(ckpt)
                    self.cnn_model.eval()
                    self.active_model_type = "resnet"
                    self.lbl_model_file.configure(
                        text=f"{selected_file} · 7类 · 原始信号", 
                        fg="#A6E3A1"
                    )
                    print(f"-> 成功载入模型: {selected_file} (ResNet1D-Light)")
                    return
                else:
                    # 旧版 5分类 CNN
                    self.cnn_model = EMG1DCNN(in_channels=8, num_classes=5).to(self.device)
                    self.cnn_model.load_state_dict(ckpt)
                    self.cnn_model.eval()
                    self.active_model_type = "cnn"
                    self.lbl_model_file.configure(
                        text=f"{selected_file} · 5类 · 原始信号", 
                        fg="#A6E3A1"
                    )
                    print(f"-> 成功载入模型: {selected_file} (EMG1DCNN 5分类)")
                    return
            else:
                self.lbl_model_file.configure(text=f"无法识别模型结构: {selected_file}", fg="#F38BA8")
        except Exception as e:
            self.lbl_model_file.configure(text=f"载入失败: {e}", fg="#F38BA8")
            print(f"[Error] 载入 {selected_file} 失败: {e}")

    def _on_waveform_mode_changed(self):
        mode = self.waveform_mode.get()
        self.lbl_waveform_range.configure(text='量程 0～' if mode=='envelope' else '量程 ±')
        self.waveform_range.set('100' if mode=='envelope' else '200' if mode=='filtered' else '1200')

    def _gui_tick(self):
        """Update waveform canvas and diagnostic meters at ~30 FPS"""
        data, filtered, envelope, sps, total, loss = self.core.get_display_snapshots()
        mode = self.waveform_mode.get()
        show_envelope = mode == "envelope"
        show_filtered = mode == "filtered"
        waveform_data = envelope if show_envelope else filtered if show_filtered else data

        if self.core.running:
            self.lbl_sps.configure(text=f"{sps:4.0f} 点/秒")
            self.lbl_diag_sps.configure(text=f"有效速率  {sps:4.0f} 点/秒")
            self.lbl_diag_total.configure(text=f"累计接收  {total:,}")
            self.lbl_diag_loss.configure(text=f"错帧  {loss}")

            diag = self.core.get_link_diag()
            self.lbl_diag_adc.configure(
                text=f"ADC采样 {diag['adc_frames_s']:6.1f} /s"
            )
            self.lbl_diag_tx.configure(
                text=f"发送 {diag['tx_generated_pkt_s']:5.1f} / 成功 {diag['tx_espnow_ok_s']:5.1f}"
            )
            self.lbl_diag_rx.configure(
                text=f"接收 {diag['rx_packets_s']:5.1f} pkt/s"
            )
            self.lbl_diag_drop.configure(
                text=(
                    f"丢包  无线 {diag['wireless_gap_s']:4.1f} | "
                    f"发送 {diag['tx_queue_drop_s']:4.1f} | "
                    f"接收 {diag['rx_queue_overflow_s']:4.1f}"
                )
            )

            # Update per-channel amplitude meters (last point)
            if len(data) > 0:
                last_pt = data[-1]
                for ch in range(8):
                    val = float(last_pt[ch])
                    self.ch_bars[ch]["value"] = val
                    self.ch_val_labels[ch].configure(text=f"{val:4.0f} mV")

            # --- 实时 7分类/5分类 识别推理 (120点滑动窗口，约200ms) ---
            if (not self.core.board_protocol and self.infer_active_var.get() and self.cnn_model is not None and len(data) >= 120
                    and getattr(self, "active_model_type", "") not in ("field_int8", "field_waiting")):
                try:
                    seg_raw = data[-120:].T.astype(np.float32)  # [8, 120] 原始毫伏数据

                    if getattr(self, "active_model_type", "") == "mlp_56":
                        # 1. 提取 56 维高阶生物力学时域特征 (FPGA 纯硬件友好算子)
                        feats = extract_single_window_features(seg_raw) # [1, 56]
                        if hasattr(self, "scaler_mean") and hasattr(self, "scaler_scale"):
                            feats_norm = (feats - self.scaler_mean) / (self.scaler_scale + 1e-6)
                        else:
                            feats_norm = feats
                        tensor_in = torch.tensor(feats_norm, dtype=torch.float32).to(self.device)
                        with torch.no_grad():
                            logits = self.cnn_model(tensor_in)
                            probs = torch.softmax(logits, dim=1).cpu().numpy()[0]
                    else:
                        # 2. 卷积模型 (Z-score 归一化)
                        mean = np.mean(seg_raw, axis=1, keepdims=True)
                        std = np.std(seg_raw, axis=1, keepdims=True) + 1e-6
                        seg_norm = (seg_raw - mean) / std
                        tensor_in = torch.tensor(seg_norm, dtype=torch.float32).unsqueeze(0).to(self.device)
                        with torch.no_grad():
                            logits = self.cnn_model(tensor_in)
                            probs = torch.softmax(logits, dim=1).cpu().numpy()[0]

                    pred_id = int(np.argmax(probs))
                    pred_conf = float(probs[pred_id]) * 100.0
                    pred_name = GESTURE_DICT.get(pred_id, (f"动作{pred_id}", ""))[0]

                    # 更新大字手势与颜色
                    color_map = {
                        0: "#A6ADC8", # 静息 (淡灰)
                        1: "#A6E3A1", # 握拳 (翠绿)
                        2: "#89B4FA", # 张开手掌 (天蓝)
                        3: "#F9E2AF", # 点赞 (亮黄)
                        4: "#FAB387", # 剪刀手 (橙粉)
                        5: "#CBA6F7", # 数字1 (紫罗兰)
                        6: "#94E2D5"  # 数字6 (薄荷绿)
                    }
                    self.lbl_pred_gesture.configure(text=f"【{pred_name}】", fg=color_map.get(pred_id, "#FFFFFF"))
                    self.lbl_pred_conf.configure(text=f"置信度: {pred_conf:.1f}%")

                    # 更新各类别概率条
                    for gid in range(len(GESTURE_DICT)):
                        if gid < len(probs) and gid < len(self.prob_bars):
                            p_val = probs[gid] * 100.0
                            self.prob_bars[gid]["value"] = p_val
                            self.prob_labels[gid].configure(text=f"{p_val:3.0f}%")
                        elif gid < len(self.prob_bars):
                            self.prob_bars[gid]["value"] = 0
                            self.prob_labels[gid].configure(text="--")
                except Exception as e:
                    pass

        self._update_collection_focus()

        # Draw 8-channel Oscilloscope
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        self.canvas.delete("all")
        if w > 50 and h > 50:
            ch_h = h / 8.0
            colors = ["#F38BA8", "#FAB387", "#F9E2AF", "#A6E3A1", 
                      "#94E2D5", "#89B4FA", "#CBA6F7", "#F5C2E7"]
            n_pts = len(waveform_data)
            x_coords = np.linspace(88, w-10, n_pts)
            for fraction in np.linspace(0, 1, 9):
                x_grid = 88+(w-98)*fraction
                self.canvas.create_line(x_grid, 0, x_grid, h, fill="#1B293B")
            voltage_range = float(self.waveform_range.get())

            for ch in range(8):
                y_base = ch_h * ch + ch_h * (0.85 if show_envelope else 0.5)
                ch_name = PHYSICAL_CH_NAMES[ch]
                self.canvas.create_line(88, y_base, w-10, y_base, fill="#2B384C", dash=(2, 4))
                self.canvas.create_text(12, ch_h*(ch+.5), text=ch_name.split("(")[0], fill=colors[ch], anchor="w", font=(self.ui_font, 14, "bold"))
                if n_pts==0:
                    continue

                vals = waveform_data[:, ch]
                baseline = 0.0 if show_filtered or show_envelope else np.mean(vals)
                y_coords = y_base - np.clip(
                    (vals - baseline) / voltage_range, 0.0 if show_envelope else -1.0, 1.0
                ) * (ch_h * (0.70 if show_envelope else 0.42))

                points = []
                for x, y in zip(x_coords, y_coords):
                    points.extend([x, y])

                if len(points) >= 4:
                    self.canvas.create_line(points, fill=colors[ch], width=1)

        self.after(33, self._gui_tick)


def main():
    app = EMGStudioGUI()
    app.mainloop()

if __name__ == "__main__":
    main()

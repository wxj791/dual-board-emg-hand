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
import struct
import threading
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox
import numpy as np
import serial
from serial.tools import list_ports
import torch
import torch.nn as nn

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


class SerialCore:
    """Universal high-performance serial receiver for 921600 baud JustFloat stream"""
    def __init__(self):
        self.ser = None
        self.running = False
        self.thread = None
        
        # Display waveform buffer: 500 samples x 8 channels
        self.disp_buf = np.zeros((500, 8), dtype=np.float32)
        self.disp_lock = threading.Lock()
        
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

    def connect(self, port, baud=921600):
        self.ser = serial.Serial(port, baud, timeout=0.1)
        self.ser.reset_input_buffer()
        self.running = True
        self.total_received = 0
        self.corrupt_frames = 0
        self.last_stat_time = time.time()
        self.last_stat_count = 0
        self.thread = threading.Thread(target=self._recv_loop, daemon=True)
        self.thread.start()

    def disconnect(self):
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=0.5)
        if self.ser and self.ser.is_open:
            self.ser.close()

    def start_recording(self):
        with self.record_lock:
            self.record_buf.clear()
            self.is_recording = True

    def stop_recording(self):
        with self.record_lock:
            self.is_recording = False
            return list(self.record_buf)

    def set_label(self, label):
        self.current_label = label

    def _recv_loop(self):
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
                            # 按照物理环形拓扑重新排序
                            remapped = tuple(raw_floats[i] for i in PHYSICAL_CH_MAP)
                            parsed.append(remapped)
                            del raw_stream[:tail_idx + 4]
                        else:
                            del raw_stream[:tail_idx + 4]
                            self.corrupt_frames += 1

                    if parsed:
                        now_ts = time.time()
                        self.total_received += len(parsed)

                        # Update display waveform buffer
                        with self.disp_lock:
                            arr = np.array(parsed, dtype=np.float32)
                            n_new = len(arr)
                            if n_new >= 500:
                                self.disp_buf[:] = arr[-500:]
                            else:
                                self.disp_buf = np.roll(self.disp_buf, -n_new, axis=0)
                                self.disp_buf[-n_new:] = arr

                        # Append to training dataset if active
                        if self.is_recording:
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
                break

    def get_snapshot(self):
        with self.disp_lock:
            buf = self.disp_buf.copy()
        return buf, self.instant_sps, self.total_received, self.corrupt_frames


class EMGStudioGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("肌电工作站 - 8通道表面肌电一体化调试与采集系统")
        self.geometry("1180x780")
        self.configure(bg="#1E1E2E")
        
        self.core = SerialCore()
        self.paradigm_thread = None
        self.stop_requested = False
        self.space_event = threading.Event()

        self._build_ui()
        self._refresh_ports()
        
        # 绑定空格键全局触发下一轮采集
        self.bind("<space>", self._on_space_pressed)

        # Periodic GUI update timer (~30 FPS)
        self.after(33, self._gui_tick)

    def _build_ui(self):
        # 1. Top connection toolbar
        top = tk.Frame(self, bg="#2A2A3E", padx=12, pady=8)
        top.pack(fill=tk.X, side=tk.TOP)

        tk.Label(top, text="接收端串口:", fg="#FFFFFF", bg="#2A2A3E", font=("微软雅黑", 10)).pack(side=tk.LEFT, padx=(0, 4))
        self.cb_ports = ttk.Combobox(top, width=32, state="readonly")
        self.cb_ports.pack(side=tk.LEFT, padx=4)
        self.port_map = {}  # { "COM3 (USB-SERIAL CH340)": "COM3" }

        btn_refresh = tk.Button(top, text="刷新", command=self._refresh_ports, bg="#3E4259", fg="#FFFFFF", relief="flat", padx=6)
        btn_refresh.pack(side=tk.LEFT, padx=4)

        tk.Label(top, text="波特率:", fg="#FFFFFF", bg="#2A2A3E", font=("微软雅黑", 10)).pack(side=tk.LEFT, padx=(12, 4))
        self.entry_baud = tk.Entry(top, width=9)
        self.entry_baud.insert(0, "921600")
        self.entry_baud.pack(side=tk.LEFT, padx=4)

        self.btn_connect = tk.Button(top, text="打开串口", command=self._toggle_connect, bg="#28A745", fg="#FFFFFF", font=("微软雅黑", 10, "bold"), relief="flat", padx=14)
        self.btn_connect.pack(side=tk.LEFT, padx=12)

        # Link status banner
        self.lbl_link_status = tk.Label(top, text="链路状态: 未连接", fg="#FFAA00", bg="#2A2A3E", font=("微软雅黑", 10, "bold"))
        self.lbl_link_status.pack(side=tk.LEFT, padx=15)

        self.lbl_sps = tk.Label(top, text="实时采样率: 0 点/秒", fg="#89B4FA", bg="#2A2A3E", font=("微软雅黑", 10))
        self.lbl_sps.pack(side=tk.RIGHT, padx=10)

        # 2. Main Middle Area
        mid = tk.Frame(self, bg="#1E1E2E", padx=10, pady=10)
        mid.pack(fill=tk.BOTH, expand=True)

        # Left: Oscilloscope (Waveform Display)
        left = tk.Frame(mid, bg="#181825", relief="sunken", bd=1)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        top_osc = tk.Frame(left, bg="#181825")
        top_osc.pack(fill=tk.X, padx=8, pady=4)
        tk.Label(top_osc, text="8通道实时滤波肌电示波器 (1号板 ~ 8号板)", fg="#BAC2DE", bg="#181825", font=("微软雅黑", 10, "bold")).pack(side=tk.LEFT)
        self.lbl_channel_diag = tk.Label(top_osc, text="提示: 用力握拳观察各通道波形跳动", fg="#6C7086", bg="#181825", font=("微软雅黑", 9))
        self.lbl_channel_diag.pack(side=tk.RIGHT)

        self.canvas = tk.Canvas(left, bg="#11111B", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        # Right: Tabs Container (Tab 1: 信号测试与诊断, Tab 2: 数据集打标采集)
        right = tk.Frame(mid, bg="#2A2A3E", width=380)
        right.pack(side=tk.RIGHT, fill=tk.Y, padx=(10, 0))
        right.pack_propagate(False)

        style = ttk.Style()
        style.theme_use('default')
        style.configure("TNotebook", background="#2A2A3E", borderwidth=0)
        style.configure("TNotebook.Tab", background="#3E4259", foreground="#CDD6F4", padding=[12, 6], font=("微软雅黑", 9, "bold"))
        style.map("TNotebook.Tab", background=[("selected", "#89B4FA")], foreground=[("selected", "#11111B")])

        self.notebook = ttk.Notebook(right)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        # --- TAB 1: 信号诊断与通道健康度 (Signal Diagnostics) ---
        tab_diag = tk.Frame(self.notebook, bg="#2A2A3E", padx=10, pady=10)
        self.notebook.add(tab_diag, text=" 信号诊断与测试 ")

        diag_box = tk.LabelFrame(tab_diag, text="链路质量监视器", fg="#BAC2DE", bg="#2A2A3E", font=("微软雅黑", 9, "bold"), padx=10, pady=8)
        diag_box.pack(fill=tk.X, pady=5)

        self.lbl_diag_sps = tk.Label(diag_box, text="有效速率: -- Sps (目标 2000)", fg="#CDD6F4", bg="#2A2A3E", font=("微软雅黑", 9), anchor="w")
        self.lbl_diag_sps.pack(fill=tk.X, pady=2)
        self.lbl_diag_total = tk.Label(diag_box, text="累计接收点数: 0", fg="#CDD6F4", bg="#2A2A3E", font=("微软雅黑", 9), anchor="w")
        self.lbl_diag_total.pack(fill=tk.X, pady=2)
        self.lbl_diag_loss = tk.Label(diag_box, text="错帧 / 丢包数: 0", fg="#CDD6F4", bg="#2A2A3E", font=("微软雅黑", 9), anchor="w")
        self.lbl_diag_loss.pack(fill=tk.X, pady=2)

        meter_box = tk.LabelFrame(tab_diag, text="8路电极接触状态 (实时电压与活动度)", fg="#BAC2DE", bg="#2A2A3E", font=("微软雅黑", 9, "bold"), padx=10, pady=8)
        meter_box.pack(fill=tk.BOTH, expand=True, pady=8)

        self.ch_bars = []
        self.ch_val_labels = []
        for ch in range(8):
            ch_name = PHYSICAL_CH_NAMES[ch]
            row = tk.Frame(meter_box, bg="#2A2A3E")
            row.pack(fill=tk.X, pady=3)
            tk.Label(row, text=f"{ch_name}:", fg="#A6ADC8", bg="#2A2A3E", width=10, anchor="w", font=("Arial", 9, "bold")).pack(side=tk.LEFT)
            pb = ttk.Progressbar(row, maximum=3300, value=0, length=150)
            pb.pack(side=tk.LEFT, padx=6)
            lbl = tk.Label(row, text="0 mV", fg="#89B4FA", bg="#2A2A3E", width=8, anchor="e", font=("Arial", 9))
            lbl.pack(side=tk.RIGHT)
            self.ch_bars.append(pb)
            self.ch_val_labels.append(lbl)

        # --- TAB 2: 数据集打标采集 (Dataset Paradigm Collector) ---
        tab_col = tk.Frame(self.notebook, bg="#2A2A3E", padx=10, pady=8)
        self.notebook.add(tab_col, text=" 数据集录制 ")

        cfg_box = tk.LabelFrame(tab_col, text="采集参数配置", fg="#BAC2DE", bg="#2A2A3E", font=("微软雅黑", 9, "bold"), padx=8, pady=6)
        cfg_box.pack(fill=tk.X, pady=4)

        tk.Label(cfg_box, text="受试者名称:", fg="#FFFFFF", bg="#2A2A3E").grid(row=0, column=0, sticky="w", pady=2)
        self.entry_subject = tk.Entry(cfg_box, width=11)
        self.entry_subject.insert(0, "DGT")
        self.entry_subject.grid(row=0, column=1, sticky="e", pady=2)

        tk.Label(cfg_box, text="指定起始批次(P几):", fg="#F9E2AF", bg="#2A2A3E", font=("微软雅黑", 9, "bold")).grid(row=1, column=0, sticky="w", pady=2)
        self.entry_p_idx = tk.Entry(cfg_box, width=11, fg="#F9E2AF", bg="#181825", insertbackground="#FFFFFF", font=("微软雅黑", 9, "bold"))
        self.entry_p_idx.insert(0, "1")
        self.entry_p_idx.grid(row=1, column=1, sticky="e", pady=2)

        tk.Label(cfg_box, text="每动作重复次数:", fg="#FFFFFF", bg="#2A2A3E").grid(row=2, column=0, sticky="w", pady=2)
        self.entry_repeats = tk.Entry(cfg_box, width=11)
        self.entry_repeats.insert(0, "3")
        self.entry_repeats.grid(row=2, column=1, sticky="e", pady=2)

        tk.Label(cfg_box, text="动作发力(秒):", fg="#FFFFFF", bg="#2A2A3E").grid(row=3, column=0, sticky="w", pady=2)
        self.entry_hold = tk.Entry(cfg_box, width=11)
        self.entry_hold.insert(0, "5.0")
        self.entry_hold.grid(row=3, column=1, sticky="e", pady=2)

        tk.Label(cfg_box, text="静息放松(秒):", fg="#FFFFFF", bg="#2A2A3E").grid(row=4, column=0, sticky="w", pady=2)
        self.entry_rest = tk.Entry(cfg_box, width=11)
        self.entry_rest.insert(0, "3.0")
        self.entry_rest.grid(row=4, column=1, sticky="e", pady=2)

        self.continuous_mode_var = tk.BooleanVar(value=True)
        cb_cont = tk.Checkbutton(
            cfg_box, 
            text="🔁 连续持续采集 (自动循环)", 
            variable=self.continuous_mode_var, 
            fg="#A6E3A1", 
            bg="#2A2A3E", 
            selectcolor="#181825", 
            activebackground="#2A2A3E", 
            font=("微软雅黑", 9, "bold")
        )
        cb_cont.grid(row=5, column=0, columnspan=2, sticky="w", pady=4)

        g_box = tk.LabelFrame(tab_col, text="待录制动作勾选 (6个动作)", fg="#BAC2DE", bg="#2A2A3E", font=("微软雅黑", 9, "bold"), padx=8, pady=4)
        g_box.pack(fill=tk.X, pady=4)

        self.gesture_vars = {}
        for idx, gid in enumerate(range(1, len(GESTURE_DICT))):
            gname = GESTURE_DICT[gid][0]
            # 默认全部勾选 6 个动作 (握拳, 张开手掌, 点赞, 剪刀手, 数字一, 数字六)
            var = tk.BooleanVar(value=True)
            self.gesture_vars[gid] = var
            cb = tk.Checkbutton(g_box, text=f"动作{gid}: {gname}", variable=var, fg="#CDD6F4", bg="#2A2A3E", selectcolor="#181825", activebackground="#2A2A3E", activeforeground="#FFFFFF", font=("微软雅黑", 9))
            cb.grid(row=idx // 2, column=idx % 2, sticky="w", padx=6, pady=2)

        board = tk.LabelFrame(tab_col, text="动作指令看板", fg="#BAC2DE", bg="#2A2A3E", font=("微软雅黑", 9, "bold"), padx=8, pady=6)
        board.pack(fill=tk.BOTH, expand=True, pady=4)

        self.lbl_stage = tk.Label(board, text="就绪待命", fg="#A6ADC8", bg="#2A2A3E", font=("微软雅黑", 10))
        self.lbl_stage.pack(pady=1)

        self.lbl_action = tk.Label(board, text="--", fg="#89B4FA", bg="#2A2A3E", font=("微软雅黑", 18, "bold"))
        self.lbl_action.pack(pady=2)

        self.lbl_countdown = tk.Label(board, text="", fg="#FAB387", bg="#2A2A3E", font=("Arial", 22, "bold"))
        self.lbl_countdown.pack(pady=2)

        self.lbl_next_action = tk.Label(board, text="👉 下一个动作: --", fg="#A6ADC8", bg="#1E1E2E", font=("微软雅黑", 11, "bold"), padx=10, pady=4, relief="ridge")
        self.lbl_next_action.pack(pady=4, fill=tk.X)

        btn_box = tk.Frame(tab_col, bg="#2A2A3E")
        btn_box.pack(fill=tk.X, side=tk.BOTTOM, pady=4)

        self.btn_start = tk.Button(btn_box, text="▶ 开始采集 (静息/动作交替录制)", command=self._start_collection, bg="#89B4FA", fg="#11111B", font=("微软雅黑", 10, "bold"), relief="flat", pady=5)
        self.btn_start.pack(fill=tk.X, pady=2)

        self.btn_abort = tk.Button(btn_box, text="⏹ 停止采集并保存本段数据", command=self._abort_collection, bg="#F38BA8", fg="#11111B", font=("微软雅黑", 9, "bold"), relief="flat", state="disabled")
        self.btn_abort.pack(fill=tk.X, pady=2)

        # --- TAB 3: 模型实时推理验证 (Real-time CNN Validation) ---
        tab_infer = tk.Frame(self.notebook, bg="#2A2A3E", padx=10, pady=8)
        self.notebook.add(tab_infer, text=" 模型实时验证 ")

        model_ctrl = tk.LabelFrame(tab_infer, text="模型状态控制", fg="#BAC2DE", bg="#2A2A3E", font=("微软雅黑", 9, "bold"), padx=10, pady=8)
        model_ctrl.pack(fill=tk.X, pady=4)

        self.lbl_model_file = tk.Label(model_ctrl, text="当前模型: 未载入", fg="#F9E2AF", bg="#2A2A3E", font=("微软雅黑", 9), anchor="w")
        self.lbl_model_file.pack(fill=tk.X, pady=2)

        infer_btn_box = tk.Frame(model_ctrl, bg="#2A2A3E")
        infer_btn_box.pack(fill=tk.X, pady=4)
        self.btn_load_model = tk.Button(infer_btn_box, text="🔄 重新载入最新模型", command=self._load_trained_model, bg="#3E4259", fg="#FFFFFF", font=("微软雅黑", 9), relief="flat")
        self.btn_load_model.pack(side=tk.LEFT, padx=2)
        self.infer_active_var = tk.BooleanVar(value=True)
        self.cb_infer_active = tk.Checkbutton(infer_btn_box, text="开启实时识别", variable=self.infer_active_var, fg="#A6E3A1", bg="#2A2A3E", selectcolor="#181825", activebackground="#2A2A3E", font=("微软雅黑", 9, "bold"))
        self.cb_infer_active.pack(side=tk.RIGHT, padx=4)

        infer_card = tk.LabelFrame(tab_infer, text="当前识别手势", fg="#BAC2DE", bg="#2A2A3E", font=("微软雅黑", 9, "bold"), padx=10, pady=10)
        infer_card.pack(fill=tk.BOTH, expand=True, pady=6)

        self.lbl_pred_gesture = tk.Label(infer_card, text="静息", fg="#A6E3A1", bg="#2A2A3E", font=("微软雅黑", 26, "bold"))
        self.lbl_pred_gesture.pack(pady=4)

        self.lbl_pred_conf = tk.Label(infer_card, text="置信度: 0.0%", fg="#89B4FA", bg="#2A2A3E", font=("Arial", 13, "bold"))
        self.lbl_pred_conf.pack(pady=2)

        # 概率分布柱状条
        self.prob_bars = []
        self.prob_labels = []
        prob_box = tk.Frame(infer_card, bg="#2A2A3E")
        prob_box.pack(fill=tk.BOTH, expand=True, pady=6)
        for gid in range(len(GESTURE_DICT)):
            gname = GESTURE_DICT[gid][0]
            p_row = tk.Frame(prob_box, bg="#2A2A3E")
            p_row.pack(fill=tk.X, pady=1)
            tk.Label(p_row, text=f"{gname}:", fg="#CDD6F4", bg="#2A2A3E", width=8, anchor="w", font=("微软雅黑", 8)).pack(side=tk.LEFT)
            p_bar = ttk.Progressbar(p_row, maximum=100, value=0, length=150)
            p_bar.pack(side=tk.LEFT, padx=4)
            p_lbl = tk.Label(p_row, text="0%", fg="#BAC2DE", bg="#2A2A3E", width=5, anchor="e", font=("Arial", 8))
            p_lbl.pack(side=tk.RIGHT)
            self.prob_bars.append(p_bar)
            self.prob_labels.append(p_lbl)

        # 默认尝试载入已训练模型
        self.cnn_model = None
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self._load_trained_model()

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
            except Exception as e:
                messagebox.showerror("连接失败", f"无法打开串口: {e}")
        else:
            if self.core.is_recording:
                self._abort_collection()
            self.core.disconnect()
            self.btn_connect.configure(text="打开串口", bg="#28A745")
            self.lbl_link_status.configure(text="链路状态: 🔴 未连接", fg="#FFAA00")
            self.lbl_sps.configure(text="采样率: 0 Sps")

    def _start_collection(self):
        if not self.core.running:
            messagebox.showwarning("提示", "请先连接串口再开始采集！")
            return
        
        gestures = [gid for gid, var in self.gesture_vars.items() if var.get()]
        if not gestures:
            messagebox.showwarning("提示", "请至少勾选一个手势动作！")
            return

        try:
            repeats = int(self.entry_repeats.get().strip())
            hold_sec = float(self.entry_hold.get().strip())
            rest_sec = float(self.entry_rest.get().strip())
            subject = self.entry_subject.get().strip() or "user01"
            p_text = self.entry_p_idx.get().strip()
            start_p = int(p_text) if p_text.isdigit() else 1
        except Exception:
            messagebox.showerror("参数错误", "请输入合法的数值格式！")
            return

        self.btn_start.configure(state="disabled")
        self.btn_abort.configure(state="normal")
        self.stop_requested = False

        self.paradigm_thread = threading.Thread(
            target=self._run_collection_worker,
            args=(subject, gestures, repeats, hold_sec, rest_sec, start_p),
            daemon=True
        )
        self.paradigm_thread.start()

    def _on_space_pressed(self, event=None):
        """用户按下空格键触发下一轮采集"""
        self.space_event.set()

    def _abort_collection(self):
        self.stop_requested = True
        self.space_event.set()  # 解除可能正在等待的空格阻塞
        partial_data = self.core.stop_recording()
        self.btn_start.configure(state="normal")
        self.btn_abort.configure(state="disabled")
        self.lbl_stage.configure(text="已中止 (部分数据已保留)", fg="#F38BA8")
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
                emg=emg, labels=lbl, timestamps=ts, sample_rate=fs, gestures=GESTURE_DICT
            )

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

    def _run_collection_worker(self, subject, gestures, repeats, hold_sec, rest_sec, start_p=1):
        continuous_mode = self.continuous_mode_var.get()
        cycle_count = 0
        current_p = int(start_p)

        while not self.stop_requested:
            cycle_count += 1
            self.core.start_recording()

            # 1. 准备倒计时 (首次3秒倒计时，后续循环为1.5秒平滑过渡)
            first_gname = GESTURE_DICT[gestures[0]][0] if gestures else "--"
            self.lbl_stage.configure(text=f"【当前录制: P{current_p}】准备开始", fg="#F9E2AF")
            self.lbl_action.configure(text="准备就绪", fg="#FAB387")
            self.lbl_next_action.configure(text="👉 先行静息录制 (请手臂完全放松)", fg="#A6E3A1")
            prep_secs = 3 if cycle_count == 1 else 2
            for c in range(prep_secs, 0, -1):
                if self.stop_requested: break
                self.lbl_countdown.configure(text=f"{c}")
                time.sleep(1)
            if self.stop_requested: break

            # 2. 纯净交替循环：静息 -> 发力 -> 静息 -> 发力 -> ... -> 静息
            # 全程统一标签 0: 静息/放松
            schedule = []
            for gid in gestures:
                for r in range(1, repeats + 1):
                    schedule.append((gid, GESTURE_DICT[gid][0], r))

            total_actions = len(schedule)

            for i, (gid, gname, r) in enumerate(schedule):
                if self.stop_requested: break
                done = i + 1

                # ----------------------------------------------------------
                # A. 静息放松期 (标签 0: 静息)
                # ----------------------------------------------------------
                self.core.set_label(0)
                self.lbl_stage.configure(text=f"录制: P{current_p} | 进度 {done}/{total_actions} [静息放松]", fg="#A6E3A1")
                self.lbl_action.configure(text="【静息 / 放松】", fg="#A6E3A1")
                self.lbl_next_action.configure(text=f"👉 准备动作: 【{gname}】(第 {r}/{repeats} 次)", fg="#FAB387")
                
                t_rest = time.time()
                while time.time() - t_rest < rest_sec:
                    if self.stop_requested: break
                    rem = rest_sec - (time.time() - t_rest)
                    self.lbl_countdown.configure(text=f"放松: {rem:3.1f} s")
                    time.sleep(0.05)
                if self.stop_requested: break

                # ----------------------------------------------------------
                # B. 动作发力期 (标签 gid)
                # ----------------------------------------------------------
                self.core.set_label(gid)
                if r < repeats:
                    next_desc = f"【放松】后继续【{gname}】"
                elif i + 1 < len(schedule):
                    next_desc = f"【放松】后做【{schedule[i+1][1]}】"
                else:
                    next_desc = "【尾部放松】后保存"

                self.lbl_stage.configure(text=f"录制: P{current_p} | 进度 {done}/{total_actions} [发力保持]", fg="#F38BA8")
                self.lbl_action.configure(text=f"【{gname}】发力保持!", fg="#F38BA8")
                self.lbl_next_action.configure(text=f"👉 下一步: {next_desc}", fg="#A6ADC8")
                
                t_hold = time.time()
                while time.time() - t_hold < hold_sec:
                    if self.stop_requested: break
                    rem = hold_sec - (time.time() - t_hold)
                    self.lbl_countdown.configure(text=f"保持: {rem:3.1f} s")
                    time.sleep(0.05)
                if self.stop_requested: break

            # --------------------------------------------------------------
            # C. 最后一个发力动作完成后的尾部收尾静息 (保证最后一个动作后也有静息)
            # --------------------------------------------------------------
            if not self.stop_requested:
                self.core.set_label(0)
                self.lbl_stage.configure(text=f"录制: P{current_p} | 尾部收尾 [静息放松]", fg="#A6E3A1")
                self.lbl_action.configure(text="【静息 / 放松】", fg="#A6E3A1")
                self.lbl_next_action.configure(text="👉 本轮动作结束，即将落盘存盘", fg="#A6E3A1")
                t_tail = time.time()
                while time.time() - t_tail < rest_sec:
                    if self.stop_requested: break
                    rem = rest_sec - (time.time() - t_tail)
                    self.lbl_countdown.configure(text=f"放松: {rem:3.1f} s")
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
                    emg=emg,
                    labels=lbl,
                    timestamps=ts,
                    sample_rate=fs,
                    gestures=GESTURE_DICT
                )
                print(f"[采集落盘] 成功保存第 {p_idx} 组数据 -> {out_path} (共 {len(lbl)} 点)")

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
                text=f"✅ P{saved_p_idx} 已存盘！当前处于等待中...", 
                fg="#A6E3A1"
            )
            self.lbl_action.configure(text="【请按 空格键 开始下一轮】", fg="#F9E2AF")
            self.lbl_countdown.configure(text=f"等待 P{next_p_idx}")
            self.lbl_next_action.configure(
                text=f"👉 已就绪: 准备采集 P{next_p_idx}，手臂准备好后轻按【空格键】立即开始！", 
                fg="#89B4FA"
            )

            self.space_event.clear()
            # 循环等待空格事件或中止信号
            while not self.stop_requested and not self.space_event.is_set():
                time.sleep(0.05)

            if self.stop_requested:
                break

            # 空格按下后，流转到下一个 P 序号
            current_p = next_p_idx

        self.btn_start.configure(state="normal")
        self.btn_abort.configure(state="disabled")
        self.lbl_countdown.configure(text="")
        if not self.stop_requested:
            self.lbl_stage.configure(text="采集流程全部顺利完成!", fg="#A6E3A1")
            self.lbl_action.configure(text="全部就绪", fg="#A6E3A1")
            messagebox.showinfo("完成", f"恭喜！受试者 【{subject}】 的手势采集已全部完成并归档存盘！")
        else:
            self.lbl_stage.configure(text="连续采集已手动停止 (已录制部分均已安全存盘)", fg="#F38BA8")
            self.lbl_action.configure(text="已停止", fg="#A6ADC8")

    def _load_trained_model(self):
        models_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
        dgt_fpga_path = os.path.join(models_dir, "best_dgt_fpga_model.pth")
        resnet_path = os.path.join(models_dir, "best_resnet1d.pth")
        cnn_path = os.path.join(models_dir, "best_emg_cnn.pth")

        # 优先载入最新训练模型 (DGTV1.9.pth -> DGTV1.8.pth -> DGTV1.0.pth -> best_dgt_fpga_model.pth)
        candidates = [
            os.path.join(models_dir, "DGTV1.9.pth"),
            os.path.join(models_dir, "DGTV1.8.pth"),
            os.path.join(models_dir, "DGTV1.0.pth"),
            dgt_fpga_path
        ]
        load_target = next((p for p in candidates if os.path.exists(p)), None)

        if os.path.exists(load_target):
            try:
                ckpt = torch.load(load_target, map_location=self.device, weights_only=False)
                if isinstance(ckpt, dict) and 'scaler_mean' in ckpt:
                    # MLP 格式
                    self.cnn_model = SuperFeatureMLP(in_dim=56, num_classes=7).to(self.device)
                    self.cnn_model.load_state_dict(ckpt['state_dict'])
                    self.cnn_model.eval()
                    self.scaler_mean = np.array(ckpt['scaler_mean'], dtype=np.float32)
                    self.scaler_scale = np.array(ckpt['scaler_scale'], dtype=np.float32)
                    self.active_model_type = "mlp_56"
                    self.lbl_model_file.configure(
                        text=f"当前模型: 🟢 {os.path.basename(load_target)} [SuperFeatureMLP 7分类]", 
                        fg="#A6E3A1"
                    )
                    print(f"-> 成功载入 DGT FPGA 7分类模型: SuperFeatureMLP")
                    return
                else:
                    # 纯 state_dict 格式 (如 ResNet1D-Light)
                    self.cnn_model = ResNet1DLight(in_channels=8, num_classes=7).to(self.device)
                    self.cnn_model.load_state_dict(ckpt)
                    self.cnn_model.eval()
                    self.active_model_type = "resnet"
                    self.lbl_model_file.configure(
                        text=f"当前模型: 🟢 {os.path.basename(load_target)} [ResNet1D-Light 99.32% (FPGA全片上BRAM最优)]", 
                        fg="#A6E3A1"
                    )
                    print(f"-> 成功载入最新 DGT 2.0 冠军模型: ResNet1D-Light (99.32%)")
                    return
            except Exception as e:
                print(f"[Warn] 载入 {load_target} 异常: {e}")

        if os.path.exists(resnet_path):
            try:
                self.cnn_model = ResNet1DLight(in_channels=8, num_classes=7).to(self.device)
                self.cnn_model.load_state_dict(torch.load(resnet_path, map_location=self.device))
                self.cnn_model.eval()
                self.active_model_type = "resnet"
                self.lbl_model_file.configure(text="当前模型: 🟢 best_resnet1d.pth [ResNet1D-Light]", fg="#A6E3A1")
                return
            except Exception as e:
                print(f"[Warn] 载入 best_resnet1d.pth 异常: {e}")

        if os.path.exists(cnn_path):
            try:
                self.cnn_model = EMG1DCNN(in_channels=8, num_classes=5).to(self.device)
                self.cnn_model.load_state_dict(torch.load(cnn_path, map_location=self.device))
                self.cnn_model.eval()
                self.active_model_type = "cnn"
                self.lbl_model_file.configure(text="当前模型: 🟢 best_emg_cnn.pth (旧版5分类)", fg="#A6E3A1")
            except Exception as e:
                self.lbl_model_file.configure(text=f"载入失败: {e}", fg="#F38BA8")
        else:
            self.lbl_model_file.configure(text="当前模型: 🔴 未找到模型权重文件", fg="#FFAA00")

    def _gui_tick(self):
        """Update waveform canvas and diagnostic meters at ~30 FPS"""
        data, sps, total, loss = self.core.get_snapshot()

        if self.core.running:
            self.lbl_sps.configure(text=f"实时采样率: {sps:4.0f} 点/秒")
            self.lbl_diag_sps.configure(text=f"有效传输速率: {sps:4.0f} 点/秒 (目标 2000)")
            self.lbl_diag_total.configure(text=f"累计接收点数: {total:,}")
            self.lbl_diag_loss.configure(text=f"错帧 / 丢包数: {loss}")

            # Update per-channel amplitude meters (last point)
            if len(data) > 0:
                last_pt = data[-1]
                for ch in range(8):
                    val = float(last_pt[ch])
                    self.ch_bars[ch]["value"] = val
                    self.ch_val_labels[ch].configure(text=f"{val:4.0f} mV")

            # --- 实时 7分类/5分类 识别推理 (120点滑动窗口，约200ms) ---
            if self.infer_active_var.get() and self.cnn_model is not None and len(data) >= 120:
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

        # Draw 8-channel Oscilloscope
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        if w > 50 and h > 50 and len(data) > 0:
            self.canvas.delete("all")
            ch_h = h / 8.0
            colors = ["#F38BA8", "#FAB387", "#F9E2AF", "#A6E3A1", 
                      "#94E2D5", "#89B4FA", "#CBA6F7", "#F5C2E7"]
            n_pts = len(data)
            x_coords = np.linspace(0, w, n_pts)

            for ch in range(8):
                y_base = ch_h * ch + ch_h * 0.5
                ch_name = PHYSICAL_CH_NAMES[ch]
                self.canvas.create_line(0, y_base, w, y_base, fill="#313244", dash=(2, 4))
                self.canvas.create_text(8, y_base - 10, text=f"{ch_name}", fill=colors[ch], anchor="nw", font=("微软雅黑", 8, "bold"))

                vals = data[:, ch]
                baseline = np.mean(vals) if len(vals) > 0 else 1650.0
                y_coords = y_base - (vals - baseline) * (ch_h * 0.42 / 1200.0)

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

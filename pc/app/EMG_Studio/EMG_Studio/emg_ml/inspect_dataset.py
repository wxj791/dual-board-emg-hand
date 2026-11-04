#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
inspect_dataset.py
Quick quality inspection tool for recorded EMG dataset (.npz)
"""

import os
import glob
import numpy as np

def inspect_latest():
    raw_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "raw")
    files = glob.glob(os.path.join(raw_dir, "*.npz"))
    valid_files = [f for f in files if "checkpoint" not in f and "aborted" not in f]
    target_file = sorted(valid_files, key=os.path.getmtime)[-1] if valid_files else (sorted(files, key=os.path.getmtime)[-1] if files else None)

    if not target_file:
        print("未找到任何 .npz 数据集文件！")
        return

    print("=" * 65)
    print(f"正在分析最新数据集: {os.path.basename(target_file)}")
    print("=" * 65)

    data = np.load(target_file, allow_pickle=True)
    emg = data['emg']          # [N, 8]
    labels = data['labels']    # [N]
    ts = data['timestamps']    # [N]
    fs = float(data['sample_rate'])
    gestures = data['gestures'].item() if 'gestures' in data else {}

    total_samples = len(labels)
    dur = ts[-1] - ts[0]

    print(f"1. 基本概况:")
    print(f"   - 总采样点数 : {total_samples:,} 点")
    print(f"   - 录制总时长 : {dur:.1f} 秒 ({dur/60:.1f} 分钟)")
    print(f"   - 实际传输率 : {fs:.1f} Hz (标准为 2000Hz)")

    print(f"\n2. 各动作标签有效时间统计:")
    for gid in sorted(np.unique(labels)):
        cnt = np.sum(labels == gid)
        sec = cnt / fs
        g_info = gestures.get(gid, ("未知", ""))
        g_name = g_info[0] if isinstance(g_info, (list, tuple)) else str(g_info)
        print(f"   - [动作 {gid}] {g_name:<6}: {cnt:6,d} 点 ({sec:5.1f} 秒, 占比 {cnt/total_samples*100:4.1f}%)")

    print(f"\n3. 8通道电极信号接触质量诊断 (0~3300mV):")
    print("   通道    |   均值(基线)   | 最小 ~ 最大 (mV) | 标准差(波动度) | 接触质量评价")
    print("   " + "-" * 62)
    for ch in range(8):
        vals = emg[:, ch]
        mean_v = np.mean(vals)
        min_v = np.min(vals)
        max_v = np.max(vals)
        std_v = np.std(vals)

        # 评价逻辑
        if max_v < 100.0:
            eval_str = "[FAIL] 严重接地短路 / 悬空无信号"
        elif min_v > 3200.0:
            eval_str = "[FAIL] 严重饱和顶格 / 电源串扰"
        elif std_v < 5.0:
            eval_str = "[WARN] 波动极死板，电极可能未贴紧"
        elif std_v > 600.0:
            eval_str = "[WARN] 波动过大，可能手环晃动"
        else:
            eval_str = "[PASS] 接触优良，动态范围正常"

        print(f"   {ch+1}号板  |  {mean_v:6.1f} mV   | {min_v:4.0f} ~ {max_v:4.0f} mV |   {std_v:5.1f} mV    | {eval_str}")

    print("=" * 65)

if __name__ == "__main__":
    inspect_latest()

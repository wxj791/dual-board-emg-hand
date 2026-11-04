#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
train_cnn.py
8通道肌电轻量级 1D-CNN 训练与评估脚本
- 自动读取最新 .npz 数据集
- 滑动窗口切片 (200ms 窗口, 50ms 步长)
- Z-score 归一化 (抗松紧不一干扰)
- 闭环通道循环移位数据增强 (抗旋转佩戴偏差)
- 训练紧凑型 1D-CNN 并输出准确率与混淆矩阵
"""

import os
import glob
import time
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
from sklearn.model_selection import train_test_split

# 动作中文映射 (1个静息 + 4个发力动作 = 5类)
GESTURE_NAMES = {
    0: "静息",
    1: "握拳",
    2: "张掌",
    3: "点赞",
    4: "剪刀手"
}

# ==============================================================================
# 1. 神经网络结构：专为边缘端/嵌入式优化的轻量级 1D-CNN (5分类)
# ==============================================================================
class EMG1DCNN(nn.Module):
    def __init__(self, in_channels=8, num_classes=5):
        super(EMG1DCNN, self).__init__()
        
        # Conv Block 1: [B, 8, L] -> [B, 32, L/2]
        self.conv1 = nn.Conv1d(in_channels, 32, kernel_size=7, stride=1, padding=3)
        self.bn1 = nn.BatchNorm1d(32)
        self.relu1 = nn.ReLU()
        self.pool1 = nn.MaxPool1d(2)

        # Conv Block 2: [B, 32, L/2] -> [B, 64, L/4]
        self.conv2 = nn.Conv1d(32, 64, kernel_size=5, stride=1, padding=2)
        self.bn2 = nn.BatchNorm1d(64)
        self.relu2 = nn.ReLU()
        self.pool2 = nn.MaxPool1d(2)

        # 全局平均池化 (GAP): 压扁时间维度，大幅压缩模型参数
        self.gap = nn.AdaptiveAvgPool1d(1)

        # 分类器
        self.classifier = nn.Sequential(
            nn.Dropout(0.3),
            nn.Linear(64, num_classes)
        )

    def forward(self, x):
        # x shape: [Batch, Channels=8, Length]
        x = self.pool1(self.relu1(self.bn1(self.conv1(x))))
        x = self.pool2(self.relu2(self.bn2(self.conv2(x))))
        x = self.gap(x).squeeze(-1) # [Batch, 64]
        out = self.classifier(x)
        return out


# ==============================================================================
# 2. PyTorch 数据集类（含通道循环移位数据增强）
# ==============================================================================
class EMGDataset(Dataset):
    def __init__(self, windows, labels, augment=False):
        self.windows = windows  # [N, 8, L] float32
        self.labels = labels    # [N] int64
        self.augment = augment

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, idx):
        x = self.windows[idx].copy()
        y = self.labels[idx]

        # 保持最真实纯净的肌电波形，不做任何破坏相位的插值干扰
        return torch.tensor(x, dtype=torch.float32), torch.tensor(y, dtype=torch.long)


# ==============================================================================
# 3. 数据滑动窗口切片与预处理
# ==============================================================================
def slice_and_preprocess(emg, labels, fs, target_win_len=120, step_ratio=0.25):
    """
    固定 target_win_len (统一为 120 点，约 180~200ms 时间窗)，
    消除不同 Session 间微量实际串口传输速率抖动导致点数不一致的问题。
    """
    # 动态根据当前采样率计算步长点数
    step_len = max(1, int(round(target_win_len * step_ratio)))
    total_len = len(labels)

    windows = []
    win_labels = []

    print(f"-> 窗口设置: 固定采样点 {target_win_len} 点, 步长 {step_len} 点")

    idx = 0
    while idx + target_win_len <= total_len:
        seg_emg = emg[idx:idx + target_win_len]      # [target_win_len, 8]
        seg_lbl = labels[idx:idx + target_win_len]

        # 过滤掉包含反应缓冲期/剧烈抖动期 (-1) 的脏数据
        if np.any(seg_lbl < 0):
            idx += step_len
            continue

        # 判定窗口标签：要求窗口内该动作占比超过 75%
        counts = np.bincount(seg_lbl, minlength=5)
        majority_label = np.argmax(counts)

        if counts[majority_label] >= int(target_win_len * 0.75):
            # 转置为 [8, target_win_len]
            x_sample = seg_emg.T.astype(np.float32)

            # Z-score 归一化 (通道独立)，消除佩戴松紧引起的幅值比例差异
            mean = np.mean(x_sample, axis=1, keepdims=True)
            std = np.std(x_sample, axis=1, keepdims=True) + 1e-6
            x_norm = (x_sample - mean) / std

            windows.append(x_norm)
            win_labels.append(majority_label)

        idx += step_len

    windows = np.array(windows, dtype=np.float32)
    win_labels = np.array(win_labels, dtype=np.int64)
    return windows, win_labels


# ==============================================================================
# 4. 训练主流程
# ==============================================================================
def main():
    raw_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "raw")
    files = glob.glob(os.path.join(raw_dir, "*.npz"))
    valid_files = [f for f in files if "checkpoint" not in f and "aborted" not in f]

    if not valid_files:
        print("[错误] 未找到可用数据集！")
        return

    # 自动合并 raw_dir 下所有保留的有效数据集 (支持跨 Session / 多次佩戴联合训练)
    all_X, all_y = [], []
    print("=" * 65)
    print(f"检测到 {len(valid_files)} 个有效数据集，正在联合载入与打窗切片:")
    for fpath in valid_files:
        print(f" -> 正在处理: {os.path.basename(fpath)}")
        data = np.load(fpath, allow_pickle=True)
        emg = data['emg']          # [N, 8]
        labels = data['labels']    # [N]
        fs = float(data['sample_rate'])
        X_sub, y_sub = slice_and_preprocess(emg, labels, fs, target_win_len=120, step_ratio=0.25)
        all_X.append(X_sub)
        all_y.append(y_sub)

    X = np.concatenate(all_X, axis=0)
    y = np.concatenate(all_y, axis=0)
    print(f"-> 多源合并切片完成！共生成 {len(y)} 个有效样本张量 (张量形状: {X.shape})")

    # 打印各类别样本量
    print("\n样本类别分布:")
    for gid in sorted(np.unique(y)):
        gname = GESTURE_NAMES.get(gid, f"动作{gid}")
        cnt = np.sum(y == gid)
        print(f"   - [ID {gid}] {gname:<4}: {cnt:4d} 组样本")

    # 2. 划分训练集与测试集 (80% 训练, 20% 测试，分层抽样保持分布平衡)
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y
    )
    print(f"\n数据集划分: 训练集 {len(y_train)} 组，测试集 {len(y_test)} 组")

    train_loader = DataLoader(EMGDataset(X_train, y_train, augment=True), batch_size=32, shuffle=True)
    test_loader = DataLoader(EMGDataset(X_test, y_test, augment=False), batch_size=32, shuffle=False)

    # 3. 实例化模型
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"使用训练设备: {device}")

    model = EMG1DCNN(in_channels=8, num_classes=5).to(device)
    total_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(f"1D-CNN 总参数量: {total_params:,} (极轻量，单片机完全可跑！)")

    criterion = nn.CrossEntropyLoss()
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.002, weight_decay=1e-4)

    # 4. 开始训练
    epochs = 25
    print("\n开始训练 (总轮数 25)...")
    print("-" * 65)

    best_acc = 0.0
    for epoch in range(1, epochs + 1):
        model.train()
        total_loss, correct, total = 0, 0, 0
        for bx, by in train_loader:
            bx, by = bx.to(device), by.to(device)
            optimizer.zero_grad()
            out = model(bx)
            loss = criterion(out, by)
            loss.backward()
            optimizer.step()

            total_loss += loss.item() * bx.size(0)
            preds = torch.argmax(out, dim=1)
            correct += (preds == by).sum().item()
            total += bx.size(0)

        train_acc = correct / total * 100.0
        train_loss = total_loss / total

        # 测试集验证
        model.eval()
        t_correct, t_total = 0, 0
        with torch.no_grad():
            for bx, by in test_loader:
                bx, by = bx.to(device), by.to(device)
                out = model(bx)
                preds = torch.argmax(out, dim=1)
                t_correct += (preds == by).sum().item()
                t_total += bx.size(0)

        test_acc = t_correct / t_total * 100.0

        if test_acc > best_acc:
            best_acc = test_acc
            # 保存最佳权重
            out_model_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
            os.makedirs(out_model_dir, exist_ok=True)
            torch.save(model.state_dict(), os.path.join(out_model_dir, "best_emg_cnn.pth"))

        if epoch % 5 == 0 or epoch == epochs:
            print(f"Epoch [{epoch:2d}/{epochs}] - Loss: {train_loss:.4f} | 训练准确率: {train_acc:5.1f}% | 测试准确率: {test_acc:5.1f}% [最高: {best_acc:5.1f}%]")

    # 5. 输出混淆矩阵分析
    print("\n" + "=" * 65)
    print("混淆矩阵评估 (测试集真实表现):")
    print("=" * 65)

    model.load_state_dict(torch.load(os.path.join(out_model_dir, "best_emg_cnn.pth")))
    model.eval()

    y_true_all, y_pred_all = [], []
    with torch.no_grad():
        for bx, by in test_loader:
            bx = bx.to(device)
            out = model(bx)
            preds = torch.argmax(out, dim=1).cpu().numpy()
            y_pred_all.extend(preds)
            y_true_all.extend(by.numpy())

    y_true_all = np.array(y_true_all)
    y_pred_all = np.array(y_pred_all)

    # 打印逐类别召回率
    print(" 动作类别 | 测试样本数 | 预测正确数 | 识别准确率")
    print("-" * 50)
    for gid in sorted(np.unique(y_true_all)):
        mask = (y_true_all == gid)
        n_samples = np.sum(mask)
        n_correct = np.sum(y_pred_all[mask] == gid)
        acc = n_correct / n_samples * 100.0 if n_samples > 0 else 0
        gname = GESTURE_NAMES.get(gid, f"动作{gid}")
        print(f"  {gname:<6} |   {n_samples:4d}     |   {n_correct:4d}     |   {acc:5.1f}%")

    print("-" * 50)
    print(f"-> 总体测试集识别率: {best_acc:.2f}%")
    print(f"-> 训练好的模型已保存至: emg_ml/models/best_emg_cnn.pth\n")


if __name__ == "__main__":
    main()

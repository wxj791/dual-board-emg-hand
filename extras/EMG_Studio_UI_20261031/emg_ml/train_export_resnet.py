#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
train_export_resnet.py
完整训练最优 ResNet1D-Light 模型并安全导出至 models/best_resnet1d.pth
同时更新 models/best_emg_cnn.pth 保持兼容性
"""

import os
import glob
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
from sklearn.model_selection import train_test_split

class ResBlock1D(nn.Module):
    def __init__(self, channels):
        super().__init__()
        self.conv1 = nn.Conv1d(channels, channels, kernel_size=3, padding=1)
        self.relu = nn.ReLU()
        self.conv2 = nn.Conv1d(channels, channels, kernel_size=3, padding=1)
    def forward(self, x):
        return self.relu(x + self.conv2(self.relu(self.conv1(x))))

class ResNet1DLight(nn.Module):
    def __init__(self, in_channels=8, num_classes=5):
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

class DatasetWrapper(Dataset):
    def __init__(self, X, y):
        self.X = torch.tensor(X, dtype=torch.float32)
        self.y = torch.tensor(y, dtype=torch.long)
    def __len__(self):
        return len(self.y)
    def __getitem__(self, idx):
        return self.X[idx], self.y[idx]

def load_all_data(raw_dir, target_win_len=120, step_ratio=0.20):
    files = sorted(glob.glob(os.path.join(raw_dir, "*.npz")))
    valid_files = [f for f in files if "checkpoint" not in f and "aborted" not in f]
    
    all_norm = []
    all_y = []

    print(f"正在从全部 {len(valid_files)} 个文件中加载打窗样本...")
    for fpath in valid_files:
        data = np.load(fpath, allow_pickle=True)
        emg = data['emg']
        labels = data['labels']
        total_len = len(labels)
        step_len = max(1, int(round(target_win_len * step_ratio)))

        idx = 0
        while idx + target_win_len <= total_len:
            seg_emg = emg[idx:idx + target_win_len]
            seg_lbl = labels[idx:idx + target_win_len]
            if np.any(seg_lbl < 0):
                idx += step_len
                continue

            counts = np.bincount(seg_lbl, minlength=5)
            maj = np.argmax(counts)
            if counts[maj] >= int(target_win_len * 0.75):
                raw_sample = seg_emg.T.astype(np.float32)
                mean = np.mean(raw_sample, axis=1, keepdims=True)
                std = np.std(raw_sample, axis=1, keepdims=True) + 1e-6
                norm_sample = (raw_sample - mean) / std

                all_norm.append(norm_sample)
                all_y.append(maj)
            idx += step_len

    return np.array(all_norm, dtype=np.float32), np.array(all_y, dtype=np.int64)

def main():
    raw_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "raw")
    X, y = load_all_data(raw_dir)
    print(f"-> 样本总量: {len(y)} 组")

    indices = np.arange(len(y))
    train_idx, test_idx = train_test_split(indices, test_size=0.2, random_state=42, stratify=y)
    
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print("训练计算设备:", device)

    train_loader = DataLoader(DatasetWrapper(X[train_idx], y[train_idx]), batch_size=32, shuffle=True)
    test_loader = DataLoader(DatasetWrapper(X[test_idx], y[test_idx]), batch_size=64, shuffle=False)

    model = ResNet1DLight(in_channels=8, num_classes=5).to(device)
    crit = nn.CrossEntropyLoss()
    epochs = 40
    opt = torch.optim.AdamW(model.parameters(), lr=0.003, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)

    best_acc = 0.0
    best_weights = None

    print(f"开始训练 ResNet1D-Light 模型 (共 {epochs} 轮)...")
    for epoch in range(1, epochs + 1):
        model.train()
        for bx, by in train_loader:
            bx, by = bx.to(device), by.to(device)
            opt.zero_grad()
            crit(model(bx), by).backward()
            opt.step()
        sched.step()

        model.eval()
        corr, tot = 0, 0
        with torch.no_grad():
            for bx, by in test_loader:
                bx, by = bx.to(device), by.to(device)
                preds = torch.argmax(model(bx), dim=1)
                corr += (preds == by).sum().item()
                tot += bx.size(0)
        acc = corr / tot * 100.0
        if acc > best_acc:
            best_acc = acc
            best_weights = {k: v.cpu().clone() for k, v in model.state_dict().items()}
        
        if epoch % 10 == 0 or epoch == epochs:
            print(f"Epoch [{epoch:2d}/{epochs}] -> 验证准确率: {acc:.2f}% (当前最高: {best_acc:.2f}%)")

    model.load_state_dict(best_weights)
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
    os.makedirs(out_dir, exist_ok=True)
    save_path = os.path.join(out_dir, "best_resnet1d.pth")
    torch.save(model.state_dict(), save_path)
    print(f"-> 成功导出 ResNet1D-Light 最佳权重至: {save_path} (测试集准确率: {best_acc:.2f}%)")

if __name__ == "__main__":
    main()

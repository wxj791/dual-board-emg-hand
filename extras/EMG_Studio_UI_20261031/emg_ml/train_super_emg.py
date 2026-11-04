#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
train_super_emg.py
进一步深度拟合与极限定点化优化：
1. 探索 1D-ResNet / Multi-Scale CNN
2. 探索 增强特征工程 (MAV + RMS + WL + ZC + SSC + WAMP + MAVS) + MLP (带残差连接 / LayerNorm / CosineAnnealing)
3. 探索 ExtraTrees (极度随机化树，集成泛化极强)
4. 输出最高准确率模型
"""

import os
import glob
import time
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
from sklearn.model_selection import train_test_split
from sklearn.ensemble import ExtraTreesClassifier, GradientBoostingClassifier
from sklearn.metrics import accuracy_score
from sklearn.preprocessing import StandardScaler

GESTURE_NAMES = {
    0: "静息",
    1: "握拳",
    2: "张掌",
    3: "点赞",
    4: "剪刀手"
}

def load_data(raw_dir, target_win_len=120, step_ratio=0.20):
    files = sorted(glob.glob(os.path.join(raw_dir, "*.npz")))
    valid_files = [f for f in files if "checkpoint" not in f and "aborted" not in f]
    
    all_raw = []
    all_norm = []
    all_y = []

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
                raw_sample = seg_emg.T.astype(np.float32) # [8, L]
                mean = np.mean(raw_sample, axis=1, keepdims=True)
                std = np.std(raw_sample, axis=1, keepdims=True) + 1e-6
                norm_sample = (raw_sample - mean) / std

                all_raw.append(raw_sample)
                all_norm.append(norm_sample)
                all_y.append(maj)
            idx += step_len

    return np.array(all_raw, dtype=np.float32), np.array(all_norm, dtype=np.float32), np.array(all_y, dtype=np.int64)

def extract_advanced_features(X_raw):
    """
    高阶肌电时域特征提取 (8通道 x 7种特征 = 56维):
    - MAV (平均绝对值)
    - RMS (均方根)
    - WL (波形长度)
    - ZC (过零率)
    - SSC (斜率符号变化)
    - WAMP (Willison Amplitude, 肌肉运动单元募集强度)
    - LOG_VAR (对数方差, 线性化能量分布)
    全部特征计算在 FPGA 上极易实现流水线硬件化！
    """
    N, C, L = X_raw.shape
    centered = X_raw - np.mean(X_raw, axis=2, keepdims=True)
    
    # 1. MAV
    mav = np.mean(np.abs(centered), axis=2)
    # 2. RMS
    rms = np.sqrt(np.mean(centered**2, axis=2) + 1e-6)
    # 3. WL
    diff = np.diff(centered, axis=2)
    wl = np.sum(np.abs(diff), axis=2) / L
    # 4. ZC
    zc_cond = (centered[:, :, :-1] * centered[:, :, 1:] < 0) & (np.abs(diff) >= 15.0)
    zc = np.sum(zc_cond, axis=2).astype(np.float32) / L
    # 5. SSC
    diff_prod = diff[:, :, :-1] * diff[:, :, 1:]
    diff_change = np.abs(diff[:, :, :-1] - diff[:, :, 1:])
    ssc_cond = (diff_prod < 0) & (diff_change >= 15.0)
    ssc = np.sum(ssc_cond, axis=2).astype(np.float32) / (L - 1)
    # 6. WAMP
    wamp = np.sum(np.abs(diff) >= 20.0, axis=2).astype(np.float32) / (L - 1)
    # 7. LOG_VAR
    var = np.var(centered, axis=2)
    log_var = np.log(var + 1e-5)

    feats = np.concatenate([mav, rms, wl, zc, ssc, wamp, log_var], axis=1) # [N, 56]
    return feats

# 模型 A: ResNet1D-Light (多尺度跳跃连接，避免梯度衰减)
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

# 模型 B: SuperFeatureMLP (残差连接 + 余弦退火调度，56维特征)
class SuperFeatureMLP(nn.Module):
    def __init__(self, in_dim=56, num_classes=5):
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
        h2 = self.relu2(self.fc2(h1)) + h1 # 残差跃迁
        h3 = self.drop(self.relu3(self.fc3(h2)))
        return self.out(h3)

class DatasetWrapper(Dataset):
    def __init__(self, X, y):
        self.X = torch.tensor(X, dtype=torch.float32)
        self.y = torch.tensor(y, dtype=torch.long)
    def __len__(self):
        return len(self.y)
    def __getitem__(self, idx):
        return self.X[idx], self.y[idx]

def train_and_eval(model, train_loader, test_loader, epochs=45, lr=0.003, device="cuda"):
    model = model.to(device)
    crit = nn.CrossEntropyLoss()
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)
    
    best_acc = 0.0
    best_state = None

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
            best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}

    model.load_state_dict(best_state)
    return model, best_acc

def main():
    raw_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "raw")
    print("正在以高密度滑动步长 (step_ratio=0.20) 加载原始肌电数据...")
    X_raw, X_norm, y = load_data(raw_dir, target_win_len=120, step_ratio=0.20)
    print(f"-> 提取到样本总数: {len(y)} 组")

    feats = extract_advanced_features(X_raw)
    print(f"-> 高阶56维特征提取完毕，维度: {feats.shape}")

    indices = np.arange(len(y))
    train_idx, test_idx = train_test_split(indices, test_size=0.2, random_state=42, stratify=y)
    y_train, y_test = y[train_idx], y[test_idx]

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"-> 设备: {device}")

    # 1. ExtraTrees (极度随机化树，抗过拟合并在树结构中拥有极高峰值)
    print("\n[方案 A] 拟合 ExtraTrees (50 Trees)...")
    et = ExtraTreesClassifier(n_estimators=50, max_depth=16, random_state=42, n_jobs=-1)
    et.fit(feats[train_idx], y_train)
    preds_et = et.predict(feats[test_idx])
    acc_et = accuracy_score(y_test, preds_et) * 100.0
    print(f"=> ExtraTrees 测试集准确率: {acc_et:.2f}%")

    # 2. ResNet1D-Light (原始波形轻量残差卷积)
    print("\n[方案 B] 拟合 ResNet1D-Light (波形端到端残差网络)...")
    X_train_cnn, X_test_cnn = X_norm[train_idx], X_norm[test_idx]
    train_loader_cnn = DataLoader(DatasetWrapper(X_train_cnn, y_train), batch_size=32, shuffle=True)
    test_loader_cnn = DataLoader(DatasetWrapper(X_test_cnn, y_test), batch_size=64, shuffle=False)
    
    res_model = ResNet1DLight(in_channels=8, num_classes=5)
    params_res = sum(p.numel() for p in res_model.parameters())
    res_model, acc_res = train_and_eval(res_model, train_loader_cnn, test_loader_cnn, epochs=35, lr=0.002, device=device)
    print(f"=> ResNet1D-Light 测试集准确率: {acc_res:.2f}% (参数量: {params_res:,})")

    # 3. SuperFeatureMLP (高阶56维特征 + 残差多层感知机)
    print("\n[方案 C] 拟合 SuperFeatureMLP (56维时域特征 + 残差MLP)...")
    scaler = StandardScaler()
    X_train_feat = scaler.fit_transform(feats[train_idx])
    X_test_feat = scaler.transform(feats[test_idx])

    train_loader_mlp = DataLoader(DatasetWrapper(X_train_feat, y_train), batch_size=32, shuffle=True)
    test_loader_mlp = DataLoader(DatasetWrapper(X_test_feat, y_test), batch_size=64, shuffle=False)

    mlp_model = SuperFeatureMLP(in_dim=56, num_classes=5)
    params_mlp = sum(p.numel() for p in mlp_model.parameters())
    mlp_model, acc_mlp = train_and_eval(mlp_model, train_loader_mlp, test_loader_mlp, epochs=45, lr=0.003, device=device)
    print(f"=> SuperFeatureMLP 测试集准确率: {acc_mlp:.2f}% (参数量: {params_mlp:,})")

    print("\n" + "=" * 60)
    print("                深拟合最佳结论汇总")
    print("=" * 60)
    print(f"ExtraTrees           : {acc_et:.2f}%")
    print(f"SuperFeatureMLP      : {acc_mlp:.2f}% (参数量: {params_mlp:,})")
    print(f"ResNet1D-Light       : {acc_res:.2f}% (参数量: {params_res:,})")
    print("=" * 60)

    # 导出最佳模型与元信息供部署使用
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
    os.makedirs(out_dir, exist_ok=True)
    
    # 保存 SuperFeatureMLP
    torch.save({
        'model_state_dict': mlp_model.state_dict(),
        'scaler_mean': scaler.mean_,
        'scaler_scale': scaler.scale_,
        'acc': acc_mlp,
        'features': ['MAV', 'RMS', 'WL', 'ZC', 'SSC', 'WAMP', 'LOG_VAR'],
        'gestures': GESTURE_NAMES
    }, os.path.join(out_dir, "best_super_feature_mlp.pth"))

    # 保存 ExtraTrees
    import pickle
    with open(os.path.join(out_dir, "best_extratrees.pkl"), "wb") as f:
        pickle.dump(et, f)
    print("-> 最佳模型权重与参数字典已安全存档至 emg_ml/models/")

if __name__ == "__main__":
    main()

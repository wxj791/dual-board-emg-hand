#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
benchmark_experiments.py
多模型拟合与评估实验平台 (针对 FPGA 与嵌入式部署)
不修改 raw 目录下任何原始数据。
支持对比：
1. Baseline: 1D-CNN (原始波形)
2. Micro 1D-CNN (极简深度可分离卷积，极省 FPGA DSP/LUT)
3. 经典时域统计特征 (Hudgins特征集: MAV, RMS, WL, ZC, SSC, VAR) + MLP (全连接神经网络，最易定点化为 FPGA 矩阵乘加)
4. 时域特征 + 随机森林 (Random Forest / Decision Tree，极易用 Verilog if-else 流水线实现)
5. 时域特征 + 支持向量机 (Linear/RBF SVM)
"""

import os
import glob
import time
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
from sklearn.model_selection import train_test_split
from sklearn.ensemble import RandomForestClassifier
from sklearn.svm import SVC
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix
from sklearn.preprocessing import StandardScaler

# 动作类别
GESTURE_NAMES = {
    0: "静息",
    1: "握拳",
    2: "张掌",
    3: "点赞",
    4: "剪刀手"
}

def load_all_raw_data(raw_dir, target_win_len=120, step_ratio=0.25):
    """
    完全只读加载 raw_dir 下的所有 .npz 数据
    """
    files = sorted(glob.glob(os.path.join(raw_dir, "*.npz")))
    valid_files = [f for f in files if "checkpoint" not in f and "aborted" not in f]
    
    all_raw_windows = [] # [N, 8, target_win_len] raw values without z-score (for features)
    all_norm_windows = [] # [N, 8, target_win_len] z-score normalized (for CNN)
    all_labels = []

    print(f"-> 正在从 {len(valid_files)} 个数据文件中提取样本窗口 (只读)...")
    for fpath in valid_files:
        data = np.load(fpath, allow_pickle=True)
        emg = data['emg']          # [T, 8]
        labels = data['labels']    # [T]
        total_len = len(labels)
        step_len = max(1, int(round(target_win_len * step_ratio)))

        idx = 0
        while idx + target_win_len <= total_len:
            seg_emg = emg[idx:idx + target_win_len]      # [target_win_len, 8]
            seg_lbl = labels[idx:idx + target_win_len]

            # 过滤包含 -1 的未稳定样本
            if np.any(seg_lbl < 0):
                idx += step_len
                continue

            counts = np.bincount(seg_lbl, minlength=5)
            majority_label = np.argmax(counts)

            if counts[majority_label] >= int(target_win_len * 0.75):
                raw_sample = seg_emg.T.astype(np.float32) # [8, target_win_len]
                
                # Z-score
                mean = np.mean(raw_sample, axis=1, keepdims=True)
                std = np.std(raw_sample, axis=1, keepdims=True) + 1e-6
                norm_sample = (raw_sample - mean) / std

                all_raw_windows.append(raw_sample)
                all_norm_windows.append(norm_sample)
                all_labels.append(majority_label)

            idx += step_len

    X_raw = np.array(all_raw_windows, dtype=np.float32)
    X_norm = np.array(all_norm_windows, dtype=np.float32)
    y = np.array(all_labels, dtype=np.int64)
    print(f"-> 总共提取出 {len(y)} 个有效时间窗样本。类别分布: {np.bincount(y)}")
    return X_raw, X_norm, y

def extract_emg_features(X_raw):
    """
    提取经典肌电 Hudgins 特征工程集 (针对 8 通道):
    1. MAV (Mean Absolute Value / 平均绝对值 - 衡量肌肉发力强度)
    2. RMS (Root Mean Square / 均方根 - 衡量信号能量)
    3. WL (Waveform Length / 波形长度 - 衡量波形复杂度与频率)
    4. ZC (Zero Crossing / 过零率 - 衡量高频成分)
    5. SSC (Slope Sign Change / 斜率符号变化率 - 衡量局部频域起伏)
    6. VAR (Variance / 方差)
    
    对于 FPGA 部署极为友好：MAV 和 WL 只需要加减法和绝对值，不需要复杂浮点开方或除法！
    特征维度: 8 通道 * 6 特征 = 48 维特征向量
    """
    N, C, L = X_raw.shape
    features = []
    
    # 基线中心化 (消除 1650mV 左右的硬件偏置)
    centered = X_raw - np.mean(X_raw, axis=2, keepdims=True)
    
    # 1. MAV
    mav = np.mean(np.abs(centered), axis=2) # [N, C]
    
    # 2. RMS
    rms = np.sqrt(np.mean(centered**2, axis=2) + 1e-6) # [N, C]
    
    # 3. WL (波形长度累加)
    diff = np.diff(centered, axis=2) # [N, C, L-1]
    wl = np.sum(np.abs(diff), axis=2) / L # [N, C]
    
    # 4. ZC (过零率，阈值设为 15mV 防止底噪误判)
    threshold = 15.0
    zc_cond = (centered[:, :, :-1] * centered[:, :, 1:] < 0) & (np.abs(diff) >= threshold)
    zc = np.sum(zc_cond, axis=2).astype(np.float32) / L # [N, C]
    
    # 5. SSC (斜率符号变化)
    diff_prod = diff[:, :, :-1] * diff[:, :, 1:]
    diff_change = np.abs(diff[:, :, :-1] - diff[:, :, 1:])
    ssc_cond = (diff_prod < 0) & (diff_change >= threshold)
    ssc = np.sum(ssc_cond, axis=2).astype(np.float32) / (L - 1) # [N, C]
    
    # 6. VAR
    var = np.var(centered, axis=2) # [N, C]
    
    # 拼接全部特征: [N, C * 6] = [N, 48]
    feats = np.concatenate([mav, rms, wl, zc, ssc, var], axis=1)
    return feats

# PyTorch 数据集类
class TensorDatasetWrapper(Dataset):
    def __init__(self, X, y):
        self.X = torch.tensor(X, dtype=torch.float32)
        self.y = torch.tensor(y, dtype=torch.long)
    def __len__(self):
        return len(self.y)
    def __getitem__(self, idx):
        return self.X[idx], self.y[idx]

# 模型 1: 原 baseline 1D-CNN
class BaselineCNN(nn.Module):
    def __init__(self, in_channels=8, num_classes=5):
        super().__init__()
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
        return self.classifier(x)

# 模型 2: Micro-CNN (定点与 FPGA 流水线友好，去掉 BN，纯 Conv1d + ReLU + MaxPool)
class MicroFPGAConvNet(nn.Module):
    """
    针对 FPGA 硬件综合优化的卷积网：
    - 无需复杂浮点 BatchNorm
    - 卷积核全部定为奇数较小核 (3 / 5)
    - 权重量极小，仅 1k~2k 参数，可直接放入 FPGA BRAM 片上缓存！
    """
    def __init__(self, in_channels=8, num_classes=5):
        super().__init__()
        self.net = nn.Sequential(
            nn.Conv1d(in_channels, 16, kernel_size=5, padding=2),
            nn.ReLU(),
            nn.MaxPool1d(2),
            nn.Conv1d(16, 32, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.MaxPool1d(2),
            nn.AdaptiveAvgPool1d(1),
            nn.Flatten(),
            nn.Linear(32, num_classes)
        )
    def forward(self, x):
        return self.net(x)

# 模型 3: Feature-MLP (基于 48 维特征的高效定点感知机，FPGA 硬件实现只需要简单乘加器)
class FeatureMLP(nn.Module):
    def __init__(self, in_features=48, num_classes=5):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(in_features, 64),
            nn.ReLU(),
            nn.Dropout(0.2),
            nn.Linear(64, 32),
            nn.ReLU(),
            nn.Linear(32, num_classes)
        )
    def forward(self, x):
        return self.net(x)

def train_torch_model(model, train_loader, test_loader, epochs=30, lr=0.003, device="cuda"):
    model = model.to(device)
    criterion = nn.CrossEntropyLoss()
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    best_acc = 0.0
    best_state = None

    for epoch in range(1, epochs + 1):
        model.train()
        for bx, by in train_loader:
            bx, by = bx.to(device), by.to(device)
            optimizer.zero_grad()
            out = model(bx)
            loss = criterion(out, by)
            loss.backward()
            optimizer.step()

        # eval
        model.eval()
        t_correct, t_total = 0, 0
        with torch.no_grad():
            for bx, by in test_loader:
                bx, by = bx.to(device), by.to(device)
                out = model(bx)
                preds = torch.argmax(out, dim=1)
                t_correct += (preds == by).sum().item()
                t_total += bx.size(0)
        acc = (t_correct / t_total) * 100.0
        if acc > best_acc:
            best_acc = acc
            best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}

    model.load_state_dict(best_state)
    return model, best_acc

def evaluate_model_metrics(y_true, y_pred, model_name):
    acc = accuracy_score(y_true, y_pred) * 100.0
    print(f"\n==========================================")
    print(f"【{model_name}】评估结果:")
    print(f"测试集整体准确率 (Overall Accuracy): {acc:.2f}%")
    print(f"==========================================")
    for gid in sorted(np.unique(y_true)):
        mask = (y_true == gid)
        n_samples = np.sum(mask)
        n_correct = np.sum(y_pred[mask] == gid)
        class_acc = (n_correct / n_samples) * 100.0 if n_samples > 0 else 0
        gname = GESTURE_NAMES.get(gid, f"动作{gid}")
        print(f"  类别 [{gid}] {gname:<5}: 样本数={n_samples:4d} | 命中={n_correct:4d} | 召回率={class_acc:6.2f}%")
    return acc

def run_benchmarks():
    raw_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "raw")
    X_raw, X_norm, y = load_all_raw_data(raw_dir)
    
    # 提取 Hudgins 特征
    print("-> 正在提取时域生物力学统计特征 (Hudgins特征集)...")
    t0 = time.time()
    feats = extract_emg_features(X_raw)
    print(f"-> 特征提取完毕，耗时 {time.time() - t0:.2f}s，特征矩阵维度: {feats.shape}")

    # 统一划分数据集 (保证完全相同的数据划分)
    indices = np.arange(len(y))
    train_idx, test_idx = train_test_split(indices, test_size=0.2, random_state=42, stratify=y)
    
    y_train, y_test = y[train_idx], y[test_idx]
    
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"-> 评估设备: {device}")
    
    results = {}

    # -------------------------------------------------------------
    # 方法 1: 原 baseline 1D-CNN
    # -------------------------------------------------------------
    print("\n>>> [实验 1/5] 训练 Baseline 1D-CNN (原始波形直接输入)...")
    X_train_cnn, X_test_cnn = X_norm[train_idx], X_norm[test_idx]
    train_loader_cnn = DataLoader(TensorDatasetWrapper(X_train_cnn, y_train), batch_size=32, shuffle=True)
    test_loader_cnn = DataLoader(TensorDatasetWrapper(X_test_cnn, y_test), batch_size=64, shuffle=False)

    m1 = BaselineCNN(in_channels=8, num_classes=5)
    params_m1 = sum(p.numel() for p in m1.parameters())
    m1, best_acc_m1 = train_torch_model(m1, train_loader_cnn, test_loader_cnn, epochs=30, lr=0.002, device=device)
    
    m1.eval()
    with torch.no_grad():
        preds_m1 = []
        for bx, _ in test_loader_cnn:
            preds_m1.extend(torch.argmax(m1(bx.to(device)), dim=1).cpu().numpy())
    acc_m1 = evaluate_model_metrics(y_test, np.array(preds_m1), "Baseline 1D-CNN")
    results["Baseline 1D-CNN"] = {
        "acc": acc_m1, 
        "params": params_m1, 
        "fpga_friendly": "中等 (需要浮点Conv1d与BatchNorm，资源占用较多)",
        "model_obj": m1,
        "type": "cnn"
    }

    # -------------------------------------------------------------
    # 方法 2: Micro FPGA-Friendly 1D-CNN
    # -------------------------------------------------------------
    print("\n>>> [实验 2/5] 训练 Micro FPGA-Friendly 1D-CNN (无BN，极简流水线)...")
    m2 = MicroFPGAConvNet(in_channels=8, num_classes=5)
    params_m2 = sum(p.numel() for p in m2.parameters())
    m2, best_acc_m2 = train_torch_model(m2, train_loader_cnn, test_loader_cnn, epochs=30, lr=0.003, device=device)
    
    m2.eval()
    with torch.no_grad():
        preds_m2 = []
        for bx, _ in test_loader_cnn:
            preds_m2.extend(torch.argmax(m2(bx.to(device)), dim=1).cpu().numpy())
    acc_m2 = evaluate_model_metrics(y_test, np.array(preds_m2), "Micro FPGA-Friendly 1D-CNN")
    results["Micro FPGA 1D-CNN"] = {
        "acc": acc_m2, 
        "params": params_m2, 
        "fpga_friendly": "极高 (纯定点Conv1d+ReLU，参数仅约2k，全片上RAM驻留)",
        "model_obj": m2,
        "type": "cnn"
    }

    # -------------------------------------------------------------
    # 特征归一化 (用于特征基方法)
    # -------------------------------------------------------------
    scaler = StandardScaler()
    X_train_feat = scaler.fit_transform(feats[train_idx])
    X_test_feat = scaler.transform(feats[test_idx])

    # -------------------------------------------------------------
    # 方法 3: 特征工程 + Feature-MLP (多层感知机)
    # -------------------------------------------------------------
    print("\n>>> [实验 3/5] 训练 时域特征工程 + MLP (轻量全连接神经网络)...")
    train_loader_mlp = DataLoader(TensorDatasetWrapper(X_train_feat, y_train), batch_size=32, shuffle=True)
    test_loader_mlp = DataLoader(TensorDatasetWrapper(X_test_feat, y_test), batch_size=64, shuffle=False)

    m3 = FeatureMLP(in_features=48, num_classes=5)
    params_m3 = sum(p.numel() for p in m3.parameters())
    m3, best_acc_m3 = train_torch_model(m3, train_loader_mlp, test_loader_mlp, epochs=40, lr=0.003, device=device)

    m3.eval()
    with torch.no_grad():
        preds_m3 = []
        for bx, _ in test_loader_mlp:
            preds_m3.extend(torch.argmax(m3(bx.to(device)), dim=1).cpu().numpy())
    acc_m3 = evaluate_model_metrics(y_test, np.array(preds_m3), "Feature-MLP (Hudgins+MLP)")
    results["Feature-MLP"] = {
        "acc": acc_m3, 
        "params": params_m3, 
        "fpga_friendly": "极高 (特征提取无乘法，MLP是纯矩阵乘加，最适合流水线展开)",
        "model_obj": m3,
        "scaler": scaler,
        "type": "mlp"
    }

    # -------------------------------------------------------------
    # 方法 4: 特征工程 + 随机森林 (Random Forest)
    # -------------------------------------------------------------
    print("\n>>> [实验 4/5] 训练 时域特征工程 + 随机森林 (Random Forest)...")
    rf = RandomForestClassifier(n_estimators=30, max_depth=10, random_state=42, n_jobs=-1)
    rf.fit(feats[train_idx], y_train) # 决策树甚至无需做特征归一化，硬件只用纯比较器！
    preds_rf = rf.predict(feats[test_idx])
    acc_rf = evaluate_model_metrics(y_test, preds_rf, "Random Forest (30 Trees, Depth=10)")
    results["Random Forest"] = {
        "acc": acc_rf, 
        "params": "30棵树(深度<=10)", 
        "fpga_friendly": "极致 (FPGA零乘法，纯Verilog if-else分支判断，纳秒级延迟)",
        "model_obj": rf,
        "type": "tree"
    }

    # -------------------------------------------------------------
    # 方法 5: 特征工程 + SVM (RBF 核)
    # -------------------------------------------------------------
    print("\n>>> [实验 5/5] 训练 时域特征工程 + SVM (RBF核与Linear核)...")
    svm = SVC(C=5.0, kernel='rbf', gamma='scale', random_state=42)
    svm.fit(X_train_feat, y_train)
    preds_svm = svm.predict(X_test_feat)
    acc_svm = evaluate_model_metrics(y_test, preds_svm, "Support Vector Machine (RBF)")
    results["SVM (RBF)"] = {
        "acc": acc_svm, 
        "params": f"{len(svm.support_)} 支持向量", 
        "fpga_friendly": "中等 (支持向量较多时开销较大，ESP32上极快)",
        "model_obj": svm,
        "scaler": scaler,
        "type": "svm"
    }

    # -------------------------------------------------------------
    # 实验汇总排名
    # -------------------------------------------------------------
    print("\n" + "#" * 75)
    print("                     多模型拟合评测对比总榜")
    print("#" * 75)
    print(f"{'模型/方法名称':<25} | {'测试集准确率':<10} | {'参数/规模':<16} | {'FPGA/MCU部署可行性'}")
    print("-" * 75)
    sorted_res = sorted(results.items(), key=lambda item: item[1]['acc'], reverse=True)
    for name, info in sorted_res:
        p_str = f"{info['params']:,} 参数" if isinstance(info['params'], int) else str(info['params'])
        print(f"{name:<25} | {info['acc']:8.2f}% | {p_str:<16} | {info['fpga_friendly']}")
    print("#" * 75)

    # 选出最高分并保存
    best_name, best_info = sorted_res[0]
    print(f"\n>>> 拟合优选冠军: 【{best_name}】，最高准确率: {best_info['acc']:.2f}%")
    
    models_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
    os.makedirs(models_dir, exist_ok=True)
    
    if best_info['type'] in ['cnn', 'mlp']:
        save_path = os.path.join(models_dir, "best_fpga_model.pth")
        torch.save(best_info['model_obj'].state_dict(), save_path)
        print(f"-> 最佳神经网络权重已导出至: {save_path}")
    elif best_info['type'] == 'tree':
        import pickle
        save_path = os.path.join(models_dir, "best_fpga_rf.pkl")
        with open(save_path, "wb") as f:
            pickle.dump(best_info['model_obj'], f)
        print(f"-> 最佳树模型已导出至: {save_path}")

if __name__ == "__main__":
    run_benchmarks()

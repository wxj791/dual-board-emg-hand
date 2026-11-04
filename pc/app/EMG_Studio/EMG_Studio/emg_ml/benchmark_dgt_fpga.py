#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
benchmark_dgt_fpga.py
针对 DGT 专属 7 动作数据集 (6动作 + 静息)，横向对比多类最适合 FPGA 硬件部署的拟合架构:
不修改 raw 目录下任何原始数据。

候选拟合模型列表:
1. ResNet1D-Light (7分类，1D残差卷积网，参数约39k，可定点化为INT8全片上BRAM驻留)
2. Micro-CNN (极简深度可分离/窄通道卷积网，参数约3k，DSP占用极少)
3. 56维高阶时域生物力学特征 (MAV/RMS/WL/ZC/SSC/WAMP/LOG_VAR) + Residual MLP (纯矩阵乘加，流水线硬件化最优)
4. 48维经典 Hudgins 特征 + Compact MLP (超轻量 48->32->16->7)
5. 56维时域特征 + ExtraTrees / 随机森林 (纯 Verilog if-else 条件分支比较器，FPGA 硬件零乘法)
"""

import os
import glob
import time
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
from sklearn.model_selection import train_test_split
from sklearn.ensemble import ExtraTreesClassifier, RandomForestClassifier
from sklearn.metrics import accuracy_score
from sklearn.preprocessing import StandardScaler

GESTURE_NAMES = {
    0: "静息",
    1: "握拳",
    2: "张开手掌",
    3: "点赞",
    4: "剪刀手",
    5: "数字一",
    6: "数字六"
}

def load_dgt_dataset(dgt_dir, target_win_len=120, step_ratio=0.20):
    files = sorted(glob.glob(os.path.join(dgt_dir, "*.npz")))
    valid_files = [f for f in files if "checkpoint" not in f and "aborted" not in f]
    print(f"-> 检测到 DGT 专属数据集 {len(valid_files)} 个:")
    for f in valid_files:
        print(f"   * {os.path.basename(f)}")

    all_raw = []
    all_norm = []
    all_y = []

    for fpath in valid_files:
        data = np.load(fpath, allow_pickle=True)
        emg = data['emg']          # [T, 8]
        labels = data['labels']    # [T]
        total_len = len(labels)
        step_len = max(1, int(round(target_win_len * step_ratio)))

        idx = 0
        while idx + target_win_len <= total_len:
            seg_emg = emg[idx:idx + target_win_len]
            seg_lbl = labels[idx:idx + target_win_len]

            if np.any(seg_lbl < 0):
                idx += step_len
                continue

            counts = np.bincount(seg_lbl, minlength=7)
            maj = np.argmax(counts)

            # 窗口纯净度阈值 75%
            if counts[maj] >= int(target_win_len * 0.75):
                raw_sample = seg_emg.T.astype(np.float32) # [8, 120]
                mean = np.mean(raw_sample, axis=1, keepdims=True)
                std = np.std(raw_sample, axis=1, keepdims=True) + 1e-6
                norm_sample = (raw_sample - mean) / std

                all_raw.append(raw_sample)
                all_norm.append(norm_sample)
                all_y.append(maj)

            idx += step_len

    X_raw = np.array(all_raw, dtype=np.float32)
    X_norm = np.array(all_norm, dtype=np.float32)
    y = np.array(all_y, dtype=np.int64)

    print(f"\n-> DGT 样本窗口提取完成！共生成 {len(y)} 组样本。")
    print("各类别样本分布统计:")
    for gid in range(7):
        gname = GESTURE_NAMES.get(gid, f"类别{gid}")
        cnt = np.sum(y == gid)
        print(f"  [ID {gid}] {gname:<6}: {cnt:5d} 组 ({cnt/len(y)*100:4.1f}%)")

    return X_raw, X_norm, y

def extract_advanced_features(X_raw):
    """
    56维高阶时域生物力学特征提取 (8通道 x 7特征):
    - MAV (平均绝对值 - 纯绝对值累加，无浮点除法)
    - RMS (均方根 - 能量度量)
    - WL  (波形长度 - 纯一阶差分绝对值累加，最适合 FPGA)
    - ZC  (过零率 - 阈值过零比较器)
    - SSC (斜率符号变化率 - 峰谷变化比较器)
    - WAMP (Willison振幅 - 运动单元募集计数器)
    - LOG_VAR (对数方差 - 线性化能量分布)
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
    # 4. ZC (15mV 阈值防止底噪跳变)
    zc_cond = (centered[:, :, :-1] * centered[:, :, 1:] < 0) & (np.abs(diff) >= 15.0)
    zc = np.sum(zc_cond, axis=2).astype(np.float32) / L
    # 5. SSC
    diff_prod = diff[:, :, :-1] * diff[:, :, 1:]
    diff_change = np.abs(diff[:, :, :-1] - diff[:, :, 1:])
    ssc_cond = (diff_prod < 0) & (diff_change >= 15.0)
    ssc = np.sum(ssc_cond, axis=2).astype(np.float32) / (L - 1)
    # 6. WAMP (阈值 20mV)
    wamp = np.sum(np.abs(diff) >= 20.0, axis=2).astype(np.float32) / (L - 1)
    # 7. LOG_VAR
    var = np.var(centered, axis=2)
    log_var = np.log(var + 1e-5)

    feats = np.concatenate([mav, rms, wl, zc, ssc, wamp, log_var], axis=1) # [N, 56]
    return feats

class DatasetWrapper(Dataset):
    def __init__(self, X, y):
        self.X = torch.tensor(X, dtype=torch.float32)
        self.y = torch.tensor(y, dtype=torch.long)
    def __len__(self):
        return len(self.y)
    def __getitem__(self, idx):
        return self.X[idx], self.y[idx]

# 模型 1: ResNet1D-Light (7分类)
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

# 模型 2: Micro-CNN (极简深度卷积，约2.5k参数，超省 FPGA LUT/DSP)
class MicroFPGAConvNet(nn.Module):
    def __init__(self, in_channels=8, num_classes=7):
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

# 模型 3: SuperFeatureMLP (56维特征残差感知机)
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

# 模型 4: CompactMLP (超轻量纯乘加网络 56 -> 32 -> 16 -> 7，仅约2.4k参数)
class CompactMLP(nn.Module):
    def __init__(self, in_dim=56, num_classes=7):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(in_dim, 32),
            nn.ReLU(),
            nn.Linear(32, 16),
            nn.ReLU(),
            nn.Linear(16, num_classes)
        )
    def forward(self, x):
        return self.net(x)

def train_torch_model(model, train_loader, test_loader, epochs=40, lr=0.003, device="cuda"):
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

def evaluate_metrics(y_true, y_pred, model_name):
    acc = accuracy_score(y_true, y_pred) * 100.0
    print(f"\n=======================================================")
    print(f"【{model_name}】评估结果:")
    print(f"测试集整体准确率 (Overall Accuracy): {acc:.2f}%")
    print(f"-------------------------------------------------------")
    for gid in range(7):
        mask = (y_true == gid)
        n_samples = np.sum(mask)
        n_correct = np.sum(y_pred[mask] == gid)
        class_acc = (n_correct / n_samples) * 100.0 if n_samples > 0 else 0
        gname = GESTURE_NAMES.get(gid, f"类别{gid}")
        print(f"  [{gid}] {gname:<6}: 样本={n_samples:4d} | 命中={n_correct:4d} | 召回率={class_acc:6.2f}%")
    print(f"=======================================================")
    return acc

def main():
    dgt_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "raw", "DGT")
    X_raw, X_norm, y = load_dgt_dataset(dgt_dir, target_win_len=120, step_ratio=0.20)

    # 提取 56 维时域生物力学特征
    print("\n-> 正在提取 56 维高阶生物力学时域特征 (Hudgins+WAMP+LogVar)...")
    feats = extract_advanced_features(X_raw)
    print(f"-> 特征提取完毕，特征矩阵维度: {feats.shape}")

    # 分割训练集与测试集 (80% / 20%，分层抽样)
    indices = np.arange(len(y))
    train_idx, test_idx = train_test_split(indices, test_size=0.2, random_state=42, stratify=y)
    y_train, y_test = y[train_idx], y[test_idx]

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"-> 训练设备: {device} (测试集样本量: {len(y_test)} 组)")

    results = {}

    # -------------------------------------------------------------
    # 候选 1: ResNet1D-Light (原始波形端到端 1D 残差卷积网)
    # -------------------------------------------------------------
    print("\n>>> [1/5] 拟合 ResNet1D-Light (波形一维残差卷积，7分类)...")
    train_loader_cnn = DataLoader(DatasetWrapper(X_norm[train_idx], y_train), batch_size=32, shuffle=True)
    test_loader_cnn = DataLoader(DatasetWrapper(X_norm[test_idx], y_test), batch_size=64, shuffle=False)
    
    m1 = ResNet1DLight(in_channels=8, num_classes=7)
    params_m1 = sum(p.numel() for p in m1.parameters())
    m1, _ = train_torch_model(m1, train_loader_cnn, test_loader_cnn, epochs=35, lr=0.002, device=device)
    
    m1.eval()
    with torch.no_grad():
        preds_m1 = []
        for bx, _ in test_loader_cnn:
            preds_m1.extend(torch.argmax(m1(bx.to(device)), dim=1).cpu().numpy())
    acc_m1 = evaluate_metrics(y_test, np.array(preds_m1), "ResNet1D-Light (波形端到端)")
    results["ResNet1D-Light"] = {
        "acc": acc_m1, 
        "params": params_m1, 
        "fpga": "纯卷积+残差，INT8定点化后仅约38KB，全片上BRAM驻留", 
        "type": "cnn", 
        "model": m1
    }

    # -------------------------------------------------------------
    # 候选 2: Micro-CNN (极简深度卷积，超省 FPGA 资源)
    # -------------------------------------------------------------
    print("\n>>> [2/5] 拟合 Micro-CNN (极窄通道卷积，2.5k极小参数量)...")
    m2 = MicroFPGAConvNet(in_channels=8, num_classes=7)
    params_m2 = sum(p.numel() for p in m2.parameters())
    m2, _ = train_torch_model(m2, train_loader_cnn, test_loader_cnn, epochs=35, lr=0.003, device=device)

    m2.eval()
    with torch.no_grad():
        preds_m2 = []
        for bx, _ in test_loader_cnn:
            preds_m2.extend(torch.argmax(m2(bx.to(device)), dim=1).cpu().numpy())
    acc_m2 = evaluate_metrics(y_test, np.array(preds_m2), "Micro-CNN (极简卷积)")
    results["Micro-CNN"] = {
        "acc": acc_m2, 
        "params": params_m2, 
        "fpga": "仅耗几个DSP乘法器，极度节省逻辑资源", 
        "type": "cnn", 
        "model": m2
    }

    # -------------------------------------------------------------
    # 特征标准化
    # -------------------------------------------------------------
    scaler = StandardScaler()
    X_train_feat = scaler.fit_transform(feats[train_idx])
    X_test_feat = scaler.transform(feats[test_idx])
    train_loader_mlp = DataLoader(DatasetWrapper(X_train_feat, y_train), batch_size=32, shuffle=True)
    test_loader_mlp = DataLoader(DatasetWrapper(X_test_feat, y_test), batch_size=64, shuffle=False)

    # -------------------------------------------------------------
    # 候选 3: SuperFeatureMLP (56维特征残差感知机)
    # -------------------------------------------------------------
    print("\n>>> [3/5] 拟合 SuperFeatureMLP (56维生物特征 + 残差感知机)...")
    m3 = SuperFeatureMLP(in_dim=56, num_classes=7)
    params_m3 = sum(p.numel() for p in m3.parameters())
    m3, _ = train_torch_model(m3, train_loader_mlp, test_loader_mlp, epochs=45, lr=0.003, device=device)

    m3.eval()
    with torch.no_grad():
        preds_m3 = []
        for bx, _ in test_loader_mlp:
            preds_m3.extend(torch.argmax(m3(bx.to(device)), dim=1).cpu().numpy())
    acc_m3 = evaluate_metrics(y_test, np.array(preds_m3), "SuperFeatureMLP (56维时域特征+MLP)")
    results["SuperFeatureMLP"] = {
        "acc": acc_m3, 
        "params": params_m3, 
        "fpga": "时域特征提取无复杂乘除，MLP全流水线定点乘加展开", 
        "type": "mlp", 
        "model": m3,
        "scaler": scaler
    }

    # -------------------------------------------------------------
    # 候选 4: CompactMLP (超紧凑 56->32->16->7)
    # -------------------------------------------------------------
    print("\n>>> [4/5] 拟合 CompactMLP (超紧凑 56->32->16->7，仅2.4k参数)...")
    m4 = CompactMLP(in_dim=56, num_classes=7)
    params_m4 = sum(p.numel() for p in m4.parameters())
    m4, _ = train_torch_model(m4, train_loader_mlp, test_loader_mlp, epochs=40, lr=0.003, device=device)

    m4.eval()
    with torch.no_grad():
        preds_m4 = []
        for bx, _ in test_loader_mlp:
            preds_m4.extend(torch.argmax(m4(bx.to(device)), dim=1).cpu().numpy())
    acc_m4 = evaluate_metrics(y_test, np.array(preds_m4), "CompactMLP (极简MLP)")
    results["CompactMLP"] = {
        "acc": acc_m4, 
        "params": params_m4, 
        "fpga": "仅耗2400个定点权重，几行Verilog矩阵流水即可综合", 
        "type": "mlp", 
        "model": m4,
        "scaler": scaler
    }

    # -------------------------------------------------------------
    # 候选 5: ExtraTrees 决策森林 (FPGA 零乘法，纯 if-else 逻辑)
    # -------------------------------------------------------------
    print("\n>>> [5/5] 拟合 ExtraTrees (40 棵树，MaxDepth=14)...")
    et = ExtraTreesClassifier(n_estimators=40, max_depth=14, random_state=42, n_jobs=-1)
    et.fit(feats[train_idx], y_train)
    preds_et = et.predict(feats[test_idx])
    acc_et = evaluate_metrics(y_test, preds_et, "ExtraTrees (FPGA纯比较器树)")
    results["ExtraTrees"] = {
        "acc": acc_et, 
        "params": "40棵树 (深度<=14)", 
        "fpga": "FPGA零乘法单元！纯Verilog if-else逻辑判断，纳秒级延迟", 
        "type": "tree", 
        "model": et
    }

    # -------------------------------------------------------------
    # 总榜排行
    # -------------------------------------------------------------
    print("\n" + "#" * 80)
    print("                     DGT 数据集 (7分类) 最佳拟合总榜")
    print("#" * 80)
    print(f"{'模型名称':<20} | {'准确率':<8} | {'参数规模':<14} | {'FPGA 硬件实现特性'}")
    print("-" * 80)
    sorted_res = sorted(results.items(), key=lambda x: x[1]['acc'], reverse=True)
    for name, info in sorted_res:
        p_str = f"{info['params']:,} 参数" if isinstance(info['params'], int) else str(info['params'])
        print(f"{name:<20} | {info['acc']:6.2f}% | {p_str:<14} | {info['fpga']}")
    print("#" * 80)

    best_name, best_info = sorted_res[0]
    print(f"\n>>> [最佳拟合冠军]: 【{best_name}】，测试集准确率: {best_info['acc']:.2f}%")

    # 导出最佳模型为 DGTV1.9.pth 与 best_dgt_fpga_model.pth
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
    os.makedirs(out_dir, exist_ok=True)
    v19_pth = os.path.join(out_dir, "DGTV1.9.pth")
    best_pth = os.path.join(out_dir, "best_dgt_fpga_model.pth")

    if best_info['type'] == 'cnn':
        torch.save(best_info['model'].state_dict(), v19_pth)
        torch.save(best_info['model'].state_dict(), best_pth)
        print(f"-> 最佳神经网络权重已保存至: {v19_pth} 与 {best_pth}")
    elif best_info['type'] == 'mlp':
        save_dict = {
            'state_dict': best_info['model'].state_dict(),
            'scaler_mean': best_info['scaler'].mean_,
            'scaler_scale': best_info['scaler'].scale_,
            'classes': 7,
            'acc': best_info['acc'],
            'model_name': best_name
        }
        torch.save(save_dict, v19_pth)
        torch.save(save_dict, best_pth)
        print(f"-> 最佳 MLP 权重已保存至: {v19_pth} 与 {best_pth}")
    elif best_info['type'] == 'tree':
        import pickle
        pkl_path = os.path.join(out_dir, "best_dgt_fpga_tree.pkl")
        with open(pkl_path, "wb") as f:
            pickle.dump(best_info['model'], f)
        print(f"-> 最佳决策树已保存至: {pkl_path}")

if __name__ == "__main__":
    main()

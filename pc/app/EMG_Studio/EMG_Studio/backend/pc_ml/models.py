from __future__ import annotations

import torch
from torch import nn


class CBR(nn.Module):
    def __init__(self, cin, cout, kernel, dilation=1, groups=1):
        super().__init__()
        self.conv = nn.Conv1d(cin, cout, kernel, padding=dilation*(kernel//2),
                              dilation=dilation, groups=groups, bias=False)
        self.bn = nn.BatchNorm1d(cout)
        self.relu = nn.ReLU()

    def forward(self, x):
        return self.relu(self.bn(self.conv(x)))


class SequentialCNN(nn.Module):
    def __init__(self, num_classes=7):
        super().__init__()
        self.stem = CBR(8, 16, 5)
        self.conv2 = CBR(16, 24, 3)
        self.conv3 = CBR(24, 32, 3)
        self.fc = nn.Linear(32, num_classes)

    def features(self, x):
        x = torch.nn.functional.max_pool1d(self.stem(x), 2)
        x = torch.nn.functional.max_pool1d(self.conv2(x), 2)
        return self.conv3(x).mean(-1)

    def forward(self, x):
        return self.fc(self.features(x))


class MultiScaleCNN(nn.Module):
    """Parallel k3/5/7 temporal paths plus a cheap amplitude bypass.

    No attention softmax, recurrent state, FFT, online training, or division
    other than power-of-two pooling is required by the inference datapath.
    """
    def __init__(self, num_classes=7):
        super().__init__()
        self.stem = CBR(8, 12, 3)
        self.branches = nn.ModuleList([CBR(12, 8, k) for k in (3, 5, 7)])
        self.fusion = CBR(24, 24, 1)
        self.temporal = CBR(24, 24, 5, dilation=2, groups=24)
        self.fc = nn.Linear(32, num_classes)

    def features(self, x):
        envelope = x.abs().mean(-1)
        stem = torch.nn.functional.max_pool1d(self.stem(x), 2)
        branches = [torch.nn.functional.max_pool1d(branch(stem), 2) for branch in self.branches]
        temporal = self.temporal(self.fusion(torch.cat(branches, dim=1))).mean(-1)
        return torch.cat((temporal, envelope), dim=1)

    def forward(self, x):
        return self.fc(self.features(x))


def make_model(kind, num_classes=7):
    if kind == 'sequential':
        return SequentialCNN(num_classes)
    if kind == 'multiscale':
        return MultiScaleCNN(num_classes)
    raise ValueError(f'Unknown network topology: {kind}')


def profile(model):
    rows = []
    hooks = []
    def capture(name):
        def hook(module, inputs, output):
            count = output.numel()*(module.in_channels//module.groups*module.kernel_size[0]
                                     if isinstance(module, nn.Conv1d) else module.in_features)
            rows.append({'name': name, 'output': list(output.shape), 'macs': count})
        return hook
    for name, mod in model.named_modules():
        if isinstance(mod, (nn.Conv1d, nn.Linear)):
            hooks.append(mod.register_forward_hook(capture(name)))
    old = model.training
    model.eval()
    with torch.no_grad():
        model(torch.zeros(1, 8, 128))
    model.train(old)
    for h in hooks:
        h.remove()
    return {'parameters_training_bn': sum(p.numel() for p in model.parameters()),
            'macs': sum(r['macs'] for r in rows), 'layers': rows}

"""Untrained EMG CNN candidates for architecture comparisons, not deployment.

These are small adaptations of architectural ideas, not paper reproductions.
Every model consumes a window ending at the decision time; only CausalTCN also
guarantees that its internal feature at t never uses input after t.
"""
import torch
from torch import nn
from torch.nn import functional as F


def cbr(cin, cout, kernel):
    return nn.Sequential(nn.Conv1d(cin, cout, kernel, padding=kernel//2, bias=False),
                         nn.BatchNorm1d(cout), nn.ReLU())


def statistics(x):
    """Mean and variance; keeps amplitude distribution lost by mean alone."""
    return torch.cat((x.mean(-1), x.var(-1, unbiased=False)), dim=1)


class SequentialCNN(nn.Module):
    def __init__(self, head='gap'):
        super().__init__()
        self.blocks = nn.Sequential(cbr(8, 16, 5), nn.MaxPool1d(2),
                                    cbr(16, 24, 3), nn.MaxPool1d(2), cbr(24, 32, 3))
        self.head = head
        # SPP preserves four temporal regions without a fully flattened head.
        self.fc = nn.Linear(128 if head == 'spp' else 64 if head == 'stats' else 32, 9)

    def forward(self, x):
        x = self.blocks(x)
        if self.head == 'spp':
            x = F.adaptive_avg_pool1d(x, 4).flatten(1)
        else:
            x = statistics(x) if self.head == 'stats' else x.mean(-1)
        return self.fc(x)


class MultiScaleCNN(nn.Module):
    def __init__(self):
        super().__init__()
        self.stem = cbr(8, 12, 1)
        self.branches = nn.ModuleList([cbr(12, 8, k) for k in (3, 7, 15)])
        self.fusion = nn.Sequential(cbr(24, 24, 1), nn.MaxPool1d(2),
                                    cbr(24, 32, 5), nn.MaxPool1d(2))
        self.fc = nn.Linear(32, 9)

    def forward(self, x):
        x = self.stem(x)
        x = self.fusion(torch.cat([branch(x) for branch in self.branches], dim=1))
        return self.fc(x.mean(-1))


class TemporalSpatialCNN(nn.Module):
    """Temporal filtering per electrode, followed by learned channel mixing."""
    def __init__(self):
        super().__init__()
        self.temporal = nn.Conv1d(8, 32, 15, padding=7, groups=8, bias=False)
        self.spatial = cbr(32, 24, 1)
        self.last = nn.Sequential(nn.MaxPool1d(2), cbr(24, 32, 5), nn.MaxPool1d(2))
        self.fc = nn.Linear(64, 9)

    def forward(self, x):
        x = self.last(self.spatial(self.temporal(x)))
        return self.fc(statistics(x))


class ChannelAttentionCNN(nn.Module):
    def __init__(self):
        super().__init__()
        self.first = nn.Sequential(cbr(8, 16, 5), nn.MaxPool1d(2))
        self.gate = nn.Sequential(nn.Linear(16, 4), nn.ReLU(), nn.Linear(4, 16), nn.Sigmoid())
        self.last = nn.Sequential(cbr(16, 24, 3), nn.MaxPool1d(2), cbr(24, 32, 3))
        self.fc = nn.Linear(32, 9)

    def forward(self, x):
        x = self.first(x)
        x = x * self.gate(x.mean(-1)).unsqueeze(-1)
        return self.fc(self.last(x).mean(-1))


class CausalConv(nn.Module):
    def __init__(self, channels, dilation):
        super().__init__()
        self.left = 2*dilation
        self.conv = nn.Conv1d(channels, channels, 3, dilation=dilation)

    def forward(self, x):
        return self.conv(F.pad(x, (self.left, 0)))


class CausalBlock(nn.Module):
    def __init__(self, channels, dilation):
        super().__init__()
        self.layers = nn.Sequential(CausalConv(channels, dilation), nn.ReLU(),
                                    CausalConv(channels, dilation))

    def forward(self, x):
        return F.relu(x+self.layers(x))


class CausalTCN(nn.Module):
    def __init__(self):
        super().__init__()
        self.stem = nn.Conv1d(8, 16, 1)
        self.blocks = nn.Sequential(*[CausalBlock(16, d) for d in (1, 2, 4, 8, 16, 32)])
        self.fc = nn.Linear(16, 9)

    def encode(self, x):
        return self.blocks(self.stem(x))

    def forward(self, x):
        return self.fc(self.encode(x)[..., -1])


class RingSpatialCNN(nn.Module):
    """Electrode-neighbor prior; requires validated physical channel order."""
    def __init__(self):
        super().__init__()
        self.temporal = nn.Conv2d(1, 8, (1, 9), padding=(0, 4))
        self.ring = nn.Conv2d(8, 8, (3, 1))
        self.last = nn.Conv2d(8, 8, (1, 5), padding=(0, 2))
        # Preserve electrode positions for classification; no rotation invariance claim.
        self.fc = nn.Linear(128, 9)

    def encode(self, x):
        x = F.avg_pool2d(F.relu(self.temporal(x.unsqueeze(1))), (1, 2))
        x = F.relu(self.ring(F.pad(x, (0, 0, 1, 1), mode='circular')))
        return F.avg_pool2d(F.relu(self.last(x)), (1, 2))

    def forward(self, x):
        x = self.encode(x)
        return self.fc(torch.cat((x.mean(-1), x.var(-1, unbiased=False)), dim=1).flatten(1))


def folded(conv, bn):
    scale = bn.weight / torch.sqrt(bn.running_var+bn.eps)
    bias = conv.bias if conv.bias is not None else torch.zeros_like(bn.running_mean)
    return conv.weight*scale[:, None, None], bn.bias+(bias-bn.running_mean)*scale


class RepBlock(nn.Module):
    """Only linear branches are fused; ReLU is applied after their sum."""
    def __init__(self, channels, kernel=7):
        super().__init__()
        self.kernel = kernel
        self.wide = nn.Conv1d(channels, channels, kernel, padding=kernel//2, bias=False)
        self.wide_bn = nn.BatchNorm1d(channels)
        self.point = nn.Conv1d(channels, channels, 1, bias=False)
        self.point_bn = nn.BatchNorm1d(channels)
        self.identity_bn = nn.BatchNorm1d(channels)

    def forward(self, x):
        return F.relu(self.wide_bn(self.wide(x))+self.point_bn(self.point(x))+self.identity_bn(x))

    def to_deploy(self):
        if self.training:
            raise ValueError('Fold only frozen evaluation-mode BN')
        w, b = folded(self.wide, self.wide_bn)
        p, pb = folded(self.point, self.point_bn)
        identity = nn.Conv1d(w.shape[0], w.shape[1], 1, bias=False).to(w)
        with torch.no_grad():
            identity.weight.copy_(torch.eye(w.shape[0], device=w.device).unsqueeze(-1))
        iw, ib = folded(identity, self.identity_bn)
        output = nn.Conv1d(w.shape[1], w.shape[0], self.kernel, padding=self.kernel//2).to(w)
        pad = self.kernel//2
        with torch.no_grad():
            output.weight.copy_(w+F.pad(p, (pad, pad))+F.pad(iw, (pad, pad)))
            output.bias.copy_(b+pb+ib)
        return nn.Sequential(output, nn.ReLU())


class RepCNN(nn.Module):
    def __init__(self):
        super().__init__()
        self.stem = cbr(8, 16, 5)
        self.rep = RepBlock(16)
        self.last = nn.Sequential(nn.MaxPool1d(2), cbr(16, 24, 3), nn.MaxPool1d(2))
        self.fc = nn.Linear(24, 9)

    def forward(self, x):
        return self.fc(self.last(self.rep(self.stem(x))).mean(-1))


def candidates():
    return {
        'current_shape_reference_raw128': (SequentialCNN(), 128),
        'matched_sequential_raw256': (SequentialCNN(), 256),
        'statistics_pool_raw256': (SequentialCNN('stats'), 256),
        'four_region_pool_raw256': (SequentialCNN('spp'), 256),
        'multiscale_raw256': (MultiScaleCNN(), 256),
        'temporal_spatial_statistics_raw256': (TemporalSpatialCNN(), 256),
        'channel_attention_raw256': (ChannelAttentionCNN(), 256),
        'causal_dilated_tcn_raw256': (CausalTCN(), 256),
        'ring_spatial_raw256': (RingSpatialCNN(), 256),
        'rep_training_graph_raw256': (RepCNN(), 256),
    }

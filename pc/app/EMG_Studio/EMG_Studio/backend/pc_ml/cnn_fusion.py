"""PC trial: fixed clean/256 statistics plus a real temporal CNN correction.

One package contains all numeric tensors; TrialLog can snapshot it unchanged.
No serial/device access, pickle deserialization, or production FPGA claims.
"""
import hashlib
import io
import json
from pathlib import Path

import numpy as np
import torch
from torch import nn

from .cnn_variants import SequentialCNN
from .data import round_shift
from .fixed_linear import FixedLinear
from .fusion import FusionFeatures, FusionDecision, advance

FORMAT = 'EMG-CNN-FUSION-PC-1'
RUNTIME = 'cnn_fusion_clean256_v1'
TITLE = '统计＋时序CNN融合'
RECIPE = {'architecture': 'sequential256', 'epochs': 12, 'seed': 17,
          'alpha': .5, 'gate': .25, 'learning_rate': .002, 'weight_decay': .003,
          'batch_size': 128, 'gain_sigma': .08, 'auxiliary_weight': .5}


def make_model(classes):
    model = SequentialCNN()
    model.fc = nn.Linear(32, classes)
    return model


def normalize(waves, shifts):
    return np.stack([np.clip(round_shift(waves[:, c], int(s)), -127, 127)
                     for c, s in enumerate(shifts)], 1).astype(np.float32)/32


def wave_q4(signal):
    return round_shift(signal[:, :8].astype(np.int64), 4).astype(np.int32)


@torch.no_grad()
def logits(model, x):
    # Always one window per call: real-time results cannot depend on block size.
    model.eval()
    return np.stack([model(torch.from_numpy(w[None]))[0].numpy() for w in x]) if len(x) else np.empty((0, model.fc.out_features), dtype=np.float32)


def fuse(base, cnn, fraction, alpha=.5, gate=.25):
    correction = np.rint(cnn.astype(np.float64)*alpha*2.**fraction).astype(np.int64)
    top = np.sort(base, axis=1)
    strong = top[:, -1]-top[:, -2] >= int(gate*2.**fraction)
    correction[strong] = round_shift(correction[strong], 2)
    return base+correction


def export(path, head, model, shifts, metadata):
    path = Path(path); path.mkdir(parents=True, exist_ok=True)
    values = {f'cnn.{name}': t.detach().cpu().numpy() for name, t in model.state_dict().items()}
    values.update(offset=head.offset_, weights=head.weights_, bias=head.bias_, classes=head.classes_,
                  wave_shifts=np.asarray(shifts, dtype=np.int8))
    buffer = io.BytesIO(); np.savez_compressed(buffer, **values); payload = buffer.getvalue()
    (path/'parameters.bin').write_bytes(payload)
    names = metadata['class_names']; profile = metadata.get('gesture_profile')
    front = FusionFeatures()
    manifest = {'format': FORMAT, 'sha256': hashlib.sha256(payload).hexdigest(),
        'payload_bytes': len(payload), 'parameter_bytes': len(payload), 'class_names': names,
        'gesture_profile': profile, 'action_mapping': [c['output'] for c in profile['classes']] if profile else [],
        'frontend': {'name': 'clean'}, 'cnn_correction_active': True,
        'metadata': {**metadata, 'runtime': RUNTIME, 'model_title': TITLE,
                     'window': 256, 'stride': 32, 'guard': 320, 'feature_dimensions': 304,
                     'filter_coefficients_q14': front.coeff.tolist(),
                     'decision_policy': 'raw_rest_else_ewma_shift2', 'recipe': RECIPE,
                     'inference_backend': 'PC_float_CNN_integer_statistics', 'fpga_deployed': False},
        'linear_fraction': int(head.fraction_), 'linear_bound': int(head.bound_),
        'architecture': RECIPE['architecture'], 'alpha': RECIPE['alpha'], 'gate': RECIPE['gate'],
        'tensor_shapes': {name: list(value.shape) for name, value in values.items()}}
    (path/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf8')
    return manifest


def load(path):
    path = Path(path); m = json.loads((path/'manifest.json').read_text(encoding='utf8'))
    if m.get('format') != FORMAT or m.get('metadata', {}).get('runtime') != RUNTIME:
        raise ValueError('不支持的时序CNN融合包')
    payload = (path/'parameters.bin').read_bytes()
    if len(payload) != m['payload_bytes'] or hashlib.sha256(payload).hexdigest() != m['sha256']:
        raise ValueError('时序CNN融合参数校验失败')
    names = m['class_names']; k = len(names)
    expected = {'window': 256, 'stride': 32, 'guard': 320, 'feature_dimensions': 304,
                'filter_coefficients_q14': FusionFeatures().coeff.tolist(),
                'decision_policy': 'raw_rest_else_ewma_shift2', 'recipe': RECIPE}
    if (not 2 <= k <= 32 or len(set(names)) != k or m['metadata']['class_names'] != names or
            any(m['metadata'].get(key) != value for key, value in expected.items()) or
            m.get('architecture') != 'sequential256' or m.get('alpha') != .5 or m.get('gate') != .25):
        raise ValueError('时序CNN融合配置不匹配')
    profile = m.get('gesture_profile')
    if profile and ([c['name'] for c in profile['classes']] != names or
                    [c['id'] for c in profile['classes']] != list(range(k)) or
                    profile['classes'][0]['key'] != 'rest' or
                    m.get('action_mapping') != [c['output'] for c in profile['classes']]):
        raise ValueError('时序CNN动作方案不匹配')
    with np.load(io.BytesIO(payload), allow_pickle=False) as z:
        arrays = {name: z[name].copy() for name in z.files}
    if {name: list(value.shape) for name, value in arrays.items()} != m['tensor_shapes']:
        raise ValueError('时序CNN参数形状不匹配')
    head = FixedLinear()
    for field in ['offset', 'weights', 'bias', 'classes']: setattr(head, field+'_', arrays[field])
    head.fraction_ = int(m['linear_fraction']); head.bound_ = int(m['linear_bound'])
    shifts = arrays['wave_shifts']
    if (head.weights_.shape != (k, 304) or head.offset_.shape != (304,) or
            head.bias_.shape != (k,) or shifts.shape != (8,) or np.any((shifts < 0) | (shifts > 31)) or
            not np.array_equal(head.classes_, np.arange(k)) or not -16 <= head.fraction_ <= 40):
        raise ValueError('时序CNN分类头或缩放无效')
    model = make_model(k)
    state = {name[4:]: torch.from_numpy(value) for name, value in arrays.items() if name.startswith('cnn.')}
    if any(not torch.isfinite(t).all() for t in state.values()): raise ValueError('时序CNN包含无效参数')
    model.load_state_dict(state, strict=True); model.eval()
    return head, model, shifts, m


class StreamingCNNFusionPredictor:
    def __init__(self, path):
        # Avoid over-subscribing GUI worker threads for tiny batch inference.
        torch.set_num_threads(2)
        self.head, self.model, self.shifts, self.manifest = load(path)
        self.reset()

    def reset(self):
        self.frontend = FusionFeatures(); self.wave_front = FusionFeatures()
        self.decision = FusionDecision(len(self.manifest['class_names']))
        self.tail = np.empty((0, 8), dtype=np.int32); self.count = 0

    def push(self, raw):
        raw = np.asarray(raw)
        x, ends = self.frontend.push(raw)
        if not len(raw): return []
        q8 = (np.rint(raw).clip(0, 3300).astype(np.int64)-1650)*256
        front = self.wave_front
        signal, _ = advance(q8, front.coeff, front.state, front.env_state)
        waves = np.concatenate([self.tail, wave_q4(signal)])
        local_ends = len(self.tail)+ends-self.count
        self.tail = waves[-256:].copy(); self.count += len(raw)
        if not len(ends): return []
        windows = np.stack([waves[e-256:e].T for e in local_ends])
        cnn = logits(self.model, normalize(windows, self.shifts))
        scores = fuse(self.head.scores(x), cnn, self.head.fraction_)
        output = []
        for end, score in zip(ends, scores):
            pred, stable, margin = self.decision.step(score)
            output.append({'sample_end': int(end), 'prediction': pred, 'stable': stable,
                           'margin_q8': margin, 'accepted': True,
                           'name': self.manifest['class_names'][stable], 'cnn_participates': True,
                           'acceptance_validated': False})
        return output

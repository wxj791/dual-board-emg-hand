"""BN folding, symmetric INT8 weights/activations, INT32 sums, shifts only.

The PC emulator evaluates integer products with float64 conv kernels (exact for
the proven <2**31 accumulators) and verifies a direct int64 reference sample.
This is a numeric deployment artifact, not an FPGA bitstream.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

import numpy as np
import torch
from torch import nn

from .data import load_prepared, round_shift, GESTURES
from .models import make_model, CBR
from .train import infer, metrics


def fraction(max_abs):
    return int(np.clip(np.floor(np.log2(127/max(float(max_abs), 1e-9))), -16, 24))


def fold(block):
    conv, bn = block.conv, block.bn
    factor = bn.weight.detach()/torch.sqrt(bn.running_var+bn.eps)
    weights = conv.weight.detach()*factor[:, None, None]
    bias = bn.bias.detach()-bn.running_mean*factor
    return weights.numpy(), bias.numpy()


def qlayer(name, weights, bias, input_fraction, output_fraction, groups=1,
           dilation=1, relu=True, clamp=True):
    weight_fraction = np.array([fraction(abs(row).max()) for row in weights])
    reshape = (-1,) + (1,)*(weights.ndim-1)
    quantized = np.clip(np.rint(weights*np.exp2(weight_fraction.reshape(reshape))), -127, 127).astype(np.int8)
    qb = np.rint(bias*np.exp2(input_fraction+weight_fraction)).astype(np.int64)
    # Bound covers every input combination in [-127,127], not only calibration.
    bounds = np.abs(qb) + np.abs(quantized.astype(np.int64)).reshape(len(qb), -1).sum(1)*127
    if np.any(bounds >= 2**31):
        raise OverflowError(f'INT32 bound exceeded: {name}')
    return {'name': name, 'weights': quantized, 'bias': qb.astype(np.int32),
            'input_fraction': input_fraction, 'output_fraction': output_fraction,
            'weight_fraction': weight_fraction,
            'shifts': input_fraction+weight_fraction-output_fraction,
            'groups': groups, 'dilation': dilation, 'relu': relu, 'clamp': clamp,
            'worst_case_accumulator': int(bounds.max())}


def execute(layer, x):
    x = np.asarray(x, dtype=np.int64)
    w = layer['weights'].astype(np.int64)
    if w.ndim == 3:
        raw = nn.functional.conv1d(torch.from_numpy(x.astype(np.float64)),
                                  torch.from_numpy(w.astype(np.float64)),
                                  padding=layer['dilation']*(w.shape[-1]//2),
                                  dilation=layer['dilation'], groups=layer['groups']).numpy().astype(np.int64)
    else:
        raw = x @ w.T
    raw += layer['bias'].reshape((1, -1)+(1,)*(x.ndim-2))
    if np.max(np.abs(raw)) >= 2**31:
        raise OverflowError(layer['name'])
    result = np.stack([round_shift(raw[:, i], int(shift)) for i, shift in enumerate(layer['shifts'])], axis=1)
    if layer['relu']:
        result = np.maximum(result, 0)
    if layer['clamp']:
        result = np.clip(result, -127, 127)
    return result.astype(np.int64)


def pool(x):
    return np.maximum(x[..., ::2], x[..., 1::2])


def gap(x):
    assert x.shape[-1] & (x.shape[-1]-1) == 0
    return round_shift(x.sum(-1), int(np.log2(x.shape[-1])))


class IntegerModel:
    def __init__(self, kind, layers, feature_fraction):
        self.kind = kind
        self.layers = layers
        self.feature_fraction = feature_fraction

    def trace(self, q):
        x = np.asarray(q, dtype=np.int64)
        t = {'input': x}
        t['stem'] = execute(self.layers['stem'], x)
        t['pool1'] = pool(t['stem'])
        if self.kind == 'sequential':
            t['conv2'] = execute(self.layers['conv2'], t['pool1'])
            t['pool2'] = pool(t['conv2'])
            t['conv3'] = execute(self.layers['conv3'], t['pool2'])
            t['features'] = gap(t['conv3'])
        else:
            branches = []
            for i in range(3):
                name = f'branches.{i}'
                t[name] = execute(self.layers[name], t['pool1'])
                branches.append(pool(t[name]))
            t['concat'] = np.concatenate(branches, axis=1)
            t['fusion'] = execute(self.layers['fusion'], t['concat'])
            t['temporal'] = execute(self.layers['temporal'], t['fusion'])
            envelope = np.clip(round_shift(gap(np.abs(x)), 5-self.feature_fraction), 0, 127)
            t['features'] = np.concatenate((gap(t['temporal']), envelope), axis=1)
        t['logits'] = execute(self.layers['fc'], t['features'])
        return t

    def predict_logits(self, x, batch=256):
        q = np.rint(np.asarray(x)*32).astype(np.int64)
        return np.concatenate([self.trace(q[i:i+batch])['logits'] for i in range(0, len(q), batch)])


def calibrate(model, kind, calibration):
    model.eval()
    maxima = {}
    handles = []
    def capture(name):
        def hook(mod, inputs, output):
            maxima[name] = max(maxima.get(name, 0), float(output.abs().max()))
        return hook
    for name, mod in model.named_modules():
        if isinstance(mod, CBR):
            handles.append(mod.register_forward_hook(capture(name)))
    infer(model, calibration)
    for handle in handles:
        handle.remove()
    fs = {name: fraction(value*1.05) for name, value in maxima.items()}
    if kind == 'multiscale':
        common = min(fs[f'branches.{i}'] for i in range(3))
        for i in range(3):
            fs[f'branches.{i}'] = common
        # One scale shared by temporal GAP and amplitude bypass at FC input.
        fs['temporal'] = min(fs['temporal'], fraction(float(np.abs(calibration).mean(-1).max())*1.05))
    layers = {}
    for name, mod in model.named_modules():
        if not isinstance(mod, CBR):
            continue
        if name == 'stem':
            fi = 5
        elif name == 'conv2' or name.startswith('branches.'):
            fi = fs['stem']
        elif name == 'conv3':
            fi = fs['conv2']
        elif name == 'fusion':
            fi = fs['branches.0']
        else:
            fi = fs['fusion']
        weights, bias = fold(mod)
        layers[name] = qlayer(name, weights, bias, fi, fs[name],
                              groups=mod.conv.groups, dilation=mod.conv.dilation[0])
    feature_fraction = fs['conv3'] if kind == 'sequential' else fs['temporal']
    layers['fc'] = qlayer('fc', model.fc.weight.detach().numpy(), model.fc.bias.detach().numpy(),
                          feature_fraction, 8, relu=False, clamp=False)
    return IntegerModel(kind, layers, feature_fraction)


def direct_reference(layer, x):
    """Separate int64 implementation for an entire first calibration window."""
    w = layer['weights'].astype(np.int64)
    if w.ndim == 2:
        raw = (x[:, None, :]*w[None]).sum(-1) + layer['bias'][None]
    else:
        pad = layer['dilation']*(w.shape[-1]//2)
        padded = np.pad(x, ((0, 0), (0, 0), (pad, pad)))
        raw = np.empty((len(x), len(w), x.shape[-1]), dtype=np.int64)
        cout_group = len(w)//layer['groups']
        cin_group = w.shape[1]
        for oc in range(len(w)):
            group = oc//cout_group
            for time in range(x.shape[-1]):
                patch = padded[:, group*cin_group:(group+1)*cin_group,
                               time:time+layer['dilation']*w.shape[-1]:layer['dilation']]
                raw[:, oc, time] = (patch*w[oc]).sum((1, 2))+layer['bias'][oc]
    output = np.stack([round_shift(raw[:, i], int(s)) for i, s in enumerate(layer['shifts'])], 1)
    if layer['relu']:
        output = np.maximum(output, 0)
    return np.clip(output, -127, 127) if layer['clamp'] else output


def export(model, out, input_shifts, checkpoint_name, class_names=None):
    class_names = list(GESTURES if class_names is None else class_names)
    if len(class_names) != len(model.layers['fc']['bias']):
        raise ValueError('Class names do not match classifier outputs')
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    payload = bytearray()
    descriptors = []
    def add(name, array, dtype):
        arr = np.asarray(array, dtype=dtype)
        offset = len(payload)
        payload.extend(arr.tobytes(order='C'))
        descriptors.append({'name': name, 'offset': offset, 'bytes': arr.nbytes,
                            'dtype': arr.dtype.str, 'shape': list(arr.shape)})
    add('input_shifts', input_shifts, '<i4')
    topology = []
    for name, layer in model.layers.items():
        for field, dtype in [('weights', 'i1'), ('bias', '<i4'), ('shifts', '<i4')]:
            add(f'{name}.{field}', layer[field], dtype)
        topology.append({k: (v.tolist() if isinstance(v, np.ndarray) else v)
                         for k, v in layer.items() if k not in ('weights', 'bias', 'shifts')})
    binary = bytes(payload)
    (out/'parameters.bin').write_bytes(binary)
    (out/'parameters_bytes.hex').write_text(''.join(f'{v:02x}\n' for v in binary), encoding='ascii')
    descriptor = {'format': 'EMG-PARAM-1', 'kind': model.kind, 'checkpoint': checkpoint_name,
                  'payload_bytes': len(binary), 'sha256': hashlib.sha256(binary).hexdigest(),
                  'crc32': __import__('zlib').crc32(binary), 'class_names': class_names,
                  'num_classes': len(class_names),
                  'input_fraction': 5, 'feature_fraction': model.feature_fraction,
                  'window': 128, 'stride': 32, 'layers': topology, 'tensors': descriptors,
                  'rounding': 'Nearest, ties away from zero; per-layer sat [-127,127], FC INT32',
                  'layout': 'Conv [out,in/group,k], FC [out,in], C order; little-endian INT32',
                  'status': 'PC numeric package; current production FPGA RTL cannot load this format yet.'}
    (out/'manifest.json').write_text(json.dumps(descriptor, ensure_ascii=False, indent=2), encoding='utf-8')
    return descriptor


def load_integer(path):
    path = Path(path)
    manifest = json.loads((path/'manifest.json').read_text(encoding='utf-8'))
    from .frontend import create_filter
    if manifest['format'] not in ('EMG-PARAM-1', 'EMG-PARAM-2'):
        raise ValueError('Unsupported parameter format')
    if manifest['kind'] not in ('sequential', 'multiscale'):
        raise ValueError('Unsupported network topology')
    create_filter(manifest.get('frontend'))
    binary = (path/'parameters.bin').read_bytes()
    if hashlib.sha256(binary).hexdigest() != manifest['sha256']:
        raise ValueError('Parameter SHA mismatch')
    arrays = {t['name']: np.frombuffer(binary, dtype=t['dtype'], count=t['bytes']//np.dtype(t['dtype']).itemsize,
                                      offset=t['offset']).reshape(t['shape']).copy() for t in manifest['tensors']}
    layers = {}
    for row in manifest['layers']:
        layer = dict(row)
        for key in ('weights', 'bias', 'shifts'):
            layer[key] = arrays[f'{row["name"]}.{key}']
        layers[row['name']] = layer
    if not 2 <= len(manifest['class_names']) <= 32 or len(set(manifest['class_names'])) != len(manifest['class_names']):
        raise ValueError('Invalid classifier class names')
    if len(layers['fc']['bias']) != len(manifest['class_names']):
        raise ValueError('Class names do not match classifier outputs')
    return IntegerModel(manifest['kind'], layers, manifest['feature_fraction']), arrays['input_shifts'], manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', type=Path, default=Path('artifacts/pc_ml'))
    parser.add_argument('--threads', type=int, default=4)
    args = parser.parse_args()
    torch.set_num_threads(args.threads)
    checkpoint = torch.load(args.run/'selected.pth', weights_only=True)
    model = make_model(checkpoint['kind'], checkpoint.get('num_classes', checkpoint['state_dict']['fc.weight'].shape[0]))
    model.load_state_dict(checkpoint['state_dict'])
    data, data_manifest = load_prepared(args.run/'data')
    mask = data['split'] == 0
    indices = np.flatnonzero(mask)
    rng = np.random.default_rng(88)
    calibration = data['x'][rng.choice(indices, min(2048, len(indices)), replace=False)]
    integer = calibrate(model, checkpoint['kind'], calibration)
    out = args.run/'export'
    descriptor = export(integer, out, checkpoint['input_shifts'], checkpoint['name'], data_manifest.get('class_names', GESTURES))
    reloaded, _, _ = load_integer(out)
    sample = np.rint(calibration[:1]*32).astype(np.int64)
    trace = integer.trace(sample)
    assert all(np.array_equal(v, reloaded.trace(sample)[k]) for k, v in trace.items())
    # Independently verify all arithmetic layers against int64 reference.
    for name, layer in integer.layers.items():
        if name == 'stem':
            inp = trace['input']
        elif name == 'conv2' or name.startswith('branches.'):
            inp = trace['pool1']
        elif name == 'conv3':
            inp = trace['pool2']
        elif name == 'fusion':
            inp = trace['concat']
        elif name == 'temporal':
            inp = trace['fusion']
        else:
            inp = trace['features']
        assert np.array_equal(execute(layer, inp), direct_reference(layer, inp)), name
    vectors = np.rint(calibration[:20]*32).astype(np.int8)
    np.savez_compressed(out/'golden_layers.npz', **integer.trace(vectors))
    results = {}
    for name, target in [('source', False), ('field', True)]:
        for split_name, split in [('validation', 1), ('test', 2)]:
            partition = (data['target'] == target) & (data['split'] == split)
            x, y = data['x'][partition], data['y'][partition]
            fp = infer(model, x).argmax(1)
            ip = integer.predict_logits(x).argmax(1)
            results[f'{name}_{split_name}'] = {'float': metrics(y, fp), 'integer': metrics(y, ip),
                                                      'prediction_agreement': float(np.mean(fp == ip))}
    report = {'parameters': descriptor, 'evaluation': results,
              'int64_reference_all_layers': True, 'package_roundtrip': True,
              'calibration': '2048 windows or fewer from training partitions only',
              'golden_windows': 20, 'rtl_verified': False}
    (args.run/'quantization.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    m = results['field_test']
    print('INT8 field test:', m, '\nParameter bytes:', descriptor['payload_bytes'], flush=True)


if __name__ == '__main__':
    main()

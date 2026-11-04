"""Portable compare/add forest: signed INT16 inputs/leaves, INT32 scores.

All nodes use 128 bits. Internal nodes store feature[6:0], threshold[23:8],
left[39:24], right[55:40]. Leaves set bit127 and store seven signed16 scores
in bits111:0. This format also expresses scalar boosted trees without DSPs.
"""
import hashlib
import json
from pathlib import Path

import numpy as np
from numba import njit


def feature_map(x, centered=False):
    x = np.asarray(x, dtype=np.int16)
    if not centered:
        return x
    blocks = x.astype(np.int32).reshape(-1, 8, 8)
    relative = blocks - (blocks.sum(2, keepdims=True) >> 3)
    return np.concatenate([x, relative.reshape(-1, 64).clip(-32768, 32767)], 1).astype(np.int16)


@njit(cache=True)
def traverse(x, roots, feature, threshold, left, right, leaf, values, baseline):
    scores = np.empty((len(x), 7), dtype=np.int32)
    for i in range(len(x)):
        for c in range(7):
            scores[i, c] = baseline[c]
        for root in roots:
            node = root
            while not leaf[node]:
                node = left[node] if x[i, feature[node]] <= threshold[node] else right[node]
            for c in range(7):
                scores[i, c] += values[node, c]
    return scores


class FixedTree:
    @classmethod
    def from_sklearn(cls, model, kind, dimensions):
        obj = cls()
        obj.dimensions = dimensions
        obj.kind = kind
        obj.fraction = 12 if kind == 'boost' else 15
        rows, roots, leaves = [], [], []
        baseline = np.zeros(7, dtype=np.int64)
        if kind == 'boost':
            baseline = np.rint(model._baseline_prediction.ravel()*4096).astype(np.int64)
            trees = [(p.nodes, c) for iteration in model._predictors for c, p in enumerate(iteration)]
        else:
            trees = [(tree.tree_, None) for tree in model.estimators_]
        depth = 0
        for tree, target_class in trees:
            offset = len(rows)
            roots.append(offset)
            count = len(tree) if kind == 'boost' else tree.node_count
            for i in range(count):
                if kind == 'boost':
                    n = tree[i]
                    assert not n['is_categorical']
                    is_leaf = bool(n['is_leaf'])
                    f, t = int(n['feature_idx']), float(n['num_threshold'])
                    a, b = int(n['left']), int(n['right'])
                    value = np.zeros(7)
                    if is_leaf:
                        value[target_class] = n['value']
                    depth = max(depth, int(n['depth']))
                else:
                    is_leaf = tree.children_left[i] == -1
                    f, t = int(tree.feature[i]), float(tree.threshold[i])
                    a, b = int(tree.children_left[i]), int(tree.children_right[i])
                    v = tree.value[i, 0]
                    value = v/max(v.sum(), 1e-20) if is_leaf else np.zeros(7)
                    depth = max(depth, int(tree.max_depth))
                # For integral inputs, x <= floating threshold equals x <= floor(t).
                threshold = int(np.floor(t)) if not is_leaf else 0
                if not is_leaf and not (-32768 <= threshold <= 32767):
                    raise OverflowError('Tree threshold is not INT16')
                q = np.rint(value*(1 << obj.fraction))
                # RF probability 1 is represented by 32767, not wrapped to -32768.
                if kind == 'boost' and (abs(q) > 32767).any():
                    raise OverflowError('Boosted leaf is not signed16')
                q = q.clip(-32768, 32767).astype(np.int16)
                rows.append((max(f, 0), threshold, offset+a if not is_leaf else 0,
                             offset+b if not is_leaf else 0, is_leaf))
                leaves.append(q)
        assert len(rows) < 65536
        r = np.asarray(rows, dtype=np.int64)
        obj.roots = np.asarray(roots, dtype=np.int32)
        obj.feature = r[:, 0].astype(np.int16)
        obj.threshold = r[:, 1].astype(np.int16)
        obj.left, obj.right = r[:, 2].astype(np.int32), r[:, 3].astype(np.int32)
        obj.leaf = r[:, 4].astype(np.bool_)
        obj.values = np.asarray(leaves, dtype=np.int16)
        obj.baseline = baseline.astype(np.int32)
        obj.depth = depth
        obj.bound = int(np.max(abs(baseline)) + len(roots)*32768)
        assert obj.bound < 2**31
        return obj

    def scores(self, x):
        x = np.asarray(x)
        if x.shape[1] != self.dimensions or x.dtype != np.int16:
            raise ValueError('Expected signed16 feature matrix with declared dimensions')
        return traverse(x, self.roots, self.feature, self.threshold, self.left,
                        self.right, self.leaf, self.values, self.baseline)

    def package(self, directory, metadata):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        words = []
        for i in range(len(self.feature)):
            if self.leaf[i]:
                word = 1 << 127
                for c, v in enumerate(self.values[i]):
                    word |= (int(v) & 65535) << (16*c)
            else:
                word = (int(self.feature[i]) | ((int(self.threshold[i]) & 65535) << 8)
                        | (int(self.left[i]) << 24) | (int(self.right[i]) << 40))
            words.append(word)
        payload = b''.join(v.to_bytes(16, 'little') for v in words)
        payload += self.roots.astype('<u2').tobytes()+self.baseline.astype('<i4').tobytes()
        (directory/'parameters.bin').write_bytes(payload)
        (directory/'nodes.hex').write_text(''.join(f'{v:032x}\n' for v in words), encoding='ascii')
        (directory/'roots.hex').write_text(''.join(f'{v:04x}\n' for v in self.roots), encoding='ascii')
        (directory/'baseline.hex').write_text(''.join(f'{int(v)&0xffffffff:08x}\n' for v in self.baseline), encoding='ascii')
        m = {'format': 'EMG-TREE-INT-1', 'kind': self.kind, 'features': self.dimensions,
             'trees': len(self.roots), 'nodes': len(words), 'max_depth': self.depth,
             'leaf_fraction': self.fraction, 'parameter_bytes': len(payload),
             'node_bits': 128, 'score_bits': 32, 'score_bound': self.bound,
             'baseline': self.baseline.tolist(), 'sha256': hashlib.sha256(payload).hexdigest(),
             'ties': 'lowest class index', 'metadata': metadata,
             'scope': 'Integer classifier; complete acquisition-to-board integration absent'}
        (directory/'manifest.json').write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding='utf-8')
        return m

    @classmethod
    def load(cls, directory):
        directory = Path(directory)
        m = json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
        payload = (directory/'parameters.bin').read_bytes()
        if m['format'] != 'EMG-TREE-INT-1' or hashlib.sha256(payload).hexdigest() != m['sha256']:
            raise ValueError('Invalid integer tree package')
        obj = cls()
        rows, values = [], []
        for i in range(m['nodes']):
            w = int.from_bytes(payload[i*16:i*16+16], 'little')
            signed_threshold = (w >> 8) & 65535
            if signed_threshold >= 32768:
                signed_threshold -= 65536
            leaf = bool(w >> 127)
            rows.append((0 if leaf else w & 127, 0 if leaf else signed_threshold,
                         0 if leaf else (w >> 24) & 65535, 0 if leaf else (w >> 40) & 65535, leaf))
            v = []
            for c in range(7):
                code = (w >> (16*c)) & 65535
                v.append((code-65536 if code >= 32768 else code) if leaf else 0)
            values.append(v)
        r = np.asarray(rows, dtype=np.int64)
        obj.feature, obj.threshold = r[:, 0].astype(np.int16), r[:, 1].astype(np.int16)
        obj.left, obj.right, obj.leaf = r[:, 2].astype(np.int32), r[:, 3].astype(np.int32), r[:, 4].astype(bool)
        obj.values = np.asarray(values, dtype=np.int16)
        offset = m['nodes']*16
        obj.roots = np.frombuffer(payload, dtype='<u2', count=m['trees'], offset=offset).astype(np.int32)
        obj.baseline = np.frombuffer(payload, dtype='<i4', count=7, offset=offset+2*m['trees']).copy()
        obj.dimensions, obj.kind, obj.depth = m['features'], m['kind'], m['max_depth']
        obj.fraction, obj.bound = m['leaf_fraction'], m['score_bound']
        return obj, m

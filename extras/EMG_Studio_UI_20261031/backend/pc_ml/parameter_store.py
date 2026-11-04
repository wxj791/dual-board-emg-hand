"""Executable PC contract for future FPGA double-bank parameter loading.

No serial I/O. Production RTL does not implement this interface yet.
"""
from __future__ import annotations

import copy
import hashlib
import json
import zlib
from pathlib import Path
from .frontend import create_filter, frontend_descriptor


class ParameterStore:
    def __init__(self, manifest, payload):
        self.check(manifest, payload)
        self.banks = [(copy.deepcopy(manifest), bytes(payload)), None]
        self.active = 0
        self.version = 1
        self.topology = self.signature(manifest)
        self.staging = None
        self.pending = False
        self.pending_version = None

    @staticmethod
    def signature(manifest):
        frontend = manifest.get('frontend')
        fixed_frontend = frontend_descriptor(frontend['name']) if frontend else None
        return (manifest['kind'], manifest['window'], manifest['stride'],
                json.dumps(fixed_frontend, sort_keys=True),
                manifest['input_fraction'], tuple(manifest['class_names']),
                tuple((layer['name'], layer['groups'], layer['dilation'], layer['relu'], layer['clamp'])
                      for layer in manifest['layers']),
                tuple((t['name'], tuple(t['shape']), t['dtype'], t['offset'], t['bytes'])
                      for t in manifest['tensors']))

    @staticmethod
    def check(manifest, payload):
        create_filter(manifest.get('frontend'))
        if len(payload) != manifest['payload_bytes']:
            raise ValueError('Length mismatch')
        if hashlib.sha256(payload).hexdigest() != manifest['sha256']:
            raise ValueError('SHA256 mismatch')
        if zlib.crc32(payload) != manifest['crc32']:
            raise ValueError('CRC32 mismatch')

    def begin(self, manifest, version):
        create_filter(manifest.get('frontend'))
        if self.staging is not None or self.pending:
            raise ValueError('Update already pending')
        if self.signature(manifest) != self.topology:
            raise ValueError('Network topology differs from fixed hardware')
        if version <= self.version:
            raise ValueError('Non-increasing parameter version')
        self.staging = (copy.deepcopy(manifest), bytearray(manifest['payload_bytes']))
        self.offset = 0
        self.pending_version = version

    def write(self, offset, payload, crc32):
        if self.staging is None:
            raise ValueError('BEGIN required')
        if not 0 < len(payload) <= 256:
            raise ValueError('Chunk must contain 1..256 bytes')
        if offset != self.offset:
            raise ValueError('Unexpected offset')
        if zlib.crc32(payload) != crc32:
            raise ValueError('Chunk CRC32 mismatch')
        if offset+len(payload) > len(self.staging[1]):
            raise ValueError('Write past staging bank')
        self.staging[1][offset:offset+len(payload)] = payload
        self.offset += len(payload)

    def verify(self):
        if self.staging is None:
            raise ValueError('BEGIN required')
        manifest, payload = self.staging
        if self.offset != len(payload):
            raise ValueError('Incomplete transfer')
        self.check(manifest, payload)
        self.banks[1-self.active] = (manifest, bytes(payload))
        self.staging = None
        self.pending = True

    def window_boundary(self, inference_busy=False):
        if inference_busy or not self.pending:
            return False
        self.active = 1-self.active
        self.version = self.pending_version
        self.pending = False
        self.pending_version = None
        # FPGA must also flush in-flight old-scale windows and vote history.
        return True

    def cancel(self):
        self.staging = None
        self.pending = False
        self.pending_version = None

    def snapshot(self):
        manifest, payload = self.banks[self.active]
        return self.version, copy.deepcopy(manifest), payload


def exercise(package, out):
    package = Path(package)
    manifest = json.loads((package/'manifest.json').read_text(encoding='utf-8'))
    payload = (package/'parameters.bin').read_bytes()
    store = ParameterStore(manifest, payload)
    old = store.snapshot()
    new_payload = bytearray(payload)
    weight_offset = next(t['offset'] for t in manifest['tensors'] if t['name'].endswith('.weights'))
    new_payload[weight_offset] ^= 1
    new_payload = bytes(new_payload)
    new_manifest = copy.deepcopy(manifest)
    new_manifest['sha256'] = hashlib.sha256(new_payload).hexdigest()
    new_manifest['crc32'] = zlib.crc32(new_payload)
    checks = []
    def rejects(name, fn):
        try:
            fn()
        except ValueError:
            checks.append(name)
            return
        raise AssertionError(name)
    rejects('reject_write_before_begin', lambda: store.write(0, b'x', zlib.crc32(b'x')))
    wrong = copy.deepcopy(manifest)
    wrong['window'] = 256
    rejects('reject_changed_topology', lambda: store.begin(wrong, 2))
    wrong = copy.deepcopy(manifest)
    wrong['layers'][0]['dilation'] = 2
    rejects('reject_changed_convolution_dilation', lambda: store.begin(wrong, 2))
    rejects('reject_old_version', lambda: store.begin(manifest, 1))
    store.begin(manifest, 2)
    rejects('reject_incomplete_transfer', store.verify)
    rejects('reject_wrong_offset', lambda: store.write(1, payload[:256], zlib.crc32(payload[:256])))
    rejects('reject_chunk_crc', lambda: store.write(0, payload[:256], zlib.crc32(payload[:256]) ^ 1))
    store.write(0, payload[:256], zlib.crc32(payload[:256]))
    store.cancel()
    assert store.snapshot() == old
    checks.append('interrupted_transfer_keeps_old_model')
    store.begin(new_manifest, 2)
    for offset in range(0, len(new_payload), 256):
        block = new_payload[offset:offset+256]
        store.write(offset, block, zlib.crc32(block))
        assert store.snapshot() == old
    store.verify()
    assert not store.window_boundary(inference_busy=True)
    assert store.snapshot() == old
    checks.append('no_swap_during_inference')
    assert store.window_boundary()
    assert store.snapshot()[0] == 2 and store.snapshot()[2] == new_payload
    checks.append('atomic_swap_at_window_boundary')
    altered = bytearray(payload)
    altered[-1] ^= 1
    rejects('reject_whole_package_corruption', lambda: ParameterStore(manifest, altered))
    result = {'checks_passed': checks, 'parameter_bytes': len(payload), 'chunks': (len(payload)+255)//256,
              'wire_payload_minimum_115200_seconds': len(payload)*10/115200,
              'wire_payload_minimum_921600_seconds': len(payload)*10/921600,
              'scope': 'PC model contract only; no FPGA loader, ACK frames, or physical transfer verified.'}
    Path(out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--package', type=Path, default=Path('artifacts/pc_ml/field/export'))
    p.add_argument('--output', type=Path, default=Path('artifacts/pc_ml/parameter_store_checks.json'))
    a = p.parse_args()
    print(json.dumps(exercise(a.package, a.output), indent=2))

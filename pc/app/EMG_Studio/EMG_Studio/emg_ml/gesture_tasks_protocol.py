"""Frozen gesture-task wire format. No serial ports are opened here."""
from __future__ import annotations
import binascii
import os
from dataclasses import dataclass, field
import struct
import threading
import time
import zlib

MAX_CLASSES = 32

def sparse_wire_opcode(opcode):
    """Wire bit 0 is redundant; logical bit 0 travels in reserved wire bit 15."""
    if type(opcode) is not int or not 0 <= opcode < 0x4000:
        raise ValueError('Sparse logical opcode must be in 0..0x3fff')
    # Keep the zero alias on the wire: the original A then recognizes STOP
    # and records even motion opcodes exactly as B will acknowledge them.
    return (opcode & 0x3ffe) | ((opcode & 1) << 15)

def wire_trace(step, **details):
    # A logging failure must not change command or reply handling.
    try:
        from gesture_tasks_customer import diagnostic
        diagnostic(step, **details)
    except Exception:
        pass
RANGES = (20, 50, 100, 200, 500, 1200, 3300)
TASK_NAMES = ('不执行', '普通姿态回放', '碰撞检测（持续往复，固定100档）',
              '上一页', '下一页', '波形放大（减小量程）', '波形缩小（增大量程）')
ASYNC = {0x300, 0x304, 0x305, 0x605, 0x606}

def frame(kind, payload, seq=0):
    if len(payload) > 1024: raise ValueError('frame exceeds 1024 bytes')
    body = struct.pack('<BBHHI', 1, kind, seq & 65535, len(payload), 0)+payload
    return b'\xa5\x5a'+body+struct.pack('<H', binascii.crc_hqx(body, 65535))

class Parser:
    def __init__(self): self.buffer = bytearray(); self.errors = 0
    def feed(self, data, detailed=False):
        self.buffer.extend(data); result = []
        while len(self.buffer) >= 2:
            start = self.buffer.find(b'\xa5\x5a')
            if start < 0:
                self.buffer[:] = self.buffer[-1:] if self.buffer[-1] == 0xa5 else b''
                break
            del self.buffer[:start]
            if len(self.buffer) < 12: break
            size = int.from_bytes(self.buffer[6:8], 'little')
            if self.buffer[2] != 1 or size > 1024:
                self.errors += 1; del self.buffer[0]; continue
            end = size+14
            if len(self.buffer) < end: break
            raw = bytes(self.buffer[:end])
            if binascii.crc_hqx(raw[2:-2], 65535) != int.from_bytes(raw[-2:], 'little'):
                self.errors += 1; del self.buffer[0]; continue
            del self.buffer[:end]
            item=(raw[3], raw[12:-2])
            if detailed: item+=(int.from_bytes(raw[4:6],'little'),int.from_bytes(raw[8:12],'little'),self.errors)
            result.append(item)
        return result

@dataclass(frozen=True)
class Binding:
    class_id: int
    kind: int = 0
    enabled: bool = False
    argument: int = 0
    def pack(self):
        if (type(self.class_id) is not int or not 0 <= self.class_id < MAX_CLASSES or
            type(self.kind) is not int or not 0 <= self.kind <= 6 or
            type(self.enabled) is not bool or type(self.argument) is not int or
            not (0 <= self.argument <= 8 if self.kind == 1 else self.argument == 0) or
            (self.kind == 0 and self.enabled) or (self.class_id == 0 and (self.kind or self.enabled or self.argument))):
            raise ValueError('非法任务/槽号；静息必须禁用，槽的协议编号为0～8')
        return struct.pack('<HBBHH', self.class_id, self.kind, self.enabled, self.argument, 0)

def rows_bytes(rows):
    if not 2 <= len(rows) <= MAX_CLASSES or [r.class_id for r in rows] != list(range(len(rows))):
        raise ValueError('任务表必须包含2～32个连续类别的全部行（含禁用项）')
    return b''.join(r.pack() for r in rows)

def map_commands(generation, revision, rows):
    raw = rows_bytes(rows)
    yield 0x600, struct.pack('<HHI', generation, len(rows), revision)
    for r in rows: yield 0x601, r.pack()
    yield 0x602, struct.pack('<II', revision, zlib.crc32(raw))

def task_state(data):
    if len(data) != 28: raise ValueError('TASK_STATE必须是28字节')
    keys = ('schema','display_enabled','mechanical_armed','teach_mode','generation','class_count',
            'map_revision','mapping_valid','motion_state','active_mask','retired_mask',
            'pose_valid_mask','flash_ready','page','range_mv','reserved','fault_flags')
    d = dict(zip(keys, struct.unpack('<BBBBHHIBBBBHBBHHI', data)))
    if (d['schema'] != 1 or d['reserved'] or any(d[k] not in (0,1) for k in
        ('display_enabled','mechanical_armed','teach_mode','mapping_valid','flash_ready')) or
        d['motion_state'] > 3 or d['page'] > 5 or d['range_mv'] not in (*RANGES,65535) or
        d['active_mask'] & ~31 or d['retired_mask'] & ~31 or
        (d['motion_state'] == 2 and d['active_mask'] & 2)):
        raise ValueError('TASK_STATE字段无效')
    return d

def range_command(page, channel, mv):
    if page not in range(4) or channel not in (*range(8),255) or mv not in RANGES:
        raise ValueError('非法显示页/通道/量程')
    return struct.pack('<BBH', page, channel, mv)

@dataclass
class Pending:
    target: int
    rid: int
    opcode: int
    asynchronous: bool
    deadline: float
    event: threading.Event = field(default_factory=threading.Event)
    phase: str = 'sent'
    data: bytes = b''
    error: str = ''
    accepted: bool = False
    status: int | None = None

class BoardRequestError(RuntimeError):
    def __init__(self, pending):
        super().__init__(pending.error)
        self.target = pending.target
        self.opcode = pending.opcode
        self.rid = pending.rid
        self.phase = pending.phase
        self.status = pending.status
        self.accepted = pending.accepted

class Session:
    """One reader, serialized writes, no blind retries, IDs quarantined until reconnect."""
    def __init__(self, writer, clock=time.monotonic, *, sparse_opcodes=None):
        self.writer = writer; self.clock = clock; self.lock = threading.RLock()
        self.sparse_opcodes = (os.environ.get('EMG_SPARSE_OPCODES') == '1'
                               if sparse_opcodes is None else bool(sparse_opcodes))
        self.pending = {}; self.next_id = 1; self.seq = 0; self.connected = False
        self.ignored = 0; self.epoch = 0
        self.ignore_b_replies = False
        self._b_send_lock = threading.Lock(); self._last_b_send = 0.0
    def reset(self, connected=False):
        with self.lock:
            for p in self.pending.values():
                if not p.event.is_set():
                    p.error = '连接会话结束，执行结果未知'; p.phase = 'unknown'; p.event.set()
            self.pending.clear(); self.next_id = 1; self.seq = 0; self.connected = connected; self.epoch += 1
    def send(self, target, opcode, data=b'', timeout=5, asynchronous=None, rid=None, expect_reply=True):
        with self.lock:
            if not self.connected: raise ConnectionError('未连接A；板端状态未知')
            if target not in (1,2): raise ValueError('target must be A=1 or B=2')
            if rid is None: rid = self.next_id
            if not self.next_id <= rid <= 65535 or any(k[1] == rid for k in self.pending):
                raise ValueError('request_id已使用；请查询状态或重新连接')
            self.next_id = rid+1
            # Entering teach mode unloads asynchronously; leaving teach mode
            # completes in the ordinary status=0 ACK and has no final event.
            if asynchronous is None:
                asynchronous = opcode in ASYNC and not (opcode == 0x605 and data == b'\x00')
            p = Pending(target, rid, opcode, asynchronous,
                        self.clock()+timeout)
            if expect_reply:self.pending[(target,rid,opcode)] = p
            wire_sparse = self.sparse_opcodes and target == 2
            wire_opcode = sparse_wire_opcode(opcode) if wire_sparse else opcode
            payload = struct.pack('<BBHHH',1,target,rid,wire_opcode,len(data))+data
            wire = frame(0x50,payload,self.seq)
            wire_trace('board_tx_attempt', target=target, opcode=f'{opcode:04x}',
                       wire_opcode=f'{wire_opcode:04x}', sparse_opcodes=wire_sparse,
                       request_id=rid, session_epoch=self.epoch, frame_bytes=len(wire), frame_hex=wire[:96].hex())
            try:
                self.writer(wire)
                wire_trace('board_tx_written', target=target, opcode=f'{opcode:04x}',
                           request_id=rid, session_epoch=self.epoch, frame_bytes=len(wire),
                           meaning='writer returned successfully; board receipt is not yet confirmed')
            except Exception as e:
                p.error = f'发送失败，结果未知：{e}'; p.phase = 'unknown'; p.event.set()
            self.seq = (self.seq+1)&65535
            return p
    def send_only(self, opcode, data=b''):
        """Write one B command. Never await, retry, or claim board execution."""
        # A has a single ordinary forwarding slot; pace bursts without B ACKs.
        with self._b_send_lock:
            delay = .1 - (time.monotonic() - self._last_b_send)
            if delay > 0 and opcode != 0x300:time.sleep(delay)
            p = self.send(2,opcode,data,expect_reply=False)
            self._last_b_send = time.monotonic()
        if p.error:raise BoardRequestError(p)
        return {'sent_only':True,'request_id':p.rid,'opcode':opcode}
    def receive(self, kind, payload):
        if self.ignore_b_replies and kind in (0x51,0x52) and len(payload)>=2 and payload[1]==2:
            return
        if kind in (0x51,0x52):
            wire_trace('board_rx_reply', kind=kind, payload_bytes=len(payload),
                       payload_hex=payload[:96].hex(), session_epoch=self.epoch)
        if kind not in (0x51,0x52) or len(payload) < 10: return
        ver,target,rid,op,status,size = struct.unpack_from('<BBHHHH',payload)
        if self.sparse_opcodes and target == 2:op &= 0x7fff
        if ver != 1 or len(payload) != size+10 or status > 8: return
        with self.lock:
            p = self.pending.get((target,rid,op))
            if p is None or p.event.is_set(): self.ignored += 1; return
            if kind == 0x51 and p.accepted: self.ignored += 1; return
            p.status = status
            if status not in (0,1):
                p.error = f'板端拒绝/失败 status={status}'; p.phase = 'failed'; p.event.set(); return
            if kind == 0x51:
                p.accepted = True
                if status == 1 or p.asynchronous:
                    p.phase = 'accepted'; return
            elif status != 0: return
            p.data = payload[10:]; p.phase = 'complete'; p.event.set()
    def expire(self):
        with self.lock:
            for p in self.pending.values():
                if not p.event.is_set() and self.clock() >= p.deadline:
                    p.error = '超时，结果未知；请读回状态，不自动重试'; p.phase = 'unknown'; p.event.set()
    def request(self, target, opcode, data=b'', timeout=5, **kw):
        p = self.send(target,opcode,data,timeout,**kw)
        while not p.event.wait(.03): self.expire()
        from gesture_tasks_customer import diagnostic
        if p.error:
            error = BoardRequestError(p)
            diagnostic('board_request', error, target=target, opcode=f'{opcode:04x}',
                       request_id=p.rid, phase=p.phase, status=p.status, request_bytes=len(data))
            raise error
        diagnostic('board_request', target=target, opcode=f'{opcode:04x}', request_id=p.rid,
                   phase=p.phase, response_hex=p.data.hex() if len(p.data)<=512 else p.data[:512].hex(), response_bytes=len(p.data))
        return p.data

    def request_b_when_idle(self, opcode, data=b'', timeout=15):
        """Send once to command-driven B. No polling or repeated busy requests."""
        epoch = self.epoch
        # BEGIN/rows use send_only; retain their ordinary inter-command gap
        # before the first acknowledged COMMIT as well.
        delay = .1 - (time.monotonic() - self._last_b_send)
        if delay > 0 and opcode != 0x300:
            time.sleep(delay)
        if self.epoch != epoch or not self.connected:
            raise ConnectionError('连接已改变，请重新操作')
        return self.request(2, opcode, data, timeout=timeout)

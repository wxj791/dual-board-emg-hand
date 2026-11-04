"""Explicit A user-Flash save. No serial connection, retries or idle polling."""
from __future__ import annotations
import json
import struct
import zlib
from gesture_tasks_protocol import frame

MAX_IMAGE = 61440


class ModelFlashError(ValueError):
    pass


def model_image(model):
    """Reuse the exporter and the original A loader byte for byte."""
    from pc_ml.gesture_tasks_export import commands
    image = bytearray()
    for index, (op, data) in enumerate(commands(model)):
        if op not in (0x102, 0x103, 0x104):
            raise ModelFlashError('模型镜像包含不支持的命令')
        payload = struct.pack('<BBHHH', 1, 1, 0x8000+index, op, len(data))+data
        image.extend(frame(0x50, payload, index))
    if not 0 < len(image) <= MAX_IMAGE:
        raise ModelFlashError('模型镜像超出A板Flash槽容量，尚未开始写入')
    return bytes(image)


def flash_state(raw):
    if len(raw) != 24:
        raise ModelFlashError('A板Flash状态长度无效，未确认保存')
    schema, flags, jedec, slot, classes, crc, seq, length, maximum = struct.unpack('<BB3sBHIIII', raw)
    if (schema != 1 or flags & ~31 or slot > 1 or maximum != MAX_IMAGE or
            flags & 2 and (not flags & 1 or not 2 <= classes <= 32 or not 0 < length <= maximum)):
        raise ModelFlashError('A板Flash状态无效，未确认保存')
    return dict(available=bool(flags & 1), saved=bool(flags & 2), boot_busy=bool(flags & 4),
                staging=bool(flags & 8), error=bool(flags & 16), jedec=jedec.hex(), slot=slot,
                classes=classes, model_crc32=f'{crc:08x}', sequence=seq, image_length=length,
                maximum=maximum)


def save_model(session, model):
    """Save only the already loaded model; ACK success means verified commit."""
    from gesture_tasks_deploy import a_state
    image = model_image(model)  # Validate before asking the board to erase anything.
    epoch = getattr(session, 'epoch', None)

    def request(op, data=b'', timeout=5):
        if getattr(session, 'epoch', None) != epoch:
            raise ConnectionError('连接已改变，本次Flash保存未确认')
        result = session.request(1, op, data, timeout=timeout, asynchronous=False)
        if getattr(session, 'epoch', None) != epoch:
            raise ConnectionError('连接已改变，本次Flash保存未确认')
        return result

    caps = json.loads(request(1))
    if caps.get('flash') is not True:
        raise ModelFlashError('当前A固件只支持断电丢失的RAM模型；需要先升级支持用户Flash的A固件')
    state = flash_state(request(0x10b))
    if not state['available'] or state['boot_busy']:
        raise ModelFlashError('A板用户Flash未就绪，本次未写入')
    loaded = a_state(request(2))
    crc = model['transaction_crc32']
    classes = model['active_classes']
    if (not loaded['valid'] or loaded['loading'] or not loaded['crc_valid'] or
            loaded['model_crc32'] != crc or loaded['class_count'] != classes):
        raise ModelFlashError('A板当前模型与所选模型不一致，请先上传对应模型再保存')
    request(0x109, struct.pack('<IIIHH', len(image), zlib.crc32(image), int(crc, 16), classes, 0), timeout=60)
    for offset in range(0, len(image), 256):
        request(0x10a, struct.pack('<I', offset)+image[offset:offset+256], timeout=5)
    request(0x106, timeout=15)
    saved = flash_state(request(0x10b))
    if (not saved['available'] or not saved['saved'] or saved['error'] or saved['staging'] or saved['boot_busy'] or
            saved['model_crc32'] != crc or saved['classes'] != classes or saved['image_length'] != len(image)):
        raise ModelFlashError('A板Flash保存结果未通过核对，尚未确认断电保留')
    return saved

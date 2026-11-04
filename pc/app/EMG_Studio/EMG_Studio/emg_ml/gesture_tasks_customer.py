"""Customer action responses, identity checks and local diagnostic logging.

The wire protocol remains in gesture_tasks_protocol. Old preview output IDs
are deliberately never interpreted as pose slots.
"""
from __future__ import annotations
import copy
from datetime import datetime
import json
import os
from pathlib import Path
import threading
import traceback
from gesture_tasks_protocol import Binding

RESPONSE_OPTIONS = {'不执行': (0, 0), **{f'回放已示教姿态 {i}': (1, i-1) for i in range(1, 10)},
    '显示上一页': (3, 0),
    '显示下一页': (4, 0), '显示波形放大': (5, 0), '显示波形缩小': (6, 0)}
_log_lock = threading.Lock()

def log_path():
    state = Path(os.environ.get('EMG_GESTURE_TASKS_STATE', str(Path(__file__).resolve().parents[1]/'runtime/gesture_tasks_session.json')))
    return state.with_name('gesture_tasks.log')

def diagnostic(step, error=None, **details):
    record = {'time': datetime.now().astimezone().isoformat(timespec='milliseconds'), 'step': step, **details}
    if error is not None:
        record.update(exception=repr(error), traceback=''.join(traceback.format_exception(type(error), error, error.__traceback__)))
    path = log_path()
    try:
        with _log_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str)+'\n')
    except OSError:
        # Logging must never turn a completed board command into a failed one.
        pass
    return path

def response_binding(index, response=None):
    r = response or {'kind': 0, 'argument': 0, 'enabled': False}
    # Retire saved collision actions when opening an older profile.
    if r.get('kind') == 2:r = {'kind': 0, 'enabled': False, 'argument': 0}
    row = Binding(index, r['kind'], r['enabled'], r['argument'])
    row.pack()
    return row

def response_value(text, rest=False):
    kind, argument = RESPONSE_OPTIONS[text]
    row = Binding(0 if rest else 1, kind, bool(kind), argument)
    row.pack()
    return {'kind': row.kind, 'enabled': row.enabled, 'argument': row.argument}

def response_text(response=None):
    row = response_binding(1, response)
    if not row.enabled: return '不执行'
    return next(text for text, value in RESPONSE_OPTIONS.items() if value == (row.kind, row.argument))

def action_identity(row):
    key = row.get('key')
    if key and key != 'custom': return 'action:'+key
    return 'custom:'+str(row.get('response_uid') or row['name']+'|'+row.get('instruction',''))

def model_actions(names, profile=None, identity_scope=''):
    if profile and names == [r['name'] for r in profile['classes']]:
        actions = copy.deepcopy(profile['classes'])
    else:
        # Older models have names only; they cannot inherit arbitrary preview IDs.
        actions = [{'id': i, 'key': 'rest' if i == 0 else 'custom', 'name': name,
                    'response_uid': 'legacy:'+identity_scope+':'+name,
                    'instruction': '自然放松' if i == 0 else '保持该训练动作',
                    'output': {'id': 0, 'name': '保持 / 停止触发'}} for i, name in enumerate(names)]
    identities = [action_identity(r) for r in actions]
    if len(set(identities)) != len(identities):
        raise ValueError('动作身份重复，请重新选择动作后训练')
    return actions

def reconcile_actions(actions, current):
    """Match stable identities, never numeric class positions; new actions stay off."""
    result = copy.deepcopy(actions)
    old = {}
    for row in current:
        identity = action_identity(row)
        if identity in old: raise ValueError('动作身份重复，请核对机械手响应')
        old[identity] = row
    for index, row in enumerate(result):
        source = old.get(action_identity(row))
        if source and 'response' in source:
            row['response'] = copy.deepcopy(source['response'])
        if index == 0: row['response'] = response_value('不执行')
        response_binding(index, row.get('response'))
    return result

def friendly_error(error, operation=''):
    if type(error).__name__=='ModelFlashError':return str(error)
    if getattr(error,'local_model_materials',False):return str(error)
    if type(error).__name__ in ('BindingSynchronizationError','ArmPreconditionError'):return str(error)
    if isinstance(error, ConnectionError): return '未连接A板，请先连接后重试'
    target = getattr(error, 'target', None)
    if target and getattr(error, 'phase', '') == 'unknown':
        return ('A板' if target == 1 else 'B板')+'未响应，请检查连接后查询状态'
    if target:
        if target==2 and getattr(error,'opcode',None)==0x600 and getattr(error,'status',None)==8:
            return 'A模型已更新，但B尚未确认新模型识别，动作绑定未完成；请保持采样后重试'
        if target==2 and getattr(error,'opcode',None)==0x602 and getattr(error,'status',None)==8:
            return 'B动作绑定表提交被拒绝（版本、完整性或CRC不一致），绑定尚未生效；无需因此重新训练'
        if target==2 and getattr(error,'opcode',None)==0x305 and getattr(error,'status',None)==5:
            return 'B板拒绝回放：请退出示教，核对所选姿态已保存及机械手反馈状态'
        if target==2 and getattr(error,'status',None)==6:
            return 'B板不支持此操作，请打开诊断日志核对板端版本'
        return ('A板' if target == 1 else 'B板')+'未完成操作，请打开诊断日志查看原因'
    if isinstance(error,OverflowError):
        return '本地模型转换未通过，尚未上传A板；可使用PC识别，详情请打开诊断日志'
    if operation == '上传': return '模型上传未完成，请检查连接并打开诊断日志'
    message = str(error)
    if isinstance(error, ValueError) and not any(s in message for s in ('CRC', 'generation', 'TASK_', 'opcode', '0x', '参数不属于')):
        return message
    return '操作未完成，请打开诊断日志查看原因'

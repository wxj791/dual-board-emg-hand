"""Verified deployment and B maintenance through the one A serial session."""
from __future__ import annotations
from dataclasses import asdict
import json
from pathlib import Path
import struct
import zlib
import re
import time
from gesture_tasks_protocol import BoardRequestError
from gesture_tasks_customer import diagnostic


class BindingSynchronizationError(ValueError):
    pass


class ArmPreconditionError(ValueError):
    """A definite, non-executed ARM with a successfully read-back task map."""
    preserve_verified_bindings = True
from gesture_tasks_protocol import Binding, rows_bytes, map_commands, task_state

def a_state(data):
    d=json.loads(data)
    if not isinstance(d,dict):raise ValueError('A状态格式无效')
    for key in ('valid','loading','model_crc32_valid'):
        if type(d.get(key)) is not int or d[key] not in (0,1):raise ValueError('A状态缺少有效的模型CRC/加载状态')
    for key,width in (('generation_hex',4),('active_hex',4),('model_crc32_hex',8)):
        if not isinstance(d.get(key),str) or re.fullmatch('[0-9a-f]{'+str(width)+'}',d[key]) is None:
            raise ValueError('A状态缺少规范模型CRC/版本字段')
    if d['valid']!=d['model_crc32_valid'] or d['loading'] and d['valid']:
        raise ValueError('A模型有效位/CRC/加载状态矛盾')
    result={'valid':bool(d['valid']), 'generation':int(d['generation_hex'],16),
            'class_count':int(d['active_hex'],16),'loading':bool(d['loading']),
            'model_crc32':d['model_crc32_hex'],'crc_valid':bool(d['model_crc32_valid'])}
    if not 0<=result['generation']<=65535 or not 2<=result['class_count']<=32: raise ValueError('A状态版本/类数无效')
    return result

class Deployment:
    def __init__(self, session, path, b_send_only=False):
        self.session=session; self.path=Path(path); self.verified=False
        self.b_send_only=b_send_only; self.b_mapping_sent=False; self.b_sent_epoch=None
        self.model=None; self.rows=[]; self.revision=0; self.generation=None
        self.verified_session_id=None
        self.a_verified=False; self.a_verified_session_id=None
        self.a_generation=None; self.a_model_crc32=None
        self.a_model_persistent=False; self.a_persistent_crc32=None
    def select(self, model):
        self.a_model_persistent=False; self.a_persistent_crc32=None
        self.b_mapping_sent=False
        self.restored_bindings=False
        names=model['class_names']
        if (type(model.get('active_classes')) is not int or model['active_classes']!=len(names) or
            not 2<=len(names)<=32 or any(not isinstance(n,str) or not n.strip() for n in names) or len(set(names))!=len(names)):
            raise ValueError('模型实际类别须为2～32个非空且唯一的名称')
        self.verified=False; self.a_verified=False; self.model=model; self.generation=None
        self.a_generation=None; self.a_model_crc32=None
        self.rows=[Binding(i) for i in range(len(names))]
        if self.path.is_file():
            saved=json.loads(self.path.read_text(encoding='utf8'))
            self.revision=int(saved.get('revision',0))
            if not 0<=self.revision<=0xffffffff: raise ValueError('本地revision超出u32范围')
            if saved.get('source_sha256')==model['source_sha256'] and saved.get('class_names')==names:
                rows=[Binding(**r) for r in saved['bindings']]; rows_bytes(rows)
                if len(rows)!=len(names): raise ValueError('保存任务表类别数错误')
                if saved.get('rows_crc32')!=f'{zlib.crc32(rows_bytes(rows)):08x}': raise ValueError('本地任务表CRC错误')
                self.rows=[Binding(r.class_id) if r.kind==2 else r for r in rows]
                self.restored_bindings=True
        self.save()
    def save(self):
        if self.model is None: return
        raw=rows_bytes(self.rows)
        saved={'version':1,'source_sha256':self.model['source_sha256'],
               'class_names':self.model['class_names'],'revision':self.revision,
               'generation':self.generation,'rows_crc32':f'{zlib.crc32(raw):08x}',
               'bindings':[asdict(r) for r in self.rows], 'deployment_status':(('b_commit_confirmed' if self.b_mapping_sent else 'b_not_committed') if self.b_send_only else 'requires_board_readback'),
               'a_model_persistent':self.a_model_persistent,'a_persistent_crc32':self.a_persistent_crc32,
               'b_task_map_persistent':'not_assumed',
               'pose_flash_independent':True}
        saved['action_identities']=self.model.get('action_identities')
        saved['a_model_path']=self.model.get('a_model_path')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        tmp=self.path.with_suffix('.tmp'); tmp.write_text(json.dumps(saved,ensure_ascii=False,indent=2)+'\n',encoding='utf8'); tmp.replace(self.path)
    def edit(self, rows):
        rows_bytes(rows)
        if not self.model or len(rows)!=self.model['active_classes']: raise ValueError('类别数不匹配')
        self.rows=list(rows); self.verified=False; self.b_mapping_sent=False; self.save()
    def save_to_flash(self):
        from gesture_tasks_flash import save_model, ModelFlashError
        self.a_model_persistent=False; self.a_persistent_crc32=None; self.save()
        if self.model is None:raise ModelFlashError('请先完成本轮模型导出和上传')
        result=save_model(self.session,self.model)
        self.a_model_persistent=True; self.a_persistent_crc32=result['model_crc32']; self.save()
        return result
    def readback(self, generation, revision):
        a=a_state(self.session.request(1,2)); b=task_state(self.session.request(2,0x604))
        k=len(self.rows)
        if (not a['valid'] or a['loading'] or not a['crc_valid'] or a['model_crc32']!=self.model['transaction_crc32'] or
            (a['generation'],a['class_count'])!=(generation,k) or
            not b['mapping_valid'] or (b['generation'],b['class_count'],b['map_revision'])!=(generation,k,revision)):
            raise RuntimeError('A/B模型或任务表版本不匹配；保持未确认/禁止ARM')
        for row in self.rows:
            if self.session.request(2,0x603,struct.pack('<H',row.class_id))!=row.pack():
                raise RuntimeError(f'B任务表读回不一致 class={row.class_id}')
        return b
    def upload(self):
        from pc_ml.gesture_tasks_export import commands
        if self.model is None: raise ValueError('请先完成本轮量化导出')
        self.verified=False; self.a_verified=False
        self.b_mapping_sent=False
        model_commands=list(commands(self.model))
        rows_bytes(self.rows)
        if len(self.rows)!=self.model['active_classes']: raise ValueError('模型和任务表类别数不一致')
        caps=json.loads(self.session.request(1,1))
        if caps.get('capacity',0)<len(self.rows): raise ValueError('A实际上限不足')
        old=a_state(self.session.request(1,2))
        if old['loading']:raise ValueError('A模型正在加载；禁止开始新事务')
        if self.b_send_only:
            self.session.send_only(0x300)
        else:
            bcaps=json.loads(self.session.request(2,1))
            if bcaps.get('max_classes',bcaps.get('capacity',0))<len(self.rows): raise ValueError('B任务表实际上限不足')
            if 'typed_tasks' in bcaps and any(r.enabled and r.kind not in bcaps['typed_tasks'] for r in self.rows):
                raise ValueError('任务表含B尚未实现的任务；不能先更新A再发现无法提交B表')
            self.session.request(2,0x300,timeout=15)
            stopped=task_state(self.session.request(2,0x604))
            if stopped['mechanical_armed'] or stopped['motion_state']!=0:
                raise RuntimeError('B尚未确认停止/撤销ARM，不能更新A')
        reuse=(old['valid'] and old['crc_valid'] and old['model_crc32']==self.model['transaction_crc32'] and
               old['class_count']==len(self.rows))
        if not reuse:
            for op,data in model_commands: self.session.request(1,op,data,timeout=10,asynchronous=False)
        new=a_state(self.session.request(1,2))
        expected=old['generation'] if reuse else (old['generation']+1)&65535
        if (not new['valid'] or new['loading'] or not new['crc_valid'] or
            new['model_crc32']!=self.model['transaction_crc32'] or
            (new['generation'],new['class_count'])!=(expected,len(self.rows))):
            raise RuntimeError('A提交后generation/class_count/模型CRC核对失败；禁止提交B表')
        self.generation=expected; self.revision=(self.revision+1)&0xffffffff; self.save()
        self.a_verified=True; self.a_verified_session_id=getattr(self.session,'epoch',None)
        self.a_generation=expected; self.a_model_crc32=new['model_crc32']
        diagnostic('deployment_a_confirmed',generation=expected,model_crc32=new['model_crc32'],reused=reuse)
        self.wait_for_classification(expected)
        mapping=list(map_commands(expected,self.revision,self.rows))
        if self.b_send_only:
            # Restore the pre-experiment send path: send BEGIN/rows once and
            # await COMMIT itself, without status queries or row readback.
            for op,data in mapping[:-1]:self.session.send_only(op,data)
            self.session.request_b_when_idle(*mapping[-1],timeout=3)
            self.b_mapping_sent=True;self.b_sent_epoch=getattr(self.session,'epoch',None)
            self.save()
            return {'b_commit_confirmed':True,'a_confirmed':True,'mapping_commands':len(mapping)}
        # Only a definite rejection of BEGIN is safe to retry. No row, COMMIT,
        # motion command, accepted request or unknown result is repeated.
        for attempt in range(3):
            try:
                self.session.request(2,*mapping[0]);break
            except BoardRequestError as exc:
                if not (exc.target==2 and exc.opcode==0x600 and exc.phase=='failed' and exc.status==8):raise
                diagnostic('binding_begin_not_synchronized',attempt=attempt+1,generation=expected)
                if attempt==2:
                    raise BindingSynchronizationError('A模型已上传并核对，但B尚未确认新模型识别，动作绑定未完成；请保持采样后重新上传确认（不会重复写入相同A模型）') from exc
                time.sleep(.25)
                self.wait_for_classification(expected)
        for op,data in mapping[1:]: self.session.request(2,op,data)
        b=self.readback(expected,self.revision)
        if b['mechanical_armed']: raise RuntimeError('B提交后未撤销ARM')
        self.verified=True; self.verified_session_id=getattr(self.session,'epoch',None); self.save()
        return b

    def wait_for_classification(self,generation,timeout=5):
        deadline=time.monotonic()+timeout
        epoch=getattr(self.session,'epoch',None)
        baseline=None
        while True:
            if getattr(self.session,'epoch',None)!=epoch:raise ConnectionError('A连接会话已改变')
            state=json.loads(self.session.request(1,2,timeout=2));a=a_state(json.dumps(state))
            if getattr(self.session,'epoch',None)!=epoch:raise ConnectionError('A连接会话已改变')
            if (not a['valid'] or a['loading'] or not a['crc_valid'] or a['generation']!=generation or
                a['class_count']!=len(self.rows) or a['model_crc32']!=self.model['transaction_crc32']):
                self.a_verified=False
                raise ValueError('A模型在等待识别期间发生变化，请重新核对')
            counter=state.get('results_hex')
            if not isinstance(counter,str) or re.fullmatch('[0-9a-f]{8}',counter) is None:
                raise BindingSynchronizationError('A模型已核对，但无法确认新模型识别是否恢复，未提交B动作绑定')
            current=int(counter,16)
            delta=0 if baseline is None else (current-baseline)&0xffffffff
            if delta>=0x80000000:
                raise BindingSynchronizationError('A分类计数发生倒退，无法确认实时识别；请检查采样连接后重试')
            if delta:
                # Give the existing A→B classification frame time to arrive.
                time.sleep(.15)
                if getattr(self.session,'epoch',None)!=epoch:raise ConnectionError('A连接会话已改变')
                diagnostic('deployment_classification_ready',generation=generation,results=current,previous_results=baseline)
                return
            if baseline is None:baseline=current
            if time.monotonic()>=deadline:
                raise BindingSynchronizationError('A模型已核对，但尚未恢复识别：实时分类计数未增长；请检查采集端供电、采样和无线连接后重试')
            time.sleep(.1)
    def arm(self):
        if self.b_send_only:
            if (not self.b_mapping_sent or self.generation is None or
                self.b_sent_epoch!=getattr(self.session,'epoch',None)):
                raise RuntimeError('请先在本次连接中上传模型并发送动作绑定')
            return self.session.request_b_when_idle(0x301,struct.pack('<H',self.generation))
        if (not self.verified or self.generation is None or
            self.verified_session_id!=getattr(self.session,'epoch',None)):
            raise RuntimeError('本会话尚未完整上传并读回；不能ARM')
        # Invalidate on read-back/identity/connection failure. Only definite local
        # preconditions or a rejected ARM may preserve this completed read-back.
        epoch=self.verified_session_id
        self.verified=False
        before=self.readback(self.generation,self.revision)
        if getattr(self.session,'epoch',None)!=epoch:raise ConnectionError('A连接会话已改变')
        if before['teach_mode'] or before['motion_state']!=0:
            self.verified=True
            raise ArmPreconditionError('请先停止机械手并退出示教，再启用；已核对的动作绑定保留')
        try:
            self.wait_for_classification(self.generation)
        except BindingSynchronizationError as exc:
            self.verified=True
            raise ArmPreconditionError(str(exc)+'；恢复后可直接再次启用，无需重复上传') from exc
        if getattr(self.session,'epoch',None)!=epoch:raise ConnectionError('A连接会话已改变')
        try:
            self.session.request(2,0x301,struct.pack('<H',self.generation))
        except BoardRequestError as exc:
            if exc.target==2 and exc.opcode==0x301 and exc.phase=='failed' and exc.status==5:
                self.verified=True
                raise ArmPreconditionError('B板拒绝启用：实时识别、五指在线、总线空闲或示教状态未满足；请检查状态后再次启用，已核对的动作绑定保留') from exc
            raise
        state=task_state(self.session.request(2,0x604))
        if not state['mechanical_armed']: raise RuntimeError('B未确认ARM')
        if getattr(self.session,'epoch',None)!=epoch:raise ConnectionError('A连接会话已改变')
        self.verified=True
        return state

def query_pose(session, slot):
    if type(slot) is not int or slot not in range(9): raise ValueError('槽应为1～9（内部0～8）')
    raw=session.request(2,0x30a,struct.pack('<H',slot))
    if len(raw)!=13 or any(raw[i] not in (0,1) for i in (0,11,12)): raise ValueError('未知POSE_QUERY格式')
    return {'valid':bool(raw[0]),'positions':list(struct.unpack_from('<5H',raw,1)),
            'flash_available':bool(raw[11]),'flash_initialized':bool(raw[12])}

def capture_and_verify(session, slot):
    if type(slot) is not int or slot not in range(9): raise ValueError('姿态槽范围为1～9')
    state=task_state(session.request(2,0x604))
    if not state['teach_mode'] or state['motion_state']!=0: raise RuntimeError('请先进入示教并等待总线空闲')
    session.request_b_when_idle(0x304,struct.pack('<H',slot),timeout=30)
    pose=query_pose(session,slot); state=task_state(session.request(2,0x604))
    if (not pose['valid'] or not pose['flash_available'] or not pose['flash_initialized'] or
        not state['flash_ready'] or state['fault_flags'] or not state['pose_valid_mask']&(1<<slot)):
        raise RuntimeError('捕获结束但Flash/槽有效性未确认；不能声明已保存')
    return pose

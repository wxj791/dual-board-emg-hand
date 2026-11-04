"""Customer flow built around the trained actions. No changes to A/B wire formats."""
from __future__ import annotations
import copy
import json
import os
from pathlib import Path
import queue
import struct
import threading
import time
import tkinter as tk
from tkinter import ttk
from ui_style import PANEL, PLOT, FG, label, button, check, open_path
from gesture_tasks_protocol import Binding, RANGES, task_state, range_command
from gesture_tasks_deploy import Deployment, a_state, query_pose, capture_and_verify
from gesture_tasks_materials import validated_deployment, readonly_identities, require_export_inputs
from gesture_tasks_customer import (RESPONSE_OPTIONS, response_text, response_value, response_binding,
    model_actions, reconcile_actions, action_identity, diagnostic, log_path, friendly_error)


class GestureTasksPanel:
    def __init__(self, app):
        self.app=app; self.events=queue.Queue(); self.busy=0
        self.source_key=None; self.state=None; self.model=None; self.actions=[]
        self.querying=set(); self.last_query={1:0,2:0}; self.endpoint_epoch=app.core.board_session.epoch
        self.query_generation=0
        self.endpoint={1:None,2:None}; self.endpoint_error={1:None,2:None}; self.endpoint_time={1:0,2:0}
        self.recognition_enabled=False
        self.display_pending={}; self.last_display_id=None
        state_path=Path(os.environ.get('EMG_GESTURE_TASKS_STATE',str(Path(__file__).resolve().parents[1]/'runtime/gesture_tasks_session.json')))
        self.log_path=log_path(); self.deploy=Deployment(app.core.board_session,state_path,b_send_only=True)
        app.core.board_session.ignore_b_replies=False
        self.status=tk.StringVar(value='先选择动作并训练，再上传和启用')
        self.a_text=tk.StringVar(value='A板：未连接'); self.board_text=tk.StringVar(value='B板：等待舵机操作反馈')
        self.class_text=tk.StringVar(value='等待A板识别'); self.model_text=tk.StringVar(value='请先完成本轮训练')
        self.auto_upload=tk.BooleanVar(value=False)
        app.report_callback_exception=lambda kind,error,trace:self.report_error('界面操作',error.with_traceback(trace))
        tab=self.tab=tk.Frame(app.notebook,bg=PANEL,padx=12,pady=8)
        app.notebook.add(tab,text='机械手')
        for var in (self.a_text,self.board_text):
            label(tab,variable=var,anchor='w',justify='left',wraplength=460).pack(fill='x',pady=(0,4))
        tabs=self.tabs=ttk.Notebook(tab); tabs.pack(fill='both',expand=True,pady=(2,4))
        self.bind_tab=tk.Frame(tabs,bg=PANEL,padx=10,pady=10)
        self.teach_tab=tk.Frame(tabs,bg=PANEL,padx=10,pady=10)
        self.display_tab=tk.Frame(tabs,bg=PANEL,padx=10,pady=10)
        for frame,name in ((self.bind_tab,'使用'),(self.teach_tab,'姿态示教'),(self.display_tab,'显示')): tabs.add(frame,text=name)
        self.build_bindings(); self.build_teach(); self.build_display()
        label(tab,variable=self.status,wraplength=460,justify='left',anchor='w').pack(fill='x',pady=(0,4))
        line=tk.Frame(tab,bg=PANEL); line.pack(fill='x')
        button(line,'开发诊断',self.open_diagnostics).pack(side='left')
        button(line,'打开诊断日志',self.open_log).pack(side='right')
        tab.bind('<Configure>',lambda e:self.wrap_labels(tab,max(280,e.width-28)))
        app.after(150,self.poll)

    @staticmethod
    def wrap_labels(widget,width):
        for child in widget.winfo_children():
            if isinstance(child,tk.Label): child.configure(wraplength=width)
            elif isinstance(child,tk.Frame): GestureTasksPanel.wrap_labels(child,max(240,width-20))

    def build_bindings(self):
        tab=self.bind_tab
        label(tab,'训练动作 → 机械手响应',size=16,bold=True).pack(anchor='w',pady=(0,10))
        label(tab,variable=self.model_text,wraplength=440,justify='left').pack(fill='x',pady=(0,8))
        button(tab,'设置机械手响应',self.edit_responses).pack(fill='x',pady=4)
        uploads=tk.Frame(tab,bg=PANEL);uploads.pack(fill='x',pady=4)
        button(uploads,'核对A现有模型',self.start_check_a).pack(side='left',fill='x',expand=True,padx=(0,4))
        button(uploads,'上传模型并发送绑定',self.start_upload,primary=True).pack(side='left',fill='x',expand=True,padx=(4,0))
        button(tab,'保存模型到A板（断电保留）',self.start_save_flash).pack(fill='x',pady=4)
        check(tab,'训练完成后上传模型并发送绑定',self.auto_upload).pack(anchor='w',pady=4)
        label(tab,'上传完成后，点击启用才会允许机械手响应。',muted=True,wraplength=440,justify='left').pack(fill='x',pady=8)
        controls=tk.Frame(tab,bg=PANEL);controls.pack(fill='x',pady=4)
        button(controls,'启用机械手',self.start_arm,primary=True).pack(side='left',fill='x',expand=True,padx=(0,4))
        button(controls,'停止并卸力',lambda:self.launch('停止',self.stop,priority=True),danger=True).pack(side='left',fill='x',expand=True,padx=(4,0))
        label(tab,variable=self.class_text,wraplength=440,justify='left').pack(fill='x',pady=10)

    def build_teach(self):
        tab=self.teach_tab
        label(tab,'进入示教并卸力后，摆好姿态再保存。',wraplength=440,justify='left').pack(fill='x',pady=(0,6))
        self.teach_slot=tk.StringVar(value='1')
        line=tk.Frame(tab,bg=PANEL); line.pack(fill='x',pady=4)
        label(line,'姿态槽').pack(side='left')
        ttk.Combobox(line,textvariable=self.teach_slot,values=list(map(str,range(1,10))),state='readonly',width=5).pack(side='left',padx=10)
        grid=tk.Frame(tab,bg=PANEL); grid.pack(fill='x',pady=6)
        for c in (0,1):grid.columnconfigure(c,weight=1,uniform='teach')
        actions=(('进入示教',lambda s:self.teach_mode(True)),('全部卸力',lambda s:self.stop()),
            ('记录并保存',self.record_pose),
            ('回放所选姿态',lambda s:self.command(0x305,struct.pack('<H',s),30)),('退出示教',lambda s:self.teach_mode(False)))
        for i,(title,fn) in enumerate(actions):
            button(grid,title,lambda t=title,f=fn:self.start_slot(t,f)).grid(row=i//2,column=i%2,sticky='ew',padx=3,pady=3)
        self.pose_text=tk.StringVar(value='选择姿态槽；操作完成后显示B板执行结果')
        label(tab,variable=self.pose_text,wraplength=440,justify='left',anchor='w').pack(fill='x',pady=6)
        label(tab,'姿态操作等待B板执行结果；翻页直接发送。',muted=True,wraplength=440).pack(fill='x',pady=4)
        label(tab,'退出示教后需要重新启用机械手。',muted=True,wraplength=440).pack(fill='x',pady=6)

    def build_display(self):
        tab=self.display_tab
        self.pc_text=tk.StringVar(); label(tab,variable=self.pc_text,wraplength=440,justify='left').pack(fill='x',pady=(0,8))
        label(tab,'B板显示手势由物理拨码控制；这里的量程只改变B板画面。',wraplength=440,justify='left').pack(fill='x',pady=8)
        self.page=tk.StringVar(value='滤波'); self.channel=tk.StringVar(value='全部'); self.mv=tk.StringVar(value='200')
        self.pages=('原始','滤波','快包络','慢包络')
        fields=tk.Frame(tab,bg=PANEL);fields.pack(fill='x',pady=4);fields.columnconfigure(1,weight=1)
        for i,(name,var,values) in enumerate((('波形页',self.page,self.pages),('通道',self.channel,('全部',*map(str,range(1,9)))),('量程 / mV',self.mv,RANGES))):
            label(fields,name).grid(row=i,column=0,sticky='w',padx=(0,12),pady=5)
            ttk.Combobox(fields,textvariable=var,values=list(values),state='readonly').grid(row=i,column=1,sticky='ew',pady=5)
        button(tab,'设置B板量程',self.set_range).pack(fill='x',pady=10)
        line=tk.Frame(tab,bg=PANEL); line.pack(fill='x')
        button(line,'上一页',lambda:self.send_display('上一页',0x201,b'\xff')).pack(side='left',fill='x',expand=True,padx=(0,4))
        button(line,'下一页',lambda:self.send_display('下一页',0x201,b'\x01')).pack(side='left',fill='x',expand=True,padx=(4,0))

    def sync_model(self,force=False):
        field=self.app.field_training; key=(str(field.package),field.parameter_sha256)
        if key==self.source_key and not force:return
        if field.package is None:return
        path=field.package.parent/'a_deployment/a_model.json'
        if not path.is_file() and self.deploy.path.is_file():
            saved=json.loads(self.deploy.path.read_text(encoding='utf8'))
            if saved.get('source_sha256')==field.parameter_sha256 and saved.get('a_model_path'):path=Path(saved['a_model_path'])
        model=validated_deployment(path,field.parameter_sha256,list(field.class_names)) if path.is_file() else {
            'class_names':list(field.class_names),'active_classes':len(field.class_names),'source_sha256':field.parameter_sha256}
        if model['source_sha256']!=field.parameter_sha256 or model['class_names']!=field.class_names:
            raise ValueError('模型与本轮训练动作不一致，请重新训练')
        actions=model_actions(list(field.class_names),field.gesture_profile,field.parameter_sha256)
        # The customer's current selection has priority for matching identities.
        # Class indices are never used to carry responses across retraining.
        model['action_identities']=[action_identity(r) for r in actions]
        self.deploy.select(model)
        if self.deploy.restored_bindings:
            for action,row in zip(actions,self.deploy.rows):
                action['response']={'kind':row.kind,'enabled':row.enabled,'argument':row.argument}
        actions=reconcile_actions(actions,self.app.gesture_profile['classes'])
        if any('response' in r for r in actions[1:]):
            self.deploy.edit([response_binding(i,r.get('response')) for i,r in enumerate(actions)])
        for action,row in zip(actions,self.deploy.rows):
            action['response']={'kind':row.kind,'enabled':row.enabled,'argument':row.argument}
        self.actions=actions; self.model=model; self.source_key=key
        self.recognition_enabled=False
        self.update_recognition_controls()
        self.model_text.set(f'本轮 {len(actions)-1} 个动作＋静息，机械手响应已按动作核对')
        diagnostic('model_selected',source_sha256=field.parameter_sha256,actions=actions)

    def profile_changed(self):
        if not self.model:return
        if self.busy:raise ValueError('请等待上传或机械手操作结束后修改动作')
        updated=reconcile_actions(self.actions,self.app.gesture_profile['classes'])
        rows=[response_binding(i,r.get('response')) for i,r in enumerate(updated)]
        if rows!=self.deploy.rows:
            self.deploy.edit(rows);self.actions=updated
            self.status.set('机械手响应已更新，请重新上传模型并发送绑定')

    def edit_responses(self):
        try:
            if self.busy:raise ValueError('请等待当前操作完成')
            self.sync_model()
            if not self.model:
                self.app.open_gesture_profile();return
            dialog=ResponseDialog(self)
            return dialog
        except Exception as exc:self.report_error('设置响应',exc)

    def save_responses(self,actions):
        rows=[response_binding(i,r.get('response')) for i,r in enumerate(actions)]
        if [action_identity(r) for r in actions]!=self.model['action_identities']:
            raise ValueError('训练动作已改变，请重新打开机械手响应')
        # Use the original action profile as the durable customer setting.
        profile={'version':1,'name':'本轮训练动作','classes':copy.deepcopy(actions)}
        from gesture_config import save_profile
        save_profile(profile)
        self.app.gesture_profile=profile
        self.app._rebuild_gesture_choices()
        self.app.field_training.gesture_profile=copy.deepcopy(profile)
        self.actions=copy.deepcopy(actions);self.deploy.edit(rows)
        self.status.set('机械手响应已保存，请上传模型并发送绑定')
        diagnostic('responses_saved',actions=actions)

    def on_trained(self):
        try:
            self.sync_model()
            self.status.set('训练完成，响应已核对，请上传模型并发送绑定')
            if self.auto_upload.get():self.app.after(0,self.start_upload)
        except Exception as exc:self.report_error('训练动作核对',exc)

    def start_slot(self,title,fn):
        slot=int(self.teach_slot.get())-1;self.launch(title,lambda:fn(slot))

    def export_model(self,package):
        import numpy as np
        from pc_ml.gesture_tasks_export import export_current
        require_export_inputs(package)
        with np.load(package.parent/'prepared.npz',allow_pickle=False) as z:d={k:z[k] for k in z.files}
        model=export_current(package,d,package.parent/f'a_deployment_{time.time_ns()}')
        return model

    def start_export(self):
        package=self.app.field_training.package
        if package is None:self.status.set('请先完成本轮训练');return
        self.launch('离线导出',lambda:self.export_model(Path(package)))

    def start_upload(self):
        try:
            if self.busy:raise ValueError('请等待当前操作完成')
            if self.app.field_training.busy:raise ValueError('请等待本轮训练完成')
            if not self.deploy.session.connected:raise ConnectionError('未连接A板')
            self.sync_model()
            if not self.model:raise ValueError('请先完成本轮训练')
            self.app.field_training.stop_live()
            package=Path(self.app.field_training.package)
            model=copy.deepcopy(self.model); rows=list(self.deploy.rows)
            if 'banks' not in model:require_export_inputs(package)
            def upload():
                if 'banks' not in model:
                    exported=self.export_model(package)
                    exported['action_identities']=model['action_identities']
                    self.deploy.select(exported);self.deploy.edit(rows)
                return self.deploy.upload()
            self.launch('上传',upload)
        except Exception as exc:self.report_error('上传',exc)

    def start_save_flash(self):
        try:
            if self.busy or self.app.field_training.busy:raise ValueError('请等待当前操作完成')
            if not self.deploy.session.connected:raise ConnectionError('未连接A板')
            self.sync_model()
            if not self.deploy.model or 'banks' not in self.deploy.model:
                from gesture_tasks_flash import ModelFlashError
                raise ModelFlashError('请先上传本轮模型，再保存到A板Flash')
            self.launch('保存A模型',self.deploy.save_to_flash)
        except Exception as exc:self.report_error('保存A模型',exc)

    def start_check_a(self):
        """Read-only identity query: no B request, model writes, STOP or ARM."""
        try:
            if self.busy or self.app.field_training.busy:raise ValueError('请等待当前操作完成')
            if not self.deploy.session.connected:raise ConnectionError('未连接A板')
            self.sync_model()
            if not self.model:raise ValueError('请先载入模型及训练动作')
            package=Path(self.app.field_training.package);model=copy.deepcopy(self.model)
            # A shipped factory identity can be checked without calibration inputs.
            known=readonly_identities(package,model['source_sha256'],model['class_names'],model)
            def check_a():
                # Query first. Different class counts prove a mismatch without
                # requiring calibration data or an otherwise unrelated export.
                state=json.loads(self.deploy.session.request(1,2,timeout=3))
                actual=a_state(json.dumps(state))
                local=model
                if not known and actual['class_count']==len(model['class_names']) and actual['valid'] and not actual['loading']:
                    require_export_inputs(package)
                    local=self.export_model(package)
                    local['action_identities']=model['action_identities']
                return {'a_model':local,'a_status':state}
            self.launch('核对A模型',check_a)
        except Exception as exc:self.report_error('核对A模型',exc)

    def start_arm(self):
        if not self.deploy.b_mapping_sent:
            self.status.set('请先上传模型并发送动作绑定，再发送启用指令');return
        self.launch('启用',lambda:self.command(0x301,struct.pack('<H',self.deploy.generation)))

    def command(self,op,data=b'',timeout=15):
        if op in (0x200,0x201,0x202,0x203,0x204):
            return self.deploy.session.send_only(op,data)
        self.deploy.session.request_b_when_idle(op,data,timeout=timeout)
        return {'command_completed':True,'opcode':op}
    def stop(self):return self.command(0x300)
    def teach_mode(self,enabled):
        return self.command(0x605,bytes([enabled]))
    def record_pose(self,slot):
        """Save the user's positioned hand, completing the required teach flow."""
        if type(slot) is not int or slot not in range(9):
            raise ValueError('姿态槽范围为1～9')
        session=self.deploy.session
        epoch=session.epoch
        session.request_b_when_idle(0x605,b'\x01',timeout=15)
        time.sleep(.1)
        if session.epoch!=epoch:raise ConnectionError('连接已改变，请重新操作')
        session.request_b_when_idle(0x304,struct.pack('<H',slot),timeout=30)
        time.sleep(.1)
        if session.epoch!=epoch:raise ConnectionError('连接已改变，请重新操作')
        session.request_b_when_idle(0x605,b'\x00',timeout=3)
        time.sleep(.1)
        if session.epoch!=epoch:raise ConnectionError('连接已改变，请重新操作')
        pose=query_pose(session,slot)
        if not (pose['valid'] and pose['flash_available'] and pose['flash_initialized']):
            raise RuntimeError('姿态保存尚未确认')
        return pose
    def set_range(self):
        data=range_command(self.pages.index(self.page.get()),255 if self.channel.get()=='全部' else int(self.channel.get())-1,int(self.mv.get()))
        self.send_display('设置量程',0x204,data)

    def send_display(self,title,op,data):
        """Send once without B status/model checks or an ACK wait in busy.

        Only display operations use this path. Mechanical operations use their
        own completion replies. Optional display ACKs are logged later.
        """
        try:
            if op not in (0x201,0x204):raise ValueError('此入口只允许显示命令')
            diagnostic('display_command_clicked',operation=title,opcode=f'{op:04x}')
            session=self.deploy.session
            pending=session.send(2,op,data,timeout=2,asynchronous=False)
            if pending.error:raise OSError(pending.error)
            self.display_pending[pending.rid]=(session.epoch,title,pending)
            self.last_display_id=pending.rid
            self.status.set(title+'命令已发送，未等待B核对；请观察B板屏幕')
            diagnostic('display_command_sent',operation=title,request_id=pending.rid,
                session_epoch=session.epoch,opcode=f'{op:04x}',board_confirmed=False)
        except Exception as exc:self.report_error(title,exc)

    def poll_display(self):
        session=self.deploy.session
        if self.display_pending:session.expire()
        for rid,(epoch,title,pending) in list(self.display_pending.items()):
            if epoch!=session.epoch:
                del self.display_pending[rid];continue
            if not pending.event.is_set():continue
            del self.display_pending[rid]
            diagnostic('display_command_reply',operation=title,request_id=rid,
                session_epoch=epoch,phase=pending.phase,status=pending.status,error_text=pending.error)
            if pending.phase=='failed' and rid==self.last_display_id and not self.busy:
                self.status.set(title+'被板端拒绝，请打开诊断日志查看原因')
    def query_endpoint(self,target,epoch,generation):
        try:
            raw=self.deploy.session.request(target,2 if target==1 else 0x604,timeout=2)
            state=json.loads(raw) if target==1 else task_state(raw)
            if not isinstance(state,dict):raise ValueError('板状态格式错误')
            self.events.put(('endpoint',(epoch,generation,target,state,None)))
        except Exception as exc:
            diagnostic('query_endpoint',exc,target=target)
            self.events.put(('endpoint',(epoch,generation,target,None,exc)))
            if target==2 and self.deploy.session.connected and self.deploy.session.epoch==epoch:
                self.query_bridge_after_timeout(epoch)

    def query_bridge_after_timeout(self,epoch):
        # Read-only A diagnostic through the existing reader/session. No B retry.
        now=time.monotonic()
        if now-getattr(self,'last_bridge_diagnostic',-30)<30:return
        self.last_bridge_diagnostic=now
        try:
            raw=self.deploy.session.request(1,2,bytes([2]),timeout=2)
            if self.deploy.session.epoch!=epoch:return
            state=json.loads(raw)
            value=state.get('bridge_v1_hex') if isinstance(state,dict) else None
            if not isinstance(value,str) or len(value)!=64:raise ValueError('A桥接诊断格式无效')
            bits=int(value,16)
            if bits&0xffffffff!=0x31544741:raise ValueError('A桥接诊断标识无效')
            diagnostic('bridge_after_b_timeout',session_epoch=epoch,bridge_hex=value,
                b_return_errors=(bits>>224)&0xffffffff,queue_state=(bits>>32)&15,
                cancelled=(bits>>48)&65535,motion_pending=bool((bits>>36)&1),
                usb_stream_drops=(bits>>64)&65535,usb_stream_queued=(bits>>96)&31)
        except Exception as exc:
            diagnostic('bridge_after_b_timeout_failed',exc,session_epoch=epoch)

    def query(self,targets=(1,)):
        # Separate workers and separate results: B timeout cannot hide A status
        # or occupy the foreground operation slot. Existing opcodes are unchanged.
        session=self.deploy.session
        if not session.connected or self.busy:return
        for target in targets:
            if target not in self.querying:
                self.querying.add(target);self.last_query[target]=time.monotonic()
                threading.Thread(target=self.query_endpoint,args=(target,session.epoch,self.query_generation),daemon=True).start()

    def launch(self,title,fn,priority=False):
        if self.busy and not priority:self.status.set('请等待当前操作完成；停止始终可用');return
        if title!='离线导出' and not self.deploy.session.connected:
            self.report_error(title,ConnectionError('未连接A板'));return
        self.busy+=1;self.status.set(title+'中，请稍候')
        self.query_generation+=1
        diagnostic('operation_started',operation=title)
        epoch=self.deploy.session.epoch
        def worker():
            try:self.events.put(('result',(epoch,title,fn())))
            except Exception as exc:
                diagnostic(title,exc);self.events.put(('error',(epoch,title,exc)))
            finally:self.events.put(('idle',None))
        threading.Thread(target=worker,daemon=True).start()

    def report_error(self,title,error):
        diagnostic(title,error)
        self.status.set(friendly_error(error,title))
        if title in ('上传','启用') and not (title=='启用' and getattr(error,'preserve_verified_bindings',False)):
            self.deploy.verified=False

    def apply_state(self,state):
        self.state=state;self.endpoint[2]=state;self.endpoint_time[2]=time.monotonic();self.endpoint_error[2]=None
        motion=('已停止','姿态回放中','未知动作','保存中')[state['motion_state']]
        mv='混合量程' if state['range_mv']==65535 else str(state['range_mv'])+' mV'
        mode='示教中' if state['teach_mode'] else '已启用' if state['mechanical_armed'] else '未启用'
        self.board_text.set(f'B板：已响应 · {mode} · {motion}'+(' · 有故障，请看日志' if state['fault_flags'] else ''))
        self.b_display=f'B板显示：第{state["page"]+1}页 · {mv} · 显示手势'+('开启' if state['display_enabled'] else '关闭')
        if self.deploy.verified and (not state['mapping_valid'] or
            (state['generation'],state['class_count'],state['map_revision'])!=(self.deploy.generation,len(self.deploy.rows),self.deploy.revision)):
            self.deploy.verified=False;self.status.set('板端配置已改变，请重新上传并确认')

    def verify_a_identity(self,state):
        try:a=a_state(json.dumps(state))
        except (ValueError,KeyError,TypeError) as exc:
            diagnostic('a_identity_unavailable',exc)
            self.deploy.a_verified=False;self.deploy.verified=False;return False
        expected=self.deploy.model or {}
        try:
            identities=readonly_identities(self.app.field_training.package or Path('.'),
                expected.get('source_sha256'),expected.get('class_names'),expected)
        except (ValueError,KeyError,TypeError) as exc:
            diagnostic('a_identity_binding',exc);identities=[]
        matched=(a['valid'] and not a['loading'] and a['crc_valid'] and
            any(a['model_crc32']==entry['model_crc32'] for entry in identities) and a['class_count']==len(self.actions))
        self.deploy.a_verified=bool(matched)
        if self.deploy.verified and (not matched or a['generation']!=self.deploy.generation or
            a['model_crc32']!=expected.get('transaction_crc32')):
            self.deploy.verified=False;self.status.set('A板模型已改变，请重新上传并确认')
        self.deploy.a_generation=a['generation'] if matched else None
        self.deploy.a_model_crc32=a['model_crc32'] if matched else None
        self.deploy.a_verified_session_id=self.deploy.session.epoch if matched else None
        return matched

    def render_connections(self):
        core=self.app.core;now=time.monotonic();session=self.deploy.session
        if not session.connected:
            self.deploy.verified=False;self.deploy.a_verified=False
            self.deploy.b_mapping_sent=False
            self.a_text.set('A板：未连接');self.board_text.set('B板：尚未连接A板');return
        raw=core.last_data_time is not None and now-core.last_data_time<3
        classified=bool(core.board_classification and now-core.board_classification_time<3)
        queried=now-self.endpoint_time[1]<5
        if raw or classified or queried:
            suffix='采样和分类正常' if raw and classified else '正在接收采样' if raw else '正在接收分类' if classified else '已响应'
            if self.endpoint_error[1]:suffix+='，配置查询未完成'
            self.a_text.set('A板：'+suffix)
        else:self.a_text.set('A板：未响应' if self.endpoint_error[1] else 'A板：等待响应')
        if self.endpoint_error[2]:self.board_text.set('B板：未响应；显示命令仍可发送')
        elif self.endpoint[2] is None:self.board_text.set('B板：'+getattr(self,'last_b_command','等待舵机操作反馈'))

    def render_recognition(self):
        core=self.app.core;p=core.board_classification
        good=(self.deploy.session.connected and p is not None and len(p)==32 and
              time.monotonic()-core.board_classification_time<3 and p[1]==3)
        text='等待A板识别';response=''
        if good:
            cid=int.from_bytes(p[2:4],'little');generation=int.from_bytes(p[12:14],'little');k=int.from_bytes(p[14:16],'little')
            if (self.deploy.a_verified and self.deploy.a_verified_session_id==self.deploy.session.epoch and
                generation==self.deploy.a_generation and k==len(self.actions) and cid<k):
                text=self.actions[cid]['name'];response=response_text(self.actions[cid].get('response'))
            else:
                text='已收到A板分类，请核对A现有模型'
                if self.deploy.a_verified and generation!=self.deploy.a_generation:
                    self.deploy.a_verified=False;self.deploy.verified=False
        self.class_text.set('A板识别：'+text)
        if core.board_protocol and self.recognition_enabled and not self.pc_selected():
            self.app.lbl_pred_gesture.configure(text=f'【{text}】' if response else text,fg=FG)
            self.app.lbl_pred_conf.configure(text='识别来源：A板')
            self.app.lbl_pred_output.configure(text='机械手响应：'+response if response else '等待模型与训练动作核对')

    def pc_selected(self):
        return not self.app.core.board_protocol or self.app.recognition_source.get()=='PC识别'

    def update_recognition_controls(self):
        pc=self.pc_selected()
        active=self.app.field_training.live_generation is not None if pc else self.recognition_enabled
        text=('停止PC识别' if active else '开始PC识别') if pc else ('暂停识别显示' if active else '查看A板识别')
        self.app.infer_toggle.configure(text=text)
        self.app.field_training.live_button.configure(text=text)

    def change_recognition_source(self):
        # Stop and invalidate the old worker before showing the new source.
        # Switching never sends a board request or changes mechanical control.
        self.app.field_training.stop_live()
        self.recognition_enabled=False
        self.app.infer_active_var.set(False)
        self.update_recognition_controls()
        pc=self.pc_selected()
        self.app.lbl_pred_gesture.configure(text='等待开始PC识别' if pc else '等待查看A板识别',fg=FG)
        self.app.lbl_pred_conf.configure(text='识别来源：PC' if pc else '识别来源：A板')
        self.app.lbl_pred_output.configure(text='PC结果仅供预览，机械手仍由A/B控制' if pc else '')
        self.app.recognition_note.set('使用当前模型和A板原始采样；机械手仍由A/B控制。' if pc else '显示A板返回的动作；请先核对A现有模型。')
        diagnostic('recognition_source_changed',source='PC' if pc else 'A')

    def toggle_recognition(self):
        if self.pc_selected():
            self.recognition_enabled=False
            return self.app.field_training.toggle_pc_live()
        if not self.deploy.session.connected:
            self.report_error('识别',ConnectionError('未连接A板'));return
        self.app.field_training.stop_live()
        self.recognition_enabled=not self.recognition_enabled
        self.update_recognition_controls()
        if not self.recognition_enabled:
            self.app.lbl_pred_gesture.configure(text='识别显示已暂停');self.app.lbl_pred_conf.configure(text='识别来源：A板')
            self.app.lbl_pred_output.configure(text='机械手启用和停止请在机械手页操作')
        else:self.render_recognition()

    def open_log(self):
        try:
            diagnostic('open_log');open_path(self.log_path)
        except OSError as exc:self.report_error('打开日志',exc)

    def open_diagnostics(self):
        dialog=tk.Toplevel(self.app);dialog.title('开发诊断');dialog.configure(bg=PANEL);dialog.geometry('780x600')
        label(dialog,'日志：'+str(self.log_path),wraplength=740,justify='left').pack(fill='x',padx=14,pady=10)
        mode=tk.StringVar(value='A帧协议' if self.app.core.board_protocol else '原JustFloat采集')
        combo=ttk.Combobox(dialog,textvariable=mode,values=('A帧协议','原JustFloat采集'),state='readonly');combo.pack(fill='x',padx=14)
        def change(event=None):
            if self.app.core.running or self.busy:
                mode.set('A帧协议' if self.app.core.board_protocol else '原JustFloat采集');return
            self.app.core.board_protocol=mode.get()=='A帧协议'
        combo.bind('<<ComboboxSelected>>',change)
        label(dialog,'训练算法（客户流程默认使用可上传的统计＋时序CNN）').pack(anchor='w',padx=14,pady=(10,3))
        from field_training import CNN_FUSION_MODE,FUSION_MODE,LEGACY_MODE
        ttk.Combobox(dialog,textvariable=self.app.field_training.training_mode,values=(CNN_FUSION_MODE,FUSION_MODE,LEGACY_MODE),state='readonly').pack(fill='x',padx=14)
        core=self.app.core
        info={'A_status':self.endpoint[1],'B_status':self.endpoint[2], 'A_error':str(self.endpoint_error[1]),'B_error':str(self.endpoint_error[2]),
            'raw_type':core.board_raw_type,'received':core.total_received,'breaks':core.board_stream_breaks,
            'model':self.model,'B_readback_verified':self.deploy.verified,'A_identity_verified':self.deploy.a_verified,
            'recognition_source':'PC' if self.pc_selected() else 'A','PC_running':self.app.field_training.live_generation is not None,
            'A_identity_crc32':self.deploy.a_model_crc32,'A_identity_generation':self.deploy.a_generation}
        text=tk.Text(dialog,bg=PLOT,fg=FG,wrap='word',height=12);text.pack(fill='both',expand=True,padx=14,pady=10)
        text.insert('1.0',json.dumps(info,ensure_ascii=False,indent=2));text.configure(state='disabled')
        button(dialog,'离线导出当前模型',self.start_export).pack(side='left',padx=14,pady=10)
        button(dialog,'刷新A板状态',self.query).pack(side='left',padx=4,pady=10)
        button(dialog,'打开诊断日志',self.open_log).pack(side='right',padx=14,pady=10)
        return dialog

    def poll(self):
        try:
            if not self.busy:self.sync_model()
            session=self.deploy.session
            if session.epoch!=self.endpoint_epoch:
                self.endpoint_epoch=session.epoch;self.querying.clear()
                self.last_b_command='等待舵机操作反馈'
                self.endpoint={1:None,2:None};self.endpoint_error={1:None,2:None};self.endpoint_time={1:0,2:0};self.last_query={1:0,2:0}
                self.deploy.verified=False;self.deploy.a_verified=False
                self.deploy.b_mapping_sent=False
                self.display_pending.clear();self.last_display_id=None
            while True:
                try:kind,value=self.events.get_nowait()
                except queue.Empty:break
                if kind=='idle':self.busy=max(0,self.busy-1)
                elif kind=='endpoint':
                    epoch,generation,target,state,error=value
                    if epoch!=session.epoch:continue
                    self.querying.discard(target)
                    if generation!=self.query_generation:continue
                    self.endpoint_error[target]=error
                    if error is None:
                        self.endpoint[target]=state;self.endpoint_time[target]=time.monotonic()
                        if target==1:self.verify_a_identity(state)
                        else:self.apply_state(state)
                elif kind in ('error','result'):
                    epoch,title,result=value
                    if epoch!=session.epoch:continue
                    if kind=='error':self.report_error(title,result);continue
                    if title=='核对A模型':
                        self.model=result['a_model'];self.deploy.model=self.model
                        self.endpoint[1]=result['a_status'];self.endpoint_time[1]=time.monotonic();self.endpoint_error[1]=None
                        if self.verify_a_identity(result['a_status']):
                            self.recognition_enabled=not self.pc_selected()
                            self.update_recognition_controls()
                            self.status.set('A模型已核对，可以查看训练动作；B命令采用仅发送模式')
                        else:
                            actual=a_state(json.dumps(result['a_status']))
                            if actual['class_count']!=len(self.actions):
                                self.status.set(f'A板现有{actual["class_count"]}类，当前训练{len(self.actions)}类（均含静息）；可切换PC识别，使用A识别需先上传对应模型')
                            else:self.status.set('A现有模型与当前训练动作不匹配，请选择对应模型；也可切换PC识别')
                    elif title=='离线导出':
                        rows=list(self.deploy.rows);result['action_identities']=self.model['action_identities']
                        self.deploy.select(result);self.deploy.edit(rows);self.model=result
                    elif isinstance(result,dict) and result.get('command_completed'):
                        self.state=None;self.endpoint[2]=None;self.endpoint_time[2]=0
                        self.endpoint_error[2]=None
                        self.last_b_command=title+'已完成'
                        self.board_text.set('B板：'+self.last_b_command)
                    elif isinstance(result,dict) and 'schema' in result:self.apply_state(result)
                    elif isinstance(result,dict) and 'positions' in result:
                        self.pose_text.set('所选姿态：'+('已保存并确认' if result['valid'] and result['flash_available'] and result['flash_initialized'] else '尚未确认保存'))
                    if title=='上传':self.model=self.deploy.model;self.status.set('A模型已核对，B动作绑定已提交；机械手尚未启用')
                    elif title=='保存A模型':self.status.set('A模型已写入用户Flash并核对；支持断电后自动恢复')
                    elif isinstance(result,dict) and result.get('sent_only'):self.status.set(title+'指令已发送（未确认B板执行）')
                    elif title!='核对A模型':self.status.set(title+'已完成')
                    diagnostic('operation_completed',operation=title,result=result)
            self.poll_display()
            self.render_connections();self.render_recognition()
            self.pc_text.set(f'电脑画面：{dict(raw="原始",filtered="滤波",envelope="包络").get(self.app.waveform_mode.get(),"波形")} · {self.app.waveform_range.get()} mV\nB板显示：请直接观察B板屏幕')
            if session.connected and not self.busy and time.monotonic()-self.last_query[1]>4:
                self.query(targets=(1,))
        except Exception as exc:self.report_error('界面更新',exc)
        self.app.after(150,self.poll)


class ResponseDialog(tk.Toplevel):
    """Edit the actual trained actions through the existing hand-response entry."""
    def __init__(self,panel):
        super().__init__(panel.app);self.panel=panel;self.actions=copy.deepcopy(panel.actions)
        self.title('机械手响应');self.configure(bg=PANEL);self.geometry('620x370');self.minsize(520,340)
        self.transient(panel.app);self.grab_set()
        label(self,'训练动作',bold=True).pack(anchor='w',padx=20,pady=(20,6))
        self.action=tk.StringVar(value=self.actions[0]['name'])
        self.action_combo=ttk.Combobox(self,textvariable=self.action,values=[a['name'] for a in self.actions],state='readonly')
        self.action_combo.pack(fill='x',padx=20);self.action_combo.bind('<<ComboboxSelected>>',self.select_action)
        label(self,'机械手响应',bold=True).pack(anchor='w',padx=20,pady=(18,6))
        self.response=tk.StringVar();self.response_combo=ttk.Combobox(self,textvariable=self.response,values=list(RESPONSE_OPTIONS),state='readonly')
        self.response_combo.pack(fill='x',padx=20);self.response_combo.bind('<<ComboboxSelected>>',self.change_response)
        label(self,'静息不执行操作；姿态槽需要先在姿态示教中保存。',wraplength=560,justify='left',muted=True).pack(fill='x',padx=20,pady=16)
        line=tk.Frame(self,bg=PANEL);line.pack(fill='x',side='bottom',padx=20,pady=16)
        button(line,'取消',self.destroy).pack(side='left')
        button(line,'保存响应',self.save,primary=True).pack(side='right')
        self.select_action()
    def select_action(self,event=None):
        index=self.action_combo.current();self.response.set(response_text(self.actions[index].get('response')))
        self.response_combo.configure(state='disabled' if index==0 else 'readonly')
    def change_response(self,event=None):
        index=self.action_combo.current()
        self.actions[index]['response']=response_value(self.response.get(),rest=index==0)
    def save(self):
        try:
            if self.panel.busy:raise ValueError('请等待当前操作完成')
            self.change_response();self.panel.save_responses(self.actions);self.destroy()
        except Exception as exc:self.panel.report_error('设置响应',exc)

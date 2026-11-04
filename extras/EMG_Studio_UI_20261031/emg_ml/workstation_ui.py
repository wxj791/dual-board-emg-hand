"""Fixed-page workstation layout. Device and model logic live in the host app."""
import tkinter as tk
from tkinter import ttk
from ui_style import (BG, PANEL, SURFACE, PLOT, FG, MUTED, ACCENT, BODY,
                      HEADING, setup, button, label, check)


def build_workstation(app, channel_names):
    face = setup(app)
    app.configure(bg=BG)
    top = tk.Frame(app, bg=PANEL, padx=12, pady=8)
    top.pack(fill='x')
    label(top, '串口').pack(side='left', padx=(0, 6))
    app.cb_ports = ttk.Combobox(top, state='readonly', width=24)
    app.cb_ports.pack(side='left', padx=(0, 6));app.port_map = {}
    button(top, '刷新', app._refresh_ports).pack(side='left')
    label(top, '波特率').pack(side='left', padx=(12, 6))
    app.entry_baud = tk.Entry(top, width=8, relief='flat')
    app.entry_baud.insert(0, '921600');app.entry_baud.pack(side='left')
    app.btn_connect = button(top, '打开串口', app._toggle_connect, primary=True)
    app.btn_connect.pack(side='left', padx=10)
    app.lbl_link_status = label(top, '未连接', bold=True)
    app.lbl_link_status.pack(side='left', padx=6)
    app.lbl_sps = label(top, '0 点/秒', muted=True)
    app.lbl_sps.pack(side='right')

    mid = tk.Frame(app, bg=BG, padx=10, pady=10)
    mid.pack(fill='both', expand=True)
    mid.columnconfigure(0, weight=1, minsize=540)
    mid.rowconfigure(0, weight=1)
    left = tk.Frame(mid, bg=PLOT)
    left.grid(row=0, column=0, sticky='nsew', padx=(0, 10))
    plot_header = tk.Frame(left, bg=PANEL, padx=12, pady=12)
    plot_header.pack(fill='x')
    label(plot_header, '八通道肌电', size=HEADING, bold=True).pack(side='left')
    app.btn_wave_replay=button(plot_header,'回放',app._toggle_waveform_replay)
    app.btn_wave_replay.pack(side='left',padx=10)
    controls = tk.Frame(plot_header, bg=PANEL)
    controls.pack(side='right')
    app.waveform_mode = tk.StringVar(value='raw')
    for text, value in [('原始', 'raw'), ('滤波', 'filtered'), ('包络', 'envelope')]:
        tk.Radiobutton(controls, text=text, variable=app.waveform_mode, value=value,
                       command=app._on_waveform_mode_changed, bg=PANEL, fg=FG,
                       selectcolor=SURFACE, activebackground=PANEL,
                       activeforeground=FG, font=(face, BODY), bd=0,
                       highlightthickness=0).pack(side='left', padx=(0, 8))
    app.lbl_waveform_range = label(controls, '量程 ±')
    app.lbl_waveform_range.pack(side='left', padx=(6, 3))
    app.waveform_range = tk.StringVar(value='1200')
    app.combo_waveform_range = ttk.Combobox(controls, textvariable=app.waveform_range,
        state='readonly', width=5, values=('20','50','100','200','500','1200','3300'))
    app.combo_waveform_range.pack(side='left')
    label(controls, 'mV', muted=True).pack(side='left', padx=4)
    # Kept as a diagnostic target for existing callbacks; no explanatory banner.
    app.lbl_channel_diag = label(left)
    app.canvas = tk.Canvas(left, bg=PLOT, highlightthickness=0)
    app.canvas.pack(fill='both', expand=True, padx=8, pady=8)

    app.right_panel = tk.Frame(mid, bg=PANEL, width=500)
    app.right_panel.grid(row=0, column=1, sticky='nsew')
    app.right_panel.pack_propagate(False)
    def resize(event):
        app.right_panel.configure(width=max(480, min(600, int(event.width*.35))))
    mid.bind('<Configure>', resize)
    app.notebook = ttk.Notebook(app.right_panel)
    app.notebook.pack(fill='both', expand=True)

    app.tab_diag = diag = tk.Frame(app.notebook, bg=PANEL, padx=12, pady=10)
    app.notebook.add(diag, text='信号诊断')
    metrics = tk.Frame(diag, bg=PANEL)
    metrics.pack(fill='x')
    for attr,text in [
        ('lbl_diag_sps','有效速率  -- 点/秒'), ('lbl_diag_total','累计接收  0'),
        ('lbl_diag_loss','错帧  0'), ('lbl_diag_adc','ADC采样  -- 组/秒'),
        ('lbl_diag_tx','发送 -- / 成功 --'), ('lbl_diag_rx','接收 -- 包/秒'),
        ('lbl_diag_drop','丢包  无线 --  /  发送 --  /  接收 --')]:
        widget = label(metrics, text, anchor='w')
        index = len(metrics.winfo_children())-1
        widget.grid(row=index//2, column=index%2, columnspan=2 if index==6 else 1,
                    sticky='w', pady=3, padx=(0,10))
        setattr(app, attr, widget)
    ttk.Separator(diag).pack(fill='x', pady=12)
    label(diag, '通道电压', size=HEADING, bold=True).pack(anchor='w', pady=(0, 8))
    meter = tk.Frame(diag, bg=PANEL)
    meter.pack(fill='both', expand=True)
    for c in range(2):meter.columnconfigure(c, weight=1, uniform='channels')
    app.ch_bars=[];app.ch_val_labels=[]
    for ch,name in enumerate(channel_names):
        frame=tk.Frame(meter,bg=PANEL,padx=4,pady=4)
        frame.grid(row=ch//2,column=ch%2,sticky='nsew',padx=(0,6),pady=3)
        head=tk.Frame(frame,bg=PANEL);head.pack(fill='x')
        label(head,name.replace('号板','号'),bold=True).pack(side='left')
        value=label(head,'0 mV',muted=True);value.pack(side='right')
        bar=ttk.Progressbar(frame,maximum=3300,value=0);bar.pack(fill='x',pady=(6,0))
        app.ch_bars.append(bar);app.ch_val_labels.append(value)

    app.tab_collection = col = tk.Frame(app.notebook,bg=PANEL,padx=12,pady=10)
    app.notebook.add(col,text='数据录制')
    config=tk.Frame(col,bg=PANEL);config.pack(fill='x')
    for c in (1,3):config.columnconfigure(c,weight=1)
    for attr,title,value,row,column in [
        ('entry_subject','受试者','DGT',0,0),('entry_p_idx','批次','1',0,2),
        ('entry_repeats','重复次数','4',1,0),('entry_hold','发力 / 秒','3.0',1,2),
        ('entry_rest','静息 / 秒','2.0',2,0),('entry_transition','切换 / 秒','0.8',2,2)]:
        label(config,title).grid(row=row,column=column,sticky='w',padx=(0,6),pady=3)
        entry=tk.Entry(config,width=7,relief='flat');entry.insert(0,value)
        entry.grid(row=row,column=column+1,sticky='ew',padx=(0,14 if column==0 else 0),pady=3)
        setattr(app,attr,entry)
    app.continuous_mode_var=tk.BooleanVar(value=True)
    check(col,'连续采集',app.continuous_mode_var).pack(anchor='w',pady=(16,12))
    profile=tk.Frame(col,bg=PANEL);profile.pack(fill='x',pady=(4,12))
    app.profile_summary=tk.StringVar()
    label(profile,variable=app.profile_summary,size=HEADING,bold=True).pack(side='left')
    app.btn_gesture_config=button(profile,'选择动作',app.open_gesture_profile)
    app.btn_gesture_config.pack(side='right')
    app.gesture_choices=tk.Frame(col,bg=PANEL)
    app.gesture_choices.pack(fill='both',expand=True)
    app._rebuild_gesture_choices()
    # Keep the action cue in the same gaze area as the waveform.
    board=app.collection_guide=tk.Frame(left,bg=PANEL,padx=14,pady=10)
    board.columnconfigure(1,weight=1)
    app.lbl_action_image=tk.Label(board,bg=PANEL)
    app.lbl_action_image.grid(row=0,column=0,rowspan=2,sticky='nw',padx=(0,14))
    app.lbl_action=label(board,'等待采集',size=24,bold=True,anchor='w')
    app.lbl_action.grid(row=0,column=1,sticky='ew')
    app.lbl_countdown=label(board,'',size=30,bold=True,anchor='e')
    app.lbl_countdown.grid(row=0,column=2,sticky='e',padx=(12,0))
    app.lbl_action_instruction=label(board,anchor='w',justify='left',wraplength=550)
    app.lbl_action_instruction.grid(row=1,column=1,columnspan=2,sticky='ew',pady=(4,0))
    footer=tk.Frame(board,bg=PANEL);footer.grid(row=2,column=0,columnspan=3,sticky='ew',pady=(8,0))
    app.lbl_stage=label(footer,'就绪',muted=True,anchor='w')
    app.lbl_stage.pack(side='left')
    app.lbl_next_action=label(footer,'下一步：等待开始',muted=True,anchor='e')
    app.lbl_next_action.pack(side='right',padx=(12,0))
    # Starting/stopping stays at the top of the collection controls.
    actions=tk.Frame(col,bg=PANEL)
    actions.pack(fill='x',before=config,pady=(0,14))
    for c in range(2):actions.columnconfigure(c,weight=1)
    app.btn_start=button(actions,'开始采集',app._start_collection,primary=True)
    app.btn_start.grid(row=0,column=0,sticky='ew',padx=(0,6))
    app.btn_abort=button(actions,'停止并保存',app._abort_collection,danger=True,state='disabled')
    app.btn_abort.grid(row=0,column=1,sticky='ew',padx=(6,0))
    def wrap_collection(event):
        app.lbl_action_instruction.configure(wraplength=max(240,event.width-156))
        app.lbl_action.configure(wraplength=max(220,event.width-300))
    board.bind('<Configure>',wrap_collection)
    def guide_visibility(event=None):
        thread=app.paradigm_thread
        visible=(app.notebook.select()==str(col) or app.core.is_recording
                 or bool(thread and thread.is_alive()))
        if visible and not board.winfo_manager():
            board.pack(fill='x',before=app.canvas,padx=8,pady=(8,0))
        elif not visible and board.winfo_manager():
            board.pack_forget()
    app._update_collection_focus=guide_visibility
    app.notebook.bind('<<NotebookTabChanged>>',guide_visibility,add='+')

    app.tab_infer = infer = tk.Frame(app.notebook,bg=PANEL,padx=12,pady=10)
    app.notebook.add(infer,text='实时识别')
    app.lbl_model_file=label(infer,'未载入模型',anchor='w',wraplength=520)
    app.lbl_model_file.pack(fill='x',pady=(0,8))
    models=tk.Frame(infer,bg=PANEL);models.pack(fill='x')
    app.model_select_row=models
    app.combo_model_var=tk.StringVar()
    app.combo_model=ttk.Combobox(models,textvariable=app.combo_model_var,state='readonly',width=20)
    app.combo_model.pack(side='left',fill='x',expand=True)
    app.combo_model.bind('<<ComboboxSelected>>',lambda e:app._load_trained_model(selected_file=app.combo_model_var.get()))
    app.btn_load_model=button(models,'刷新',app._refresh_model_list)
    app.btn_load_model.pack(side='left',padx=(8,0))
    app.infer_active_var=tk.BooleanVar(value=True)
    app.cb_infer_active=check(infer,'开启实时识别',app.infer_active_var)
    # One visible control owns the field engine; variable retained for legacy mode.
    app.infer_toggle=button(infer,'开始识别',app._toggle_recognition,primary=True)
    app.infer_toggle.pack(fill='x',pady=10)
    app.lbl_pred_gesture=label(infer,'等待识别',size=32,bold=True,wraplength=510)
    app.lbl_pred_gesture.pack(pady=(12,6))
    app.lbl_pred_conf=label(infer,'分类间隔：--',size=16)
    app.lbl_pred_conf.pack(pady=3)
    app.lbl_pred_output=label(infer,'',wraplength=510)
    app.lbl_pred_output.pack(fill='x',pady=4)
    ttk.Separator(infer).pack(fill='x',pady=12)
    app.prob_box=tk.Frame(infer,bg=PANEL);app.prob_box.pack(fill='both',expand=True)
    def wrap_inference(event):
        for widget in (app.lbl_model_file,app.lbl_pred_gesture,app.lbl_pred_output):
            widget.configure(wraplength=max(240,event.width-28))
    infer.bind('<Configure>',wrap_inference)
    app.set_inference_classes(app.inference_names)
    app.cnn_model=None
    import torch
    app.device=torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    app._load_trained_model()
    from field_training import FieldTrainingPanel
    app.field_training=FieldTrainingPanel(app)

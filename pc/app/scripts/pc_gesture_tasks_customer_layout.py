"""Record requested widget geometry with every window withdrawn; no desktop access."""
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import sys
import tkinter as tk
from unittest.mock import patch
import serial
from serial.tools import list_ports
from pc_gesture_tasks_hidden_ui import hidden_init,EMG,ROOT


def main():
    out=ROOT/'artifacts/pc_gesture_tasks'/datetime.now().strftime('customer_layout_%Y%m%d_%H%M%S_%f')
    out.mkdir(parents=True,exist_ok=False)
    env={'EMG_BOARD_PROTOCOL':'1','EMG_GESTURE_TASKS_STATE':str(out/'runtime/gesture_tasks_session.json'),
         'EMG_FIELD_SETTINGS_FILE':str(out/'runtime/preferred_model.json')}
    with patch.dict(os.environ,env),patch('field_training.DEFAULT_PACKAGE_FILE',out/'missing.json'), \
         patch.object(tk.Tk,'__init__',hidden_init(tk.Tk.__init__)), \
         patch.object(tk.Toplevel,'__init__',hidden_init(tk.Toplevel.__init__)), \
         patch.object(tk.Wm,'deiconify',lambda self:None), \
         patch.object(serial,'Serial',side_effect=AssertionError('offline only')), \
         patch.object(list_ports,'comports',return_value=[]):
        spec=importlib.util.spec_from_file_location('customer_layout_app',EMG/'emg_studio_gui_V3.0.py')
        studio=importlib.util.module_from_spec(spec);spec.loader.exec_module(studio)
        app=studio.EMGStudioGUI();panel=app.gesture_tasks
        try:
            assert app.field_training.load_package(EMG.parent/'backend/artifacts/cnn_fusion_pc_trial_20261029/export')
            app.update_idletasks()
            def size(widget):return {'width':widget.winfo_reqwidth(),'height':widget.winfo_reqheight()}
            geometry={name:size(widget) for name,widget in (
                ('hand_page',panel.tab),('use',panel.bind_tab),('teach',panel.teach_tab),('display',panel.display_tab),
                ('inference',app.tab_infer),('source_selector',app.source_combo))}
            app.recognition_source.set('PC识别');panel.change_recognition_source();app.update_idletasks()
            geometry['inference_pc']=size(app.tab_infer)
            assert str(app.source_combo['state'])=='readonly'
            assert tuple(app.source_combo['values'])==('A板识别','PC识别')
            assert geometry['inference_pc']['height']<=630,geometry
            assert geometry['inference_pc']['width']<=600,geometry
            app.lbl_pred_gesture.configure(text='【向小指侧偏腕】')
            app.lbl_pred_conf.configure(text='识别来源：PC · 分类间隔: 12.34（非概率）')
            app.lbl_pred_output.configure(text='机械手响应：回放已示教姿态 9 · PC预览，机械手仍由A/B控制')
            app.update_idletasks();geometry['inference_pc_result']=size(app.tab_infer)
            assert geometry['inference_pc_result']['height']<=630,geometry
            from gesture_tasks_customer import model_actions,action_identity
            from gesture_tasks_ui import ResponseDialog
            panel.actions=model_actions(['rest',*[f'action{i}' for i in range(1,32)]])
            model={'class_names':[r['name'] for r in panel.actions],'active_classes':32,
                   'source_sha256':'0'*64,'action_identities':[action_identity(r) for r in panel.actions]}
            panel.model=model;panel.deploy.select(model)
            dialog=ResponseDialog(panel);app.update_idletasks()
            geometry['response_editor_32']=size(dialog)
            geometry['response_options']=len(dialog.action_combo['values'])
            assert not app.winfo_ismapped() and not dialog.winfo_ismapped()
            assert all(geometry[key]['width']<=500 and geometry[key]['height']<=600 for key in ('use','teach','display'))
            assert geometry['hand_page']['height']<=630
            result={'desktop_accessed':False,'serial_opened':False,'measurement':'Tk requested geometry, not a screen capture',
                    'body_font_points':14,'geometry':geometry,'mapped_visual_checks':'not performed'}
            (out/'layout.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            print(json.dumps(result,ensure_ascii=False,indent=2));print(out)
            dialog.destroy()
        finally:
            app.field_training.close()
            for timer in app.tk.call('after','info'):app.after_cancel(timer)
            app.destroy()

if __name__=='__main__':main()

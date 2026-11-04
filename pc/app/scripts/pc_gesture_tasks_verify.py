"""Repeatable offline checks. Does not access serial/JTAG/servos or A/B source files."""
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime
ROOT=Path(__file__).resolve().parents[1]
STUDIO=ROOT/'EMG_Studio/EMG_Studio'

def main():
    out=ROOT/'artifacts/pc_gesture_tasks'/datetime.now().strftime('verify_%Y%m%d_%H%M%S_%f')
    out.mkdir(parents=True,exist_ok=False)
    commands=[([sys.executable,'-X','utf8','-m','unittest','discover','-s','tests','-p','test_gesture_tasks.py','-v'],STUDIO/'emg_ml','protocol'),
              ([sys.executable,'-X','utf8','-m','unittest','discover','-s','tests','-p','test_gesture_tasks_raw.py','-v'],STUDIO/'emg_ml','compact_raw'),
              ([sys.executable,'-X','utf8','-m','unittest','discover','-s','tests','-p','test_gesture_tasks_customer.py','-v'],STUDIO/'emg_ml','customer'),
              ([sys.executable,'-X','utf8','-m','unittest','discover','-s','tests','-p','test_gesture_tasks_materials.py','-v'],STUDIO/'emg_ml','default_materials'),
              ([sys.executable,'-X','utf8','-m','unittest','pc_ml.test_gesture_tasks_export','-v'],STUDIO/'backend','export'),
              ([sys.executable,'-X','utf8','-m','unittest','discover','-s','tests','-p','test_signal_processing.py','-v'],STUDIO/'emg_ml','signals'),
              ([sys.executable,'-X','utf8',str(ROOT/'scripts/pc_gesture_tasks_hidden_ui.py')],STUDIO/'emg_ml','ui_hidden')]
    results=[]
    env=os.environ.copy()
    env['EMG_GESTURE_TASKS_STATE']=str(out/'runtime/gesture_tasks_session.json')
    for cmd,cwd,name in commands:
        p=subprocess.run(cmd,cwd=cwd,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,encoding='utf8',errors='replace')
        (out/f'{name}.log').write_text(p.stdout,encoding='utf8')
        results.append({'check':name,'returncode':p.returncode}); print(name,p.returncode,flush=True)
    (out/'summary.json').write_text(json.dumps({'checks':results,'hardware_connected':False},indent=2)+'\n',encoding='utf8')
    print(out)
    return int(any(r['returncode'] for r in results))
if __name__=='__main__':raise SystemExit(main())

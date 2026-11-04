"""Run the existing PC regression suites; never connects to hardware."""
from pathlib import Path
import subprocess,sys,os,json
R=Path(__file__).resolve().parents[1];A=R/'pc/app/EMG_Studio/EMG_Studio'
out=R/'local_verification';out.mkdir(exist_ok=True)
results=[]
for name in ['test_gesture_tasks.py','test_gesture_tasks_customer.py','test_gesture_tasks_raw.py']:
    env=dict(os.environ);env['PYTHONDONTWRITEBYTECODE']='1'
    p=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-p',name,'-v'],cwd=A/'emg_ml',env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace')
    (out/(name+'.log')).write_text(p.stdout,encoding='utf-8');results.append({'suite':name,'returncode':p.returncode});print(name,'PASS' if p.returncode==0 else 'FAIL',flush=True)
(out/'regression.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
raise SystemExit(0 if all(r['returncode']==0 for r in results) else 1)

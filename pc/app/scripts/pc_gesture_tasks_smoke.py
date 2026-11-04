"""Persistent synthetic recording -> fresh training -> A integer export. No devices."""
import json
from pathlib import Path
import sys
from datetime import datetime
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'EMG_Studio/EMG_Studio/backend'))
from pc_ml.cnn_fusion_field import run

def main():
    out=ROOT/'artifacts/pc_gesture_tasks'/datetime.now().strftime('synthetic_training_%Y%m%d_%H%M%S_%f')
    out.mkdir(parents=True,exist_ok=False)
    rng=np.random.default_rng(1031);n=1920
    labels=np.concatenate([np.full(n,c,dtype=np.int16) for rep in range(3) for c in range(2)])
    t=np.arange(len(labels))/1000
    raw=1650+rng.normal(0,3,(len(labels),8))
    for c in range(8):raw[:,c]+=(3+labels*(90+12*c))*np.sin(2*np.pi*(43+c*7)*t)
    profile={'version':1,'name':'OFFLINE SYNTHETIC ONLY','classes':[
        {'id':i,'key':'rest' if i==0 else 'custom','name':'静息' if i==0 else '离线合成动作',
         'instruction':'合成测试，不代表真实手势','image':'custom.png','output':{'id':0,'name':'保持 / 停止触发'}} for i in range(2)]}
    source=out/'synthetic_recording.npz'
    np.savez_compressed(source,emg=raw.astype(np.float32),labels=labels,timestamps=t,sample_rate=1000.,
        gestures={c['id']:(c['name'],c['instruction']) for c in profile['classes']},gesture_profile=json.dumps(profile),capture_complete=True)
    report=run(source,out/'run')
    print(json.dumps(report['a_deployment'],ensure_ascii=False,indent=2),flush=True)
    print(out,flush=True)
    return int('final' not in report['a_deployment'] or 'heldout' not in report['a_deployment'])
if __name__=='__main__':raise SystemExit(main())

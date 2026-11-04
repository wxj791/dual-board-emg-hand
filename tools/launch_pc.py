"""Start the packaged current GUI using paths relative to this repository."""
from pathlib import Path
import os,sys,runpy
ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/'pc/app/EMG_Studio/EMG_Studio'
PACKAGE=ROOT/'models/current_8class/export'
for key in ['EMG_GESTURE_TASKS_STATE','EMG_FIELD_DEFAULT_PACKAGE','EMG_FIELD_SETTINGS_FILE','EMG_FPGA_PROJECT','EMG_SPARSE_OPCODES']:
    os.environ.pop(key,None)
sys.path[:0]=[str(APP),str(APP/'backend'),str(APP/'emg_ml')]
sys.argv=[str(APP/'start_gesture_tasks.py'),'--legacy-opcodes','--model-package',str(PACKAGE)]
runpy.run_path(sys.argv[0],run_name='__main__')

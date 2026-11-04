"""Portable launcher. All runtime paths stay inside this extracted folder."""
import os
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parent

def configure():
    backend = ROOT/'backend'
    package = backend/'artifacts/cnn_fusion_pc_trial_20261029/export'
    runtime = ROOT/'runtime'
    runtime.mkdir(exist_ok=True)
    os.environ['EMG_FPGA_PROJECT'] = str(backend)
    os.environ['EMG_FIELD_TRAINING_MODE'] = 'cnn_fusion'
    os.environ['EMG_FIELD_DEFAULT_PACKAGE'] = str(package)
    os.environ['EMG_FIELD_SETTINGS_FILE'] = str(runtime/'cnn_fusion_default.json')
    os.environ['EMG_LIVE_TRIAL_ROOT'] = str(runtime/'live_trials')
    sys.path[:0] = [str(ROOT/'emg_ml'), str(backend)]
    os.chdir(ROOT/'emg_ml')
    return package

if __name__ == '__main__':
    configure()
    runpy.run_path(str(ROOT/'emg_ml/emg_studio_gui_V3.0.py'), run_name='__main__')

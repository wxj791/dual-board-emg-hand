@echo off
cd /d "%~dp0"
py -3.11 EMG_Studio\EMG_Studio\start_gesture_tasks.py --model-package EMG_Studio\EMG_Studio\backend\artifacts\pc_ml\sessions\recovered_model\export
pause

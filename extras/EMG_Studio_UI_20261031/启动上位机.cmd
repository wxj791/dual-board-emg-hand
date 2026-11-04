@echo off
setlocal
py -3.11 -X utf8 "%~dp0start_emg_studio.py"
if errorlevel 1 pause
endlocal

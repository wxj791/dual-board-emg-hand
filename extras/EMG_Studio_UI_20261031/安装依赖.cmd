@echo off
setlocal
py -3.11 -m pip install -r "%~dp0requirements.txt"
pause
endlocal

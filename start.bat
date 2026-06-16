@echo off
REM Manual start for the Telegram <-> Claude bridge.
cd /d "%~dp0"

REM Install deps on first run (harmless if already installed).
py -m pip install -q -r requirements.txt

REM First run launches an interactive setup wizard; after that it just starts.
echo Starting Claude bridge... (close this window to stop)
py bridge.py
pause

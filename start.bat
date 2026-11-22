@echo off
cd /d "%~dp0"
echo Starting PayFlow onboarding on http://localhost:8000  (Ctrl+C to stop)
start "" http://localhost:8000
python run.py --reset --demo
if errorlevel 9009 py run.py --reset --demo
pause

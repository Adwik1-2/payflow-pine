@echo off
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo Running the live end-to-end test against the real Claude API...
echo The result is also saved to live-test-result.txt
powershell -NoProfile -Command "if (Get-Command python -ErrorAction SilentlyContinue) { $py = 'python' } else { $py = 'py' }; & $py tools\live_e2e.py 2>&1 | Tee-Object -FilePath live-test-result.txt"
echo.
echo Done. Result saved to live-test-result.txt
pause

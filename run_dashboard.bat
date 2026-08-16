@echo off
cd /d "%~dp0"

set PORT=5001

if exist "..\.venv-flow\Scripts\python.exe" (
  "..\.venv-flow\Scripts\python.exe" app.py
) else (
  python app.py
)

pause

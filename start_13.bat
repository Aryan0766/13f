@echo off
rem Double-click to open the 13F Tracker in your browser. Close this window to stop it.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Could not find .venv\Scripts\python.exe in this folder.
  echo Open the project in VS Code once and run the "Install requirements" task, then try again.
  pause
  exit /b 1
)
set PYTHONPATH=%~dp0src
echo Starting the 13F Tracker. Your browser will open in a moment...
".venv\Scripts\python.exe" -m streamlit run src\thirteenf_processor\web.py --server.address localhost --browser.gatherUsageStats false
pause
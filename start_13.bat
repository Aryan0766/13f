@echo off
rem Double-click to open the 13F Tracker in your browser. Close this window to stop it.
rem Optional: if you copy this file elsewhere (for example the Desktop), put the project folder here.
set "PROJ="
if not defined PROJ set "PROJ=%~dp0"
cd /d "%PROJ%"
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY if exist "venv\Scripts\python.exe" set "PY=venv\Scripts\python.exe"
if not defined PY (
  where py >nul 2>nul && set "PY=py"
)
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo [PROBLEM] Python was not found. Install Python, or run the project once in VS Code.
  pause
  exit /b 1
)
if not exist "src\thirteenf_processor\web.py" (
  echo [PROBLEM] Could not find the project at: %CD%
  echo Edit the PROJ line at the top of this file to point to your thirteenf-processor folder.
  pause
  exit /b 1
)
echo Project: %CD%
echo Using: %PY%
"%PY%" -c "import streamlit" 2>nul
if errorlevel 1 (
  echo [PROBLEM] streamlit is not installed in %PY%
  echo Fix: open a Command Prompt in this folder and run:  %PY% -m pip install -r requirements.txt
  pause
  exit /b 1
)
set PYTHONPATH=%CD%\src
echo Starting the 13F Tracker. Your browser will open in a moment...
echo If it does not, open Chrome and go to http://localhost:8501
"%PY%" -m streamlit run src\thirteenf_processor\web.py --server.address localhost --browser.gatherUsageStats false
echo.
echo [The page stopped. Read the lines above for the reason.]
pause
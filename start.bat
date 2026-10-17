@echo off
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo ==== Smash Streak Counter ====
echo Folder: %CD%
where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python is not installed. Install it from https://www.python.org/downloads/
  echo IMPORTANT: check "Add python.exe to PATH" in the installer.
  pause
  exit /b
)
if not exist ".venv\Scripts\python.exe" (
  echo [1/2] Creating .venv ...
  python -m venv .venv
  if errorlevel 1 goto err
  echo [2/2] Installing packages - this takes 1-2 minutes ...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto err
)
echo Starting counter. Close this window to stop.
".venv\Scripts\python.exe" streak_counter.py
echo.
echo Counter stopped.
pause
exit /b
:err
echo.
echo SETUP FAILED. See the messages above.
if exist .venv rmdir /s /q .venv
pause

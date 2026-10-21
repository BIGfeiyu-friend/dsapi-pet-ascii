@echo off
rem Install / update the dsh-pet-ascii add-ons into your installed dsh-pet-standalone-webm-chat.
rem Quit the pet from its tray icon first. Needs Python 3.8+ (standard library only).
setlocal
cd /d "%~dp0"
set "PYEXE="
where py >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE (
  python --version >nul 2>nul && set "PYEXE=python"
)
if not defined PYEXE (
  echo Python 3.8 or newer is required: https://www.python.org/downloads/windows/
  pause
  exit /b 1
)
%PYEXE% "%~dp0install.py" %*
pause

@echo off
rem Remove the dsh-pet-ascii add-ons and restore the original exe from the .orig backup.
rem Your chat memory and settings in %APPDATA% are not touched.
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
%PYEXE% "%~dp0install.py" --uninstall %*
pause

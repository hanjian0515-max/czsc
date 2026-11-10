@echo off
cd /d "%~dp0"
cscript.exe //nologo "%~dp0create-desktop-shortcut.vbs"
if errorlevel 1 (
  echo Shortcut creation failed. You can still double-click start-alerts.cmd.
  pause
  exit /b 1
)
echo Desktop shortcut created. Keep the extracted application folder in place.
pause

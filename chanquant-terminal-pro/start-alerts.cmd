@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "CQ_ALERT_PY="
where py >nul 2>nul
if not errorlevel 1 (
  py -3.12 -c "import sys,struct; assert sys.version_info[:2] == (3,12) and struct.calcsize('P') == 8" >nul 2>nul
  if not errorlevel 1 set "CQ_ALERT_PY=py -3.12"
)
if not defined CQ_ALERT_PY (
  where python >nul 2>nul
  if not errorlevel 1 (
    python -c "import sys,struct; assert (3,10) <= sys.version_info[:2] < (3,14) and struct.calcsize('P') == 8" >nul 2>nul
    if not errorlevel 1 set "CQ_ALERT_PY=python"
  )
)
if not defined CQ_ALERT_PY (
  echo 请先安装64位Python 3.12，再重新双击本文件。
  echo 安装时可勾选Add Python to PATH；无需提供账号密码。
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo 正在为预警助手创建本地Python环境...
  %CQ_ALERT_PY% -m venv .venv
  if errorlevel 1 goto :failed
)
".venv\Scripts\python.exe" -c "import importlib.metadata; from zoneinfo import ZoneInfo; assert importlib.metadata.version('czsc') == '1.0.2'; assert importlib.metadata.version('pytdx') == '1.72'; from pytdx.hq import TdxHq_API; ZoneInfo('Asia/Shanghai')" >nul 2>nul
if errorlevel 1 (
  echo 首次启动或升级需要联网安装CZSC和通达信行情依赖，可能需要几分钟。
  ".venv\Scripts\python.exe" -m pip install -r requirements-alerts.txt
  if errorlevel 1 goto :failed
)
".venv\Scripts\python.exe" desktop_alerts.py
if errorlevel 1 goto :failed
exit /b 0
:failed
echo 启动失败。请保留本窗口中的错误信息，并检查Python安装或依赖网络。
pause
exit /b 1

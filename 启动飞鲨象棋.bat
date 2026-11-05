@echo off
chcp 936 >nul
title 飞鲨象棋 PC 版
cd /d "%~dp0"
setlocal EnableDelayedExpansion

set "MODE=run"
if /i "%~1"=="debug" set "MODE=debug"
if /i "%~1"=="-d" set "MODE=debug"
if /i "%~1"=="check" set "MODE=check"
if /i "%~1"=="-c" set "MODE=check"

set "PY="
set "PYW="

rem ---- 1. 本机 Python 3.12 安装目录（优先） ----
set "P312=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if exist "%P312%" (
  "%P312%" -c "import PySide6" >nul 2>&1
  if !errorlevel!==0 set "PY=%P312%"
)

rem ---- 2. py 启动器选择 3.12 ----
if not defined PY (
  py -3.12 -c "import PySide6" >nul 2>&1
  if !errorlevel!==0 (
    for /f "delims=" %%A in ('py -3.12 -c "import sys;print(sys.executable)"') do set "PY=%%A"
  )
)

rem ---- 3. PATH 上的 python ----
if not defined PY (
  python -c "import PySide6" >nul 2>&1
  if !errorlevel!==0 (
    for /f "delims=" %%A in ('python -c "import sys;print(sys.executable)"') do set "PY=%%A"
  )
)

rem ---- 找不到依赖则给出明确指引 ----
if not defined PY (
  echo.
  echo   [启动失败] 没有找到已安装 PySide6 的 Python 环境。
  echo.
  echo   请执行下面任意一条命令安装依赖后重试：
  echo       py -3.12 -m pip install -U PySide6
  echo       py -3.12 -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

for %%A in ("%PY%") do set "PYDIR=%%~dpA"
set "PYW=!PYDIR!pythonw.exe"
if not exist "!PYW!" set "PYW=!PY!"

rem ---- check：只检查环境，不启动 ----
if /i "!MODE!"=="check" (
  echo.
  echo   环境检查通过
  echo   解释器 : !PY!
  echo   启动器 : !PYW!
  echo   入口   : %~dp0main.py
  echo.
  echo   双击本文件即可启动；加 debug 参数可查看运行日志。
  echo.
  pause
  exit /b 0
)

rem ---- debug：保留控制台，显示日志与退出码 ----
if /i "!MODE!"=="debug" (
  title 飞鲨象棋 PC 版 - 诊断模式
  echo.
  echo   ===== 诊断模式：程序输出会显示在这里 =====
  echo   解释器 : !PY!
  echo.
  "!PY!" "%~dp0main.py"
  echo.
  echo   ===== 程序已退出，退出码 !errorlevel! =====
  pause
  exit /b 0
)

rem ---- 默认：无黑框一键启动 ----
echo   正在启动 飞鲨象棋 PC 版 ...
start "" "!PYW!" "%~dp0main.py"
exit /b 0

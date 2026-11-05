@echo off
chcp 936 >nul
title 飞鲨象棋 PC 版 - 发布版单 exe 打包
cd /d "%~dp0"

set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PYEXE%" (
  echo.
  echo   [失败] 未找到 Python 3.12：%PYEXE%
  echo   请安装 Python 3.12 并执行： py -3.12 -m pip install -U PySide6 pyinstaller
  echo.
  pause
  exit /b 1
)

"%PYEXE%" -c "import PySide6, PyInstaller, onnxruntime, cv2" >nul 2>&1
if errorlevel 1 (
  echo.
  echo   [提示] 缺少打包依赖，正在自动安装 ...
  "%PYEXE%" -m pip install -U PySide6 pyinstaller onnxruntime opencv-python numpy Pillow pywin32 psutil
)

if not exist "detector.onnx" (
  echo.
  echo   [失败] 缺少 detector.onnx，发布版必须内嵌识别模型
  pause
  exit /b 1
)

echo.
echo   ===== 开始打包：单 exe + 无黑框 + 内嵌 detector.onnx =====
echo.
"%PYEXE%" -m PyInstaller --clean --noconfirm --distpath dist --workpath build feisha.spec
if errorlevel 1 (
  echo.
  echo   [失败] 打包出错，请查看上方 PyInstaller 日志
  pause
  exit /b 1
)

echo.
echo   ===== 生成发布版（外部引擎 + 说明 + 分发 zip） =====
"%PYEXE%" make_release.py
if errorlevel 1 (
  echo.
  echo   [失败] 发布版生成失败
  pause
  exit /b 1
)

echo.
echo   自检命令： dist\飞鲨象棋PC版.exe --selftest
echo   自检结果写在 dist\飞鲨象棋_自检.txt （可确认模型已内嵌成功）
echo.
pause
exit /b 0

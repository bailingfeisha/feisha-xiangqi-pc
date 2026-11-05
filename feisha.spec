# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 单文件发布版打包配置 —— 飞鲨象棋 PC 版。

要点：
  * onefile：Python 模块 / PySide6 / Qt 插件 / onnxruntime / cv2 全部合并进一个 exe；
  * console=False：GUI 程序，无黑框；
  * detector.onnx 已内嵌（datas），运行时从 _MEIPASS 读取；
    用户若在 exe 同级放同名文件，可覆盖内置模型；
  * 引擎 exe / NNUE 仍不嵌入，放在 exe 同级 engines/ 目录；
  * excludes：剔除无关大型库（尤其 324MB 的 ORT CUDA provider、torch）。
"""
import os

from PyInstaller.utils.hooks import collect_dynamic_libs

SPEC_DIR = os.path.abspath(SPECPATH)
ENTRY = os.path.join(SPEC_DIR, "main.py")
ICON = os.path.join(SPEC_DIR, "app.ico")
MODEL = os.path.join(SPEC_DIR, "detector.onnx")

if not os.path.isfile(MODEL):
    raise SystemExit(f"[feisha.spec] 缺少识别模型：{MODEL}")

# onnxruntime 的第三方 hook 会无差别收集 capi 下所有 DLL，
# 其中包含 324MB 的 CUDA provider：本程序只用 CPUExecutionProvider，
# 在 Analysis 结束后统一从 TOC 里剔除 GPU 相关库。
# 同时往 _MEIPASS 根目录放一份 CPU 主 DLL，兼容扩展模块被展平的情况。
extra_binaries = []
for src, _dst in collect_dynamic_libs("onnxruntime"):
    low = os.path.basename(src).lower()
    if "cuda" in low or "tensorrt" in low:
        continue
    extra_binaries.append((src, "."))
for src, dst in collect_dynamic_libs("cv2"):
    extra_binaries.append((src, dst))

GPU_ORT_KEYS = (
    "onnxruntime_providers_cuda", "onnxruntime_providers_tensorrt",
    "cudnn", "cublas", "cufft", "curand", "cusolver", "cusparse",
    "nvinfer", "nvonnxparser", "cuda", "tensorrt", "nvidia",
    "cudart", "nvrtc", "nvjitlink",
)


def _is_gpu_ort(entry) -> bool:
    text = (str(entry[0]) + "|" + str(entry[1])).lower()
    return any(k in text for k in GPU_ORT_KEYS)

hidden = [
    "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets",
    "PySide6.QtNetwork", "PySide6.QtSvg",
    "core", "core.constants", "core.options", "core.board",
    "core.rules", "core.notation", "core.game",
    "ai", "ai.engine", "ai.league",
    "ui", "ui.board_widget", "ui.dialogs", "ui.main_window",
    "ui.league_window", "ui.vision_window",
    "network", "network.client",
    "vision", "vision.model", "vision.capture", "vision.reconstruct",
    "vision.clicker", "vision.controller", "vision.adb_clicker",
    "onnxruntime", "onnxruntime.capi", "onnxruntime.capi._pybind_state",
    "onnxruntime.capi.onnxruntime_pybind11_state",
    "onnxruntime.capi._ld_preload", "onnxruntime.capi.version_info",
    "cv2", "numpy", "PIL", "PIL.Image", "PIL.ImageGrab",
    "win32gui", "win32ui", "win32process", "win32api", "win32con", "psutil",
]

excluded = [
    "tkinter", "matplotlib", "pandas", "scipy",
    "PyQt5", "PyQt6", "PySide2", "shiboken2",
    "jupyter", "IPython", "pytest", "curses", "pydoc_data",
    "torch", "torchvision", "torchaudio",
]

a = Analysis(
    [ENTRY],
    pathex=[SPEC_DIR],
    binaries=extra_binaries,
    datas=[(MODEL, ".")],     # 识别模型内嵌：发布版单 exe 自带模型
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excluded,
    noarchive=False,
    optimize=0,
)

# 剔除 hook 收进来的 GPU provider（约 325MB），只保留 CPU 推理库
a.binaries[:] = [e for e in a.binaries if not _is_gpu_ort(e)]
a.datas[:] = [e for e in a.datas if not _is_gpu_ort(e)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="飞鲨象棋PC版",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                # UPX 压缩 Qt DLL 易导致启动失败
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,            # 无控制台黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON if os.path.isfile(ICON) else None,
)

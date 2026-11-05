# -*- coding: utf-8 -*-
"""飞鲨象棋 PC 版 —— 程序入口。

源码运行：  python main.py
单文件版：  飞鲨象棋PC版.exe
            （识别模型 detector.onnx 已内嵌进 exe；引擎不打包，
              放在 exe 同级 engines/ 目录，或留在原位置由程序自动发现）

依赖：  PySide6>=6.5  （pip install -U PySide6）
隐藏参数：  --selftest   构造界面 + 真实加载外部引擎分析，结果写入
            exe 同级「飞鲨象棋_自检.txt」，供无控制台的单 exe 排错。
"""
from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime

IS_FROZEN = getattr(sys, "frozen", False)

# PyInstaller onefile 下 __file__ 指向 _MEIxxxxx 临时解压目录，
# 源码目录下才需要手动把项目根加入 sys.path。
if IS_FROZEN:
    sys.path.insert(0, getattr(sys, "_MEIPASS",
                               os.path.dirname(os.path.abspath(sys.executable))))
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import Qt, QEventLoop, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QTabWidget

from ui.main_window import MainWindow


def app_base_dir() -> str:
    """日志/自检文件写入目录：exe 同级（打包）或项目根（源码）。"""
    if IS_FROZEN:
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def write_log(filename: str, text: str, mode: str = "a"):
    try:
        with open(os.path.join(app_base_dir(), filename), mode,
                  encoding="utf-8") as f:
            f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
            f.write(text.rstrip() + "\n")
    except OSError:
        pass


def _safe_print(text: str):
    """windowed 打包下 sys.stdout 可能为空，不能让 print 抛异常。"""
    try:
        print(text)
    except Exception:
        pass


def _install_excepthook():
    """单 exe 无控制台时，把未捕获异常写到 exe 同级日志，避免静默闪退。"""
    def hook(etype, exc, tb):
        text = "".join(traceback.format_exception(etype, exc, tb))
        write_log("飞鲨象棋_错误日志.txt", text)
        try:
            sys.__excepthook__(etype, exc, tb)
        except Exception:
            pass
    sys.excepthook = hook


def _wait_engine_ready(eng, timeout_ms=6000) -> bool:
    """轮询握手状态，不依赖信号到达时序（界面可能已提前加载引擎）。"""
    loop = QEventLoop()
    state = {"ok": False}
    timer = QTimer()

    def check():
        if (eng.info.options or
                eng.info.name != os.path.basename(eng.path)):
            state["ok"] = True
            loop.quit()
        elif not eng.running:
            loop.quit()

    timer.timeout.connect(check)
    timer.start(100)
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    timer.stop()
    return state["ok"]


def run_selftest(app: QApplication) -> int:
    """打包后自检：UI 冒烟 + 外部引擎真实加载，结果写到 exe 同级文件。"""
    from ai.engine import UciEngine, bundled_engine_path, discover_engines
    from core import Game, Board, Options, Mode
    from core.notation import uci_to_move

    lines: list[str] = []

    def rec(key, value):
        text = f"{key} = {value}"
        lines.append(text)
        _safe_print(text)

    try:
        rec("SELFTEST", "START")
        rec("frozen", IS_FROZEN)
        rec("app_base_dir", app_base_dir())
        rec("bundled_engine", bundled_engine_path())
        found = discover_engines()
        rec("discovered_engines", len(found))
        for p in found:
            rec("  engine", p)

        win = MainWindow()
        win.show()
        app.processEvents()
        win.board_widget.repaint()
        app.processEvents()
        tabs = win.findChildren(QTabWidget)
        rec("window_title", win.windowTitle())
        rec("tab_widgets", len(tabs))
        rec("tab_count", tabs[0].count() if tabs else -1)
        rec("legal_moves", len(win.game.legal_moves()))
        m = win.game.legal_moves()[0]
        win._on_square_clicked(m.src)
        win._on_square_clicked(m.dst)
        app.processEvents()
        rec("played", win.game.current.notation)
        rec("ply_after_play", win.game.ply)

        # 发布版内嵌模型自检：确认 detector.onnx 真在 exe 里且 ORT 能推理
        from vision.model import default_model_path, model_source, YoloDetector
        meipass = getattr(sys, "_MEIPASS", "")
        rec("meipass", meipass)
        rec("meipass_model_exists",
            bool(meipass and os.path.isfile(
                os.path.join(meipass, "detector.onnx"))))
        mp = default_model_path()
        rec("model_path", mp or "(none)")
        rec("model_source", model_source(mp))
        rec("model_size",
            os.path.getsize(mp) if mp and os.path.isfile(mp) else -1)
        if mp and os.path.isfile(mp):
            import numpy as np
            det = YoloDetector(mp)
            rec("model_input", str(det.session.get_inputs()[0].shape))
            rec("model_output", str(det.session.get_outputs()[0].shape))
            blank = np.full((480, 480, 3), 114, dtype=np.uint8)
            rec("model_inference", "OK dets=" + str(len(det.detect(blank))))
        # 发布版新增模块也要确认被打进 exe
        from vision.adb_clicker import detect_emulator, AdbBridge
        from vision.reconstruct import position_turn_hint
        rec("adb_module", "OK")
        rec("emulator_probe", detect_emulator("MuMuPlayer.exe", "", ""))
        rec("turn_hint_module", "OK" if position_turn_hint else "MISSING")

        # 真实加载外部引擎（引擎不嵌入 exe）并取 bestmove
        eng = getattr(win, "active_engine", None)
        own_engine = False
        if eng is None or not eng.running:
            path = bundled_engine_path()
            if path:
                eng = UciEngine(path)
                eng.start()
                own_engine = True
        if eng is None:
            rec("engine_test", "SKIPPED (no external engines/ dir)")
        else:
            rec("engine_path", eng.path)
            rec("engine_ready", _wait_engine_ready(eng))
            rec("engine_name", eng.info.name)
            rec("engine_nnue", eng.info.nnue_info or "(未报告)")
            if eng.running:
                loop = QEventLoop()
                result = {}
                eng.analysis_ready.connect(
                    lambda a: (result.update(a=a), loop.quit()))
                eng.failed.connect(
                    lambda msg: (result.update(err=msg), loop.quit()))
                QTimer.singleShot(12000, loop.quit)
                fen = Board(Mode.JIEQI, Options()).to_fen()
                eng.analyse(fen, movetime=300, use_movetime=True,
                            hash_mb=16, threads=1)
                loop.exec()
                a = result.get("a")
                rec("engine_bestmove", a.bestmove if a else "NONE")
                rec("engine_summary", a.summary if a else "NONE")
                if a and a.bestmove:
                    mv = uci_to_move(a.bestmove, True)
                    rec("bestmove_legal",
                        mv is not None and
                        Game(Options(), Mode.JIEQI).is_legal(mv))
                if result.get("err"):
                    rec("engine_error", result["err"])
            if own_engine:
                eng.quit()
        # 发布版新增：确认视觉窗口能构造、连线日志能写到 exe 同级
        try:
            from ui.vision_window import VisionWindow
            vw = VisionWindow(win)
            vw._on_log("SELFTEST: 连线日志落盘检查")
            logp = vw._log_path()
            rec("vision_log_path", logp)
            rec("vision_log_written", os.path.isfile(logp))
            vw.close()
        except Exception as exc:
            rec("vision_selftest_error", repr(exc))
        win.close()
        rec("SELFTEST", "OK")
        rc = 0
    except Exception:
        lines.append(traceback.format_exc())
        lines.append("SELFTEST = FAILED")
        rc = 1
    write_log("飞鲨象棋_自检.txt", "\n".join(lines), mode="w")
    return rc


def main():
    _install_excepthook()
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("飞鲨象棋")
    app.setApplicationDisplayName("飞鲨象棋 PC 版")
    app.setOrganizationName("FeishaChess")
    app.setFont(QFont("Microsoft YaHei", 10))
    if "--selftest" in sys.argv:
        sys.exit(run_selftest(app))
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

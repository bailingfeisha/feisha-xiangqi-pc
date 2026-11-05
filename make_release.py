# -*- coding: utf-8 -*-
"""生成发布版交付物：单 exe（内嵌模型）+ 外部引擎 + 说明 + 分发 zip。

由「一键打包单exe.bat」在 PyInstaller 成功后调用，也可单独运行：
    py -3.12 make_release.py
"""
from __future__ import annotations

import os
import shutil
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist")
EXE_NAME = "飞鲨象棋PC版.exe"
ZIP_NAME = "飞鲨象棋PC版_发布版.zip"
NOTE_NAME = "使用说明.txt"
QQ_GROUP = "1131198508"

NOTE = f"""飞鲨象棋 PC 版 · 发布版
========================================

【直接运行】
双击「{EXE_NAME}」即可，无需安装 Python 或任何依赖。

【已经打包进 exe 的内容】
  * 程序本体、PySide6 界面、Qt 插件
  * 识别模型 detector.onnx（YOLO11s int8）与 ONNX Runtime 推理库
  * 支持普通象棋 / 揭棋、人机对战、引擎联赛、窗口识别与连续自动下棋
  * 模拟器 ADB 触摸落子通道、落子方式自动降级、启动轮次自动判定

【没有打包、必须随程序一起提供的目录】
  engines\\   —— 引擎与其 NNUE 评估文件，放在 exe 同级目录。
  请把整个 engines 文件夹和 exe 放在同一个目录下，否则内置揭棋引擎
  AB JChess 1 RB 不会被自动加载。其他引擎可在「引擎管理」里自行添加。

【换识别模型】
本版已内置 detector.onnx。若要替换，把新的 detector.onnx 放到 exe 同级
目录（或 models\\ 下），程序会优先使用外置文件。

【连线 / 自动下棋常用设置】
  * 落子方式默认「自动」：普通窗口走后台消息（不占鼠标）；检测到模拟器
    （MuMu / 雷电 / 夜神等）优先 ADB 触摸，ADB 不可用或点击没生效会自动
    降到真实鼠标。模拟器的 Win32 后台消息通常无效，属正常现象。
  * ADB：在「对局控制 / 诊断」页点「ADB 探测 / 连接」。程序会**自动识别
    屏幕旋转**（雷电 9 常见 ROTATION_270：物理 1920×1080，但触摸坐标系
    是 1080×1920），一般不用手动设置；画面有黑边 / 侧边工具栏用 X/Y 微调，
    自动识别失效才勾「额外交换宽高」，也可手动指定 adb.exe。
  * 轮次：启动 / 重开时按当前局面判定「现在轮到我」（揭棋开局按已翻开的
    暗子数推断）。完整开局且我方执黑时会先等红方；「久等自动出招」默认
    8 秒，超时仍没检测到对手走子就自动出招（设 0 = 一直等；勾「等对手
    先走」时始终等）。急着测试可直接点「立即出招」，不用等超时。
  * 新功能：包干用时分配、思考细节（深度/评分/主变/候选）、后台思考、
    立即出招、变招（MultiPV）、着法不符诊断面板。

【发给别人】
只发「{ZIP_NAME}」一个压缩包即可，对方解压后双击 exe 使用。

【自检 / 排错】
命令行执行：{EXE_NAME} --selftest
结果写在 exe 同级的「飞鲨象棋_自检.txt」；崩溃信息见「飞鲨象棋_错误日志.txt」。
连线日志：视觉窗口的运行日志（含每 3 秒一条内部状态心跳，可直接看出卡在
判色 / 稳帧 / 思考 / 等对手 / 冷却）自动写入 exe 同级
「飞鲨象棋_连线日志.txt」；在「对局控制 / 诊断」页点「复制全部日志」
或「打开日志文件」即可，排查「不下棋」时把该文件发来。

【官方交流群】{QQ_GROUP}
（主窗口右下角、视觉识别窗口右下角、「帮助」菜单与「关于」里均可一键复制）

【注意】
窗口识别 / 自动下棋功能仅用于自己的本地软件测试与学习交流，
请勿用于在线对弈平台。
"""


def main() -> int:
    exe = os.path.join(DIST, EXE_NAME)
    if not os.path.isfile(exe):
        print(f"[失败] 未找到 {exe}，请先运行 PyInstaller 打包")
        return 1

    # 1) 同步外部引擎（引擎不嵌入 exe）
    src_engines = os.path.join(ROOT, "engines")
    dst_engines = os.path.join(DIST, "engines")
    if os.path.isdir(src_engines):
        shutil.copytree(src_engines, dst_engines, dirs_exist_ok=True)
        n = sum(len(fs) for _r, _d, fs in os.walk(dst_engines))
        print(f"[OK] 外部引擎目录：{dst_engines}（{n} 个文件）")
    else:
        print("[警告] 项目根目录没有 engines/，发布包将不含引擎")

    # 2) 分发说明（UTF-8 BOM，Windows 记事本可直接读）
    note = os.path.join(DIST, NOTE_NAME)
    with open(note, "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(NOTE)
    print(f"[OK] 说明：{note}")

    # 3) 发布版 zip
    zip_path = os.path.join(DIST, ZIP_NAME)
    if os.path.isfile(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=6) as z:
        z.write(exe, EXE_NAME)
        z.write(note, NOTE_NAME)
        if os.path.isdir(dst_engines):
            for cur, _dirs, files in os.walk(dst_engines):
                for name in files:
                    full = os.path.join(cur, name)
                    rel = os.path.relpath(full, DIST)
                    z.write(full, rel.replace("\\", "/"))

    def mb(p):
        return f"{os.path.getsize(p) / 1048576:.1f} MB"

    print(f"[OK] 单 exe：{exe}  {mb(exe)}")
    print(f"[OK] 发布包：{zip_path}  {mb(zip_path)}")
    print("发布版已就绪。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

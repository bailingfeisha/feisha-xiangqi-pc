# -*- coding: utf-8 -*-
"""视觉识别：目标窗口截图 → YOLO11 棋子检测 → 局面还原 → 连续同步 / 自动落子。

模块划分：
  model.py        YOLO11 ONNX 推理（letterbox + 解码 + NMS）
  capture.py      窗口 / 进程枚举与客户区截图
  reconstruct.py  检测框 → 9x10 网格 → Board / FEN（只识别主棋盘）
  clicker.py      棋盘格 → 屏幕坐标 → 模拟鼠标点击
  controller.py   QThread 连续循环 + 自动下棋状态机

用途声明：本模块面向自有 / 本地单机棋盘的识别与自动化测试，
采用「截图 + 模拟鼠标」方式，不含注入、内存读取、协议抓包或反检测。
"""

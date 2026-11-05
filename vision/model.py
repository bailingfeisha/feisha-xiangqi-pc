# -*- coding: utf-8 -*-
"""YOLO11s ONNX 棋子检测器。

模型（detector.onnx，量化 int8）：
  * 输入  images [1,3,640,640] float32，0..1，RGB（Ultralytics 默认）
  * 输出  output0 [1,23,8400]：前 4 行 xywh(中心/宽高，640 尺度)，后 19 行
          已 sigmoid 的类别分数；anchor-free，类别分数即置信度，需自己 NMS
  * 19 类：红/黑各 7 种棋子、红/黑暗子背面、board、tray、tray_badge
    本模块按需求只使用 0..16，忽略 tray(17) / tray_badge(18) 计数器。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import numpy as np

CLASS_NAMES = (
    "redBing", "redPao", "redMa", "redJu", "redXiang", "redShi", "redShuai",
    "blackBing", "blackPao", "blackMa", "blackJu", "blackXiang", "blackShi",
    "blackShuai", "back_red", "back_black", "board", "tray", "tray_badge",
)
NUM_CLASSES = len(CLASS_NAMES)
PIECE_CLASS_MAX = 13          # 0..13 明子
DARK_CLASSES = (14, 15)       # back_red / back_black
BOARD_CLASS = 16
IGNORED_CLASSES = frozenset((17, 18))   # 计数器/托盘：不参与局面还原

# 预览画框用色（BGR）
CLASS_COLORS = {
    **{i: (60, 60, 220) for i in range(0, 7)},     # 红子
    **{i: (220, 160, 40) for i in range(7, 14)},   # 黑子
    14: (90, 130, 235), 15: (235, 175, 90),        # 暗子背面
    16: (60, 200, 90),                              # 棋盘
    17: (160, 160, 160), 18: (160, 160, 160),
}


@dataclass
class Detection:
    x1: float
    y1: float
    x2: float
    y2: float
    conf: float
    cls: int
    name: str

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) / 2

    @property
    def cy(self) -> float:
        return (self.y1 + self.y2) / 2

    @property
    def w(self) -> float:
        return self.x2 - self.x1

    @property
    def h(self) -> float:
        return self.y2 - self.y1

    @property
    def is_board(self) -> bool:
        return self.cls == BOARD_CLASS

    @property
    def is_dark(self) -> bool:
        return self.cls in DARK_CLASSES

    @property
    def is_piece(self) -> bool:
        return self.cls <= 15      # 明子 + 暗子背面


def bundled_model_path() -> str:
    """发布版内嵌在单 exe 里的模型（PyInstaller onefile 的 _MEIPASS 解压目录）。"""
    import sys

    meipass = getattr(sys, "_MEIPASS", "")
    if not meipass:
        return ""
    for rel in ("detector.onnx", os.path.join("models", "detector.onnx")):
        p = os.path.join(meipass, rel)
        if os.path.isfile(p):
            return os.path.abspath(p)
    return ""


def default_model_path() -> str:
    """识别模型查找顺序：外置覆盖 → exe 内置 → 源码目录。

    发布版已把 detector.onnx 打进单 exe；若用户想换模型，只要把新的
    detector.onnx 放在 exe 同级（或 models/ 下）即可优先使用它。
    """
    from ai.engine import app_dir

    base = app_dir()
    for p in (os.path.join(base, "detector.onnx"),
              os.path.join(base, "models", "detector.onnx")):
        if os.path.isfile(p):
            return os.path.abspath(p)
    p = bundled_model_path()
    if p:
        return p
    src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(src, "detector.onnx")
    return os.path.abspath(p) if os.path.isfile(p) else ""


def model_source(path: str | None = None) -> str:
    """给界面显示模型来源：内置 / 外置 / 未找到。"""
    p = path or default_model_path()
    if not p or not os.path.isfile(p):
        return "未找到"
    bundled = bundled_model_path()
    if bundled and os.path.normcase(os.path.abspath(p)) == os.path.normcase(
            os.path.abspath(bundled)):
        return "exe 内置"
    return "外置文件"


def _nms(boxes: np.ndarray, scores: np.ndarray, iou_thr: float) -> list[int]:
    """纯 numpy NMS，避免依赖 cv2 版本差异。boxes 为 xyxy。"""
    if boxes.size == 0:
        return []
    x1, y1, x2, y2 = boxes.T
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(x1[i], x1[rest])
        yy1 = np.maximum(y1[i], y1[rest])
        xx2 = np.minimum(x2[i], x2[rest])
        yy2 = np.minimum(y2[i], y2[rest])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        union = areas[i] + areas[rest] - inter
        iou = np.where(union > 0, inter / union, 0.0)
        order = rest[iou <= iou_thr]
    return keep


class YoloDetector:
    """线程内独立持有 ORT session；每个 worker 线程创建自己的实例。"""

    def __init__(self, path: str | None = None, conf_thr: float = 0.35,
                 iou_thr: float = 0.45, imgsz: int = 640,
                 channel_order: str = "RGB", threads: int = 2):
        import onnxruntime as ort

        self.path = path or default_model_path()
        if not self.path or not os.path.isfile(self.path):
            raise FileNotFoundError(
                "未找到识别模型 detector.onnx（发布版已内置；"
                "如需替换请放到程序根目录或 models/ 下）")
        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, int(threads))
        so.inter_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(
            self.path, so, providers=["CPUExecutionProvider"])
        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name
        self.conf_thr = float(conf_thr)
        self.iou_thr = float(iou_thr)
        self.imgsz = int(imgsz)
        self.channel_order = channel_order.upper()
        self._ratio = 1.0
        self._pad = (0.0, 0.0)

    # ---------- 预处理 ----------
    def _letterbox(self, img: np.ndarray) -> np.ndarray:
        """等比缩放 + 114 填充，返回 [1,3,S,S] float32 0..1。"""
        import cv2

        h, w = img.shape[:2]
        s = self.imgsz
        r = min(s / max(1, w), s / max(1, h))
        nw, nh = max(1, round(w * r)), max(1, round(h * r))
        resized = (cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
                   if (nw, nh) != (w, h) else img)
        dx, dy = (s - nw) / 2.0, (s - nh) / 2.0
        left, top = int(round(dx - 0.1)), int(round(dy - 0.1))
        canvas = np.full((s, s, 3), 114, dtype=np.uint8)
        canvas[top:top + nh, left:left + nw] = resized
        self._ratio, self._pad = r, (float(left), float(top))
        data = canvas if self.channel_order == "RGB" else canvas[:, :, ::-1]
        blob = np.ascontiguousarray(
            data.transpose(2, 0, 1)[None], dtype=np.float32) / 255.0
        return blob

    # ---------- 推理 ----------
    def detect(self, image: np.ndarray) -> list[Detection]:
        """image: RGB uint8 [H,W,3]；返回还原到原图坐标的检测框（已 NMS）。"""
        if image is None or image.size == 0:
            return []
        img = np.asarray(image)
        if img.ndim == 2:
            import cv2
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)
        if img.dtype != np.uint8:
            img = np.clip(img, 0, 255).astype(np.uint8)
        h, w = img.shape[:2]
        blob = self._letterbox(img[:, :, :3])
        raw = self.session.run([self.output_name],
                               {self.input_name: blob})[0]
        pred = np.asarray(raw)[0].T                 # [8400, 23]
        if pred.shape[1] < 4 + NUM_CLASSES:
            return []
        xywh, cls_scores = pred[:, :4], pred[:, 4:4 + NUM_CLASSES]
        cls_ids = cls_scores.argmax(axis=1)
        confs = cls_scores.max(axis=1)
        mask = confs >= self.conf_thr
        if not mask.any():
            return []
        xywh, cls_ids, confs = xywh[mask], cls_ids[mask], confs[mask]

        cx, cy, bw, bh = xywh.T
        boxes = np.stack((cx - bw / 2, cy - bh / 2,
                          cx + bw / 2, cy + bh / 2), axis=1)
        # 还原 letterbox 缩放与填充
        r, (px, py) = self._ratio, self._pad
        boxes[:, [0, 2]] = (boxes[:, [0, 2]] - px) / r
        boxes[:, [1, 3]] = (boxes[:, [1, 3]] - py) / r
        boxes[:, [0, 2]] = np.clip(boxes[:, [0, 2]], 0, w)
        boxes[:, [1, 3]] = np.clip(boxes[:, [1, 3]], 0, h)

        result: list[Detection] = []
        for cls_id in np.unique(cls_ids):
            idx = np.where(cls_ids == cls_id)[0]
            keep = _nms(boxes[idx], confs[idx], self.iou_thr)
            for k in keep:
                j = int(idx[k])
                c = int(cls_id)
                result.append(Detection(
                    float(boxes[j, 0]), float(boxes[j, 1]),
                    float(boxes[j, 2]), float(boxes[j, 3]),
                    float(confs[j]), c, CLASS_NAMES[c]))
        result.sort(key=lambda d: d.conf, reverse=True)
        return result

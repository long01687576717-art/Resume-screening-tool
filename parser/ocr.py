"""图片文字识别（本地离线运行，识别结果先脱敏再发给 AI）。

用于图片格式的简历，以及没有文字层的扫描件 PDF。
使用 RapidOCR 3.x（PP-OCRv6 模型）：比旧版 rapidocr_onnxruntime 1.2.3 快约 30%，错字也明显更少。
"""
import threading
from statistics import median

# 平均置信度低于这个值时，在报告中提示识别质量。
# 新模型对清晰图片的置信度普遍在 0.95 以上，这个阈值还需要用更多模糊图片校准。
LOW_CONFIDENCE = 0.8

_engine = None
_engine_lock = threading.Lock()  # 多份简历并行处理时，避免重复加载模型


def _get_engine():
    # 识别模型加载较慢，第一次用到时才加载
    global _engine
    with _engine_lock:
        if _engine is None:
            from rapidocr import RapidOCR
            _engine = RapidOCR(params={"Global.log_level": "error"})
    return _engine


def ocr_image(image):
    """识别一张图片（文件路径或图像数组），返回 (按行整理的文字, 平均置信度)。"""
    result = _get_engine()(image)
    if not result.txts:
        return "", 0.0
    blocks = [_block(box, text, score) for box, text, score in zip(result.boxes, result.txts, result.scores)]
    confidence = sum(b["score"] for b in blocks) / len(blocks)
    return "\n".join(_group_lines(blocks)), confidence


def _block(box, text, score):
    ys = [float(p[1]) for p in box]
    return {"text": text, "score": float(score), "x": min(float(p[0]) for p in box),
            "y": (min(ys) + max(ys)) / 2, "height": max(ys) - min(ys)}


def _group_lines(blocks):
    """把文字块按纵向位置拼成行：中心高度相差不到半个字高的算同一行，行内从左到右排列。
    这样左右两栏并排的内容（如"姓名"和"出生年月"）会合成一行。"""
    tolerance = median(b["height"] for b in blocks) / 2
    lines = []
    for block in sorted(blocks, key=lambda b: b["y"]):
        if lines and abs(block["y"] - lines[-1]["y"]) <= tolerance:
            lines[-1]["blocks"].append(block)
        else:
            lines.append({"y": block["y"], "blocks": [block]})
    return ["  ".join(b["text"] for b in sorted(line["blocks"], key=lambda b: b["x"])) for line in lines]

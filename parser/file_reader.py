"""读取简历文件，统一转成纯文本。支持 PDF（含扫描件）、Word（.docx）、TXT 和图片。"""
import io
import logging
import unicodedata
from pathlib import Path

import numpy as np
import pdfplumber
from docx import Document

from parser.ocr import LOW_CONFIDENCE, ocr_image

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
SUPPORTED_SUFFIXES = {".pdf", ".docx", ".txt"} | IMAGE_SUFFIXES
SCANNED_PAGE_CHARS = 20  # PDF 某页提取到的文字少于这个数时，视为扫描页，改用图片识别
PDF_RENDER_DPI = 200     # 扫描页做图片识别时的分辨率
PAGE_IMAGE_WIDTH = 1460  # "原简历"显示 PDF 页面的目标宽度（像素）
PAGE_DPI_MIN, PAGE_DPI_MAX = 150, 200
# PDF 字体信息不完整时 pdfminer 会输出大量警告，不影响文字提取
logging.getLogger("pdfminer").setLevel(logging.ERROR)
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def read_file(path):
    """返回 (文字, 来源说明)。来源说明只在用了图片识别时才有，用于在报告里提醒 HR。"""
    path = Path(path)
    suffix = path.suffix.lower()
    note = None
    if suffix == ".pdf":
        text, note = _read_pdf(path)
    elif suffix == ".docx":
        text = _read_docx(path)
    elif suffix == ".txt":
        text = _read_txt(path)
    elif suffix in IMAGE_SUFFIXES:
        text, confidence = ocr_image(str(path))
        note = _ocr_note("图片文字识别", [confidence])
    elif suffix == ".doc":
        raise ValueError("暂不支持旧版 .doc 格式，请用 Word 另存为 .docx")
    else:
        raise ValueError(f"不支持的文件格式：{suffix}")

    return _clean(text), note


def read_bytes(name, data):
    """从内存里的文件内容读文字（网页"原简历"显示用，不写临时文件）。只支持 TXT 和 Word；
    和 read_file 共用同一套解码、Word 遍历和收尾处理，结果与读同一个文件完全一致。"""
    suffix = Path(name).suffix.lower()
    if suffix == ".txt":
        text = _decode_txt(data)
    elif suffix == ".docx":
        text = _read_docx(io.BytesIO(data))      # python-docx 可以直接读内存里的文件
    else:
        raise ValueError(f"read_bytes 目前只支持 .txt 和 .docx，不支持：{suffix}")
    return _clean(text)


def _clean(text):
    """统一收尾：部首字符换成正常汉字，去掉每行首尾空白和空行。"""
    text = "\n".join(line.strip() for line in normalize_chars(text).splitlines() if line.strip())
    if not text:
        raise ValueError("文件中没有识别到文字")
    return text


def pdf_pages(data):
    """把 PDF 的每一页转成图片（网页"原简历"里显示用）；只在内存里处理，不写硬盘。
    每页按 1460 像素宽渲染（Streamlit 显示图片的最大宽度，再宽会被它缩小一次），
    分辨率限制在 150～200 DPI：A4 约 176 DPI、Letter 约 172 DPI。原来固定 110 DPI，放大后文字发虚。"""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return [page.to_image(resolution=_page_dpi(page)).original for page in pdf.pages]


def _page_dpi(page):
    return min(PAGE_DPI_MAX, max(PAGE_DPI_MIN, PAGE_IMAGE_WIDTH * 72 / float(page.width)))


# 部分 Word / PDF 导出工具会把常用字存成"部首字符"（如"硕⼠"里的⼠是 U+2F20，不是汉字"士"），
# 看起来一模一样，但代码在原文里找"硕士"会找不到。读取时统一换成正常汉字。
RADICAL_SUPPLEMENT = dict(zip("⻓⻔⻅⻉⻋⻜⻢⻛⻩⻬⻮⻰⻳⻚⻝⻥⻦⻨⻤⻆⺠⺟⺩⻄⻘",
                              "长门见贝车飞马风黄齐齿龙龟页食鱼鸟麦鬼角民母王西青"))


def normalize_chars(text):
    out = []
    for ch in text:
        code = ord(ch)
        if 0x2F00 <= code <= 0x2FDF:        # 康熙部首：Unicode 标准里有对应的汉字
            ch = unicodedata.normalize("NFKC", ch)
        elif 0x2E80 <= code <= 0x2EFF:      # 部首补充：没有标准对应，只换常见的简化字形
            ch = RADICAL_SUPPLEMENT.get(ch, ch)
        out.append(ch)
    return "".join(out)


def _read_pdf(path):
    """逐页提取文字；没有文字层的扫描页改用图片识别。"""
    pages, confidences = [], []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if len("".join(text.split())) < SCANNED_PAGE_CHARS:
                image = page.to_image(resolution=PDF_RENDER_DPI).original.convert("RGB")
                text, confidence = ocr_image(np.array(image))
                confidences.append(confidence)
            pages.append(text)
    note = None
    if confidences:
        note = _ocr_note(f"其中 {len(confidences)}/{len(pages)} 页为扫描件，已用图片文字识别", confidences)
    return "\n".join(pages), note


def _ocr_note(prefix, confidences):
    confidence = sum(confidences) / len(confidences)
    note = f"{prefix}（平均置信度 {confidence:.2f}）"
    if confidence < LOW_CONFIDENCE:
        note += "，识别质量较低，个别文字可能有误，请以原件为准"
    return note


def _read_txt(path):
    return _decode_txt(path.read_bytes())


def _decode_txt(raw):
    """先按 UTF-8（自动去掉 BOM），再按 GBK；都不行就按 UTF-8 把认不出的字节换成 �。"""
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# ---------- Word ----------
# 简历模板常用表格和文本框排版，python-docx 的 doc.paragraphs 读不到这些内容，
# 所以直接按文档顺序遍历底层 XML。

def _read_docx(path):
    body = Document(path).element.body
    return "\n".join(_walk(body))


def _walk(element):
    for child in element:
        if child.tag == W + "p":
            yield from _paragraph_lines(child)
        elif child.tag == W + "tbl":
            yield from _table_lines(child)
        else:
            yield from _walk(child)


def _paragraph_lines(p):
    """段落本身的文字 + 段落里嵌入的文本框。"""
    yield "".join(t.text or "" for t in p.iter(W + "t") if _owner(t, W + "p") is p)
    for box in p.iter(W + "txbxContent"):
        # 同一个文本框在 Fallback 里还有一份兼容副本，跳过避免重复
        if _owner(box, W + "p") is p and not _inside_fallback(box):
            yield from _walk(box)


def _table_lines(tbl):
    """普通表格（每个单元格一行字）：一行表格合并成一行，单元格之间用" | "分隔。
    用表格做排版的简历（单元格里有多段文字）：逐个单元格、逐行输出，保留分行结构。"""
    for row in tbl.iter(W + "tr"):
        if _owner(row, W + "tbl") is not tbl:
            continue
        cells = []
        for cell in row.iter(W + "tc"):
            if _owner(cell, W + "tr") is row:
                lines = [line for line in _walk(cell) if line.strip()]
                if lines and (not cells or cells[-1] != lines):  # 合并单元格会重复出现
                    cells.append(lines)
        if all(len(lines) == 1 for lines in cells):
            yield " | ".join(lines[0] for lines in cells)
        else:
            for lines in cells:
                yield from lines


def _owner(element, tag):
    """向上找到最近的指定标签祖先。"""
    parent = element.getparent()
    while parent is not None and parent.tag != tag:
        parent = parent.getparent()
    return parent


def _inside_fallback(element):
    parent = element.getparent()
    while parent is not None:
        if parent.tag.endswith("Fallback"):
            return True
        parent = parent.getparent()
    return False

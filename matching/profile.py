"""候选人画像：把各模块的结构化数据合并成一份，给筛选使用。

画像的核心是统一的证据列表（evidence）：每条证据是"什么能力、做到什么程度、原句、出自哪段经历、什么情境"。
现在由各模块把已有数据转换成证据；以后整体优化改成统一的证据提取时，匹配部分不用改。
"""
import re

# 证据情境的说服力（"能不能接手工作"）：真实工作 > 项目 > 校园 / 课程实习 > 学过 > 自述
CONTEXT_RANK = {"全职工作": 5, "实习": 5, "项目": 4, "社会实践": 3, "校园经历": 3, "课程实习": 3,
                "团队项目": 2, "课程": 1, "证书": 1, "自述": 0, "": 2}
MIN_TEXT = 300          # 识别出的文字少于 300 字，很可能是解析失败
TEXT_FORMATS = (".txt", ".docx")
LOW_OCR = re.compile(r"识别质量较低")


def build(path, text, source_note, results):
    """合并各模块的画像，补全证据的情境，判断解析是否异常。"""
    profile = {"file": path.name, "evidence": []}
    for r in results:
        for key, value in r.profile.items():
            if key == "evidence":
                profile["evidence"] += value
            else:
                profile[key] = value
    _resolve_contexts(profile)
    profile["parse"] = _parse_check(path, text, source_note, profile)
    return profile


def _resolve_contexts(profile):
    """技能证据只知道出自哪段经历（where），按经历名称补上情境：实习 / 全职 / 校园……"""
    kinds = {}
    for e in profile.get("practice", {}).get("experiences", []):
        kinds[e["name"]] = e["kind"]
    for e in profile.get("campus", {}).get("experiences", []):
        kinds.setdefault(e["name"], "校园经历")
    for item in profile["evidence"]:
        if item["context"]:
            continue
        where = item["where"]
        kind = kinds.get(where) or next((k for name, k in kinds.items() if name and (name in where or where in name)), "")
        item["context"] = kind
    for item in profile["evidence"]:
        item["rank"] = CONTEXT_RANK.get(item["context"], 2)


def _parse_check(path, text, source_note, profile):
    """解析异常的简历放进"需人工查看"，不参与排序：否则格式特殊的简历会因为"什么都没写"被排到最后。"""
    reasons = []
    # 字少只有 PDF 和图片才可能是解析失败；txt / docx 的文字是直接读出来的，字少就是写得少
    if path.suffix.lower() not in TEXT_FORMATS and len(re.sub(r"\s+", "", text)) < MIN_TEXT:
        reasons.append("识别出的文字很少，可能解析失败")
    if source_note and LOW_OCR.search(source_note):
        reasons.append("图片识别质量较低")
    if not profile.get("education"):
        reasons.append("没有识别到教育经历")
    return {"source": source_note or "文字", "abnormal": bool(reasons), "reasons": reasons}

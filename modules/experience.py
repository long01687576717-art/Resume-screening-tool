"""经历提取：实习、社会实践、课程实习、校园经历共用的一次 AI 提取，以及几个模块共用的核对函数。

实习经历、校园经历、素质画像三个模块都用同一个提示词分析同一份简历，
AI 客户端会合并成一次调用（见 llm/client.py），所以不增加调用次数。
"""
import datetime
import re

from modules.base import clean_text as _text

EXPERIENCE_PROMPT = """你是简历信息提取助手。从用户给出的简历文本中提取实习、社会实践、课程实习、校园经历，以及教育阶段的时间，只输出 JSON。

规则：
1. 只提取简历中明确写出的信息，不要推测、不要编造；没写的字段填 null。原文摘录不要改写。
2. experiences：逐段提取。不要提取项目经历、竞赛（这些由其他模块处理）。教育背景里顺带写的职务（如"丨班长"）也要作为校园经历提取。
3. kind 只能是：
   - 实习：在某个单位担任岗位（XX实习生、助理、专员等），企业、机关、事业单位都算；
   - 全职工作：毕业后的正式工作（不是实习），如写在"工作经历"里、职位不带"实习"字样且在毕业之后；
   - 社会实践：挂职、志愿者、三下乡、"XX实干家""XX计划"等项目性质的经历；
   - 课程实习：学校课程安排的实习，如野外实习、教学实习、课程实习、认识实习；
   - 校园经历：在学校里的学生组织任职（班委、党团支部、学生会、社团等）、校内岗位（助教、导师助理、勤工助学）、兴趣社群和文体活动（如玩家群会长、主持队、组织球赛）；
   - 待核实：无法判断性质。
   kind_reason：判断依据的原文。
4. campus_type：仅校园经历填写，只能是：学生组织、校内岗位、兴趣社群；其他 kind 填 null。
5. org：单位或组织名称原文，没写填 null；name：没写单位时这段经历的名称原文（如"空间数据库实习"），写了单位的填 null；
   title：职位或身份原文，没写填 null；roles：原文写明"历任"的多个职务，逐个列出（如["班长","纪律委员"]），没有填 []；
   section：所在板块标题原文；stage：原文写明属于哪个学历阶段（如"本科阶段""研究生阶段"），没写填 null。
6. start、end：只填这段经历本身写明的时间，格式 YYYY.MM；结束时间写"至今"的 end 填"至今"；只写了一个月份的，start 和 end 相同。不要借用其他经历或教育背景的时间。
7. part_time：原文写明"兼职"填 true，否则 false。
8. duties：每一条工作描述，逐条列出：
   - text：原文，完整摘录。只写收获、能力提升的句子（如"提升了沟通能力"）不要单独列为一条。
   - level：承担了多少判断，只能是 1、2、3。只看做了什么事，不看职位名称：
     1 = 辅助：在指导下做辅助性、事务性工作，如协助、整理、录入、归档、会议纪要、后勤；
     2 = 独立：独立完成一块完整的常规工作；
     3 = 改进：解决非常规问题或改进原有做法，如设计方案、搭建模型或工具、优化流程，并且有结果。
   - basis：定级依据的原文词语；specifics：涉及的具体对象或数字，原样摘录，多个用"、"分隔，没有填 null；
   - result：原文写明的结果，原样摘录，没有填 null。
9. signals：这段经历中出现的下列信号，每个信号一条，没有填 []。type 只能是：
   - 被选举任命：原文写明被选为、当选、任命；
   - 连任：原文写明连任、连续担任、连续 N 年；
   - 职责变多：原文写明中期起独立负责、后来负责、晋升、留用、转正；
   - 获认可：获奖、获评、评为、通过评审、表扬（要和这段经历直接相关）；
   - 自发组织：原文写明自己发起、创办、自发组织、主动组织；
   - 推动他人：没有上下级关系时推动别人一起做事，如推动其他部门整改、协调多方、统筹团队；
   - 组织活动：组织、策划了具体的活动或比赛。
   text：体现这个信号的原文。
10. education：每段教育经历。school：学校；stage：只能是 专科、本科、硕士、博士；major：专业原文；start、end：原文写明的时间（YYYY.MM）。
11. self_claims：自我评价、个人优势、核心优势等自我总结板块里的说法，一句或一条一项：
    - text：原文；
    - kind：具体（写了具体的经验、成果或能力，如"曾参与8项采购项目""具备财务数据分析实践经验"）或 形容词（只有性格、态度的形容，如"责任心强""沟通能力较好"）；
    - evidence：在其他板块（实习、项目、校园、教育、荣誉）中能证明这句话的原文句子，找不到填 null。不要用自我总结板块里的句子当证据。
12. job_direction：原文写明的求职方向或求职意向（如"游戏运营""采购管理及供应链管理"），没写填 null。

输出格式示例：
{
  "experiences": [
    {"org": "某某科技有限公司", "name": null, "title": "数据分析实习生", "roles": [], "section": "实习经历", "stage": null,
     "kind": "实习", "kind_reason": "数据分析实习生", "campus_type": null,
     "start": "2025.06", "end": "2025.09", "part_time": false,
     "duties": [
       {"text": "协助整理客户资料并归档", "level": 1, "basis": "协助", "specifics": null, "result": null},
       {"text": "用 Python 搭建销售周报自动化脚本，替代原手工报表，周报制作时间由 2 天缩短到 2 小时", "level": 3, "basis": "搭建", "specifics": "销售周报", "result": "替代原手工报表，周报制作时间由 2 天缩短到 2 小时"}
     ],
     "signals": [{"type": "职责变多", "text": "中期起独立负责周报"}]}
  ],
  "education": [{"school": "某某大学", "stage": "本科", "major": "统计学", "start": "2020.09", "end": "2024.06"}],
  "self_claims": [{"text": "具备数据分析实践经验", "kind": "具体", "evidence": "用 Python 搭建销售周报自动化脚本"},
                  {"text": "责任心强", "kind": "形容词", "evidence": null}],
  "job_direction": "数据分析"
}"""

KINDS = ("实习", "全职工作", "社会实践", "课程实习", "校园经历", "待核实")
WORK_KINDS = ("实习", "全职工作")  # 目前只做校招：全职工作和实习放在一起分析
CAMPUS_TYPES = ("学生组织", "校内岗位", "兴趣社群")
SIGNAL_TYPES = ("被选举任命", "连任", "职责变多", "获认可", "自发组织", "推动他人", "组织活动")
# 给 HR 看的说法（代码内部仍用 1～3，设计记录里叫 L1～L3）
LEVEL_NAMES = {1: "协助完成", 2: "独立负责", 3: "主导改进，有成果"}
DEPTH_NAMES = {1: "浅", 2: "中", 3: "深"}

# L3 的结果必须是别人认可或采用、或有数字的改进，自己的评价（"提升效率"）不算
ADOPTED = re.compile(r"通过|替代|取代|上线|采纳|采用|投入使用|推广|落地|获评|认可|表扬|留用|转正")
QUANT_CHANGE = re.compile(r"(提升|提高|降低|减少|缩短|节省|节约|增长|增加|压缩)[^，。；,;]{0,12}\d")
# 本人角色是协助；"辅助""帮助"常用来说产品用途（"辅助 HR 自查"），不算
ASSIST = re.compile(r"协助|配合")
OWN = re.compile(r"独立|负责|主导|牵头")
DATE = re.compile(r"(?:19|20)\d{2}\s*[.\-/年]\s*\d{1,2}")


def extract(llm, text):
    """经历提取。几个模块同时调用时，AI 客户端只真正请求一次。"""
    return llm.extract_json(EXPERIENCE_PROMPT, text)


def build_entries(data, source, today=None):
    """整理 AI 提取的经历：核对时间、逐条核对工作描述和信号。不含公司匹配（实习模块自己做）。"""
    today = today or datetime.date.today()
    written = {month(m.group(), today) for m in DATE.finditer(source)}
    entries = []
    for raw in data.get("experiences") or []:
        if not isinstance(raw, dict):
            continue
        org = _text(raw.get("org"))
        name = org or _text(raw.get("name")) or _text(raw.get("kind_reason"))
        if not name:
            continue
        kind = _text(raw.get("kind"))
        start_text, end_text = _text(raw.get("start")), _text(raw.get("end"))
        start, end = month(start_text, today), month(end_text, today)
        ongoing = "至今" in end_text or "现在" in end_text
        # 时间必须能在原文找到（格式可以不同），防止 AI 按常识补全
        if start not in written:
            start = None
        if end not in written and not ongoing:
            end = None
        # 结束时间在未来的（如"2024.09-2027.06"是预计毕业时间），时长只算到今天
        now = today.year * 12 + today.month
        months = min(end, now) - start if start is not None and end is not None and end >= start else None
        campus_type = _text(raw.get("campus_type"))
        duties = [d for d in (build_duty(d, source) for d in raw.get("duties") or []) if d]
        entries.append({
            "org": name, "raw_org": org,
            "title": _text(raw.get("title")),
            "roles": [r for r in (_text(x) for x in raw.get("roles") or []) if r and squash(r) in source],
            "stage": _text(raw.get("stage")),
            "kind": kind if kind in KINDS else "待核实",
            "campus_type": campus_type if campus_type in CAMPUS_TYPES else "",
            "start": start, "end": end, "months": months,
            "start_text": start_text, "end_text": end_text,
            "ongoing": ongoing, "single_month": months == 0,
            "part_time": raw.get("part_time") is True and "兼职" in source,
            "duties": duties,
            "level": max((d["level"] for d in duties), default=0),
            "signals": _signals(raw.get("signals"), source),
        })
    return entries


def build_education(data, source, today=None):
    """教育阶段的起止时间，用来检查校园经历的时间是否超出所在学历阶段。"""
    today = today or datetime.date.today()
    stages = []
    for raw in data.get("education") or []:
        if not isinstance(raw, dict):
            continue
        start, end = month(_text(raw.get("start")), today), month(_text(raw.get("end")), today)
        stage = _text(raw.get("stage"))
        if start is None or end is None or stage not in ("专科", "本科", "硕士", "博士"):
            continue
        stages.append({"school": _text(raw.get("school")), "stage": stage, "major": _text(raw.get("major")),
                       "start": start, "end": end})
    return sorted(stages, key=lambda s: s["start"])


def build_self_claims(data, source):
    """自我评价里的具体说法和它的佐证。形容词不核对；佐证必须在原文里，且不能是自我总结板块自己的句子。"""
    claims = []
    raw_claims = [c for c in data.get("self_claims") or [] if isinstance(c, dict)]
    own = [squash(_text(c.get("text"))) for c in raw_claims]
    for raw in raw_claims:
        text, evidence = _text(raw.get("text")), _text(raw.get("evidence"))
        if _text(raw.get("kind")) != "具体" or not text or squash(text)[:12] not in source:
            continue
        body = squash(evidence)
        valid = evidence and body[:12] in source and not any(body[:12] in o for o in own)
        claims.append({"text": text, "evidence": evidence if valid else ""})
    return claims


def job_direction(data):
    """求职方向：先提取存着，以后做动机 / 方向一致性和 JD 匹配时使用。"""
    return _text(data.get("job_direction"))


def _signals(raw_signals, source):
    signals = []
    for raw in raw_signals or []:
        if not isinstance(raw, dict):
            continue
        kind, text = _text(raw.get("type")), _text(raw.get("text"))
        # 信号的原文必须能在简历里找到
        if kind in SIGNAL_TYPES and text and squash(text)[:12] in source:
            signals.append({"type": kind, "text": text})
    return signals


def build_duty(raw, source):
    if not isinstance(raw, dict):
        return None
    text = _text(raw.get("text"))
    # 工作描述要能在原文找到（比较前 12 个字，去掉空白后比较，兼容 PDF 换行）
    if not text or squash(text)[:12] not in source:
        return None
    body = squash(text)
    level = raw.get("level") if raw.get("level") in (1, 2, 3) else 1
    specifics = [s for s in re.split(r"[、，,；;]", _text(raw.get("specifics"))) if s.strip() and squash(s) in body]
    result = found_parts(_text(raw.get("result")), body)
    # 只保留别人认可 / 采用、或有数字的结果；"提升了效率"这类自我评价不算结果
    strong = result and (ADOPTED.search(result) or QUANT_CHANGE.search(result))
    result = result if strong else ""
    # 依据词必须出现在这条原句里（不在职位名称里查找），否则降一级
    if not basis_found(_text(raw.get("basis")), body):
        level = max(level - 1, 1)
    # 说得越大，证据要越多：L3 要有被认可 / 采用或有数字的改进，L2 要有具体对象或数字
    if level == 3 and not result:
        level = 2
    if level == 2 and not (specifics or re.search(r"\d", body)):
        level = 1
    # 协助类工作最高 L1
    if ASSIST.search(body) and not OWN.search(body):
        level = 1
    return {"text": text, "level": level, "result": result}


# ---------- 工具 ----------

def month(text, today=None):
    """"2025.07""2025-07""2025年7月" → 月份序号；"至今"按今天计算。"""
    if not text:
        return None
    if "至今" in text or "现在" in text:
        today = today or datetime.date.today()
        return today.year * 12 + today.month
    m = re.search(r"((?:19|20)\d{2})\s*[.\-/年]\s*(\d{1,2})", text)
    return int(m.group(1)) * 12 + int(m.group(2)) if m else None


def month_text(index):
    year, month_ = divmod(index, 12)
    if month_ == 0:
        year, month_ = year - 1, 12
    return f"{year}.{month_:02d}"


def found_parts(text, body):
    """AI 可能把原文不相连的几处拼成一句、或改动个别字：按分号拆开，只保留原文里找得到的部分。"""
    parts = [p.strip() for p in re.split(r"[；;]", text or "") if p.strip() and squash(p) in body]
    return "；".join(parts)


def basis_found(basis, body):
    """依据词可能写成"开发...优化"：按省略号拆开，每段都要在原句里。"""
    parts = [p for p in re.split(r"\.{2,}|…+|、", basis or "") if p.strip()]
    return bool(parts) and all(squash(p) in body for p in parts)


def squash(text):
    return re.sub(r"\s+", "", text or "")


def short(text, width=30):
    text = text.strip()
    return text if len(text) <= width else text[:width] + "……"

"""教育背景模块：把教育经历抽象成三个维度，供后续综合判断使用。

- 院校层次：学历 + 学校梯队，以最高学历为主，第一学历作补充
- 学业表现：排名、绩点，以最近一段学历为主，其他阶段一并列出
- 学业发展轨迹：学历路径、院校层次变化、推免、专升本、跨专业等，只描述不分档

AI 只负责从简历中提取事实（学校、学历、时间、排名等），
所有档位判断都由下面的规则 + 知识库完成，保证结果稳定、可解释。
奖学金留给后续的"奖励"模块分析。
"""
import re

from modules.base import Module, ModuleResult, clean_text as _text

SYSTEM_PROMPT = """你是简历信息提取助手。从用户给出的简历文本中提取教育经历，只输出 JSON。

规则：
1. 只提取简历中明确写出的信息，不要推测、不要编造；没写的字段填 null。
2. 每一段学历（专科 / 本科 / 硕士 / 博士）是一条记录；交换、访学、夏令营不单独成条，写进所属学历的 exchange 字段。
3. major 只写主修专业的中文名称，原文为外文时翻译成常用中文名称，原文写入 major_raw。双学位或辅修专业写入 second_major（专业名称）和 second_major_type（"双学位"或"辅修"），没有则填 null。school 写学校全称；简历用简称时补全为常用全称，并把原文写入 school_raw。分校区保留括号，如"哈尔滨工业大学（深圳）"。is_overseas 表示是否为境外（含港澳台）院校。
4. 日期统一写成 "YYYY-MM"，只有年份时写 "YYYY"；写"至今"时 end 填 null、ongoing 填 true。只填教育经历本身写明的起止时间，不要用校园经历、实习等其他经历的时间代替；没写就填 null。
5. degree 只能是：博士、硕士、本科、专科、未注明。只有原文出现学历字样时才填写，否则必须填"未注明"，不要根据学校或经历补全。"学士"算本科，"研究生"算硕士，"大专""高职"算专科。专升本的本科阶段 degree 填"本科"，is_upgrade 填 true。简历其他位置明确写了学历的（如开头写"2027届硕士研究生"），也算写明，按原文填写。
   只有 degree 为"未注明"时，才填写 degree_guess：根据出生年月与在校时间、其他经历的时间线等线索判断学历（只能是博士、硕士、本科、专科），并在 degree_guess_reason 中用一句话写明依据；线索不足时两项都填 null。degree 写明了的，这两项填 null。
6. recommended_admission：这段学历是否通过推免（保研）入学。简历写"推免至某大学"时，标在该大学那段学历上。
7. study_form 只能是：全日制、非全日制、自考、成人教育、网络教育、未注明。"统招"算全日制。只有原文出现这些字样时才填写，否则必须填"未注明"，不要按常识补全。
8. academic_records：简历中的每一条排名、GPA、均分都单独作为一条记录。一句话里有多条就拆成多条，不要合并，也不要舍弃任何一条。
   - type 只能是：排名、GPA、均分。text 为这一条自己的原文。
   - 排名填 position（名次）、total（总人数）、percent（百分比数字，"前10%" 填 10）；GPA 填 value 和 scale（满分，没写填 null）；均分填 value。
   - stated_degree：只看这一条自己的原文，原文里带学历字样（如"本科专业排名前 15%"中的"本科"）才填，否则填 null。同一句里其他记录的学历字样不能沿用。
   - under_entry：这一条写在某段学历的同一行，或写在该段学历之后、下一段学历（或下一个板块标题）之前时，填该段学历的 degree；否则填 null。学历用表格列出时，表格下方的内容不属于表格最后一行。
9. major_category：按教育部专业目录给出该专业的学科门类和专业类。这一项允许根据专业名称判断。
10. source 填该段学历在简历中的原文（一行以内）。

输出格式示例：
{
  "entries": [
    {
      "school": "华中科技大学",
      "school_raw": "华科",
      "is_overseas": false,
      "degree": "硕士",
      "degree_guess": null,
      "degree_guess_reason": null,
      "major": "计算机技术",
      "major_category": {"discipline": "工学", "major_class": "计算机类"},
      "second_major": null,
      "second_major_type": null,
      "recommended_admission": false,
      "start": "2023-09",
      "end": "2026-06",
      "ongoing": false,
      "study_form": "全日制",
      "is_upgrade": false,
      "courses": ["机器学习", "分布式系统"],
      "exchange": null,
      "source": "2023.09-2026.06 华中科技大学 计算机技术 硕士"
    }
  ],
  "academic_records": [
    {"type": "GPA", "text": "GPA 3.6/4.0", "value": 3.6, "scale": 4.0, "stated_degree": null, "under_entry": "硕士"},
    {"type": "排名", "text": "本科专业排名前 15%", "position": null, "total": null, "percent": 15, "stated_degree": "本科", "under_entry": null}
  ]
}"""

DEGREES = ("博士", "硕士", "本科", "专科")
DEGREE_ALIASES = {"学士": "本科", "研究生": "硕士", "大专": "专科", "高职": "专科"}
# 核对原文用：AI 填写的学历、学习形式必须能在简历原文中找到对应字样，找不到就视为没写（防止 AI 按常识补全）
DEGREE_EVIDENCE = {
    "博士": re.compile(r"博士|Ph\.?D", re.I),
    "硕士": re.compile(r"硕士|研究生|Master|M\.?Sc|MBA|MPA|MPAcc|MEng", re.I),
    "本科": re.compile(r"本科|学士|专升本|Bachelor|B\.?Sc|B\.?Eng", re.I),
    "专科": re.compile(r"专科|大专|高职"),
}
FORM_EVIDENCE = {
    "全日制": re.compile(r"(?<!非)全日制|统招"),
    "非全日制": re.compile(r"非全日制"),
    "自考": re.compile(r"自考|自学考试"),
    "成人教育": re.compile(r"成人|成教|函授|夜大"),
    "网络教育": re.compile(r"网络教育|远程教育"),
}
# 专科院校：职业技术学院、职业学院、高等专科学校等（"职业技术大学""职业大学"是职业本科，不在此列）
VOCATIONAL_COLLEGE = re.compile(r"职业(?:技术)?学院|高等专科学校|专科学校")
FORMS = ("全日制", "非全日制", "自考", "成人教育", "网络教育")
MAX_MONTHS = {"专科": 36, "本科": 48, "硕士": 36, "博士": 72}  # 常规学制
TOLERANCE_MONTHS = 6
GAP_MONTHS = 6
MIN_RANK_TOTAL = 3     # 专业人数少于 3 人：排名不用于分档，只展示
SMALL_RANK_TOTAL = 10  # 专业人数 3～9 人：小样本，排在绩点之后使用
DATE_RE = re.compile(r"^(\d{4})(?:\D+(\d{1,2}))?")


class EducationModule(Module):
    title = "教育背景"

    def analyze(self, resume, context):
        kb, llm = context["kb"], context["llm"]
        data = llm.extract_json(SYSTEM_PROMPT, resume.text)
        result = ModuleResult(self.title, extracted=data)

        entries = [self._build_entry(raw, kb, resume.text) for raw in data.get("entries") or [] if isinstance(raw, dict)]
        if not entries:
            result.add("", "简历中未识别到教育经历")
            return result
        # 按入学时间排序，没写时间的放最后
        entries.sort(key=lambda e: e["start"] or (9999, 12))
        _assign_records(data.get("academic_records") or [], entries)
        for e in entries:
            e["tier"], e["tier_label"] = _school_tier(e)
            e["tier_level"] = kb.tier_level(e["tier"], e["match"].record if e["match"] else None)
            e["performance"] = _stage_performance(e)

        # 摘要：三个维度
        self._school_level(entries, result)
        self._academic_performance(entries, result)
        trend = self._development(entries, result)
        # 详情：按学历阶段展开，供核实
        self._details(entries, result)
        # 需要核实的事项
        self._checks(entries, result)
        result.profile = {"education": {**_profile(entries), "trend": trend}}
        return result

    # ---------- 整理 AI 提取的数据 ----------

    def _build_entry(self, raw, kb, text):
        school = _text(raw.get("school")) or _text(raw.get("school_raw"))
        degree, degree_source = _resolve_degree(raw, school, text)
        form = _text(raw.get("study_form"))
        if form in FORM_EVIDENCE and not FORM_EVIDENCE[form].search(text):
            form = ""  # 原文没有对应字样，视为没写
        major = _text(raw.get("major"))

        category = kb.match_major(major)
        category_source = "知识库"
        if not category:
            ai = raw.get("major_category") or {}
            if _text(ai.get("discipline")) and _text(ai.get("major_class")):
                category = (_text(ai["discipline"]), _text(ai["major_class"]))
                category_source = "AI 归类"

        return {
            "degree": degree,
            "degree_source": degree_source,  # 空表示简历写明；否则为（标注, 依据）
            "school": school,
            "match": kb.match_school(school),
            "overseas": bool(raw.get("is_overseas")),
            "major": major,
            "category": category,
            "category_source": category_source,
            "second_major": _text(raw.get("second_major")),
            "second_major_type": _text(raw.get("second_major_type")) or "双学位/辅修",
            "second_category": kb.match_major(_text(raw.get("second_major"))),
            "start": _parse_date(raw.get("start")),
            "end": _parse_date(raw.get("end")),
            "ongoing": bool(raw.get("ongoing")),
            "form": form if form in FORMS else "",
            "upgrade": bool(raw.get("is_upgrade")),
            "recommended": bool(raw.get("recommended_admission")),
            "courses": [c for c in (_text(c) for c in raw.get("courses") or []) if c],
            "exchange": _text(raw.get("exchange")),
            "ranks": [],   # 由 _assign_records 按规则填入
            "scores": [],
        }

    # ---------- 维度一：院校层次 ----------

    def _school_level(self, entries, result):
        highest = _highest(entries)
        text = f"{_degree_name(highest)} · {_school_desc(highest)}"
        first = entries[0]
        if first is not highest:
            text += f"｜第一学历 {_degree_name(first)} · {first['tier']}（{_school_name(first)}）"
        result.add("院校层次", text)

    # ---------- 维度二：学业表现 ----------

    def _academic_performance(self, entries, result):
        rated = [e for e in entries if e["performance"]]
        if not rated:
            result.add("学业表现", "未提供（简历中没有排名或成绩，不代表成绩差）")
            return
        main = _main_rated(rated)
        parts = [f"{main['performance'][0]}（{_degree_name(main)}：{main['performance'][1]}）"]
        parts += [f"{_degree_name(e)}：{e['performance'][0]}（{e['performance'][1]}）" for e in rated if e is not main]
        missing = [_degree_name(e) for e in entries if not e["performance"]]
        if missing:
            parts.append(f"{'、'.join(missing)}未提供成绩")
        result.add("学业表现", "｜".join(parts))

    # ---------- 维度三：学业发展轨迹 ----------

    def _development(self, entries, result):
        parts = []
        if len(entries) > 1:
            parts.append(" → ".join(_degree_name(e) for e in entries))
        for prev, cur in zip(entries, entries[1:]):
            if prev["tier_level"] is not None and cur["tier_level"] is not None and prev["tier_level"] != cur["tier_level"]:
                direction = "提升" if cur["tier_level"] < prev["tier_level"] else "下降"
                parts.append(f"院校层次{direction}（{prev['tier']} → {cur['tier']}）")
        for e in entries:
            if e["recommended"]:
                parts.append(f"{_degree_name(e)}推免入学")
        if any(e["upgrade"] for e in entries):
            parts.append("专升本")
        for prev, cur in zip(entries, entries[1:]):
            # 专升本推断：专科之后接一段不超过 2.5 年的本科
            if (prev["degree"] == "专科" and cur["degree"] == "本科" and not cur["upgrade"]
                    and cur["start"] and cur["end"] and _months_between(cur["start"], cur["end"]) <= 30):
                parts.append("推断为专升本（专科后接约 2 年本科）")
            if prev["category"] and cur["category"] and prev["category"][1] != cur["category"][1]:
                parts.append(f"跨专业（{prev['major']} → {cur['major']}）")
            gap = _gap_months(prev, cur)
            if gap > GAP_MONTHS:
                parts.append(f"{_degree_name(prev)}与{_degree_name(cur)}之间空档约 {gap} 个月")
        if any(e["exchange"] for e in entries):
            parts.append("有交换/访学经历")
        text = "｜".join(parts) if parts else "单段学历，无特殊变化"
        result.add("学业发展轨迹", text)
        return text

    # ---------- 详情：按学历阶段展开 ----------

    def _details(self, entries, result):
        result.add("", "")
        for e in entries:
            label = f"{e['degree'] or '学历'}详情"
            recommended = "，推免入学" if e["recommended"] else ""
            extra = "，".join(x for x in (e["tier_label"], recommended.lstrip("，")) if x)
            result.add(label, f"{_school_name(e)}｜{e['tier']}" + (f"（{extra}）" if extra else ""))
            result.add("", f"{_major_desc(e)}｜{e['form'] or '学习形式未注明'}｜{_period(e)}")
            if e["second_major"]:
                path = " → ".join(e["second_category"]) + " → " if e["second_category"] else ""
                result.add("", f"　└ {e['second_major_type']}：{path}{e['second_major']}")
            if e["courses"]:
                courses = "、".join(e["courses"][:8]) + ("等" if len(e["courses"]) > 8 else "")
                result.add("", f"　└ 主修课程：{courses}")
            if e["exchange"]:
                result.add("", f"　└ 交换/访学：{e['exchange']}")
            if e["performance"]:
                level, basis, used = e["performance"]
                result.add("", f"学业表现：{level}（{basis}）")
                others = [_record_desc(r) for r in e["ranks"] + e["scores"] if r is not used]
                if others:
                    result.add("", f"　└ 另有：{'；'.join(others)}")
            elif e["ranks"]:
                result.add("", f"学业表现：未分档（{'；'.join(_record_desc(r) for r in e['ranks'])}）")

    # ---------- 需要核实的事项 ----------

    def _checks(self, entries, result):
        for e in entries:
            name = _degree_name(e)
            if _ranks_conflict(e["ranks"]):
                texts = "；".join(r["text"] for r in e["ranks"])
                result.notes.append(f"{name}的排名信息互相矛盾（{texts}），未用于分档，请核对原文")
            if e["form"] and e["form"] != "全日制":
                result.notes.append(f"{name}阶段为{e['form']}")
            if e["match"] and e["match"].adult_edu:
                result.notes.append(f"{name}阶段就读于继续教育/网络教育学院，可能为非全日制，建议确认")
            if e["match"] and e["match"].independent:
                result.notes.append(f"{name}院校「{_school_name(e)}」疑似独立学院，请核实")
            if e["degree_source"]:
                reason = e["degree_source"][1]
                result.notes.append(f"「{_school_name(e)}」这段经历未注明学历，{reason}，按{e['degree']}处理，请核实")
            if _overran(e):
                months = _months_between(e["start"], e["end"])
                result.notes.append(
                    f"{e['degree']}阶段历时约 {months / 12:.1f} 年，超过常规的 {MAX_MONTHS[e['degree']] // 12} 年"
                    f"（可能是五年制专业、休学或延毕），建议确认")

        # 学习形式：只在有疑点时提示（很少有人在简历里写"全日制"，没疑点就不打扰）
        if _has_doubt(entries):
            unnoted = [_degree_name(e) for e in entries if not e["form"] and not e["overseas"]]
            if unnoted:
                result.notes.append(f"{'、'.join(unnoted)}阶段未注明学习形式，且学历路径存在疑点，建议确认是否为全日制")

        if any(r["guessed"] for e in entries for r in e["ranks"] + e["scores"]):
            result.notes.append("部分成绩未写明属于哪段学历，已按最高学历处理（标注「归属为推断」），请核对原文")


# ---------- 判断规则 ----------

def _school_tier(e):
    """返回 (档位, 说明)。"""
    match = e["match"]
    if e["degree"] == "专科":
        return "基础", "专科层次"
    if match and match.independent:
        return "基础", "疑似独立学院"
    if match and match.record:
        return match.record["tier"], match.record["label"]
    if re.search(r"职业|专科", e["school"]):
        return "基础", "专科院校"
    if e["overseas"]:
        return "待定", "境外院校，未收录于排名库，需人工判断"
    return "未分档", "未收录于院校库和软科排名"


def _stage_performance(e):
    """某段学历的学业表现，返回 (档位, 依据描述, 依据记录)；没有可用成绩时返回 None。
    判断顺序：排名（10 人及以上）→ 绩点 / 均分 → 小样本排名（3～9 人）。"""
    ranks = [] if _ranks_conflict(e["ranks"]) else e["ranks"]
    large = [r for r in ranks if not r["total"] or r["total"] >= SMALL_RANK_TOTAL]
    small = [r for r in ranks if r["total"] and MIN_RANK_TOTAL <= r["total"] < SMALL_RANK_TOTAL]
    if large:
        r = large[0]
        return _level_by_rank(r["pct"]), f"{_rank_desc(r)}{_guess_mark(r)}", r
    if e["scores"]:
        r = e["scores"][0]
        level, desc = _grade_by_score(r)
        return level, desc + _guess_mark(r), r
    if small:
        r = small[0]
        return _level_by_rank(r["pct"]), f"{_rank_desc(r)}{_sample_mark(r)}{_guess_mark(r)}", r
    return None


def _rank_desc(r):
    """排名描述；原文没写百分比时补上换算结果，如"专业第一（1/121），约前 1%"。"""
    return r["text"] if "%" in r["text"] else f"{r['text']}，约前 {r['pct']:.0f}%"


def _level_by_rank(pct):
    return "优秀" if pct <= 10 else "良好" if pct <= 30 else "一般"


def _grade_by_score(record):
    """用均分或 GPA 分档，返回 (档位, 描述)。"""
    value, scale = record["value"], record["scale"]
    if record["type"] == "均分":
        return _level_by_score(value), f"均分 {value:g}"

    notes = []
    if not scale:
        scale = 100 if value > 5 else 4.0 if value <= 4.0 else 5.0
        notes.append(f"未注明满分，按 {scale:g} 分制估算")

    if scale >= 10:
        level = _level_by_score(value * 100 / scale)
    elif scale == 5:
        score = value * 10 + 50  # 常见的 5 分制换算：绩点 =（分数 − 50）÷ 10
        level = _level_by_score(score)
        notes.insert(0, f"约合均分 {score:.0f}，换算值")
    else:
        gpa4 = value * 4 / scale
        level = "优秀" if gpa4 >= 3.7 else "良好" if gpa4 >= 3.3 else "一般"
        if scale != 4:
            notes.insert(0, f"折合 4.0 制约 {gpa4:.2f}，换算值")

    desc = f"GPA {value:g}/{scale:.1f}" if scale < 10 else f"成绩 {value:g}/{scale:g}"
    if notes:
        desc += f"（{'；'.join(notes)}）"
    return level, desc


def _level_by_score(score):
    return "优秀" if score >= 90 else "良好" if score >= 85 else "一般"


def _assign_records(raw_records, entries):
    """把成绩记录归到对应学历：原文写明的 → 所在位置 → 都没有则归最高学历（标记为推断）。"""
    highest = _highest(entries)
    for raw in raw_records:
        record = _build_record(raw)
        if not record:
            continue
        target = _degree_alias(raw.get("stated_degree")) or _degree_alias(raw.get("under_entry"))
        matched = [e for e in entries if target and e["degree"] == target]
        entry = matched[-1] if matched else highest
        record["guessed"] = not matched
        entry["ranks" if record["type"] == "排名" else "scores"].append(record)


def _build_record(raw):
    """整理一条成绩记录；数据不完整时返回 None。"""
    if not isinstance(raw, dict):
        return None
    kind, text = _text(raw.get("type")), _text(raw.get("text"))
    if kind == "排名":
        pct = _rank_percent(raw)
        if pct is None:
            return None
        return {"type": kind, "text": text or f"前 {pct:.0f}%", "pct": pct, "total": _number(raw.get("total")),
                "self_conflict": _rank_self_conflict(raw)}
    if kind in ("GPA", "均分"):
        value = _number(raw.get("value"))
        if not value:
            return None
        return {"type": kind, "text": text, "value": value, "scale": _number(raw.get("scale"))}
    return None


def _rank_percent(raw):
    position, total = _number(raw.get("position")), _number(raw.get("total"))
    if position and total and 0 < position <= total:
        return position / total * 100
    percent = _number(raw.get("percent"))
    return percent if percent and 0 < percent <= 100 else None


def _rank_self_conflict(raw):
    """同一条排名里，名次/总人数算出的百分比和写明的百分比相差超过 5 个百分点。"""
    position, total, percent = _number(raw.get("position")), _number(raw.get("total")), _number(raw.get("percent"))
    if position and total and percent and 0 < position <= total:
        return abs(position / total * 100 - percent) > 5
    return False


def _ranks_conflict(ranks):
    """同一段学历的排名互相矛盾（相差超过 5 个百分点），或单条排名自相矛盾。"""
    if any(r["self_conflict"] for r in ranks):
        return True
    pcts = [r["pct"] for r in ranks]
    return len(pcts) > 1 and max(pcts) - min(pcts) > 5


def _has_doubt(entries):
    """学历路径是否存在需要核实学习形式的疑点。"""
    if any(e["upgrade"] or e["degree"] == "专科" or _overran(e) for e in entries):
        return True
    if any(e["match"] and e["match"].adult_edu for e in entries):
        return True
    return any(_gap_months(prev, cur) > GAP_MONTHS for prev, cur in zip(entries, entries[1:]))


def _overran(e):
    limit = MAX_MONTHS.get(e["degree"])
    return bool(limit and e["start"] and e["end"]
                and _months_between(e["start"], e["end"]) > limit + TOLERANCE_MONTHS)


def _gap_months(prev, cur):
    if not (prev["end"] and cur["start"]):
        return 0
    # 起点是毕业时间、终点是入学时间，默认月份和入学/毕业相反
    return _months_between(prev["end"], cur["start"], start_default=6, end_default=9)


# ---------- 显示 ----------

def _degree_name(e):
    return e["degree"] + (f"（{e['degree_source'][0]}）" if e["degree_source"] else "")


def _main_rated(rated):
    """学业表现以本科（专科生看专科）的成绩为主：研究生阶段的成绩 HR 不怎么关注（用户 2026-10-01）；
    本科没写成绩时才用最近一段有成绩的学历。"""
    undergrad = [e for e in rated if e["degree"] in ("本科", "专科")]
    return undergrad[-1] if undergrad else rated[-1]


def _profile(entries):
    """给筛选用的教育画像。学历是推断的（AI 判断 / 默认），certain 为 False：碰到门槛只能进"待确认"。"""
    highest, first = _highest(entries), entries[0]
    rated = [e for e in entries if e["performance"]]

    def stage(e):
        return {"degree": e["degree"], "certain": not e["degree_source"], "school": _school_name(e),
                "tier": e["tier"], "tier_level": e["tier_level"], "major": e["major"],
                "major_class": e["category"][1] if e["category"] else "", "form": e["form"]}

    return {"highest": stage(highest), "first": stage(first), "stages": [stage(e) for e in entries],
            "performance": _main_rated(rated)["performance"][0] if rated else "",
            # 成绩依据（如"GPA 3.92/4.0"），给 HR 看是凭什么定的档
            "performance_basis": f"{_degree_name(_main_rated(rated))}：{_main_rated(rated)['performance'][1]}" if rated else "",
            # 各阶段的成绩（总览排序优先看本科：研究生阶段的成绩 HR 不怎么关注）
            "grades": [{"degree": e["degree"], "stage": _degree_name(e), "tier": e["performance"][0],
                        "basis": e["performance"][1]} for e in rated]}


def _resolve_degree(raw, school, text):
    """返回 (学历, 来源)。简历写明时来源为 None；没写时依次用 AI 判断、学校类型默认，来源为（标注, 依据）。
    专科院校不能授予本科及以上学历，所以学校是专科院校时一律按专科处理。"""
    degree = _degree_alias(raw.get("degree"))
    if degree and DEGREE_EVIDENCE[degree].search(text):
        return degree, None
    # 走到这里说明原文没写学历（AI 填了但原文找不到对应字样的，也视为没写）
    if degree and not _degree_alias(raw.get("degree_guess")):
        raw = {**raw, "degree_guess": degree}
    if VOCATIONAL_COLLEGE.search(school):
        return "专科", ("按院校类型默认", "学校为专科院校")
    guess = _degree_alias(raw.get("degree_guess"))
    if guess:
        reason = _text(raw.get("degree_guess_reason")) or "AI 根据上下文判断"
        return guess, ("AI 判断", reason.rstrip("。"))
    return "本科", ("按院校类型默认", "学校为本科院校")


def _degree_alias(value):
    """把"学士""研究生"等说法统一成 DEGREES 中的学历；无法识别时返回空字符串。"""
    degree = _text(value)
    degree = DEGREE_ALIASES.get(degree, degree)
    return degree if degree in DEGREES else ""


def _highest(entries):
    """最高学历；同级时取最近一段。"""
    return max(reversed(entries), key=lambda e: DEGREES[::-1].index(e["degree"]) if e["degree"] else -1)


def _school_name(e):
    """知识库识别出的学校用标准名称（如 University of Warwick → 华威大学），否则用简历原文。"""
    match = e["match"]
    if match and match.record:
        return match.name
    return e["school"] or "学校未注明"


def _school_desc(e):
    label = f"，{e['tier_label']}" if e["tier_label"] else ""
    return f"{e['tier']}（{_school_name(e)}{label}）"


def _major_desc(e):
    if not e["major"]:
        return "专业未注明"
    if not e["category"]:
        return f"{e['major']}（未能归类）"
    mark = "（AI 归类）" if e["category_source"] == "AI 归类" else ""
    return f"{e['major']}（{' → '.join(e['category'])}）{mark}"


def _period(e):
    start = _fmt_date(e["start"]) if e["start"] else "?"
    end = "至今" if e["ongoing"] and not e["end"] else _fmt_date(e["end"]) if e["end"] else "?"
    return f"{start}–{end}"


def _record_desc(r):
    if r["type"] == "排名":
        return r["text"] + _sample_mark(r) + _guess_mark(r)
    return _grade_by_score(r)[1] + _guess_mark(r)


def _sample_mark(record):
    total = record.get("total")
    if total and total < MIN_RANK_TOTAL:
        return f"（仅 {total:g} 人，未用于分档）"
    if total and total < SMALL_RANK_TOTAL:
        return f"（仅 {total:g} 人，样本较小）"
    return ""


def _guess_mark(record):
    return "，归属为推断" if record and record.get("guessed") else ""


# ---------- 工具函数 ----------

def _number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().rstrip("%"))
        except ValueError:
            return None
    return None


def _parse_date(value):
    """把 "2023-09" / "2023" 转成 (年, 月)，月份缺失时为 None。"""
    m = DATE_RE.match(_text(value))
    if not m:
        return None
    month = int(m.group(2)) if m.group(2) else None
    return int(m.group(1)), month if month and 1 <= month <= 12 else None


def _months_between(start, end, start_default=9, end_default=6):
    """两个日期相差的月数。缺月份时按常见的 9 月入学、6 月毕业估算。"""
    return (end[0] - start[0]) * 12 + ((end[1] or end_default) - (start[1] or start_default))


def _fmt_date(date):
    return f"{date[0]}.{date[1]:02d}" if date[1] else str(date[0])

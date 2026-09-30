"""校园经历模块：只描述和核查，不评价素质（素质统一在素质画像里跨所有经历评价）。

- 任职概况：学生组织 / 校内岗位 / 兴趣社群各几项，其中负责人几项
- 每段经历：组织级别（从名称判断，看不出来标"未注明"）、任期、校内岗位按 L1～L3 看做事深度
- 核查：时间超出所在学历阶段、含大学前阶段、只写职务没写做了什么

AI 提取和实习经历、素质画像共用一次（见 modules/experience.py）。
"""
import re

from modules.base import Module, ModuleResult
from modules.experience import DATE, LEVEL_NAMES, build_education, build_entries, extract, month, month_text, short, squash

# 教育背景行里可能出现的职务（整段完全匹配才算，避免把"学生会"这类组织名当职务）
EDU_ROLE = re.compile(r"班长|副班长|团支书|团支部书记|党支部书记|支部书记|班委|学习委员|组织委员|宣传委员|生活委员|"
                      r"学生会主席|学生会副主席|研究生会主席|研究生会副主席|部长|副部长|社长|会长")
LEADER = re.compile(r"班长|书记|团支书|主席|部长|会长|社长|队长|组长|负责人|主任")
CLASS_LEVEL = re.compile(r"班委|班长|班级|团支部|党支部|团支书|支部")
COLLEGE_LEVEL = re.compile(r"学院|[^学]院(?!校)")
SCHOOL_LEVEL = re.compile(r"大学|学校|校级|全校")
UNIVERSITY_STAGES = ("专科", "本科")


class CampusModule(Module):
    title = "校园经历"

    def analyze(self, resume, context):
        data = extract(context["llm"], resume.text)
        result = ModuleResult(self.title, extracted={})  # 原始数据已在实习经历里输出，不重复
        source = squash(resume.text)
        stages = build_education(data, source)
        entries = campus_entries(data, resume.text, stages)
        if not entries:
            result.add("任职概况", "无校园经历")
            return result

        result.add("任职概况", _overview(entries))
        timed = [e for e in entries if e["months"]]
        if timed:
            longest = max(timed, key=lambda e: e["months"])
            result.add("最长任期", f"{_role(longest)}：{_years(longest['months'])}")
        for n, e in enumerate(entries, 1):
            result.add("", "")
            result.add(f"经历 {n}" if len(entries) > 1 else "经历", label(e))
            level = org_level(e)
            kind = e["campus_type"] or "类型未注明"
            parts = [kind if level == kind else f"{kind}·{level}", _time(e)]
            if e["campus_type"] == "校内岗位":
                parts.append(f"深度 {LEVEL_NAMES[e['level']]}" if e["level"] else "未写工作内容")
            result.add("", "｜".join(parts))
            for s in e["signals"]:
                if s["type"] in ("获认可", "连任", "被选举任命", "自发组织"):
                    result.add("", f"{s['type']}：“{short(s['text'], 40)}”")
            if e["duties"]:
                result.add("", f"做了什么：“{short(e['duties'][0]['text'], 50)}”")
        self._notes(entries, stages, result)
        result.profile = {"campus": {"experiences": [
            {"name": e["org"], "kind": "校园经历", "campus_type": e["campus_type"], "title": _role(e),
             "level": org_level(e), "leader": is_leader(e), "months": e["months"],
             "duties": [{"text": d["text"], "level": d["level"]} for d in e["duties"]]} for e in entries]}}
        return result

    @staticmethod
    def _notes(entries, stages, result):
        bare = [label(e) for e in entries if not e["duties"]]
        if bare:
            result.notes.append(f"只写了职务、没写做了什么：{'、'.join(bare)}。建议面试时请候选人讲具体做过的事")
        for e in entries:
            for note in time_conflicts(e, stages):
                result.notes.append(note)


def campus_entries(data, text, stages):
    """校园经历。职务写在教育背景那一行时，后面的时间是学历阶段的时间，不是任期：标注出来，不算任期。"""
    source = squash(text)
    entries = [e for e in build_entries(data, source) if e["kind"] == "校园经历"]
    entries += _roles_in_education(text, entries)
    spans = {(s["start"], s["end"]) for s in stages}
    for e in entries:
        e["borrowed_time"] = e.get("borrowed_time") or (e["start"], e["end"]) in spans
        if e["borrowed_time"]:
            e["months"] = None
    return entries


def _roles_in_education(text, entries):
    """教育背景那一行里用"丨"隔开的职务（"XX大学丨商学院丨硕士丨党支部书记"），AI 有时会漏掉，由代码识别。"""
    found = []
    have = {e["title"] for e in entries} | {r for e in entries for r in e["roles"]}
    for line in text.splitlines():
        if not re.search(r"大学|学院", line):
            continue
        parts = [p.strip() for p in re.split(r"[|｜丨]", line)]
        for part in parts:
            role = re.sub(r"\s*(?:19|20)\d{2}.*$", "", part)  # 去掉职务后面紧跟的时间
            if not EDU_ROLE.fullmatch(role) or role in have:
                continue
            school = next((p for p in parts if re.search(r"大学|学院", p)), "")
            dates = [m.group() for m in DATE.finditer(line)]
            start = month(dates[0]) if dates else None
            end = month(dates[1]) if len(dates) > 1 else None
            found.append({
                "org": school, "raw_org": school, "title": role, "roles": [], "stage": "", "kind": "校园经历",
                "campus_type": "学生组织", "start": start, "end": end, "months": None,
                "start_text": dates[0] if dates else "", "end_text": dates[1] if len(dates) > 1 else "",
                "ongoing": False, "single_month": False, "part_time": False, "duties": [], "level": 0,
                "signals": [], "borrowed_time": bool(dates),
            })
            have.add(role)
    return found


# ---------- 核查 ----------

def time_conflicts(entry, stages):
    """任职时间和学历阶段对不上：含大学前阶段；跨越两个学历阶段（可能是两段不同的任职被写成一段）。"""
    if entry["start"] is None or not stages or entry.get("borrowed_time"):
        return []
    notes = []
    end = entry["end"] if entry["end"] is not None else entry["start"]
    first = next((s for s in stages if s["stage"] in UNIVERSITY_STAGES), stages[0])
    name = f"{label(entry)}（{entry['start_text']}–{entry['end_text'] or '?'}）"
    if entry["start"] < first["start"] - 2:
        notes.append(f"{name}：开始时间早于大学入学（{month_text(first['start'])}），含大学前阶段，请核实")
        return notes
    home = next((s for s in stages if s["start"] - 2 <= entry["start"] <= s["end"]), None)
    if home and end > home["end"] + 3:
        after = [s for s in stages if s["start"] > home["start"]]
        where = f"之后的{after[0]['stage']}阶段（{after[0]['school']}）" if after else "毕业之后"
        notes.append(f"{name}：跨越了{home['stage']}阶段（{home['school']}）和{where}，"
                     f"请核实是否是两段不同的任职被写成了一段")
    return notes


# ---------- 显示 ----------

def org_level(e):
    """组织级别只从名称判断，看不出来标"级别未注明"，不猜。"""
    if e["campus_type"] == "兴趣社群":
        return "兴趣社群"
    text = e["org"] + e["title"] + "".join(e["roles"])
    if CLASS_LEVEL.search(text):
        return "班级 / 支部"
    if COLLEGE_LEVEL.search(e["org"]):
        return "院级"
    if SCHOOL_LEVEL.search(e["org"]):
        return "校级"
    return "级别未注明"


def label(e):
    """"组织｜职务"；组织名和职务相同（如只写了"班长"）时只显示一次。"""
    role = _role(e)
    return role if e["org"] == role or e["org"] == e["title"] else f"{e['org']}｜{role}"


def is_leader(e):
    return bool(LEADER.search(e["title"] + "".join(e["roles"])))


def _overview(entries):
    parts = []
    for kind in ("学生组织", "校内岗位", "兴趣社群"):
        group = [e for e in entries if e["campus_type"] == kind]
        if group:
            leaders = sum(is_leader(e) for e in group)
            parts.append(f"{kind} {len(group)} 项" + (f"（负责人 {leaders} 项）" if leaders else ""))
    return "｜".join(parts) or f"{len(entries)} 项（类型未注明）"


def _role(e):
    if e["roles"]:
        return "历任" + "、".join(e["roles"])
    return e["title"] or "职务未写"


def _time(e):
    if e["start"] is None:
        return "时间未写明"
    if e.get("borrowed_time"):
        return f"{e['start_text']}–{e['end_text']}（为所在学历阶段的时间，任期未写明）"
    if e["months"] is None or e["single_month"]:
        return e["start_text"]
    end = "至今" if e["ongoing"] else e["end_text"]
    return f"{e['start_text']}–{end}（{_years(e['months'])}）"


def _years(months):
    return f"约 {months // 12} 年" + (f" {months % 12} 个月" if months % 12 else "") if months >= 12 else f"{months} 个月"

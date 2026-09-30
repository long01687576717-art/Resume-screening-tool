"""素质画像：跨实习、社会实践、校园经历、项目，判断简历中素质的证据强度。

素质属于人，不属于某个板块；素质是稳定的，会在不同情境里反复出现，所以跨情境一致是判断的核心。
- 责任心、主动性、影响力、学习成长：AI 只识别信号类型（事实），代码按 knowledge/quality_signals.json 对应到素质
  证据强度：强 = 有别人的决定（被选举、放权、获认可等）且在 2 个以上情境出现；
            中 = 有别人的决定，或 2 个以上情境都有具体行为；弱 = 只在 1 个情境有具体行为；未体现
- 自我认知：自述的技能在经历中有没有佐证
- 严谨细致：复用各模块已有的检查（时间矛盾、模板残留）和正向信号（写明测试验证方法）

输出的是"证据强度"，不是"素质高低"；证据弱或未体现的给出行为面试问题。
本模块不单独调用 AI：复用经历提取和项目与技能的提取（AI 客户端会合并同样的调用）。
"""
import json
import re

from knowledge.kb import DATA_DIR
from modules import project_skill
from modules.base import Module, ModuleResult
from modules.campus import campus_entries, is_leader, label, time_conflicts
from modules.experience import (ASSIST, OWN, build_education, build_entries, build_self_claims, extract, job_direction,
                                short, squash)
from modules.internship import GROWTH

QUALITIES = ("责任心", "主动性", "影响力", "学习成长")
STRENGTH_ORDER = ("强", "中", "弱")
ASSIGNED_SOURCES = ("导师课题", "课程", "企业项目", "国家级立项", "省级立项", "校级立项", "毕业设计", "竞赛")
PROJECT_LEADER = re.compile(r"组长|队长|负责人")
LONG_TENURE = 24  # 同一职务任期 24 个月以上算"长期任职"
TEST_METHOD = re.compile(r"测试|验证|实测|对照|对比")
# 由代码识别的信号（AI 有时会漏标）：组织了具体的活动；原文写明自己发起
ORGANIZE = re.compile(r"(组织|策划|举办|筹办)[^，。；,;]{0,12}(活动|比赛|晚会|讲座|会议|赛事|培训|招新)")
SELF_START = re.compile(r"自发|发起|创办|创立|牵头成立|自主组织")
ACTION_VERBS = {
    "组织活动": re.compile(r"组织|策划|举办|筹办|发起"),
    "推动他人": re.compile(r"推动|协调|统筹|带领|说服|协同|动员|召集"),
    "自发组织": SELF_START,
}
# 任职（班委、干部、职务）长期担任 = 被反复选择；普通成员长期参加只说明能坚持
ROLE_TITLE = re.compile(r"班长|书记|支书|委员|主席|部长|会长|社长|队长|组长|负责人|干部|班委|助理|助教")
MAX_EVIDENCE = 3


def _load_signals():
    with open(DATA_DIR / "quality_signals.json", encoding="utf-8") as f:
        data = json.load(f)
    return data["signals"], data["qualities"]


SIGNALS, QUESTIONS = _load_signals()


class QualityModule(Module):
    title = "素质画像"

    def analyze(self, resume, context):
        kb, llm = context["kb"], context["llm"]
        source = squash(resume.text)
        exp_data = extract(llm, resume.text)
        proj_data = project_skill.extract(llm, kb, resume.text)
        stages = build_education(exp_data, source)
        entries = [e for e in build_entries(exp_data, source) if e["kind"] in ("实习", "全职工作", "社会实践", "待核实")]
        entries += campus_entries(exp_data, resume.text, stages)
        collected = project_skill.collect(proj_data, kb, resume.text)
        result = ModuleResult(self.title, extracted={})

        evidence = _experience_evidence(entries) + _project_evidence(collected["projects"])
        evidence += _leader_evidence(entries, collected["projects"])
        evidence += _cross_domain_evidence(collected, stages, kb)

        weak, strengths = [], {}
        for quality in QUALITIES:
            items = [e for e in evidence if e["quality"] == quality]
            strength, summary = _strength(items)
            strengths[quality] = strength
            result.add(quality, f"{strength}｜{summary}" if items else "简历中未体现")
            for e in _top(items):
                result.add("", f"· {e['context']}：“{short(e['text'], 36)}”（{e['signal']}）")
            if strength in ("弱", "未体现"):
                weak.append(quality)

        claims = build_self_claims(exp_data, source)
        strong = project_skill.strong_claims(resume.text, kb)
        summary, details = _self_awareness(strong, collected["usages"], claims)
        result.add("自我认知", summary)
        for line in details:
            result.add("", line)
        careful = _carefulness(resume.text, entries, stages, collected["projects"])
        result.add("严谨细致", careful)
        if weak:
            result.add("", "")
            for i, q in enumerate(weak):
                result.add("面试建议" if i == 0 else "", f"{q}：{QUESTIONS[q]['question']}")
        result.notes.append("以上是简历中的证据强度，不代表素质高低；沟通协作、抗压等无法从简历判断，需面试了解")
        # 素质只用于排序，不用于淘汰（输出的是证据强度，按它淘汰会刷掉不会写简历的人）
        result.profile = {"qualities": strengths, "self_awareness": summary, "careful": careful,
                          "job_direction": job_direction(exp_data)}
        return result


# ---------- 收集证据 ----------

def _item(quality, context, text, signal, others):
    return {"quality": quality, "context": context, "text": text, "signal": signal, "others": others}


def _context(e):
    """情境 = 一段经历。同一段经历的所有证据用同一个名字，才能正确统计"几个情境"。"""
    return label(e) if e["kind"] == "校园经历" else e["org"]


def _experience_evidence(entries):
    """实习、社会实践、校园经历里 AI 识别出的信号，加上代码识别的职责变多、组织活动、自发发起、长期任职。"""
    items = []
    for e in entries:
        context = _context(e)
        recognized = any(s["type"] == "获认可" for s in e["signals"])
        signals = list(e["signals"]) + _code_signals(e)
        for s in signals:
            body = squash(s["text"])
            # "协助策划"是协助别人，不算本人推动或组织
            if s["type"] in ("推动他人", "组织活动", "自发组织") and ASSIST.search(body) and not OWN.search(body):
                continue
            # 行为信号的原句里必须有对应的动词（"晚会主持人"不是组织活动）
            if s["type"] in ACTION_VERBS and not ACTION_VERBS[s["type"]].search(body):
                continue
            rule = SIGNALS[s["type"]]
            for quality in rule["qualities"]:
                # 同一段经历获得了认可，这段经历里的行为证据升级为"别人的决定"
                items.append(_item(quality, context, s["text"], s["type"], rule["others"] or recognized))
        if e["months"] and e["months"] >= LONG_TENURE and e["kind"] in ("校园经历", "实习"):
            role = e["title"] or "、".join(e["roles"]) or "任职"
            # 担任职务多年 = 被反复选择（别人的决定）；普通成员多年只说明能坚持
            elected = bool(ROLE_TITLE.search(role))
            items.append(_item("责任心", context, f"{role}，{e['start_text']}–{e['end_text']}，约 {e['months'] // 12} 年",
                               "长期任职" if elected else "长期坚持", elected))
    return items


def _code_signals(e):
    """AI 有时会漏标信号，这几类由代码在原句里再找一遍（已经标过的不重复）。"""
    found = []
    marked = [squash(s["text"]) for s in e["signals"]]
    for d in e["duties"]:
        for clause in re.split(r"[，。；,;]", d["text"]):
            body = squash(clause)
            if not body or any(body[:8] in m or m[:8] in body for m in marked):
                continue
            for kind, pattern in (("职责变多", GROWTH), ("自发组织", SELF_START), ("组织活动", ORGANIZE)):
                if pattern.search(body):
                    found.append({"type": kind, "text": clause.strip()})
                    marked.append(body)
                    break
    return found


def _project_evidence(projects):
    """个人独立完成、来源不是导师 / 课程 / 企业等安排的项目：没人要求、代价高的自由选择。"""
    items = []
    for p in projects:
        if p["team"] or not p["duties"] or p["source"] in ASSIGNED_SOURCES or not re.search(r"独立|个人", p["role"]):
            continue
        items.append(_item("主动性", p["name"], p["duties"][0]["text"], "自主项目", False))
    return items


def _leader_evidence(entries, projects):
    """在 2 个以上不同组织担任负责人：不同的集体都选择了他。"""
    leaders = [(_context(e), e["title"] or "、".join(e["roles"])) for e in entries if e["kind"] == "校园经历" and is_leader(e)]
    leaders += [(p["name"], p["role"]) for p in projects if p["team"] and PROJECT_LEADER.search(p["role"])]
    if len({context for context, _ in leaders}) < 2:
        return []
    return [_item("责任心", context, role, "多个集体的负责人", True) for context, role in leaders]


def _cross_domain_evidence(collected, stages, kb):
    """在专业和课程都没覆盖的领域做到"能构建"：自学并做成了东西。
    一门课可能涉及几个领域（"物流成本管理"= 采购供应链 + 成本分析），全部算作已覆盖。"""
    covered = set()
    for name in [c["name"] for c in collected["courses"]] + [s["major"] for s in stages]:
        covered |= kb.match_skill_domains_all(name)
    covered |= {c["domain"] for c in collected["courses"]}
    items = []
    for domain, d in collected["depth"].items():
        if d["level"] < 3 or domain in covered or domain == "其他":
            continue
        e = next(x for x in d["evidence"] if x["level"] == 3)
        items.append(_item("学习成长", e["where"], e["text"], f"跨专业做成：{domain}", False))
    return items


# ---------- 定档 ----------

def _strength(items):
    if not items:
        return "未体现", ""
    contexts = {e["context"] for e in items}
    others = any(e["others"] for e in items)
    if others and len(contexts) >= 2:
        strength = "强"
    elif others or len(contexts) >= 2:
        strength = "中"
    else:
        strength = "弱"
    parts = [f"{len(contexts)} 个情境" + ("一致" if len(contexts) >= 2 else "")]
    if others:
        parts.append("有别人的决定（" + "、".join(dict.fromkeys(e["signal"] for e in items if e["others"])) + "）")
    return strength, "；".join(parts)


def _top(items):
    """每个情境取一条，别人的决定优先。"""
    shown, seen = [], set()
    for e in sorted(items, key=lambda x: not x["others"]):
        if e["context"] not in seen:
            seen.add(e["context"])
            shown.append(e)
    return shown[:MAX_EVIDENCE]


def _self_awareness(strong, usages, claims):
    """两部分，返回 (结论, 明细行)：
    - 技能：只看原文自述"熟练 / 精通 / 擅长"的英文工具名（由代码从原文找，结果稳定）。
      其他技能名称五花八门（"大模型 API 调用"在经历里写成"由大模型通读简历"），按名称比对不可靠，不计入
    - 自我评价：具体说法（经验、成果、能力）逐条对照经历；"责任心强"这类形容词不核对
    任一部分超过一半找不到佐证 → 自评偏高（正好一半不算：一半有实际证据，说明自评大体靠谱）。"""
    details, high, checked = [], False, False
    if strong:
        checked = True
        unproven = [n for n in strong if not project_skill.evidenced(n, usages)]
        high = len(unproven) * 2 > len(strong)
        line = f"技能：自述熟练 / 精通的 {len(strong)} 项中 {len(strong) - len(unproven)} 项有经历佐证"
        details.append(line + (f"，{'、'.join(unproven)} 未见使用" if unproven else ""))
    if claims:
        checked = True
        unproven = [c for c in claims if not c["evidence"]]
        high = high or (len(claims) >= 2 and len(unproven) * 2 > len(claims))
        details.append(f"自我评价：具体说法 {len(claims)} 条中 {len(claims) - len(unproven)} 条有经历佐证")
        for c in unproven:
            details.append(f"　找不到佐证：“{short(c['text'], 40)}”，建议请候选人举例")
    if not checked:
        return "无法判断（没有自述熟练 / 精通的技能，也没有具体的自我评价）", []
    return ("自评偏高" if high else "相符"), details


def _carefulness(text, entries, stages, projects):
    negative = []
    residue = project_skill.TEMPLATE.search(text)
    if residue:
        negative.append(f"残留模板文字“{residue.group()}”")
    for e in entries:
        if e["kind"] == "校园经历" and time_conflicts(e, stages):
            negative.append(f"{e['org']}任职时间与学历阶段对不上")
    positive = [f"{p['name']}写明了测试验证方法" for p in projects
                if any(TEST_METHOD.search(d["text"]) and re.search(r"\d", d["text"]) for d in p["duties"])]
    parts = []
    if negative:
        parts.append("反向信号：" + "；".join(negative))
    if positive:
        parts.append("正向信号：" + "；".join(positive))
    return "｜".join(parts) or "未发现明显问题"

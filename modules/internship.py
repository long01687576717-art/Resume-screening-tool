"""实习经历模块：把实习抽象成三个维度。

- 实习平台：按头部机构名单、500 强、A 股上市名单定档，不让 AI 判断公司知名度
- 实习经历量：段数 + 总时长；时间重叠不重复计算，重叠超过一半的两段算 1 段
- 工作深度：按"承担了多少判断"分 L1 辅助 / L2 独立 / L3 改进，说得越大需要的证据越多，
  证据由代码在原句里核对；职位名称不参与定档

先分类：实习（岗位性质，企业和机关都算）/ 社会实践（挂职、志愿者等项目）/ 课程实习，
社会实践和课程实习单独列出，不计入经历量。AI 提取和校园经历、素质画像共用一次（见 modules/experience.py）。
"""
import re

from modules.base import Module, ModuleResult
from modules.experience import DEPTH_NAMES, LEVEL_NAMES, WORK_KINDS, build_entries, extract, month_text, short, squash

PLATFORM_ORDER = ("头部平台", "知名平台", "一般平台")
PARTICIPATE = re.compile(r"参与")
VAGUE_CHANGE = re.compile(r"(提升|提高|降低|减少|缩短|节省|优化|改善|增强|加快)[^，。；,;]{2,15}")
# 写了本人做了什么的，不再追问"本人负责哪一部分"
OWN_ACTION = re.compile(r"独立|负责|主导|牵头|完成")
# 职责变多：实习期间被放权或被留用，是带教人做的决定，比"独立负责"的自述更可信，作为亮点展示
GROWTH = re.compile(r"(中期|后期|后来|随后|逐步|之后)[^，。；,;]{0,4}(起|开始)?[^，。；,;]{0,6}(独立|负责|主导|牵头)|留用|转正|延长实习|续签实习")
# 层级较高的职位名称：工作内容全是辅助性事务时提示名称与内容不符
HIGH_TITLE = re.compile(r"总经理助理|总裁助理|经理|主管|负责人|总监|组长|主任|合伙人")
MAX_QUESTIONS = 2  # 每段经历最多列出的追问数


class InternshipModule(Module):
    title = "实习经历"

    def analyze(self, resume, context):
        kb, llm = context["kb"], context["llm"]
        data = extract(llm, resume.text)
        result = ModuleResult(self.title, extracted=data)
        entries = [_add_details(e, kb) for e in build_entries(data, squash(resume.text)) if e["kind"] != "校园经历"]

        # 目前只做校招：毕业后的全职工作和实习一起分析，报告里标注"全职"
        internships = [e for e in entries if e["kind"] in WORK_KINDS]
        practices = [e for e in entries if e["kind"] == "社会实践"]
        courses = [e for e in entries if e["kind"] == "课程实习"]
        unknown = [e for e in entries if e["kind"] == "待核实"]

        if not internships:
            result.add("实习平台", "无")
            result.add("实习经历量", "无实习")
            platform = amount = "无"
        else:
            platform, amount = _platform_summary(internships), _amount_summary(internships, result)
            result.add("实习平台", platform)
            result.add("实习经历量", amount)
        # 社会实践的深度照样计入：做事的深度和在哪里做无关；课程实习是课程安排，不计入
        rated = internships + practices
        best = max((e["level"] for e in rated), default=0)
        result.add("工作深度", f"{DEPTH_NAMES[best]}（{LEVEL_NAMES[best]}）" if best else "无")

        for label, group in (("实习", internships), ("社会实践", practices), ("课程实习", courses), ("性质待核实", unknown)):
            for n, e in enumerate(group, 1):
                result.add("", "")
                name = "全职工作" if e["kind"] == "全职工作" else label
                result.add(f"{name} {n}" if len(group) > 1 else name, _entry_head(e))
                for line in _entry_lines(e, show_platform=e["kind"] in WORK_KINDS):
                    result.add("", line)

        questions = [q for e in rated for q in e["questions"]]
        if questions:
            result.add("", "")
            result.add("面试追问", questions[0])
            for q in questions[1:]:
                result.add("", q)
        self._notes(entries, internships, result)
        result.profile = {"practice": {
            "platform": platform.split("（")[0], "amount": amount.split("（")[0],
            "amount_text": amount,   # 如"一般（1 段，共 2 个月，含兼职 1 段）"：时间重叠的已合并
            "depth": DEPTH_NAMES[best] if best else "无",
            # 每段经历：给匹配用来判断技能证据出自哪种情境（实习 / 全职 / 社会实践 / 课程实习）
            "experiences": [{"name": e["org"], "kind": e["kind"], "title": e["title"], "months": e["months"],
                             "level": e["level"], "platform": e["company"].tier if e.get("company") else "",
                             "duties": [{"text": d["text"], "level": d["level"]} for d in e["duties"]]}
                            for e in entries],
        }}
        return result

    @staticmethod
    def _notes(entries, internships, result):
        for e in internships:
            if e["title"] and HIGH_TITLE.search(e["title"]) and e["level"] == 1:
                result.notes.append(f"{e['org']}：职位名称为“{e['title']}”，但工作内容以辅助性事务为主，建议面试核实实际职责")
            if e["company"] and e["company"].parent:
                result.notes.append(f"{e['org']}：未在名单中找到，名称和“{e['company'].parent}”相近，疑似其子公司，"
                                    f"平台暂按一般平台计，建议核实")
            if e["months"] is None:
                result.notes.append(f"{e['org']}：未写明起止时间，未计入总时长")
            elif e["single_month"]:
                result.notes.append(f"{e['org']}：只写了一个月份（{e['start_text']}），时长可能不足 1 个月")
        full_time = [e for e in internships if e["kind"] == "全职工作"]
        if full_time:
            result.notes.append(f"有毕业后的全职工作经历 {len(full_time)} 段，已和实习一并统计；候选人可能是往届生，请核实应聘身份")
        if any(e["kind"] == "社会实践" for e in entries):
            result.notes.append("社会实践单独列出，不计入实习经历量；工作深度照样计入")
        if any(e["kind"] == "待核实" for e in entries):
            result.notes.append("有经历无法判断是实习还是社会实践，未计入实习经历量，建议核实")


# ---------- 整理 ----------

def _add_details(entry, kb):
    """在共用的整理结果上补充实习模块自己的内容：公司定档、追问、职责变多。"""
    entry["company"] = kb.match_company(entry["raw_org"]) if entry["raw_org"] else None
    entry["questions"] = _questions(entry["duties"])
    # 显示匹配所在的整个分句，如"中期起独立运营公司公众号"
    entry["growth"] = [c for d in entry["duties"] for c in re.split(r"[，。；,;]", d["text"]) if GROWTH.search(squash(c))]
    return entry


def _questions(duties):
    """面试追问：结果没给数字、"参与"但没写本人负责哪部分。"""
    questions = []
    for d in duties:
        body = squash(d["text"])
        for m in VAGUE_CHANGE.finditer(body):
            if not re.search(r"\d", m.group()):
                questions.append(f"“{m.group()}”，具体幅度是多少？")
                break
        if d["level"] >= 2 and PARTICIPATE.search(body) and not OWN_ACTION.search(body):
            questions.append(f"“{short(d['text'])}”，本人具体负责哪一部分？")
    return questions[:MAX_QUESTIONS]


# ---------- 定档 ----------

def _platform_summary(internships):
    companies = [e for e in internships if e["company"] and e["company"].tier in PLATFORM_ORDER]
    if not companies:
        return "机关 / 事业单位" if any(e["company"] for e in internships) else "未写单位"
    best = min(companies, key=lambda e: PLATFORM_ORDER.index(e["company"].tier))
    basis = f"：{best['company'].basis}" if best["company"].listed else ""
    return f"{best['company'].tier}（{best['org']}{basis}）"


def _amount_summary(internships, result):
    """段数 + 总时长。重叠部分不重复计算；重叠超过较短一段一半的两段算 1 段。"""
    timed = sorted((e for e in internships if e["months"] is not None), key=lambda e: e["start"])
    total = _union_months([(e["start"], e["end"]) for e in timed])
    segments = len(internships) - _merged_pairs(timed, result)
    if segments >= 2 and total >= 6:
        tier = "优秀"
    elif total >= 3:
        tier = "良好"
    else:
        tier = "一般"
    extras = []
    full_time = sum(e["kind"] == "全职工作" for e in internships)
    if full_time:
        extras.append(f"含全职 {full_time} 段")
    part_time = sum(e["part_time"] for e in internships)
    if part_time:
        extras.append(f"含兼职 {part_time} 段")
    if any(e["ongoing"] for e in internships):
        extras.append("含进行中")
    extra = "，" + "，".join(extras) if extras else ""
    return f"{tier}（{segments} 段，共 {total} 个月{extra}）"


def _union_months(spans):
    total, cur_start, cur_end = 0, None, None
    for start, end in sorted(spans):
        if cur_end is None or start > cur_end:
            total += (cur_end - cur_start) if cur_end is not None else 0
            cur_start, cur_end = start, end
        else:
            cur_end = max(cur_end, end)
    return total + ((cur_end - cur_start) if cur_end is not None else 0)


def _merged_pairs(timed, result):
    """重叠超过较短一段一半的，合并为 1 段，并提示 HR 追问两段的关系。"""
    merged = 0
    for i, a in enumerate(timed):
        for b in timed[i + 1:]:
            overlap = min(a["end"], b["end"]) - max(a["start"], b["start"])
            shorter = min(a["months"], b["months"])
            if overlap > 0 and overlap * 2 > shorter:
                merged += 1
                span = f"{month_text(max(a['start'], b['start']))}–{month_text(min(a['end'], b['end']))}"
                result.notes.append(f"{a['org']} 与 {b['org']} 时间重叠（{span}），经历量按 1 段计算；"
                                    f"建议追问两段实习的关系和时间分配")
                break
    return merged


# ---------- 显示 ----------

def _entry_head(e):
    head = e["org"] + (f"｜{e['title']}" if e["title"] else "")
    return head + ("（兼职）" if e["part_time"] and "兼职" not in head else "")


def _entry_lines(e, show_platform):
    parts = [time_text(e)]
    if show_platform and e["company"]:
        company = e["company"]
        parts.append(f"{company.tier}（{company.basis}）" if company.basis else company.tier)
    parts.append(f"深度 {LEVEL_NAMES[e['level']]}" if e["level"] else "未写工作内容")
    lines = ["｜".join(parts)]
    if e["growth"]:
        lines.append(f"亮点：职责变多（{'；'.join(f'“{g}”' for g in e['growth'])}）")
    top = [d for d in e["duties"] if d["level"] == e["level"]]
    for d in top[:2]:
        # 有结果的把结果单独列出，避免被截断
        tail = f" → 结果：{d['result']}" if d["result"] else ""
        lines.append(f"依据：“{short(d['text'], 40 if tail else 60)}”{tail}")
    return lines


def time_text(e):
    if e["months"] is None:
        return "时间未写明" if not e["start_text"] else f"{e['start_text']}–{e['end_text'] or '?'}"
    if e["single_month"]:
        return f"{e['start_text']}（时长不明）"
    end = "至今" if e["ongoing"] else e["end_text"]
    return f"{e['start_text']}–{end}（{e['months']} 个月{'，进行中' if e['ongoing'] else ''}）"

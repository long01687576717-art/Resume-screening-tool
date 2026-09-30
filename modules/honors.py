"""证书荣誉模块：把奖学金、荣誉称号、竞赛、证书抽象成三个维度。

- 校内荣誉：奖学金 + 荣誉称号，按级别 × 等级定档，标注次数
- 竞赛成绩：按《全国普通高校大学生竞赛分析报告》竞赛目录判断权威性，按级别 × 奖项定档；
  目录外竞赛最高只到"一般"，入围、优秀奖等不算获奖
- 证书资质：只分类和描述，不定档（证书的价值取决于岗位，留给以后和 JD 匹配）

AI 只调用一次，负责把各条内容拆开、分类，所有定档都由下面的规则 + 知识库完成。
"""
import re

from modules.base import Module, ModuleResult, clean_text as _text

SYSTEM_PROMPT = """你是简历信息提取助手。从用户给出的简历文本中提取所有奖学金、荣誉称号、竞赛获奖、证书和科研成果，只输出 JSON。

规则：
1. 只提取简历中明确写出的信息，不要推测、不要编造；没写的字段填 null。内容可能分散在荣誉奖项、教育背景、技能证书、自我评价等任何位置，都要提取。
2. category 只能是：奖学金、荣誉称号、竞赛、证书、科研成果。
   - 奖学金：各类奖学金、奖金（如"创新创业奖金"）。
   - 荣誉称号：优秀毕业生、三好学生、优秀学生干部、优秀党员、优秀班级等。
   - 竞赛：各类比赛、大赛、挑战赛、竞赛的获奖或参赛经历。
   - 证书：语言证书与考试成绩、职业资格证书、计算机证书、驾照、普通话等。
   - 科研成果：论文、专利、软件著作权（写在项目经历里的也要提取）。
3. 一条原文包含多个等级或多个奖项时要拆开，每个等级一条。例如"校级奖学金 3 次（一等 2 次、二等 1 次）"拆成两条：一等 count=2、二等 count=1；"连续四年获校级奖学金"为一条，count=4。
4. name：奖学金、荣誉称号填名称；竞赛只填竞赛名称，不含年份、届数、级别和奖项；证书填证书名称（如"CET-6""日语 JLPT N2""C1 驾照"）。
5. level 只能是：国际级、国家级、省级、市级、校级、院级、null。竞赛的"全国总决赛""国赛"算国家级，"省赛""赛区"算省级；"区级"算市级。原文没写级别填 null。
6. grade：奖项或等级的原文，如"一等奖""二等""金奖""三等奖""特等奖""入围""优秀奖""决赛资格"。没有填 null。
7. count：获得次数，默认 1。"连续四年获得"按 count=4 处理。
8. stage：原文写明或所在位置表明属于哪段学历时填"本科""硕士""博士"（如"研二""研究生期间"算硕士），否则填 null。
9. scarcity：原文写了名额或比例等稀缺程度时，原样填写（如"学院仅3个名额""前1%"），否则填 null。
10. cert_type：仅证书填写，只能是：语言、职业资格、计算机、其他。score：证书的分数或等级原文（如"439分""口语B级"），没有填 null。
11. research_type：仅科研成果填写，只能是：论文、专利、软著。name 填论文题目、专利名称或软件名称；level 填 null。
12. text：这一条的原文，要包含期刊或会议名称、收录级别（如 SSCI、北大核心）、作者排序、发表或授权状态等原文。

输出格式示例：
{
  "items": [
    {"category": "奖学金", "name": "校级奖学金", "level": "校级", "grade": "一等", "count": 2, "stage": "本科", "scarcity": null, "cert_type": null, "score": null, "text": "校级奖学金 3 次（一等 2 次、二等 1 次）"},
    {"category": "竞赛", "name": "全国大学生数学建模竞赛", "level": "省级", "grade": "一等奖", "count": 1, "stage": null, "scarcity": null, "cert_type": null, "score": null, "text": "全国大学生数学建模竞赛省一等奖"},
    {"category": "证书", "name": "CET-6", "level": null, "grade": null, "count": 1, "stage": null, "scarcity": null, "cert_type": "语言", "score": "560分", "text": "CET-6 560分"}
  ]
}"""

CATEGORIES = ("奖学金", "荣誉称号", "竞赛", "证书", "科研成果")
RESEARCH_TYPES = ("论文", "专利", "软著")
# 科研成果：级别、作者排序、状态都由代码在原文里判断，没写的按"未写明"处理，不猜
TOP_JOURNAL = re.compile(r"SSCI|SCI|A&HCI|CSSCI", re.IGNORECASE)
FIRST_AUTHOR = re.compile(r"第一作者|一作|独立作者|通讯作者|通讯|first author|corresponding", re.IGNORECASE)
PENDING = re.compile(r"在投|投稿|审稿|返修|under review|submitted|申请中|已受理|受理|公开", re.IGNORECASE)
GRANTED = re.compile(r"授权|已授权|granted", re.IGNORECASE)
LEVELS = ("国际级", "国家级", "省级", "市级", "校级", "院级")
CERT_TYPES = ("语言", "职业资格", "计算机", "其他")
TIER_ORDER = ("优秀", "良好", "一般")
STAGE_ORDER = ("专科", "本科", "硕士", "博士")

# 竞赛奖项：数字越小越高；入围、优秀奖等不算获奖
AWARD_RANKS = [(re.compile(r"特等|一等|金奖|冠军|第一名"), 1),
               (re.compile(r"二等|银奖|亚军|第二名"), 2),
               (re.compile(r"三等|铜奖|季军|第三名"), 3)]
NOT_AWARD = re.compile(r"入围|优秀奖|参与|参赛|决赛资格|提名|成功参赛|鼓励奖|纪念奖")
# 单项奖励：按项目成果、特长评定，不按学业评定，不计入学业奖学金次数（如中南大学创新创业奖与学年奖学金是两套体系）
SPECIAL_AWARD = re.compile(r"创新|创业|科技|实践|文体|文艺|体育|社会工作|志愿")
# 难度公认较高的职业资格，单独标注
HARD_CERTS = re.compile(r"注册会计师|CPA|法律职业资格|法考|司法考试|CFA|ACCA|精算师")


class HonorsModule(Module):
    title = "证书荣誉"

    def analyze(self, resume, context):
        kb, llm = context["kb"], context["llm"]
        data = llm.extract_json(SYSTEM_PROMPT, resume.text)
        result = ModuleResult(self.title, extracted=data)

        items = [item for item in (_build_item(raw, kb) for raw in data.get("items") or []) if item]
        honors = [i for i in items if i["category"] in ("奖学金", "荣誉称号")]
        competitions = [i for i in items if i["category"] == "竞赛"]
        certificates = [i for i in items if i["category"] == "证书"]

        school = self._school_honors(honors, result)
        result.add("竞赛成绩", _summary(competitions, "无"))
        result.add("证书资质", _certificates_summary(certificates))
        research = [i for i in items if i["category"] == "科研成果"]
        result.add("科研成果", _summary(research, "无"))

        for i in items:
            if i["scarcity"]:
                result.add("亮点", f"{i['display']}（{i['scarcity']}）")
        self._notes(competitions, result)
        result.profile = {
            # school：各阶段奖学金档位和荣誉称号，给总览排序用
            "honors": {"school": school,
                       "competitions": [{"name": c["display"], "tier": c["tier"]} for c in competitions],
                       "research": [{"name": r["display"], "tier": r["tier"]} for r in research]},
            # 证书是可以核验的事实，JD 常把它写成门槛（CPA、英语六级）
            "evidence": [{"kind": "证书", "name": c["name"], "domain": c["cert_type"], "level": 0, "context": "证书",
                          "where": "证书", "text": c["text"] or c["name"], "score": c["score"]} for c in certificates],
        }
        return result

    @staticmethod
    def _school_honors(honors, result):
        """校内荣誉：奖学金按学历阶段分别定档，荣誉称号单独列出。
        - 本科（含专科）：看次数和质量
        - 研究生，以及没写阶段的：只看最高等级（研究生学业奖学金覆盖面广，次数说明不了什么）"""
        if not honors:
            result.add("校内荣誉", "无")
            return []
        scholarships = [i for i in honors if i["category"] == "奖学金" and not SPECIAL_AWARD.search(i["name"])]
        specials = [i for i in honors if i["category"] == "奖学金" and SPECIAL_AWARD.search(i["name"])]
        # 按学历先后排列，阶段未注明的放最后
        stages = sorted({i["stage"] for i in scholarships},
                        key=lambda s: STAGE_ORDER.index(s) if s in STAGE_ORDER else len(STAGE_ORDER))
        tiers, details = [], []
        for stage in stages:
            group = [i for i in scholarships if i["stage"] == stage]
            tier, rule = _stage_scholarship_tier(stage, group)
            name = stage or "阶段未注明"
            tiers.append(f"{name}：{tier}")
            details.append(f"　└ {name}：{_stage_items(group)}" + (f"（{rule}）" if rule else ""))
        result.add("校内荣誉", "｜".join(tiers) if tiers else "无奖学金")
        for line in details:
            result.add("", line)
        if specials:
            result.add("", f"　└ 单项奖励（不计入学业奖学金）：{'；'.join(i['display'] for i in specials)}")
        titles = [f"{i['display']}（{i['tier']}）" for i in honors if i["category"] == "荣誉称号"]
        if titles:
            result.add("", f"　└ 荣誉称号：{'；'.join(titles)}")
        school = [{"name": f"{s or '阶段未注明'}奖学金", "tier": t.split("：")[1]} for s, t in zip(stages, tiers)]
        return school + [{"name": i["display"], "tier": i["tier"]} for i in honors if i["category"] == "荣誉称号"]

    @staticmethod
    def _notes(competitions, result):
        outside = [c["display"] for c in competitions if not c["listed"] and c["award"]]
        if outside:
            result.notes.append(f"以下竞赛不在《全国普通高校大学生竞赛分析报告》目录内，最高按「一般」定档：{'；'.join(outside)}")
        not_awarded = [c["display"] for c in competitions if not c["award"]]
        if not_awarded:
            result.notes.append(f"以下为入围、参赛或优秀奖等，不计为获奖：{'；'.join(not_awarded)}")


# ---------- 整理与定档 ----------

def _build_item(raw, kb):
    if not isinstance(raw, dict):
        return None
    category = _text(raw.get("category"))
    name = _text(raw.get("name")) or _text(raw.get("text"))
    if category not in CATEGORIES or not name:
        return None
    level = _text(raw.get("level"))
    # 奖学金、荣誉称号的级别必须能在原文找到（如"校级""院级""国家"），找不到视为没写，防止 AI 按常识补全。
    # 竞赛不核对：原文常写"国赛""全国总决赛"，需要换算成"国家级"。
    if category in ("奖学金", "荣誉称号") and level:
        source = _text(raw.get("text")) + name
        if level.rstrip("级") not in source:
            level = ""
    item = {
        "category": category,
        "name": name,
        "level": level if level in LEVELS else "",
        "grade": _text(raw.get("grade")),
        "count": _count(raw.get("count")),
        "stage": _text(raw.get("stage")),
        "scarcity": _text(raw.get("scarcity")),
        "cert_type": _text(raw.get("cert_type")) if _text(raw.get("cert_type")) in CERT_TYPES else "其他",
        "score": _text(raw.get("score")),
        "text": _text(raw.get("text")),
    }
    if category == "竞赛":
        record = kb.match_competition(name)
        item["listed"] = record is not None
        item["award"] = _award_rank(item["grade"])
        item["tier"] = _competition_tier(item)
    elif category == "奖学金":
        item["tier"] = ""  # 奖学金按阶段整体定档，见 _stage_scholarship_tier
    elif category == "荣誉称号":
        item["tier"] = _title_tier(item)
    elif category == "科研成果":
        research_type = _text(raw.get("research_type"))
        item["research_type"] = research_type if research_type in RESEARCH_TYPES else "论文"
        item["tier"], item["research_note"] = _research_tier(item)
    else:
        item["tier"] = ""
    item["display"] = _display(item)
    return item


def _research_tier(item):
    """优秀：SCI / SSCI / CSSCI 第一作者或通讯作者（已发表或已录用），或已授权的发明专利；
    良好：其他已发表的论文、已授权的专利；一般：在投、申请中、软著。返回 (档位, 说明)。"""
    text = item["text"] + item["name"]
    if item["research_type"] == "软著":
        return "一般", ""
    if PENDING.search(text):
        return "一般", "在投" if item["research_type"] == "论文" else "申请中"
    if item["research_type"] == "专利":
        if not GRANTED.search(text):
            return "一般", "授权状态未写明"
        return ("优秀", "") if "发明" in text else ("良好", "")
    if TOP_JOURNAL.search(text):
        if FIRST_AUTHOR.search(text):
            return "优秀", ""
        return "良好", "作者排序未写明或非第一作者"
    return "良好", ""


def _award_rank(grade):
    """奖项等级：1 最高；入围、优秀奖等返回 None（不算获奖）；写了获奖但没写等级的按 3 处理。"""
    if not grade or NOT_AWARD.search(grade):
        return None
    for pattern, rank in AWARD_RANKS:
        if pattern.search(grade):
            return rank
    return 3


def _competition_tier(item):
    award, level = item["award"], item["level"]
    if award is None:
        return ""  # 不算获奖，只展示
    if not item["listed"]:
        return "一般"  # 目录外竞赛最高只到"一般"
    if level in ("国际级", "国家级"):
        return "优秀" if award <= 2 else "良好"
    if level == "省级":
        return "良好" if award <= 2 else "一般"
    return "一般"


def _stage_scholarship_tier(stage, group):
    """某一阶段奖学金的档位，返回 (档位, 规则说明)。"""
    if any(_is_top_level(i) for i in group):
        return "优秀", "含国家级/省级奖学金"
    if stage in ("专科", "本科"):
        return _undergraduate_tier(group)
    return _graduate_tier(group)


def _undergraduate_tier(group):
    """本科：次数 + 质量。院级奖学金不计入次数，只在没有校级奖学金时算"一般"。"""
    school = [i for i in group if not _is_college_level(i)]
    first = sum(i["count"] for i in school if _grade_rank(i) == 1)
    total = sum(i["count"] for i in school)
    # 本科的次数和等级在明细里已经列出，不再重复说明
    if first >= 2 or (total >= 3 and first >= 1):
        return "优秀", ""
    if first >= 1 or total >= 2:
        return "良好", ""
    return "一般", "" if total else "仅院级奖学金"


def _graduate_tier(group):
    """研究生（及没写阶段的）：只看最高等级；新生奖学金一律按"一般"。"""
    ranks = [_grade_rank(i) for i in group if "新生" not in i["name"]]
    known = [r for r in ranks if r]
    best = min(known) if known else None
    if best == 1:
        return "良好", "研究生只看最高等级：一等"
    names = {2: "二等", 3: "三等"}
    return "一般", f"研究生只看最高等级：{names[best]}" if best else "研究生只看最高等级：未注明等级或仅新生奖学金"


def _is_top_level(item):
    # "国家励志奖学金"不含"国家奖学金"四个连续字，不会被误判
    return "国家奖学金" in item["name"] or item["level"] in ("国际级", "国家级", "省级")


def _is_college_level(item):
    return item["level"] == "院级" or "院级" in item["name"]


def _grade_rank(item):
    """奖学金等级：特等/一等为 1，二等为 2，三等为 3，没写等级为 None。等级可能写在 grade 里，也可能在名称里。"""
    text = item["grade"] + item["name"]
    for pattern, rank in ((r"特等|一等", 1), (r"二等", 2), (r"三等", 3)):
        if re.search(pattern, text):
            return rank
    return None


def _stage_items(group):
    """同一阶段的奖学金按名称汇总，如"校级奖学金 3 次（一等 ×2、二等）；学业新生奖学金 1 次（二等奖）"。"""
    groups = {}
    for i in group:
        groups.setdefault(_with_level(i), []).append(i)
    return "；".join(_scholarship_group(name, items) for name, items in groups.items())


def _title_tier(item):
    level = item["level"]
    if level in ("国际级", "国家级", "省级"):
        return "优秀"
    if level == "院级":
        return "一般"
    return "良好"  # 校级，或没写级别


def _best(items):
    """档位最高的一条；同档时奖学金优先于荣誉称号，次数多的优先（"连续四年"比一次更有说服力）。"""
    graded = sorted((i for i in items if i["tier"]),
                    key=lambda i: (TIER_ORDER.index(i["tier"]), CATEGORIES.index(i["category"]), -i["count"]))
    return graded[0] if graded else None


def _with_level(item):
    """名称里没体现级别时补上，如"学业奖学金"→"校级学业奖学金"；"研究生国家奖学金"已含"国家"，不重复。"""
    level = item["level"]
    if level and level.rstrip("级") not in item["name"]:
        return level + item["name"]
    return item["name"]


def _scholarship_group(name, group):
    """同一阶段、同一种奖学金汇总成一句，如"校级奖学金 3 次（一等 ×2、二等）"。"""
    total = sum(i["count"] for i in group)
    grades = [f"{i['grade'] or '等级未注明'}" + (f" ×{i['count']}" if i["count"] > 1 else "") for i in group]
    # 没写等级，或等级已经包含在名称里（如"校级三等奖学金"）时，不再重复显示
    redundant = all(not i["grade"] or i["grade"].rstrip("奖") in name for i in group)
    detail = "" if redundant else f"（{'、'.join(grades)}）"
    return f"{name} {total} 次{detail}"


def _summary(items, empty):
    """维度摘要：最高档位 + 依据，其余全部列出。"""
    if not items:
        return empty
    best = _best(items)
    if not best:
        return "未定档（" + "；".join(i["display"] for i in items) + "）"
    others = [i["display"] for i in items if i is not best]
    text = f"{best['tier']}（{best['display']}）"
    if others:
        text += "｜另有：" + "；".join(others)
    return text


def _certificates_summary(items):
    if not items:
        return "无"
    groups = []
    for cert_type in CERT_TYPES:
        names = [i["display"] for i in items if i["cert_type"] == cert_type]
        if names:
            groups.append(f"{cert_type}：{'、'.join(names)}")
    return "｜".join(groups)


def _display(item):
    """展示用的描述，如"校级奖学金（一等）×2""全国大学生数学建模竞赛 省级一等奖"。"""
    category = item["category"]
    if category == "证书":
        score = item["score"] if item["score"] and item["score"] not in item["name"] else ""
        text = item["name"] + (f"（{score}）" if score else "")
        if HARD_CERTS.search(item["name"]):
            text += "［高难度职业资格］"
        return text
    if category == "科研成果":
        # 显示原文（含期刊、收录级别、作者排序、状态），HR 可以直接核对
        text = item["text"] or item["name"]
        text = text if len(text) <= 50 else text[:50] + "……"
        note = f"，{item['research_note']}" if item["research_note"] else ""
        return f"{item['research_type']}：{text}{note}"
    if category == "竞赛":
        text = f"{item['name']} {item['level']}{item['grade']}".strip()
    else:
        grade = item["grade"] if item["grade"] and item["grade"] not in item["name"] else ""
        text = item["name"] + (f"（{grade}）" if grade else "")
    stage = f"［{item['stage']}］" if item["stage"] else ""
    count = f" ×{item['count']}" if item["count"] > 1 else ""
    return f"{text}{count}{stage}"


def _count(value):
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1

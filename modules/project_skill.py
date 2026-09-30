"""项目与技能模块：项目经历 + 专业技能合并。技能栏是纯自述，项目、实习等经历是它的证据。

- 项目可信度：有什么能证明项目是真的、做得好（背书 + 可验证性），强 / 中 / 弱，来源必须原文写明
- 项目深度：沿用实习模块的 L1～L3，只看本人做的部分；团队项目没写个人工作的最高 L2
- 技能范畴与深度：
  深度只看有经历佐证的，按"用它做出了什么"分级：能构建 / 能解决问题 / 会操作；
  广度单独列出学过或了解的内容（课程有成绩 > 课程 > 自述），不定档；
  按"可迁移能力 / 领域知识"两层归类，归类用知识库，知识库没有的由 AI 归类并标注

不推断简历没写的能力。AI 只调用一次，负责拆分和摘录原文，定级和归类由代码完成。
"""
import re

from modules.base import Module, ModuleResult, clean_text as _text
from modules.experience import ADOPTED, ASSIST, LEVEL_NAMES, OWN, QUANT_CHANGE, basis_found, found_parts
from modules.experience import short as _short, squash as _squash

SYSTEM_PROMPT = """你是简历信息提取助手。从用户给出的简历文本中提取项目经历、技能、课程和技能的使用证据，只输出 JSON。

规则：
1. 只提取简历中明确写出的信息，不要推测、不要编造；没写的字段填 null。原文摘录不要改写。
2. projects：项目经历（含毕业设计、科研项目、个人项目）。实习、校园职务、竞赛经历不算项目，除非原文把它写在项目经历里。
   - name：项目名称；role：本人角色原文（如"组长""独立开发"），没写填 null；start、end：只填这个项目写明的时间（YYYY.MM，"至今"照写）。
   - source：原文写明的项目来源，只能是：导师课题、国家级立项、省级立项、校级立项、企业项目、毕业设计、竞赛、课程、个人项目、null。原文没写来源填 null，不要根据内容猜。source_text：写明来源的原文。
   - team：原文写明"独立"或只有本人参与填"个人"，写了组长、成员、团队等填"团队"，否则 null。
   - personal_split：原文是否把本人工作单独写出来（如"个人工作：""本人负责："），true / false。
   - links：原文中的网址、演示地址、GitHub 地址，原样摘录。
   - outputs：原文写明的论文、专利、软著、上线、获奖等成果，原样摘录。
   - metrics：原文中的效果指标（如"准确率90%以上""精度>92%"），原样摘录。
   - overview：原文单独写的"项目概述 / 项目背景"那一段原文，没有填 null。
   - overview_tools：项目概述里列出的技术或工具（如"Vue3""PostGIS"）。
   - duties：本人做的每一条工作。原文分开写了个人工作的，只摘个人工作；独立完成的项目，全部都是本人工作。每条：
     text（原文）、level（1 辅助 / 2 独立完成常规工作 / 3 设计、搭建、优化并有结果；只看做了什么，不看角色名称）、basis（定级依据的原文词语）、result（原文写明的结果，没有填 null）。
3. skills：技能栏、个人优势、自我评价里声称会的技能，逐项拆开（"Python / Stata"拆成两项）。
   name：具体的技能名称，不要把分类标题（如"AI与数字化""系统工具""专业技能"）当成技能；claim：原文的熟练程度词（精通、熟练、熟悉、了解、掌握），没写填 null；text：原文。语言证书、驾照不要提取。
4. courses：主修课程、核心课程，逐门列出。name：课程名；score：原文写的成绩，没有填 null。
5. usages：技能在经历中实际使用的证据。只从项目、实习、校园经历、竞赛经历里找，不要从技能栏、个人优势、自我评价、课程里找。
   技能既包括工具（如 Python、Excel、ENVI），也包括业务能力和方法（如采购、招聘、审计、行业研究、社群运营、数据清洗、文本分析），两类都要列。
   每条：skill（技能名称，尽量和 skills 里的写法一致；经历里用到但技能栏没写的也要列）、text（体现使用这项技能的原文句子）、
   where（所在经历的名称，如公司名或项目名）、output（用这项技能做出的东西，原文摘录，如"自动化报价模版""动态经营看板"，没有填 null）。
   团队项目"项目概述"里列出的技术，如果本人工作里没有写到，不要列入 usages。
   每段实习、项目的主要工作都要有对应的 usage，业务能力不要漏（如实习里做采购，就要有一条 skill 为"采购"的 usage）。
6. domain：skills、courses、usages 的每一项都填 domain，只能从下面的领域中选一个，都不合适填"其他"：
   {domains}
7. interests：兴趣爱好、游戏经历等和工作能力无关的内容，原样摘录。

输出格式：
{{"projects": [{{"name": "...", "role": "...", "start": "2026.04", "end": "2026.08", "source": "导师课题", "source_text": "...", "team": "个人", "personal_split": false,
   "links": [], "outputs": [], "metrics": ["准确率90%以上"], "overview_tools": [],
   "duties": [{{"text": "...", "level": 3, "basis": "开发", "result": "..."}}]}}],
 "skills": [{{"name": "Python", "claim": "熟练", "text": "...", "domain": "编程开发"}}],
 "courses": [{{"name": "劳动法", "score": "96", "domain": "人力资源"}}],
 "usages": [{{"skill": "Python", "text": "...", "where": "某公司", "output": "...", "domain": "数据处理与分析"}}],
 "interests": []}}"""

CREDIBILITY_ORDER = ("强", "中", "弱")
STRONG_SOURCES = ("导师课题", "国家级立项", "省级立项", "企业项目")
MIDDLE_SOURCES = ("校级立项", "毕业设计")
LINK = re.compile(r"https?://|www\.|\.app\b|\.com\b|\.cn\b|github", re.IGNORECASE)
OUTPUT_WORDS = re.compile(r"论文|专利|软著|著作权|上线|发表|收录|获.{0,6}奖|投入使用")
METRIC = re.compile(r"\d+(\.\d+)?\s*%|准确率|精度|召回率|F1|提升至|提高至|由.{0,8}提升")
DEPTH_NAMES = {1: "浅", 2: "中", 3: "深"}

# 技能深度：用它做出了什么
SKILL_LEVELS = {3: "能构建", 2: "能解决问题", 1: "会操作"}
BUILD_VERB = re.compile(r"搭建|开发|制作|构建|建立|编写|设计|建库|实现|部署")
BUILD_OUTPUT = re.compile(r"模版|模板|系统|看板|工具|脚本|数据库|平台|Demo|模型|应用|知识库|网站|小程序|插件|助手|函数|地图")
# 写在技能栏、个人优势里的自述句子，不能当使用证据
CLAIM_WORDS = re.compile(r"熟练|熟悉|精通|掌握|具备|擅长|了解")
DOING = re.compile(r"负责|完成|参与|协助|实施|执行|推动|组织|撰写")
SOLO_ROLE = re.compile(r"独立|个人")
TEAM_ROLE = re.compile(r"组长|组员|成员|负责人|队长|团队|小组")
TEMPLATE = re.compile(r"【[^】]*(如|例如)[:：][^】]*】|XXX|请填写|在此填写", re.IGNORECASE)


class ProjectSkillModule(Module):
    title = "项目与技能"

    def analyze(self, resume, context):
        kb, llm = context["kb"], context["llm"]
        data = extract(llm, kb, resume.text)
        result = ModuleResult(self.title, extracted=data)
        c = collect(data, kb, resume.text)
        self._projects(c["projects"], result)
        self._skills(c["depth"], c["skills"], c["courses"], result)
        self._notes(resume.text, c["depth"], c["skills"], c["projects"], data, result)
        result.profile = {"projects": [{"name": p["name"], "credibility": p["credibility"], "level": p["level"],
                                        "team": p["team"], "source": p["source"],
                                        "duties": [{"text": d["text"], "level": d["level"]} for d in p["duties"]]}
                                       for p in c["projects"]],
                          "evidence": evidence(c)}
        return result

    # ---------- 项目 ----------

    @staticmethod
    def _projects(projects, result):
        if not projects:
            result.add("项目可信度", "无项目")
            result.add("项目深度", "无项目")
            return
        best = min(projects, key=lambda p: CREDIBILITY_ORDER.index(p["credibility"]))
        result.add("项目可信度", f"{best['credibility']}（{best['name']}：{best['basis']}）")
        level = max(p["level"] for p in projects)
        result.add("项目深度", f"{DEPTH_NAMES[level]}（{LEVEL_NAMES[level]}）" if level else "未写本人工作")
        for n, p in enumerate(projects, 1):
            result.add("", "")
            head = p["name"] + (f"｜{p['role']}" if p["role"] else "") + (f"｜{p['time']}" if p["time"] else "")
            result.add(f"项目 {n}" if len(projects) > 1 else "项目", head)
            parts = [f"可信度 {p['credibility']}：{p['basis']}", f"深度 {LEVEL_NAMES[p['level']]}" if p["level"] else "未写本人工作"]
            result.add("", "｜".join(parts))
            for d in [d for d in p["duties"] if d["level"] == p["level"]][:1]:
                tail = f" → 结果：{d['result']}" if d["result"] else ""
                result.add("", f"依据：“{_short(d['text'], 40 if tail else 60)}”{tail}")
            for link in p["links"]:
                result.add("", f"链接：{link}")

    # ---------- 技能 ----------

    @staticmethod
    def _skills(depth, skills, courses, result):
        best = max((e["level"] for d in depth.values() for e in d["evidence"]), default=0)
        if best:
            top = [name for name, d in depth.items() if d["level"] == best]
            result.add("技能深度", f"{SKILL_LEVELS[best]}（{'、'.join(top)}）")
        else:
            result.add("技能深度", "无经历佐证")
        for layer in ("可迁移能力", "领域知识"):
            rows = [(name, d) for name, d in depth.items() if d["layer"] == layer]
            if not rows:
                continue
            result.add("", "")
            for i, (name, d) in enumerate(sorted(rows, key=lambda r: -r[1]["level"])):
                result.add(layer if i == 0 else "", f"{name}{_ai_mark(d)}｜{_depth_text(d)}")

        # 广度：学过或了解的（课程、自述），不定档
        breadth = {}
        for c in courses:
            breadth.setdefault(c["domain"], {"ai": c["ai"], "scored": [], "courses": [], "claims": []})
            breadth[c["domain"]]["scored" if c["score"] else "courses"].append(c["display"])
        used = {e["skill"].lower() for d in depth.values() for e in d["evidence"]}
        for s in skills:
            if s["general"] or s["name"].lower() in used:
                continue
            breadth.setdefault(s["domain"], {"ai": s["ai"], "scored": [], "courses": [], "claims": []})
            breadth[s["domain"]]["claims"].append(s["name"])
        if breadth:
            result.add("", "")
            for i, (name, b) in enumerate(breadth.items()):
                parts = []
                if b["scored"] or b["courses"]:
                    parts.append("课程：" + "、".join(b["scored"] + b["courses"]))
                if b["claims"]:
                    # "ERP系统"和"ERP"是同一项，只显示一次
                    unique = {re.sub(r"系统$", "", c).lower(): c for c in reversed(b["claims"])}
                    parts.append("自述：" + "、".join(reversed(list(unique.values()))))
                consistent = "　← 与实践一致" if name in depth and depth[name]["level"] >= 2 and (b["scored"] or b["courses"]) else ""
                result.add("技能广度" if i == 0 else "", f"{name}{' (AI 归类)' if b['ai'] else ''}｜{'；'.join(parts)}{consistent}")
        general = [s["name"] for s in skills if s["general"]]
        if general:
            result.add("通用技能", "、".join(dict.fromkeys(general)))

    @staticmethod
    def _notes(text, depth, skills, projects, data, result):
        interests = [_text(i) for i in data.get("interests") or [] if _text(i)]
        if interests:
            result.add("兴趣相关", _short("；".join(interests), 40) + "（不计入技能）")
        residue = TEMPLATE.search(text)
        if residue:
            result.notes.append(f"技能栏疑似残留模板文字：“{residue.group()}”")
        used = {e["skill"].lower() for d in depth.values() for e in d["evidence"]}
        unproven = [s["name"] for s in skills if s["claim"] in ("精通", "熟练", "擅长") and s["name"].lower() not in used]
        questions = []
        if unproven:
            questions.append(f"自述{'/'.join(sorted({s['claim'] for s in skills if s['name'] in unproven and s['claim']}))}："
                             f"{'、'.join(dict.fromkeys(unproven))}，经历中未见使用，可请候选人举例")
        for p in projects:
            if p["team_capped"]:
                questions.append(f"“{p['name']}”是团队项目，没有单独写本人工作，本人具体负责哪一部分？")
            for m in p["metric_questions"]:
                questions.append(f"“{m}”是怎么测出来的？")
        if questions:
            result.add("面试追问", questions[0])
            for q in questions[1:]:
                result.add("", q)


# ---------- 整理（素质画像也会用到） ----------

def extract(llm, kb, text):
    """项目与技能的 AI 提取。素质画像同时调用时，AI 客户端只真正请求一次。"""
    return llm.extract_json(SYSTEM_PROMPT.format(domains="、".join(kb.skill_domain_names)), text)


def collect(data, kb, text):
    """整理项目、技能使用证据、自述技能、课程，并按领域汇总技能深度。"""
    source = _squash(text)
    projects = [p for p in (_build_project(raw, source) for raw in data.get("projects") or []) if p]
    # 团队项目的"项目概述"写的是整个团队的成果，里面的句子不能当本人的技能证据
    overviews = [p["overview"] for p in projects if p["team"] and p["overview"]]
    usages = [u for u in (_build_usage(raw, kb, source) for raw in data.get("usages") or [])
              if u and not any(_squash(u["text"])[:12] in o for o in overviews)]
    skills = _split_claims([s for s in (_build_skill(raw, kb) for raw in data.get("skills") or []) if s], kb)
    courses = [c for c in (_build_course(raw, kb, source) for raw in data.get("courses") or []) if c]
    # 项目里写到的工具由代码直接在原句里找，不依赖 AI 有没有列出（AI 每次列的不一样）
    usages += _project_tool_usages(projects, skills, usages, kb)
    # 团队项目概述里的技术：本人工作没写到的，只算"参与过使用该技术的团队项目"
    team_tools = [(tool, p["name"]) for p in projects if p["team"] for tool in p["overview_tools"]]
    return {"projects": projects, "usages": usages, "skills": skills, "courses": courses,
            "depth": _skill_depth(usages, team_tools, kb)}


def evidence(c):
    """把技能相关的数据统一成证据：经历中用过的（level 1～3）、团队项目里的技术（level 1，情境为团队项目）、
    课程学过的（level 0）、只在技能栏自述的（level -1）。情境（实习 / 校园……）由 main.py 按经历名称补全。"""
    projects = {p["name"] for p in c["projects"]}
    items = [{"kind": "技能", "name": u["skill"], "domain": u["domain"], "level": u["level"],
              "context": "项目" if u["where"] in projects else "", "where": u["where"], "text": u["text"]}
             for u in c["usages"]]
    for domain, d in c["depth"].items():
        for tool, project in d["team"]:
            items.append({"kind": "技能", "name": tool, "domain": domain, "level": 1, "context": "团队项目",
                          "where": project, "text": f"团队项目使用的技术（本人工作未写到）"})
    for course in c["courses"]:
        items.append({"kind": "技能", "name": course["name"], "domain": course["domain"], "level": 0,
                      "context": "课程", "where": "课程", "text": course["display"]})
    for s in c["skills"]:
        # "RAG 知识库"和经历里的"RAG"算同一项（名称互相包含）
        if not s["general"] and not evidenced(s["name"], c["usages"]):
            items.append({"kind": "技能", "name": s["name"], "domain": s["domain"], "level": -1,
                          "context": "自述", "where": "技能栏", "text": s["text"] or s["name"], "claim": s["claim"]})
    return items


def evidenced(skill_name, usages):
    """自述的技能在经历里有没有使用证据（名称互相包含即算，如"Python"和"Python 数据清洗"）。"""
    name = skill_name.lower().replace(" ", "")
    return any(name in u["skill"].lower().replace(" ", "") or u["skill"].lower().replace(" ", "") in name for u in usages)


# ---------- 项目整理 ----------

def _build_project(raw, source):
    if not isinstance(raw, dict):
        return None
    name = _text(raw.get("name"))
    if not name:
        return None
    found = lambda s: s and _squash(s) in source
    links = [l for l in (_original_link(_text(l), source) for l in raw.get("links") or []) if l]
    outputs = [_text(o) for o in raw.get("outputs") or [] if found(_text(o)) and OUTPUT_WORDS.search(_text(o))]
    metrics = [_text(m) for m in raw.get("metrics") or [] if found(_text(m)) and re.search(r"\d", _text(m))]
    src, src_text = _text(raw.get("source")), _text(raw.get("source_text"))
    # 来源必须原文写明：依据原文要找得到，且含来源的关键字
    if not (found(src_text) and _source_written(src, src_text)):
        src = ""
    # 团队还是个人由代码看角色词判断（AI 每次判断不一样），角色没写时才用 AI 的判断
    role = _text(raw.get("role"))
    if SOLO_ROLE.search(role):
        team = "个人"
    elif TEAM_ROLE.search(role):
        team = "团队"
    else:
        team = _text(raw.get("team"))
    overview = _squash(_text(raw.get("overview")))
    duties =[d for d in (_build_duty(d, source) for d in raw.get("duties") or []) if d]
    level = max((d["level"] for d in duties), default=0)
    no_split = team == "团队" and raw.get("personal_split") is not True
    if no_split:
        level = min(level, 2)  # 团队项目没写个人工作，最高 L2
    credibility, basis = _credibility(src, links, outputs, metrics)
    start, end = _text(raw.get("start")), _text(raw.get("end"))
    return {
        "name": name, "role": role, "team": team == "团队", "source": src,
        "overview": overview if overview and overview[:12] in source else "", "time": f"{start}–{end}" if start and end and start != end else start,
        "links": links, "duties": duties, "level": level, "credibility": credibility, "basis": basis,
        "team_capped": no_split and bool(duties),
        "overview_tools": [_text(t) for t in raw.get("overview_tools") or [] if found(_text(t))],
        # 只有指标、没写测试方法的，追问指标怎么测的
        "metric_questions": metrics[:1] if metrics and not re.search(r"测试|验证|实测|评估|对比", "".join(d["text"] for d in duties)) else [],
    }


URL = re.compile(r"(?:https?://)?[A-Za-z0-9][A-Za-z0-9\-.]*\.(?:app|com|cn|io|net|org)(?:/[^\s，。；]*)?", re.IGNORECASE)


def _original_link(link, source):
    """AI 抄网址可能抄错个别字母：到原文里找开头相同的网址，用原文的写法。"""
    if not link or not LINK.search(link):
        return ""
    for url in URL.findall(source):
        if url.lower()[:12] == link.lower()[:12]:
            return url
    return ""


def _source_written(src, text):
    keys = {"导师课题": "导师|课题", "国家级立项": "国家级", "省级立项": "省级", "校级立项": "校级", "企业项目": "企业|公司",
            "毕业设计": "毕业设计|毕设", "竞赛": "赛", "课程": "课程", "个人项目": "独立|个人"}
    return src in keys and re.search(keys[src], text)


def _credibility(src, links, outputs, metrics):
    if links:
        return "强", "有演示链接或代码地址"
    if outputs:
        return "强", "成果：" + "、".join(outputs)
    if src in STRONG_SOURCES:
        return "强", src
    if src in MIDDLE_SOURCES:
        return "中", src
    if metrics:
        return "中", "有指标：" + "、".join(metrics)
    return "弱", f"来源：{src}" if src else "只有描述，来源未注明"


def _build_duty(raw, source):
    """和实习模块同一套定级规则；项目的结果还可以是效果指标（准确率、精度等）。"""
    if not isinstance(raw, dict):
        return None
    text = _text(raw.get("text"))
    if not text or _squash(text)[:12] not in source:
        return None
    body = _squash(text)
    level = raw.get("level") if raw.get("level") in (1, 2, 3) else 1
    result = found_parts(_text(raw.get("result")), body)
    strong = result and (ADOPTED.search(result) or QUANT_CHANGE.search(result) or METRIC.search(result))
    result = result if strong else ""
    basis = _text(raw.get("basis"))
    if not basis_found(basis, body):
        level = max(level - 1, 1)
    if level == 3 and not result:
        level = 2
    if level == 2 and not re.search(r"\d", body) and not BUILD_VERB.search(body):
        level = 1
    if ASSIST.search(body) and not OWN.search(body):
        level = 1
    return {"text": text, "level": level, "result": result}


# ---------- 技能整理 ----------

def _domain(name, raw_domain, kb):
    """先查知识库，找不到用 AI 归类（要在领域清单里），返回 (层, 领域, 通用, 是否 AI 归类)。"""
    found = kb.match_skill_domain(name)
    if found:
        return (*found, False)
    layer = kb.skill_layer(raw_domain)
    if layer:
        return layer, raw_domain, raw_domain == "通用办公", True
    return "可迁移能力", "其他", False, True


def _project_tool_usages(projects, skills, usages, kb):
    """项目里写到的工具（技能栏里的英文名称、概述里列的技术），由代码直接在原句里找。
    个人项目的概述也是本人做的，一起算；团队项目只看本人工作。"""
    have = {(u["skill"].lower(), u["where"]) for u in usages}
    names = {s["name"] for s in skills if s["name"].isascii()} | {t for p in projects for t in p["overview_tools"] if t.isascii()}
    found = []
    for p in projects:
        sentences = [d["text"] for d in p["duties"]]
        if not p["team"] and p["overview"]:
            sentences += re.split(r"[。；;]", p["overview"])
        for name in names:
            if (name.lower(), p["name"]) in have:
                continue
            hit = next((s for s in sentences if re.search(rf"(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])", s, re.IGNORECASE)), None)
            domain = kb.match_skill_domain(name)
            if not hit or not domain or domain[2]:
                continue
            body = _squash(hit)
            level = 1 if ASSIST.search(body) and not OWN.search(body) else 3 if BUILD_VERB.search(body) and BUILD_OUTPUT.search(body) else 2
            found.append({"skill": name, "text": hit, "where": p["name"], "output": "", "level": level,
                          "layer": domain[0], "domain": domain[1], "ai": False})
            have.add((name.lower(), p["name"]))
    return found


def _build_usage(raw, kb, source):
    if not isinstance(raw, dict):
        return None
    skill, text = _text(raw.get("skill")), _text(raw.get("text"))
    if not skill or not text or _squash(text)[:12] not in source:
        return None
    body = _squash(text)
    # 自述句子（"熟练使用 Python"）不是使用证据；但同一句写了"负责、完成"等实际动作的是经历，不过滤
    if CLAIM_WORDS.search(body) and not (BUILD_VERB.search(body) or DOING.search(body)):
        return None
    # 工具（英文专有名称）必须按字面出现在原句里；方法和业务能力由 AI 指出，报告附原句
    if skill.isascii() and skill.lower().replace(" ", "") not in body.lower():
        return None
    output = _text(raw.get("output"))
    output = output if output and _squash(output) in body else ""
    if ASSIST.search(body) and not OWN.search(body):
        level = 1
    elif output and BUILD_VERB.search(body) and BUILD_OUTPUT.search(output):
        level = 3
    else:
        level = 2
    layer, domain, general, ai = _domain(skill, _text(raw.get("domain")), kb)
    if general:
        return None
    return {"skill": skill, "text": text, "where": _text(raw.get("where")), "output": output,
            "level": level, "layer": layer, "domain": domain, "ai": ai}


def _build_skill(raw, kb):
    if not isinstance(raw, dict) or not _text(raw.get("name")):
        return None
    name = _text(raw.get("name"))
    layer, domain, general, ai = _domain(name, _text(raw.get("domain")), kb)
    # 熟练程度由代码看原文："熟练使用 Excel、Python、SQL"里 AI 常常只给第一项标"熟练"
    level_word = re.search(r"精通|熟练|擅长|熟悉|掌握|了解", _text(raw.get("text")))
    claim = level_word.group() if level_word else _text(raw.get("claim"))
    return {"name": name, "claim": claim, "layer": layer, "domain": domain, "general": general, "ai": ai,
            "text": _text(raw.get("text"))}


CLAIM_ORDER = ("精通", "熟练", "擅长", "熟悉", "掌握", "了解", "")
TOOL_NAME = re.compile(r"[A-Za-z][A-Za-z0-9+#.\- ]*[A-Za-z0-9+#]|[A-Za-z]")


def strong_claims(text, kb):
    """原文里自述"精通 / 熟练 / 擅长"的工具，由代码直接找（AI 有时漏掉"核心优势"里的这类句子）。
    只取这些词后面、同一句里知识库认识的英文工具名；括号里的是前一个工具的功能，不单独算。"""
    names = []
    for m in re.finditer(r"(精通|熟练|擅长)([^。；;\n]*)", text):
        tail = re.sub(r"[（(][^）)]*[）)]", "", m.group(2))
        for tool in TOOL_NAME.findall(tail):
            found = kb.match_skill_domain(tool.strip())
            if found and not found[2]:
                names.append(tool.strip())
    return list(dict.fromkeys(names))


def _split_claims(skills, kb):
    """AI 有时把"熟练使用 Excel、Python、SQL、BI"整句当成一项技能（如"数据分析"）。
    把句子里知识库认识的英文工具名拆出来，各自带上这句的熟练程度；同名的保留最高的熟练程度。"""
    expanded = list(skills)
    for s in skills:
        if not s["claim"]:
            continue
        # 括号里的是前一个工具的具体功能（"Excel（VLOOKUP、数据透视表）"），不单独算一项
        for tool in TOOL_NAME.findall(re.sub(r"[（(][^）)]*[）)]", "", s["text"])):
            tool = tool.strip()
            found = kb.match_skill_domain(tool)
            if found and tool.lower() != s["name"].lower():
                expanded.append({"name": tool, "claim": s["claim"], "layer": found[0], "domain": found[1],
                                 "general": found[2], "ai": False, "text": s["text"]})
    best = {}
    for s in expanded:
        key = s["name"].lower()
        if key not in best or CLAIM_ORDER.index(s["claim"] if s["claim"] in CLAIM_ORDER else "") < \
                CLAIM_ORDER.index(best[key]["claim"] if best[key]["claim"] in CLAIM_ORDER else ""):
            best[key] = s
    return list(best.values())


def _build_course(raw, kb, source):
    if not isinstance(raw, dict) or not _text(raw.get("name")):
        return None
    name, score = _text(raw.get("name")), _text(raw.get("score"))
    if _squash(name) not in source:
        return None
    score = score if score and _squash(score) in source else ""
    layer, domain, general, ai = _domain(name, _text(raw.get("domain")), kb)
    return {"name": name, "score": score, "display": f"{name} {score}" if score else name,
            "layer": layer, "domain": domain, "ai": ai}


def _skill_depth(usages, team_tools, kb):
    """按领域汇总：每个领域的深度取最深的一条证据；同一技能在几段经历里用过单独统计。"""
    depth = {}
    for u in usages:
        d = depth.setdefault(u["domain"], {"layer": u["layer"], "ai": u["ai"], "evidence": [], "team": []})
        d["evidence"].append(u)
        d["ai"] = d["ai"] and u["ai"]
    used = {u["skill"].lower() for u in usages}
    for tool, project in team_tools:
        if tool.lower() in used:
            continue
        found = kb.match_skill_domain(tool)
        if not found or found[2]:
            continue  # 知识库不认识的多是具体方法名（如"八叉树"），不列出
        d = depth.setdefault(found[1], {"layer": found[0], "ai": False, "evidence": [], "team": []})
        d["team"].append((tool, project))
    for d in depth.values():
        d["level"] = max((e["level"] for e in d["evidence"]), default=0)
    return depth


def _depth_text(d):
    # 每项技能只在最高级别下显示一次；"几段经历"按这项技能的全部证据统计
    skills = {}
    for e in d["evidence"]:
        skills.setdefault(e["skill"].lower(), []).append(e)
    parts = []
    for level in (3, 2, 1):
        shown = []
        for items in skills.values():
            top = max(e["level"] for e in items)
            if top != level:
                continue
            places = list(dict.fromkeys(e["where"] for e in items if e["where"]))
            what = next((e["output"] for e in items if e["level"] == top and e["output"]), "")
            detail = "、".join(places[:2]) + (f"：{what}" if what else "")
            count = f"，{len(places)} 段经历" if len(places) >= 2 else ""
            shown.append(f"{items[0]['skill']}（{detail}{count}）" if detail else items[0]["skill"])
        if shown:
            parts.append(f"{SKILL_LEVELS[level]}：{'；'.join(shown)}")
    if d["team"]:
        tools = "、".join(dict.fromkeys(t for t, _ in d["team"]))
        parts.append(f"参与过使用该技术的团队项目：{tools}")
    return "｜".join(parts)


def _ai_mark(d):
    return " (AI 归类)" if d["ai"] else ""

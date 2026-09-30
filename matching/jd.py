"""企业导入 JD：提取要求 → 对照通用岗位画像 → 推荐最看重的 3 项 → 存成 HR 可以修改的岗位要求文件。

- 提取方法和构建通用岗位画像时完全相同（matching/jd_extract.py），两边的结果可以比较
- 找最接近的 1～2 个参考岗位并显示相似度；都不接近时明说"没有参考岗位"，不硬套
- 基础要求（在一半以上岗位里都常见的领域，如 Office）不参与排序，只提示面试确认
- 同类岗位普遍要求、区分度高的必须项，推荐为"最看重"；JD 写得太空时用参考岗位的常见要求补充并标注
- 最终标准由 HR 确认：打开生成的文件修改即可
"""
import json
import re
from pathlib import Path

from matching.jd_extract import LEVELS, QUALITIES, RESUME_JUDGEABLE, clean, prompt

ROOT = Path(__file__).resolve().parent.parent
PROFILES = ROOT / "knowledge" / "job_profiles.json"
CATEGORIES = ROOT / "knowledge" / "job_categories.json"
DEGREES = ("专科", "本科", "硕士", "博士")
SUPPLEMENT_SHARE = 0.2   # JD 太空时，用参考岗位里占比 20% 以上（常见及以上）的要求补充
TOP_N = 3


def load_knowledge():
    profiles = json.loads(PROFILES.read_text(encoding="utf-8"))["profiles"]
    specs = {c: s for g in json.loads(CATEGORIES.read_text(encoding="utf-8"))["categories"].values() for c, s in g.items()}
    # 基础要求：在一半以上岗位里占比都达到 20% 的领域（数据显示只有"通用办公"）
    counts = {}
    for p in profiles.values():
        # 每个岗位里一个领域只算一次（技能和业务能力里都出现也只算一次）
        common = {d["domain"] for key in ("skills", "business") for d in p[key] if d["share"] >= 0.2}
        for domain in common:
            counts[domain] = counts.get(domain, 0) + 1
    basic = {d for d, n in counts.items() if n * 2 >= len(profiles)}
    return profiles, specs, basic


def parse(text, title, kb, llm):
    profiles, specs, basic = load_knowledge()
    data = clean(llm.extract_json(prompt(kb), f"职位：{title}\n{text}"), text, kb)
    references = _references(title, data, profiles, specs)
    ref_profile = profiles[references[0]["岗位"]] if references else None

    requirements = []
    for key, kind in (("skills", "技能"), ("business", "业务能力")):
        for item in data[key]:
            share = _share(ref_profile, item["domain"])
            requirements.append({
                "名称": item["name"], "类型": kind, "领域": item["domain"], "程度": item["level"],
                "必须": item["required"], "基础要求": item["domain"] in basic,
                "参考岗位占比": share, "来源": "JD 原文"})
    # JD 写得太空（非基础要求不到 3 条）时，用参考岗位的常见要求补充，默认算加分，由 HR 决定
    if ref_profile and sum(not r["基础要求"] for r in requirements) < TOP_N:
        have = {r["领域"] for r in requirements}
        for key, kind in (("skills", "技能"), ("business", "业务能力")):
            for d in ref_profile[key]:
                if d["share"] >= SUPPLEMENT_SHARE and d["domain"] not in have and d["domain"] not in basic:
                    have.add(d["domain"])
                    requirements.append({
                        "名称": d["domain"], "类型": kind, "领域": d["domain"], "程度": d["typical_level"], "必须": False,
                        "基础要求": False, "参考岗位占比": d["share"],
                        "来源": f"参考通用要求（{references[0]['岗位']}岗 {d['share']:.0%} 的 JD 要求，如{'、'.join(d['top'][:3])}）"})

    qualities = [{"素质": q, "原文": data["quality_texts"].get(q, ""),
                  "用途": "排序参考" if q in RESUME_JUDGEABLE else "面试考察（简历无法判断）"}
                 for q in dict.fromkeys(data["qualities"])]
    return {
        "说明": "工具对 JD 的理解，请 HR 确认或修改：最看重（最多 3 项，按顺序）决定阅读队列；"
               "必须 / 加分可以改；门槛只用确定的事实判断，推断的一律进待确认；可在「补充条件」里加上 JD 没写但看重的条件。",
        "岗位": title,
        "参考岗位": references,
        "门槛": {"学历": _min_degree(data["hard"]["degree"]), "学历原文": data["hard"]["degree"]},
        "职责": parse_duties(text, kb, llm),
        "最看重": _recommend(requirements),
        "要求": requirements,
        "专业": {"要求": data["hard"]["majors"], "必须": False},
        "证书": {"要求": data["hard"]["certificates"], "必须": False},
        "素质": qualities,
        "补充条件": [],
    }


# ---------- 岗位职责：用来找"做过类似事"的人 ----------

DUTY_PROMPT = """你是招聘信息提取助手。从用户给出的岗位描述（JD）的"岗位职责"部分，逐条提取职责，只输出 JSON。

{{"duties": [{{"text": "职责原文（原样复制，一条职责一项）", "domain": "所属领域", "keywords": ["关键词"], "generic": false}}]}}

- text：原样复制，不改写、不概括；没有编号的按句号或分号拆开
- domain：从以下领域中选一个最贴近的：{domains}；都不贴近填 ""
- keywords：2～6 个能把这条职责和别的工作区分开的短词，必须原样出现在这条职责里；
  每个中文词不超过 4 个字（长短语要拆开，只取核心词），英文工具名、系统名原样保留；
  不要"数据""工作""业务""协助""相关""负责"这类到处都有的词
  例："负责门店销售数据的日报周报制作与异常预警" → ["销售", "日报", "周报", "预警"]
- generic：是否是空泛职责（如"完成领导交办的其他工作""服从安排""配合各部门工作"），是填 true
- 只提取岗位职责，不提取任职要求
"""
MAX_WORD = 4
# 关键词分级（按在 1596 份真实 JD 里出现的比例，knowledge/common_words.json）：
# ≥20% 是泛词（数据、分析、优化……），区分不出工作内容，不用；5%～20% 是常见词；不在表里（<5%）是具体词
VAGUE_SHARE = 0.2
SPECIFIC_SHARE = 0.05
WORD_SHARES = json.loads((ROOT / "knowledge" / "common_words.json").read_text(encoding="utf-8"))["words"]


def word_share(word):
    return WORD_SHARES.get(word.lower(), 0.0)


def domain_spread():
    """每个领域在多少比例的岗位里是常见要求（≥20% 的 JD 提到）。"""
    profiles = json.loads(PROFILES.read_text(encoding="utf-8"))["profiles"]
    counts = {}
    for p in profiles.values():
        for domain in {d["domain"] for key in ("skills", "business") for d in p[key] if d["share"] >= 0.2}:
            counts[domain] = counts.get(domain, 0) + 1
    return {d: n / len(profiles) for d, n in counts.items()}


def parse_duties(text, kb, llm):
    data = llm.extract_json(DUTY_PROMPT.format(domains="、".join(kb.skill_domain_names)), text)
    body = _squash(text)
    duties = []
    for raw in data.get("duties") or []:
        if not isinstance(raw, dict):
            continue
        duty = re.sub(r"\s+", " ", str(raw.get("text") or "")).strip()
        # 职责要能在原文找到（比较前 12 个字）
        if not duty or _squash(duty)[:12] not in body:
            continue
        words = [w.strip() for w in raw.get("keywords") or [] if isinstance(w, str)]
        # 关键词太长（如"经营分析报表"），简历里几乎不会原样出现，只保留 4 个字以内的中文词和英文名称
        words = list(dict.fromkeys(w for w in words if w and word_share(w) < VAGUE_SHARE and _squash(w) in _squash(duty)
                                   and (w.isascii() or len(w) <= MAX_WORD)))
        domain = raw.get("domain") if raw.get("domain") in kb.skill_domain_names else ""
        if not domain:
            found = kb.match_skill_domain(duty)
            domain = found[1] if found else ""
        # 空泛职责、或者提不出关键词的，不参与匹配
        generic = raw.get("generic") is True or not words
        duties.append({"职责": duty, "关键词": [] if generic else words, "领域": domain, "空泛": generic})
    return duties


def _squash(s):
    return re.sub(r"\s+", "", s or "").lower()


def _references(title, data, profiles, specs):
    """最接近的 1～2 个参考岗位。
    职位名称里有这个岗位的名称或搜索关键词（如"数据分析""采购专员"）才算名称相符，按要求重合度排序；
    名称都对不上时，才按要求重合度找（要求重合度 ≥ 70%，相似度"一般"）。
    不能只凭一个共同领域就算相近（"数据分析工程师"和嵌入式岗都要求编程，但不是同一类岗位）。"""
    domains = {i["domain"] for key in ("skills", "business") for i in data[key]}
    by_title, by_overlap = [], []
    for name, p in profiles.items():
        spec = specs.get(name, {})
        words = [name] + spec.get("keywords", [])
        title_hit = any(w.lower() in title.lower() for w in words) and not (
            spec.get("exclude") and re.search(spec["exclude"], title, re.I))
        typical = {d["domain"]: d["share"] for key in ("skills", "business") for d in p[key] if d["share"] >= 0.2}
        overlap = sum(s for d, s in typical.items() if d in domains) / sum(typical.values()) if typical else 0
        if title_hit:
            by_title.append({"岗位": name, "相似度": "高" if overlap >= 0.3 or not domains else "一般", "要求重合度": round(overlap, 2)})
        elif overlap >= 0.7:
            by_overlap.append({"岗位": name, "相似度": "一般", "要求重合度": round(overlap, 2)})
    ranked = sorted(by_title, key=lambda s: -s["要求重合度"]) or sorted(by_overlap, key=lambda s: -s["要求重合度"])
    return ranked[:2]


def _share(profile, domain):
    if not profile:
        return None
    return next((d["share"] for key in ("skills", "business") for d in profile[key] if d["domain"] == domain), 0.0)


def _recommend(requirements):
    """推荐最看重的 3 项：非基础的必须项，同类岗位里越普遍、JD 要求的程度越高越优先；每个领域只选一项。"""
    level = {"精通": 0.15, "熟练": 0.1, "掌握": 0.05}
    pool = sorted((r for r in requirements if not r["基础要求"]),
                  key=lambda r: -((r["参考岗位占比"] or 0.1) + 0.3 * r["必须"] + level.get(r["程度"], 0)))
    # 优先选不同领域；不够 3 项时再从同一领域补（如人力资源 JD 的要求全在人力资源领域）
    picked, domains = [], set()
    for r in pool:
        if r["领域"] not in domains and len(picked) < TOP_N:
            domains.add(r["领域"])
            picked.append(r["名称"])
    for r in pool:
        if r["名称"] not in picked and len(picked) < TOP_N:
            picked.append(r["名称"])
    return picked


def _min_degree(text):
    """"本科及以上"→ 本科；"硕士"→ 硕士；没写或"不限"→ 不限。"""
    found = [d for d in DEGREES if d in (text or "")]
    if "研究生" in (text or "") and "硕士" not in found:
        found.append("硕士")
    return min(found, key=DEGREES.index) if found else "不限"


def save(requirement, path):
    Path(path).write_text(json.dumps(requirement, ensure_ascii=False, indent=1), encoding="utf-8")


def load(path, kb):
    """读取岗位要求：jobs/xxx.json 是工具的原始理解，jobs/xxx.txt 是 HR 改过的版本，以 txt 为准。
    返回 (岗位要求, 提示列表)。"""
    path = Path(path)
    req = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    txt = path.with_suffix(".txt")
    if not txt.exists():
        return req, []
    return apply_text(req, txt.read_text(encoding="utf-8-sig"), kb)


# ---------- 给 HR 看和改的文字文件 ----------

SECTIONS = ("岗位职责", "最看重", "必须", "加分", "基础要求", "素质", "补充条件")
EXTRA_TYPES = ("院校", "学历", "实习", "证书", "专业")
SCHOOL_LEVELS = ("985", "211", "双一流")
PROTECTED = ("性别", "男", "女", "年龄", "岁", "民族", "籍贯", "户籍", "婚", "育", "政治面貌", "党员")
SUPPLEMENT_MARK = "［参考通用要求］"
HEADER = """\
# 岗位要求：{title}
# 这是工具对 JD 的理解，请直接修改后保存，再运行 python screen.py 检查 "{file}" 看工具是否理解对了。
# 规则：以 # 开头的行是说明，工具不读；每行一条，同一领域的要求用"、"分开；括号里是程度，可删；
#      想把某项从必须改成加分，把它剪切到"加分"下面即可；不要的直接删掉。
"""


def to_text(req, file_name):
    lines = [HEADER.format(title=req["岗位"], file=file_name)]
    lines.append(f"岗位：{req['岗位']}")
    refs = "、".join(f"{r['岗位']}（相似度{r['相似度']}）" for r in req["参考岗位"]) or "无（知识库里没有相近岗位）"
    lines.append(f"参考岗位：{refs}      # 只供参考，改了无效")
    lines.append(f"学历门槛：{req['门槛']['学历']}      # 可填 不限 / 专科 / 本科 / 硕士 / 博士；只按确定的学历判断")
    lines.append("")
    lines.append("岗位职责（用来找做过类似事的人；关键词可以增删，删光或写「不参与匹配」就不参与）：")
    lines.append("# 简历里同一句经历出现 2 个关键词，或出现 1 个且领域相同，就算做过这条职责")
    for n, d in enumerate(req["职责"], 1):
        tail = "｜不参与匹配" if d["空泛"] else f"｜关键词：{'、'.join(d['关键词'])}" + (f"｜领域：{d['领域']}" if d["领域"] else "")
        lines.append(f"{n}. {d['职责']} {tail}")
    lines.append("")
    lines.append("最看重（按顺序，最多 3 项，决定阅读顺序）：")
    lines += [f"{i}. {name}" for i, name in enumerate(req["最看重"], 1)]
    for title, keep in (("必须", lambda r: r["必须"] and not r["基础要求"]),
                        ("加分", lambda r: not r["必须"] and not r["基础要求"])):
        lines.append("")
        lines.append(f"{title}：")
        lines += _group_lines([r for r in req["要求"] if keep(r)])
        for key in ("专业", "证书"):
            if req[key]["要求"] and req[key]["必须"] == (title == "必须"):
                lines.append(f"- {key}：" + "、".join(req[key]["要求"]))
    basic = [r for r in req["要求"] if r["基础要求"]]
    if basic:
        lines.append("")
        lines.append("基础要求（大多数岗位都要求，不参与排序，面试时确认）：")
        lines += _group_lines(basic)
    lines.append("")
    lines.append("素质（简历能看出的只用于排序，其余面试考察；不看重的可删）：")
    lines += [f"- {q['素质']}（{'排序参考' if q['用途'] == '排序参考' else '面试考察'}）" for q in req["素质"]]
    lines.append("")
    lines.append("补充条件（JD 没写、但你们看重的；默认算加分，要当门槛就在后面写「（必须）」）：")
    lines.append("# 支持：院校：211 及以上 / 学历：硕士 / 实习：有 / 实习：数据分析 / 证书：CPA / 专业：统计学")
    lines += [f"- {c['类型']}：{c['值']}" + ("（必须）" if c["必须"] else "") for c in req["补充条件"]]
    lines.append("-")
    return "\n".join(lines) + "\n"


def _group_lines(requirements):
    groups = {}
    for r in requirements:
        groups.setdefault(r["领域"] or "其他", []).append(
            r["名称"] + (f"（{r['程度']}）" if r["程度"] else "") + ("" if r["来源"] in ("JD 原文", "HR 添加") else SUPPLEMENT_MARK))
    return [f"- {domain}：" + "、".join(items) for domain, items in groups.items()]


def apply_text(req, text, kb):
    """把 HR 改过的文字文件合并到岗位要求上。看不懂的行不猜，跳过并提示第几行。"""
    req = json.loads(json.dumps(req, ensure_ascii=False))
    old = {r["名称"]: r for r in req["要求"]}
    warnings, section, top, requirements, qualities, extras, duties = [], None, [], [], [], [], []
    majors = {"要求": [], "必须": False}
    certs = {"要求": [], "必须": False}

    for no, raw in enumerate(text.splitlines(), 1):
        line = re.sub(r"\s+#.*$", "", raw).strip()
        if not line or line.startswith("#"):
            continue
        head = re.match(r"^(" + "|".join(SECTIONS) + r")\s*[（(：:]", line)
        if head:
            section = head.group(1)
            continue
        key_value = re.match(r"^(岗位|参考岗位|学历门槛)\s*[：:]\s*(.*)$", line)
        if key_value:
            key, value = key_value.groups()
            if key == "学历门槛":
                degree = _min_degree(value)
                if degree != "不限" or "不限" in value:
                    req["门槛"]["学历"] = degree
                else:
                    warnings.append(f"第 {no} 行：学历门槛「{value}」看不懂，应填 不限 / 专科 / 本科 / 硕士 / 博士，仍按「{req['门槛']['学历']}」")
            continue
        item = re.sub(r"^(\d+[.、．)]|[-－·•])\s*", "", line).strip()
        if not item:
            continue
        if section is None:
            warnings.append(f"第 {no} 行：「{line}」不在任何一栏下面，已忽略")
        elif section == "岗位职责":
            duties.append(_duty(item, kb))
        elif section == "最看重":
            top.append((no, _strip_level(item)[0]))
        elif section in ("必须", "加分", "基础要求"):
            label, names = _split_label(item)
            if label in ("专业", "证书"):
                target = majors if label == "专业" else certs
                target["要求"] += names
                target["必须"] = target["必须"] or section == "必须"
                continue
            for name in names:
                requirements.append(_requirement(name, label, section, old, kb))
        elif section == "素质":
            name = re.sub(r"\s*[（(].*$", "", item)
            if name in QUALITIES:
                qualities.append({"素质": name, "原文": next((q["原文"] for q in req["素质"] if q["素质"] == name), ""),
                                  "用途": "排序参考" if name in RESUME_JUDGEABLE else "面试考察（简历无法判断）"})
            else:
                warnings.append(f"第 {no} 行：素质「{name}」不认识，已忽略（可选：{'、'.join(QUALITIES)}）")
        elif section == "补充条件":
            extra, problem = _extra(item)
            if problem:
                warnings.append(f"第 {no} 行：补充条件「{item}」{problem}")
            elif extra["类型"] == "学历":
                req["门槛"]["学历"] = extra["值"]
            else:
                extras.append(extra)

    # 同一项写了两次，以最后一次为准
    requirements = list({r["名称"]: r for r in requirements}.values())
    names = {r["名称"] for r in requirements}
    picked = []
    for no, name in top:
        if name in picked:
            continue
        if len(picked) == TOP_N:
            warnings.append(f"第 {no} 行：最看重最多 3 项，「{name}」没有算进去")
            continue
        if name not in names:
            requirements.append(_requirement(name, "", "必须", old, kb))
            names.add(name)
            warnings.append(f"第 {no} 行：「{name}」在最看重里，但必须 / 加分里没有，已按必须项加入")
        picked.append(name)
    for r in requirements:
        if r["名称"] in picked and r["基础要求"]:
            warnings.append(f"「{r['名称']}」是基础要求（人人都写，区分不出人），放进最看重作用不大，已保留")
    for extra in extras:
        if extra["类型"] in ("证书", "专业"):
            target = certs if extra["类型"] == "证书" else majors
            if extra["值"] not in target["要求"]:
                target["要求"].append(extra["值"])
            target["必须"] = target["必须"] or extra["必须"]
    req.update({"职责": duties, "最看重": picked, "要求": requirements, "专业": majors, "证书": certs, "素质": qualities,
                "补充条件": [e for e in extras if e["类型"] not in ("证书", "专业")]})
    if not picked:
        warnings.append("没有填最看重的项，所有简历只能按加分项排序")
    line_no = lambda w: int(re.match(r"第 (\d+) 行", w).group(1)) if w.startswith("第 ") else 10 ** 6
    return req, sorted(warnings, key=line_no)


def _duty(item, kb):
    """"职责原文 ｜关键词：成本、报价｜领域：采购供应链" → 职责；"｜不参与匹配"的不参与匹配。"""
    parts = [p.strip() for p in re.split(r"\s*[｜|]\s*", item)]
    duty, words, domain, generic = parts[0], [], "", False
    for p in parts[1:]:
        m = re.match(r"^(关键词|领域)\s*[：:]\s*(.*)$", p)
        if m and m.group(1) == "关键词":
            words = [w.strip() for w in re.split(r"[、，,；;\s]+", m.group(2)) if w.strip()]
        elif m:
            domain = m.group(2).strip() if m.group(2).strip() in kb.skill_domain_names else ""
        elif "不参与" in p:
            generic = True
    return {"职责": duty, "关键词": [] if generic else words, "领域": domain, "空泛": generic or not words}


def _strip_level(item):
    """"Excel（掌握）［参考通用要求］" → ("Excel", "掌握", 是否参考通用要求)"""
    supplement = SUPPLEMENT_MARK in item
    item = item.replace(SUPPLEMENT_MARK, "").strip()
    m = re.match(r"^(.*?)\s*[（(]([^（）()]*)[）)]$", item)
    if m and m.group(2) in LEVELS:
        return m.group(1).strip(), m.group(2), supplement
    return item, "", supplement


def _split_label(item):
    """"数据处理与分析：数据清洗、ETL" → ("数据处理与分析", ["数据清洗", "ETL"])；没有冒号时整行都是要求。"""
    m = re.match(r"^([^：:]{1,20})\s*[：:]\s*(.+)$", item)
    label, rest = (m.group(1).strip(), m.group(2)) if m else ("", item)
    names = [n.strip() for n in re.split(r"[、，,；;]", rest) if n.strip()]
    return label, names


def _requirement(text, label, section, old, kb):
    name, level, supplement = _strip_level(text)
    if name in old:
        r = dict(old[name])
    else:
        match = kb.match_skill_domain(name)
        domain = match[1] if match else ("" if label in ("", "其他") else label)
        r = {"名称": name, "类型": "技能", "领域": domain, "参考岗位占比": None,
             "来源": "参考通用要求" if supplement else "HR 添加"}
    r["程度"] = level
    # 基础要求不参与排序，必须 / 加分保留原样
    r["必须"] = r.get("必须", False) if section == "基础要求" else section == "必须"
    r["基础要求"] = section == "基础要求"
    return r


def _extra(item):
    """补充条件："院校：211 及以上""实习：数据分析（必须）"。返回 (条件, 问题说明)。"""
    must = bool(re.search(r"[（(]必须[）)]$", item))
    item = re.sub(r"\s*[（(](必须|加分)[）)]$", "", item)
    m = re.match(r"^([^：:]+)\s*[：:]\s*(.+)$", item)
    if not m:
        return None, "格式应为「类型：内容」，如「院校：211 及以上」，已忽略"
    kind, value = m.group(1).strip(), m.group(2).strip()
    if any(word in kind for word in PROTECTED):
        return None, "不能作为筛选条件（就业歧视风险，《就业促进法》等法规禁止），已忽略"
    if kind not in EXTRA_TYPES:
        return None, f"暂不支持「{kind}」，已忽略（支持：{'、'.join(EXTRA_TYPES)}）"
    if kind == "院校" and not any(s in value for s in SCHOOL_LEVELS):
        return None, f"院校层次看不懂，可写 {' / '.join(SCHOOL_LEVELS)}（如「211 及以上」），已忽略"
    if kind == "学历":
        value = next((d for d in DEGREES if d in value), None)
        if not value:
            return None, "学历应为 专科 / 本科 / 硕士 / 博士，已忽略"
    return {"类型": kind, "值": value, "必须": must}, None


def summary(req):
    """给 HR 看的确认摘要："我理解你们要的是这些，对吗？" """
    lines = [f"岗位：{req['岗位']}"]
    refs = "、".join(f"{r['岗位']}（相似度{r['相似度']}）" for r in req["参考岗位"])
    lines.append(f"参考岗位：{refs or '知识库里没有相近的岗位，核心要求请以 JD 和您的确认为准'}")
    source = req["门槛"]["学历原文"]
    changed = _min_degree(source) != req["门槛"]["学历"]
    lines.append(f"学历门槛：{req['门槛']['学历']}" + (f"（{'HR 已修改，' if changed else ''}JD 原文：{source}）" if source else ""))
    used = [d for d in req.get("职责", []) if not d["空泛"]]
    lines.append(f"岗位职责（{len(used)} 条参与匹配，用来找做过类似事的人）：")
    lines += [f"  {i}. {d['职责'][:30]}{'…' if len(d['职责']) > 30 else ''}  关键词：{'、'.join(d['关键词'])}"
              for i, d in enumerate(used, 1)]
    lines.append("建议最看重（决定阅读顺序）："+ "；".join(f"{i}. {n}" for i, n in enumerate(req["最看重"], 1)))
    must = [r for r in req["要求"] if r["必须"] and not r["基础要求"] and r["名称"] not in req["最看重"]]
    plus = [r for r in req["要求"] if not r["必须"] and not r["基础要求"]]
    basic = [r for r in req["要求"] if r["基础要求"]]
    if must:
        lines.append("其他必须项：" + "、".join(_req_text(r) for r in must))
    if plus:
        lines.append("加分项：" + "、".join(_req_text(r) for r in plus))
    if basic:
        lines.append("基础要求（人人都写，不参与排序，面试确认）：" + "、".join(r["名称"] for r in basic))
    for key in ("专业", "证书"):
        if req[key]["要求"]:
            lines.append(f"{key}（{'必须' if req[key]['必须'] else '加分'}）：" + "、".join(req[key]["要求"]))
    ranked = [q["素质"] for q in req["素质"] if q["用途"] == "排序参考"]
    interview = [q["素质"] for q in req["素质"] if q["用途"] != "排序参考"]
    if ranked:
        lines.append("素质（简历可判断，只用于排序）：" + "、".join(ranked))
    if interview:
        lines.append("素质（需面试考察）：" + "、".join(interview))
    if req["补充条件"]:
        lines.append("补充条件：" + "；".join(
            f"{c['类型']}：{c['值']}（{'必须' if c['必须'] else '加分'}）" for c in req["补充条件"]))
    return "\n".join(lines)


def _req_text(r):
    text = r["名称"] + (f"（{r['程度']}）" if r["程度"] else "")
    return text + {"JD 原文": "", "HR 添加": "［HR 添加］"}.get(r["来源"], SUPPLEMENT_MARK)

"""总览排序：不导入 JD 也能对一批简历做基本判断，HR 勾选几个维度分层。

- 每个维度都用各模块已有的档位，不打总分、不加权
- **只在"全面不差"时才分先后**：A 在所选每一项上都不比 B 差、且至少一项更好，A 才排在 B 前面；
  否则两人"各有所长"，放在同一层（2026-10-01 用户确认；原来"先比第一项、相同再比下一项"等于给第一项无限大的权重，
  10 个人排出 9～10 个名次，是虚假的精确）
- 档位细度和判断的可靠程度匹配：学校层次来自外部排名，用细档位；实习、项目的深度是从措辞判断的，只用粗档位，
  时长、平台只显示不排序
- 学历是门槛（达到即可），默认不参与；学业走势只显示；硬性要求只筛选
- 没写的不等于差：学业成绩"未写"排在"一般"后面，但标注"未写 ≠ 差"
"""
import re

from matching import match
from modules.experience import LEVEL_NAMES

DIMENSIONS = ("学校层次", "学历", "学业成绩", "实习经历", "项目经历", "技能", "通用素质", "证书荣誉", "岗位匹配")
DEFAULT_DIMENSIONS = ("实习经历", "项目经历", "技能")
SUGGESTED_MAX = 3        # 勾选太多时，"各有所长"的人会很多
SCHOOL_MODES = ("先看最高学历", "先看第一学历")
DEGREES = ("专科", "本科", "硕士", "博士")
SCHOOL_LIMITS = {"不限": None, "双非第一梯队及以上": 3, "211 / 双一流及以上": 2, "985 及以上": 1}
OTHER_TIER, VOCATIONAL = 9, 10          # 知识库"基础"档（其他本科）= 9；专科排在最后
TIER3 = {"优秀": 3, "良好": 2, "一般": 1}
SKILL_NAMES = {3: "能构建", 2: "能解决问题", 1: "会操作", 0: "学过（课程）", -1: "只有自述", -2: "没体现"}
QUALITY_ITEMS = ("责任心", "主动性", "影响力", "学习成长")
# 实习经历：实习、全职工作、社会实践（做事深度和在哪做无关，实习模块也这样算）；课程实习是课程安排，算在项目经历里
WORK_KINDS = ("实习", "全职工作", "社会实践")
SEGMENTS = re.compile(r"（(\d+) 段")


def rank(profiles, dims, school_mode=SCHOOL_MODES[0], filters=None, job=None, kb=None):
    """返回 (分好层的行, 硬性要求不符的行, 需人工查看的行)。每行带"层"和"为什么"。
    job：已确认的岗位要求（可选），有了才能用"岗位匹配"维度。"""
    filters = filters or {}
    matched = {}
    if job:
        results, _ = match.screen(profiles, job, kb)
        matched = {r["文件"]: r for r in results}
    rows = [_row(p, school_mode, filters, matched.get(p["file"])) for p in profiles]
    dims = [d for d in dims if d in DIMENSIONS and (d != "岗位匹配" or job)]
    ranked = [r for r in rows if not r["不符"] and not r["解析异常"]]
    _layer(ranked, dims)
    return ranked, [r for r in rows if r["不符"] and not r["解析异常"]], [r for r in rows if r["解析异常"]]


# ---------- 分层 ----------

def _layer(rows, dims):
    """逐层取出"没有人全面超过他"的人；同层内按勾选的第一项显示（只为方便浏览，不代表先后）。"""
    if not dims:
        for r in rows:
            r["层"], r["为什么"] = 1, "没有选排序依据"
        return
    values = {id(r): [r["_sort"][d] for d in dims] for r in rows}
    left, layer = rows[:], 0
    while left:
        layer += 1
        top = [r for r in left if not any(_beats(values[id(o)], values[id(r)]) for o in left)]
        for r in top:
            r["层"] = layer
            r["为什么"] = _why_top(r, rows, dims, values) if layer == 1 else _why_below(r, rows, dims, values, layer)
        left = [r for r in left if r not in top]
    rows.sort(key=lambda r: (r["层"], [tuple(-x for x in v) for v in values[id(r)]]))


def _beats(a, b):
    """a 在每一项上都不比 b 差，且至少一项更好。"""
    return all(x >= y for x, y in zip(a, b)) and any(x > y for x, y in zip(a, b))


def _why_top(r, rows, dims, values):
    best = [d for i, d in enumerate(dims) if values[id(r)][i] == max(values[id(o)][i] for o in rows)]
    if len(dims) == 1:
        return f"{dims[0]}在所有人里最好的一档"
    return "所选各项没有被任何人全面超过" + (f"；{'、'.join(best)}是所有人里最好的一档" if best else "")


def _why_below(r, rows, dims, values, layer):
    """找上一层里全面超过他的人，说明在哪几项上更好。"""
    above = [o for o in rows if o.get("层") == layer - 1 and _beats(values[id(o)], values[id(r)])]
    o = max(above, key=lambda o: sum(x > y for x, y in zip(values[id(o)], values[id(r)])))
    better = [d for i, d in enumerate(dims) if values[id(o)][i] > values[id(r)][i]]
    if len(dims) == 1:
        return f"{dims[0]}低于上一层（如 {_name(o['文件'])}）"
    return f"被 {_name(o['文件'])} 全面超过：{'、'.join(better)}更好，其余各项不差"


def _name(file):
    return file.rsplit(".", 1)[0]


def _row(p, school_mode, filters, job_result):
    school, school_sort = _school(p, school_mode)
    degree, degree_sort = _degree(p)
    grades, grades_sort = _grades(p)
    work, work_sort = _work(p)
    project, project_sort = _project(p)
    skill, skill_sort = _skill(p)
    quality, quality_sort = _quality(p)
    honor, honor_sort = _honor(p)
    fails, unsure = _filters(p, school_mode, filters)
    row = {"文件": p["file"], "学校层次": school, "学历": degree, "学业成绩": grades,
           "学业走势": _tier_name_in((p.get("education") or {}).get("trend", "")),
           "实习经历": work, "项目经历": project, "技能": skill,
           "通用素质": quality, "证书荣誉": honor, "不符": fails, "待确认": unsure,
           "解析异常": p["parse"]["reasons"] if p["parse"]["abnormal"] else [],
           "_sort": {"学校层次": school_sort, "学历": degree_sort, "学业成绩": grades_sort, "实习经历": work_sort,
                     "项目经历": project_sort, "技能": skill_sort, "通用素质": quality_sort, "证书荣誉": honor_sort}}
    # 岗位匹配只比队列（优先看 / 值得看 / 可以后看），队列内的细小差别不参与分层
    row["岗位匹配"] = job_result["队列"] if job_result else ""
    row["_sort"]["岗位匹配"] = (-match.QUEUES.index(job_result["队列"]),) if job_result else (0,)
    return row


def _tier_name_in(text):
    return text.replace("（基础 →", "（其他本科 →").replace("→ 基础）", "→ 其他本科）")


# ---------- 各维度：返回 (显示文字, 排序值，越大越好) ----------

def _stages(p, school_mode):
    edu = p.get("education") or {}
    highest, first = edu.get("highest"), edu.get("first")
    return (highest, first) if school_mode == SCHOOL_MODES[0] else (first, highest)


def _tier_value(stage):
    if not stage:
        return -OTHER_TIER, "未写学校"
    if stage["degree"] == "专科":
        return -VOCATIONAL, f"专科（{stage['school']}）"
    if stage.get("tier_level") is None:
        return -OTHER_TIER, f"{stage['school']}（未收录，请核实）"
    return -stage["tier_level"], f"{_tier_name(stage['tier'])}（{stage['school']}）"


def _tier_name(tier):
    """知识库里的"基础"档就是没进名单的普通本科，给 HR 看时说"其他本科"。"""
    return "其他本科" if tier == "基础" else tier


def _school(p, school_mode):
    main, other = _stages(p, school_mode)
    value, text = _tier_value(main)
    other_value, other_text = _tier_value(other) if other and other != main else (value, "")
    label = "第一学历" if school_mode == SCHOOL_MODES[0] else "最高学历"
    return text + (f"｜{label}：{other_text}" if other_text else ""), (value, other_value)


def _degree(p):
    highest = (p.get("education") or {}).get("highest")
    if not highest or highest["degree"] not in DEGREES:
        return "未写", (0,)
    return highest["degree"] + ("（推断，待确认）" if not highest["certain"] else ""), (DEGREES.index(highest["degree"]) + 1,)


def _grades(p):
    """优先看本科（专科生看专科）的成绩：研究生阶段的成绩 HR 不怎么关注（用户 2026-10-01）。
    本科没写成绩时才用其他阶段，并注明。"""
    grades = [g for g in (p.get("education") or {}).get("grades", []) if g["tier"] in TIER3]
    if not grades:
        return "未写（≠ 差）", (0,)
    undergrad = [g for g in grades if g["degree"] in ("本科", "专科")]
    g = undergrad[-1] if undergrad else grades[0]
    note = "" if undergrad else "，本科未写成绩"
    return f"{g['tier']}（{g['stage']}：{g['basis']}{note}）", (TIER3[g["tier"]],)


def _depth_value(entries):
    """粗档位：主导改进，有成果 3 / 独立负责 2 / 协助完成 1 / 有经历但没写做了什么 0 / 没有 -1。"""
    return max((e["level"] for e in entries), default=-1)


def _depth_text(value):
    return LEVEL_NAMES[value] if value > 0 else "没写本人做了什么"


def _work(p):
    """实习经历：只按做到什么程度分档；时长、平台只显示（用实习模块算好的，时间重叠的已合并）。"""
    practice = p.get("practice") or {}
    work = [e for e in practice.get("experiences", []) if e["kind"] in WORK_KINDS]
    value = _depth_value(work)
    if value < 0:
        return "无", (value,)
    kinds = "、".join(dict.fromkeys(e["kind"] for e in work))
    detail = []
    real = sum(e["kind"] != "社会实践" for e in work)
    amount = practice.get("amount_text", "")
    if real and "（" in amount:
        merged = SEGMENTS.search(amount)
        detail.append(amount.split("（", 1)[1].rstrip("）")
                      + ("，时间重叠的按 1 段计" if merged and int(merged.group(1)) < real else ""))
        detail.append(practice.get("platform", ""))
    return f"{_depth_text(value)}｜{kinds}" + (f"（{'，'.join(x for x in detail if x)}）" if detail else ""), (value,)


def _project(p):
    """项目经历：个人项目、团队项目、课程实习，只按做到什么程度分档。"""
    projects = p.get("projects", [])
    courses = [e for e in (p.get("practice") or {}).get("experiences", []) if e["kind"] == "课程实习"]
    value = _depth_value(projects + courses)
    if value < 0:
        return "无", (value,)
    parts = [f"项目 {len(projects)} 个"] if projects else []
    if courses:
        parts.append(f"课程实习 {len(courses)} 段")
    strong = [x["name"] for x in projects if x.get("credibility") == "强"]
    note = f"，可信度强：{'、'.join(strong[:2])}" if strong else ""
    return f"{_depth_text(value)}｜{'、'.join(parts)}{note}", (value,)


def _skill(p):
    """技能深度：只算在实习 / 项目 / 校园经历里真的用过的；有实证的领域数只显示。"""
    skills = [e for e in p.get("evidence", []) if e["kind"] == "技能"]
    proven = [e for e in skills if e["level"] >= 1 and e["context"] in match.PROVEN]
    if proven:
        best = max(e["level"] for e in proven)
        domains = list(dict.fromkeys(e["domain"] for e in proven if e["level"] == best))
        all_domains = {e["domain"] for e in proven}
        text = f"{SKILL_NAMES[best]}（{'、'.join(domains[:3])}{' 等' if len(domains) > 3 else ''}）｜有实证的领域 {len(all_domains)} 个"
        return text, (best,)
    if any(e["level"] == 0 for e in skills):
        return SKILL_NAMES[0], (0,)
    if skills:
        return SKILL_NAMES[-1], (-1,)
    return SKILL_NAMES[-2], (-2,)


def _quality(p):
    q = p.get("qualities") or {}
    strong = [k for k in QUALITY_ITEMS if q.get(k) == "强"]
    medium = [k for k in QUALITY_ITEMS if q.get(k) == "中"]
    parts = [f"{k} 强" for k in strong] + [f"{k} 中" for k in medium]
    return ("、".join(parts) or "简历中未体现"), (len(strong), len(medium))


def _honor(p):
    honors = p.get("honors") or {}
    items = [i for key in ("school", "competitions", "research") for i in honors.get(key, []) if i.get("tier") in TIER3]
    if not items:
        return "无", (0,)
    best = max(TIER3[i["tier"]] for i in items)
    top = [i["name"] for i in items if TIER3[i["tier"]] == best]
    tier = next(k for k, v in TIER3.items() if v == best)
    name = top[0] if len(top[0]) <= 24 else top[0][:24] + "…"
    return f"{tier}（{name}{' 等' if len(top) > 1 else ''}）", (best,)


# ---------- 硬性要求：只筛选 ----------

def _filters(p, school_mode, filters):
    """返回 (确定不符合的, 待确认的)。推断的、没写的只进待确认。"""
    fails, unsure = [], []
    edu = p.get("education") or {}
    need = filters.get("最低学历")
    highest = edu.get("highest")
    if need in DEGREES:
        if not highest or highest["degree"] not in DEGREES:
            unsure.append(f"学历没写（要求{need}）")
        elif DEGREES.index(highest["degree"]) < DEGREES.index(need):
            (fails if highest["certain"] else unsure).append(f"学历{highest['degree']}，要求{need}")
        elif not highest["certain"]:
            unsure.append(f"学历为推断（{highest['degree']}）")
    limit = SCHOOL_LIMITS.get(filters.get("院校"))
    if limit is not None:
        main, _ = _stages(p, school_mode)
        if not main or (main.get("tier_level") is None and main.get("degree") != "专科"):
            unsure.append("学校未收录，层次待确认")
        elif main["degree"] == "专科" or main["tier_level"] > limit:
            fails.append(f"学校层次不符（要求{filters['院校']}）")
    words = [w for w in filters.get("专业", []) if w]
    if words:
        stages = edu.get("stages", [])
        text = "".join((s.get("major") or "") + (s.get("major_class") or "") for s in stages)
        if not text:
            unsure.append("专业没写")
        elif not any(w in text for w in words):
            fails.append(f"专业不含：{'、'.join(words)}")
    for cert in filters.get("证书", []):
        if cert and not match._cert_hit(cert, p):
            # 用户决定（2026-10-01）：有证书的人几乎都会写，没写就算不符合
            fails.append(f"简历里没有证书：{match.cert_key(cert)}")
    return fails, unsure

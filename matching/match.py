"""按岗位要求给一批简历分队列：告诉 HR 先看谁、为什么。

证据从强到弱（按顺序比较，不加权）：
1. 做过类似的事：JD 的某条职责，在简历的某条经历原句里对上
   （同一句出现 2 个关键词且领域不冲突，或 1 个具体词且这句话能识别出同一领域；泛词不算，见 jd.py 的关键词分级）
2. 最看重的能力有实证：在实习 / 项目 / 校园经历里用过（课程、技能栏自述、团队项目里别人用的技术都不算）
   先比具体名称；对不上再比领域（标"相近"），但宽泛领域（1/3 以上岗位都常见，如数据处理与分析）不比领域
3. 只有自述或课程；加分项
"宁可多留"：没写的能力不算不符合，只是排在后面；只有确定的事实不满足门槛才进"硬条件不符"。
"""
import re

from matching.jd import SPECIFIC_SHARE, VAGUE_SHARE, domain_spread, word_share

QUEUES = ("优先看", "值得看", "可以后看", "硬条件不符", "需人工查看")
DEGREES = ("专科", "本科", "硕士", "博士")
# 算"实证"的情境：真的做过（团队项目里别人用的技术、课程、自述、证书不算）
PROVEN = {"全职工作", "实习", "项目", "社会实践", "校园经历", "课程实习"}
CONTEXT_RANK = {"全职工作": 5, "实习": 5, "项目": 4, "社会实践": 3, "校园经历": 3, "课程实习": 3}
SCHOOL_LEVEL = {"985": 1, "211": 2, "双一流": 2}   # 院校补充条件 → 允许的最低层级（数字越小层次越高）
# 高级别证书包含低级别（考过六级一定考过四级）
CERT_COVERS = {"英语四级": ("四级", "六级", "CET-4", "CET4", "CET-6", "CET6", "专八", "专四", "专业八级", "专业四级"),
               "英语六级": ("六级", "CET-6", "CET6", "专八", "专业八级")}
MAX_QUESTIONS = 3
BROAD_SPREAD = 1 / 3   # 在 1/3 以上岗位里都是常见要求的领域算宽泛领域，领域相同说明不了什么


def screen(profiles, req, kb):
    """返回每个候选人的结果（已按队列和队列内顺序排好）和整体提示。"""
    top = [r for r in req["要求"] if r["名称"] in req["最看重"]]
    top.sort(key=lambda r: req["最看重"].index(r["名称"]))
    duties = [d for d in req.get("职责", []) if not d["空泛"]]
    broad = {d for d, s in domain_spread().items() if s >= BROAD_SPREAD}
    results = [_judge(p, req, top, duties, kb, broad) for p in profiles]
    results.sort(key=lambda r: (QUEUES.index(r["队列"]), r["_key"]))
    return results, _hints(results)


def _judge(profile, req, top, duties, kb, broad):
    units = _units(profile)
    done = [m for m in (_duty_match(d, units, kb, broad) for d in duties) if m]
    abilities = [_ability(r, profile["evidence"], units, kb, broad) for r in top]
    proven = sum(a["grade"] == "实证" for a in abilities)
    claimed = sum(a["grade"] == "自述 / 课程" for a in abilities)
    bonus = _bonus(profile, req, units, kb, broad)
    fails, unsure = _hard(profile, req)

    if profile["parse"]["abnormal"]:
        queue = "需人工查看"
    elif fails:
        queue = "硬条件不符"
    elif len(done) >= 2 or proven >= 2:
        queue = "优先看"
    elif done or proven or claimed >= 2:
        queue = "值得看"
    else:
        queue = "可以后看"
    # 证据强度：先比做得多深（L1 辅助 / L2 独立 / L3 改进），再比在什么情境做的（实习 > 项目 > 校园）
    depth = sum(m["level"] for m in done) + sum(a["level"] for a in abilities if a["grade"] == "实证")
    strength = sum(m["rank"] for m in done) + sum(a["rank"] for a in abilities if a["grade"] == "实证")
    return {
        "文件": profile["file"], "队列": queue,
        "做过类似的事": done, "最看重": abilities, "加分": bonus,
        "硬条件不符": fails, "待确认": unsure,
        "电话问题": _questions(profile, abilities, unsure) if queue in ("优先看", "值得看") else [],
        "解析": profile["parse"]["reasons"],
        "_key": (-len(done), -proven, -depth, -strength, -claimed, -len(bonus)),
    }


# ---------- 做过类似的事 ----------

def _units(profile):
    """简历里每条经历原句，附上出自哪段经历、什么情境。"""
    units = []
    for e in profile.get("practice", {}).get("experiences", []):
        units += [{"text": d["text"], "where": e["name"], "context": e["kind"], "level": d["level"]} for d in e.get("duties", [])]
    for e in profile.get("campus", {}).get("experiences", []):
        units += [{"text": d["text"], "where": e["name"], "context": "校园经历", "level": d["level"]} for d in e.get("duties", [])]
    for p in profile.get("projects", []):
        units += [{"text": d["text"], "where": p["name"], "context": "项目", "level": d["level"]} for d in p.get("duties", [])]
    return units


def _duty_match(duty, units, kb, broad):
    # 这句话明确属于别的领域（如人事制度里提到"品质、生产"）时，关键词对上也不算；
    # 宽泛领域（数据处理等）的工作本来就出现在各种领域的句子里，不查冲突
    check_conflict = duty["领域"] and duty["领域"] not in broad
    best = None
    for u in units:
        body = _squash(u["text"])
        hits = [w for w in duty["关键词"] if word_share(w) < VAGUE_SHARE and _squash(w) in body]
        specific = [w for w in hits if word_share(w) < SPECIFIC_SHARE]
        domains = kb.match_skill_domains_all(u["text"])
        conflict = check_conflict and domains and duty["领域"] not in domains
        # 只对上 1 个词时，这句话必须能识别出属于同一领域（"决策权""专题讲座"里的词罕见，但意思不具体）
        if (len(hits) >= 2 and not conflict) or (specific and duty["领域"] in domains):
            rank = CONTEXT_RANK.get(u["context"], 2)
            # 同一条职责对上多句时，取做得最深（L3 改进 > L2 独立 > L1 辅助）、情境最强的一句
            if not best or (u["level"], rank, len(hits)) > (best["level"], best["rank"], len(best["hits"])):
                best = {"duty": duty["职责"], "hits": hits, "rank": rank, **u}
    return best


# ---------- 最看重的能力 ----------

def _ability(req, evidence, units, kb, broad):
    """先比具体名称（Excel、SQL），对不上再比领域（标"相近"）；取最强的一条证据。
    证据来自两处：各模块整理的证据列表，以及代码直接在经历原句里找（不依赖 AI 有没有列出）。"""
    name = _squash(req["名称"])
    by_domain = req["领域"] and req["领域"] not in broad
    candidates = []
    for e in evidence:
        if e["kind"] != "技能":
            continue
        other = _squash(e["name"])
        # 要求名称被证据包含（"Python" ⊂ "Python 数据清洗"）算对上；反过来证据名称短、要求名称长时
        # （"数据分析" ⊂ "AI 数据分析"）只说明做过其中一部分，算"相近"；证据名称是泛词的不算
        exact = len(name) >= 2 and name in other
        part = len(other) >= 2 and other in name and word_share(other) < VAGUE_SHARE
        if exact or part or (by_domain and e["domain"] == req["领域"]):
            proven = e["level"] >= 1 and e["context"] in PROVEN
            candidates.append((proven, exact, e["context"], e["where"], e["name"], e["text"], max(e["level"], 0)))
    for u in units:
        exact = len(name) >= 2 and name in _squash(u["text"])
        span = None if exact or not by_domain else kb.domain_word_at(u["text"], req["领域"])
        if exact or span:
            # 按领域对上的，截取对上的词前后的原句，让 HR 自己判断
            text = u["text"] if exact else u["text"][max(0, span[0] - 12):span[1] + 18]
            candidates.append((u["context"] in PROVEN, exact, u["context"], u["where"], "同领域原句", text, u["level"]))
    if not candidates:
        return {"name": req["名称"], "grade": "没体现", "rank": 0}
    proven, exact, context, where, matched, text, level = max(
        candidates, key=lambda c: (c[0], c[1], c[6], CONTEXT_RANK.get(c[2], 1)))
    return {"name": req["名称"], "grade": "实证" if proven else "自述 / 课程", "exact": exact, "level": level,
            "rank": CONTEXT_RANK.get(context, 1), "where": where, "context": context, "matched": matched, "text": text}


# ---------- 门槛和加分 ----------

def _hard(profile, req):
    """返回 (确定不符合的门槛, 待确认的事项)。推断出来的事实碰到门槛只进待确认。"""
    fails, unsure = [], []
    need = req["门槛"]["学历"]
    highest = (profile.get("education") or {}).get("highest")
    if need in DEGREES:
        if not highest or highest["degree"] not in DEGREES:
            unsure.append(f"学历没写清楚（要求{need}）")
        elif DEGREES.index(highest["degree"]) < DEGREES.index(need):
            (fails if highest["certain"] else unsure).append(
                f"学历{highest['degree']}{'' if highest['certain'] else '（推断）'}，要求{need}")
        elif not highest["certain"]:
            unsure.append(f"学历没写明（推断为{highest['degree']}）")
    for c in req.get("补充条件", []):
        if not c["必须"]:
            continue
        met = _extra_met(c, profile)
        if met is False:
            fails.append(f"{c['类型']}要求「{c['值']}」，简历不符合")
        elif met is None:
            unsure.append(f"{c['类型']}要求「{c['值']}」，简历未体现")
    for p in profile.get("practice", {}).get("experiences", []):
        if p["kind"] == "全职工作":
            unsure.append(f"有全职工作经历（{p['name']}），是否为应届毕业生")
            break
    return fails, unsure


def _extra_met(c, profile):
    """补充条件是否满足：True / False（确定不满足）/ None（简历没写，待确认）。"""
    stages = (profile.get("education") or {}).get("stages", [])
    if c["类型"] == "院校":
        limit = min(v for k, v in SCHOOL_LEVEL.items() if k in c["值"])
        levels = [s["tier_level"] for s in stages if s.get("tier_level") is not None]
        return any(l <= limit for l in levels) if levels else None
    if c["类型"] == "专业":
        return any(_major_hit(c["值"], s) for s in stages) if stages else None
    if c["类型"] == "实习":
        work = [e for e in profile.get("practice", {}).get("experiences", []) if e["kind"] in ("实习", "全职工作")]
        if c["值"] == "有":
            return True if work else None
        hit = any(c["值"] in (e["title"] or "") + e["name"] + "".join(d["text"] for d in e.get("duties", [])) for e in work)
        return True if hit else None
    if c["类型"] == "证书":
        # 用户决定（2026-10-01）：有证书的人几乎都会写，简历里没写要求的证书就算不符合
        return _cert_hit(c["值"], profile)
    return None


def _bonus(profile, req, units, kb, broad):
    """加分项：只列有证据的，队列内排序用。"""
    out = []
    for r in req["要求"]:
        if r["必须"] or r["基础要求"] or r["名称"] in req["最看重"]:
            continue
        a = _ability(r, profile["evidence"], units, kb, broad)
        if a["grade"] != "没体现":
            out.append(f"{r['名称']}（{a['grade']}）")
    stages = (profile.get("education") or {}).get("stages", [])
    majors = [m for m in req["专业"]["要求"] if any(_major_hit(m, s) for s in stages)]
    if majors:
        out.append("专业对口：" + "、".join(majors))
    certs = [c for c in req["证书"]["要求"] if _cert_hit(c, profile)]
    if certs:
        out.append("证书：" + "、".join(certs))
    for c in req.get("补充条件", []):
        if not c["必须"] and _extra_met(c, profile):
            out.append(f"{c['类型']}：{c['值']}")
    return out


def _major_hit(major, stage):
    a, b = _squash(major), _squash(stage.get("major") or "")
    return bool(a and b) and (a in b or b in a or a in _squash(stage.get("major_class") or ""))


def cert_key(cert):
    """大学英语四六级的常见写法统一成标准名称："CET6""cet-6""六级""大学英语六级" → 英语六级。"""
    text = _squash(cert).upper()
    if re.fullmatch(r"(大学)?(英语)?(CET-?6|六级)(证书|考试)?", text):
        return "英语六级"
    if re.fullmatch(r"(大学)?(英语)?(CET-?4|四级)(证书|考试)?", text):
        return "英语四级"
    return cert


def _cert_hit(cert, profile):
    words = CERT_COVERS.get(cert_key(cert), (cert,))
    names = [e["name"] + e.get("text", "") for e in profile["evidence"] if e["kind"] == "证书"]
    return any(_squash(w) in _squash(n) for w in words for n in names)


# ---------- 电话问题和提示 ----------

def _questions(profile, abilities, unsure):
    """先问事实（学历、是否应届），再问最看重但只有自述、或没体现的能力；最多 3 个。"""
    qs = [f"{u}？" for u in unsure]
    for a in abilities:
        if a["grade"] == "自述 / 课程":
            qs.append(f"「{a['name']}」只在{a['context'] or '技能栏'}写到，请举一个实际用过的例子")
        elif a["grade"] == "没体现":
            qs.append(f"简历没提到「{a['name']}」，是否用过？")
    return qs[:MAX_QUESTIONS]


def _hints(results):
    hints = []
    ranked = [r for r in results if r["队列"] in ("优先看", "值得看", "可以后看")]
    for q in ("优先看", "值得看"):
        n = sum(r["队列"] == q for r in ranked)
        if len(ranked) >= 6 and n * 2 > len(ranked):
            hints.append(f"「{q}」里有 {n} / {len(ranked)} 人，这份岗位要求区分度低，建议结合专业、加分项再看，或调整最看重的项")
    return hints


def _squash(s):
    return re.sub(r"\s+", "", s or "").lower()

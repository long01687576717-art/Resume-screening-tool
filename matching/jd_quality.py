"""高质量 JD 判定：一份 JD 只有能代表"这一层次的公司对这个岗位的真实要求"时，才进入企业层次画像的统计。

不打总分：4 道门槛（全部通过才算高质量）+ 2 个标记（只记录，不排除）。
    1 完整      岗位职责、任职要求都有，且各自 ≥3 条或 ≥80 字
    2 具体要求  任职要求里 ≥3 项能在简历上核对的具体要求（技能、工具、专业知识、证书、程度）；纯软素质不算
    3 职责具体  一半以上的职责写的是具体工作，不是"完成领导交办的其他工作"这类空话
    4 真实单一  一条 JD 只写一个岗位；内容相同的只算一份（批量去重）；公司能定到企业层次
    标记 5 分清必须 / 加分：写了"优先""加分""更佳"之类
    标记 6 要求与职责一致：任职要求涉及的能力领域，职责里也出现

在 AI 提取之前，门槛 2、3 和标记 6 用能力词典（knowledge/skill_domains.json）+ 证书正则近似判断，
局限见 docs/企业层次JD方案.md。都是纯函数，同一份 JD 结果永远相同。

自检：python -m matching.jd_quality          （检查 samples/jd/*.txt）
"""
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------- 阈值（由作者决定，改这里即可） ----------
MIN_ITEMS = 3            # 门槛 1：每部分至少 3 条……
MIN_CHARS = 80           # ……或至少 80 字
MIN_CONCRETE = 3         # 门槛 2：至少 3 项具体要求
DUTY_SPECIFIC_SHARE = 0.5  # 门槛 3：具体职责要超过这个比例（"大多数"）
VAGUE_SHARE = 0.2        # 与 matching/jd.py 相同：在 ≥20% 的 JD 里出现的词是泛词
SHORT_DUTY = 12          # 门槛 3：去掉编号后不到 12 字、又没有具体词的职责算空泛
CONSISTENT_SHARE = 0.5   # 标记 6：要求里的领域有一半以上在职责里出现，算一致
TIERS = ("头部平台", "知名平台", "一般平台")

# 软素质领域：词典里的"协调""组织"等多出现在"沟通协调能力强"这类软要求里，不算具体要求
SOFT_DOMAINS = {"协同组织"}
# 证书、资格、语言等级（能在简历上核对）
CERTIFICATE = re.compile(
    r"(CPA|ACCA|CFA|FRM|CMA|CIIA|PMP|CET-?[46]|TEM-?[48]|雅思|托福|IELTS|TOEFL|"
    r"(英语|日语|韩语|德语|法语)?(四|六|专四|专八)级|N[12]|"
    r"[一-龥]{2,10}(资格证书?|执业证书?|从业资格|资格证|证书)|注册[一-龥]{2,8}师|驾照|驾驶证)", re.IGNORECASE)
# 英文工具、技术名（Python、SQL、Linux……）；排除常见英文虚词
ASCII_TOOL = re.compile(r"(?<![A-Za-z])[A-Za-z][A-Za-z0-9+#./]{1,20}(?<![./])")
ASCII_STOP = {"and", "or", "the", "of", "to", "in", "for", "with", "etc", "is", "a", "an", "on", "hr"}
# 空泛职责
GENERIC_DUTY = re.compile(r"领导交办|上级交办|交办的其他|其他(相关)?工作|其他临时|临时性工作|服从(公司|工作|领导)?(安排|分配)|"
                          r"公司安排的|领导安排的|完成(部门|公司)?(下达|交代)的")
# 必须 / 加分
PLUS = re.compile(r"优先|加分|更佳|者佳|为佳|更好|bonus|plus", re.IGNORECASE)
# 一条职位名称写了多个岗位（与 tools/crawl_jd.py 相同）
MULTI_ROLE = re.compile(r"(岗|师|员|经理|专员)[^、/／]{0,12}[、/／][^、/／]{0,12}(岗|师|员|经理|专员)")
# 正文里出现两段以上"岗位职责"，或"岗位一 / 岗位二"，多半是几个岗位写在一起
MULTI_BODY = re.compile(r"岗位[一二三四五1-5][：:、]|职位[一二三四五1-5][：:、]")
DUTY_HEAD = re.compile(r"(岗位职责|工作职责|职位描述|工作内容|岗位描述|职责描述)")
REQUIREMENT_HEAD = re.compile(r"(任职要求|任职资格|岗位要求|职位要求|应聘条件|招聘条件|资格要求|能力要求|基本要求)[:：】\]]?")
ITEM_SPLIT = re.compile(r"\n+|(?<=[；;。])\s*|\s*(?=(?<![\d.])(?:\d{1,2}[、.．)）]|[（(]\d{1,2}[)）])(?!\d))")
NUMBERING = re.compile(r"^[\s★☆◆◇■□●○•·▪\-*]*(\d{1,2}[、.．)）]|[（(]\d{1,2}[)）])?\s*")
# 小标题行（"★工作内容：""【任职要求】"），不算条目
HEADING = re.compile(r"^[【\[]?(岗位职责|工作职责|任职要求|任职资格|岗位要求|职位要求|职位描述|工作内容|岗位描述|职责描述|"
                     r"具有以下条件者优先|加分项|优先条件|我们希望你|你将负责)[】\]]?[：:]?$")


@dataclass
class Gate:
    passed: bool
    reason: str               # 给人看的一句话
    detail: dict = field(default_factory=dict)


@dataclass
class QualityResult:
    passed: bool
    gates: dict               # 名称 → Gate
    flags: dict               # 名称 → {"value": True / False / None, "reason": ...}

    def failed(self):
        return [name for name, g in self.gates.items() if not g.passed]

    def explain(self):
        head = "通过全部门槛" if self.passed else "未通过：" + "、".join(self.failed())
        lines = [head] + [f"  {'✓' if g.passed else '✗'} {name}：{g.reason}" for name, g in self.gates.items()]
        lines += [f"  · {name}：{f['reason']}" for name, f in self.flags.items()]
        return "\n".join(lines)


# ---------- 词典 ----------

_WORDS = None
_GENERAL = set()  # 通用办公的词（Office、Word……）：几乎每份 JD 都写，不算具体要求


def _dictionary():
    """(关键词, 领域, 正则)，长词优先；不含通用办公（general）领域。"""
    global _WORDS
    if _WORDS is None:
        data = json.loads((ROOT / "knowledge" / "skill_domains.json").read_text(encoding="utf-8"))
        data.pop("说明", None)
        words = []
        for domains in data.values():
            for domain, value in domains.items():
                if isinstance(value, dict) and value.get("general"):
                    _GENERAL.update(w.lower() for w in value["keywords"])
                    continue
                for w in value["keywords"] if isinstance(value, dict) else value:
                    pattern = rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])" if w.isascii() else re.escape(w)
                    words.append((w, domain, re.compile(pattern, re.IGNORECASE)))
        _WORDS = sorted(words, key=lambda x: -len(x[0]))
    return _WORDS


_COMMON = None


def _common_words():
    global _COMMON
    if _COMMON is None:
        path = ROOT / "knowledge" / "common_words.json"
        _COMMON = json.loads(path.read_text(encoding="utf-8"))["words"] if path.exists() else {}
    return _COMMON


def dictionary_hits(text):
    """文字里命中的词典关键词 {词: 领域}；长词优先，已被长词覆盖的位置不再算短词（"数据分析"不再拆出"分析"）。"""
    text = text or ""
    taken, hits = [], {}
    for word, domain, regex in _dictionary():
        for m in regex.finditer(text):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            taken.append(m.span())
            hits[word.lower()] = domain
    return hits


# ---------- 拆分 ----------

def split_sections(text):
    """整段 JD → (职责, 要求)：按"任职要求"之类的小标题拆开；找不到时全部算职责（与 tools/crawl_jd.py 相同）。"""
    m = REQUIREMENT_HEAD.search(text or "")
    duty, req = ((text[:m.start()], text[m.end():]) if m else (text or "", ""))
    return DUTY_HEAD.sub("", duty, count=1).strip(" ：:\n"), req.strip(" ：:\n")


def split_items(text):
    """按换行、分号、句号、编号拆成条目；去掉小标题和编号，丢掉空条目。"""
    items = []
    for part in ITEM_SPLIT.split(text or ""):
        part = NUMBERING.sub("", (part or "").strip())
        if HEADING.match(part):
            continue
        part = re.sub(r"^(岗位职责|工作职责|任职要求|任职资格|岗位要求|职位要求|职位描述|工作内容)[：:]", "", part).strip()
        if len(part) >= 2:
            items.append(part)
    return items


def concrete_requirements(requirement):
    """任职要求里能在简历上核对的具体要求（去重）：词典关键词（软素质领域除外）、证书、英文工具名。"""
    found = {w for w, d in dictionary_hits(requirement).items() if d not in SOFT_DOMAINS}
    found |= {m.group(0) for m in CERTIFICATE.finditer(requirement or "")}
    dict_ascii = {w for w in found if w.isascii()}
    for m in ASCII_TOOL.finditer(requirement or ""):
        word = m.group(0).lower()
        if word not in ASCII_STOP and word not in _GENERAL and word not in dict_ascii and not re.fullmatch(r"[a-z]", word):
            found.add(word)
    return sorted(found)


def duty_is_vague(item):
    """空泛职责：写了"领导交办""服从安排"；或很短、又没有具体词（词典词 / 英文名 / 非泛词的两字词占多数）。"""
    if GENERIC_DUTY.search(item):
        return True
    if dictionary_hits(item) or ASCII_TOOL.search(item):
        return False
    chinese = re.sub(r"[^一-龥]", "", item)
    if len(chinese) < SHORT_DUTY:
        return True
    # 两字词里泛词（出现在 ≥20% JD 里）超过一半，也算空泛（"负责公司日常工作的管理和协调"）
    common = _common_words()
    grams = [chinese[i:i + 2] for i in range(len(chinese) - 1)]
    vague = sum(common.get(g, 0) >= VAGUE_SHARE for g in grams)
    return bool(common) and vague * 2 > len(grams)


def content_key(duty, requirement):
    """内容去重键（与 tools/crawl_jd.py 相同：职责 + 要求的 md5）。"""
    return hashlib.md5(f"{duty}|{requirement}".encode()).hexdigest()


# ---------- 门槛和标记 ----------

def _section_ok(text):
    items, chars = split_items(text), len(re.sub(r"\s", "", text or ""))
    return (len(items) >= MIN_ITEMS or chars >= MIN_CHARS), len(items), chars


def check(title, duty, requirement, tier=None, duplicate=False):
    """检查一份 JD。tier：公司层次（KnowledgeBase().match_company(公司).tier）；None 表示不检查公司（自检用）。
    duplicate：内容与已收录的 JD 相同（由 check_batch 批量判断）。"""
    duty, requirement = duty or "", requirement or ""
    gates = {}

    ok_d, n_d, c_d = _section_ok(duty)
    ok_r, n_r, c_r = _section_ok(requirement)
    missing = [name for name, text in (("岗位职责", duty), ("任职要求", requirement)) if not text.strip()]
    if missing:
        reason = f"缺少{'、'.join(missing)}"
    elif not (ok_d and ok_r):
        reason = "；".join(f"{name}只有 {n} 条、{c} 字" for name, ok, n, c in
                          (("岗位职责", ok_d, n_d, c_d), ("任职要求", ok_r, n_r, c_r)) if not ok)
    else:
        reason = f"岗位职责 {n_d} 条 {c_d} 字，任职要求 {n_r} 条 {c_r} 字"
    gates["完整"] = Gate(not missing and ok_d and ok_r, reason, {"duty_items": n_d, "requirement_items": n_r})

    concrete = concrete_requirements(requirement)
    gates["具体要求"] = Gate(len(concrete) >= MIN_CONCRETE,
                         f"具体要求 {len(concrete)} 项（需 ≥{MIN_CONCRETE}）" + (f"：{'、'.join(concrete[:8])}" if concrete else ""),
                         {"concrete": concrete})

    duties = split_items(duty)
    vague = [d for d in duties if duty_is_vague(d)]
    share = (len(duties) - len(vague)) / len(duties) if duties else 0.0
    gates["职责具体"] = Gate(bool(duties) and share > DUTY_SPECIFIC_SHARE,
                         f"{len(duties) - len(vague)} / {len(duties)} 条职责具体" +
                         (f"；空泛：{'｜'.join(v[:20] for v in vague[:3])}" if vague else ""),
                         {"vague": vague, "specific_share": round(share, 2)})

    problems = []
    if MULTI_ROLE.search(title or ""):
        problems.append("职位名称写了多个岗位")
    if MULTI_BODY.search(duty + requirement) or len(DUTY_HEAD.findall(duty + requirement)) >= 2:
        problems.append("正文像是几个岗位写在一起")
    if duplicate:
        problems.append("与已收录的 JD 内容相同")
    if tier is not None and tier not in TIERS:
        problems.append(f"公司定不到企业层次（{tier or '未知'}）")
    gates["真实单一"] = Gate(not problems, "；".join(problems) if problems else
                         ("一个岗位、内容不重复" + (f"，{tier}" if tier else "，未检查公司层次")), {"tier": tier})

    flags = {}
    plus = PLUS.search(requirement)
    flags["分清必须/加分"] = {"value": bool(plus), "reason": f"写了\"{plus.group(0)}\"" if plus else "没有区分必须和加分"}
    req_domains = {d for d in dictionary_hits(requirement).values() if d not in SOFT_DOMAINS}
    duty_domains = set(dictionary_hits(duty).values())
    if req_domains:
        overlap = req_domains & duty_domains
        value = len(overlap) / len(req_domains) >= CONSISTENT_SHARE
        missing_d = sorted(req_domains - duty_domains)
        flags["要求与职责一致"] = {"value": value, "reason": f"要求涉及 {len(req_domains)} 个领域，职责里出现 {len(overlap)} 个"
                                                           + (f"（职责里没有：{'、'.join(missing_d)}）" if missing_d else "")}
    else:
        flags["要求与职责一致"] = {"value": None, "reason": "要求里没有识别出能力领域，无法判断"}

    return QualityResult(all(g.passed for g in gates.values()), gates, flags)


def check_batch(rows, kb=None):
    """批量检查（行格式同 tools/crawl_jd.py：job_name、company、duty、requirement，可带 tier）。
    内容相同的只有第一份不算重复。返回 [(行, QualityResult)]。"""
    seen, out = set(), []
    for row in rows:
        tier = row.get("tier")
        if tier is None and kb is not None and row.get("company"):
            found = kb.match_company(row["company"])
            tier = found.tier if found else ""
        key = content_key(row.get("duty", ""), row.get("requirement", ""))
        duplicate = key in seen
        seen.add(key)
        out.append((row, check(row.get("job_name", ""), row.get("duty", ""), row.get("requirement", ""), tier, duplicate)))
    return out


def _self_check():
    files = sorted((ROOT / "samples" / "jd").glob("*.txt"))
    passed = 0
    for path in files:
        text = path.read_text(encoding="utf-8")
        title, _, body = text.partition("\n")
        duty, req = split_sections(body)
        result = check(title.strip(), duty, req)
        passed += result.passed
        print(f"【{path.stem}】{result.explain()}\n")
    print(f"共 {len(files)} 份，通过 {passed} 份（未检查公司层次：样例 JD 没有写公司）")


if __name__ == "__main__":
    _self_check()

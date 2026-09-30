"""JD 要求的提取：构建通用岗位画像（tools/build_job_profiles.py）和企业导入 JD（matching/jd.py）共用，
保证两边的提取方法完全相同、结果可以比较。提示词改动会让已有缓存失效。"""
import re

# JD 里常见的素质要求；前五项能从简历判断（对应素质画像），其余只能面试考察
QUALITIES = ["责任心", "主动性", "影响力", "学习成长", "严谨细致", "沟通表达", "团队协作", "抗压能力", "创新能力", "执行力", "逻辑思维"]
RESUME_JUDGEABLE = {"责任心", "主动性", "影响力", "学习成长", "严谨细致"}
LEVELS = ("了解", "熟悉", "掌握", "熟练", "精通")

PROMPT = """你是招聘信息提取助手。从用户给出的一份岗位描述（JD）中提取对候选人的要求，只输出 JSON。

规则：
1. 只提取 JD 中明确写出的要求，不要推测、不要补充常识。原文没写的字段填 null 或 []。
2. hard：硬性要求。degree：学历要求原文；majors：专业要求，逐个列出原文；certificates：证书要求（如"CPA""英语四级"）；
   campus：原文是否写明面向应届生 / 校招 / 实习生（true / false）。
3. skills：工具和技术（如 Excel、Python、SQL、CAD、PS），逐个列出：
   name：原文名称；level：原文的程度词，只能是 了解、熟悉、掌握、熟练、精通、null；required：原文写"优先""加分""者优先"的填 false，否则 true；
   domain：从下面的领域中选一个：{domains}
4. business：业务能力和专业知识（如"财务报表分析""供应链管理流程""会计准则""用户增长"），逐个列出：name、level、required、domain（规则同上）。
5. qualities：素质和软能力要求，逐个列出：text：原文；quality：只能从下面选一个：{qualities}、其他。
6. 不要把岗位职责（"负责……"）当成要求；职责里没写要求的，skills 和 business 可以为空。

输出格式：
{{"hard": {{"degree": "本科及以上", "majors": ["统计学"], "certificates": ["英语四级"], "campus": true}},
 "skills": [{{"name": "Excel", "level": "熟练", "required": true, "domain": "数据处理与分析"}}],
 "business": [{{"name": "财务报表分析", "level": "熟悉", "required": false, "domain": "财务会计"}}],
 "qualities": [{{"text": "沟通表达能力强", "quality": "沟通表达"}}]}}"""



def prompt(kb):
    return PROMPT.format(domains="、".join(kb.skill_domain_names), qualities="、".join(QUALITIES))


def clean(data, text, kb):
    """核对原文：技能名、业务能力名必须在 JD 原文里找得到；归类先查词典，查不到用 AI 选的领域并标注。"""
    body = re.sub(r"\s+", "", text).lower()

    def items(key):
        out = []
        for raw in data.get(key) or []:
            if not isinstance(raw, dict) or not raw.get("name"):
                continue
            name = str(raw["name"]).strip()
            if re.sub(r"\s+", "", name).lower() not in body:
                continue
            # "要求统计学、数据分析等相关专业"里的"数据分析"是专业，不是技能（flash 偶尔会这样提取）
            if re.sub(r"\s+", "", name).lower() in majors:
                continue
            found = kb.match_skill_domain(name)
            domain = found[1] if found else (raw.get("domain") if kb.skill_layer(raw.get("domain") or "") else "其他")
            level = raw.get("level") if raw.get("level") in LEVELS else ""
            out.append({"name": name, "level": level, "required": raw.get("required") is not False,
                        "domain": domain, "in_dictionary": bool(found)})
        return out

    hard = data.get("hard") or {}
    majors = {re.sub(r"\s+", "", m).lower() for m in hard.get("majors") or [] if isinstance(m, str)}
    qualities = [q for q in data.get("qualities") or [] if isinstance(q, dict) and q.get("quality") in QUALITIES]
    return {
        "hard": {"degree": hard.get("degree") if isinstance(hard.get("degree"), str) else "",
                 "majors": [m for m in hard.get("majors") or [] if isinstance(m, str)],
                 "certificates": [c for c in hard.get("certificates") or [] if isinstance(c, str)],
                 "campus": hard.get("campus") is True},
        "skills": items("skills"), "business": items("business"),
        "qualities": [q["quality"] for q in qualities],
        "quality_texts": {q["quality"]: str(q.get("text") or "") for q in qualities},
    }

"""筛选结果的文字：队列报告、每个人的理由、CSV。命令行（screen.py）和网页（app.py）共用。"""
import csv
import io

from matching import match
from modules.experience import LEVEL_NAMES

NOTE = "说明：队列只决定阅读顺序，不代表录用建议；没写的能力只是排在后面，不等于不具备。"
COMPLIANCE = "合规：性别、年龄、民族、籍贯、婚育、政治面貌未作为筛选条件。"
RANKED = ("优先看", "值得看", "可以后看")
CSV_HEADER = ["队列", "简历", "做过类似的事", "最看重的能力", "加分项", "硬条件不符", "电话初筛问题", "解析异常"]


def text_report(req, results, hints, failed):
    lines = [f"岗位：{req['岗位']}　最看重：{'、'.join(req['最看重']) or '（未填）'}　学历门槛：{req['门槛']['学历']}", ""]
    lines += [f"提示：{h}" for h in hints]
    for queue in match.QUEUES:
        group = [r for r in results if r["队列"] == queue]
        if not group:
            continue
        lines += ["", f"【{queue}】{len(group)} 人" + ("（按证据强弱排列）" if queue in RANKED else "")]
        for r in group:
            lines.append(f"· {r['文件']}")
            lines += [f"    {line}" for line in reasons(r)]
    if failed:
        lines += ["", "【无法分析】"] + [f"· {f}" for f in failed]
    lines += ["", NOTE, COMPLIANCE]
    return "\n".join(lines)


def reasons(r):
    out = []
    if r["解析"]:
        out.append("解析异常：" + "；".join(r["解析"]) + "，请直接打开原文件看")
    out += [f"硬条件不符：{f}" for f in r["硬条件不符"]]
    for m in r["做过类似的事"]:
        out.append(f"做过类似的事：职责「{short(m['duty'], 24)}」↔ {m['where']}（{m['context']}·{LEVEL_NAMES[m['level']]}）"
                   f"“{short(m['text'], 40)}”（共同：{'、'.join(m['hits'])}）")
    if r["最看重"]:
        out.append("最看重：" + "；".join(ability_text(a) for a in r["最看重"]))
    if r["加分"]:
        out.append("加分：" + "、".join(r["加分"]))
    out += [f"电话初筛可以问：{q}" for q in r["电话问题"]]
    return out


def ability_text(a):
    if a["grade"] == "没体现":
        return f"{a['name']} 没体现"
    # 按领域对上的，给出原句让 HR 自己判断
    similar = "" if a["exact"] else (f"，相近：“{short(a['text'], 30)}”" if a["matched"] == "同领域原句" else f"，相近：{a['matched']}")
    depth = f"·{LEVEL_NAMES[a['level']]}" if a["grade"] == "实证" and a["level"] in LEVEL_NAMES else ""
    return f"{a['name']} {a['grade']}（{a['where']}·{a['context'] or '未注明'}{depth}{similar}）"


def csv_text(results):
    """返回 CSV 文字；保存时用 utf-8-sig 编码，Excel 直接打开不乱码。"""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_HEADER)
    for r in results:
        w.writerow([r["队列"], r["文件"],
                    "\n".join(f"{m['where']}（{LEVEL_NAMES[m['level']]}）：{short(m['text'], 40)}（共同：{'、'.join(m['hits'])}）"
                              for m in r["做过类似的事"]),
                    "\n".join(ability_text(a) for a in r["最看重"]),
                    "、".join(r["加分"]), "；".join(r["硬条件不符"]), "\n".join(r["电话问题"]), "；".join(r["解析"])])
    return buf.getvalue()


def short(text, n):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n] + "…"


# ---------- 总览排序（不需要 JD） ----------

OVERVIEW_COLUMNS = ("学校层次", "学历", "学业成绩", "学业走势", "实习经历", "项目经历", "技能", "通用素质", "证书荣誉", "岗位匹配")
LAYER_RULE = "只有在所选每一项上都不比别人差、且至少一项更好，才排在前面；否则各有所长，放在同一层，层内不分先后"


def overview_text(ranked, failed, abnormal, dims):
    lines = [f"分层依据：{'、'.join(dims) or '（未选）'}", LAYER_RULE]
    for r in ranked:
        lines += ["", f"【第 {r['层']} 层】{r['文件']}", f"    为什么：{r['为什么']}"]
        lines += [f"    {c}：{r[c]}" for c in OVERVIEW_COLUMNS if r.get(c)]
        if r["待确认"]:
            lines.append(f"    待确认：{'；'.join(r['待确认'])}")
    if failed:
        lines += ["", "【硬性要求不符】"] + [f"· {r['文件']}：{'；'.join(r['不符'])}" for r in failed]
    if abnormal:
        lines += ["", "【需人工查看】"] + [f"· {r['文件']}：{'；'.join(r['解析异常'])}" for r in abnormal]
    lines += ["", "说明：档位来自简历里写明的证据，没写 ≠ 不会；通用素质是证据强度，不代表素质高低。", COMPLIANCE]
    return "\n".join(lines)


def overview_csv(ranked, failed, abnormal):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["层", "简历", "为什么", *OVERVIEW_COLUMNS, "待确认", "硬性要求不符", "解析异常"])
    for r in ranked + failed + abnormal:
        w.writerow([r.get("层", ""), r["文件"], r.get("为什么", ""), *[r.get(c, "") for c in OVERVIEW_COLUMNS],
                    "；".join(r["待确认"]), "；".join(r["不符"]), "；".join(r["解析异常"])])
    return buf.getvalue()

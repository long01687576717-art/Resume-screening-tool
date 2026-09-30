"""企业层次岗位画像：同一岗位类别，头部 / 知名 / 一般平台分别要求什么。

    python tools/build_tier_profiles.py --dry-run       # 只做质量检查和计数，不调用 AI（不需要 API Key）
    python tools/build_tier_profiles.py                  # 提取并统计，生成 knowledge/job_profiles_tiers.json（默认 deepseek-flash）

流程：
1. 合并两份 JD：data/jd_raw/ncss_jd.jsonl（24365，tools/crawl_jd.py）和 data/company_jd/company_jd.jsonl（大公司官网，
   tools/crawl_company_jd.py）；按内容去重（先到先留，24365 在前）；24365 的公司用 KnowledgeBase().match_company 定层次
2. 高质量 JD 门槛（matching/jd_quality.py）：不通过的不参与统计
3. 每个（岗位类别 × 企业层次）至少 MIN_JD 份高质量 JD 才生成画像，否则标"样本不足"，不凑数
4. 只对够数的格子调用 AI 提取：提示词和 tools/build_job_profiles.py 完全相同（matching/jd_extract.py，不改），
   输入文字的拼法也相同，所以 24365 已提取过的 JD 直接用缓存；统计方法复用 build_job_profiles.aggregate

输出：knowledge/job_profiles_tiers.json（画像，只含统计比例和技能名，不含 JD 原文）；
data/company_jd/quality.jsonl（每份 JD 的门槛结果，本地查看用）。
"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from knowledge.kb import KnowledgeBase  # noqa: E402
from matching.jd_quality import TIERS, check_batch  # noqa: E402

NCSS = ROOT / "data" / "jd_raw" / "ncss_jd.jsonl"
COMPANY = ROOT / "data" / "company_jd" / "company_jd.jsonl"
QUALITY = ROOT / "data" / "company_jd" / "quality.jsonl"
OUTPUT = ROOT / "knowledge" / "job_profiles_tiers.json"
MIN_JD = 15  # 一个（岗位 × 层次）至少 15 份高质量 JD 才出画像


def load(path, source):
    if not path.exists():
        print(f"没有找到 {path}，跳过")
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for r in rows:
        r.setdefault("source", source)
    return rows


def main():
    parser = argparse.ArgumentParser(description="按企业层次统计岗位画像")
    parser.add_argument("--dry-run", action="store_true", help="只做质量检查和计数，不调用 AI")
    parser.add_argument("--ncss", default=str(NCSS), help="24365 JD 文件")
    parser.add_argument("--company", default=str(COMPANY), help="大公司官网 JD 文件")
    parser.add_argument("--model", default="deepseek-flash", help="提取用的模型，默认 deepseek-flash")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    kb = KnowledgeBase()
    rows = load(Path(args.ncss), "24365") + load(Path(args.company), "官网")
    for r in rows:
        if not r.get("tier"):
            found = kb.match_company(r.get("company", ""))
            r["tier"], r["tier_basis"] = (found.tier, found.basis) if found else ("", "")

    checked = check_batch(rows, kb)
    QUALITY.parent.mkdir(parents=True, exist_ok=True)
    QUALITY.write_text("".join(json.dumps({"source": r["source"], "category": r["category"], "company": r.get("company", ""),
                                           "job_name": r.get("job_name", ""), "tier": r["tier"], "passed": q.passed,
                                           "failed": q.failed(), "reasons": {k: g.reason for k, g in q.gates.items()},
                                           "flags": {k: f["value"] for k, f in q.flags.items()}}, ensure_ascii=False) + "\n"
                               for r, q in checked), encoding="utf-8")

    _report(checked)

    cells = defaultdict(list)
    for r, q in checked:
        if q.passed and r["tier"] in TIERS:
            cells[(r["category"], r["tier"])].append(r)
    if args.dry_run:
        print(f"\n--dry-run：没有调用 AI。每份 JD 的门槛结果在 {QUALITY}")
        return

    from llm.client import LLMClient  # 只在真正提取时才需要 Key
    from tools.build_job_profiles import aggregate, extract_all, remap

    llm = LLMClient(use_cache=True, cache="jd")
    llm.model = args.model  # 只影响这个脚本
    enough = [r for key, rs in cells.items() if len(rs) >= MIN_JD for r in rs]
    print(f"\n用 {llm.model} 提取 {len(enough)} 份高质量 JD（已提取过的走缓存）……")
    results = remap(extract_all(enough, kb, llm, args.workers), kb)

    by_cell = defaultdict(list)
    for r in results:
        by_cell[(r["category"], r["tier"])].append({**r, "category": r["category"]})
    profiles = defaultdict(dict)
    all_categories = {r["category"] for r, _ in checked}
    for category in sorted(all_categories):
        for tier in TIERS:
            n = len(cells.get((category, tier), []))
            if n < MIN_JD:
                profiles[category][tier] = {"status": "样本不足", "high_quality_jd": n, "need": MIN_JD}
                continue
            prof = aggregate(by_cell[(category, tier)]).get(category)
            companies = Counter(r.get("company", "") for r in cells[(category, tier)])
            profiles[category][tier] = {"status": "已生成", "high_quality_jd": n, "companies": len(companies),
                                        "top_company_share": round(companies.most_common(1)[0][1] / n, 2), **(prof or {})}
    OUTPUT.write_text(json.dumps({"说明": "企业层次岗位画像，由 tools/build_tier_profiles.py 生成：同一岗位类别下，头部 / 知名 / 一般平台"
                                         "分别统计（企业层次按 knowledge/kb.py 的公司名单）。只用通过 matching/jd_quality.py 门槛的高质量 JD；"
                                         f"不足 {MIN_JD} 份的标\"样本不足\"。share 等字段含义同 job_profiles.json。",
                                  "profiles": profiles}, ensure_ascii=False, indent=1), encoding="utf-8")
    errors = sum("error" in r for r in results)
    print(f"完成 → {OUTPUT}；提取失败 {errors} 份")


def _report(checked):
    """打印：各来源的门槛通过率、各门槛的失败数、（岗位 × 层次）高质量 JD 数。"""
    by_source = defaultdict(Counter)
    for r, q in checked:
        c = by_source[r["source"]]
        c["总数"] += 1
        c["通过"] += q.passed
        for name in q.failed():
            c[name] += 1
    print("质量门槛（未通过数可重叠）：")
    for source, c in by_source.items():
        fails = "，".join(f"{k}未过 {c[k]}" for k in ("完整", "具体要求", "职责具体", "真实单一"))
        print(f"  {source}：{c['总数']} 份，通过 {c['通过']}（{c['通过'] / max(c['总数'], 1):.0%}）；{fails}")

    table = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for r, q in checked:
        cell = table[r["category"]][r["tier"] if r["tier"] in TIERS else "其他"]
        cell[0] += 1
        cell[1] += q.passed
    print(f"\n岗位类别 × 企业层次：高质量 / 全部（≥{MIN_JD} 份高质量才出画像，★ = 够数）")
    print("| 岗位类别 | " + " | ".join(TIERS) + " |")
    print("|---|" + "---|" * len(TIERS))
    for category in sorted(table, key=lambda c: -sum(v[1] for v in table[c].values())):
        cells = [table[category].get(t, [0, 0]) for t in TIERS]
        print(f"| {category} | " + " | ".join(f"{'★' if p >= MIN_JD else ''}{p} / {n}" for n, p in cells) + " |")


if __name__ == "__main__":
    main()

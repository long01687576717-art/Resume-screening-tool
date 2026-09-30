"""用爬到的真实 JD 构建通用岗位画像：AI 逐份提取要求，代码按岗位统计出现频率。

    python tools/build_job_profiles.py                     # 默认用 deepseek-flash
    python tools/build_job_profiles.py --model deepseek-v4-pro

流程：
1. AI 逐份提取：硬性要求、技能、业务能力、素质。技能名必须能在 JD 原文里找到
2. 归类到能力词典（knowledge/skill_domains.json）：先查词典，查不到用 AI 选的领域并标注
3. 按岗位类别统计：一半以上 JD 要求的是"核心要求"，两成到一半是"常见要求"，更少的是个别公司偏好
4. 词典里没有的词单独统计出现次数，用来扩充能力词典

输入 data/jd_raw/ncss_jd.jsonl（tools/crawl_jd.py 生成）；
输出 knowledge/job_profiles.json（岗位画像）、data/jd_raw/jd_extracted.jsonl（逐份提取结果）、
data/jd_raw/unmapped_terms.txt（词典里没有的词）。
"""
import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from knowledge.kb import KnowledgeBase  # noqa: E402
from llm.client import LLMClient  # noqa: E402
from matching.jd_extract import RESUME_JUDGEABLE, clean  # noqa: E402
from matching.jd_extract import prompt as jd_prompt  # noqa: E402

JD_FILE = ROOT / "data" / "jd_raw" / "ncss_jd.jsonl"
EXTRACTED = ROOT / "data" / "jd_raw" / "jd_extracted.jsonl"
UNMAPPED = ROOT / "data" / "jd_raw" / "unmapped_terms.txt"
OUTPUT = ROOT / "knowledge" / "job_profiles.json"
WORDS = ROOT / "knowledge" / "common_words.json"
WORD_MIN_SHARE = 0.05  # 只记录在 5% 以上 JD 里出现的词：不在表里的就是"具体词"

CORE, COMMON = 0.5, 0.2  # 核心要求：一半以上 JD 提到；常见要求：两成以上

def extract_all(rows, kb, llm, workers):
    prompt = jd_prompt(kb)

    def one(row):
        text = f"职位：{row['job_name']}\n岗位职责：\n{row['duty']}\n任职要求：\n{row['requirement']}"
        try:
            data = llm.extract_json(prompt, text)
        except Exception as e:  # 单份失败不影响整体
            return {**row, "error": str(e)}
        return {**row, **clean(data, text, kb)}

    results = []
    with ThreadPoolExecutor(workers) as pool:
        for n, r in enumerate(pool.map(one, rows), 1):
            results.append(r)
            if n % 100 == 0:
                print(f"  已提取 {n} / {len(rows)}")
    return results


def remap(results, kb):
    """统计前重新查一遍词典：以后扩充了词典，只要重新统计，不用重新提取。"""
    for r in results:
        for key in ("skills", "business"):
            for item in r.get(key, []):
                found = kb.match_skill_domain(item["name"])
                if found:
                    item["domain"], item["in_dictionary"] = found[1], True
    return results


def aggregate(results):
    """按岗位类别统计每项要求出现在多少比例的 JD 里。"""
    profiles = {}
    by_category = defaultdict(list)
    for r in results:
        if "error" not in r:
            by_category[r["category"]].append(r)
    for category, all_rows in by_category.items():
        # 只写了岗位职责、没写任何要求的 JD 不参与统计：没写要求不等于不需要，算进分母会拉低所有占比
        rows = [r for r in all_rows if r["skills"] or r["business"] or r["qualities"] or r["hard"]["majors"]]
        n = len(rows)
        if not n:
            continue
        profile = {"group": rows[0]["group"], "jd_count": len(all_rows), "jd_with_requirements": n,
                   "degree": _share(Counter(r["degree"] or "未写明" for r in rows), n),
                   "majors": _share(Counter(m for r in rows for m in set(r["hard"]["majors"])), n, top=10),
                   "certificates": _share(Counter(c for r in rows for c in set(r["hard"]["certificates"])), n, top=8)}
        for key in ("skills", "business"):
            profile[key] = _requirements(rows, key, n)
        quality_count = Counter(q for r in rows for q in set(r["qualities"]))
        profile["qualities"] = [{"quality": q, "share": round(c / n, 2), "tier": _tier(c / n),
                                 "judge": "简历可判断" if q in RESUME_JUDGEABLE else "需面试考察"}
                                for q, c in quality_count.most_common()]
        profiles[category] = profile
    return profiles


def _requirements(rows, key, n):
    """按领域统计（一份 JD 里同一领域提到多次只算一次），领域下列出最常见的具体技能和程度。"""
    domains = defaultdict(lambda: {"jds": 0, "required": 0, "names": Counter(), "levels": Counter()})
    for r in rows:
        seen = set()
        for item in r[key]:
            d = domains[item["domain"]]
            d["names"][item["name"]] += 1
            if item["level"]:
                d["levels"][item["level"]] += 1
            if item["domain"] not in seen:
                seen.add(item["domain"])
                d["jds"] += 1
                d["required"] += item["required"]
    out = []
    for domain, d in sorted(domains.items(), key=lambda x: -x[1]["jds"]):
        share = d["jds"] / n
        out.append({"domain": domain, "share": round(share, 2), "tier": _tier(share),
                    "required_share": round(d["required"] / d["jds"], 2),
                    "typical_level": d["levels"].most_common(1)[0][0] if d["levels"] else "",
                    "top": [name for name, _ in d["names"].most_common(6)]})
    return out


def _tier(share):
    return "核心" if share >= CORE else "常见" if share >= COMMON else "个别"


def _share(counter, n, top=None):
    return [{"value": v, "share": round(c / n, 2)} for v, c in counter.most_common(top)]


def common_words(rows):
    """统计 2～4 个字的词在多少比例的 JD 里出现，用来判断 JD 职责关键词是泛词、常见词还是具体词。
    只保存词和比例，不保存 JD 原文。"""
    df = Counter()
    for r in rows:
        text = (r["duty"] + " " + r["requirement"]).lower()
        # 中文按 2～4 个字切；英文按整词统计（"熟练使用Excel"里的 excel 不能被切成 exce / xcel）
        chinese = re.sub(r"[^一-龥]+", " ", text).split()
        grams = {s[i:i + n] for s in chinese for n in (2, 3, 4) for i in range(len(s) - n + 1)}
        grams |= set(re.findall(r"[a-z][a-z0-9+#.]*[a-z0-9+#]|[a-z]", text))
        df.update(grams)
    words = {w: round(c / len(rows), 3) for w, c in df.items() if c / len(rows) >= WORD_MIN_SHARE}
    WORDS.write_text(json.dumps({"说明": f"由 tools/build_job_profiles.py 统计：{len(rows)} 份真实校招 JD 中，2～4 字的词出现在多少比例的 JD 里"
                                          f"（只记录 ≥{WORD_MIN_SHARE:.0%} 的）", "words": dict(sorted(words.items(), key=lambda x: -x[1]))},
                                ensure_ascii=False, indent=0), encoding="utf-8")
    return words


def main():
    parser = argparse.ArgumentParser(description="用真实 JD 构建通用岗位画像")
    parser.add_argument("--model", default="deepseek-flash", help="提取用的模型，默认 deepseek-flash")
    parser.add_argument("--workers", type=int, default=8, help="同时提取的数量")
    parser.add_argument("--limit", type=int, help="每个岗位只取前 N 份（试跑用）")
    args = parser.parse_args()

    rows = [json.loads(line) for line in JD_FILE.read_text(encoding="utf-8").splitlines()]
    if args.limit:
        counts = Counter()
        rows = [r for r in rows if (counts.update([r["category"]]) or counts[r["category"]] <= args.limit)]
    if not args.limit:
        print(f"常见词表：{len(common_words(rows))} 个 → {WORDS}")
    kb, llm = KnowledgeBase(), LLMClient(use_cache=True, cache="jd")
    llm.model = args.model  # 只影响这个脚本，不改 .env 里分析简历用的模型
    print(f"用 {llm.model} 提取 {len(rows)} 份 JD……")
    results = extract_all(rows, kb, llm, args.workers)
    EXTRACTED.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in results), encoding="utf-8")

    profiles = aggregate(remap(results, kb))
    OUTPUT.write_text(json.dumps({"说明": "通用岗位画像，由 tools/build_job_profiles.py 根据国家大学生就业服务平台的真实校招 JD 统计生成。"
                                         "share = 提到这项要求的 JD 占比；tier：核心 ≥50%，常见 ≥20%，个别 <20%。",
                                  "profiles": profiles}, ensure_ascii=False, indent=1), encoding="utf-8")
    unmapped = Counter(i["name"] for r in results if "error" not in r for key in ("skills", "business")
                       for i in r[key] if not i["in_dictionary"])
    UNMAPPED.write_text("".join(f"{c}\t{name}\n" for name, c in unmapped.most_common()), encoding="utf-8")
    errors = sum("error" in r for r in results)
    print(f"完成：{len(profiles)} 个岗位画像 → {OUTPUT}；提取失败 {errors} 份；词典外的词 {len(unmapped)} 个 → {UNMAPPED}")


if __name__ == "__main__":
    main()

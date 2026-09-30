"""生成网页"看演示"用的数据：虚构简历的分析结果 + 一份确认好的岗位要求，存到 demo/。

演示模式不需要 API Key、不调用 AI：直接读这里的结果。
只用虚构简历（samples/、samples/test/dev/）；分析规则改了以后要重新运行本脚本。

用法：python tools/build_demo.py
"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main as analyzer  # noqa: E402
from knowledge.kb import KnowledgeBase  # noqa: E402
from llm.client import LLMClient  # noqa: E402
from matching import jd  # noqa: E402
from report.formatter import format_report  # noqa: E402

DEMO_DIR = ROOT / "demo"
SOURCES = ("samples", "samples/test/dev")   # 检验集 holdout 不放进演示，留作最终检验
DEMO_JOB = ROOT / "jobs" / "数据分析工程师.json"


def analyze(path, context):
    results, profile = analyzer.run_modules(path, context)
    note = profile["parse"]["source"] if profile["parse"]["source"] != "文字" else None
    return {"name": path.name, "profile": profile, "note": note,
            "results": [{"title": r.title, "items": r.items, "notes": r.notes} for r in results],
            "report": format_report(path.name, results, note)}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    kb = KnowledgeBase()
    context = {"kb": kb, "llm": LLMClient(), "job_city": None}
    files = [p for d in SOURCES for p in sorted((ROOT / d).iterdir()) if p.suffix.lower() in analyzer.SUPPORTED_SUFFIXES]
    with ThreadPoolExecutor(analyzer.MAX_PARALLEL_RESUMES) as pool:
        analyses = list(pool.map(lambda p: analyze(p, context), files))
    job, warnings = jd.load(DEMO_JOB, kb)
    if warnings:
        sys.exit("演示用岗位要求有看不懂的行：" + "；".join(warnings))
    DEMO_DIR.mkdir(exist_ok=True)
    (DEMO_DIR / "analyses.json").write_text(json.dumps(analyses, ensure_ascii=False, indent=1), encoding="utf-8")
    (DEMO_DIR / "job.json").write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已生成演示数据：{len(analyses)} 份虚构简历 + 岗位要求「{job['岗位']}」→ {DEMO_DIR}")


if __name__ == "__main__":
    main()

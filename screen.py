"""简历筛选：帮 HR 决定先看哪些简历。

用法：
    python screen.py 导入 岗位JD.txt [--title 职位名称]
        解析 JD，生成 jobs/职位名称.json（工具的原始理解）和 jobs/职位名称.txt（给 HR 看和改）
    python screen.py 检查 jobs/职位名称.txt
        读取 HR 改过的文字文件，显示工具的理解和看不懂的行
    python screen.py 筛选 jobs/职位名称.txt 简历文件夹 [更多文件夹或文件]
        按岗位要求把简历分进队列（优先看 / 值得看 / 可以后看 / 硬条件不符 / 需人工查看），写明理由，
        同时生成 jobs/职位名称_筛选结果.csv（Excel 可直接打开）
    python screen.py 总览 简历文件夹 [--排序 实习经历,项目经历,技能] [--学校 第一] [--最低学历 本科] [--院校 "211 / 双一流及以上"]
                       [--专业 统计,计算机] [--证书 英语六级] [--岗位 jobs/职位名称.txt]
        不导入 JD 也能用：在所选各项上全面不差才排在前面，各有所长的放同一层；硬性要求只筛选不排序
"""
import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from openai import OpenAIError

import main as analyzer

from knowledge.kb import KnowledgeBase
from llm.client import LLMClient
from matching import jd, match, overview
from report import screening

JOBS_DIR = Path(__file__).resolve().parent / "jobs"


def import_jd(args):
    path = Path(args.file)
    text = path.read_text(encoding="utf-8-sig")
    title = args.title or next((line.strip() for line in text.splitlines() if line.strip()), path.stem)
    requirement = jd.parse(text, title, KnowledgeBase(), LLMClient(cache="jd"))
    JOBS_DIR.mkdir(exist_ok=True)
    out = JOBS_DIR / f"{_safe(title)}.json"
    jd.save(requirement, out)
    txt = out.with_suffix(".txt")
    txt.write_text(jd.to_text(requirement, txt.relative_to(JOBS_DIR.parent).as_posix()), encoding="utf-8")
    print(jd.summary(requirement))
    print(f"\n以上是工具对 JD 的理解。确认无误可以直接筛选；需要修改请打开 {txt}")


def check_job(args):
    path = Path(args.file)
    if not path.with_suffix(".json").exists():
        print(f"找不到 {path.with_suffix('.json')}，请先用「导入」命令解析 JD")
        return
    requirement, warnings = jd.load(path, KnowledgeBase())
    print(jd.summary(requirement))
    if warnings:
        print("\n需要注意：")
        print("\n".join(f"- {w}" for w in warnings))
    else:
        print("\n文件没有问题。")


def screen_resumes(args):
    path = Path(args.job)
    if not path.with_suffix(".json").exists():
        print(f"找不到 {path.with_suffix('.json')}，请先用「导入」命令解析 JD")
        return
    kb = KnowledgeBase()
    req, warnings = jd.load(path, kb)
    if warnings:
        print("岗位要求文件有看不懂的地方（已跳过），可运行「检查」命令查看：")
        print("\n".join(f"- {w}" for w in warnings) + "\n")
    context = {"kb": kb, "llm": LLMClient(), "job_city": None}
    files = analyzer.collect_files(args.resumes)
    failed = []

    def profile_of(f):
        try:
            return analyzer.run_modules(f, context)[1]
        except (OSError, ValueError, OpenAIError) as e:
            failed.append(f"{f.name}：{e}")
            return None

    print(f"正在分析 {len(files)} 份简历……\n")
    with ThreadPoolExecutor(max(1, args.workers)) as pool:
        profiles = [p for p in pool.map(profile_of, files) if p]
    results, hints = match.screen(profiles, req, kb)
    print(screening.text_report(req, results, hints, failed))
    out = path.with_name(f"{path.stem}_筛选结果.csv")
    out.write_text(screening.csv_text(results), encoding="utf-8-sig", newline="")
    print(f"\n结果表格：{out}")


def overview_resumes(args):
    kb = KnowledgeBase()
    job = jd.load(Path(args.岗位), kb)[0] if args.岗位 else None
    context = {"kb": kb, "llm": LLMClient(), "job_city": None}
    files = analyzer.collect_files(args.resumes)
    with ThreadPoolExecutor(max(1, args.workers)) as pool:
        profiles = list(pool.map(lambda f: analyzer.run_modules(f, context)[1], files))
    dims = [d.strip() for d in args.排序.split(",") if d.strip()]
    unknown = [d for d in dims if d not in overview.DIMENSIONS]
    if unknown:
        print(f"不认识的维度：{'、'.join(unknown)}（可选：{'、'.join(overview.DIMENSIONS)}）")
    filters = {"最低学历": args.最低学历, "院校": args.院校,
               "专业": [w for w in (args.专业 or "").split(",") if w], "证书": [c for c in (args.证书 or "").split(",") if c]}
    mode = overview.SCHOOL_MODES[1] if args.学校 == "第一" else overview.SCHOOL_MODES[0]
    ranked, failed, abnormal = overview.rank(profiles, dims, mode, filters, job, kb)
    print(screening.overview_text(ranked, failed, abnormal, [d for d in dims if d in overview.DIMENSIONS]))


def _safe(name):
    return "".join(ch for ch in name if ch not in '\\/:*?"<>|').strip()[:40] or "岗位"


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="简历筛选：帮 HR 决定先看哪些简历")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("导入", help="解析 JD，生成可修改的岗位要求文件")
    p.add_argument("file", help="JD 文本文件")
    p.add_argument("--title", help="职位名称，不写时取文件第一行")
    p.set_defaults(func=import_jd)
    p = sub.add_parser("检查", help="读取 HR 改过的岗位要求文件，显示工具的理解")
    p.add_argument("file", help="jobs/ 下的 .txt 文件")
    p.set_defaults(func=check_job)
    p = sub.add_parser("筛选", help="按岗位要求给一批简历分队列，写明理由")
    p.add_argument("job", help="jobs/ 下的 .txt 文件")
    p.add_argument("resumes", nargs="+", help="简历文件或文件夹")
    p.add_argument("--workers", type=int, default=5, help="同时分析的简历数量，默认 5")
    p.set_defaults(func=screen_resumes)
    p = sub.add_parser("总览", help="不导入 JD：按勾选的维度给简历排序")
    p.add_argument("resumes", nargs="+", help="简历文件或文件夹")
    p.add_argument("--排序", default=",".join(overview.DEFAULT_DIMENSIONS), help=f"分层依据，用逗号分开，建议 2～3 项，可选：{'、'.join(overview.DIMENSIONS)}")
    p.add_argument("--学校", choices=["最高", "第一"], default="最高", help="学校层次先看最高学历还是第一学历（另一段在相同时比较）")
    p.add_argument("--最低学历", choices=list(overview.DEGREES))
    p.add_argument("--院校", choices=list(overview.SCHOOL_LIMITS))
    p.add_argument("--专业", help="专业需包含的字，用逗号分开")
    p.add_argument("--证书", help="用逗号分开")
    p.add_argument("--岗位", help="jobs/ 下的 .txt 文件；给了才能用「岗位匹配」维度")
    p.add_argument("--workers", type=int, default=5)
    p.set_defaults(func=overview_resumes)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

"""简历分析工具入口。

用法：
    python main.py 简历.pdf
    python main.py 简历文件夹/ --city 深圳
    python main.py 简历.docx --show-extracted   # 同时输出 AI 提取的原始数据，便于核查
"""
import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from openai import AuthenticationError, OpenAIError

from knowledge.kb import KnowledgeBase
from matching.profile import build as build_profile
from llm.client import LLMClient
from modules.base import Resume
from modules.basic_info import BasicInfoModule
from modules.campus import CampusModule
from modules.education import EducationModule
from modules.honors import HonorsModule
from modules.internship import InternshipModule
from modules.project_skill import ProjectSkillModule
from modules.quality import QualityModule
from parser.file_reader import SUPPORTED_SUFFIXES, read_file
from parser.privacy import mask_personal_info
from report.formatter import format_report

# 报告中模块的显示顺序；新增模块在这里注册
MODULES = [BasicInfoModule(), EducationModule(), HonorsModule(), InternshipModule(), CampusModule(),
           ProjectSkillModule(), QualityModule()]
# 同时处理的简历数量；AI 调用互不依赖，并行可以大幅缩短总耗时，数量过多可能触发 API 频率限制
MAX_PARALLEL_RESUMES = 4


def collect_files(paths):
    files = []
    for p in map(Path, paths):
        if p.is_dir():
            files.extend(sorted(f for f in p.iterdir() if f.suffix.lower() in SUPPORTED_SUFFIXES))
        else:
            files.append(p)
    return files


def run_modules(path, context):
    """分析一份简历：返回 (各模块结果, 候选人画像)。筛选（screen.py）也用这个函数。"""
    text, source_note = read_file(path)
    # 手机号、邮箱、身份证号不参与分析，发送给 AI 前先脱敏
    resume = Resume(path=path, text=mask_personal_info(text), source_note=source_note)
    # 各模块互不依赖，同时调用 AI
    with ThreadPoolExecutor(len(MODULES)) as pool:
        results = list(pool.map(lambda module: module.analyze(resume, context), MODULES))
    return results, build_profile(path, resume.text, source_note, results)


def analyze(path, context, show_extracted):
    results, profile = run_modules(path, context)
    report = format_report(path.name, results, profile["parse"]["source"] if profile["parse"]["source"] != "文字" else None)
    if show_extracted:
        extracted = {r.title: r.extracted for r in results}
        report += "\n\n【AI 提取的原始数据】\n" + json.dumps(extracted, ensure_ascii=False, indent=2)
    return report


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="简历分析工具")
    parser.add_argument("paths", nargs="+", help="简历文件（PDF / DOCX / TXT）或文件夹，可传多个")
    parser.add_argument("--city", help="应聘地点，用于籍贯提醒")
    parser.add_argument("--no-cache", action="store_true", help="不使用缓存，重新调用 AI")
    parser.add_argument("--show-extracted", action="store_true", help="同时输出 AI 提取的原始数据")
    parser.add_argument("--workers", type=int, default=MAX_PARALLEL_RESUMES, help=f"同时处理的简历数量，默认 {MAX_PARALLEL_RESUMES}")
    args = parser.parse_args()

    try:
        llm = LLMClient(use_cache=not args.no_cache)
    except RuntimeError as e:
        sys.exit(f"错误：{e}")
    context = {"kb": KnowledgeBase(), "llm": llm, "job_city": args.city}

    def run(path):
        try:
            return analyze(path, context, args.show_extracted)
        except (OSError, ValueError) as e:
            return f"[{path.name}] 无法分析：{e}"
        except AuthenticationError:
            raise  # Key 无效时所有简历都会失败，交给外层直接退出
        except OpenAIError as e:
            return f"[{path.name}] 调用 AI 失败：{e}"

    # 多份简历同时处理，报告仍按文件顺序输出
    try:
        with ThreadPoolExecutor(max(1, args.workers)) as pool:
            for report in pool.map(run, collect_files(args.paths)):
                print(report)
                print()
    except AuthenticationError:
        sys.exit("错误：API Key 无效，请运行 python setup_key.py 重新填写")


if __name__ == "__main__":
    main()

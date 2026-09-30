"""从软科中国大学排名下载近 3 年数据，给非双一流院校划分梯队，生成 knowledge/rankings.json。

软科每年更新一次排名，每年运行一次即可：
    python tools/update_rankings.py              # 默认使用今年及前两年
    python tools/update_rankings.py --latest 2026

分档规则：
- 主榜（综合、理工、师范、农业、林业类公办院校）：按近 3 年平均名次，每 100 名一个梯队，
  500 名以后为第六梯队（软科从 2025 年起只公布前 500 名的具体名次）。
- 行业院校（财经、政法、医药等只有类别内名次）：按在本类别非双一流院校中的比例位置，
  对应到主榜非双一流院校中同一比例位置的梯队，并标注为"折算"。
- 中外合作办学：不划梯队，只记录名次。
- 民办院校：统一归入"基础"档，同时记录民办榜名次。
双一流院校不录入，它们在 schools.json 中已有档位。
"""
import argparse
import datetime
import json
import subprocess
import time
from collections import defaultdict
from pathlib import Path

API = "https://www.shanghairanking.cn/api/pub/v1/bcur?bcur_type={type}&year={year}"
OUTPUT = Path(__file__).resolve().parent.parent / "knowledge" / "rankings.json"
DOWNLOAD_DIR = Path(__file__).resolve().parent / ".rankings_download"  # 已下载的榜单，重新运行时直接复用
MAIN_LIST = 11
CATEGORY_LISTS = {21: "医药", 22: "财经", 23: "语言", 24: "民族", 25: "政法", 26: "体育", 30: "艺术"}
JOINT_LIST = 14
PRIVATE_LISTS = {15: "民办", 16: "民办财经类", 17: "民办语言类"}
UNPUBLISHED_RANK = 501  # "500+" 按 501 计算
TIER_NAMES = ["", "双非第一梯队", "双非第二梯队", "双非第三梯队", "双非第四梯队", "双非第五梯队", "双非第六梯队"]


def fetch(list_type, year, retries=5):
    saved = DOWNLOAD_DIR / f"{year}_{list_type}.json"
    if saved.exists():
        return json.loads(saved.read_text(encoding="utf-8"))

    # 软科服务器会断开 Python 自带网络库（urllib / httpx）的加密连接，所以改用系统自带的 curl 下载
    url = API.format(type=list_type, year=year)
    for attempt in range(1, retries + 1):
        proc = subprocess.run(["curl", "-s", "--max-time", "30", "-A", "Mozilla/5.0", url], capture_output=True)
        try:
            rankings = json.loads(proc.stdout.decode("utf-8"))["data"]["rankings"]
        except (ValueError, KeyError, TypeError):
            if attempt == retries:
                raise RuntimeError(f"下载失败：{url}（curl 退出码 {proc.returncode}），请稍后重新运行，已下载的部分会保留")
            time.sleep(3 * attempt)  # 可能触发了访问频率限制，逐步延长等待
            continue
        DOWNLOAD_DIR.mkdir(exist_ok=True)
        saved.write_text(json.dumps(rankings, ensure_ascii=False), encoding="utf-8")
        time.sleep(1)  # 控制请求频率
        return rankings


def collect(list_type, years):
    """返回 {学校: {"ranks": {年份: 名次}, "tags": [...]}}，只保留非双一流院校。"""
    schools = defaultdict(lambda: {"ranks": {}, "tags": []})
    for year in years:
        for x in fetch(list_type, year):
            if "双一流" in x["univTags"]:
                continue
            rank = x["ranking"]
            if not rank:
                continue
            school = schools[x["univNameCn"]]
            school["ranks"][year] = UNPUBLISHED_RANK if rank.endswith("+") else int(rank)
            school["tags"] = x["univTags"]
    return schools


def average(ranks):
    return sum(ranks.values()) / len(ranks)


def main_tier(avg_rank):
    return min(int((avg_rank - 1) // 100) + 1, 6)


def rank_text(avg_rank):
    return "500 名以后" if avg_rank >= UNPUBLISHED_RANK else f"第 {round(avg_rank)} 名"


def build(years):
    result = {}

    # 1. 主榜：按平均名次直接分档
    main = collect(MAIN_LIST, years)
    main_sorted = sorted(main.items(), key=lambda item: average(item[1]["ranks"]))
    for name, data in main_sorted:
        avg_rank = average(data["ranks"])
        tier = main_tier(avg_rank)
        result[name] = {"tier": TIER_NAMES[tier], "tier_level": tier,
                        "label": f"软科主榜近 {len(data['ranks'])} 年平均{rank_text(avg_rank)}"}
    main_tiers = [main_tier(average(data["ranks"])) for _, data in main_sorted]

    # 2. 行业院校：按比例位置对应到主榜梯队
    latest = max(years)
    for list_type, category in CATEGORY_LISTS.items():
        schools = collect(list_type, years)
        ordered = sorted(schools.items(), key=lambda item: average(item[1]["ranks"]))
        latest_ranks = {x["univNameCn"]: x["ranking"] for x in fetch(list_type, latest)}
        list_size = len(latest_ranks)
        for position, (name, data) in enumerate(ordered, start=1):
            if name in result:
                continue
            ratio = (position - 0.5) / len(ordered)
            tier = main_tiers[min(int(ratio * len(main_tiers)), len(main_tiers) - 1)]
            current = latest_ranks.get(name)
            source = f"{category}类第 {current}/{list_size} 名" if current else f"{category}类"
            result[name] = {"tier": TIER_NAMES[tier], "tier_level": tier, "label": f"按{source}折算"}

    # 3. 中外合作办学：只记录名次
    for x in fetch(JOINT_LIST, latest):
        result.setdefault(x["univNameCn"], {"tier": "中外合作办学", "tier_level": None,
                                             "label": f"软科合作办学榜第 {x['ranking']} 名"})

    # 4. 民办院校：归入基础档
    for list_type, list_name in PRIVATE_LISTS.items():
        for x in fetch(list_type, latest):
            result.setdefault(x["univNameCn"], {"tier": "基础", "tier_level": None,
                                                 "label": f"民办院校，软科{list_name}榜第 {x['ranking']} 名"})
    return result


def main():
    parser = argparse.ArgumentParser(description="更新软科排名梯队数据")
    parser.add_argument("--latest", type=int, default=datetime.date.today().year, help="最新一年的排名年份")
    args = parser.parse_args()
    years = [args.latest - 2, args.latest - 1, args.latest]

    schools = build(years)
    OUTPUT.write_text(json.dumps({
        "说明": "由 tools/update_rankings.py 自动生成，请勿手工修改。数据来源：软科中国大学排名（shanghairanking.cn）。",
        "years": years,
        "schools": schools,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    counts = defaultdict(int)
    for s in schools.values():
        counts[s["tier"]] += 1
    print(f"已生成 {OUTPUT}（{years[0]}～{years[-1]} 年，共 {len(schools)} 所）")
    for tier in TIER_NAMES[1:] + ["中外合作办学", "基础"]:
        print(f"  {tier}：{counts[tier]} 所")


if __name__ == "__main__":
    main()

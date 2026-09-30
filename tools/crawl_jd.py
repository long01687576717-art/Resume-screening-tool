"""从国家大学生就业服务平台（24365，教育部主办）收集校招 JD，用于构建通用岗位知识库。

    python tools/crawl_jd.py                 # 全部岗位类别
    python tools/crawl_jd.py --only 数据分析  # 只爬一个类别（试爬用）

来源与规则：
- 24365 没有 robots.txt 限制，岗位列表是公开接口，详情是普通网页，不需要登录，没有反爬
- 控制频率：全局每秒最多 3 次请求；不登录、不绕过任何验证
- 只保存 JD 内容（职位、单位、学历、专业、岗位职责、任职资格），去掉电话、邮箱
- 原始数据只存本地 data/jd_raw/（已加入 .gitignore），只用于统计分析，不公开
- 每个请求的结果都缓存到本地，中断后重新运行会从断点继续
"""
import argparse
import hashlib
import json
import re
import subprocess
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "jd_raw" / "ncss"
OUTPUT = ROOT / "data" / "jd_raw" / "ncss_jd.jsonl"
CATEGORIES = ROOT / "knowledge" / "job_categories.json"

LIST_API = ("https://www.ncss.cn/student/jobs/jobslist/ajax/?jobType=&areaCode=&jobName={kw}&monthPay=&industrySectors="
            "&recruitType=&property=&categoryCode=&memberLevel=&keyUnits=&degreeCode=&sourcesName=&sourcesType="
            "&offset={page}&limit=20")
DETAIL_PAGE = "https://www.ncss.cn/student/jobs/{job_id}/detail.html"
MAX_PAGES = 10          # 接口每个关键词最多返回 10 页 × 20 条
TARGET = 60             # 每个岗位类别收集 60 份内容完整的 JD 就停
MIN_TEXT = 80           # 岗位职责 + 任职资格少于 80 字的算内容不完整
MAX_PER_COMPANY = 3     # 同一单位在同一岗位类别里最多收几份
WORKERS = 3
MIN_INTERVAL = 1 / 3    # 全局每秒最多 3 次请求

PHONE = re.compile(r"1[3-9]\d{9}|0\d{2,3}-?\d{7,8}")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")


class RateLimiter:
    """所有线程共用：两次请求之间至少间隔 MIN_INTERVAL 秒。"""

    def __init__(self, interval):
        self.interval, self.lock, self.next_time = interval, threading.Lock(), 0.0

    def wait(self):
        with self.lock:
            now = time.time()
            delay = max(0.0, self.next_time - now)
            self.next_time = max(now, self.next_time) + self.interval
        time.sleep(delay)


limiter = RateLimiter(MIN_INTERVAL)


def fetch(url, cache_file, retries=3):
    """下载并缓存；已缓存的直接读本地，不再请求。"""
    if cache_file.exists():
        return cache_file.read_text(encoding="utf-8")
    for attempt in range(1, retries + 1):
        limiter.wait()
        proc = subprocess.run(["curl", "-4", "-sL", "--compressed", "--max-time", "30", "-A", "Mozilla/5.0",
                               "-H", "Referer: https://www.ncss.cn/student/jobs/index.html", url], capture_output=True)
        text = proc.stdout.decode("utf-8", errors="ignore")
        if proc.returncode == 0 and text:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(text, encoding="utf-8")
            return text
        time.sleep(2 * attempt)
    return ""


def search(keyword):
    """一个关键词的全部搜索结果（最多 200 条），按接口返回的顺序。"""
    jobs = []
    for page in range(1, MAX_PAGES + 1):
        cache = RAW_DIR / "list" / f"{_safe(keyword)}_{page}.json"
        try:
            data = json.loads(fetch(LIST_API.format(kw=quote(keyword), page=page), cache)).get("data") or {}
        except ValueError:
            cache.unlink(missing_ok=True)
            break
        items = data.get("list") or []
        jobs += items
        if len(items) < 20:
            break
    return jobs


def detail(job):
    """职位详情有两种写法：
    - 大多数是普通网页文字，在"职位详情"和"扫码快速投递简历"之间，职责和要求写在一起
    - 少数是一段 JSON：gwzz = 岗位职责，rzzg = 任职资格"""
    job_id = job["jobId"]
    html = fetch(DETAIL_PAGE.format(job_id=job_id), RAW_DIR / "detail" / f"{job_id}.html")
    start = html.find('{"gwzz"')
    if start >= 0:
        try:
            info, _ = json.JSONDecoder().raw_decode(html[start:])
            duty, requirement = info.get("gwzz"), info.get("rzzg")
        except ValueError:
            return None
    else:
        m = re.search(r"职位详情(.*?)扫码快速投递简历", html, re.S)
        if not m:
            return None
        duty, requirement = _split(_html_text(m.group(1)))
    industry = re.search(r"所属行业\s*</[^>]+>\s*<[^>]+>\s*([^<]+)", html)
    return {
        "job_id": job_id,
        "job_name": job.get("jobName") or "",
        "company": job.get("recName") or "",
        "company_type": job.get("recProperty") or "",
        "degree": job.get("degreeName") or "",
        "major": job.get("major") or "",
        "area": job.get("areaCodeName") or "",
        "publish_date": time.strftime("%Y-%m-%d", time.localtime((job.get("publishDate") or 0) / 1000)),
        "source_school": job.get("sourcesNameCh") or "",
        "industry": industry.group(1).strip() if industry else "",
        "duty": _clean(duty),
        "requirement": _clean(requirement),
    }


REQUIREMENT_HEAD = re.compile(r"(任职要求|任职资格|岗位要求|职位要求|应聘条件|招聘条件|资格要求|能力要求|基本要求)[:：】\]]?")


def _split(text):
    """把"岗位职责……任职要求……"拆成两部分；找不到"要求"的小标题时全部算职责（AI 提取时会再分）。"""
    m = REQUIREMENT_HEAD.search(text)
    return (text[:m.start()], text[m.end():]) if m else (text, "")


def _html_text(fragment):
    fragment = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", fragment, flags=re.I)
    text = re.sub(r"<[^>]+>", "", fragment)
    text = text.replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return re.sub(r"\n\s*\n+", "\n", re.sub(r"[ \t]+", " ", text)).strip()


# 一条职位名称写了多个岗位（"数据分析岗、营销企划岗、产品市场岗"）：要求是几个岗位共用的，会稀释岗位特点
MULTI_ROLE = re.compile(r"(岗|师|员|经理|专员)[^、/／]{0,12}[、/／][^、/／]{0,12}(岗|师|员|经理|专员)")


def crawl_category(group, category, spec):
    """spec：keywords 搜索关键词；title 职位名称里应该出现的词（正则）。
    名称里没有这些词的职位（如"银行"搜到的"安保专员"）跳过，继续往后找，保证分类干净。"""
    title = re.compile(spec["title"], re.IGNORECASE)  # "Gis数据处理工程师"也要算
    exclude = re.compile(spec["exclude"], re.IGNORECASE) if spec.get("exclude") else None
    seen_ids, seen_content, results = set(), set(), []
    per_company = Counter()
    candidates, skipped = [], Counter()
    for keyword in spec["keywords"]:
        for job in search(keyword):
            if not job.get("jobId") or job["jobId"] in seen_ids:
                continue
            seen_ids.add(job["jobId"])
            name = job.get("jobName") or ""
            if not title.search(name):
                skipped["名称不相关"] += 1
                continue
            if exclude and exclude.search(name):
                skipped["属于别的岗位"] += 1
                continue
            if MULTI_ROLE.search(name):
                skipped["一条写了多个岗位"] += 1
                continue
            candidates.append((keyword, job))
    # 按列表顺序分批取详情，凑够 TARGET 份内容完整的就停，少发请求
    with ThreadPoolExecutor(WORKERS) as pool:
        for i in range(0, len(candidates), WORKERS * 4):
            batch = candidates[i:i + WORKERS * 4]
            for (keyword, _), jd in zip(batch, pool.map(lambda c: detail(c[1]), batch)):
                if not jd or len(jd["duty"]) + len(jd["requirement"]) < MIN_TEXT:
                    continue
                # 内容相同的只留一份，不管挂在哪家单位（同一岗位常被多所高校转发；
                # 从实习僧转来的 JD 还有同一段文字挂在很多公司名下的情况）
                key = hashlib.md5(f"{jd['duty']}|{jd['requirement']}".encode()).hexdigest()
                if key in seen_content:
                    continue
                # 同一单位最多 3 份：大量招聘的公司（如某公司 25 个开发岗）会让统计变成"这家公司的要求"
                if per_company[jd["company"]] >= MAX_PER_COMPANY:
                    continue
                seen_content.add(key)
                per_company[jd["company"]] += 1
                results.append({"group": group, "category": category, "keyword": keyword, **jd})
            if len(results) >= TARGET:
                break
    return results[:TARGET], len(seen_ids), skipped


def main():
    parser = argparse.ArgumentParser(description="从 24365 收集校招 JD")
    parser.add_argument("--only", help="只爬这个岗位类别（试爬用）")
    args = parser.parse_args()
    groups = json.loads(CATEGORIES.read_text(encoding="utf-8"))["categories"]

    existing = {}
    if OUTPUT.exists():
        for line in OUTPUT.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            existing.setdefault(row["category"], []).append(row)

    started = time.time()
    for group, categories in groups.items():
        for category, spec in categories.items():
            if args.only and category != args.only:
                continue
            rows, found, skipped = crawl_category(group, category, spec)
            existing[category] = rows
            skip_text = "，".join(f"{k} {v}" for k, v in skipped.items())
            print(f"[{time.time() - started:5.0f} 秒] {group} / {category}：搜索到 {found} 个职位"
                  f"{'（跳过：' + skip_text + '）' if skip_text else ''}，保留 {len(rows)} 份完整 JD")
            OUTPUT.parent.mkdir(parents=True, exist_ok=True)
            OUTPUT.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for rows_ in existing.values() for r in rows_),
                              encoding="utf-8")
    total = sum(len(v) for v in existing.values())
    print(f"完成：共 {total} 份 JD，保存在 {OUTPUT}")


def _clean(text):
    """统一换行，去掉电话和邮箱。"""
    text = (text or "").replace("\r", "").strip()
    return EMAIL.sub("[邮箱]", PHONE.sub("[电话]", text))


def _safe(name):
    return re.sub(r"[^\w一-龥]+", "_", name)


if __name__ == "__main__":
    main()

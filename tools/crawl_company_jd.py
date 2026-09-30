"""从大公司自己的校招官网（及北森招聘门户）收集校招 JD，用于企业层次画像（头部 / 知名平台）。

    python tools/crawl_company_jd.py                          # 全部来源
    python tools/crawl_company_jd.py --only 腾讯,百度          # 只爬这几家（试爬用）
    python tools/crawl_company_jd.py --category 数据分析       # 只保留这个岗位类别

来源与规则（调查结果见 docs/企业层次JD方案.md）：
- 只用 robots.txt 允许（或没有 robots.txt）、不需要登录、没有签名 / 加密 / 验证码的公开接口；
  接口一旦开始要求签名、验证码或返回 403，就停下来，不绕过
- 全局每秒最多 3 次请求；如实标明是脚本（User-Agent 写明用途），不伪造浏览器指纹
- 只保存 JD 文字（职位、公司、地点、岗位职责、任职要求），去掉电话、邮箱
- 每个请求都缓存到 data/company_jd/cache/，中断后重新运行会从断点继续（已缓存的不再请求）
- 输出 data/company_jd/company_jd.jsonl（data/ 在 .gitignore 里，不上传）

岗位类别：官网一次列出全部校招职位，按 knowledge/job_categories.json 的 title / exclude 规则用职位名称归类；
职位名称里含某类别的搜索关键词（如"前端开发"）的优先归到那一类。
同一段内容只留一份；同一公司在同一岗位类别最多 MAX_PER_COMPANY 份。
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from knowledge.kb import KnowledgeBase  # noqa: E402

OUT_DIR = ROOT / "data" / "company_jd"
CACHE = OUT_DIR / "cache"
OUTPUT = OUT_DIR / "company_jd.jsonl"
CATEGORIES = ROOT / "knowledge" / "job_categories.json"

MIN_INTERVAL = 1 / 3      # 全局每秒最多 3 次请求
MAX_PER_COMPANY = 5       # 同一公司在同一岗位类别最多收几份（24365 用 3；这里公司少，见方案文档"待定"）
MAX_PAGES = 50            # 每个来源最多翻 50 页，防止接口异常时无限翻页
PAGE_SIZE = 20
USER_AGENT = "Mozilla/5.0 (compatible; resume-screening-tool JD research; +https://github.com/long01687576717-art/Resume-screening-tool)"

PHONE = re.compile(r"1[3-9]\d{9}|0\d{2,3}-?\d{7,8}")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
MULTI_ROLE = re.compile(r"(岗|师|员|经理|专员)[^、/／]{0,12}[、/／][^、/／]{0,12}(岗|师|员|经理|专员)")  # 与 crawl_jd.py 相同


class RateLimiter:
    """两次请求之间至少间隔 MIN_INTERVAL 秒（单线程顺序爬，也保留这层保护）。"""

    def __init__(self, interval):
        self.interval, self.lock, self.next_time = interval, threading.Lock(), 0.0

    def wait(self):
        with self.lock:
            now = time.time()
            delay = max(0.0, self.next_time - now)
            self.next_time = max(now, self.next_time) + self.interval
        time.sleep(delay)


limiter = RateLimiter(MIN_INTERVAL)


class Blocked(Exception):
    """来源拒绝访问（403 / 405 / 验证码）：停止这个来源，不绕过。"""


def fetch(url, cache_name, data=None, referer=None, retries=3):
    """GET（data=None）或 POST JSON；返回文字并缓存。已缓存的直接读本地。"""
    cache_file = CACHE / cache_name
    if cache_file.exists():
        return cache_file.read_text(encoding="utf-8")
    cmd = ["curl", "-4", "-sL", "--compressed", "--max-time", "30", "-A", USER_AGENT, "-w", "\n%{http_code}"]
    if referer:
        cmd += ["-H", f"Referer: {referer}"]
    if data is not None:
        cmd += ["-H", "Content-Type: application/json", "-X", "POST", "-d", json.dumps(data, ensure_ascii=False)]
    for attempt in range(1, retries + 1):
        limiter.wait()
        proc = subprocess.run(cmd + [url], capture_output=True)
        text, _, code = proc.stdout.decode("utf-8", errors="ignore").rpartition("\n")
        if code in ("403", "405", "429") or "验证码" in text[:2000] or "captcha" in text[:500].lower():
            raise Blocked(f"{url} 返回 {code}")
        if proc.returncode == 0 and code.startswith("2") and text:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(text, encoding="utf-8")
            return text
        time.sleep(2 * attempt)
    return ""


def fetch_json(url, cache_name, data=None, referer=None):
    text = fetch(url, cache_name, data, referer)
    try:
        return json.loads(text)
    except ValueError:
        (CACHE / cache_name).unlink(missing_ok=True)  # 不是 JSON（出错页）不留缓存
        return {}


# ---------- 各来源：每个函数返回 [{job_id, job_name, area, duty, requirement}] ----------

def tencent(src):
    """腾讯校招 join.qq.com：列表只有职位名，详情接口有 desc（职责）和 request（要求）。projectMappingIdList 1 = 应届生。"""
    jobs = []
    for page in range(1, MAX_PAGES + 1):
        data = fetch_json("https://join.qq.com/api/v1/position/searchPosition", f"tencent/list_{page}.json",
                          {"projectIdList": [], "projectMappingIdList": [1], "keyword": "", "bgList": [], "workCountryType": 0,
                           "workCityList": [], "recruitCityList": [], "positionFidList": [], "pageIndex": page, "pageSize": PAGE_SIZE},
                          referer="https://join.qq.com/post.html")
        items = (data.get("data") or {}).get("positionList") or []
        for it in items:
            d = (fetch_json(f"https://join.qq.com/api/v1/jobDetails/getJobDetailsByPostId?postId={it['postId']}",
                            f"tencent/detail_{it['postId']}.json", referer="https://join.qq.com/post.html").get("data") or {})
            jobs.append({"job_id": it["postId"], "job_name": d.get("title") or it.get("positionTitle") or "",
                         "area": " ".join(d.get("workCityList") or []), "duty": d.get("desc"), "requirement": d.get("request")})
        if len(items) < PAGE_SIZE:
            break
    return jobs


def baidu(src):
    """百度校招 talent.baidu.com：列表直接带 workContent（职责）和 serviceCondition（要求）。recruitType=GRADUATE = 校招。"""
    jobs = []
    for page in range(1, MAX_PAGES + 1):
        cache = CACHE / f"baidu/list_{page}.json"
        if cache.exists():
            text = cache.read_text(encoding="utf-8")
        else:
            limiter.wait()
            proc = subprocess.run(["curl", "-4", "-sL", "--max-time", "30", "-A", USER_AGENT, "-X", "POST",
                                   "-H", "Content-Type: application/x-www-form-urlencoded",
                                   "-H", "Referer: https://talent.baidu.com/jobs/list",
                                   "-d", f"recruitType=GRADUATE&pageSize={PAGE_SIZE}&keyWord=&curPage={page}&projectType=",
                                   "https://talent.baidu.com/httservice/getPostListNew"], capture_output=True)
            text = proc.stdout.decode("utf-8", errors="ignore")
            if '"status":"ok"' in text:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(text, encoding="utf-8")
        try:
            items = (json.loads(text).get("data") or {}).get("list") or []
        except ValueError:
            break
        for it in items:
            jobs.append({"job_id": it.get("postId"), "job_name": re.sub(r"^[^-]{2,4}-|\(J\d+\)$", "", it.get("name") or ""),
                         "area": it.get("workPlace") or "", "duty": it.get("workContent"), "requirement": it.get("serviceCondition")})
        if len(items) < PAGE_SIZE:
            break
    return jobs


def meituan(src):
    """美团校招 zhaopin.meituan.com：列表带 jobDuty / jobRequirement。jobType 1 = 校招（2 实习、3 社招）。"""
    jobs = []
    for page in range(1, MAX_PAGES + 1):
        data = fetch_json("https://zhaopin.meituan.com/api/official/job/getJobList", f"meituan/list_{page}.json",
                          {"page": {"pageNo": page, "pageSize": PAGE_SIZE}, "jobShareType": "1", "keywords": "", "cityList": [],
                           "department": [], "jfJgList": [], "jobType": [{"code": "1", "subCode": []}], "typeCode": [], "specialCode": []},
                          referer="https://zhaopin.meituan.com/web/campus")
        body = data.get("data") or {}
        for it in body.get("list") or []:
            jobs.append({"job_id": it.get("jobUnionId"), "job_name": it.get("name") or "",
                         "area": " ".join(c.get("name") or "" for c in it.get("cityList") or []),
                         "duty": it.get("jobDuty"), "requirement": it.get("jobRequirement")})
        if page >= ((body.get("page") or {}).get("totalPage") or 0):
            break
    return jobs


def jd(src):
    """京东校招 campus.jd.com：列表带 workContent / qualification。type=present 为当前校招。"""
    jobs = []
    for page in range(1, MAX_PAGES + 1):
        data = fetch_json("https://campus.jd.com/api/wx/position/page?type=present", f"jd/list_{page}.json",
                          {"pageSize": PAGE_SIZE, "pageIndex": page, "parameter": {"positionName": "", "planIdList": [],
                           "jobDirectionCodeList": [], "workCityCodeList": [], "positionDeptList": []}},
                          referer="https://campus.jd.com/")
        body = data.get("body") or {}
        items = body.get("items") or []
        for it in items:
            jobs.append({"job_id": it.get("publishId"), "job_name": it.get("positionName") or "", "area": it.get("workCity") or "",
                         "duty": it.get("workContent"), "requirement": it.get("qualification")})
        if not items or page * PAGE_SIZE >= (body.get("totalNumber") or 0):
            break
    return jobs


def netease(src):
    """网易校招：campus.163.com 的"应届生"导航里列出各事业群的校招项目（网易互联网、网易互娱……），项目号每年变，自动读取。
    雷火等跳到其他站点的项目不爬（未调查）。"""
    nav = fetch_json("https://campus.163.com/api/campuspc/project/navigation/list", "netease/navigation.json")
    projects = []
    for group in nav.get("data") or []:
        if group.get("title") != "应届生":
            continue
        for child in group.get("children") or []:
            m = re.match(r"https://(campus(?:\.game)?\.163\.com)/app/job/position\?id=(\d+)", child.get("link") or "")
            if m:
                projects.append((m.group(1), m.group(2)))
    jobs = []
    for host, pid in projects:
        for page in range(1, MAX_PAGES + 1):
            data = fetch_json(f"https://{host}/api/campuspc/position/getJobList?pageNum={page}&pageSize={PAGE_SIZE}&projectId={pid}",
                              f"netease/{host}_{pid}_{page}.json")
            body = data.get("data") or {}
            for it in body.get("list") or []:
                jobs.append({"job_id": f"{pid}_{it.get('id')}", "job_name": it.get("positionName") or "",
                             "area": it.get("workPlaceName") or "", "duty": it.get("positionDescription"),
                             "requirement": it.get("positionRequirement")})
            if page >= (body.get("pages") or 0):
                break
    return jobs


def beisen(src):
    """北森招聘门户 {子域名}.zhiye.com（robots.txt：Allow /）：页面里有 PortalId，列表接口带 Duty / Require。
    每家公司的子域名手工确认后写进 SOURCES。"""
    sub = src["subdomain"]
    page_html = fetch(f"https://{sub}.zhiye.com/campus/jobs", f"beisen/{sub}/portal.html")
    m = re.search(r'PortalId":"([0-9a-f-]{36})', page_html)
    if not m:
        return []
    jobs = []
    for page in range(MAX_PAGES):
        data = fetch_json(f"https://{sub}.zhiye.com/api/Jobad/GetJobAdPageList", f"beisen/{sub}/list_{page}.json",
                          {"PageIndex": page, "PageSize": PAGE_SIZE, "KeyWords": "", "SpecialType": 0, "PortalId": m.group(1)},
                          referer=f"https://{sub}.zhiye.com/campus/jobs")
        items = data.get("Data") or []
        for it in items:
            if it.get("CategoryId") not in (None, "", "2", 2) and "校园" not in (it.get("Category") or ""):
                continue  # 只要校园招聘
            jobs.append({"job_id": it.get("JobAdId"), "job_name": it.get("JobAdName") or "",
                         "area": " ".join(it.get("LocNames") or []), "duty": it.get("Duty"), "requirement": it.get("Require")})
        if (page + 1) * PAGE_SIZE >= (data.get("Count") or 0):
            break
    return jobs


# company：用来定企业层次的名称（KnowledgeBase().match_company），运行时打印定档结果供核对
SOURCES = [
    {"name": "腾讯", "company": "腾讯", "fetch": tencent},
    {"name": "百度", "company": "百度", "fetch": baidu},
    {"name": "美团", "company": "美团", "fetch": meituan},
    {"name": "京东", "company": "京东", "fetch": jd},
    {"name": "网易", "company": "网易", "fetch": netease},
    {"name": "三一集团", "company": "三一集团有限公司", "fetch": beisen, "subdomain": "sany"},
    {"name": "360", "company": "三六零安全科技股份有限公司", "fetch": beisen, "subdomain": "360campus"},
]


# ---------- 归类、清洗、去重 ----------

# 24365 是先按关键词搜索、再用 title 规则过滤，规则可以宽；官网是全部职位直接归类，宽的词会误归
# （"安全工程师"里的"工程"→土木建筑，"用户研究"里的"研究"→金融投资），这两类改用窄规则
NARROW_TITLE = {"土木建筑": "土木|施工|建筑|造价|测量|道路|桥梁|结构设计",
                "金融投资": "投资|证券|金融|基金|理财|信贷|风控|风险|银行"}


def load_specs():
    groups = json.loads(CATEGORIES.read_text(encoding="utf-8"))["categories"]
    return [(g, c, s, re.compile(NARROW_TITLE.get(c, s["title"]), re.IGNORECASE),
             re.compile(s["exclude"], re.IGNORECASE) if s.get("exclude") else None)
            for g, cats in groups.items() for c, s in cats.items()]


def clean_title(title):
    """去掉职位名称里的编号、批次和城市："27秋招-法务专员（北京）-5363(J12444)" → "法务专员"；"北京-后端开发工程师(J100737)" → "后端开发工程师"。"""
    t = re.sub(r"[(（]J?\d+[)）]$", "", (title or "").strip())
    t = re.sub(r"-\d{3,}$", "", t)
    t = re.sub(r"^\d{2,4}(届|秋招|春招|校招)[-－—]?", "", t)
    t = re.sub(r"^[一-龥]{2,3}-(?=[一-龥A-Za-z])", "", t)  # 开头的城市
    t = re.sub(r"[（(](北京|上海|深圳|广州|杭州|成都|武汉|南京|西安|长沙|苏州|天津|重庆)[^）)]*[）)]", "", t)
    return t.strip(" -－—")


def classify(title, specs):
    """职位名称 → (大类, 岗位类别)；名称含搜索关键词的优先，其次按 title 规则、类别顺序。归不进任何类别返回 None。
    比较前去掉"工程师"：它几乎出现在所有技术岗名称里，会被土木建筑的"工程"误中。"""
    if MULTI_ROLE.search(title):
        return None
    probe = title.replace("工程师", "师")
    matched = [(t.search(probe).start(), g, c, s) for g, c, s, t, ex in specs if t.search(probe) and not (ex and ex.search(title))]
    for _, g, c, s in matched:
        if any(k.lower() in title.lower() for k in s["keywords"]):
            return g, c
    # 都不含关键词时，取名称里最靠前的词对应的类别（"测试开发工程师"是测试，不是开发）
    return min(matched, key=lambda m: m[0])[1:3] if matched else None


def _clean(text):
    text = re.sub(r"<br\s*/?>|</p>|</li>|</div>", "\n", text or "", flags=re.I)
    text = re.sub(r"<[^>]+>", "", text).replace("&nbsp;", " ").replace("\r", "")
    text = re.sub(r"^(岗位职责|岗位要求|任职要求|工作职责|职位描述)[：:]?\s*\n", "", text.strip())
    return EMAIL.sub("[邮箱]", PHONE.sub("[电话]", text)).strip()


def main():
    parser = argparse.ArgumentParser(description="从大公司校招官网收集 JD（企业层次画像用）")
    parser.add_argument("--only", help="只爬这些来源，逗号分隔（如 腾讯,百度）")
    parser.add_argument("--category", help="只保留这个岗位类别")
    parser.add_argument("--max-per-company", type=int, default=MAX_PER_COMPANY, help=f"同一公司同一类别最多几份（默认 {MAX_PER_COMPANY}）")
    args = parser.parse_args()
    only = set(args.only.split(",")) if args.only else None

    kb, specs = KnowledgeBase(), load_specs()
    existing = {}
    if OUTPUT.exists():
        for line in OUTPUT.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            existing.setdefault(row["source"], []).append(row)

    started = time.time()
    for src in SOURCES:
        if only and src["name"] not in only:
            continue
        match = kb.match_company(src["company"])
        try:
            raw = src["fetch"](src)
        except Blocked as e:
            print(f"{src['name']}：被拒绝访问，停止（不绕过）：{e}")
            continue
        per_company, seen, rows, skipped = Counter(), set(), [], Counter()
        for job in raw:
            duty, req = _clean(job["duty"]), _clean(job["requirement"])
            job["job_name"] = clean_title(job["job_name"])
            found = classify(job["job_name"], specs)
            if not found:
                skipped["归不进岗位类别"] += 1
                continue
            group, category = found
            if args.category and category != args.category:
                continue
            key = hashlib.md5(f"{duty}|{req}".encode()).hexdigest()
            if key in seen:
                skipped["内容重复"] += 1
                continue
            if per_company[category] >= args.max_per_company:
                skipped["超过每类上限"] += 1
                continue
            seen.add(key)
            per_company[category] += 1
            rows.append({"group": group, "category": category, "source": src["name"], "job_id": str(job["job_id"]),
                         "job_name": job["job_name"], "company": src["company"], "tier": match.tier, "tier_basis": match.basis,
                         "area": job.get("area") or "", "duty": duty, "requirement": req})
        existing[src["name"]] = rows
        skip_text = "，".join(f"{k} {v}" for k, v in skipped.items())
        print(f"[{time.time() - started:5.0f} 秒] {src['name']}（{match.tier}，{match.basis}）：官网 {len(raw)} 个校招职位"
              f"{'（跳过：' + skip_text + '）' if skip_text else ''}，保留 {len(rows)} 份")
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for rs in existing.values() for r in rs), encoding="utf-8")

    table = defaultdict(Counter)
    for rs in existing.values():
        for r in rs:
            table[r["category"]][r["tier"]] += 1
    print("\n岗位类别 × 企业层次（份数）：")
    for category, c in sorted(table.items(), key=lambda x: -sum(x[1].values())):
        print(f"  {category}：" + "，".join(f"{t} {n}" for t, n in c.most_common()))
    print(f"完成：共 {sum(len(v) for v in existing.values())} 份，保存在 {OUTPUT}")


if __name__ == "__main__":
    main()

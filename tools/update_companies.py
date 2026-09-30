"""下载上市公司名单和《财富》500 强榜单，生成 knowledge/companies.json。

每年更新一次即可（财富榜单每年 7 月发布，网址每年不同，用参数传入）：
    python tools/update_companies.py
    python tools/update_companies.py --global500 <世界500强网址> --china500 <中国500强网址>

数据来源：
- A 股：上交所（主板、科创板）、深交所（主板、创业板）官方名单，含公司全称
- 世界 500 强、中国 500 强：财富中文网（中国 500 强包括港股、美股上市的中国公司）
- 独角兽：胡润全球独角兽榜，只保留总部在中国的
民营企业 500 强官网只提供长图，单独整理在 knowledge/private500.json。
北交所官网拦截下载，暂缺；港股名单只有繁体简称，和简历里的全称对不上，未使用。
"""
import argparse
import html
import json
import re
import subprocess
import time
import unicodedata
import zipfile
from pathlib import Path

OUTPUT = Path(__file__).resolve().parent.parent / "knowledge" / "companies.json"
DOWNLOAD_DIR = Path(__file__).resolve().parent / ".companies_download"  # 已下载的文件，重新运行时直接复用

SSE = ("http://query.sse.com.cn/sseQuery/commonQuery.do?sqlId=COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L&isPagination=true"
       "&STOCK_TYPE={type}&pageHelp.pageSize=5000&pageHelp.pageNo=1&pageHelp.beginPage=1&pageHelp.cacheSize=1")
SSE_BOARDS = {"1": "上交所主板", "8": "科创板"}
SZSE = "http://www.szse.cn/api/report/ShowReport?SHOWTYPE=xlsx&CATALOGID=1110&TABKEY=tab1"
GLOBAL500 = "https://www.fortunechina.com/fortune500/c/2026-07/28/content_475298.htm"
CHINA500 = "https://www.caifuzhongwen.com/fortune500/rankings/china500/2026/"
# 胡润榜单接口；num 是榜单编号，每年不同，可在 https://www.hurun.net/zh-CN/Rank/HsRankDetails?pagetype=unicorn 网页源码里找到
UNICORN = "https://www.hurun.net/zh-CN/Rank/HsRankDetailsList?num={num}&search=&offset=0&limit=3000"
UNICORN_NUM = "E9A16F3H"  # 2026 年
ABBR_MARK = re.compile(r"^\*?ST|[AB]$|\s")  # 简称里的 ST、*ST、A/B 股标记


def fetch(url, filename, referer=None, retries=5):
    saved = DOWNLOAD_DIR / filename
    if saved.exists():
        return saved.read_bytes()
    # 交易所服务器会断开 IPv6 和 Python 网络库的连接，用 curl 走 IPv4
    cmd = ["curl", "-4", "-sL", "--max-time", "60", "-A", "Mozilla/5.0", url]
    if referer:
        cmd += ["-H", f"Referer: {referer}"]
    for attempt in range(1, retries + 1):
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode == 0 and len(proc.stdout) > 10000:
            DOWNLOAD_DIR.mkdir(exist_ok=True)
            saved.write_bytes(proc.stdout)
            return proc.stdout
        time.sleep(3 * attempt)
    raise RuntimeError(f"下载失败：{url}（curl 退出码 {proc.returncode}），请稍后重新运行，已下载的部分会保留")


def listed_company(name, abbr):
    """上市公司：全称 + 股票简称（简历里常写"宁德时代""中国平安"这类简称）。"""
    abbr = ABBR_MARK.sub("", unicodedata.normalize("NFKC", abbr or ""))  # 全角"Ａ"转成半角
    return {"name": name, "alias": [abbr] if abbr and abbr != name else []}


def sse_names():
    names = []
    for code, board in SSE_BOARDS.items():
        data = json.loads(fetch(SSE.format(type=code), f"sse_{code}.json", referer="http://www.sse.com.cn/"))
        names += [listed_company(r["FULL_NAME"], r["COMPANY_ABBR"]) for r in data["result"]]
        print(f"{board}：{len(data['result'])} 家")
    return names


def szse_names():
    """深交所名单是 xlsx，表格里的文字直接写在单元格中，不需要额外的库解析。"""
    fetch(SZSE, "szse.xlsx")
    sheet = zipfile.ZipFile(DOWNLOAD_DIR / "szse.xlsx").read("xl/worksheets/sheet1.xml").decode("utf-8")
    rows = [re.findall(r"<t>([^<]*)</t>", r) for r in re.findall(r"<row[^>]*>(.*?)</row>", sheet, re.S)]
    header = rows[0]
    column, abbr = header.index("公司全称"), header.index("A股简称")
    names = [listed_company(r[column], r[abbr]) for r in rows[1:] if len(r) > abbr]
    print(f"深交所：{len(names)} 家")
    return names


def fortune_names(url, filename):
    """财富榜单网页里的表格：第 2 列是公司名，外国公司写作"中文名（英文名)"，拆成两个名字。"""
    page = fetch(url, filename).decode("utf-8", errors="ignore")
    companies = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) < 2 or not cells[0].isdigit():
            continue
        m = re.match(r"(.+?)[（(]([^）)]*)[）)]?$", cells[1])
        companies.append({"name": m.group(1).strip(), "alias": [m.group(2).strip()]} if m else {"name": cells[1], "alias": []})
    print(f"{filename}：{len(companies)} 家")
    return companies


def unicorn_names(num):
    data = json.loads(fetch(UNICORN.format(num=num), f"unicorn_{num}.json"))
    names = [r["hs_Rank_Unicorn_ComName_Cn"] for r in data["rows"]
             if (r["hs_Rank_Unicorn_ComHeadquarters_Cn"] or "").startswith("中国") and r["hs_Rank_Unicorn_ComName_Cn"]]
    print(f"独角兽（中国）：{len(names)} 家")
    return names


def main():
    parser = argparse.ArgumentParser(description="更新上市公司和 500 强名单")
    parser.add_argument("--global500", default=GLOBAL500, help="财富中文网世界 500 强榜单网址")
    parser.add_argument("--china500", default=CHINA500, help="财富中文网中国 500 强榜单网址")
    parser.add_argument("--unicorn", default=UNICORN_NUM, help="胡润全球独角兽榜的榜单编号")
    args = parser.parse_args()

    listed = sorted(sse_names() + szse_names(), key=lambda c: c["name"])
    data = {
        "说明": "由 tools/update_companies.py 生成，请勿手工修改。listed：A 股上市公司全称和简称（上交所、深交所）；"
               "global500 / china500：《财富》世界 500 强、中国 500 强（财富中文网）；unicorns：胡润全球独角兽榜中国企业。北交所暂缺。",
        "global500": fortune_names(args.global500, "global500.html"),
        "china500": fortune_names(args.china500, "china500.html"),
        "listed": listed,
        "unicorns": unicorn_names(args.unicorn),
    }
    OUTPUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已生成 {OUTPUT}：A 股 {len(listed)} 家")


if __name__ == "__main__":
    main()

"""回归测试：编辑岗位要求表格时页面不刷新，点「确认岗位要求」才生效（防 removeChild 报错）。

背景：旧版每改一格表格就整页刷新，和表格下拉框的关闭动作撞在一起时，页面会报
"Failed to execute 'removeChild' on 'Node'"。这是时序问题，测试里很难直接复现，
所以这个测试检查的是**触发条件**：编辑期间发给服务器的刷新请求必须是 0 次；
同时监听页面错误和控制台错误，出现 removeChild 也算失败。

用法（在项目根目录）：
    pip install -r requirements-dev.txt
    python tests/test_job_form.py                 # 测当前代码
    python tests/test_job_form.py --root 其他目录   # 测另一份检出（例如旧版本）

不需要 API Key、不调用 AI：以公开模式启动（不写 jobs/ 和缓存），用"看演示"载入岗位要求。
用系统自带的 Edge（无头模式），不需要另外下载浏览器。全部通过退出码 0，否则 1。
"""
import argparse
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "tests" / "output"      # 失败截图，不上传
SETTLE_MS = 1500                        # 每步之后等一会儿，让可能的刷新请求发出去


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_app(root, port):
    env = {**os.environ, "PUBLIC_DEMO": "1"}
    proc = subprocess.Popen([sys.executable, "-m", "streamlit", "run", "app.py", "--server.port", str(port),
                             "--server.headless", "true"],
                            cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://localhost:{port}"
    for _ in range(60):
        try:
            urllib.request.urlopen(url, timeout=1)
            return proc, url
        except OSError:
            time.sleep(1)
    proc.terminate()
    raise RuntimeError("网页服务 60 秒内没有启动")


def run(url, shots=False):
    """返回 (检查结果列表, 页面错误, 控制台错误)；检查结果是 (名称, 是否通过, 说明)。"""
    checks, page_errors, console_errors, sent = [], [], [], []
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="msedge", headless=True)
        except PlaywrightError as e:
            raise RuntimeError(f"无法启动 Edge（需要安装 Microsoft Edge）：{e}") from e
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("websocket", lambda ws: ws.on("framesent", lambda _: sent.append(1)))

        def refreshes(action):
            before = len(sent)
            action()
            page.wait_for_timeout(SETTLE_MS)
            return len(sent) - before

        try:
            page.goto(url, wait_until="networkidle")
            page.get_by_text("一批校招简历").first.wait_for(timeout=60000)
            page.get_by_role("button", name="看演示").click()
            page.wait_for_timeout(3000)
            page.get_by_role("radio", name="岗位要求").first.click()
            page.get_by_text("补充条件").first.wait_for(timeout=30000)
            page.wait_for_timeout(2000)

            grid = page.locator('[data-testid="stDataFrame"]').last      # 岗位要求页最后一个表格是「补充条件」
            grid.scroll_into_view_if_needed()
            page.wait_for_timeout(1000)
            # 每一步前重新取表格位置（页面可能滚动过）；表头和每行约 35 像素高
            col_x = lambda b, i: b["x"] + b["width"] * (0.12, 0.45, 0.837)[i]   # 类型、内容、必须（勾选框在列正中）
            first_row = lambda b: b["y"] + 35 + 17                              # 新加的行是第 1 行

            def pick_type():                                               # 点最下面的空行会新增一行
                b = grid.bounding_box()
                y = b["y"] + b["height"] - 15
                page.mouse.click(col_x(b, 0), y)
                page.wait_for_timeout(800)
                page.mouse.dblclick(col_x(b, 0), y)
                page.wait_for_timeout(800)
                option = page.get_by_text("院校", exact=True).last.bounding_box()
                page.mouse.click(option["x"] + 10, option["y"] + option["height"] / 2)

            def type_value():
                b = grid.bounding_box()
                page.mouse.dblclick(col_x(b, 1), first_row(b))
                page.wait_for_timeout(600)
                page.keyboard.type("211 及以上")
                page.keyboard.press("Enter")

            def tick_required():
                page.keyboard.press("Escape")                              # 回车后下一行的格子处于选中状态，先退出
                page.wait_for_timeout(300)
                b = grid.bounding_box()
                page.mouse.click(col_x(b, 2), first_row(b))                # 勾选框点一下就切换，不能点两下

            counts = [refreshes(pick_type), refreshes(type_value), refreshes(tick_required)]
            edit_total = sum(counts)
            if shots:                                                      # 人工核对：三步编辑确实做了
                OUTPUT.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(OUTPUT / "job_form_edited.png"))
            checks.append(("编辑表格时不刷新页面", edit_total == 0,
                           f"选院校 {counts[0]} 次、填内容 {counts[1]} 次、勾必须 {counts[2]} 次，共 {edit_total} 次（应为 0）"))

            # "看演示"一开始就显示「已确认」，所以看确认成功才弹出的提示，才能说明这次确认生效了
            confirm = refreshes(lambda: page.get_by_role("button", name="确认岗位要求").click())
            confirmed = page.get_by_text("岗位要求已确认").count() > 0
            checks.append(("点确认后生效", confirm >= 1 and confirmed,
                           f"确认时刷新 {confirm} 次，{'弹出' if confirmed else '没有弹出'}「岗位要求已确认」提示"))

            shown = page.get_by_text("removeChild").count() + page.locator('[data-testid="stException"]').count()
            checks.append(("页面上没有报错", shown == 0, f"报错框 {shown} 个"))
        finally:
            if not all(ok for _, ok, _ in checks) or not checks:
                OUTPUT.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(OUTPUT / "job_form_failed.png"), full_page=True)
            browser.close()

    bad_console = [e for e in console_errors if "removeChild" in e or "NotFoundError" in e]
    checks.append(("没有运行时错误（页面错误 + 控制台）", not page_errors and not bad_console,
                   f"页面错误 {len(page_errors)} 条，控制台 removeChild 类错误 {len(bad_console)} 条"))
    return checks, page_errors, console_errors


def main():
    parser = argparse.ArgumentParser(description="岗位要求表单回归测试")
    parser.add_argument("--root", default=str(ROOT), help="要测试的项目目录（默认是本项目）")
    parser.add_argument("--shots", action="store_true", help="保存编辑后的截图到 tests/output/，人工核对三步编辑确实做了")
    args = parser.parse_args()
    port = free_port()
    proc, url = start_app(args.root, port)
    try:
        checks, page_errors, console_errors = run(url, args.shots)
    finally:
        proc.terminate()
        proc.wait(timeout=30)

    print(f"测试目录：{args.root}")
    for name, ok, detail in checks:
        print(f"  [{'通过' if ok else '失败'}] {name}：{detail}")
    for e in page_errors:
        print(f"    页面错误：{e[:200]}")
    for e in console_errors:
        print(f"    控制台错误：{e[:200]}")
    passed = all(ok for _, ok, _ in checks)
    print("结果：全部通过" if passed else f"结果：未通过（截图在 {OUTPUT}）")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())

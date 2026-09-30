"""页面外观：样式表和几个只负责显示的小部件（品牌区、进度条、步骤标题、空状态、页脚）。
这里不做任何判断，只把传进来的文字排版好；样式说明见 ui/theme.css 开头。"""
from html import escape
from pathlib import Path

import streamlit as st

CSS = (Path(__file__).with_name("theme.css")).read_text(encoding="utf-8")
PRIMARY = "#1F4FD1"
REPO_URL = "https://github.com/long01687576717-art/Resume-screening-tool"

# 标志：蓝底圆角方块里三条由长到短的横线（一批简历排出先后），最上面一条后面打勾
LOGO = f"""<svg width="28" height="28" viewBox="0 0 28 28" aria-hidden="true">
<rect width="28" height="28" rx="7" fill="{PRIMARY}"/>
<rect x="6" y="7.5" width="10" height="2.6" rx="1.3" fill="#fff"/>
<path d="M18.3 8.8l1.7 1.7 3.2-3.4" stroke="#fff" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
<rect x="6" y="12.8" width="13" height="2.6" rx="1.3" fill="#fff" opacity=".8"/>
<rect x="6" y="18.1" width="8" height="2.6" rx="1.3" fill="#fff" opacity=".55"/>
</svg>"""


def inject_css():
    st.html(f"<style>{CSS}</style>")


def _html(text):
    st.markdown(text, unsafe_allow_html=True)


def brand(tag):
    _html(f'<div class="rs-brand">{LOGO}<span>简历初筛助手</span><span class="rs-tag">{escape(tag)}</span></div>')


def headline(title, lead):
    _html(f'<div class="rs-h1">{escape(title)}</div><div class="rs-lead">{lead}</div>')


def stats(pairs):
    """一行事实数字，如 ("7 个", "分析模块")。只放可核实的数字。"""
    cells = "".join(f'<div class="rs-stat"><b>{escape(v)}</b><span>{escape(k)}</span></div>' for v, k in pairs)
    _html(f'<div class="rs-stats">{cells}</div>')


def link(text, anchor):
    _html(f'<a class="rs-link" href="#{anchor}" target="_self">{escape(text)}</a>')


def stepper(steps):
    """steps：[(锚点, 标题, 状态说明, 状态)]，状态为 done / now / todo；点击跳到对应步骤。"""
    cells = []
    for i, (anchor, title, sub, status) in enumerate(steps, 1):
        mark = "✓" if status == "done" else str(i)
        cells.append(f'<a class="rs-step {status}" href="#{anchor}" target="_self"><span class="rs-dot">{mark}</span>'
                     f'<span><b>{escape(title)}</b><small>{escape(sub)}</small></span></a>')
    _html(f'<div class="rs-stepper">{"".join(cells)}</div>')


def section(num, title, sub, anchor):
    _html(f'<div class="rs-sec" id="{anchor}"><span class="rs-num">{num}</span><span class="rs-sec-title">{escape(title)}</span></div>'
          f'<div class="rs-sec-sub">{escape(sub)}</div>')


def empty_state(icon, title, text):
    _html(f'<div class="rs-empty"><div class="rs-empty-icon">{icon}</div><b>{escape(title)}</b><span>{escape(text)}</span></div>')


def footer(disclaimer):
    _html(f'<div class="rs-footer">简历初筛助手 · 面向中小企业校招的简历阅读排序工具<br>{escape(disclaimer)}<br>'
          f'判断规则和代码公开：<a href="{REPO_URL}" target="_blank">GitHub</a></div>')

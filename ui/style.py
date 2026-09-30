"""页面外观：样式表和几个只负责显示的小部件（品牌、首屏标题、空状态、页脚）。
这里不做任何判断，只把传进来的文字排版好；样式说明见 ui/theme.css 开头。"""
from html import escape
from pathlib import Path

import streamlit as st

CSS_FILE = Path(__file__).with_name("theme.css")
INK = "#111827"
REPO_URL = "https://github.com/long01687576717-art/Resume-screening-tool"

# 标志：深色圆角方块里三条由长到短的横线（一批简历排出先后），最上面一条后面打勾
LOGO = f"""<svg width="26" height="26" viewBox="0 0 28 28" aria-hidden="true">
<rect width="28" height="28" rx="7" fill="{INK}"/>
<rect x="6" y="7.5" width="10" height="2.6" rx="1.3" fill="#fff"/>
<path d="M18.3 8.8l1.7 1.7 3.2-3.4" stroke="#fff" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
<rect x="6" y="12.8" width="13" height="2.6" rx="1.3" fill="#fff" opacity=".8"/>
<rect x="6" y="18.1" width="8" height="2.6" rx="1.3" fill="#fff" opacity=".55"/>
</svg>"""


def inject_css():
    st.html(f"<style>{CSS_FILE.read_text(encoding='utf-8')}</style>")


def _html(text):
    st.markdown(text, unsafe_allow_html=True)


def brand():
    _html(f'<div class="rs-brand">{LOGO}<span>简历初筛助手</span></div>')


def headline(title, lead):
    _html(f'<div class="rs-h1">{escape(title)}</div><div class="rs-lead">{escape(lead)}</div>')


def empty_state(title, text):
    _html(f'<div class="rs-empty"><b>{escape(title)}</b>{escape(text)}</div>')


def footer(items):
    """一行页脚：几条要点 + 代码公开链接。"""
    cells = "".join(f"<span>{escape(i)}</span>" for i in items)
    _html(f'<div class="rs-footer">{cells}<a href="{REPO_URL}" target="_blank">GitHub</a></div>')

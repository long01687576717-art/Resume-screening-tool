"""网页版：给一份 JD 和一批简历，告诉 HR 先看谁、为什么。

运行：streamlit run app.py
页面只负责展示和收集 HR 的修改，判断全部复用命令行版的代码（matching/、modules/），两边结果一致。

公开部署时设置环境变量 PUBLIC_DEMO=1（公开模式）：
- 只用访客自己填的 API Key（不读服务器上的 .env / 环境变量，否则所有访客都在花部署者的钱），不能保存
- 不写硬盘缓存、不保存岗位要求文件：访客之间互相看不到，关掉页面就消失
- "看演示"读 demo/ 里预先生成的虚构简历结果，不需要 Key、不调用 AI（python tools/build_demo.py 生成）
"""
import hashlib
import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import streamlit as st
from openai import OpenAI, OpenAIError

import main as analyzer
from knowledge.kb import KnowledgeBase
from llm.client import BASE_URL, RESUME_CACHE_DAYS, LLMClient, clear_resume_cache, read_env_file, save_env
from matching import jd, match, overview
from matching.jd_extract import QUALITIES, RESUME_JUDGEABLE
from modules.base import ModuleResult
from modules.experience import LEVEL_NAMES
from parser.file_reader import IMAGE_SUFFIXES, pdf_pages, read_bytes
from report import screening
from report.formatter import DISCLAIMER, format_report
from ui import style as ui

ROOT = Path(__file__).resolve().parent
def _public_mode():
    """部署平台上用环境变量或 Streamlit secrets 设置 PUBLIC_DEMO=1；本机没有 secrets 文件时读取会报错，当作本机模式。"""
    if os.environ.get("PUBLIC_DEMO") == "1":
        return True
    try:
        return str(st.secrets.get("PUBLIC_DEMO", "")) == "1"
    except Exception:
        return False


PUBLIC = _public_mode()
MAX_PUBLIC_UPLOADS = 20          # 公开模式下一次最多分析的份数（占用的是公共服务器）
DEMO_DIR = ROOT / "demo"
JOBS_DIR = ROOT / "jobs"
JD_SAMPLES = sorted((ROOT / "samples" / "jd").glob("*.txt"))
# 示例简历只用虚构的（resumes/ 里是真实简历，不在网页里提供）
RESUME_SAMPLES = [p for d in ("samples", "samples/test/dev") for p in sorted((ROOT / d).iterdir())
                  if p.suffix.lower() in analyzer.SUPPORTED_SUFFIXES]
UPLOAD_TYPES = ["pdf", "docx", "txt", "png", "jpg", "jpeg"]
DEGREE_OPTIONS = ["不限", "专科", "本科", "硕士", "博士"]
KINDS = ["必须", "加分", "基础要求"]

QUEUE_STYLE = {   # 颜色固定：绿 = 先看，蓝 = 其次，灰 = 后看，橙 = 不符，紫 = 需人工
    "优先看": ("green", "做过 2 条以上岗位职责，或最看重的能力有 2 项以上实证"),
    "值得看": ("blue", "做过 1 条岗位职责，或 1 项能力有实证，或 2 项自述 / 课程"),
    "可以后看": ("gray", "简历里没有体现最看重的能力（没写 ≠ 不会）"),
    "硬条件不符": ("orange", "确定的事实不满足门槛（推断出来的不会放进这里）"),
    "需人工查看": ("violet", "文件解析异常，请直接打开原文件"),
}
GRADE_MARK = {"实证": "●", "自述 / 课程": "◐", "没体现": "○"}
# HR 在完整分析里标记的决定；只存在这次打开的网页里，刷新就没有了，靠导出表格留存
DECISIONS = ("通过", "待定", "淘汰")
DECISION_BADGE = {"通过": ":green-badge[✓ 通过]", "待定": ":blue-badge[待定]", "淘汰": ":gray-badge[✕ 淘汰]"}
VIEWS = ("阅读队列", "总览")
NO_ORIGINAL = "没有原文件，重新上传后可以查看"   # 原简历页和"查看原简历"按钮共用同一句


@st.cache_resource
def knowledge():
    return KnowledgeBase()


@st.cache_resource
def _local_clients(api_key):
    """本机：简历用的客户端（缓存 30 天删除）、JD 用的（缓存长期保留）。"""
    return LLMClient(api_key=api_key), LLMClient(cache="jd", api_key=api_key)


def _clients(api_key):
    """返回 (简历用, JD 用) 两个 AI 客户端；没有 Key 时返回 (None, None)。"""
    if not PUBLIC:
        try:
            return _local_clients(api_key)
        except RuntimeError:
            return None, None
    # 公开模式：只用访客自己的 Key，不写硬盘缓存；客户端只存在这个访客的页面里
    if not api_key:
        return None, None
    state = st.session_state
    if state.get("_clients_key") != api_key:
        state._clients = (LLMClient(use_cache=False, api_key=api_key),
                          LLMClient(use_cache=False, cache="jd", api_key=api_key))
        state._clients_key = api_key
    return state._clients


def _settings():
    """右上角「设置」里的内容：API Key 和隐私。返回网页里输入的 API Key。"""
    state = st.session_state
    saved = not PUBLIC and bool(read_env_file().get("DEEPSEEK_API_KEY") or os.environ.get("DEEPSEEK_API_KEY"))
    st.markdown("**AI 服务**", help="使用 DeepSeek 提取简历和 JD 里的事实；判断由规则完成。"
                + ("Key 只在这个页面里使用，不会保存到服务器。" if PUBLIC else "Key 默认只在这个页面里使用，勾选才保存到本机。"))
    st.caption("已连接" if state.get("api_key") or saved else "未设置 API Key")
    key = st.text_input("API Key", type="password", placeholder="sk-…", key="key_input", label_visibility="collapsed")
    remember = False if PUBLIC else st.checkbox("保存到本机", key="key_remember")
    if st.button("连接", disabled=not key.strip(), width="stretch"):
        try:
            OpenAI(api_key=key.strip(), base_url=BASE_URL, timeout=20).models.list()
        except OpenAIError:
            # 不把接口返回的原始信息显示出来：里面可能带着部分 Key
            st.error("连接失败")
        else:
            state.api_key = key.strip()
            if remember:
                save_env({"DEEPSEEK_API_KEY": key.strip()})
            state.pop("key_input", None)
            st.rerun()
    st.divider()
    st.markdown("**隐私**", help=("本站不保存简历和分析结果，关掉页面就消失；手机号、邮箱、身份证号不发给 AI。请不要上传未经本人同意的简历。"
                                  if PUBLIC else f"上传的文件分析完即删除；AI 提取的内容缓存在本机，{RESUME_CACHE_DAYS} 天后自动删除；"
                                                 "手机号、邮箱、身份证号不发给 AI。"))
    if st.button("清除分析结果", width="stretch"):
        if not PUBLIC:
            clear_resume_cache()
            st.cache_resource.clear()
        for k in ("analyses", "selected", "req", "draft", "demo_loaded"):
            state.pop(k, None)
        st.rerun()
    return state.get("api_key")


PAGES = ("岗位要求", "简历", "结果")
FOOTER = ("仅辅助阅读，不作为录用依据", "没写 ≠ 不会", "不按性别、年龄等筛选",
          "本站不保存简历" if PUBLIC else f"本机缓存 {RESUME_CACHE_DAYS} 天自动删除")


ABOUT = "**简历初筛助手**：给一批校招简历（有 JD 更好），告诉 HR 先看谁、为什么。仅辅助阅读，不作为录用依据。"


def main():
    st.set_page_config(page_title="简历初筛助手", page_icon="📋", layout="wide",
                       initial_sidebar_state="collapsed", menu_items={"About": ABOUT})
    ui.inject_css()
    kb = knowledge()
    state = st.session_state
    state.setdefault("analyses", {})
    if "goto" in state:                  # 看演示、分析完成后跳到对应页面（要在切换控件画出来之前设置）
        state.page = state.pop("goto")
    state.setdefault("page", PAGES[1])

    with st.container(key="rs_topbar"):
        c1, c2 = st.columns([8, 1], vertical_alignment="center")
        with c1:
            ui.brand()
        with c2.popover("设置", icon=":material/settings:", width="stretch"):
            api_key = _settings()
    llm, jd_llm = _clients(api_key)

    if not state.get("selected"):
        _hero()
    page = st.segmented_control("页面", PAGES, key="page", label_visibility="collapsed") or PAGES[1]
    if page == PAGES[0]:
        step_job(kb, jd_llm)
    elif page == PAGES[1]:
        step_resumes(kb, llm)
    else:
        step_results(kb)
    ui.footer(FOOTER)


def _hero():
    """首屏：一句标题、一句副标题、两个按钮。分析过简历后不再显示。"""
    with st.container(key="rs_hero"):
        ui.headline("一批校招简历，先看谁、为什么", "基于简历证据的阅读排序，不打分，只给理由。")
        c1, c2, _ = st.columns([1, 1, 5])
        if c1.button("看演示", type="primary", width="stretch", help="11 份虚构简历，无需 API Key"):
            _load_demo()
        if c2.button("上传简历", width="stretch"):
            st.session_state.goto = PAGES[1]
            st.rerun()


def _load_demo():
    """载入预先生成的虚构简历分析结果和一份确认好的岗位要求，不需要 Key、不调用 AI。"""
    state = st.session_state
    analyses = json.loads((DEMO_DIR / "analyses.json").read_text(encoding="utf-8"))
    samples = {p.name: p for p in RESUME_SAMPLES}   # 演示的原简历就是示例文件夹里的虚构简历
    for a in analyses:
        a["results"] = [ModuleResult(r["title"], [tuple(i) for i in r["items"]], r["notes"]) for r in a["results"]]
        if a["name"] in samples:
            a["data"] = samples[a["name"]].read_bytes()
        state.analyses[f"demo:{a['name']}"] = a
    state.selected = [f"demo:{a['name']}" for a in analyses]
    job = json.loads((DEMO_DIR / "job.json").read_text(encoding="utf-8"))
    _set_draft(job, [])
    state.req = job
    state.demo_loaded = True
    state.goto = PAGES[2]
    st.rerun()


# ---------- ① 岗位要求 ----------

def step_job(kb, llm):
    state = st.session_state
    source = st.segmented_control("JD 来源", ["粘贴新的 JD", "打开已保存的岗位"], default="粘贴新的 JD",
                                  label_visibility="collapsed")
    if source == "打开已保存的岗位":
        saved = sorted(p for p in JOBS_DIR.glob("*.json"))
        if not saved:
            st.caption("还没有保存过岗位")
            return
        pick = st.selectbox("已保存的岗位", [p.stem for p in saved])
        if st.button("打开"):
            req, warnings = jd.load(JOBS_DIR / f"{pick}.json", kb)
            _set_draft(req, warnings)
    else:
        c1, c2 = st.columns([3, 1])
        c2.selectbox("示例 JD", ["—"] + [p.stem for p in JD_SAMPLES], key="jd_example", on_change=_fill_example)
        title = c1.text_input("职位名称", key="jd_title", placeholder="例如：数据分析工程师")
        text = st.text_area("岗位 JD", key="jd_text", height=200, placeholder="粘贴岗位职责和任职要求")
        if st.button("解析 JD", type="primary", disabled=not (llm and title.strip() and text.strip()),
                     help=None if llm else "需要在右上角「设置」里连接 API Key"):
            with st.spinner("正在理解 JD（首次约 20 秒）……"):
                req = jd.parse(text, title.strip(), kb, llm)
            _save_job(req, original=True)
            _set_draft(req, [])

    if state.get("draft"):
        _edit_form(kb)


def _fill_example():
    name = st.session_state.jd_example
    path = next((p for p in JD_SAMPLES if p.stem == name), None)
    if path:
        text = path.read_text(encoding="utf-8-sig")
        st.session_state.jd_title = next(line.strip() for line in text.splitlines() if line.strip())
        st.session_state.jd_text = text


def _set_draft(req, warnings):
    st.session_state.draft = req
    st.session_state.draft_warnings = warnings
    st.session_state.form_id = st.session_state.get("form_id", 0) + 1   # 换了岗位，表单控件重新生成
    st.session_state.req = None


NO_LEVEL = "未写"      # 表格下拉框里空值会显示成英文 None，用中文代替


def _edit_form(kb):
    """把工具对 JD 的理解做成表单让 HR 改；改完经过和 txt 文件相同的读取、核对流程。
    用 st.form：填写过程中不刷新页面，点「确认岗位要求」才一起生效。
    否则表格里每改一格就整页刷新，和表格下拉框的关闭动作撞在一起时，页面会报 removeChild 错误。"""
    state = st.session_state
    base = state.draft
    fid = state.form_id
    with st.form(f"job_form{fid}", border=True, enter_to_submit=False):
        refs = "、".join(f"{r['岗位']}（相似度{r['相似度']}）" for r in base["参考岗位"]) or "知识库里没有相近岗位"
        st.subheader(base["岗位"], help=f"参考岗位：{refs}。以下内容都可以修改，改完点最下面的「确认」。")

        c1, c2 = st.columns([1, 3])
        degree = c1.selectbox("学历门槛", DEGREE_OPTIONS, index=DEGREE_OPTIONS.index(base["门槛"]["学历"]), key=f"degree{fid}",
                              help="只按简历里写明的学历判断；推断出来的只会进「待确认」")

        st.markdown("**岗位职责**", help="用来找「做过类似事」的人：简历里同一句经历出现 2 个关键词，或 1 个少见的关键词且领域相同，"
                                         "就算做过。关键词用「、」分开；空泛的职责取消勾选。")
        duties = st.data_editor(
            pd.DataFrame([{"参与匹配": not d["空泛"], "职责": d["职责"], "关键词": "、".join(d["关键词"])} for d in base["职责"]]),
            column_config={"参与匹配": st.column_config.CheckboxColumn(width="small"),
                           "职责": st.column_config.TextColumn(disabled=True, width="large"),
                           "关键词": st.column_config.TextColumn(width="medium")},
            hide_index=True, key=f"duties{fid}")

        st.markdown("**能力要求**", help="「基础要求」是大多数岗位都写的（如 Office），区分不出人，不参与排序。"
                                         "可以在最后一行添加 JD 没写但你们看重的能力。")
        reqs = st.data_editor(
            pd.DataFrame([{"名称": r["名称"], "类别": "基础要求" if r["基础要求"] else ("必须" if r["必须"] else "加分"),
                           "程度": r["程度"] or NO_LEVEL, "领域": r["领域"] or ""} for r in base["要求"]]),
            column_config={"名称": st.column_config.TextColumn(required=True),
                           "类别": st.column_config.SelectboxColumn(options=KINDS, required=True, default="加分"),
                           "程度": st.column_config.SelectboxColumn(options=[NO_LEVEL, "了解", "熟悉", "掌握", "熟练", "精通"], default=NO_LEVEL),
                           "领域": st.column_config.TextColumn(disabled=True, default="", help="由能力词典自动归类")},
            hide_index=True, num_rows="dynamic", key=f"reqs{fid}")
        names = [n for n, k in zip(reqs["名称"], reqs["类别"]) if isinstance(n, str) and n.strip() and k != "基础要求"]
        top = st.multiselect("最看重（最多 3 项）", names,
                             help="决定阅读顺序，按选择的先后。表格里新加的能力，点一次「确认岗位要求」后才出现在这里",
                             default=[n for n in base["最看重"] if n in names], max_selections=3, key=f"top{fid}")

        c1, c2 = st.columns(2)
        majors, majors_must = _list_input(c1, "专业", base["专业"], fid)
        certs, certs_must = _list_input(c2, "证书", base["证书"], fid)

        qualities = st.multiselect(
            "素质", QUALITIES, default=[q["素质"] for q in base["素质"]], key=f"qualities{fid}",
            help=f"简历能看出的（{'、'.join(sorted(RESUME_JUDGEABLE))}）只用于排序参考；其余需面试考察")

        st.markdown("**补充条件**", help="JD 没写、但你们看重的。默认算加分，勾选「必须」才当门槛。"
                                         "性别、年龄、婚育、籍贯等不能作为筛选条件，这里不提供。")
        extras = st.data_editor(
            pd.DataFrame([{"类型": c["类型"], "内容": c["值"], "必须": c["必须"]} for c in base["补充条件"]],
                         columns=["类型", "内容", "必须"]),
            column_config={"类型": st.column_config.SelectboxColumn(options=list(jd.EXTRA_TYPES), required=True),
                           "内容": st.column_config.TextColumn(default="", help="院校：211 及以上 / 实习：有 或 某方向 / 证书：CPA"),
                           "必须": st.column_config.CheckboxColumn(default=False)},
            hide_index=True, num_rows="dynamic", key=f"extras{fid}")

        edited = _form_to_req(base, degree, duties, reqs, top, majors, majors_must, certs, certs_must, qualities, extras)
        text = jd.to_text(edited, f"jobs/{_safe(base['岗位'])}.txt")
        req, warnings = jd.apply_text(base, text, kb)
        for w in state.draft_warnings + warnings:
            st.warning(w)
        if st.form_submit_button("确认岗位要求", type="primary"):
            if req["最看重"]:
                _save_job(req, original=False)
                state.req = req
                st.toast("岗位要求已确认" if PUBLIC else "岗位要求已确认，并保存到 jobs/ 文件夹")
        if not req["最看重"]:
            st.caption("请至少选择 1 项「最看重」，再点确认")
        elif state.get("req") == req:
            st.success("已确认（再修改的话，改完重新点确认）")


def _list_input(col, label, value, fid):
    text = col.text_input(label, "、".join(value["要求"]), key=f"{label}{fid}", help="多个用「、」分开")
    must = col.checkbox(f"{label}是门槛（不勾选算加分）", value["必须"], key=f"{label}must{fid}")
    return [s.strip() for s in text.replace("，", "、").split("、") if s.strip()], must


def _form_to_req(base, degree, duties, reqs, top, majors, majors_must, certs, certs_must, qualities, extras):
    old = {r["名称"]: r for r in base["要求"]}
    requirements = []
    for name, kind, level in zip(reqs["名称"], reqs["类别"], reqs["程度"]):
        if not isinstance(name, str) or not name.strip():
            continue
        r = dict(old.get(name.strip()) or {"名称": name.strip(), "类型": "技能", "领域": "", "参考岗位占比": None, "来源": "HR 添加"})
        r.update({"程度": level if isinstance(level, str) and level != NO_LEVEL else "", "必须": kind == "必须", "基础要求": kind == "基础要求"})
        requirements.append(r)
    return {**base,
            "门槛": {**base["门槛"], "学历": degree},
            "职责": [{"职责": d, "关键词": [w.strip() for w in str(k or "").replace("，", "、").split("、") if w.strip()],
                    "领域": next((x["领域"] for x in base["职责"] if x["职责"] == d), ""), "空泛": not use}
                   for use, d, k in zip(duties["参与匹配"], duties["职责"], duties["关键词"])],
            "要求": requirements, "最看重": top,
            "专业": {"要求": majors, "必须": majors_must}, "证书": {"要求": certs, "必须": certs_must},
            "素质": [{"素质": q, "原文": "", "用途": "排序参考" if q in RESUME_JUDGEABLE else "面试考察"} for q in qualities],
            "补充条件": [{"类型": t, "值": str(v).strip(), "必须": bool(m)} for t, v, m in zip(extras["类型"], extras["内容"], extras["必须"])
                     if isinstance(t, str) and isinstance(v, str) and v.strip()]}


def _save_job(req, original):
    """和命令行版用同一套文件：json 是工具的原始理解，txt 是 HR 改过的版本。
    公开模式不保存（否则访客之间能互相看到），只留在这个页面里。"""
    if PUBLIC:
        return
    JOBS_DIR.mkdir(exist_ok=True)
    path = JOBS_DIR / f"{_safe(req['岗位'])}.json"
    if original or not path.exists():
        jd.save(req, path)
    path.with_suffix(".txt").write_text(jd.to_text(req, f"jobs/{path.stem}.txt"), encoding="utf-8")


def _safe(name):
    return "".join(ch for ch in name if ch not in '\\/:*?"<>|').strip()[:40] or "岗位"


# ---------- ② 简历 ----------

def step_resumes(kb, llm):
    state = st.session_state
    c1, c2 = st.columns([3, 2])
    uploads = c1.file_uploader("上传简历", type=UPLOAD_TYPES, accept_multiple_files=True,
                               help="PDF、Word（.docx）、TXT 或图片，可多选")
    with c2:
        use_samples = st.toggle(f"加入示例简历（{len(RESUME_SAMPLES)} 份，虚构）")
    items = [(f.name, f.getvalue()) for f in uploads or []]
    if use_samples:
        items += [(p.name, p.read_bytes()) for p in RESUME_SAMPLES]

    too_many = PUBLIC and len(items) > MAX_PUBLIC_UPLOADS
    if st.button(f"开始分析 {len(items)} 份" if items else "开始分析", type="primary",
                 disabled=not (items and llm) or too_many,
                 help=None if llm else "需要在右上角「设置」里连接 API Key；也可以回到首页看演示"):
        state.selected = _analyze(items, kb, llm)
        state.goto = PAGES[2]
        st.rerun()
    if too_many:
        st.caption(f"在线版一次最多 {MAX_PUBLIC_UPLOADS} 份")


def _analyze(items, kb, llm):
    """逐份分析；分析过的（内容相同）直接复用。返回本次选中的简历编号。"""
    cache = st.session_state.analyses
    keys = [hashlib.sha1(data).hexdigest() for _, data in items]
    todo = [(k, name, data) for k, (name, data) in zip(keys, items) if k not in cache]
    if todo:
        bar = st.progress(0.0, text=f"正在分析 0 / {len(todo)} 份（每份约 10～20 秒，多份同时进行）")
        context = {"kb": kb, "llm": llm, "job_city": None}
        with tempfile.TemporaryDirectory() as tmp, ThreadPoolExecutor(analyzer.MAX_PARALLEL_RESUMES) as pool:
            futures = {}
            for k, name, data in todo:
                path = Path(tmp) / k[:8] / name
                path.parent.mkdir()
                path.write_bytes(data)
                futures[pool.submit(analyzer.run_modules, path, context)] = (k, name, data)
            for n, future in enumerate(as_completed(futures), 1):
                k, name, data = futures[future]
                try:
                    results, profile = future.result()
                    note = profile["parse"]["source"] if profile["parse"]["source"] != "文字" else None
                    cache[k] = {"name": name, "profile": profile, "results": results, "note": note,
                                "report": format_report(name, results, note), "data": data}   # 原文件只留在网页内存里
                except Exception as e:   # 单份失败不影响其他简历
                    cache[k] = {"name": name, "error": str(e)}
                bar.progress(n / len(todo), text=f"正在分析 {n} / {len(todo)} 份")
        bar.empty()
    return list(dict.fromkeys(keys))


# ---------- ③ 结果 ----------

def step_results(kb):
    """有岗位要求时默认看阅读队列，也能切到总览；没有就只有总览。
    每个人在完整分析里标记 通过 / 待定 / 淘汰，标完自动打开下一位没处理的人。"""
    state = st.session_state
    keys = state.get("selected")
    if not keys:
        ui.empty_state("还没有结果", "上传并分析简历后显示")
        return
    items = [state.analyses[k] for k in keys]
    for a in items:
        if "error" in a:
            st.error(f"无法分析：{a['name']}：{a['error']}")
    profiles = [a["profile"] for a in items if "profile" in a]
    reports = {a["name"]: a for a in items if "report" in a}   # 文件名 → 分析结果（完整分析用）
    decisions = state.setdefault("decisions", {})

    c1, c2 = st.columns([3, 1], vertical_alignment="center")
    with c1:
        if state.get("req"):
            view = st.segmented_control("视图", VIEWS, default=VIEWS[0], required=True, key="view",
                                        label_visibility="collapsed")
        else:
            view = VIEWS[1]
            b1, b2 = st.columns([4, 1], vertical_alignment="center")
            b1.caption("导入岗位要求后，可以按岗位排出阅读顺序")
            if b2.button("去导入", width="stretch"):
                state.goto = PAGES[0]
                st.rerun()
    done = sum(name in decisions for name in reports)
    c2.markdown(f"<div style='text-align:right;color:#6B7280;font-size:14px'>已处理 {done} / {len(reports)} 人</div>",
                unsafe_allow_html=True)
    if "done_toast" in state:
        st.toast(state.pop("done_toast"))

    if view == VIEWS[0]:
        order, why = _queues(profiles, reports, kb)
    else:
        order, why = _overview(profiles, reports, kb)
    target = state.pop("open_report", None)
    if target in reports:
        _report_dialog(target, reports[target], order, why.get(target))


def _overview(profiles, reports, kb):
    """返回 (阅读顺序, {文件名: 这一行})，给"下一份"和"为什么在这里"用。"""
    state = st.session_state
    req = state.get("req")
    decisions = state.decisions
    options = [d for d in overview.DIMENSIONS if d != "岗位匹配" or req]
    c1, c2 = st.columns([3, 2])
    dims = c1.multiselect("分层依据", options, default=list(overview.DEFAULT_DIMENSIONS), key="ov_dims",
                          help="只有在所选每一项上都不比别人差、且至少一项更好，才排在前面；否则各有所长，放在同一层。"
                               "选择的先后不影响结果。「学历」更适合当门槛（在下面的硬性要求里设）；「岗位匹配」需要先确认岗位要求")
    mode = c2.radio("学校看哪一段", overview.SCHOOL_MODES, horizontal=True, key="ov_mode",
                    help="读过硕士的人有两段学校。先看的一段决定层次，另一段在相同时再比较")
    with st.expander("硬性要求"):
        f1, f2, f3, f4 = st.columns(4)
        degree = f1.selectbox("最低学历", ["不限", *overview.DEGREES], key="ov_degree")
        school = f2.selectbox("院校层次", list(overview.SCHOOL_LIMITS), key="ov_school", help="按上面选的「看哪一段」判断")
        majors = f3.text_input("专业包含", key="ov_major", placeholder="统计、计算机")
        certs = f4.text_input("证书", key="ov_cert", placeholder="英语六级",
                              help="只筛选、不排序。没写要求的证书算不符合；推断的学历只标「待确认」。不提供性别、年龄等条件。")
    filters = {"最低学历": degree, "院校": school, "专业": _split(majors), "证书": _split(certs)}
    ranked, failed, abnormal = overview.rank(profiles, dims, mode, filters, req, kb)
    # 设了硬性要求就在表格上方说清楚结果，不然不符合的人折叠在下面，看起来像"没反应"
    text = "；".join(f"{k}：{'、'.join(v) if isinstance(v, list) else v}" for k, v in filters.items() if v and v != "不限")
    if text:
        (st.warning if failed else st.success)(
            f"{text}：{len(ranked)} 人符合" + (f"，{len(failed)} 人不符合（见表格下方）" if failed else ""))

    if ranked:
        columns = ["层", "简历", "决定", "为什么", *dims,
                   *[c for c in screening.OVERVIEW_COLUMNS if c not in dims and (c != "岗位匹配" or req)], "待确认"]
        table = pd.DataFrame([{**{c: r.get(c, "") for c in columns}, "简历": Path(r["文件"]).stem,
                               "决定": decisions.get(r["文件"], ""), "待确认": "；".join(r["待确认"])} for r in ranked],
                             columns=columns)
        # 按层交替底色：底色相同、连在一起的是同一层，层内不分先后
        shade = lambda row: ["background-color: #F7F8FA" if row["层"] % 2 == 0 else ""] * len(row)
        layers = ranked[-1]["层"]
        first = sum(r["层"] == 1 for r in ranked)
        st.markdown(f"**{len(ranked)} 人 · {layers} 层**", help=f"{screening.LAYER_RULE}。点选一行查看完整分析。")
        if len(ranked) >= 4 and first * 2 > len(ranked):
            st.caption(f"第 1 层有 {first} 人，可以减少分层依据")
        # 每次打开完整分析后换一个表格编号，清掉选中状态，同一行可以再次点开
        event = st.dataframe(table.style.apply(shade, axis=1), hide_index=True, on_select="rerun",
                             selection_mode="single-row", key=f"ov_table{state.get('ov_table_id', 0)}",
                             column_config={"层": st.column_config.NumberColumn(width="small"),
                                            "决定": st.column_config.TextColumn(width="small"),
                                            "为什么": st.column_config.TextColumn(width="medium"),
                                            **{d: st.column_config.TextColumn(f"★ {d}") for d in dims}})
        rows = event.selection.rows if event else []
        if rows:
            state.open_report = ranked[rows[0]]["文件"]
            state.ov_table_id = state.get("ov_table_id", 0) + 1
    if failed:
        with st.expander(f":orange[硬性要求不符 {len(failed)} 人]"):
            for r in failed:
                st.markdown(f"- **{Path(r['文件']).stem}**：{'；'.join(r['不符'])}")
    # 需人工查看的人不参与分层，没有表格行可点，所以在提示旁边放"查看原简历"，看完可以直接标记决定
    for r in abnormal:
        name = r["文件"]
        has_file = bool(reports.get(name, {}).get("data"))
        marked = f"（已标记：{decisions[name]}）" if name in decisions else ""
        c1, c2 = st.columns([6, 1], vertical_alignment="center")
        c1.warning(f"{name}：{'；'.join(r['解析异常'])}，请查看原简历{marked}", icon="⚠️")
        if c2.button("查看原简历", key=f"original_{name}", disabled=not has_file, width="stretch",
                     help=None if has_file else NO_ORIGINAL):
            _original_dialog(name, reports[name], r["解析异常"])
    st.download_button("导出 CSV", screening.overview_csv(ranked, failed, abnormal, decisions).encode("utf-8-sig"),
                       file_name="简历总览.csv", mime="text/csv", key="ov_csv", icon=":material/download:")
    return [r["文件"] for r in ranked], {r["文件"]: ("总览", r) for r in ranked}


def _split(text):
    return [s.strip() for s in (text or "").replace("，", "、").replace(",", "、").split("、") if s.strip()]


def _queues(profiles, reports, kb):
    """返回 (阅读顺序, {文件名: 筛选结果})，给"下一份"和"为什么在这里"用。"""
    req = st.session_state.req
    results, hints = match.screen(profiles, req, kb)
    cols = st.columns(len(match.QUEUES))
    for col, queue in zip(cols, match.QUEUES):
        col.metric(f":{QUEUE_STYLE[queue][0]}-badge[{queue}]", f"{sum(r['队列'] == queue for r in results)} 人", border=True)
    for h in hints:
        st.caption(h)
    st.caption("● 做过　◐ 自述 / 课程　○ 没体现")

    order = []
    for queue in match.QUEUES:
        group = [r for r in results if r["队列"] == queue]
        if not group:
            continue
        order += [r["文件"] for r in group]
        color, meaning = QUEUE_STYLE[queue]
        st.markdown(f"#### :{color}-badge[{queue}] {len(group)} 人",
                    help=meaning + ("；按证据强弱排列" if queue in screening.RANKED else ""))
        # 前两个队列直接展开理由；后面的收起来，需要时再看
        if queue in ("优先看", "值得看"):
            for i, r in enumerate(group):
                _card(r, reports, f"{queue}{i}")
        else:
            with st.expander(f"展开 {len(group)} 人"):
                for i, r in enumerate(group):
                    _card(r, reports, f"{queue}{i}")

    st.download_button("导出 CSV", screening.csv_text(results, st.session_state.decisions).encode("utf-8-sig"),
                       file_name=f"{_safe(req['岗位'])}_筛选结果.csv", mime="text/csv", icon=":material/download:")
    return order, {r["文件"]: ("阅读队列", r) for r in results}


def _card(r, reports, key):
    with st.container(border=True):
        c1, c2 = st.columns([5, 1], vertical_alignment="center")
        name = Path(r["文件"]).stem
        chips = "　".join(f"{GRADE_MARK[a['grade']]} {a['name']}" for a in r["最看重"])
        badge = DECISION_BADGE.get(st.session_state.decisions.get(r["文件"]), "")
        c1.markdown(f"**{name}**　{badge}　<span style='color:#6B7280'>{chips}</span>", unsafe_allow_html=True)
        if c2.button("完整分析", key=f"open_{key}", disabled=r["文件"] not in reports):
            st.session_state.open_report = r["文件"]
        _queue_detail(r)


def _queue_detail(r):
    """阅读队列里一个人的理由：卡片上和完整分析的"为什么在这里"共用。"""
    for f in r["硬条件不符"]:
        st.markdown(f":orange[硬条件不符：{f}]")
    for reason in r["解析"]:
        st.markdown(f":violet[解析异常：{reason}，请直接打开原文件]")
    for m in r["做过类似的事"]:
        st.markdown(f"✔ **做过类似的事**：职责「{screening.short(m['duty'], 26)}」  \n"
                    f"<span style='color:#6B7280'>↳ {m['where']}（{m['context']}·{LEVEL_NAMES[m['level']]}）"
                    f"“{screening.short(m['text'], 60)}”</span>", unsafe_allow_html=True)
    proven = [a for a in r["最看重"] if a["grade"] != "没体现"]
    if proven:
        st.markdown("**最看重的能力**：" + "；".join(screening.ability_text(a) for a in proven))
    if r["加分"]:
        st.markdown("**加分**：" + "、".join(r["加分"]))
    if r["电话问题"]:
        st.markdown("**电话初筛可以问**  \n" + "  \n".join(f"{i}. {q}" for i, q in enumerate(r["电话问题"], 1)))


def _overview_detail(r):
    """总览里一个人的位置和各项情况。"""
    st.markdown(f"**第 {r['层']} 层**　{_md(r['为什么'])}")
    for c in screening.OVERVIEW_COLUMNS:
        if r.get(c):
            c1, c2 = st.columns([1, 5])
            c1.markdown(f"**{c}**")
            c2.markdown(_md(r[c]))
    if r["待确认"]:
        st.warning("待确认：" + "；".join(r["待确认"]), icon="⚠️")


@st.dialog("完整分析", width="large", on_dismiss="rerun")
def _report_dialog(name, analysis, order, why):
    state = st.session_state
    current = state.decisions.get(name)
    cols = st.columns([4, 1, 1, 1, 1.3], vertical_alignment="center")
    cols[0].subheader(Path(name).stem)
    for col, decision in zip(cols[1:4], DECISIONS):
        if col.button(decision, type="primary" if current == decision else "secondary", width="stretch",
                      key=f"decide_{decision}"):
            state.decisions[name] = decision
            _open_next(name, order)
    if cols[4].button("下一份", icon=":material/arrow_forward:", width="stretch", key="next_report"):
        _open_next(name, order)
    _render_report(analysis, why)


@st.dialog("原简历", width="large", on_dismiss="rerun")
def _original_dialog(name, analysis, reasons):
    """需人工查看的人：只看原简历并标记决定。这类简历没有排序结果，完整分析里的模块可能是空的，
    所以不进完整分析；也不在阅读顺序里，没有"下一份"。关闭（Esc / 点外部 / ×）后刷新，更新已处理人数。"""
    state = st.session_state
    current = state.decisions.get(name)
    cols = st.columns([4, 1, 1, 1], vertical_alignment="center")
    cols[0].subheader(Path(name).stem)
    for col, decision in zip(cols[1:], DECISIONS):
        if col.button(decision, type="primary" if current == decision else "secondary", width="stretch",
                      key=f"original_decide_{decision}"):
            state.decisions[name] = decision
            st.rerun()
    st.warning("需人工查看：" + "；".join(reasons), icon="⚠️")
    _original(analysis)


def _open_next(name, order):
    """打开当前列表里、排在这个人后面的下一位还没处理的人；后面没有了就从头找。"""
    state = st.session_state
    left = [n for n in order if n != name and n not in state.decisions]
    after = order[order.index(name) + 1:] if name in order else []
    nxt = next((n for n in after if n in left), left[0] if left else None)
    if nxt:
        state.open_report = nxt
    else:
        state.done_toast = "这个列表里的人都处理完了"
    st.rerun()


# ---------- 完整分析的显示（只改显示方式，内容和命令行的文字报告完全相同） ----------

# 顶部摘要卡片：(模块, 报告里的标签, 卡片名称)
SUMMARY_CARDS = (("教育背景", "院校层次", "学历与院校"), ("教育背景", "学业表现", "学业表现"),
                 ("实习经历", "工作深度", "实习做到"), ("项目与技能", "项目深度", "项目做到"),
                 ("项目与技能", "技能深度", "技能深度"))
LAST_MODULES = ("基本信息",)   # 籍贯、现居城市等和能力无关，放在最后，避免先入为主


def _render_report(analysis, why=None):
    """顶部摘要卡片 + 分页：为什么在这里 → 各模块（基本信息放最后）→ 原简历 → 纯文字版。
    需要核实的提示用黄色提示框放在每页最上面；纯文字版方便复制。"""
    results = analysis.get("results")
    if not results:
        st.code(analysis.get("report", "（没有报告）"), language=None, wrap_lines=True)
        return
    if analysis.get("note"):
        st.caption(f"文字来源：{analysis['note']}")
    values = {(r.title, label): value for r in results for label, value in r.items if label}
    for col, (module, label, name) in zip(st.columns(len(SUMMARY_CARDS)), SUMMARY_CARDS):
        with col.container(border=True):
            st.caption(name)
            st.markdown(f"**{_md(_headline(values.get((module, label), '—')))}**")
    results = [r for r in results if r.title not in LAST_MODULES] + [r for r in results if r.title in LAST_MODULES]
    # 需要核实的提示用黄色提示框；"不代表素质高低"这类说明文字不是问题，放在页面底部
    warnings = {id(r): [n for n in r.notes if not _is_explanation(n)] for r in results}
    titles = [r.title + (f"（⚠ {len(warnings[id(r)])}）" if warnings[id(r)] else "") for r in results]
    tabs = st.tabs((["为什么在这里"] if why else []) + titles + ["原简历", "纯文字版"])
    if why:
        with tabs[0]:
            kind, row = why
            if kind == "阅读队列":
                _queue_detail(row)
            else:
                _overview_detail(row)
        tabs = tabs[1:]
    for tab, r in zip(tabs, results):
        with tab:
            for note in warnings[id(r)]:
                st.warning(_md(note), icon="⚠️")
            blocks = _blocks(r.items)
            if blocks:
                _summary_rows(blocks[0])
            for block in blocks[1:]:
                with st.container(border=True):
                    _entry_rows(block)
            for note in r.notes:
                if _is_explanation(note):
                    st.caption(_md(note))
    with tabs[-2]:
        _original(analysis)
    with tabs[-1]:
        st.code(analysis["report"], language=None, wrap_lines=True)
    st.caption(DISCLAIMER)


def _original(analysis):
    """原简历：PDF 逐页显示成图片，图片直接显示，Word / TXT 显示读出来的文字。
    全部在内存里处理，不写临时文件（服务器进程被强行中断也不会留下简历内容）。
    「完整分析」的原简历页和「需人工查看」的查看原简历弹窗共用这个函数。"""
    data, name = analysis.get("data"), analysis["name"]
    if not data:
        st.caption(NO_ORIGINAL)
        return
    suffix = Path(name).suffix.lower()
    try:
        if suffix == ".pdf":
            for page in pdf_pages(data):
                st.image(page, width="stretch")
        elif suffix in IMAGE_SUFFIXES:
            st.image(data, width="stretch")
        else:
            st.code(read_bytes(name, data), language=None, wrap_lines=True)
    except Exception as e:
        st.caption(f"原文件无法显示：{e}")


def _is_explanation(note):
    """说明文字（解释怎么读结果），不是需要核实的问题。"""
    return note.startswith("以上是")


def _headline(value):
    """摘要卡片只放结论："硕士 · 双非第一梯队（华东政法大学，按……折算）" → "硕士 · 双非第一梯队"；短括号保留（"中（独立负责）"）。"""
    first = value.split("｜")[0].strip()
    head, _, rest = first.partition("（")
    return first if not rest or len(rest) <= 10 else head.strip()


def _blocks(items):
    """按空行把一个模块分成几块：第一块是维度摘要，后面每块是一段经历；标签为空的行接在上一行下面。"""
    blocks, rows = [], []
    for label, value in items:
        if not label and not value:
            if rows:
                blocks.append(rows)
            rows = []
        elif not label and rows:
            rows[-1][1].append(value)
        else:
            rows.append((label, [value]))
    if rows:
        blocks.append(rows)
    return blocks


def _summary_rows(rows):
    for label, lines in rows:
        c1, c2 = st.columns([1, 5])
        c1.markdown(f"**{_md(label)}**")
        c2.markdown(_md(lines[0]))
        for line in lines[1:]:
            c2.caption(_md(_tidy(line)))


def _entry_rows(rows):
    """一段经历：第一行作标题，其余是细节。"""
    (label, lines), rest = rows[0], rows[1:]
    st.markdown(f"**{_md(label)}**　{_md(lines[0])}")
    for line in lines[1:]:
        st.caption(_md(_tidy(line)))
    for label, lines in rest:
        st.markdown(f"{_md(label)}：{_md(lines[0])}" if label else _md(lines[0]))
        for line in lines[1:]:
            st.caption(_md(_tidy(line)))


def _tidy(line):
    """去掉文字报告里用来对齐的全角空格，"└"换成箭头。"""
    return line.strip("　 ").replace("└ ", "↳ ").replace("└", "↳ ")


def _md(text):
    """网页用 Markdown 显示，报告原文里的 * _ ~ $ 等符号要转义，否则会变成加粗、删除线或公式。"""
    for ch in "\\`*_~$[]<>|#":
        text = text.replace(ch, "\\" + ch)
    return text


main()

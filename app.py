"""网页版：给一份 JD 和一批简历，告诉 HR 先看谁、为什么。

运行：streamlit run app.py
页面只负责展示和收集 HR 的修改，判断全部复用命令行版的代码（matching/、modules/），两边结果一致。
"""
import hashlib
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
from modules.experience import LEVEL_NAMES
from report import screening
from report.formatter import DISCLAIMER, format_report

ROOT = Path(__file__).resolve().parent
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


@st.cache_resource
def services(api_key):
    """知识库 + 两个 AI 客户端：简历用的（缓存 30 天删除）、JD 用的（缓存长期保留）。"""
    return KnowledgeBase(), LLMClient(api_key=api_key), LLMClient(cache="jd", api_key=api_key)


def _sidebar():
    """AI 服务设置和隐私操作。返回网页里输入的 API Key（没有输入则为 None，用本机 .env 里的）。"""
    state = st.session_state
    saved = bool(read_env_file().get("DEEPSEEK_API_KEY") or os.environ.get("DEEPSEEK_API_KEY"))
    with st.sidebar:
        st.header("⚙️ 设置")
        st.subheader("AI 服务（DeepSeek）")
        if state.get("api_key"):
            st.success("正在使用本次输入的 API Key", icon="🔑")
        elif saved:
            st.success("正在使用本机保存的 API Key", icon="🔑")
        else:
            st.warning("还没有 API Key，请在下面填写", icon="🔑")
        with st.expander("填写 / 更换 API Key", expanded=not (saved or state.get("api_key"))):
            key = st.text_input("API Key", type="password", placeholder="sk-…", key="key_input",
                                help="在 DeepSeek 开放平台（platform.deepseek.com）创建")
            remember = st.checkbox("保存到本机（写入 .env，下次不用再填）", key="key_remember")
            if st.button("测试并使用", disabled=not key.strip()):
                try:
                    OpenAI(api_key=key.strip(), base_url=BASE_URL, timeout=20).models.list()
                except OpenAIError:
                    # 不把接口返回的原始信息显示出来：里面可能带着部分 Key
                    st.error("连接失败：Key 无效，或网络连不上 DeepSeek")
                else:
                    state.api_key = key.strip()
                    if remember:
                        save_env({"DEEPSEEK_API_KEY": key.strip()})
                    state.pop("key_input", None)
                    st.rerun()
            st.caption("默认只在这次打开的页面里使用，关闭页面就会忘记；勾选才会保存到本机。Key 不会显示在页面上。")

        st.subheader("隐私")
        st.caption(f"上传的简历文件分析完立即删除；AI 从简历里提取的内容缓存在本机，**{RESUME_CACHE_DAYS} 天后自动删除**。"
                   "手机号、邮箱、身份证号在发送给 AI 前已去掉。")
        if st.button("立即清除所有简历缓存"):
            n = clear_resume_cache()
            st.cache_resource.clear()        # 内存里的分析结果也一起清掉
            for k in ("analyses", "selected"):
                state.pop(k, None)
            st.success(f"已清除 {n} 份简历缓存和本页的分析结果")
    return state.get("api_key")


def main():
    st.set_page_config(page_title="简历初筛助手", page_icon="📋", layout="wide")
    st.title("📋 简历初筛助手")
    st.caption("上传一批简历，按你关心的几项排序，快速决定**先看谁**；有岗位 JD 的话，还能看出谁做过这个岗位要做的事。"
               "工具只决定阅读顺序，不打分、不替你做录用决定。")
    api_key = _sidebar()
    try:
        kb, llm, jd_llm = services(api_key)
    except RuntimeError:
        st.info("请先在左侧「设置」里填写 DeepSeek 的 API Key。", icon="👈")
        st.stop()

    state = st.session_state
    state.setdefault("analyses", {})
    _progress_bar()
    st.divider()
    step_job(kb, jd_llm)
    st.divider()
    step_resumes(kb, llm)
    st.divider()
    step_results(kb)


def _progress_bar():
    state = st.session_state
    marks = ["✅" if state.get("req") else "➖", "✅" if state.get("selected") else "⬜", "✅" if state.get("selected") else "⬜"]
    cols = st.columns(3)
    for col, mark, name in zip(cols, marks, ("① 岗位要求（可选）", "② 上传简历并分析", "③ 排序和阅读队列")):
        col.markdown(f"{mark} **{name}**")


# ---------- ① 岗位要求 ----------

def step_job(kb, llm):
    state = st.session_state
    st.header("① 岗位要求（可选）")
    st.caption("不导入 JD 也可以直接上传简历，按学校、实践经历、技能等排序；导入并确认 JD 后，还能按「岗位匹配」排序、看阅读队列和理由。")
    source = st.segmented_control("JD 来源", ["粘贴新的 JD", "打开已保存的岗位"], default="粘贴新的 JD",
                                  label_visibility="collapsed")
    if source == "打开已保存的岗位":
        saved = sorted(p for p in JOBS_DIR.glob("*.json"))
        if not saved:
            st.info("还没有保存过岗位，请先粘贴一份 JD。")
            return
        pick = st.selectbox("已保存的岗位", [p.stem for p in saved])
        if st.button("打开"):
            req, warnings = jd.load(JOBS_DIR / f"{pick}.json", kb)
            _set_draft(req, warnings)
    else:
        c1, c2 = st.columns([3, 1])
        c2.selectbox("没有 JD？用示例填入", ["—"] + [p.stem for p in JD_SAMPLES], key="jd_example", on_change=_fill_example)
        title = c1.text_input("职位名称", key="jd_title", placeholder="例如：数据分析工程师")
        text = st.text_area("岗位 JD（岗位职责 + 任职要求）", key="jd_text", height=200,
                            placeholder="把招聘网站上的岗位描述整段粘贴进来")
        if st.button("解析 JD", type="primary", disabled=not (title.strip() and text.strip())):
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


def _edit_form(kb):
    """把工具对 JD 的理解做成表单让 HR 改；改完经过和 txt 文件相同的读取、核对流程。"""
    state = st.session_state
    base = state.draft
    fid = state.form_id
    with st.container(border=True):
        st.subheader(f"工具的理解：{base['岗位']}")
        refs = "、".join(f"{r['岗位']}（相似度{r['相似度']}）" for r in base["参考岗位"]) or "知识库里没有相近岗位"
        st.caption(f"参考岗位：{refs}。以下内容可以直接修改，改完点最下面的「确认」。")

        c1, c2 = st.columns([1, 3])
        degree = c1.selectbox("学历门槛", DEGREE_OPTIONS, index=DEGREE_OPTIONS.index(base["门槛"]["学历"]), key=f"degree{fid}",
                              help="只按简历里写明的学历判断；推断出来的只会进「待确认」")

        st.markdown("**岗位职责** — 用来找「做过类似事」的人")
        st.caption("简历里同一句经历出现 2 个关键词，或 1 个少见的关键词且领域相同，就算做过。关键词用「、」分开；空泛的职责取消勾选。")
        duties = st.data_editor(
            pd.DataFrame([{"参与匹配": not d["空泛"], "职责": d["职责"], "关键词": "、".join(d["关键词"])} for d in base["职责"]]),
            column_config={"参与匹配": st.column_config.CheckboxColumn(width="small"),
                           "职责": st.column_config.TextColumn(disabled=True, width="large"),
                           "关键词": st.column_config.TextColumn(width="medium")},
            hide_index=True, key=f"duties{fid}")

        st.markdown("**能力要求**")
        st.caption("「基础要求」是大多数岗位都写的（如 Office），区分不出人，不参与排序。可以在最后一行添加 JD 没写但你们看重的能力。")
        reqs = st.data_editor(
            pd.DataFrame([{"名称": r["名称"], "类别": "基础要求" if r["基础要求"] else ("必须" if r["必须"] else "加分"),
                           "程度": r["程度"], "领域": r["领域"]} for r in base["要求"]]),
            column_config={"名称": st.column_config.TextColumn(required=True),
                           "类别": st.column_config.SelectboxColumn(options=KINDS, required=True, default="加分"),
                           "程度": st.column_config.SelectboxColumn(options=["", "了解", "熟悉", "掌握", "熟练", "精通"]),
                           "领域": st.column_config.TextColumn(disabled=True, help="由能力词典自动归类")},
            hide_index=True, num_rows="dynamic", key=f"reqs{fid}")
        names = [n for n, k in zip(reqs["名称"], reqs["类别"]) if isinstance(n, str) and n.strip() and k != "基础要求"]
        top = st.multiselect("最看重（按选择顺序，最多 3 项）— 决定阅读顺序", names,
                             default=[n for n in base["最看重"] if n in names], max_selections=3, key=f"top{fid}")

        c1, c2 = st.columns(2)
        majors, majors_must = _list_input(c1, "专业", base["专业"], fid)
        certs, certs_must = _list_input(c2, "证书", base["证书"], fid)

        qualities = st.multiselect(
            "素质", QUALITIES, default=[q["素质"] for q in base["素质"]], key=f"qualities{fid}",
            help=f"简历能看出的（{'、'.join(sorted(RESUME_JUDGEABLE))}）只用于排序参考；其余需面试考察")

        st.markdown("**补充条件** — JD 没写、但你们看重的")
        st.caption("默认算加分，勾选「必须」才当门槛。性别、年龄、婚育、籍贯等不能作为筛选条件，这里不提供。")
        extras = st.data_editor(
            pd.DataFrame([{"类型": c["类型"], "内容": c["值"], "必须": c["必须"]} for c in base["补充条件"]],
                         columns=["类型", "内容", "必须"]),
            column_config={"类型": st.column_config.SelectboxColumn(options=list(jd.EXTRA_TYPES), required=True),
                           "内容": st.column_config.TextColumn(help="院校：211 及以上 / 实习：有 或 某方向 / 证书：CPA"),
                           "必须": st.column_config.CheckboxColumn(default=False)},
            hide_index=True, num_rows="dynamic", key=f"extras{fid}")

        edited = _form_to_req(base, degree, duties, reqs, top, majors, majors_must, certs, certs_must, qualities, extras)
        text = jd.to_text(edited, f"jobs/{_safe(base['岗位'])}.txt")
        req, warnings = jd.apply_text(base, text, kb)
        for w in state.draft_warnings + warnings:
            st.warning(w)
        if st.button("确认岗位要求", type="primary", disabled=not req["最看重"]):
            _save_job(req, original=False)
            state.req = req
            st.toast("岗位要求已确认，并保存到 jobs/ 文件夹")
        if not req["最看重"]:
            st.caption("请至少选择 1 项「最看重」")
        elif state.get("req") == req:
            st.success("已确认。修改后需要重新点「确认」。")


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
        r.update({"程度": level if isinstance(level, str) else "", "必须": kind == "必须", "基础要求": kind == "基础要求"})
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
    """和命令行版用同一套文件：json 是工具的原始理解，txt 是 HR 改过的版本。"""
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
    st.header("② 简历")
    c1, c2 = st.columns([3, 2])
    uploads = c1.file_uploader("上传简历（可多选）", type=UPLOAD_TYPES, accept_multiple_files=True,
                               help="支持 PDF、Word（.docx）、TXT 和图片")
    with c2:
        use_samples = st.toggle(f"加入示例简历（虚构，{len(RESUME_SAMPLES)} 份）")
        st.caption(f"隐私：发送给 AI 前会去掉手机号、邮箱、身份证号；上传的文件分析完即删除，"
                   f"AI 提取的结果缓存在本机，{RESUME_CACHE_DAYS} 天后自动删除（左侧可随时清除）。")
    items = [(f.name, f.getvalue()) for f in uploads or []]
    if use_samples:
        items += [(p.name, p.read_bytes()) for p in RESUME_SAMPLES]

    if st.button(f"开始分析 {len(items)} 份简历" if items else "开始分析", type="primary", disabled=not items):
        state.selected = _analyze(items, kb, llm)
    if not items:
        st.caption("请上传简历，或打开「加入示例简历」。")


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
                futures[pool.submit(analyzer.run_modules, path, context)] = (k, name)
            for n, future in enumerate(as_completed(futures), 1):
                k, name = futures[future]
                try:
                    results, profile = future.result()
                    note = profile["parse"]["source"] if profile["parse"]["source"] != "文字" else None
                    cache[k] = {"name": name, "profile": profile, "results": results, "note": note,
                                "report": format_report(name, results, note)}
                except Exception as e:   # 单份失败不影响其他简历
                    cache[k] = {"name": name, "error": str(e)}
                bar.progress(n / len(todo), text=f"正在分析 {n} / {len(todo)} 份")
        bar.empty()
    return list(dict.fromkeys(keys))


# ---------- ③ 结果 ----------

def step_results(kb):
    state = st.session_state
    st.header("③ 排序和阅读队列")
    keys = state.get("selected")
    if not keys:
        st.caption("分析完简历后，这里会给出排序总览；确认了岗位要求的话，还有阅读队列。")
        return
    items = [state.analyses[k] for k in keys]
    for a in items:
        if "error" in a:
            st.error(f"无法分析：{a['name']}：{a['error']}")
    profiles = [a["profile"] for a in items if "profile" in a]
    reports = {a["name"]: a for a in items if "report" in a}   # 文件名 → 分析结果（完整分析用）
    tab1, tab2 = st.tabs(["📊 总览排序", "🎯 阅读队列（需要岗位要求）"])
    with tab1:
        _overview(profiles, reports, kb)
    with tab2:
        if state.get("req"):
            _queues(profiles, reports, kb)
        else:
            st.info("在第 ① 步导入并确认岗位要求后，这里会按「谁做过这个岗位要做的事」给出阅读队列和理由。")


def _overview(profiles, reports, kb):
    state = st.session_state
    req = state.get("req")
    options = [d for d in overview.DIMENSIONS if d != "岗位匹配" or req]
    c1, c2 = st.columns([3, 2])
    dims = c1.multiselect("分层依据（建议 2～3 项）", options, default=list(overview.DEFAULT_DIMENSIONS), key="ov_dims",
                          help="只有在所选每一项上都不比别人差、且至少一项更好，才排在前面；否则各有所长，放在同一层。"
                               "选择的先后不影响结果。「学历」更适合当门槛（在下面的硬性要求里设）；「岗位匹配」需要先确认岗位要求")
    mode = c2.radio("学校层次看哪一段", overview.SCHOOL_MODES, horizontal=True, key="ov_mode",
                    help="读过硕士的人有两段学校。先看的一段决定层次，另一段在相同时再比较")
    with st.expander("硬性要求（只筛选，不排序）"):
        f1, f2, f3, f4 = st.columns(4)
        degree = f1.selectbox("最低学历", ["不限", *overview.DEGREES], key="ov_degree")
        school = f2.selectbox("院校层次", list(overview.SCHOOL_LIMITS), key="ov_school", help="按上面选的「看哪一段」判断")
        majors = f3.text_input("专业包含（用「、」分开）", key="ov_major", placeholder="例如：统计、计算机")
        certs = f4.text_input("证书（用「、」分开）", key="ov_cert", placeholder="例如：英语六级")
        st.caption("不符合的单独列在表格下方；简历里没写要求的证书也算不符合。学历是推断的只标「待确认」，不会被排除。"
                   "性别、年龄等不能作为筛选条件，这里不提供。")
    filters = {"最低学历": degree, "院校": school, "专业": _split(majors), "证书": _split(certs)}
    ranked, failed, abnormal = overview.rank(profiles, dims, mode, filters, req, kb)
    # 设了硬性要求就在表格上方说清楚结果，不然不符合的人折叠在下面，看起来像"没反应"
    text = "；".join(f"{k}：{'、'.join(v) if isinstance(v, list) else v}" for k, v in filters.items() if v and v != "不限")
    if text:
        (st.warning if failed else st.success)(
            f"硬性要求（{text}）：**{len(ranked)} 人符合**" + (f"，**{len(failed)} 人不符合**（列在表格下方）" if failed else "，全部符合"),
            icon="🔎")

    if ranked:
        columns = ["层", "简历", "为什么", *dims,
                   *[c for c in screening.OVERVIEW_COLUMNS if c not in dims and (c != "岗位匹配" or req)], "待确认"]
        table = pd.DataFrame([{**{c: r.get(c, "") for c in columns}, "简历": Path(r["文件"]).stem,
                               "待确认": "；".join(r["待确认"])} for r in ranked], columns=columns)
        # 按层交替底色：底色相同、连在一起的是同一层，层内不分先后
        shade = lambda row: ["background-color: #F4F6FA" if row["层"] % 2 == 0 else ""] * len(row)
        layers = ranked[-1]["层"]
        first = sum(r["层"] == 1 for r in ranked)
        st.caption(f"共 {len(ranked)} 人，分成 {layers} 层。{screening.LAYER_RULE}。点选一行，下方显示这个人的完整分析。")
        if len(ranked) >= 4 and first * 2 > len(ranked):
            st.info(f"第 1 层有 {first} / {len(ranked)} 人：勾选的项越多，各有所长的人就越多。想分得更清楚，可以减少到 2～3 项。", icon="💡")
        event = st.dataframe(table.style.apply(shade, axis=1), hide_index=True, on_select="rerun",
                             selection_mode="single-row", key="ov_table",
                             column_config={"层": st.column_config.NumberColumn(width="small"),
                                            "为什么": st.column_config.TextColumn(width="medium"),
                                            **{d: st.column_config.TextColumn(f"★ {d}") for d in dims}})
        rows = event.selection.rows if event else []
        if rows:
            name = ranked[rows[0]]["文件"]
            with st.container(border=True):
                st.subheader(f"{Path(name).stem} 的完整分析")
                if name in reports:
                    _render_report(reports[name])
                else:
                    st.caption("没有报告")
    if failed:
        with st.expander(f":orange[硬性要求不符 {len(failed)} 人]"):
            for r in failed:
                st.markdown(f"- **{Path(r['文件']).stem}**：{'；'.join(r['不符'])}")
    for r in abnormal:
        st.warning(f"{r['文件']}：{'；'.join(r['解析异常'])}，请直接打开原文件查看", icon="⚠️")
    st.download_button("下载总览表格（CSV，Excel 可直接打开）", screening.overview_csv(ranked, failed, abnormal).encode("utf-8-sig"),
                       file_name="简历总览.csv", mime="text/csv", key="ov_csv")
    st.caption("档位来自简历里写明的证据：没写 ≠ 不会；通用素质是证据强度，不代表素质高低。" + screening.COMPLIANCE)


def _split(text):
    return [s.strip() for s in (text or "").replace("，", "、").replace(",", "、").split("、") if s.strip()]


def _queues(profiles, reports, kb):
    req = st.session_state.req
    results, hints = match.screen(profiles, req, kb)
    cols = st.columns(len(match.QUEUES))
    for col, queue in zip(cols, match.QUEUES):
        col.metric(queue, f"{sum(r['队列'] == queue for r in results)} 人")
    for h in hints:
        st.info(h, icon="💡")
    st.caption("证据强度：● 在实习 / 项目 / 校园经历里做过　◐ 只在技能栏自述或学过课程　○ 简历没体现（不等于不会）")

    for queue in match.QUEUES:
        group = [r for r in results if r["队列"] == queue]
        if not group:
            continue
        color, meaning = QUEUE_STYLE[queue]
        st.markdown(f"### :{color}-badge[{queue}] {len(group)} 人")
        st.caption(meaning + ("；按证据强弱排列" if queue in screening.RANKED else ""))
        # 前两个队列直接展开理由；后面的收起来，需要时再看
        if queue in ("优先看", "值得看"):
            for i, r in enumerate(group):
                _card(r, reports, f"{queue}{i}")
        else:
            with st.expander(f"展开 {len(group)} 人"):
                for i, r in enumerate(group):
                    _card(r, reports, f"{queue}{i}")

    st.download_button("下载结果表格（CSV，Excel 可直接打开）", screening.csv_text(results).encode("utf-8-sig"),
                       file_name=f"{_safe(req['岗位'])}_筛选结果.csv", mime="text/csv")
    st.caption(f"{screening.NOTE}　{screening.COMPLIANCE}　{DISCLAIMER}")


def _card(r, reports, key):
    with st.container(border=True):
        c1, c2 = st.columns([5, 1], vertical_alignment="center")
        name = Path(r["文件"]).stem
        chips = "　".join(f"{GRADE_MARK[a['grade']]} {a['name']}" for a in r["最看重"])
        c1.markdown(f"**{name}**　　<span style='color:#6B7280'>{chips}</span>", unsafe_allow_html=True)
        if c2.button("完整分析", key=f"open_{key}", disabled=r["文件"] not in reports):
            _report_dialog(r["文件"], reports[r["文件"]])
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


@st.dialog("完整分析", width="large")
def _report_dialog(name, analysis):
    st.subheader(Path(name).stem)
    _render_report(analysis)


# ---------- 完整分析的显示（只改显示方式，内容和命令行的文字报告完全相同） ----------

# 顶部摘要卡片：(模块, 报告里的标签, 卡片名称)
SUMMARY_CARDS = (("教育背景", "院校层次", "学历与院校"), ("教育背景", "学业表现", "学业表现"),
                 ("实习经历", "工作深度", "实习做到"), ("项目与技能", "项目深度", "项目做到"),
                 ("项目与技能", "技能深度", "技能深度"))


def _render_report(analysis):
    """顶部摘要卡片 + 按模块分标签页；需要核实的提示用黄色提示框放在每页最上面；最后一页是纯文字版，方便复制。"""
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
    # 需要核实的提示用黄色提示框；"不代表素质高低"这类说明文字不是问题，放在页面底部
    warnings = {id(r): [n for n in r.notes if not _is_explanation(n)] for r in results}
    tabs = st.tabs([r.title + (f"（⚠ {len(warnings[id(r)])}）" if warnings[id(r)] else "") for r in results] + ["纯文字版"])
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
    with tabs[-1]:
        st.code(analysis["report"], language=None, wrap_lines=True)
    st.caption(DISCLAIMER)


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

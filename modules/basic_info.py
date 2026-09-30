"""基本信息模块：籍贯、现居城市与应聘地点不一致时提醒面试官。只做提醒，不参与评价。"""
import re

from modules.base import Module, ModuleResult, clean_text as _text

SYSTEM_PROMPT = """你是简历信息提取助手。从用户给出的简历文本中提取以下信息，只输出 JSON。
只提取简历中明确写出的内容，没写的字段填 null，不要推测。

{
  "hometown": "籍贯 / 生源地 / 户籍所在地，原文，如 \\"湖南长沙\\"",
  "residence": "现居住城市，原文",
  "expected_cities": ["求职意向中的期望工作城市，原文"],
  "schools": [{"name": "各段正式学历（专科/本科/硕士/博士）的学校全称，不含交换、访学、夏令营的学校", "city": "该校所在城市（这一项可以根据常识填写，不确定填 null）"}]
}"""

PLACE_SUFFIX = re.compile(r"省|市|自治区|特别行政区|壮族|回族|维吾尔")


class BasicInfoModule(Module):
    title = "基本信息"

    def analyze(self, resume, context):
        kb, llm = context["kb"], context["llm"]
        data = llm.extract_json(SYSTEM_PROMPT, resume.text)
        result = ModuleResult(self.title, extracted=data)

        hometown = _text(data.get("hometown"))
        residence = _text(data.get("residence"))
        expected = [c for c in (_text(c) for c in data.get("expected_cities") or []) if c]
        # 学校所在城市：优先用知识库，知识库没有时用 AI 根据常识补充的城市
        school_cities, ai_filled = [], False
        for school in data.get("schools") or []:
            if not isinstance(school, dict):
                continue
            match = kb.match_school(_text(school.get("name")))
            city = match.city if match and match.record else ""
            if not city:
                city = _text(school.get("city"))
                ai_filled = ai_filled or bool(city)
            if city and city not in school_cities:
                school_cities.append(city)

        # 应聘地点：优先用命令行指定的，其次用简历中的期望城市
        job_city = context.get("job_city")
        job_source = "指定"
        if not job_city and expected:
            job_city, job_source = expected[0], "简历期望城市"

        result.add("籍贯", hometown or "未注明")
        result.add("现居城市", residence or "未注明")
        result.add("学校所在地", "、".join(school_cities) + ("（部分由 AI 补充）" if ai_filled else "") if school_cities else "未知")
        result.add("应聘地点", f"{job_city}（{job_source}）" if job_city else "未指定")

        if job_city and hometown and not _same_place(hometown, job_city):
            same_province = kb.province_of(hometown) and kb.province_of(hometown) == kb.province_of(job_city)
            context_info = []
            if residence:
                relation = "一致" if _same_place(residence, job_city) else "不一致"
                context_info.append(f"现居{residence}，与应聘地点{relation}")
            local_schools = [c for c in school_cities if _same_place(c, job_city)]
            if local_schools:
                context_info.append(f"曾在{local_schools[0]}求学")
            extra = f"（{'；'.join(context_info)}）" if context_info else ""
            province_note = "，同省" if same_province else ""
            result.notes.append(
                f"籍贯{hometown}，应聘地点{job_city}{province_note}{extra}，建议面试时确认工作地点意向"
            )
        return result


def _same_place(a, b):
    a, b = PLACE_SUFFIX.sub("", a), PLACE_SUFFIX.sub("", b)
    return bool(a and b) and (a in b or b in a)


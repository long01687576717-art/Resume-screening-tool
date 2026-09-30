"""脱敏：发送给 AI 之前，去掉分析用不到的个人联系信息。"""
import re

RULES = [
    # 身份证号放在手机号前面，避免其中的数字片段被当成手机号
    ("[身份证号]", re.compile(r"(?<!\d)\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])\d{2}\d{3}[\dXx](?!\d)")),
    ("[手机号]", re.compile(r"(?<!\d)(?:\+?86[\s-]?)?1[3-9]\d(?:[\s-]?\d{4}){2}(?!\d)")),
    ("[邮箱]", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
]


def mask_personal_info(text):
    for placeholder, pattern in RULES:
        text = pattern.sub(placeholder, text)
    return text

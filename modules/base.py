"""分析模块的统一接口。新增模块时继承 Module，实现 analyze 即可。"""
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Resume:
    path: Path
    text: str
    source_note: str | None = None  # 文字来源说明（如用了图片识别）


@dataclass
class ModuleResult:
    title: str
    items: list[tuple[str, str]] = field(default_factory=list)  # (标签, 内容)，标签为空表示接上一行
    notes: list[str] = field(default_factory=list)              # 需要提醒面试官的事项
    extracted: dict = field(default_factory=dict)               # AI 提取的原始数据，便于核查
    # 候选人画像（给筛选用的结构化数据）：各模块填自己那部分，main.py 合并。
    # 其中 evidence 是统一的证据列表，每条：kind 能力类型、name 名称、domain 领域、level 程度、
    # context 情境（实习 / 项目 / 课程 / 自述……）、where 出自哪段经历、text 原句
    profile: dict = field(default_factory=dict)

    def add(self, label, value):
        self.items.append((label, value))


class Module:
    title = ""

    def analyze(self, resume: Resume, context: dict) -> ModuleResult:
        """context 包含 kb（知识库）、llm（AI 客户端）、job_city（应聘地点）。"""
        raise NotImplementedError


def clean_text(value):
    """把 AI 返回的字段整理成字符串；null、未注明等空值统一返回空字符串。"""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return "" if value in ("null", "None", "未注明", "无") else value

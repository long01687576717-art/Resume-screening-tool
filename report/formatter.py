"""把各模块的分析结果整理成文字报告。"""

LINE = "═" * 48
LABEL_WIDTH = 7  # 标签按 7 个汉字宽度对齐
# 《个人信息保护法》第 24 条对自动化决策要求透明、公平：工具只辅助阅读，不替招聘人员做决定
DISCLAIMER = "※ 本报告仅辅助阅读简历，不作为录用依据，结论需由招聘人员结合面试判断。"


def format_report(title, results, source_note=None):
    lines = [LINE, f"简历分析报告：{title}"]
    if source_note:
        lines.append(f"文字来源：{source_note}")
    lines.append(LINE)
    for result in results:
        lines.append("")
        lines.append(f"【{result.title}】")
        for label, value in result.items:
            lines.append(f"  {label:　<{LABEL_WIDTH}}{value}" if label or value else "")
        for note in result.notes:
            lines.append(f"  ⚠ {note}")
    lines += ["", DISCLAIMER]
    return "\n".join(lines)

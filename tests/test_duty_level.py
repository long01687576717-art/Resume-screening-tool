"""回归测试：工作描述定级（modules/experience.py 的 build_duty）。不调用 AI：直接给出 AI 可能返回的字段，检查代码核对后的级别。

锁定的规则：
- 只写"参与"、没写本人做了什么（独立 / 负责 / 主导 / 牵头）→ 本人角色不明，最高 1 级
- 写了"参与"但也写了本人负责的部分 → 照常定级
- "协助 / 配合"且没写本人负责 → 最高 1 级（原有规则）
- 2 级要有具体对象或数字；3 级要有被认可 / 采用或带数字的结果（原有规则）

用法（在项目根目录）：python tests/test_duty_level.py      也可以用 pytest 运行
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modules.experience import build_duty, squash   # noqa: E402

# (说明, 原句, AI 给的级别, 依据词, 具体对象, 结果, 期望级别)
CASES = [
    ("只写参与、对象是空泛领域", "参与用户增长相关的数据分析工作。", 2, "参与", "用户增长", None, 1),
    ("深度参与 + 自我评价", "深度参与业务决策，具备较强的数据敏感度和逻辑思维能力。", 2, "深度参与", "业务决策", None, 1),
    ("参与 + 写了本人负责的部分", "参与新品销量预测项目，负责整理节假日、天气等特征数据。", 2, "负责", "节假日、天气等特征数据", None, 2),
    ("参与 + 有数字，但本人角色仍不明", "参与 3 家门店的季度盘点工作。", 2, "参与", "3 家门店", None, 1),
    ("负责 + 具体对象", "负责门店销售日报的整理和发送。", 2, "负责", "门店销售日报", None, 2),
    ("协助（原有规则）", "协助整理客户资料并归档。", 1, "协助", None, None, 1),
    ("3 级：有带数字的结果", "用 Python 搭建销售周报自动化脚本，周报制作时间由 2 天缩短到 2 小时。", 3, "搭建",
     "销售周报", "周报制作时间由 2 天缩短到 2 小时", 3),
    ("3 级但结果是自我评价 → 降到 2 级", "搭建门店客流日报模板，显著提升了数据及时性。", 3, "搭建", "门店客流日报", "显著提升了数据及时性", 2),
]


def check_all():
    results = []
    for desc, text, level, basis, specifics, result, expect in CASES:
        raw = {"text": text, "level": level, "basis": basis, "specifics": specifics, "result": result}
        got = build_duty(raw, squash(text))["level"]
        results.append((desc, got == expect, f"期望 {expect} 级，实际 {got} 级"))
    return results


def test_duty_levels():
    failed = [r for r in check_all() if not r[1]]
    assert not failed, failed


def main():
    results = check_all()
    for desc, ok, detail in results:
        print(f"  [{'通过' if ok else '失败'}] {desc}：{detail}")
    passed = all(r[1] for r in results)
    print(f"共 {len(results)} 项，" + ("全部通过" if passed else f"{sum(not r[1] for r in results)} 项失败"))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())

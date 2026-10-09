import unittest

import test_inspection_ack  # noqa: F401 - lightweight plugin package
from ack_test.wecom_display import render_wecom_text


class WecomDisplayTests(unittest.TestCase):
    def test_emphasis_next_to_chinese_and_nested_lists(self):
        self.assertEqual(render_wecom_text("这有**两个信号**需要关注\n- 父项\n  - **子项**"),
                         "这有两个信号需要关注\n• 父项\n  • 子项")

    def test_normal_reply_uses_readable_text(self):
        source = "## 两个信号\n\n- **信号一：** 需求上升\n- **信号二： ** 交付加快\n\n> *请关注* __变化__\n\n---\n1. `订单数`：12"
        self.assertEqual(render_wecom_text(source),
                         "两个信号\n\n• 信号一： 需求上升\n• 信号二： 交付加快\n\n请关注 变化\n\n\n1． 订单数：12")

    def test_tables_links_and_task_states(self):
        source = "| 信号 | 状态 |\n|---|---|\n| **需求** | 上升 |\n| 交付 | 稳定 |\n\n- [x] 已核对\n- [ ] 待确认\n[详情](https://example.com/a_(b)?x=1#part)"
        result = render_wecom_text(source)
        self.assertEqual(result, "信号：需求\n状态：上升\n\n信号：交付\n状态：稳定\n\n☑ 已核对\n☐ 待确认\n详情（https://example.com/a_(b)?x=1#part）")

    def test_symbols_urls_and_literal_code_are_preserved(self):
        source = r'''C# #123，2**3 + 4**5，a * b，__init__.py，a_b_c
https://example.com/__init__?a=b*c#part
**https://example.com/path**
`**literal**` 和 \*星号\*
```python
# 注释
value = "**raw**"
    return 2**3
```'''
        result = render_wecom_text(source)
        self.assertIn("C# #123，2**3 + 4**5，a * b，__init__.py，a_b_c", result)
        self.assertIn("https://example.com/__init__?a=b*c#part", result)
        self.assertIn("\nhttps://example.com/path\n", result)
        self.assertIn("**literal** 和 *星号*", result)
        self.assertIn('# 注释\nvalue = "**raw**"\n    return 2**3', result)
        self.assertNotIn("```", result)

    def test_plain_text_and_malformed_table_lose_no_content(self):
        for source in ("正文🙂\n" * 1000, "a / b * c #备注", "\n第一段\n\n第二段\n", "", "| a | b |\n|---|---|\n| 1 | 2 | 3 |"):
            self.assertEqual(render_wecom_text(source), source)

    def test_other_markdown_keeps_meaning(self):
        source = "标题\n===\n~~旧结论~~\n![图表](https://example.com/chart.png)\n```markdown\n## 新结论\n**完成**\n```"
        self.assertEqual(render_wecom_text(source), "标题\n（已划去：旧结论）\n图表（https://example.com/chart.png）\n新结论\n完成")

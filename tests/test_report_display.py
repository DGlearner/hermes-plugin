"""Report display changes only presentation, before transport byte budgets."""
import unittest

from report_display import render_report_text


class ReportDisplayTests(unittest.TestCase):
    def test_incomplete_draft_from_chat_has_clear_fields_and_questions(self):
        source = """这是日报草稿，仍需补充。
## 三、项目／工作进展明细
| 当前目标 / 最近里程碑 | 推进至联调（具体时间待客户回复确认） |
| 当前状态 | 推进中（部分细节待确认） |
| 今日新增进展 | 与客户沟通接口相关事项，整理了部分材料 |

## 四、卡点、问题与风险
- **类型： ** 当前卡点
- **具体情况：** 测试环境与账号需协调，具体解决时间尚不确定
- **跟进负责人：** 本人

## 五、需要主管／上级协调或决策的事项
| 事项 | 需要的具体支持或决策 | 本人／团队建议 | 最迟反馈时间 | 未及时处理的影响 |
|---|---|---|---|---|
| 测试环境 | 账号开通 | 待补充 | 待确认 | 可能影响联调 |

请补充：下一次跟进时间是什么时候？"""
        rendered = render_report_text(source)
        self.assertNotIn("##", rendered)
        self.assertNotIn("**", rendered)
        self.assertNotIn("|", rendered)
        for fact in ("当前状态：推进中（部分细节待确认）", "• 类型： 当前卡点",
                     "事项：测试环境\n需要的具体支持或决策：账号开通",
                     "最迟反馈时间：待确认\n未及时处理的影响：可能影响联调",
                     "请补充：下一次跟进时间是什么时候？"):
            self.assertIn(fact, rendered)

    def test_key_value_and_multi_record_tables_preserve_associations(self):
        source = """# 工作进展同步日报
| 同步内容 | 填写说明 |
| :--- | ---: |
| 当前状态 | 按计划推进 |
| 下一步安排 | 明日15:00由本人完成 |

| 事项 | 负责人 |
|---|---|
| 接口 | 小李 |
| 页面 | 小王 |"""
        rendered = render_report_text(source)
        self.assertIn("当前状态：按计划推进\n下一步安排：明日15:00由本人完成", rendered)
        self.assertIn("事项：接口\n负责人：小李\n\n事项：页面\n负责人：小王", rendered)

    def test_real_symbols_links_and_code_survive(self):
        content = r"""# 工作进展同步日报
- **结果：** C# 的 #123 已修复，计算 2**3 + 4**5，文件 __init__.py
- **链接：** [方案](https://example.com/a_(b)?v=1&item=two#part)
| 事项 | 结果 |
|---|---|
| 转义管道 | A\|B |
| 代码 | `left | right` |

```python
def read():
    return "**raw**", "# heading", "| pipe"
```
~~~text
  - keep indents / and # symbols
~~~"""
        rendered = render_report_text(content)
        for fact in ("C# 的 #123", "2**3 + 4**5", "__init__.py", "A\\|B", "`left | right`",
                     "方案（https://example.com/a_(b)?v=1&item=two#part）",
                     '    return "**raw**", "# heading", "| pipe"',
                     "  - keep indents / and # symbols"):
            self.assertIn(fact, rendered)
        self.assertNotIn("**结果：**", rendered)

    def test_ambiguous_tables_and_unrelated_messages_are_preserved(self):
        for source in ("日报已保存。**提示**：请核对。", "## 接口文档\n| a | b |\n|---|---|\n| 1 | 2 |",
                       "普通计算 a / b * c #备注", "https://example.com/__init__?x=1#part"):
            self.assertEqual(render_report_text(source), source)
        malformed = "| 事项 | 结果 |\n|---|---|\n| A | B | C |"
        self.assertIn(malformed, render_report_text("# 工作进展同步日报\n" + malformed))

    def test_links_in_inline_code_and_indented_code_are_preserved(self):
        code = '''    # keep this comment
    value = "**literal**"
    | 事项 | 结果 |
    |---|---|
    | A | B |'''
        inline = '`echo "[说明](https://example.com/path)"`'
        source = f'# 工作进展同步日报\n\n{code}\n\n命令：{inline}\n\n## 一、基本信息\n日期：今天'
        rendered = render_report_text(source)
        self.assertIn(code, rendered)
        self.assertIn(inline, rendered)
        self.assertIn('\n一、基本信息\n', rendered)

    def test_wrapped_report_is_readable_and_idempotent(self):
        source = "```markdown\n# 工作进展同步日报\n## 一、基本信息\n- **日期：** 2026-10-09\n```"
        rendered = render_report_text(source)
        self.assertEqual(rendered, "工作进展同步日报\n一、基本信息\n• 日期： 2026-10-09")
        self.assertEqual(render_report_text(rendered), rendered)


if __name__ == "__main__":
    unittest.main()

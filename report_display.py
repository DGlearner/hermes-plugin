"""Readable WeCom report display; never changes stored or validated content."""
from __future__ import annotations

import re


_SECTION = re.compile(
    r"^[一二三四五六七八][、.．](?:基本信息|今日重点同步|项目[/／]工作进展明细|"
    r"卡点、问题与风险|需要主管[/／]上级协调或决策的事项|关键沟通与变化|"
    r"下一工作日重点安排|此前待跟进事项更新)"
)
_CODE = re.compile(r"(`+)(.*?)\1")
_URL = re.compile(r"https?://[^\s<>]+")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://(?:[^()\s]|\([^()\s]*\))+)\)")
_FIELDS = {"当前目标／最近里程碑", "当前目标/最近里程碑", "当前状态", "今日新增进展", "已形成的结果",
           "当前所处阶段", "下一步安排", "关键节点预测", "对其他工作的影响"}


def _is_report(content):
    sections = 0
    for line in content.splitlines():
        compact = re.sub(r"[\s#*]", "", line)
        sections += bool(_SECTION.match(compact))
        if compact.startswith("工作进展同步日报"):
            return True
    return sections >= 2 or (sections >= 1 and "日报" in content)


def _text_inline(text):
    # Keep actual URLs (including *, #, /, underscores and pipes).
    text = _LINK.sub(lambda m: f"{m[1]}（{m[2]}）", text)
    parts, start = [], 0

    def emphasis(value):
        return re.sub(r"(?<![A-Za-z0-9_\\])\*\*(.+?)\*\*(?![A-Za-z0-9_])",
                      lambda m: m[1].strip(), value)

    for match in _URL.finditer(text):
        parts.extend((emphasis(text[start:match.start()]), match[0]))
        start = match.end()
    parts.append(emphasis(text[start:]))
    return "".join(parts)


def _inline(text):
    # Code takes precedence even when it contains Markdown-looking links.
    parts, start = [], 0
    for match in _CODE.finditer(text):
        parts.extend((_text_inline(text[start:match.start()]), match[0]))
        start = match.end()
    parts.append(_text_inline(text[start:]))
    return "".join(parts)


def _cells(line):
    """Split structural pipes, preserving escaped pipes and inline code."""
    value = line.strip()
    cells, current, index, code = [], [], 0, 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            current.append(value[index:index + 2])
            index += 2
            continue
        if char == "`":
            end = index + 1
            while end < len(value) and value[end] == "`":
                end += 1
            size = end - index
            if not code:
                code = size
            elif code == size:
                code = 0
            current.append(value[index:end])
            index = end
            continue
        if char == "|" and not code:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        index += 1
    cells.append("".join(current).strip())
    if len(cells) < 2 or code:
        return None
    if not cells[0]:
        cells.pop(0)
    if cells and not cells[-1]:
        cells.pop()
    return cells if len(cells) >= 2 else None


def _table(lines, index):
    headers = _cells(lines[index])
    if not headers or index + 1 >= len(lines):
        return None
    # Drafts sometimes omit the header/separator for this fixed key/value table.
    # Recognize only the known template fields, never arbitrary pipe-delimited text.
    if len(headers) == 2 and re.sub(r"\s", "", _inline(headers[0])) in _FIELDS:
        end, rows = index, []
        while end < len(lines):
            cells = _cells(lines[end])
            if not cells or len(cells) != 2 or re.sub(r"\s", "", _inline(cells[0])) not in _FIELDS:
                break
            rows.append(f"{_inline(cells[0])}：{_inline(cells[1])}")
            end += 1
        if len(rows) >= 2:
            return "\n".join(rows), end
    separator = _cells(lines[index + 1])
    if (not separator or len(separator) != len(headers)
            or not all(re.fullmatch(r":?-+:?", cell) for cell in separator)):
        return None
    end, rows = index + 2, []
    while end < len(lines) and (cells := _cells(lines[end])):
        if len(cells) != len(headers):
            return None  # Ambiguous input stays visible instead of losing facts.
        rows.append(cells)
        end += 1
    if not rows:
        return None
    headers = [_inline(cell) for cell in headers]
    fields = (len(headers) == 2 and headers[0] in {"同步内容", "字段", "项目", "项目／事项", "项目/事项"}
              and headers[1] in {"填写说明", "内容", "说明", "进展", "填写内容"})
    blocks = []
    for row in rows:
        row = [_inline(cell) for cell in row]
        if fields:
            blocks.append(f"{row[0].rstrip('：:')}：{row[1]}")
        else:
            blocks.append("\n".join(f"{label.rstrip('：:')}：{cell}" for label, cell in zip(headers, row)))
    return ("\n" if fields else "\n\n").join(blocks), end


def render_report_text(content, *, known_report=False):
    """Format report-shaped replies (or known report bodies) before pagination.

    Only presentation syntax is removed. Unknown/ambiguous structures and code
    stay visible. Unrelated chat messages return byte-for-byte unchanged.
    """
    if not isinstance(content, str) or not (known_report or _is_report(content)):
        return content
    lines, output, index = content.splitlines(), [], 0
    while index < len(lines):
        line = lines[index]
        if line.startswith(("    ", "\t")) and (index == 0 or not lines[index - 1].strip()):
            # Markdown's indented code blocks must not become headings/tables.
            while index < len(lines) and (not lines[index].strip() or lines[index].startswith(("    ", "\t"))):
                output.append(lines[index])
                index += 1
            continue
        fence = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
        if fence:
            end = index + 1
            closing = re.compile(r"^\s*" + re.escape(fence[1][0]) + "{" + str(len(fence[1])) + r",}\s*$")
            while end < len(lines) and not closing.fullmatch(lines[end]):
                end += 1
            body = "\n".join(lines[index + 1:end])
            if end < len(lines) and fence[2].strip().lower() in {"", "markdown", "md", "text", "plaintext"} and _is_report(body):
                output.append(render_report_text(body, known_report=True))
            else:
                output.extend(lines[index:min(end + 1, len(lines))])
            index = end + 1
            continue
        table = _table(lines, index)
        if table:
            block, index = table
            output.append(block)
            continue
        if re.fullmatch(r"\s*(?:-{3,}|\*{3,}|_{3,})\s*", line):
            output.append("")
        else:
            line = re.sub(r"^\s*#{1,6}\s+", "", line)
            line = re.sub(r"^\s*[-+*]\s+", "• ", line)
            line = re.sub(r"^(\d+)[.)](?=\s)", r"\1．", line)
            line = re.sub(r"^\s*>\s?", "", line)
            output.append(_inline(line))
        index += 1
    return "\n".join(output)

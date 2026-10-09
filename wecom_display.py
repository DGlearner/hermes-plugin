"""Plain text presentation for the WeCom callback adapter's text messages."""
from __future__ import annotations

import re

from .report_display import _CODE, _URL, _render_text


def _protect_literals(value):
    """Escape syntax in bare URLs, numeric powers and Python-style names."""
    def url(match):
        address, suffix = match[0], ""
        # A closing emphasis wrapper belongs to the surrounding prose.
        for marker in ("**", "__", "~~", "*", "_"):
            if address.endswith(marker) and value[:match.start()].count(marker) % 2:
                address, suffix = address[:-len(marker)], marker
                break
        return re.sub(r"([*_~])", r"\\\1", address) + suffix

    value = _URL.sub(url, value)
    value = re.sub(r"(?<=[0-9])\*\*(?=[A-Za-z0-9_(])", r"\*\*", value)
    value = re.sub(r"__[A-Za-z][A-Za-z0-9_]*__(?=[.(])", lambda m: m[0].replace("_", r"\_"), value)
    return value


def _tokens_text(tokens):
    output, links = [], []
    for token in tokens:
        if token.type in {"text", "code_inline", "html_inline"}:
            output.append(token.content)
        elif token.type in {"softbreak", "hardbreak"}:
            output.append("\n")
        elif token.type == "link_open":
            links.append((len(output), token.attrGet("href") or ""))
        elif token.type == "link_close":
            start, url = links.pop()
            if url and "".join(output[start:]) != url:
                output.append(f"（{url}）")
        elif token.type == "image":
            alt = _tokens_text(token.children or []) or "图片"
            output.append(f"{alt}（{token.attrGet('src') or ''}）")
        elif token.type == "s_open":
            output.append("（已划去：")
        elif token.type == "s_close":
            output.append("）")
        elif token.children:
            output.append(_tokens_text(token.children))
        # Emphasis/strong/strike open and close tokens carry no visible text.
    return "".join(output)


def render_wecom_text(content):
    """Convert display syntax before the text transport's UTF-8 pagination.

    markdown-it-py ships in the pinned Hermes runtime. Parsing inline syntax
    preserves escaped punctuation, code, identifiers and arithmetic operators.
    Report storage and Markdown notification transports use their own renderer.
    """
    if not isinstance(content, str) or not content:
        return content
    from markdown_it import MarkdownIt

    parser = MarkdownIt("commonmark", {"html": False}).enable("strikethrough")

    def inline(value):
        # Common model output puts a space inside a bold field label's closer.
        value = re.sub(r"(?<!\\)\*\*([^*\n]+?：)\s+\*\*", r"**\1**", value)
        # Leave literal code spans alone while protecting non-Markdown data.
        pieces, start = [], 0
        for match in _CODE.finditer(value):
            pieces.extend((_protect_literals(value[start:match.start()]), match[0]))
            start = match.end()
        pieces.append(_protect_literals(value[start:]))
        value = "".join(pieces)
        return _tokens_text(parser.parseInline(value)[0].children or [])

    return _render_text(content, inline=inline, plain=True)

"""text_filter.py — 把 agent 的 markdown 改成微信能读的纯文本(照 openclaw-weixin 的 markdown-filter 规则)。

保留:代码块(```)、行内代码、表格、分隔线(---/***/___)、粗体(**)、H1-H4 标题
过滤:包住中文的斜体/粗斜体标记(只留文字)、H5/H6 的 #####/###### 标记、图片 ![alt](url)(整段删掉)、~~删除线~~ 标记
"""
from __future__ import annotations

import re

CJK = re.compile(r"[\u3400-\u9fff\u3000-\u303f\uff00-\uffff]")
IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
H5H6 = re.compile(r"^#{5,6}\s+", re.M)
INLINE_CODE = re.compile(r"`[^`\n]+`")
FENCE = re.compile(r"```")


def _strip_emphasis(line: str) -> str:
    """去掉包住中文的 * / ** 标记;纯英文的粗体保留。"""
    def repl(m: re.Match) -> str:
        inner = m.group(2)
        if CJK.search(inner):
            return inner
        return m.group(0)

    # ***text*** / **text** / *text*
    line = re.sub(r"(\*{1,3})([^*\n]+?)(\1)", repl, line)
    line = re.sub(r"~~([^~\n]+?)~~", r"\1", line)
    return line


def filter_markdown(text: str) -> str:
    """整段过滤(非流式)。微信那边没有富文本,所以标记能去就去,内容别丢。"""
    if not text:
        return text
    lines = text.split("\n")
    out: list[str] = []
    in_fence = False
    for line in lines:
        if FENCE.search(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)          # 代码块原样保留
            continue
        if INLINE_CODE.search(line) and not IMAGE.search(line):
            out.append(line)
            continue
        line = IMAGE.sub("", line)
        line = H5H6.sub("", line)
        if re.match(r"^#{1,4}\s+", line) or re.match(r"^\s*[-*]\s+", line) or re.match(r"^\s*\d+\.\s+", line):
            out.append(line)          # 标题/列表:微信里看着还行,保留
            continue
        if line.strip() in {"---", "***", "___"} or "|" in line:
            out.append(line)
            continue
        out.append(_strip_emphasis(line))
    return "\n".join(out).strip()

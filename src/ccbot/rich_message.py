"""Render Markdown into Bot API 10.2 Rich Message blocks.

Converts a Markdown string into the `InputRichBlock` list that
`sendRichMessage` expects, so Telegram renders real tables, headings, lists,
dividers, and display math instead of the flattened MarkdownV2 approximations
ccbot sends today. Inline styles (bold/italic/code/strikethrough/links) are
compiled into the recursive RichText tree each block's `text` field expects.

Block + inline schema below was confirmed empirically against the live Bot API
(10.2) — the docs list class names but not the wire schema. Crucially,
`parse_mode` and `text_entities` are BOTH silently ignored on rich blocks;
inline styling is a recursive `RichText` tree instead.

Blocks (InputRichBlock, discriminated by `type`):
  - paragraph:  {text: RichText}
  - heading:    {text: RichText, size}  # size is an int heading level (1 = top)
  - divider:    {}                       # needs sibling content, can't stand alone
  - list:       {ordered, items:[{blocks:[...]}]}
  - table:      {cells:[[{text: RichText, colspan?, rowspan?}]]}
  - mathematical_expression: {expression: str}

RichText (a node's `text`, recursive) is one of: a bare string (plain text), a
node dict, or an array mixing the two. Node types:
  - {type: bold|italic|code|strikethrough|underline|spoiler, text: RichText}
  - {type: url, text: RichText, url: str}

Core responsibilities:
  - Detect GFM pipe tables, ATX headings, `---` rules, list runs, and `$$`
    display-math, in one linear scan.
  - Compile inline Markdown (bold/italic/code/strike/links) into RichText trees
    via mistletoe's inline tokenizer.
  - Emit plain-dict blocks (no PTB objects) so `Bot.do_api_request` serializes
    them verbatim.

Key components:
  - markdown_to_rich_blocks: mixed Markdown → list[InputRichBlock] dicts.
  - parse_markdown_table: a single pipe-table string → one table block.
  - markdown_inline_to_rich_text: inline Markdown → RichText tree.

Not handled: fenced code blocks and blockquotes render as plain paragraphs;
inline math stays literal; nested lists are flattened to one level.
"""

import re

import mistletoe

# RichText inline node type per mistletoe span-token class name.
_INLINE_STYLE = {
    "Strong": "bold",
    "Emphasis": "italic",
    "Strikethrough": "strikethrough",
}

# A GFM table separator row, e.g. "| --- | :---: |". Colons (alignment) are
# tolerated but ignored — InputRichBlockTableCell has no alignment field.
_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")
# ATX heading: 1-6 leading '#', then text.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
# Thematic break / horizontal rule on its own line.
_HR_RE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
# Unordered ("- ", "* ", "+ ") or ordered ("1. ", "2) ") list item.
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")


def _is_separator(line: str) -> bool:
    return "-" in line and bool(_SEPARATOR_RE.match(line))


def _split_row(line: str) -> list[str]:
    """Split one pipe-table row into trimmed cell texts.

    Honors escaped pipes (``\\|``) and drops the optional leading/trailing
    pipes that delimit the row.
    """
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells = re.split(r"(?<!\\)\|", s)
    return [c.strip().replace("\\|", "|") for c in cells]


def _flatten(parts: list) -> object:
    """Collapse a list of RichText fragments into the smallest valid RichText.

    Empty -> "" ; single fragment -> that fragment ; else the list itself
    (an array RichText, which the API accepts).
    """
    parts = [p for p in parts if p != ""]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return parts


def _render_inline(tokens) -> list:
    """Walk mistletoe span tokens into a list of RichText fragments."""
    out: list = []
    for t in tokens:
        name = type(t).__name__
        style = _INLINE_STYLE.get(name)
        if name == "RawText":
            out.append(t.content)
        elif name == "EscapeSequence":
            out.append(t.children[0].content if t.children else "")
        elif name == "LineBreak":
            out.append("\n")
        elif name == "InlineCode":
            out.append(
                {"type": "code", "text": t.children[0].content if t.children else ""}
            )
        elif name in ("Link", "AutoLink"):
            inner = _flatten(_render_inline(t.children))
            out.append({"type": "url", "text": inner or t.target, "url": t.target})
        elif name == "Image":
            out.append(
                _flatten(_render_inline(t.children))
            )  # alt text; no inline image node
        elif style:
            out.append({"type": style, "text": _flatten(_render_inline(t.children))})
        elif getattr(t, "children", None):
            out.extend(_render_inline(t.children))
        elif hasattr(t, "content"):
            out.append(t.content)
    return out


def markdown_inline_to_rich_text(text: str) -> object:
    """Compile inline Markdown into a RichText tree.

    Handles bold/italic/code/strikethrough/links. Blank-line paragraph breaks
    inside the run are preserved as ``\\n\\n`` separators.
    """
    doc = mistletoe.Document(text)
    parts: list = []
    for i, child in enumerate(doc.children or []):
        if getattr(child, "children", None):
            if i:
                parts.append("\n\n")
            parts.extend(_render_inline(child.children))
    return _flatten(parts)


def _rows_to_table_block(rows: list[list[str]]) -> dict:
    """Build a table block, padding rows to a uniform column count."""
    width = max((len(r) for r in rows), default=0)
    cells = []
    for row in rows:
        padded = [{"text": markdown_inline_to_rich_text(c)} for c in row]
        padded += [{"text": ""}] * (width - len(row))
        cells.append(padded)
    return {"type": "table", "cells": cells}


def _paragraph(text: str) -> dict:
    """A paragraph block whose inline Markdown is compiled to a RichText tree."""
    return {"type": "paragraph", "text": markdown_inline_to_rich_text(text)}


def contains_rich_blocks(markdown: str) -> bool:
    """True if the Markdown has structure worth a Rich Message.

    A run that renders to only paragraph blocks (plain prose, even with inline
    styling) gains nothing from the rich path and keeps the cheaper editable
    text path; a table/heading/list/divider/math block flips this True.
    """
    return any(b["type"] != "paragraph" for b in markdown_to_rich_blocks(markdown))


def parse_markdown_table(md_table: str) -> dict | None:
    """Convert a single GFM pipe table into one table block.

    Returns None if the text is not a header + separator table.
    """
    lines = [ln for ln in md_table.splitlines() if ln.strip()]
    if len(lines) < 2 or not _is_separator(lines[1]):
        return None
    rows = [_split_row(lines[0])]
    rows.extend(_split_row(ln) for ln in lines[2:])
    return _rows_to_table_block(rows)


def markdown_to_rich_blocks(markdown: str) -> list[dict]:
    """Split mixed Markdown into ordered rich blocks.

    Recognizes pipe tables, ATX headings, thematic breaks, list runs, and
    ``$$…$$`` display math; everything else coalesces into paragraph blocks
    with inline styling compiled to RichText. Nested lists flatten to one level.
    """
    lines = markdown.split("\n")
    blocks: list[dict] = []
    para: list[str] = []

    def flush_para() -> None:
        text = "\n".join(para).strip()
        if text:
            blocks.append(_paragraph(text))
        para.clear()

    i, n = 0, len(lines)
    while i < n:
        line = lines[i]

        # Table: header row followed by a separator row.
        if "|" in line and i + 1 < n and _is_separator(lines[i + 1]):
            flush_para()
            rows = [_split_row(line)]
            i += 2  # consume header + separator
            while i < n and lines[i].strip() and "|" in lines[i]:
                rows.append(_split_row(lines[i]))
                i += 1
            blocks.append(_rows_to_table_block(rows))
            continue

        # Display math: a line opening with "$$", collected until it closes.
        stripped = line.strip()
        if stripped.startswith("$$"):
            flush_para()
            body = stripped[2:]
            if body.endswith("$$") and len(stripped) > 3:  # single-line $$...$$
                expr = body[:-2]
                i += 1
            else:  # multi-line block
                buf = [body]
                i += 1
                while i < n and not lines[i].strip().endswith("$$"):
                    buf.append(lines[i])
                    i += 1
                if i < n:
                    buf.append(lines[i].strip()[:-2])
                    i += 1
                expr = "\n".join(buf)
            expr = expr.strip()
            if expr:
                blocks.append({"type": "mathematical_expression", "expression": expr})
            continue

        # ATX heading.
        h = _HEADING_RE.match(line)
        if h:
            flush_para()
            level = len(h.group(1))
            text = markdown_inline_to_rich_text(h.group(2).strip())
            blocks.append({"type": "heading", "text": text, "size": level})
            i += 1
            continue

        # Thematic break -> divider.
        if _HR_RE.match(line):
            flush_para()
            blocks.append({"type": "divider"})
            i += 1
            continue

        # List run: consecutive list-item lines.
        m = _LIST_RE.match(line)
        if m:
            flush_para()
            ordered = m.group(2)[0].isdigit()
            items = []
            while i < n:
                lm = _LIST_RE.match(lines[i])
                if not lm:
                    break
                items.append({"blocks": [_paragraph(lm.group(3).strip())]})
                i += 1
            blocks.append({"type": "list", "ordered": ordered, "items": items})
            continue

        para.append(line)
        i += 1

    flush_para()
    return blocks

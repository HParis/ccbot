"""Tests for the Markdown -> Rich Message block renderer.

Covers the inline RichText compiler and the block scanner. All pure functions;
no network. The wire schema these assert against was confirmed empirically
against the live Bot API 10.2 (see rich_message module docstring).
"""

from ccbot.rich_message import (
    contains_rich_blocks,
    markdown_inline_to_rich_text,
    markdown_to_rich_blocks,
    parse_markdown_table,
)


class TestInline:
    def test_plain_is_bare_string(self) -> None:
        assert markdown_inline_to_rich_text("just text") == "just text"

    def test_bold(self) -> None:
        assert markdown_inline_to_rich_text("**b**") == {"type": "bold", "text": "b"}

    def test_italic(self) -> None:
        assert markdown_inline_to_rich_text("*i*") == {"type": "italic", "text": "i"}

    def test_inline_code(self) -> None:
        assert markdown_inline_to_rich_text("`c`") == {"type": "code", "text": "c"}

    def test_strikethrough(self) -> None:
        assert markdown_inline_to_rich_text("~~s~~") == {
            "type": "strikethrough",
            "text": "s",
        }

    def test_link_uses_url_field(self) -> None:
        assert markdown_inline_to_rich_text("[x](https://a.com)") == {
            "type": "url",
            "text": "x",
            "url": "https://a.com",
        }

    def test_nested_bold_italic(self) -> None:
        assert markdown_inline_to_rich_text("**b _i_**") == {
            "type": "bold",
            "text": ["b ", {"type": "italic", "text": "i"}],
        }

    def test_mixed_run_is_array(self) -> None:
        result = markdown_inline_to_rich_text("a **b** c")
        assert result == ["a ", {"type": "bold", "text": "b"}, " c"]

    def test_empty_is_empty_string(self) -> None:
        assert markdown_inline_to_rich_text("") == ""

    def test_cjk_preserved(self) -> None:
        assert markdown_inline_to_rich_text("中文 **粗体**") == [
            "中文 ",
            {"type": "bold", "text": "粗体"},
        ]


class TestBlocks:
    def test_paragraph(self) -> None:
        blocks = markdown_to_rich_blocks("hello world")
        assert blocks == [{"type": "paragraph", "text": "hello world"}]

    def test_heading_size_is_level(self) -> None:
        blocks = markdown_to_rich_blocks("## Title")
        assert blocks == [{"type": "heading", "text": "Title", "size": 2}]

    def test_heading_carries_inline(self) -> None:
        blocks = markdown_to_rich_blocks("# **Big**")
        assert blocks == [
            {"type": "heading", "text": {"type": "bold", "text": "Big"}, "size": 1}
        ]

    def test_thematic_break_is_divider(self) -> None:
        assert markdown_to_rich_blocks("---") == [{"type": "divider"}]

    def test_unordered_list(self) -> None:
        blocks = markdown_to_rich_blocks("- one\n- two")
        assert blocks == [
            {
                "type": "list",
                "ordered": False,
                "items": [
                    {"blocks": [{"type": "paragraph", "text": "one"}]},
                    {"blocks": [{"type": "paragraph", "text": "two"}]},
                ],
            }
        ]

    def test_ordered_list(self) -> None:
        blocks = markdown_to_rich_blocks("1. a\n2. b")
        assert blocks[0]["type"] == "list"
        assert blocks[0]["ordered"] is True
        assert len(blocks[0]["items"]) == 2

    def test_list_item_carries_inline(self) -> None:
        blocks = markdown_to_rich_blocks("- item **bold**")
        item_text = blocks[0]["items"][0]["blocks"][0]["text"]
        assert item_text == ["item ", {"type": "bold", "text": "bold"}]

    def test_display_math_single_line(self) -> None:
        blocks = markdown_to_rich_blocks("$$E = mc^2$$")
        assert blocks == [{"type": "mathematical_expression", "expression": "E = mc^2"}]

    def test_display_math_multiline(self) -> None:
        blocks = markdown_to_rich_blocks("$$\na + b\n= c\n$$")
        assert blocks == [
            {"type": "mathematical_expression", "expression": "a + b\n= c"}
        ]


class TestTable:
    def test_simple_table(self) -> None:
        md = "| a | b |\n| --- | --- |\n| 1 | 2 |"
        blocks = markdown_to_rich_blocks(md)
        assert blocks == [
            {
                "type": "table",
                "cells": [
                    [{"text": "a"}, {"text": "b"}],
                    [{"text": "1"}, {"text": "2"}],
                ],
            }
        ]

    def test_table_cell_carries_inline(self) -> None:
        md = "| h |\n| --- |\n| **x** |"
        blocks = markdown_to_rich_blocks(md)
        assert blocks[0]["cells"][1][0] == {"text": {"type": "bold", "text": "x"}}

    def test_ragged_rows_padded(self) -> None:
        md = "| a | b | c |\n| - | - | - |\n| 1 |"
        blocks = markdown_to_rich_blocks(md)
        body = blocks[0]["cells"][1]
        assert body == [{"text": "1"}, {"text": ""}, {"text": ""}]

    def test_escaped_pipe_in_cell(self) -> None:
        md = "| a |\n| --- |\n| x \\| y |"
        blocks = markdown_to_rich_blocks(md)
        assert blocks[0]["cells"][1][0] == {"text": "x | y"}

    def test_parse_markdown_table_standalone(self) -> None:
        block = parse_markdown_table("| a |\n| - |\n| 1 |")
        assert block == {"type": "table", "cells": [[{"text": "a"}], [{"text": "1"}]]}

    def test_parse_markdown_table_rejects_non_table(self) -> None:
        assert parse_markdown_table("not a table") is None


class TestMixed:
    def test_ordering_preserved(self) -> None:
        md = "# H\n\npara\n\n| a |\n| - |\n| 1 |\n\n- x"
        types = [b["type"] for b in markdown_to_rich_blocks(md)]
        assert types == ["heading", "paragraph", "table", "list"]

    def test_empty_input(self) -> None:
        assert markdown_to_rich_blocks("") == []


class TestContainsRichBlocks:
    """Gates the worker's rich vs. text send path."""

    def test_plain_prose_is_not_rich(self) -> None:
        assert contains_rich_blocks("just some text") is False

    def test_inline_styled_prose_is_not_rich(self) -> None:
        # bold/italic/links alone stay on the cheaper editable text path
        assert contains_rich_blocks("has **bold** and [a](https://x.com)") is False

    def test_table_is_rich(self) -> None:
        assert contains_rich_blocks("| a |\n| - |\n| 1 |") is True

    def test_heading_is_rich(self) -> None:
        assert contains_rich_blocks("# Title") is True

    def test_list_is_rich(self) -> None:
        assert contains_rich_blocks("- one\n- two") is True

    def test_math_is_rich(self) -> None:
        assert contains_rich_blocks("$$E=mc^2$$") is True

    def test_empty_is_not_rich(self) -> None:
        assert contains_rich_blocks("") is False

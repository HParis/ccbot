"""Message splitting utility for Telegram's 4096-character limit.

Provides:
  - utf16_len(): message length in UTF-16 code units (Telegram's accounting).
  - split_message(): splits long text into Telegram-safe chunks (≤4096 UTF-16
    units), preferring newline boundaries and preserving code block integrity.

Fenced code blocks are closed/reopened at chunk boundaries so each chunk stays
valid markdown. A GFM pipe table that fits in a chunk is kept whole (never cut
mid-table) so the send layer can still render it as a Rich Message; only a
single table larger than one chunk degrades to per-line splitting.
"""

import re

TELEGRAM_MAX_MESSAGE_LENGTH = 4096

# Headroom reserved inside code blocks so the closing "\n```" always fits
_FENCE_CLOSE_RESERVE = 4

# A GFM table separator row, e.g. "| --- | :---: |".
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")


def utf16_len(text: str) -> int:
    """Length in UTF-16 code units — how Telegram counts message length.

    Characters outside the BMP (emoji, some CJK extensions) occupy two
    UTF-16 code units; Python's len() counts them as one code point.
    """
    return sum(2 if ord(c) > 0xFFFF else 1 for c in text)


def _is_table_separator(line: str) -> bool:
    return "-" in line and bool(_TABLE_SEPARATOR_RE.match(line))


def _force_split(line: str, budget: int) -> list[str]:
    """Split a single overlong line into pieces within a UTF-16 budget."""
    pieces: list[str] = []
    piece = ""
    piece_len = 0
    for ch in line:
        ch_len = 2 if ord(ch) > 0xFFFF else 1
        if piece_len + ch_len > budget:
            pieces.append(piece)
            piece, piece_len = ch, ch_len
        else:
            piece += ch
            piece_len += ch_len
    if piece:
        pieces.append(piece)
    return pieces


def split_message(
    text: str, max_length: int = TELEGRAM_MAX_MESSAGE_LENGTH
) -> list[str]:
    """Split a message into chunks that fit Telegram's length limit.

    Lengths are measured in UTF-16 code units (Telegram's limit), not code
    points. Tries to split on newlines when possible to preserve formatting.
    When a split occurs inside a fenced code block (```), the block is
    closed at the end of the current chunk and re-opened at the start
    of the next chunk so each chunk remains valid markdown. A pipe table that
    fits a chunk is kept intact so the send layer can render it richly.
    """
    if utf16_len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    current_chunk = ""
    current_len = 0
    # Code-block state BEFORE the line being placed: a fence line toggles the
    # state only after it has been assigned to a chunk, so a flush never
    # appends a closing fence to a chunk that contains no opening fence.
    in_code_block = False
    code_fence = ""  # e.g. "```python"

    def flush() -> None:
        nonlocal current_chunk, current_len
        chunk_text = current_chunk.rstrip("\n")
        if chunk_text:
            if in_code_block:
                # Close the open code block before flushing
                chunk_text += "\n```"
            chunks.append(chunk_text)
        current_chunk = ""
        current_len = 0

    lines = text.split("\n")
    n = len(lines)
    i = 0
    while i < n:
        line = lines[i]
        stripped = line.strip()
        is_fence = stripped.startswith("```")

        # Keep a whole pipe table (header + separator + body) in one chunk so
        # the send layer can render it as a Rich Message. Only when the table
        # fits a chunk; an oversized table falls through to per-line splitting.
        if (
            not in_code_block
            and "|" in line
            and i + 1 < n
            and _is_table_separator(lines[i + 1])
        ):
            j = i + 2
            while j < n and lines[j].strip() and "|" in lines[j]:
                j += 1
            table = "\n".join(lines[i:j])
            table_len = utf16_len(table)
            if table_len <= max_length:
                if current_chunk and current_len + table_len + 1 > max_length:
                    flush()
                current_chunk += table + "\n"
                current_len += table_len + 1
                i = j
                continue
            # else: table too big to keep atomic — fall through to line logic.

        line_len = utf16_len(line)
        # Inside a code block, reserve room for the closing "\n```"
        reserve = _FENCE_CLOSE_RESERVE if in_code_block else 0

        # If single line exceeds max, split it forcefully
        if line_len > max_length - reserve:
            flush()
            if in_code_block:
                # Wrap every piece in its own fences so each chunk
                # renders as a code block on its own
                overhead = utf16_len(code_fence) + 1 + _FENCE_CLOSE_RESERVE
                for piece in _force_split(line, max_length - overhead):
                    chunks.append(f"{code_fence}\n{piece}\n```")
                # Re-open the block for subsequent lines
                current_chunk = code_fence + "\n"
                current_len = utf16_len(current_chunk)
            else:
                chunks.extend(_force_split(line, max_length))
        elif current_len + line_len + 1 > max_length - reserve:
            # Current chunk is full, start a new one
            flush()
            # Re-open code block in the new chunk
            if in_code_block:
                current_chunk = code_fence + "\n" + line + "\n"
            else:
                current_chunk = line + "\n"
            current_len = utf16_len(current_chunk)
        else:
            current_chunk += line + "\n"
            current_len += line_len + 1

        # Toggle code-block state after the line has been placed
        if is_fence:
            if not in_code_block:
                in_code_block = True
                code_fence = stripped  # remember "```lang"
            else:
                in_code_block = False

        i += 1

    if current_chunk:
        chunks.append(current_chunk.rstrip("\n"))

    return chunks

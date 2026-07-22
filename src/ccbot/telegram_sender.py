"""Message splitting utility for Telegram's per-message character limit.

Provides:
  - split_message(): splits long text into Telegram-safe chunks, preferring
    newline boundaries and preserving code block integrity.

Telegram raised the bot message limit from 4096 to 32768 chars (2026), with a
client-side "Show More" fold past ~8000 rendered chars. We split at 8000 so
each relayed chunk stays a single unfolded message while drastically reducing
the [1/N] fragmentation of long Claude output. Splitting happens on raw
markdown; MarkdownV2 escaping expands it slightly, but stays far under 32768.

Fenced code blocks and GFM pipe tables are kept intact across the split — a
code block is closed/reopened at chunk boundaries, and a table that fits in a
chunk is never cut mid-table (so the send layer can still render it as a Rich
Message). A single table larger than one chunk is the one case that degrades to
per-line splitting.
"""

import re

# Raw-markdown split size. Kept at the ~8000 client fold threshold so rendered
# messages stay unfolded for typical content (escaping adds little for prose).
TELEGRAM_MAX_MESSAGE_LENGTH = 8000

# A GFM table separator row, e.g. "| --- | :---: |".
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")


def _is_table_separator(line: str) -> bool:
    return "-" in line and bool(_TABLE_SEPARATOR_RE.match(line))


def split_message(
    text: str, max_length: int = TELEGRAM_MAX_MESSAGE_LENGTH
) -> list[str]:
    """Split a message into chunks that fit Telegram's length limit.

    Tries to split on newlines when possible to preserve formatting.
    When a split occurs inside a fenced code block (```), the block is
    closed at the end of the current chunk and re-opened at the start
    of the next chunk so each chunk remains valid markdown.
    """
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    current_chunk = ""
    in_code_block = False
    code_fence = ""  # e.g. "```python"

    lines = text.split("\n")
    n = len(lines)
    i = 0
    while i < n:
        line = lines[i]
        stripped = line.strip()

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
            if len(table) <= max_length:
                if current_chunk and len(current_chunk) + len(table) + 1 > max_length:
                    chunks.append(current_chunk.rstrip("\n"))
                    current_chunk = ""
                current_chunk += table + "\n"
                i = j
                continue
            # else: table too big to keep atomic — fall through to line logic.

        # Track code block state
        if stripped.startswith("```"):
            if not in_code_block:
                in_code_block = True
                code_fence = stripped  # remember "```lang"
            else:
                in_code_block = False

        # If single line exceeds max, split it forcefully
        if len(line) > max_length:
            if current_chunk:
                chunk_text = current_chunk.rstrip("\n")
                if in_code_block:
                    # The long line is inside a code block; close before flush
                    chunk_text += "\n```"
                chunks.append(chunk_text)
                current_chunk = (code_fence + "\n") if in_code_block else ""
            # Split long line into fixed-size pieces
            for k in range(0, len(line), max_length):
                chunks.append(line[k : k + max_length])
        elif len(current_chunk) + len(line) + 1 > max_length:
            # Current chunk is full, start a new one
            chunk_text = current_chunk.rstrip("\n")
            if in_code_block:
                chunk_text += "\n```"
            chunks.append(chunk_text)
            # Re-open code block in the new chunk
            if in_code_block:
                current_chunk = code_fence + "\n" + line + "\n"
            else:
                current_chunk = line + "\n"
        else:
            current_chunk += line + "\n"

        i += 1

    if current_chunk:
        chunks.append(current_chunk.rstrip("\n"))

    return chunks

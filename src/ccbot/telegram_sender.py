"""Message splitting utility for Telegram's per-message character limit.

Provides:
  - split_message(): splits long text into Telegram-safe chunks, preferring
    newline boundaries and preserving code block integrity.

Telegram raised the bot message limit from 4096 to 32768 chars (2026), with a
client-side "Show More" fold past ~8000 rendered chars. We split at 8000 so
each relayed chunk stays a single unfolded message while drastically reducing
the [1/N] fragmentation of long Claude output. Splitting happens on raw
markdown; MarkdownV2 escaping expands it slightly, but stays far under 32768.
"""

# Raw-markdown split size. Kept at the ~8000 client fold threshold so rendered
# messages stay unfolded for typical content (escaping adds little for prose).
TELEGRAM_MAX_MESSAGE_LENGTH = 8000


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

    for line in text.split("\n"):
        stripped = line.strip()

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
            for i in range(0, len(line), max_length):
                chunks.append(line[i : i + max_length])
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

    if current_chunk:
        chunks.append(current_chunk.rstrip("\n"))

    return chunks

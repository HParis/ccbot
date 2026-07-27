"""Tests for the send layer's length handling.

Telegram caps a message at 4096 UTF-16 code units. Splitting is the send
layer's job (the parse layer never truncates), so no path here may hand
Telegram an oversized payload — least of all the plain-text fallback,
which is what runs when formatting already failed.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ccbot.handlers.message_sender import safe_send, send_with_fallback
from ccbot.telegram_sender import TELEGRAM_MAX_MESSAGE_LENGTH, utf16_len
from ccbot.transcript_parser import TranscriptParser

EXP_START = TranscriptParser.EXPANDABLE_QUOTE_START
EXP_END = TranscriptParser.EXPANDABLE_QUOTE_END


def _oversized_quoted_text() -> str:
    """A tool_result-shaped payload: header + one huge expandable quote.

    build_response_parts keeps this atomic (the quote must not be split),
    so the send layer receives it whole. convert_markdown truncates the
    quote to fit, but stripping the sentinels for the plain fallback
    restores the full length.
    """
    body = "\n".join(f"line {i}: " + "x" * 60 for i in range(120))
    return f"**Bash**(cmd)\n{EXP_START}{body}{EXP_END}"


def _bot_rejecting_long_messages() -> tuple[Any, list[str]]:
    """Bot whose send_message enforces Telegram's real length limit.

    Also rejects the MarkdownV2 attempt outright, forcing the plain
    fallback — the path that used to drop the message.
    """
    seen: list[str] = []

    async def send_message(**kwargs: Any) -> Any:
        text = kwargs["text"]
        if kwargs.get("parse_mode"):
            raise RuntimeError("Can't parse entities")
        if utf16_len(text) > TELEGRAM_MAX_MESSAGE_LENGTH:
            raise RuntimeError("Message is too long")
        seen.append(text)
        msg = MagicMock()
        msg.message_id = 100 + len(seen)
        return msg

    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=send_message)
    return bot, seen


@pytest.mark.asyncio
async def test_send_with_fallback_splits_plain_fallback() -> None:
    """An oversized message must arrive in chunks, not be dropped."""
    bot, seen = _bot_rejecting_long_messages()

    sent = await send_with_fallback(bot, -100, _oversized_quoted_text())

    assert len(seen) > 1, "oversized fallback was not split"
    assert all(utf16_len(chunk) <= TELEGRAM_MAX_MESSAGE_LENGTH for chunk in seen)
    assert sent is not None, "caller must still get a Message back"
    # Nothing silently dropped: every line survives somewhere.
    joined = "".join(seen)
    assert "line 0:" in joined
    assert "line 119:" in joined


@pytest.mark.asyncio
async def test_safe_send_splits_plain_fallback() -> None:
    """safe_send shares the limit; it must split rather than drop."""
    bot, seen = _bot_rejecting_long_messages()

    await safe_send(bot, -100, _oversized_quoted_text(), message_thread_id=42)

    assert len(seen) > 1
    assert all(utf16_len(chunk) <= TELEGRAM_MAX_MESSAGE_LENGTH for chunk in seen)
    assert all(
        call.kwargs.get("message_thread_id") == 42
        for call in bot.send_message.call_args_list
    ), "every chunk must stay in the same topic"


@pytest.mark.asyncio
async def test_short_message_still_sends_once_with_formatting() -> None:
    """The common path is unchanged: one formatted send, no splitting."""
    bot = MagicMock()
    msg = MagicMock()
    msg.message_id = 7
    bot.send_message = AsyncMock(return_value=msg)

    sent = await send_with_fallback(bot, -100, "hello")

    assert sent is msg
    assert bot.send_message.await_count == 1
    assert bot.send_message.await_args.kwargs["parse_mode"] == "MarkdownV2"

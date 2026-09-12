"""Outbound Telegram API call accounting.

Every send, edit, delete and chat-action counts against Telegram's budget of
20 messages per minute *per group* — the budget all of a user's topics share.
Only content sends were ever logged, so the three things that quietly spent
the rest (status edits, tool_result edits, picker renders) were invisible: a
topic could go minutes without a word and the log offered no explanation.

This records every outbound call by kind and logs one summary line per minute.

Key components:
  - count: record one outbound call
  - CountingRateLimiter: AIORateLimiter that counts every request PTB makes,
    including calls this codebase does not make directly
  - summary_loop: background task logging (and resetting) the counters
"""

import asyncio
import logging
from collections import Counter
from collections.abc import Callable, Coroutine
from typing import Any

from telegram.ext import AIORateLimiter

logger = logging.getLogger(__name__)

# Seconds between summary lines. One minute matches the window Telegram's
# per-group limit is expressed in, so a line reads directly against the 20/min
# budget.
SUMMARY_INTERVAL = 60.0

_counts: Counter[str] = Counter()


def count(kind: str) -> None:
    """Record one outbound Telegram API call."""
    _counts[kind] += 1


def snapshot() -> dict[str, int]:
    """Current counts (for tests and ad-hoc inspection)."""
    return dict(_counts)


def reset() -> None:
    """Clear all counters."""
    _counts.clear()


def format_summary() -> str:
    """Render the current counts as one line, busiest first.

    ``group:*`` rows are the calls that spend the 20/min per-group budget —
    they come from the rate limiter and are the ground truth. ``api:*`` rows
    are calls with no group chat_id (getMe, getWebhookInfo …), which the
    budget does not apply to. Everything else is a call-site tag attributing
    those same calls to a purpose, so it must not be added into the total.
    """
    if not _counts:
        return "telegram api: idle"

    def _rows(prefix: str) -> tuple[int, str]:
        picked = {k: v for k, v in _counts.items() if k.startswith(f"{prefix}:")}
        rendered = ", ".join(
            f"{k.removeprefix(f'{prefix}:')}={v}"
            for k, v in sorted(picked.items(), key=lambda kv: -kv[1])
        )
        return sum(picked.values()), rendered

    group_total, group_rows = _rows("group")
    other_total, other_rows = _rows("api")
    kinds = {
        k: v
        for k, v in _counts.items()
        if not k.startswith("group:") and not k.startswith("api:")
    }
    by_kind = ", ".join(
        f"{k}={v}" for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])
    )

    line = f"telegram api: {group_total}/20 per-group calls/min"
    if group_rows:
        line += f" — {group_rows}"
    if other_rows:
        line += f" | ungated: {other_total} ({other_rows})"
    if by_kind:
        line += f" | by kind: {by_kind}"
    return line


class CountingRateLimiter(AIORateLimiter):
    """AIORateLimiter that tallies every request passing through it.

    The rate limiter is the one chokepoint every outbound request goes
    through, so counting here cannot miss a call the way instrumenting
    individual call sites can.
    """

    async def process_request(
        self,
        callback: Callable[..., Coroutine[Any, Any, Any]],
        args: Any,
        kwargs: dict[str, Any],
        endpoint: str,
        data: dict[str, Any],
        rate_limit_args: Any,
    ) -> Any:
        # Only calls carrying a group chat_id spend the 20/min group budget —
        # getMe, getWebhookInfo and friends go through the global 30/s limiter
        # instead. Mixing them into one total inflates every reading, so tag
        # them apart. Mirrors AIORateLimiter's own group test: a negative int
        # chat_id (or a @username string) means channel or supergroup.
        prefix = "api"
        chat_id = data.get("chat_id")
        if chat_id is not None:
            try:
                chat_id = int(chat_id)
            except (ValueError, TypeError):
                pass
            if (isinstance(chat_id, int) and chat_id < 0) or isinstance(chat_id, str):
                prefix = "group"
        count(f"{prefix}:{endpoint}")
        return await super().process_request(
            callback, args, kwargs, endpoint, data, rate_limit_args
        )


async def summary_loop() -> None:
    """Log a counter summary every SUMMARY_INTERVAL seconds, then reset."""
    logger.info("API call accounting started (summary every %.0fs)", SUMMARY_INTERVAL)
    while True:
        try:
            await asyncio.sleep(SUMMARY_INTERVAL)
            if _counts:
                logger.info("%s", format_summary())
                reset()
        except asyncio.CancelledError:
            break
        except Exception as e:  # never let accounting kill the bot
            logger.debug("telemetry summary failed: %s", e)

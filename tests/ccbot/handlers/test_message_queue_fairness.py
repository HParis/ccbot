"""Tests for per-topic queueing.

All of a user's topics live in one Telegram supergroup and share its
20-messages-per-minute budget, so every send is paced at roughly one per
three seconds. With a single FIFO queue per user, a chatty topic's backlog
sat in front of every other topic's messages: a quiet topic could go
minutes without a word while its answer waited behind a hundred tool calls
from elsewhere. Queueing per topic keeps the shared budget but stops one
topic from monopolising the line.
"""

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from ccbot.handlers import message_queue as mq


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    mq._status_msg_info.clear()
    mq._flood_until.clear()
    yield
    mq._status_msg_info.clear()
    mq._flood_until.clear()


@pytest.mark.asyncio
async def test_busy_topic_does_not_block_a_quiet_one(monkeypatch: Any) -> None:
    """A quiet topic's single message must not wait out a busy topic's backlog."""
    sent: list[str] = []

    async def fake_content(_bot: Any, _uid: int, task: mq.MessageTask) -> None:
        await asyncio.sleep(0.02)  # the shared rate limiter pacing each send
        sent.append(task.parts[0])

    monkeypatch.setattr(mq, "_process_content_with_retry", fake_content)

    bot = MagicMock()
    user_id = 7001
    try:
        # Busy topic enqueues a long backlog first...
        for i in range(10):
            await mq.enqueue_content_message(
                bot, user_id, "BUSY", [f"busy{i}"], thread_id=1, content_type="tool_use"
            )
        # ...then a quiet topic says one thing.
        await mq.enqueue_content_message(
            bot, user_id, "QUIET", ["quiet"], thread_id=2, content_type="text"
        )

        await asyncio.wait_for(mq._message_queues[(user_id, 2)].join(), timeout=2.0)
        # The quiet topic is served while the busy one is still draining.
        assert "quiet" in sent
        assert sent.index("quiet") < 9, (
            f"quiet message waited behind {sent.index('quiet')} busy messages"
        )

        await asyncio.wait_for(mq._message_queues[(user_id, 1)].join(), timeout=5.0)
    finally:
        await mq.shutdown_workers()

    assert len([s for s in sent if s.startswith("busy")]) == 10


@pytest.mark.asyncio
async def test_order_within_a_topic_is_still_fifo(monkeypatch: Any) -> None:
    """Fairness across topics must not loosen ordering inside one."""
    sent: list[str] = []

    async def fake_content(_bot: Any, _uid: int, task: mq.MessageTask) -> None:
        await asyncio.sleep(0.01)
        sent.extend(task.parts)

    monkeypatch.setattr(mq, "_process_content_with_retry", fake_content)

    bot = MagicMock()
    user_id = 7002
    try:
        for i in range(5):
            await mq.enqueue_content_message(
                bot, user_id, "W", [f"m{i}"], thread_id=9, content_type="tool_use"
            )
        await asyncio.wait_for(mq._message_queues[(user_id, 9)].join(), timeout=3.0)
    finally:
        await mq.shutdown_workers()

    assert sent == ["m0", "m1", "m2", "m3", "m4"]


@pytest.mark.asyncio
async def test_merging_never_crosses_topics(monkeypatch: Any) -> None:
    """Two topics' text must never be merged into one message."""
    sent: list[list[str]] = []

    async def fake_content(_bot: Any, _uid: int, task: mq.MessageTask) -> None:
        sent.append(list(task.parts))

    monkeypatch.setattr(mq, "_process_content_with_retry", fake_content)

    bot = MagicMock()
    user_id = 7003
    try:
        await mq.enqueue_content_message(bot, user_id, "W", ["a"], thread_id=1)
        await mq.enqueue_content_message(bot, user_id, "W", ["b"], thread_id=2)
        for tid in (1, 2):
            await asyncio.wait_for(
                mq._message_queues[(user_id, tid)].join(), timeout=2.0
            )
    finally:
        await mq.shutdown_workers()

    assert sorted(sent) == [["a"], ["b"]]


@pytest.mark.asyncio
async def test_flood_control_is_shared_across_topics(monkeypatch: Any) -> None:
    """A 429 is a limit on the whole supergroup, not on one topic — every
    topic's worker must back off, not just the one that tripped it."""
    import time

    bot = MagicMock()
    user_id = 7004
    mq._flood_until[user_id] = time.monotonic() + 30

    # A status update for any topic is dropped while the ban is active.
    assert mq._droppable_during_flood(
        mq.MessageTask(task_type="status_update", window_id="W", thread_id=3)
    )
    try:
        await mq.enqueue_status_update(bot, user_id, "W", "working…", thread_id=3)
        assert (user_id, 3) not in mq._message_queues
    finally:
        mq._flood_until.clear()
        await mq.shutdown_workers()

"""Tests for status-message reuse and flood-control dropping.

The worker turns the pending status message into the first content
message by editing it in place. That is only correct while the status
message is still the last thing in the topic — otherwise the reply lands
above whatever arrived since, which reads as "the bot never answered".
"""

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ccbot.handlers import message_queue as mq


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    mq._status_msg_info.clear()
    mq._flood_until.clear()
    yield
    mq._status_msg_info.clear()
    mq._flood_until.clear()


def _bot() -> Any:
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    bot.delete_message = AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_fresh_status_is_converted_in_place() -> None:
    """The common path: a just-posted status becomes the content message."""
    bot = _bot()
    mq._status_msg_info[(1, 5)] = (999, "W", "working…", time.monotonic())

    msg_id = await mq._convert_status_to_content(bot, 1, 5, "W", "answer")

    assert msg_id == 999
    bot.edit_message_text.assert_awaited_once()
    bot.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_status_is_dropped_not_edited() -> None:
    """A status message from long ago is no longer the topic's last
    message — editing it buries the reply up the history."""
    bot = _bot()
    mq._status_msg_info[(1, 5)] = (
        999,
        "W",
        "working…",
        time.monotonic() - mq.STATUS_REUSE_MAX_AGE - 1,
    )

    msg_id = await mq._convert_status_to_content(bot, 1, 5, "W", "answer")

    assert msg_id is None, "caller must send a fresh message instead"
    bot.edit_message_text.assert_not_awaited()
    bot.delete_message.assert_awaited_once()
    assert (1, 5) not in mq._status_msg_info


@pytest.mark.asyncio
async def test_status_clear_survives_flood_control() -> None:
    """Clearing is a correctness step, not a cosmetic update.

    Dropping it leaves stale tracking behind, and the next content
    message gets edited into a status message that is no longer last.
    """
    bot = MagicMock()
    mq._flood_until[1] = time.monotonic() + 30

    try:
        await mq.enqueue_status_update(bot, 1, "W", None, thread_id=5)
        queue = mq._message_queues[(1, 5)]
        assert queue.qsize() == 1
        assert queue.get_nowait().task_type == "status_clear"

        # A cosmetic status *update* is still dropped during a ban.
        await mq.enqueue_status_update(bot, 1, "W", "working…", thread_id=5)
        assert mq._message_queues[(1, 5)].qsize() == 0
    finally:
        worker = mq._queue_workers.pop((1, 5), None)
        if worker:
            worker.cancel()
        mq._message_queues.pop((1, 5), None)
        mq._queue_locks.pop((1, 5), None)


def test_status_clear_is_not_droppable_during_flood() -> None:
    """Worker-side gate: only ephemeral status updates may be skipped."""
    assert mq._droppable_during_flood(mq.MessageTask(task_type="status_update"))
    assert not mq._droppable_during_flood(mq.MessageTask(task_type="status_clear"))
    assert not mq._droppable_during_flood(mq.MessageTask(task_type="content"))

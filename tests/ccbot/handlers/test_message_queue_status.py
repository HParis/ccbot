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


class TestStatusThrottling:
    """The status line embeds a live counter — 'Gusting… (1m 45s · ↓ 5.7k
    tokens)' changes every single second. Exact-text dedup therefore never
    hit while Claude worked, so each 1-second poll produced one edit: up to
    60 API calls per minute per active topic against a group budget of 20.
    Only a change of *state* deserves an immediate edit; the counter gets a
    slow refresh so the message still visibly ticks.
    """

    def test_counter_only_change_is_the_same_state(self) -> None:
        a = mq._status_state_key("Gusting… (1m 45s · ↓ 5.7k tokens)")
        b = mq._status_state_key("Gusting… (1m 46s · ↓ 5.9k tokens)")
        assert a == b

    def test_state_change_is_distinct(self) -> None:
        a = mq._status_state_key("Gusting… (1m 45s · ↓ 5.7k tokens)")
        b = mq._status_state_key("Crafting… (1m 46s · ↓ 5.7k tokens)")
        assert a != b

    def test_elapsed_only_line_is_stable(self) -> None:
        assert mq._status_state_key("Brewed for 8m 8s") == mq._status_state_key(
            "Brewed for 8m 31s"
        )

    @pytest.mark.asyncio
    async def test_counter_tick_is_not_enqueued(self) -> None:
        bot = MagicMock()
        mq._status_msg_info[(1, 5)] = (
            999,
            "W",
            "Gusting… (1m 45s · ↓ 5.7k tokens)",
            time.monotonic(),
        )
        mq._status_last_edit[(1, 5)] = time.monotonic()
        try:
            await mq.enqueue_status_update(
                bot, 1, "W", "Gusting… (1m 46s · ↓ 5.7k tokens)", thread_id=5
            )
            assert (1, 5) not in mq._message_queues
        finally:
            mq._status_last_edit.clear()
            await mq.shutdown_workers()

    @pytest.mark.asyncio
    async def test_state_change_is_enqueued_immediately(self) -> None:
        bot = MagicMock()
        mq._status_msg_info[(1, 5)] = (
            999,
            "W",
            "Gusting… (1m 45s · ↓ 5.7k tokens)",
            time.monotonic(),
        )
        mq._status_last_edit[(1, 5)] = time.monotonic()
        try:
            await mq.enqueue_status_update(
                bot, 1, "W", "Crafting… (1m 46s · ↓ 5.7k tokens)", thread_id=5
            )
            assert mq._message_queues[(1, 5)].qsize() == 1
        finally:
            mq._status_last_edit.clear()
            await mq.shutdown_workers()

    @pytest.mark.asyncio
    async def test_counter_still_refreshes_slowly(self) -> None:
        """The seconds must not freeze — a stale-enough tick still goes out
        so the user can see the session is alive."""
        bot = MagicMock()
        mq._status_msg_info[(1, 5)] = (
            999,
            "W",
            "Gusting… (1m 45s · ↓ 5.7k tokens)",
            time.monotonic(),
        )
        mq._status_last_edit[(1, 5)] = time.monotonic() - mq.STATUS_REFRESH_INTERVAL - 1
        try:
            await mq.enqueue_status_update(
                bot, 1, "W", "Gusting… (2m 30s · ↓ 6.1k tokens)", thread_id=5
            )
            assert mq._message_queues[(1, 5)].qsize() == 1
        finally:
            mq._status_last_edit.clear()
            await mq.shutdown_workers()


class TestToolResultLeavesStatusAlone:
    """A tool_result is delivered by editing the tool_use message that is
    already above the status message. Nothing is appended, so the status
    message does not move — deleting it and posting an identical one back
    cost two API calls per tool call for no visible change. The 1s status
    poll keeps it current on its own.
    """

    @pytest.mark.asyncio
    async def test_tool_result_edit_does_not_churn_the_status_message(self) -> None:
        bot = _bot()
        bot.send_message = AsyncMock()
        mq._status_msg_info[(1, 5)] = (999, "W", "working…", time.monotonic())
        mq._tool_msg_ids[("tu_1", 1, 5)] = 555

        task = mq.MessageTask(
            task_type="content",
            window_id="W",
            parts=["result body"],
            tool_use_id="tu_1",
            content_type="tool_result",
            thread_id=5,
        )
        try:
            await mq._process_content_task(bot, 1, task)
        finally:
            mq._tool_msg_ids.clear()

        # The tool_use message was edited in place...
        bot.edit_message_text.assert_awaited_once()
        assert bot.edit_message_text.await_args.kwargs["message_id"] == 555
        # ...and the status message was left exactly where it was.
        bot.delete_message.assert_not_awaited()
        bot.send_message.assert_not_awaited()
        assert mq._status_msg_info[(1, 5)][0] == 999

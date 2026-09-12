"""Tests for interactive-picker tasks travelling through the message queue.

The picker used to be sent straight to Telegram while normal content went
through the per-user FIFO queue. Since the queue is rate limited and often
seconds deep, the picker regularly overtook the assistant text that Claude
wrote *before* asking the question — the user saw the question first and
the reasoning behind it afterwards. Routing the picker through the same
queue is what keeps the two in order.
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


async def _drain(queue: "asyncio.Queue[mq.MessageTask]") -> None:
    await asyncio.wait_for(queue.join(), timeout=2.0)


@pytest.mark.asyncio
async def test_picker_is_sent_after_earlier_content(monkeypatch: Any) -> None:
    """A picker enqueued after content must not overtake it."""
    order: list[str] = []

    async def fake_content(_bot: Any, _uid: int, task: mq.MessageTask) -> None:
        await asyncio.sleep(0.02)  # rate limiter holding the send
        order.append(f"content:{task.parts[0]}")

    async def fake_ui(*_args: Any, **_kwargs: Any) -> bool:
        order.append("picker")
        return True

    monkeypatch.setattr(mq, "_process_content_with_retry", fake_content)
    monkeypatch.setattr(mq, "handle_interactive_ui", fake_ui)

    bot = MagicMock()
    user_id = 4242
    try:
        await mq.enqueue_content_message(
            bot, user_id, "W", ["reasoning"], content_type="text", thread_id=7
        )
        await mq.enqueue_interactive_ui(bot, user_id, "W", thread_id=7)
        await _drain(mq._message_queues[(user_id, 7)])
    finally:
        await mq.shutdown_workers()

    assert order == ["content:reasoning", "picker"]


@pytest.mark.asyncio
async def test_picker_falls_back_to_content_when_ui_gone(monkeypatch: Any) -> None:
    """If the pane no longer shows the picker, the queued fallback text is
    sent instead — otherwise the tool call vanishes silently."""
    sent: list[str] = []
    cleared: list[tuple[int, int | None]] = []

    async def fake_content(_bot: Any, _uid: int, task: mq.MessageTask) -> None:
        sent.append(task.parts[0])

    async def fake_ui(*_args: Any, **_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(mq, "_process_content_with_retry", fake_content)
    monkeypatch.setattr(mq, "handle_interactive_ui", fake_ui)
    monkeypatch.setattr(
        mq,
        "clear_interactive_mode",
        lambda uid, tid=None: cleared.append((uid, tid)),
    )

    bot = MagicMock()
    user_id = 4243
    try:
        await mq.enqueue_interactive_ui(
            bot,
            user_id,
            "W",
            thread_id=7,
            parts=["**AskUserQuestion**(pick one)"],
            content_type="tool_use",
            tool_use_id="tu_1",
        )
        await _drain(mq._message_queues[(user_id, 7)])
    finally:
        await mq.shutdown_workers()

    assert sent == ["**AskUserQuestion**(pick one)"]
    assert cleared == [(user_id, 7)]


@pytest.mark.asyncio
async def test_picker_without_fallback_sends_nothing(monkeypatch: Any) -> None:
    """The polling path has no fallback text; a vanished picker is a no-op."""
    sent: list[str] = []

    async def fake_content(_bot: Any, _uid: int, task: mq.MessageTask) -> None:
        sent.append(task.parts[0])

    async def fake_ui(*_args: Any, **_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(mq, "_process_content_with_retry", fake_content)
    monkeypatch.setattr(mq, "handle_interactive_ui", fake_ui)

    bot = MagicMock()
    user_id = 4244
    try:
        await mq.enqueue_interactive_ui(bot, user_id, "W", thread_id=7)
        await _drain(mq._message_queues[(user_id, 7)])
    finally:
        await mq.shutdown_workers()

    assert sent == []


@pytest.mark.asyncio
async def test_picker_task_is_not_merged_into_content(monkeypatch: Any) -> None:
    """Merging would splice the picker's fallback text into unrelated
    content and lose the picker entirely."""
    base = mq.MessageTask(task_type="content", window_id="W", parts=["a"])
    picker = mq.MessageTask(task_type="interactive_ui", window_id="W", parts=["b"])

    assert mq._can_merge_tasks(base, picker) is False


@pytest.mark.asyncio
async def test_picker_survives_flood_control(monkeypatch: Any) -> None:
    """A picker is not a cosmetic status update — it must never be dropped."""
    assert (
        mq._droppable_during_flood(
            mq.MessageTask(task_type="interactive_ui", window_id="W")
        )
        is False
    )


@pytest.mark.asyncio
async def test_enqueue_does_not_block_on_send(monkeypatch: Any) -> None:
    """Enqueueing must return immediately: the caller is session_monitor's
    callback loop, and blocking it there freezes every other session."""
    started = asyncio.Event()

    async def fake_ui(*_args: Any, **_kwargs: Any) -> bool:
        started.set()
        await asyncio.sleep(5)
        return True

    monkeypatch.setattr(mq, "handle_interactive_ui", fake_ui)

    bot = MagicMock()
    user_id = 4245
    try:
        await asyncio.wait_for(
            mq.enqueue_interactive_ui(bot, user_id, "W", thread_id=7), timeout=0.5
        )
        await asyncio.wait_for(started.wait(), timeout=1.0)
    finally:
        await mq.shutdown_workers()

"""Tests for status_polling — Settings UI detection via the poller path.

Simulates the user workflow: /model is sent to Claude Code, the Settings
model picker renders in the terminal, and the status poller detects it
on its next 1s tick.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.handlers import message_queue as mq
from ccbot.handlers.interactive_ui import get_interactive_window
from ccbot.handlers.status_polling import update_status_message


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 999
    bot.send_message.return_value = sent_msg
    return bot


@pytest.fixture
def _clear_interactive_state():
    """Ensure interactive state is clean before and after each test."""
    from ccbot.handlers.interactive_ui import _interactive_mode, _interactive_msgs

    _interactive_mode.clear()
    _interactive_msgs.clear()
    yield
    _interactive_mode.clear()
    _interactive_msgs.clear()


@pytest.mark.usefixtures("_clear_interactive_state")
class TestStatusPollerSettingsDetection:
    """Simulate the status poller detecting a Settings UI in the terminal.

    This is the actual code path for /model: no JSONL tool_use entry exists,
    so the status poller (update_status_message) is the only detector.
    """

    @pytest.mark.asyncio
    async def test_settings_ui_detected_and_keyboard_sent(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """Poller captures Settings pane → picker is queued, not sent inline.

        Going through the queue is what keeps the picker behind the content
        Claude produced before it; sending it here would jump the queue.
        """
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.status_polling.terminal_manager") as mock_iterm,
            patch(
                "ccbot.handlers.status_polling.enqueue_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_enqueue_ui,
        ):
            mock_iterm.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_iterm.capture_pane = AsyncMock(return_value=sample_pane_settings)

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_enqueue_ui.assert_called_once_with(
                mock_bot, 1, window_id, thread_id=42
            )
            mock_bot.send_message.assert_not_called()
            # Mode is claimed up front so the next tick doesn't queue a second
            # render of the same picker.
            assert get_interactive_window(1, 42) == window_id

    @pytest.mark.asyncio
    async def test_normal_pane_no_interactive_ui(self, mock_bot: AsyncMock):
        """Normal pane text → no picker queued, just the status check."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        normal_pane = (
            "some output\n"
            "✻ Reading file\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "  [Opus 4.6] Context: 50%\n"
        )

        with (
            patch("ccbot.handlers.status_polling.terminal_manager") as mock_iterm,
            patch(
                "ccbot.handlers.status_polling.enqueue_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_enqueue_ui,
            patch(
                "ccbot.handlers.status_polling.enqueue_status_update",
                new_callable=AsyncMock,
            ),
        ):
            mock_iterm.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_iterm.capture_pane = AsyncMock(return_value=normal_pane)

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_enqueue_ui.assert_not_called()

    @pytest.mark.asyncio
    async def test_settings_ui_end_to_end_sends_telegram_keyboard(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """Full end-to-end: poller → is_interactive_ui → queued picker task
        → handle_interactive_ui → bot.send_message with keyboard.

        Nothing is mocked past the terminal, so this covers the real path
        including the trip through the message queue.
        """
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.status_polling.terminal_manager") as mock_iterm_poll,
            patch("ccbot.handlers.interactive_ui.terminal_manager") as mock_iterm_ui,
            patch("ccbot.handlers.interactive_ui.session_manager") as mock_sm,
        ):
            mock_iterm_poll.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_iterm_poll.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_iterm_ui.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_iterm_ui.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_sm.resolve_chat_id.return_value = 100

            try:
                await update_status_message(
                    mock_bot, user_id=1, window_id=window_id, thread_id=42
                )
                await asyncio.wait_for(mq._message_queues[(1, 42)].join(), timeout=2.0)
            finally:
                await mq.shutdown_workers()

            # Verify bot.send_message was called with keyboard
            mock_bot.send_message.assert_called_once()
            call_kwargs = mock_bot.send_message.call_args.kwargs
            assert call_kwargs["chat_id"] == 100
            assert call_kwargs["message_thread_id"] == 42
            keyboard = call_kwargs["reply_markup"]
            assert keyboard is not None
            # Verify the message text contains model picker content
            assert "Select model" in call_kwargs["text"]


@pytest.mark.usefixtures("_clear_interactive_state")
class TestTranscriptBackedUIDeferral:
    """AskUserQuestion / ExitPlanMode also arrive through the JSONL
    transcript, and that path is correctly ordered against the assistant
    text preceding the question. The pane, however, renders the picker up
    to a second *before* Claude Code flushes that text to the transcript —
    so a poll-driven render always lands above text the user hasn't been
    shown yet. Polling must let the transcript drive these, and only step
    in as a fallback if it never arrives.
    """

    @pytest.fixture(autouse=True)
    def _clear_pending(self):
        from ccbot.handlers.status_polling import _pending_ui_since

        _pending_ui_since.clear()
        yield
        _pending_ui_since.clear()

    @pytest.mark.asyncio
    async def test_ask_user_question_is_not_rendered_by_polling(
        self, mock_bot: AsyncMock, sample_pane_ask_user_single_tab: str
    ):
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.status_polling.terminal_manager") as mock_iterm,
            patch(
                "ccbot.handlers.status_polling.enqueue_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_enqueue_ui,
        ):
            mock_iterm.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_iterm.capture_pane = AsyncMock(
                return_value=sample_pane_ask_user_single_tab
            )

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_enqueue_ui.assert_not_called()
            # Mode stays unclaimed so the transcript path still renders it.
            assert get_interactive_window(1, 42) is None

    @pytest.mark.asyncio
    async def test_polling_renders_it_after_the_grace_period(
        self, mock_bot: AsyncMock, sample_pane_ask_user_single_tab: str
    ):
        """If the transcript never delivers the tool_use (monitor stalled,
        hook missing), polling must still surface the picker eventually."""
        import ccbot.handlers.status_polling as sp

        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        clock = [1000.0]

        with (
            patch("ccbot.handlers.status_polling.terminal_manager") as mock_iterm,
            patch(
                "ccbot.handlers.status_polling.enqueue_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_enqueue_ui,
            patch.object(sp.time, "monotonic", lambda: clock[0]),
        ):
            mock_iterm.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_iterm.capture_pane = AsyncMock(
                return_value=sample_pane_ask_user_single_tab
            )

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )
            mock_enqueue_ui.assert_not_called()

            clock[0] += sp.TRANSCRIPT_UI_GRACE + 0.1
            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_enqueue_ui.assert_called_once()
            assert get_interactive_window(1, 42) == window_id

    @pytest.mark.asyncio
    async def test_permission_prompt_is_still_rendered_immediately(
        self, mock_bot: AsyncMock, sample_pane_permission: str
    ):
        """Permission prompts never reach the transcript — deferring them
        would just make every approval two seconds slower."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.status_polling.terminal_manager") as mock_iterm,
            patch(
                "ccbot.handlers.status_polling.enqueue_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_enqueue_ui,
        ):
            mock_iterm.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_iterm.capture_pane = AsyncMock(return_value=sample_pane_permission)

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_enqueue_ui.assert_called_once()

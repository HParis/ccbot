"""Tests for interactive_ui — handle_interactive_ui and keyboard layout."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.handlers.interactive_ui import (
    _build_interactive_keyboard,
    handle_interactive_ui,
)
from ccbot.handlers.callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_TAB,
    CB_ASK_UP,
)


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
class TestHandleInteractiveUI:
    @pytest.mark.asyncio
    async def test_handle_settings_ui_sends_keyboard(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """handle_interactive_ui captures Settings pane, sends message with keyboard."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.interactive_ui.iterm2_manager") as mock_iterm,
            patch("ccbot.handlers.interactive_ui.session_manager") as mock_sm,
        ):
            mock_iterm.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_iterm.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_sm.resolve_chat_id.return_value = 100

            result = await handle_interactive_ui(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

        assert result is True
        mock_bot.send_message.assert_called_once()
        call_kwargs = mock_bot.send_message.call_args
        assert call_kwargs.kwargs["chat_id"] == 100
        assert call_kwargs.kwargs["message_thread_id"] == 42
        assert call_kwargs.kwargs["reply_markup"] is not None

    @pytest.mark.asyncio
    async def test_not_modified_edit_does_not_send_duplicate(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """When edit_message_text raises "Message is not modified" because
        the polled pane is identical to the last render, we must treat it
        as a no-op success — not as a reason to send a fresh duplicate.
        Regression: previously every quiescent picker tick spawned a copy.
        """
        from telegram.error import BadRequest

        from ccbot.handlers.interactive_ui import _interactive_msgs

        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        # Seed an existing interactive message so we hit the edit path
        _interactive_msgs[(1, 42)] = 777

        mock_bot.edit_message_text = AsyncMock(
            side_effect=BadRequest(
                "Message is not modified: specified new message content "
                "and reply markup are exactly the same as a current "
                "content and reply markup of the message"
            )
        )

        with (
            patch("ccbot.handlers.interactive_ui.iterm2_manager") as mock_iterm,
            patch("ccbot.handlers.interactive_ui.session_manager") as mock_sm,
        ):
            mock_iterm.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_iterm.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_sm.resolve_chat_id.return_value = 100

            result = await handle_interactive_ui(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

        assert result is True
        mock_bot.edit_message_text.assert_called_once()
        # The critical assertion: no fresh duplicate
        mock_bot.send_message.assert_not_called()
        # And the original message id stays registered
        assert _interactive_msgs.get((1, 42)) == 777

    @pytest.mark.asyncio
    async def test_other_badrequest_falls_back_to_send_new(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """Genuine edit failures (e.g. message deleted) should still
        trigger a fresh send so the picker doesn't disappear silently."""
        from telegram.error import BadRequest

        from ccbot.handlers.interactive_ui import _interactive_msgs

        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        _interactive_msgs[(1, 42)] = 777
        mock_bot.edit_message_text = AsyncMock(
            side_effect=BadRequest("Message to edit not found")
        )

        with (
            patch("ccbot.handlers.interactive_ui.iterm2_manager") as mock_iterm,
            patch("ccbot.handlers.interactive_ui.session_manager") as mock_sm,
        ):
            mock_iterm.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_iterm.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_sm.resolve_chat_id.return_value = 100

            result = await handle_interactive_ui(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

        assert result is True
        mock_bot.edit_message_text.assert_called_once()
        mock_bot.send_message.assert_called_once()
        assert _interactive_msgs.get((1, 42)) == 999

    @pytest.mark.asyncio
    async def test_handle_no_ui_returns_false(self, mock_bot: AsyncMock):
        """Returns False when no interactive UI detected in pane."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.interactive_ui.iterm2_manager") as mock_iterm,
            patch("ccbot.handlers.interactive_ui.session_manager"),
        ):
            mock_iterm.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_iterm.capture_pane = AsyncMock(return_value="$ echo hello\nhello\n$\n")

            result = await handle_interactive_ui(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

        assert result is False
        mock_bot.send_message.assert_not_called()


class TestKeyboardLayoutForSettings:
    def test_settings_keyboard_includes_all_nav_keys(self):
        """Settings keyboard includes Tab, arrows (not vertical_only), Space, Esc, Enter."""
        keyboard = _build_interactive_keyboard("@5", ui_name="Settings")
        # Flatten all callback data values
        all_cb_data = [
            btn.callback_data for row in keyboard.inline_keyboard for btn in row
        ]
        assert any(CB_ASK_TAB in d for d in all_cb_data if d)
        assert any(CB_ASK_SPACE in d for d in all_cb_data if d)
        assert any(CB_ASK_UP in d for d in all_cb_data if d)
        assert any(CB_ASK_DOWN in d for d in all_cb_data if d)
        assert any(CB_ASK_LEFT in d for d in all_cb_data if d)
        assert any(CB_ASK_RIGHT in d for d in all_cb_data if d)
        assert any(CB_ASK_ESC in d for d in all_cb_data if d)
        assert any(CB_ASK_ENTER in d for d in all_cb_data if d)

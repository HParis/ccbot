"""Tests for SessionManager pure dict operations."""

import pytest

from ccbot.session import SessionManager


@pytest.fixture
def mgr(monkeypatch) -> SessionManager:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    return SessionManager()


class TestThreadBindings:
    def test_bind_and_get(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        assert mgr.get_window_for_thread(100, 1) == "@1"

    def test_bind_unbind_get_returns_none(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        mgr.unbind_thread(100, 1)
        assert mgr.get_window_for_thread(100, 1) is None

    def test_unbind_nonexistent_returns_none(self, mgr: SessionManager) -> None:
        assert mgr.unbind_thread(100, 999) is None

    def test_iter_thread_bindings(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        mgr.bind_thread(100, 2, "@2")
        mgr.bind_thread(200, 3, "@3")
        result = set(mgr.iter_thread_bindings())
        assert result == {(100, 1, "@1"), (100, 2, "@2"), (200, 3, "@3")}


class TestGroupChatId:
    """Tests for group chat_id routing (supergroup forum topic support).

    IMPORTANT: These tests protect against regression. The group_chat_ids
    mapping is required for Telegram supergroup forum topics — without it,
    all outbound messages fail with "Message thread not found". This was
    erroneously removed once (26cb81f) and restored in PR #23. Do NOT
    delete these tests or the underlying functionality.
    """

    def test_resolve_with_stored_group_id(self, mgr: SessionManager) -> None:
        """resolve_chat_id returns stored group chat_id for known thread."""
        mgr.set_group_chat_id(100, 1, -1001234567890)
        assert mgr.resolve_chat_id(100, 1) == -1001234567890

    def test_resolve_without_group_id_falls_back_to_user_id(
        self, mgr: SessionManager
    ) -> None:
        """resolve_chat_id falls back to user_id when no group_id stored."""
        assert mgr.resolve_chat_id(100, 1) == 100

    def test_resolve_none_thread_id_falls_back_to_user_id(
        self, mgr: SessionManager
    ) -> None:
        """resolve_chat_id returns user_id when thread_id is None (private chat)."""
        mgr.set_group_chat_id(100, 1, -1001234567890)
        assert mgr.resolve_chat_id(100) == 100

    def test_set_group_chat_id_overwrites(self, mgr: SessionManager) -> None:
        """set_group_chat_id updates the stored value on change."""
        mgr.set_group_chat_id(100, 1, -999)
        mgr.set_group_chat_id(100, 1, -888)
        assert mgr.resolve_chat_id(100, 1) == -888

    def test_multiple_threads_independent(self, mgr: SessionManager) -> None:
        """Different threads for the same user store independent group chat_ids."""
        mgr.set_group_chat_id(100, 1, -111)
        mgr.set_group_chat_id(100, 2, -222)
        assert mgr.resolve_chat_id(100, 1) == -111
        assert mgr.resolve_chat_id(100, 2) == -222

    def test_multiple_users_independent(self, mgr: SessionManager) -> None:
        """Different users store independent group chat_ids."""
        mgr.set_group_chat_id(100, 1, -111)
        mgr.set_group_chat_id(200, 1, -222)
        assert mgr.resolve_chat_id(100, 1) == -111
        assert mgr.resolve_chat_id(200, 1) == -222

    def test_set_group_chat_id_with_none_thread(self, mgr: SessionManager) -> None:
        """set_group_chat_id handles None thread_id (mapped to 0)."""
        mgr.set_group_chat_id(100, None, -999)
        # thread_id=None in resolve falls back to user_id (by design)
        assert mgr.resolve_chat_id(100, None) == 100
        # The stored key is "100:0", only accessible with explicit thread_id=0
        assert mgr.group_chat_ids.get("100:0") == -999


class TestWindowState:
    def test_get_creates_new(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@0")
        assert state.session_id == ""
        assert state.cwd == ""

    def test_get_returns_existing(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@1")
        state.session_id = "abc"
        assert mgr.get_window_state("@1").session_id == "abc"

    def test_clear_window_session(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@1")
        state.session_id = "abc"
        mgr.clear_window_session("@1")
        assert mgr.get_window_state("@1").session_id == ""


class TestResolveWindowForThread:
    def test_none_thread_id_returns_none(self, mgr: SessionManager) -> None:
        assert mgr.resolve_window_for_thread(100, None) is None

    def test_unbound_thread_returns_none(self, mgr: SessionManager) -> None:
        assert mgr.resolve_window_for_thread(100, 42) is None

    def test_bound_thread_returns_window(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 42, "@3")
        assert mgr.resolve_window_for_thread(100, 42) == "@3"


class TestDisplayNames:
    def test_get_display_name_fallback(self, mgr: SessionManager) -> None:
        """get_display_name returns window_id when no display name is set."""
        assert mgr.get_display_name("@99") == "@99"

    def test_set_and_get_display_name(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="myproject")
        assert mgr.get_display_name("@1") == "myproject"

    def test_set_display_name_update(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="old-name")
        mgr.window_display_names["@1"] = "new-name"
        assert mgr.get_display_name("@1") == "new-name"

    def test_bind_thread_sets_display_name(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="proj")
        assert mgr.get_display_name("@1") == "proj"

    def test_bind_thread_without_name_no_display(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        # No display name set, fallback to window_id
        assert mgr.get_display_name("@1") == "@1"


class TestIsWindowId:
    def test_legacy_tmux_ids(self, mgr: SessionManager) -> None:
        """``@N`` is the legacy tmux format; kept recognisable so
        resolve_stale_ids can re-key these entries via display name."""
        assert mgr._is_window_id("@0") is True
        assert mgr._is_window_id("@12") is True
        assert mgr._is_window_id("@999") is True

    def test_iterm2_uuids(self, mgr: SessionManager) -> None:
        """iTerm2 session UUIDs (current format) — case-insensitive."""
        assert mgr._is_window_id("9F2E3A1B-DEAD-BEEF-CAFE-0123456789AB") is True
        assert mgr._is_window_id("9f2e3a1b-dead-beef-cafe-0123456789ab") is True

    def test_invalid_ids(self, mgr: SessionManager) -> None:
        assert mgr._is_window_id("myproject") is False
        assert mgr._is_window_id("@") is False
        assert mgr._is_window_id("") is False
        assert mgr._is_window_id("@abc") is False
        # Truncated UUID
        assert mgr._is_window_id("9F2E3A1B-DEAD-BEEF-CAFE") is False


class TestResolveStaleIdsTmuxMigration:
    """Migration path: state.json carries @N tmux IDs but live iTerm2
    only knows UUIDs. resolve_stale_ids must look up the display name
    against live iTerm2 sessions and re-key the bindings."""

    async def test_remaps_tmux_id_to_iterm_uuid_via_display_name(
        self, monkeypatch
    ) -> None:
        from unittest.mock import AsyncMock, patch

        from ccbot.iterm2_manager import ITermWindow
        from ccbot.session import SessionManager, WindowState

        mgr = SessionManager()
        # Seed legacy state: thread bound to tmux @5, named "myproj".
        mgr.thread_bindings = {1: {42: "@5"}}
        mgr.window_states = {"@5": WindowState(window_name="myproj", cwd="/tmp")}
        mgr.window_display_names = {"@5": "myproj"}

        live_uuid = "9F2E3A1B-DEAD-BEEF-CAFE-0123456789AB"
        live = [
            ITermWindow(
                window_id=live_uuid,
                window_name="myproj",
                cwd="/tmp",
                pane_current_command="",
            )
        ]
        with patch(
            "ccbot.session.iterm2_manager.list_windows",
            AsyncMock(return_value=live),
        ):
            await mgr.resolve_stale_ids()

        assert mgr.thread_bindings[1][42] == live_uuid
        assert live_uuid in mgr.window_states
        assert "@5" not in mgr.window_states
        assert mgr.window_display_names[live_uuid] == "myproj"

    async def test_drops_unrecoverable_legacy_binding(self, monkeypatch) -> None:
        """When the display name has no live iTerm2 match, the binding
        is dropped so the topic falls into the unbound-topic flow."""
        from unittest.mock import AsyncMock, patch

        from ccbot.session import SessionManager, WindowState

        mgr = SessionManager()
        mgr.thread_bindings = {1: {42: "@5"}}
        mgr.window_states = {"@5": WindowState(window_name="gone", cwd="/tmp")}
        mgr.window_display_names = {"@5": "gone"}

        with patch(
            "ccbot.session.iterm2_manager.list_windows", AsyncMock(return_value=[])
        ):
            await mgr.resolve_stale_ids()

        assert 42 not in mgr.thread_bindings.get(1, {})
        assert "@5" not in mgr.window_states

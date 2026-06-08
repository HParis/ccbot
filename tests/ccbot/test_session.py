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


class TestSendToWindowStaleUuidFallback:
    """send_to_window must survive an iTerm2 restart: when the cached UUID
    is gone but a live tab with the same display name exists, it should
    migrate state on the spot and deliver the keys to the new UUID."""

    async def test_remaps_via_display_name_and_sends(self) -> None:
        from unittest.mock import AsyncMock, patch

        from ccbot.iterm2_manager import ITermWindow
        from ccbot.session import SessionManager, WindowState

        mgr = SessionManager()
        old_id = "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"
        new_id = "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB"
        mgr.thread_bindings = {1: {42: old_id}}
        mgr.window_states = {old_id: WindowState(window_name="myproj", cwd="/tmp")}
        mgr.window_display_names = {old_id: "myproj"}

        live = ITermWindow(
            window_id=new_id, window_name="myproj", cwd="/tmp", pane_current_command=""
        )

        async def fake_find_by_id(wid: str):
            return live if wid == new_id else None

        with (
            patch(
                "ccbot.session.iterm2_manager.find_window_by_id",
                AsyncMock(side_effect=fake_find_by_id),
            ),
            patch(
                "ccbot.session.iterm2_manager.find_window_by_name",
                AsyncMock(return_value=live),
            ),
            patch(
                "ccbot.session.iterm2_manager.send_keys",
                AsyncMock(return_value=True),
            ) as send_keys,
        ):
            ok, msg = await mgr.send_to_window(old_id, "hello")

        assert ok is True
        assert "myproj" in msg
        send_keys.assert_awaited_once_with(new_id, "hello")
        # State migrated in place
        assert mgr.thread_bindings[1][42] == new_id
        assert new_id in mgr.window_display_names
        assert old_id not in mgr.window_display_names
        assert new_id in mgr.window_states
        assert old_id not in mgr.window_states

    async def test_no_match_returns_window_not_found(self) -> None:
        from unittest.mock import AsyncMock, patch

        from ccbot.session import SessionManager

        mgr = SessionManager()
        old_id = "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"
        mgr.window_display_names = {old_id: "gone"}

        with (
            patch(
                "ccbot.session.iterm2_manager.find_window_by_id",
                AsyncMock(return_value=None),
            ),
            patch(
                "ccbot.session.iterm2_manager.find_window_by_name",
                AsyncMock(return_value=None),
            ),
        ):
            ok, msg = await mgr.send_to_window(old_id, "hello")

        assert ok is False
        assert "not found" in msg.lower()


class TestSessionMapCleanupHonoursUntaggedTabs:
    """The session_map hook writes for any iTerm2 tab running Claude — even
    untagged tabs the user hasn't bound yet. Startup cleanup must therefore
    consult the *full* live tab list, not just ccbot-tagged ones, or it
    will wrongly nuke valid entries and then misroute the next bind.
    """

    async def test_untagged_live_session_is_not_dropped(
        self, tmp_path, monkeypatch
    ) -> None:
        import json
        from unittest.mock import AsyncMock, patch

        from ccbot import config as config_module
        from ccbot.iterm2_manager import ITermWindow
        from ccbot.session import SessionManager

        # Redirect state + session_map into tmp_path so we don't touch real files
        monkeypatch.setattr(config_module.config, "state_file", tmp_path / "state.json")
        monkeypatch.setattr(
            config_module.config, "session_map_file", tmp_path / "session_map.json"
        )

        untagged_uuid = "F53E8920-8782-4F1E-AF03-237D5F491B8C"
        (tmp_path / "session_map.json").write_text(
            json.dumps(
                {
                    f"iterm:{untagged_uuid}": {
                        "session_id": "ea602fcf-386a-4bee-8f43-c8ce67a705be",
                        "cwd": "/tmp/proj",
                        "window_name": "",
                    }
                }
            )
        )

        mgr = SessionManager()
        live_untagged = ITermWindow(
            window_id=untagged_uuid,
            window_name="Assistant",
            cwd="/tmp/proj",
            pane_current_command="",
        )

        with (
            patch(
                "ccbot.session.iterm2_manager.list_windows",
                AsyncMock(return_value=[]),  # no ccbot-tagged tabs
            ),
            patch(
                "ccbot.session.iterm2_manager.list_all_sessions",
                AsyncMock(return_value=[live_untagged]),
            ),
        ):
            await mgr.resolve_stale_ids()

        survived = json.loads((tmp_path / "session_map.json").read_text())
        assert f"iterm:{untagged_uuid}" in survived, (
            "untagged-but-live session_map entry must survive cleanup"
        )

    async def test_truly_dead_session_is_still_dropped(
        self, tmp_path, monkeypatch
    ) -> None:
        import json
        from unittest.mock import AsyncMock, patch

        from ccbot import config as config_module
        from ccbot.session import SessionManager

        monkeypatch.setattr(config_module.config, "state_file", tmp_path / "state.json")
        monkeypatch.setattr(
            config_module.config, "session_map_file", tmp_path / "session_map.json"
        )

        dead_uuid = "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"
        (tmp_path / "session_map.json").write_text(
            json.dumps(
                {
                    f"iterm:{dead_uuid}": {
                        "session_id": "deadbeef-dead-beef-dead-beefdeadbeef",
                        "cwd": "/tmp/gone",
                        "window_name": "",
                    }
                }
            )
        )

        mgr = SessionManager()

        with (
            patch(
                "ccbot.session.iterm2_manager.list_windows",
                AsyncMock(return_value=[]),
            ),
            patch(
                "ccbot.session.iterm2_manager.list_all_sessions",
                AsyncMock(return_value=[]),
            ),
        ):
            await mgr.resolve_stale_ids()

        survived = json.loads((tmp_path / "session_map.json").read_text())
        assert f"iterm:{dead_uuid}" not in survived, (
            "session_map entry whose tab is truly gone should be dropped"
        )


class TestClaimRunningClaude:
    """When the picker is about to type `claude` into a tab that already
    has Claude running, the bot should instead discover the active JSONL
    and adopt it — typing `claude` into live Claude lands as user input
    and leaves the session unmonitored."""

    async def test_discovers_session_id_from_recent_jsonl(
        self, tmp_path, monkeypatch
    ) -> None:
        import json
        import os
        import time
        from pathlib import Path

        from ccbot import config as config_module
        from ccbot.session import SessionManager

        # Point session_map at tmp_path so we don't touch ~/.ccbot
        sm_file = tmp_path / "session_map.json"
        monkeypatch.setattr(config_module.config, "session_map_file", sm_file)
        monkeypatch.setattr(config_module.config, "state_file", tmp_path / "state.json")

        # Fake a Claude projects layout under a fake $HOME
        home = tmp_path / "home"
        cwd = "/Users/test/Code/Proj With Space"
        sanitized = "-Users-test-Code-Proj-With-Space"
        proj_dir = home / ".claude" / "projects" / sanitized
        proj_dir.mkdir(parents=True)
        # Older JSONL
        old_id = "11111111-1111-1111-1111-111111111111"
        (proj_dir / f"{old_id}.jsonl").write_text("{}\n")
        # Newer JSONL — should win the mtime tiebreak
        new_id = "22222222-2222-2222-2222-222222222222"
        new_file = proj_dir / f"{new_id}.jsonl"
        new_file.write_text("{}\n")
        time.sleep(0.05)
        os.utime(new_file, None)  # bump mtime to now

        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

        mgr = SessionManager()
        window_id = "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"
        claimed = await mgr.claim_running_claude(window_id, cwd)

        assert claimed == new_id
        written = json.loads(sm_file.read_text())
        assert written[f"iterm:{window_id}"] == {
            "session_id": new_id,
            "cwd": cwd,
            "window_name": "",
        }

    async def test_returns_none_when_no_project_dir(
        self, tmp_path, monkeypatch
    ) -> None:
        from pathlib import Path

        from ccbot import config as config_module
        from ccbot.session import SessionManager

        monkeypatch.setattr(
            config_module.config, "session_map_file", tmp_path / "session_map.json"
        )
        monkeypatch.setattr(config_module.config, "state_file", tmp_path / "state.json")
        # Point $HOME at an empty dir — no ~/.claude/projects layout
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "empty"))

        mgr = SessionManager()
        result = await mgr.claim_running_claude(
            "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
            "/nonexistent/cwd",
        )
        assert result is None
        assert not (tmp_path / "session_map.json").exists()


class TestReconnectListener:
    """iterm2_manager fires reconnect listeners only on *re*-connects,
    not on the first-ever connection (startup already runs the same
    re-resolution path)."""

    async def test_listener_skipped_on_first_connection(self) -> None:
        from ccbot.iterm2_manager import ITerm2Manager

        mgr = ITerm2Manager()
        called = 0

        async def listener() -> None:
            nonlocal called
            called += 1

        mgr.add_reconnect_listener(listener)
        await mgr._handle_fresh_connection()
        assert called == 0
        assert mgr._ever_connected is True

    async def test_listener_fires_on_subsequent_connections(self) -> None:
        from ccbot.iterm2_manager import ITerm2Manager

        mgr = ITerm2Manager()
        calls: list[int] = []

        async def listener() -> None:
            calls.append(1)

        mgr.add_reconnect_listener(listener)
        await mgr._handle_fresh_connection()  # first connect
        await mgr._handle_fresh_connection()  # reconnect
        await mgr._handle_fresh_connection()  # reconnect
        assert calls == [1, 1]

    async def test_listener_exception_does_not_break_others(self) -> None:
        from ccbot.iterm2_manager import ITerm2Manager

        mgr = ITerm2Manager()
        ran: list[str] = []

        async def bad() -> None:
            ran.append("bad")
            raise RuntimeError("boom")

        async def good() -> None:
            ran.append("good")

        mgr.add_reconnect_listener(bad)
        mgr.add_reconnect_listener(good)
        await mgr._handle_fresh_connection()  # first; nothing fires
        await mgr._handle_fresh_connection()  # reconnect; both fire
        assert ran == ["bad", "good"]


class TestIsReachable:
    """is_reachable() must report False while the circuit breaker is open.
    Callers (status_polling cleanup) rely on this to avoid wiping bindings
    when iTerm2 is just transiently down — kill+restart wipes every UUID
    so without the gate, the cleanup loop reads "every tab is gone".
    """

    async def test_reports_true_when_no_failures(self) -> None:
        from ccbot.iterm2_manager import ITerm2Manager

        mgr = ITerm2Manager()
        assert mgr.is_reachable() is True

    async def test_reports_false_while_breaker_open(self) -> None:
        from ccbot.iterm2_manager import ITerm2Manager

        mgr = ITerm2Manager()
        # Simulate a fresh connection failure
        mgr._trip_breaker()
        assert mgr.is_reachable() is False

    async def test_reports_true_after_breaker_expires(self) -> None:
        import asyncio

        from ccbot.iterm2_manager import ITerm2Manager

        mgr = ITerm2Manager()
        mgr._trip_breaker()
        # Force the breaker's deadline into the past
        loop = asyncio.get_event_loop()
        mgr._circuit_open_until = loop.time() - 1.0
        assert mgr.is_reachable() is True

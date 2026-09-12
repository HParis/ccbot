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
            "ccbot.session.terminal_manager.list_windows",
            AsyncMock(return_value=live),
        ):
            await mgr.resolve_stale_ids()

        assert mgr.thread_bindings[1][42] == live_uuid
        assert live_uuid in mgr.window_states
        assert "@5" not in mgr.window_states
        assert mgr.window_display_names[live_uuid] == "myproj"

    async def test_drops_unrecoverable_legacy_binding(self, monkeypatch) -> None:
        """When the display name has no live iTerm2 match, the binding
        is dropped so the topic falls into the unbound-topic flow.

        Requires at least one live tab: a fully empty live set is
        untrustworthy and handled by the guard tests below.
        """
        from unittest.mock import AsyncMock, patch

        from ccbot.iterm2_manager import ITermWindow
        from ccbot.session import SessionManager, WindowState

        mgr = SessionManager()
        mgr.thread_bindings = {1: {42: "@5"}}
        mgr.window_states = {"@5": WindowState(window_name="gone", cwd="/tmp")}
        mgr.window_display_names = {"@5": "gone"}

        unrelated = [
            ITermWindow(
                window_id="9F2E3A1B-DEAD-BEEF-CAFE-0123456789AB",
                window_name="something-else",
                cwd="/tmp",
                pane_current_command="",
            )
        ]
        with patch(
            "ccbot.session.terminal_manager.list_windows",
            AsyncMock(return_value=unrelated),
        ):
            await mgr.resolve_stale_ids()

        assert 42 not in mgr.thread_bindings.get(1, {})
        assert "@5" not in mgr.window_states


class TestResolveStaleIdsRefusesEmptyLiveSet:
    """A transient backend drop at startup must never be read as
    "every tab was closed".

    Regression: list_windows() returns [] both for "no tabs" and for
    "unreachable". On a startup that raced the iTerm2 WebSocket coming
    up, every thread binding was dropped AND every session_map entry was
    purged. Bindings self-healed via cwd auto-rebind, but session_map
    only gets rewritten by the SessionStart hook — which never fires for
    an already-running Claude — so Claude→Telegram went silently dead
    for every topic until the map was rebuilt by hand.
    """

    async def test_unreachable_backend_leaves_state_intact(self) -> None:
        from unittest.mock import AsyncMock, patch

        from ccbot.session import SessionManager, WindowState

        wid = "F515BBC6-800A-4EDD-9EAF-FF8AA32F2FE0"
        mgr = SessionManager()
        mgr.thread_bindings = {1: {49177: wid}}
        mgr.window_states = {wid: WindowState(window_name="Quin-Global", cwd="/tmp")}
        mgr.window_display_names = {wid: "Quin-Global"}

        cleanup = AsyncMock()
        with (
            patch(
                "ccbot.session.terminal_manager.list_windows",
                AsyncMock(return_value=[]),
            ),
            patch(
                "ccbot.session.terminal_manager.is_reachable",
                lambda: False,
            ),
            patch.object(mgr, "_cleanup_stale_session_map_entries", cleanup),
        ):
            await mgr.resolve_stale_ids()

        assert mgr.thread_bindings[1][49177] == wid
        assert wid in mgr.window_states
        cleanup.assert_not_awaited()

    async def test_reachable_but_zero_tabs_leaves_state_intact(self) -> None:
        """Even a "successful" empty read is too weak to destroy state on."""
        from unittest.mock import AsyncMock, patch

        from ccbot.session import SessionManager, WindowState

        wid = "F515BBC6-800A-4EDD-9EAF-FF8AA32F2FE0"
        mgr = SessionManager()
        mgr.thread_bindings = {1: {49177: wid}}
        mgr.window_states = {wid: WindowState(window_name="Quin-Global", cwd="/tmp")}
        mgr.window_display_names = {wid: "Quin-Global"}

        cleanup = AsyncMock()
        with (
            patch(
                "ccbot.session.terminal_manager.list_windows",
                AsyncMock(return_value=[]),
            ),
            patch("ccbot.session.terminal_manager.is_reachable", lambda: True),
            patch.object(mgr, "_cleanup_stale_session_map_entries", cleanup),
        ):
            await mgr.resolve_stale_ids()

        assert mgr.thread_bindings[1][49177] == wid
        assert wid in mgr.window_states
        cleanup.assert_not_awaited()

    async def test_empty_list_all_sessions_skips_session_map_purge(self) -> None:
        """list_windows succeeding doesn't mean list_all_sessions did."""
        from unittest.mock import AsyncMock, patch

        from ccbot.iterm2_manager import ITermWindow
        from ccbot.session import SessionManager

        live = [
            ITermWindow(
                window_id="9F2E3A1B-DEAD-BEEF-CAFE-0123456789AB",
                window_name="Quin-Global",
                cwd="/tmp",
                pane_current_command="",
            )
        ]
        mgr = SessionManager()
        cleanup = AsyncMock()
        with (
            patch(
                "ccbot.session.terminal_manager.list_windows",
                AsyncMock(return_value=live),
            ),
            patch(
                "ccbot.session.terminal_manager.list_all_sessions",
                AsyncMock(return_value=[]),
            ),
            patch("ccbot.session.terminal_manager.is_reachable", lambda: True),
            patch.object(mgr, "_cleanup_stale_session_map_entries", cleanup),
        ):
            await mgr.resolve_stale_ids()

        cleanup.assert_not_awaited()


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
                "ccbot.session.terminal_manager.find_window_by_id",
                AsyncMock(side_effect=fake_find_by_id),
            ),
            patch(
                "ccbot.session.terminal_manager.find_window_by_name",
                AsyncMock(return_value=live),
            ),
            patch(
                "ccbot.session.terminal_manager.send_keys",
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
                "ccbot.session.terminal_manager.find_window_by_id",
                AsyncMock(return_value=None),
            ),
            patch(
                "ccbot.session.terminal_manager.find_window_by_name",
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
                "ccbot.session.terminal_manager.list_windows",
                AsyncMock(return_value=[]),  # no ccbot-tagged tabs
            ),
            patch(
                "ccbot.session.terminal_manager.list_all_sessions",
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

        # A live tab must be present for "absent" to mean anything: an empty
        # live set is indistinguishable from an unreachable backend, and
        # resolve_stale_ids deliberately refuses to purge on that signal.
        from ccbot.iterm2_manager import ITermWindow

        live = ITermWindow(
            window_id="BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB",
            window_name="alive",
            cwd="/tmp/alive",
            pane_current_command="",
        )

        with (
            patch(
                "ccbot.session.terminal_manager.list_windows",
                AsyncMock(return_value=[live]),
            ),
            patch(
                "ccbot.session.terminal_manager.list_all_sessions",
                AsyncMock(return_value=[live]),
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
    re-resolution path).  Listeners fire AFTER _connect_lock is released
    so they can safely re-enter _get_connection without self-deadlocking."""

    @staticmethod
    async def _connect(mgr) -> None:
        """Drive one successful _get_connection, dropping the cached
        connection first so each call re-runs the connect path (a real
        iTerm2 restart invalidates the cache the same way)."""
        from unittest.mock import AsyncMock, patch

        mgr._connection = None
        with patch(
            "iterm2.Connection.async_create",
            AsyncMock(return_value=object()),
        ):
            await mgr._get_connection()

    async def test_listener_skipped_on_first_connection(self, monkeypatch) -> None:
        from ccbot.iterm2_manager import ITerm2Manager

        monkeypatch.setattr("ccbot.iterm2_manager._RECONNECT_DELAYS", (0.0,))
        mgr = ITerm2Manager()
        called = 0

        async def listener() -> None:
            nonlocal called
            called += 1

        mgr.add_reconnect_listener(listener)
        await self._connect(mgr)  # first-ever connect
        assert called == 0
        assert mgr._ever_connected is True

    async def test_listener_fires_on_subsequent_connections(self, monkeypatch) -> None:
        from ccbot.iterm2_manager import ITerm2Manager

        monkeypatch.setattr("ccbot.iterm2_manager._RECONNECT_DELAYS", (0.0,))
        mgr = ITerm2Manager()
        calls: list[int] = []

        async def listener() -> None:
            calls.append(1)

        mgr.add_reconnect_listener(listener)
        await self._connect(mgr)  # first connect — no fire
        await self._connect(mgr)  # reconnect — fire
        await self._connect(mgr)  # reconnect — fire
        assert calls == [1, 1]

    async def test_listener_exception_does_not_break_others(self, monkeypatch) -> None:
        from ccbot.iterm2_manager import ITerm2Manager

        monkeypatch.setattr("ccbot.iterm2_manager._RECONNECT_DELAYS", (0.0,))
        mgr = ITerm2Manager()
        ran: list[str] = []

        async def bad() -> None:
            ran.append("bad")
            raise RuntimeError("boom")

        async def good() -> None:
            ran.append("good")

        mgr.add_reconnect_listener(bad)
        mgr.add_reconnect_listener(good)
        await self._connect(mgr)  # first; nothing fires
        await self._connect(mgr)  # reconnect; both fire
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


class TestThreadTargets:
    """Durable per-topic cwd targets that survive reboots, enabling auto-rebind
    when iTerm2 restarts (session UUIDs change AND the user.ccbot tag is lost,
    so the restored tabs are untagged and must be matched by cwd + re-tagged)."""

    def test_bind_records_cwd_target_from_param(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 42, "UUID-1", window_name="dev", cwd="/p/dev")
        assert mgr.thread_targets[100][42] == "/p/dev"

    def test_bind_records_cwd_target_from_window_state(
        self, mgr: SessionManager
    ) -> None:
        from ccbot.session import WindowState

        mgr.window_states["UUID-1"] = WindowState(window_name="dev", cwd="/p/dev")
        mgr.bind_thread(100, 42, "UUID-1", window_name="dev")
        assert mgr.thread_targets[100][42] == "/p/dev"

    def test_bind_without_cwd_records_no_target(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 42, "UUID-1", window_name="dev")
        assert 42 not in mgr.thread_targets.get(100, {})

    def test_unbind_keeps_target(self, mgr: SessionManager) -> None:
        """Stale cleanup (unbind) must NOT erase the durable target —
        that's what lets a reboot auto-recover instead of losing the topic."""
        mgr.bind_thread(100, 42, "UUID-1", window_name="dev", cwd="/p/dev")
        mgr.unbind_thread(100, 42)
        assert mgr.thread_targets[100][42] == "/p/dev"

    def test_clear_thread_target_removes(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 42, "UUID-1", cwd="/p/dev")
        mgr.clear_thread_target(100, 42)
        assert 42 not in mgr.thread_targets.get(100, {})

    def test_refresh_backfills_cwd_from_window_state(self, mgr: SessionManager) -> None:
        from ccbot.session import WindowState

        # Bound before the cwd was known → no target yet.
        mgr.thread_bindings = {100: {42: "U1"}}
        mgr.window_states["U1"] = WindowState(window_name="dev", cwd="/p/dev")
        mgr.refresh_thread_targets()
        assert mgr.thread_targets[100][42] == "/p/dev"

    def _sess(self, wid, name, cwd, is_ccbot=False):
        from ccbot.iterm2_manager import ITermWindow

        w = ITermWindow(wid, name, cwd, "")
        w.is_ccbot = is_ccbot
        return w

    async def test_cleared_target_is_not_auto_rebound(
        self, mgr: SessionManager
    ) -> None:
        """Explicit /unbind clears the target, so rebind_unresolved must NOT
        re-attach the topic even though a same-cwd live tab is present."""
        from unittest.mock import AsyncMock, patch

        mgr.bind_thread(100, 42, "U1", window_name="dev", cwd="/p/dev")
        mgr.unbind_thread(100, 42)
        mgr.clear_thread_target(100, 42)  # what /unbind now does
        sessions = [self._sess("U1", "dev", "/p/dev", is_ccbot=True)]
        with patch(
            "ccbot.session.terminal_manager.list_all_sessions",
            AsyncMock(return_value=sessions),
        ) as m:
            n = await mgr.rebind_unresolved()
        assert n == 0
        assert mgr.get_window_for_thread(100, 42) is None
        m.assert_not_called()  # no target left → cheap pre-check skips network

    async def test_rebind_matches_untagged_tab_by_cwd_and_retags(
        self, mgr: SessionManager
    ) -> None:
        from unittest.mock import AsyncMock, patch

        # Reboot: binding dropped, target cwd persists, restored tab is UNTAGGED.
        mgr.thread_targets = {100: {42: "/p/dev"}}
        sess = self._sess("NEW", "Dev", "/p/dev", is_ccbot=False)
        bes = AsyncMock(return_value=True)
        with (
            patch(
                "ccbot.session.terminal_manager.list_all_sessions",
                AsyncMock(return_value=[sess]),
            ),
            patch("ccbot.session.terminal_manager.bind_existing_session", bes),
        ):
            n = await mgr.rebind_unresolved()
        assert n == 1
        assert mgr.get_window_for_thread(100, 42) == "NEW"
        bes.assert_awaited_once()  # untagged tab was re-tagged on adopt

    async def test_rebind_skips_retag_when_already_ccbot(
        self, mgr: SessionManager
    ) -> None:
        from unittest.mock import AsyncMock, patch

        mgr.thread_targets = {100: {42: "/p/dev"}}
        sess = self._sess("NEW", "dev", "/p/dev", is_ccbot=True)
        bes = AsyncMock(return_value=True)
        with (
            patch(
                "ccbot.session.terminal_manager.list_all_sessions",
                AsyncMock(return_value=[sess]),
            ),
            patch("ccbot.session.terminal_manager.bind_existing_session", bes),
        ):
            n = await mgr.rebind_unresolved()
        assert n == 1
        assert mgr.get_window_for_thread(100, 42) == "NEW"
        bes.assert_not_awaited()  # already tagged — no re-tag needed

    async def test_rebind_skips_ambiguous_cwd(self, mgr: SessionManager) -> None:
        from unittest.mock import AsyncMock, patch

        mgr.thread_targets = {100: {42: "/p/dev"}}
        sessions = [
            self._sess("U1", "dev", "/p/dev"),
            self._sess("U2", "dev", "/p/dev"),
        ]
        with patch(
            "ccbot.session.terminal_manager.list_all_sessions",
            AsyncMock(return_value=sessions),
        ):
            n = await mgr.rebind_unresolved()
        assert n == 0
        assert mgr.get_window_for_thread(100, 42) is None
        assert mgr.thread_targets[100][42] == "/p/dev"  # kept

    async def test_rebind_skips_already_live_binding(self, mgr: SessionManager) -> None:
        from unittest.mock import AsyncMock, patch

        mgr.bind_thread(100, 42, "LIVE", window_name="dev", cwd="/p/dev")
        sessions = [self._sess("LIVE", "dev", "/p/dev", is_ccbot=True)]
        with patch(
            "ccbot.session.terminal_manager.list_all_sessions",
            AsyncMock(return_value=sessions),
        ):
            n = await mgr.rebind_unresolved()
        assert n == 0
        assert mgr.get_window_for_thread(100, 42) == "LIVE"

    async def test_rebind_no_unresolved_skips_network(
        self, mgr: SessionManager
    ) -> None:
        from unittest.mock import AsyncMock, patch

        with patch(
            "ccbot.session.terminal_manager.list_all_sessions", AsyncMock()
        ) as m:
            n = await mgr.rebind_unresolved()
        assert n == 0
        m.assert_not_called()

    async def test_rebind_does_not_steal_bound_tab(self, mgr: SessionManager) -> None:
        from unittest.mock import AsyncMock, patch

        # thread 1 holds the only /p/dev tab; thread 2 also targets /p/dev.
        mgr.bind_thread(100, 1, "U1", window_name="dev", cwd="/p/dev")
        mgr.thread_targets.setdefault(100, {})[2] = "/p/dev"
        sessions = [self._sess("U1", "dev", "/p/dev", is_ccbot=True)]
        with patch(
            "ccbot.session.terminal_manager.list_all_sessions",
            AsyncMock(return_value=sessions),
        ):
            n = await mgr.rebind_unresolved()
        assert n == 0
        assert mgr.get_window_for_thread(100, 2) is None

    async def test_rebind_recovers_stale_uuid_binding(
        self, mgr: SessionManager
    ) -> None:
        """A binding pointing at a DEAD UUID (gone from window_states after an
        iTerm2 restart) must count as unresolved and rebind by cwd — not be
        skipped just because the binding is non-None."""
        from unittest.mock import AsyncMock, patch

        mgr.thread_targets = {100: {42: "/p/dev"}}
        mgr.thread_bindings = {100: {42: "DEAD-UUID"}}  # bound, but dead
        # window_states does NOT contain DEAD-UUID (reconciled away on restart).
        sess = self._sess("NEW", "Dev", "/p/dev", is_ccbot=False)
        bes = AsyncMock(return_value=True)
        with (
            patch(
                "ccbot.session.terminal_manager.list_all_sessions",
                AsyncMock(return_value=[sess]),
            ),
            patch("ccbot.session.terminal_manager.bind_existing_session", bes),
        ):
            n = await mgr.rebind_unresolved()
        assert n == 1
        assert mgr.get_window_for_thread(100, 42) == "NEW"
        bes.assert_awaited_once()


class TestLoadSessionMapCwds:
    def test_returns_hook_cwd_per_window(self, tmp_path, monkeypatch) -> None:
        import json

        from ccbot import session as session_mod
        from ccbot.session import SessionManager

        f = tmp_path / "session_map.json"
        f.write_text(
            json.dumps(
                {
                    "iterm:AAA": {"session_id": "s1", "cwd": "/x/dev"},
                    "iterm:BBB": {"session_id": "s2", "cwd": ""},
                    "ccbot:@1": {"session_id": "s3", "cwd": "/legacy"},
                }
            )
        )
        monkeypatch.setattr(session_mod.config, "session_map_file", f)
        assert SessionManager().load_session_map_cwds() == {"AAA": "/x/dev"}

    def test_missing_file_is_empty(self, tmp_path, monkeypatch) -> None:
        from ccbot import session as session_mod
        from ccbot.session import SessionManager

        monkeypatch.setattr(
            session_mod.config, "session_map_file", tmp_path / "missing.json"
        )
        assert SessionManager().load_session_map_cwds() == {}


class TestRebindUsesHookCwd:
    """iTerm2's session.path only updates when a prompt is drawn, so a tab
    started with `cd <dir> && claude` reports the pre-cd directory (usually
    ~) forever. The SessionStart hook records the directory Claude itself
    reported, so the rebind matches on that and keeps session.path as the
    fallback.
    """

    @staticmethod
    def _sess(wid, name, cwd, job="claude"):
        from ccbot.iterm2_manager import ITermWindow

        return ITermWindow(wid, name, cwd, job, job_title=job)

    @staticmethod
    def _hook_map(monkeypatch, tmp_path, entries: dict[str, str]):
        import json

        from ccbot import session as session_mod

        f = tmp_path / "session_map.json"
        f.write_text(
            json.dumps(
                {
                    f"iterm:{k}": {"session_id": "s", "cwd": v}
                    for k, v in entries.items()
                }
            )
        )
        monkeypatch.setattr(session_mod.config, "session_map_file", f)

    async def test_hook_cwd_wins_over_stale_terminal_cwd(
        self, mgr: SessionManager, tmp_path, monkeypatch
    ) -> None:
        from unittest.mock import AsyncMock, patch

        mgr.thread_targets = {100: {42: "/p/dev"}}
        # What iTerm2 reports for a `cd /p/dev && claude` tab: the home dir.
        sess = self._sess("NEW", "Dev", "/Users/paris")
        self._hook_map(monkeypatch, tmp_path, {"NEW": "/p/dev"})
        with (
            patch(
                "ccbot.session.terminal_manager.list_all_sessions",
                AsyncMock(return_value=[sess]),
            ),
            patch(
                "ccbot.session.terminal_manager.bind_existing_session",
                AsyncMock(return_value=True),
            ),
        ):
            n = await mgr.rebind_unresolved()
        assert n == 1
        assert mgr.get_window_for_thread(100, 42) == "NEW"

    async def test_terminal_cwd_is_the_fallback_without_a_hook_entry(
        self, mgr: SessionManager, tmp_path, monkeypatch
    ) -> None:
        from unittest.mock import AsyncMock, patch

        mgr.thread_targets = {100: {42: "/p/dev"}}
        sess = self._sess("NEW", "Dev", "/p/dev")
        self._hook_map(monkeypatch, tmp_path, {})  # hook never fired here
        with (
            patch(
                "ccbot.session.terminal_manager.list_all_sessions",
                AsyncMock(return_value=[sess]),
            ),
            patch(
                "ccbot.session.terminal_manager.bind_existing_session",
                AsyncMock(return_value=True),
            ),
        ):
            n = await mgr.rebind_unresolved()
        assert n == 1
        assert mgr.get_window_for_thread(100, 42) == "NEW"

    async def test_hook_cwd_for_another_tab_does_not_match(
        self, mgr: SessionManager, tmp_path, monkeypatch
    ) -> None:
        """A hook entry belongs to one window_id — it must not leak across."""
        from unittest.mock import AsyncMock, patch

        mgr.thread_targets = {100: {42: "/p/dev"}}
        sess = self._sess("NEW", "Dev", "/Users/paris")
        self._hook_map(monkeypatch, tmp_path, {"OTHER": "/p/dev"})
        with (
            patch(
                "ccbot.session.terminal_manager.list_all_sessions",
                AsyncMock(return_value=[sess]),
            ),
            patch(
                "ccbot.session.terminal_manager.bind_existing_session",
                AsyncMock(return_value=True),
            ),
        ):
            n = await mgr.rebind_unresolved()
        assert n == 0
        assert mgr.get_window_for_thread(100, 42) is None

    async def test_tab_whose_claude_exited_is_not_adopted(
        self, mgr: SessionManager, tmp_path, monkeypatch
    ) -> None:
        """The hook entry outlives the Claude process, so the cwd still
        matches after the user quits Claude and keeps using the shell.
        Binding a topic to that shell would send its messages nowhere.
        """
        from unittest.mock import AsyncMock, patch

        mgr.thread_targets = {100: {42: "/p/dev"}}
        sess = self._sess("NEW", "Dev", "/Users/paris", job="zsh")
        self._hook_map(monkeypatch, tmp_path, {"NEW": "/p/dev"})
        # Adoption is patched to succeed so the *only* thing that can stop
        # the rebind is the running-job check.
        bes = AsyncMock(return_value=True)
        with (
            patch(
                "ccbot.session.terminal_manager.list_all_sessions",
                AsyncMock(return_value=[sess]),
            ),
            patch("ccbot.session.terminal_manager.bind_existing_session", bes),
        ):
            n = await mgr.rebind_unresolved()
        assert n == 0
        assert mgr.get_window_for_thread(100, 42) is None
        bes.assert_not_awaited()

    async def test_backend_without_job_signals_still_rebinds(
        self, mgr: SessionManager, tmp_path, monkeypatch
    ) -> None:
        """No signal at all means "can't tell", not "not Claude"."""
        from unittest.mock import AsyncMock, patch

        mgr.thread_targets = {100: {42: "/p/dev"}}
        sess = self._sess("NEW", "Dev", "/p/dev", job="")
        self._hook_map(monkeypatch, tmp_path, {})
        with (
            patch(
                "ccbot.session.terminal_manager.list_all_sessions",
                AsyncMock(return_value=[sess]),
            ),
            patch(
                "ccbot.session.terminal_manager.bind_existing_session",
                AsyncMock(return_value=True),
            ),
        ):
            n = await mgr.rebind_unresolved()
        assert n == 1

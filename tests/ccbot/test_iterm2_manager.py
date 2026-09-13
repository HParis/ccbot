"""Tests for iterm2_manager — scaffolding parity (Unit 1) plus
connection lifecycle and ccbot-tag-filtered discovery (Unit 2).

Live API calls go through pytest.mark.integration; the rest run
against an in-process fake iTerm2 app so no GUI is required.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.iterm2_manager import ITerm2Manager, ITermWindow, iterm2_manager


def test_singleton_exists() -> None:
    assert isinstance(iterm2_manager, ITerm2Manager)


def test_window_dataclass_fields() -> None:
    w = ITermWindow(
        window_id="UUID-1",
        window_name="proj",
        cwd="/tmp",
        pane_current_command="claude",
    )
    assert w.window_id == "UUID-1"
    assert w.window_name == "proj"
    assert w.cwd == "/tmp"
    assert w.pane_current_command == "claude"


@pytest.mark.parametrize(
    "method_name",
    [
        "list_windows",
        "find_window_by_name",
        "find_window_by_id",
        "capture_pane",
        "send_keys",
        "rename_window",
        "kill_window",
        "create_window",
    ],
)
def test_public_methods_present(method_name: str) -> None:
    """Each public method exists and is async."""
    method = getattr(ITerm2Manager, method_name, None)
    assert method is not None, f"missing: {method_name}"
    assert inspect.iscoroutinefunction(method), f"{method_name} must be async"


# tmux_manager.py is gone — the signature-parity test that lived here
# during the migration was retired in the same change-set.


# ----------------------------------------------------------------------
# Unit 2: connection lifecycle + discovery
# ----------------------------------------------------------------------


def _make_session(
    session_id: str,
    tag: str | None = "1",
    name: str = "",
    path: str = "",
    job: str = "",
) -> MagicMock:
    """Build a fake iterm2.Session whose async_get_variable lookups
    behave like a real session with the given tag/name/path/job."""
    session = MagicMock()
    session.session_id = session_id

    variables: dict[str, Any] = {
        "user.ccbot": tag,
        "session.name": name,
        "session.path": path,
        "session.jobName": job,
    }

    async def _get_var(var_name: str) -> Any:
        return variables.get(var_name)

    session.async_get_variable = AsyncMock(side_effect=_get_var)
    return session


def _make_app(sessions: list[MagicMock]) -> MagicMock:
    """Build a fake App with one window, one tab, and the given sessions."""
    app = MagicMock()

    tab = MagicMock()
    tab.sessions = sessions

    window = MagicMock()
    window.tabs = [tab]

    app.windows = [window]
    app.async_refresh = AsyncMock(return_value=None)

    def _get_session_by_id(sid: str, include_buried: bool = True) -> MagicMock | None:
        for s in sessions:
            if s.session_id == sid:
                return s
        return None

    app.get_session_by_id = MagicMock(side_effect=_get_session_by_id)
    return app


def _fresh_manager() -> ITerm2Manager:
    """Return a fresh manager so cached connection state from the
    module-level singleton doesn't leak into a test."""
    return ITerm2Manager(profile_name="ccbot")


async def test_list_windows_filters_by_ccbot_tag() -> None:
    """Sessions without ``user.ccbot=1`` must not appear in list_windows."""
    tagged = _make_session("UUID-A", tag="1", name="proj-a", path="/tmp/a")
    untagged = _make_session("UUID-B", tag=None, name="user-shell")
    other_value = _make_session("UUID-C", tag="0", name="not-ours")
    app = _make_app([tagged, untagged, other_value])

    mgr = _fresh_manager()
    with (
        patch.object(mgr, "_get_connection", AsyncMock(return_value=MagicMock())),
        patch("iterm2.async_get_app", AsyncMock(return_value=app)),
    ):
        result = await mgr.list_windows()

    assert [w.window_id for w in result] == ["UUID-A"]
    assert result[0].window_name == "proj-a"
    assert result[0].cwd == "/tmp/a"


async def test_list_windows_returns_empty_when_iterm2_unreachable() -> None:
    """Connection failure degrades gracefully to an empty list."""
    mgr = _fresh_manager()
    with patch.object(
        mgr,
        "_get_connection",
        AsyncMock(side_effect=ConnectionError("iTerm2 down")),
    ):
        result = await mgr.list_windows()
    assert result == []


async def test_find_window_by_id_returns_session_when_tagged() -> None:
    tagged = _make_session("UUID-A", tag="1", name="proj-a", path="/tmp/a")
    app = _make_app([tagged])

    mgr = _fresh_manager()
    with (
        patch.object(mgr, "_get_connection", AsyncMock(return_value=MagicMock())),
        patch("iterm2.async_get_app", AsyncMock(return_value=app)),
    ):
        found = await mgr.find_window_by_id("UUID-A")

    assert found is not None
    assert found.window_id == "UUID-A"
    assert found.window_name == "proj-a"


async def test_find_window_by_id_returns_none_for_untagged_session() -> None:
    """A session with the right UUID but no ccbot tag is invisible."""
    untagged = _make_session("UUID-X", tag=None, name="user-shell")
    app = _make_app([untagged])

    mgr = _fresh_manager()
    with (
        patch.object(mgr, "_get_connection", AsyncMock(return_value=MagicMock())),
        patch("iterm2.async_get_app", AsyncMock(return_value=app)),
    ):
        assert await mgr.find_window_by_id("UUID-X") is None


async def test_find_window_by_id_returns_none_for_unknown_uuid() -> None:
    app = _make_app([])

    mgr = _fresh_manager()
    with (
        patch.object(mgr, "_get_connection", AsyncMock(return_value=MagicMock())),
        patch("iterm2.async_get_app", AsyncMock(return_value=app)),
    ):
        assert await mgr.find_window_by_id("UUID-MISSING") is None


async def test_find_window_by_name_matches_via_list_windows() -> None:
    s1 = _make_session("UUID-A", tag="1", name="proj-a")
    s2 = _make_session("UUID-B", tag="1", name="proj-b")
    app = _make_app([s1, s2])

    mgr = _fresh_manager()
    with (
        patch.object(mgr, "_get_connection", AsyncMock(return_value=MagicMock())),
        patch("iterm2.async_get_app", AsyncMock(return_value=app)),
    ):
        found = await mgr.find_window_by_name("proj-b")

    assert found is not None
    assert found.window_id == "UUID-B"


async def test_list_all_sessions_includes_untagged_with_flags() -> None:
    """list_all_sessions returns every iTerm2 session — tagged AND
    untagged — with is_ccbot / has_claude populated. Powers the
    bind-existing-tab picker."""
    tagged_with_claude = _make_session(
        "UUID-A", tag="1", name="proj-a", path="/tmp/a", job="node"
    )
    tagged_without_claude = _make_session(
        "UUID-B", tag="1", name="proj-b", path="/tmp/b", job="zsh"
    )
    untagged_with_claude = _make_session(
        "UUID-C", tag=None, name="adhoc", path="/tmp/c", job="node"
    )
    untagged_shell = _make_session(
        "UUID-D", tag=None, name="bare", path="/tmp/d", job="zsh"
    )
    app = _make_app(
        [
            tagged_with_claude,
            tagged_without_claude,
            untagged_with_claude,
            untagged_shell,
        ]
    )

    mgr = _fresh_manager()
    known_claude_uuids = {"UUID-A", "UUID-C"}
    with (
        patch.object(mgr, "_get_connection", AsyncMock(return_value=MagicMock())),
        patch("iterm2.async_get_app", AsyncMock(return_value=app)),
    ):
        result = await mgr.list_all_sessions(known_claude_uuids)

    assert len(result) == 4
    by_uuid = {w.window_id: w for w in result}
    assert by_uuid["UUID-A"].is_ccbot is True
    assert by_uuid["UUID-A"].has_claude is True
    assert by_uuid["UUID-B"].is_ccbot is True
    assert by_uuid["UUID-B"].has_claude is False
    assert by_uuid["UUID-C"].is_ccbot is False
    assert by_uuid["UUID-C"].has_claude is True
    assert by_uuid["UUID-D"].is_ccbot is False
    assert by_uuid["UUID-D"].has_claude is False


async def test_list_all_sessions_returns_empty_when_iterm2_unreachable() -> None:
    mgr = _fresh_manager()
    with patch.object(
        mgr,
        "_get_connection",
        AsyncMock(side_effect=ConnectionError("iTerm2 down")),
    ):
        result = await mgr.list_all_sessions(set())
    assert result == []


async def test_bind_existing_session_tags_and_names() -> None:
    """bind_existing_session sets user.ccbot=1 and the display name."""
    untagged = _make_session("UUID-X", tag=None, name="user-shell")
    untagged.async_set_variable = AsyncMock(return_value=None)
    untagged.async_set_name = AsyncMock(return_value=None)
    app = _make_app([untagged])

    mgr = _fresh_manager()
    with (
        patch.object(mgr, "_get_connection", AsyncMock(return_value=MagicMock())),
        patch("iterm2.async_get_app", AsyncMock(return_value=app)),
    ):
        ok = await mgr.bind_existing_session("UUID-X", "myproj")

    assert ok is True
    untagged.async_set_variable.assert_awaited_once_with("user.ccbot", "1")
    untagged.async_set_name.assert_awaited_once_with("myproj")


async def test_bind_existing_session_returns_false_when_session_gone() -> None:
    app = _make_app([])
    mgr = _fresh_manager()
    with (
        patch.object(mgr, "_get_connection", AsyncMock(return_value=MagicMock())),
        patch("iterm2.async_get_app", AsyncMock(return_value=app)),
    ):
        ok = await mgr.bind_existing_session("UUID-MISSING", "x")
    assert ok is False


async def test_find_window_by_name_returns_none_for_unknown() -> None:
    app = _make_app([_make_session("UUID-A", tag="1", name="proj-a")])

    mgr = _fresh_manager()
    with (
        patch.object(mgr, "_get_connection", AsyncMock(return_value=MagicMock())),
        patch("iterm2.async_get_app", AsyncMock(return_value=app)),
    ):
        assert await mgr.find_window_by_name("nope") is None


async def test_get_connection_retries_then_succeeds(monkeypatch: Any) -> None:
    """Transient connection failures retry; eventual success caches the
    connection and skips further reconnect attempts."""
    monkeypatch.setattr("ccbot.iterm2_manager._RECONNECT_DELAYS", (0.0, 0.0, 0.0))

    attempts = {"n": 0}
    real_conn = MagicMock(spec=[])

    async def flaky_create() -> Any:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise OSError("iTerm2 not ready yet")
        return real_conn

    mgr = _fresh_manager()
    with patch("iterm2.Connection.async_create", flaky_create):
        conn = await mgr._get_connection()

    assert conn is real_conn
    assert attempts["n"] == 3

    # Second call returns the cached connection without retrying.
    with patch("iterm2.Connection.async_create", AsyncMock()) as create_mock:
        cached = await mgr._get_connection()
    assert cached is real_conn
    create_mock.assert_not_called()


async def test_get_connection_raises_after_exhausting_retries(
    monkeypatch: Any,
) -> None:
    """When connecting fails AND the auto-launch path also can't get
    iTerm2 up, surface a ConnectionError rather than retrying forever."""
    monkeypatch.setattr("ccbot.iterm2_manager._RECONNECT_DELAYS", (0.0, 0.0, 0.0))
    monkeypatch.setattr("ccbot.iterm2_manager._LAUNCH_DELAYS", (0.0, 0.0))

    async def always_fail() -> Any:
        raise OSError("iTerm2 missing")

    mgr = _fresh_manager()
    # Pretend `open -a iTerm` worked but the WebSocket still won't come up.
    with (
        patch("iterm2.Connection.async_create", always_fail),
        patch.object(mgr, "_launch_iterm2", AsyncMock(return_value=True)),
    ):
        with pytest.raises(ConnectionError, match="Python API"):
            await mgr._get_connection(allow_launch=True)


async def test_get_connection_auto_launches_iterm2_on_failure(
    monkeypatch: Any,
) -> None:
    """Phase 1 backoff exhausts → bot shells out 'open -a iTerm' →
    phase 3 backoff finds the API server up.  Result: the user can
    send a TG message even if iTerm2 was closed."""
    monkeypatch.setattr("ccbot.iterm2_manager._RECONNECT_DELAYS", (0.0, 0.0, 0.0))
    monkeypatch.setattr("ccbot.iterm2_manager._LAUNCH_DELAYS", (0.0, 0.0, 0.0))

    real_conn = MagicMock(spec=[])
    attempts = {"n": 0}

    async def flaky_create() -> Any:
        attempts["n"] += 1
        # Phase-1 attempts (1-3) fail, then we expect a launch, then
        # post-launch attempts (4+) succeed.
        if attempts["n"] <= 3:
            raise OSError("not running")
        return real_conn

    mgr = _fresh_manager()
    launch_mock = AsyncMock(return_value=True)
    with (
        patch("iterm2.Connection.async_create", flaky_create),
        patch.object(mgr, "_launch_iterm2", launch_mock),
    ):
        conn = await mgr._get_connection(allow_launch=True)

    assert conn is real_conn
    launch_mock.assert_awaited_once()
    # Three failed phase-1 attempts plus one successful phase-3 attempt.
    assert attempts["n"] == 4


async def test_get_connection_raises_when_open_command_fails(
    monkeypatch: Any,
) -> None:
    """``open -a iTerm`` itself failing (iTerm2 not installed at all)
    surfaces a clear ConnectionError, not a hang or a retry storm."""
    monkeypatch.setattr("ccbot.iterm2_manager._RECONNECT_DELAYS", (0.0, 0.0, 0.0))

    async def always_fail() -> Any:
        raise OSError("connection refused")

    mgr = _fresh_manager()
    with (
        patch("iterm2.Connection.async_create", always_fail),
        patch.object(mgr, "_launch_iterm2", AsyncMock(return_value=False)),
    ):
        with pytest.raises(ConnectionError, match="could not launch"):
            await mgr._get_connection(allow_launch=True)


async def test_passive_call_never_launches_iterm2(monkeypatch: Any) -> None:
    """Background work (status polling, screenshots, discovery) must not
    resurrect a closed iTerm2.  macOS quits iTerm2 during shutdown; a
    poll that relaunches it counts as a newly-started app and cancels
    the shutdown."""
    monkeypatch.setattr("ccbot.iterm2_manager._RECONNECT_DELAYS", (0.0, 0.0, 0.0))

    async def always_fail() -> Any:
        raise OSError("not running")

    mgr = _fresh_manager()
    launch_mock = AsyncMock(return_value=True)
    with (
        patch("iterm2.Connection.async_create", always_fail),
        patch.object(mgr, "_launch_iterm2", launch_mock),
    ):
        with pytest.raises(ConnectionError, match="Not auto-launching"):
            await mgr._get_connection()

    launch_mock.assert_not_awaited()


async def test_ensure_running_is_allowed_to_launch(monkeypatch: Any) -> None:
    """The one user-driven entry point that may start iTerm2."""
    monkeypatch.setattr("ccbot.iterm2_manager._RECONNECT_DELAYS", (0.0, 0.0, 0.0))
    monkeypatch.setattr("ccbot.iterm2_manager._LAUNCH_DELAYS", (0.0,))

    async def always_fail() -> Any:
        raise OSError("not running")

    mgr = _fresh_manager()
    launch_mock = AsyncMock(return_value=True)
    with (
        patch("iterm2.Connection.async_create", always_fail),
        patch.object(mgr, "_launch_iterm2", launch_mock),
    ):
        assert await mgr.ensure_running() is False

    launch_mock.assert_awaited_once()


async def test_circuit_breaker_short_circuits_after_failure(
    monkeypatch: Any,
) -> None:
    """After a failure trips the breaker, subsequent calls raise
    immediately without hitting iTerm2.  This protects iTerm2 from
    the bot's polling loops thundering during a transient outage."""
    monkeypatch.setattr("ccbot.iterm2_manager._RECONNECT_DELAYS", (0.0, 0.0, 0.0))

    create_calls = {"n": 0}

    async def always_fail() -> Any:
        create_calls["n"] += 1
        raise OSError("nope")

    mgr = _fresh_manager()
    with (
        patch("iterm2.Connection.async_create", always_fail),
        patch.object(mgr, "_launch_iterm2", AsyncMock(return_value=False)),
    ):
        # First call: retries exhausted, breaker trips.
        with pytest.raises(ConnectionError):
            await mgr._get_connection()
        first_create_count = create_calls["n"]
        assert first_create_count >= 1
        assert mgr._consecutive_failures == 1

        # Second call: breaker open → no new connect attempt at all.
        with pytest.raises(ConnectionError, match="backoff"):
            await mgr._get_connection()
        assert create_calls["n"] == first_create_count


async def test_circuit_breaker_resets_on_successful_get_app(
    monkeypatch: Any,
) -> None:
    """A successful end-to-end _get_app clears the breaker so future
    calls don't keep deferring."""
    monkeypatch.setattr("ccbot.iterm2_manager._RECONNECT_DELAYS", (0.0, 0.0, 0.0))

    real_conn = MagicMock()
    real_app = MagicMock()
    real_app.async_refresh = AsyncMock(return_value=None)

    mgr = _fresh_manager()
    # Pretend a previous failure tripped the breaker (already past
    # the deadline, so it shouldn't block this call).
    mgr._consecutive_failures = 2
    mgr._circuit_open_until = 0.0  # already expired

    with (
        patch("iterm2.Connection.async_create", AsyncMock(return_value=real_conn)),
        patch("iterm2.async_get_app", AsyncMock(return_value=real_app)),
    ):
        app = await mgr._get_app()

    assert app is real_app
    assert mgr._consecutive_failures == 0
    assert mgr._circuit_open_until == 0.0


async def test_circuit_breaker_does_not_double_trip_on_breaker_raise(
    monkeypatch: Any,
) -> None:
    """When the breaker raises pre-emptively (without touching iTerm2),
    _get_app must NOT trip the breaker again — that would push the
    open-until timer further forward each call and the breaker would
    never close."""
    mgr = _fresh_manager()
    mgr._consecutive_failures = 1
    # Open breaker for a long time so it's surely still open at call.
    loop = asyncio.get_event_loop()
    mgr._circuit_open_until = loop.time() + 100.0
    open_until_before = mgr._circuit_open_until
    failures_before = mgr._consecutive_failures

    with pytest.raises(ConnectionError, match="backoff"):
        await mgr._get_app()

    # The pre-emptive raise must not have advanced either counter.
    assert mgr._consecutive_failures == failures_before
    assert mgr._circuit_open_until == open_until_before


async def test_get_app_invalidates_on_websockets_close_error(monkeypatch: Any) -> None:
    """Regression: after iTerm2 quits or the system sleeps, the
    underlying websocket raises ``websockets.exceptions.ConnectionClosedError``
    (a plain Exception, NOT ConnectionError).  ``_get_app`` must
    invalidate the cached connection on ANY failure, not just
    ConnectionError, so the next call reconnects from scratch.

    Pre-fix symptom (observed in production): one sleep/wake cycle
    burned the cached websocket, then every subsequent /screenshot,
    every monitor poll, every /esc, every send_keys re-raised the
    same dead-connection exception forever — bot deaf until restart.
    """
    monkeypatch.setattr("ccbot.iterm2_manager._RECONNECT_DELAYS", (0.0, 0.0, 0.0))
    monkeypatch.setattr("ccbot.iterm2_manager._LAUNCH_DELAYS", (0.0, 0.0))

    # Simulate websockets' actual exception class — subclass of
    # Exception, not ConnectionError.
    class FakeWebsocketsClosed(Exception):
        pass

    bad_app = MagicMock()
    bad_app.async_refresh = AsyncMock(
        side_effect=FakeWebsocketsClosed("no close frame received or sent")
    )
    good_app = MagicMock()
    good_app.async_refresh = AsyncMock(return_value=None)

    apps = iter([bad_app, good_app])
    create_calls = {"n": 0}

    async def stub_async_get_app(_conn: Any) -> Any:
        return next(apps)

    async def stub_create_conn() -> Any:
        create_calls["n"] += 1
        return MagicMock()

    monkeypatch.setattr("iterm2.async_get_app", stub_async_get_app)
    monkeypatch.setattr("iterm2.Connection.async_create", stub_create_conn)

    mgr = _fresh_manager()

    # First _get_app(): creates conn #1, async_get_app returns bad_app,
    # async_refresh raises FakeWebsocketsClosed → must invalidate and
    # surface as ConnectionError.
    with pytest.raises(ConnectionError, match="Lost iTerm2 connection"):
        await mgr._get_app()
    assert mgr._connection is None
    assert mgr._app is None

    # The first failure trips the breaker.  In production the bot
    # would back off; in this test we simulate "enough time passed"
    # by clearing it manually so we can assert the second call
    # reconnects cleanly (the bug under test is about cache
    # invalidation, not about the breaker timing).
    mgr._reset_breaker()

    # Second _get_app(): re-creates conn #2, returns good_app cleanly.
    app = await mgr._get_app()
    assert app is good_app
    assert create_calls["n"] == 2


async def test_invalidate_connection_forces_reconnect() -> None:
    """After a transient runtime failure, callers can invalidate the
    cache and the next _get_connection call reconnects from scratch."""
    first = MagicMock(spec=[])
    second = MagicMock(spec=[])
    sequence = iter([first, second])

    async def create_one() -> Any:
        return next(sequence)

    mgr = _fresh_manager()
    with patch("iterm2.Connection.async_create", create_one):
        a = await mgr._get_connection()
        mgr._invalidate_connection()
        b = await mgr._get_connection()

    assert a is first
    assert b is second


# ----------------------------------------------------------------------
# Unit 3: send_keys + capture_pane
# ----------------------------------------------------------------------


def _bind_session(mgr: ITerm2Manager, session: MagicMock) -> Any:
    """Patch the manager so _resolve_session returns ``session``."""
    return patch.object(mgr, "_resolve_session", AsyncMock(return_value=session))


async def test_send_keys_literal_with_enter_uses_two_phase_timing(
    monkeypatch: Any,
) -> None:
    """Literal text with Enter sends text → 500ms gap → \\r."""
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("ccbot.iterm2_manager._sleep", record_sleep)

    session = MagicMock()
    sent: list[str] = []
    session.async_send_text = AsyncMock(side_effect=lambda t: sent.append(t))

    mgr = _fresh_manager()
    with _bind_session(mgr, session):
        ok = await mgr.send_keys("UUID", "hello")

    assert ok is True
    assert sent == ["hello", "\r"]
    assert sleeps == [0.5]


async def test_send_keys_bash_prefix_inserts_one_second_gap(
    monkeypatch: Any,
) -> None:
    """``!cmd`` sends ``!`` first, waits 1s, then the rest, then Enter."""
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("ccbot.iterm2_manager._sleep", record_sleep)

    session = MagicMock()
    sent: list[str] = []
    session.async_send_text = AsyncMock(side_effect=lambda t: sent.append(t))

    mgr = _fresh_manager()
    with _bind_session(mgr, session):
        ok = await mgr.send_keys("UUID", "!ls")

    assert ok is True
    assert sent == ["!", "ls", "\r"]
    assert sleeps == [1.0, 0.5]


async def test_send_keys_bash_prefix_alone_skips_extra_send(
    monkeypatch: Any,
) -> None:
    """``!`` with no rest must not waste a 1s sleep."""
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("ccbot.iterm2_manager._sleep", record_sleep)

    session = MagicMock()
    sent: list[str] = []
    session.async_send_text = AsyncMock(side_effect=lambda t: sent.append(t))

    mgr = _fresh_manager()
    with _bind_session(mgr, session):
        ok = await mgr.send_keys("UUID", "!")

    assert ok is True
    assert sent == ["!", "\r"]
    assert sleeps == [0.5]


@pytest.mark.parametrize(
    "name,sequence",
    [
        ("Up", "\x1b[A"),
        ("Down", "\x1b[B"),
        ("Right", "\x1b[C"),
        ("Left", "\x1b[D"),
        ("Escape", "\x1b"),
        ("Tab", "\t"),
        ("Enter", "\r"),
        # The picker keyboard's "␣ Space" button. Missing from the table, it
        # fell through the permissive branch below and typed the five letters
        # S-p-a-c-e into the TUI — the one key an AskUserQuestion checkbox
        # actually needs.
        ("Space", " "),
    ],
)
async def test_send_keys_special_keys_translate_correctly(
    name: str, sequence: str
) -> None:
    """Named keys with literal=False expand to escape sequences."""
    session = MagicMock()
    sent: list[str] = []
    session.async_send_text = AsyncMock(side_effect=lambda t: sent.append(t))

    mgr = _fresh_manager()
    with _bind_session(mgr, session):
        ok = await mgr.send_keys("UUID", name, enter=False, literal=False)

    assert ok is True
    assert sent == [sequence]


async def test_send_keys_unknown_special_key_falls_through_literal() -> None:
    """A literal=False key that isn't in the table is sent as-is, matching
    the previous tmux backend's permissive behaviour."""
    session = MagicMock()
    sent: list[str] = []
    session.async_send_text = AsyncMock(side_effect=lambda t: sent.append(t))

    mgr = _fresh_manager()
    with _bind_session(mgr, session):
        ok = await mgr.send_keys("UUID", "MysteryKey", enter=False, literal=False)

    assert ok is True
    assert sent == ["MysteryKey"]


async def test_send_keys_literal_no_enter_sends_text_only() -> None:
    session = MagicMock()
    sent: list[str] = []
    session.async_send_text = AsyncMock(side_effect=lambda t: sent.append(t))

    mgr = _fresh_manager()
    with _bind_session(mgr, session):
        ok = await mgr.send_keys("UUID", "\x1b", enter=False, literal=True)

    assert ok is True
    assert sent == ["\x1b"]


async def test_send_keys_returns_false_when_session_missing() -> None:
    mgr = _fresh_manager()
    with patch.object(mgr, "_resolve_session", AsyncMock(return_value=None)):
        ok = await mgr.send_keys("UUID-MISSING", "hello")
    assert ok is False


async def test_send_keys_returns_false_on_send_error() -> None:
    session = MagicMock()
    session.async_send_text = AsyncMock(side_effect=RuntimeError("disconnected"))
    mgr = _fresh_manager()
    with _bind_session(mgr, session):
        ok = await mgr.send_keys("UUID", "hi", enter=False)
    assert ok is False


# --- capture_pane plain mode ---


def _make_screen_contents(lines: list[tuple[str, list[Any]]]) -> MagicMock:
    """Build a fake ScreenContents from (text, [style_or_None_per_char]) tuples."""
    contents = MagicMock()
    contents.number_of_lines = len(lines)

    line_objects: list[MagicMock] = []
    for text, styles in lines:
        lc = MagicMock()
        lc.string = text
        # style_at(x) → styles[x]; out-of-range returns None
        lc.style_at = MagicMock(
            side_effect=lambda x, s=styles: s[x] if x < len(s) else None
        )
        line_objects.append(lc)

    contents.line = MagicMock(side_effect=lambda i: line_objects[i])
    return contents


async def test_capture_pane_plain_joins_lines_with_newline() -> None:
    contents = _make_screen_contents(
        [("first line", [None] * 10), ("second", [None] * 6)]
    )
    session = MagicMock()
    session.async_get_screen_contents = AsyncMock(return_value=contents)

    mgr = _fresh_manager()
    with _bind_session(mgr, session):
        out = await mgr.capture_pane("UUID")

    assert out == "first line\nsecond"


async def test_capture_pane_returns_none_when_session_missing() -> None:
    mgr = _fresh_manager()
    with patch.object(mgr, "_resolve_session", AsyncMock(return_value=None)):
        assert await mgr.capture_pane("UUID-MISSING") is None


async def test_capture_pane_returns_none_on_screen_error() -> None:
    session = MagicMock()
    session.async_get_screen_contents = AsyncMock(side_effect=RuntimeError("nope"))
    mgr = _fresh_manager()
    with _bind_session(mgr, session):
        assert await mgr.capture_pane("UUID") is None


# --- capture_pane ANSI round-trip ---


def _style(
    fg_standard: int | None = None,
    fg_rgb: tuple[int, int, int] | None = None,
    bg_standard: int | None = None,
) -> MagicMock:
    """Build a minimal CellStyle stand-in that mirrors iTerm2's
    ``CellStyle.Color`` discriminator API: probe via ``is_standard``
    / ``is_rgb`` / ``is_alternate``; the typed accessors raise when
    the colour isn't of that kind."""
    style = MagicMock()

    def _color(standard: int | None, rgb: tuple[int, int, int] | None) -> Any:
        if standard is None and rgb is None:
            return None
        c = MagicMock()
        c.is_standard = standard is not None
        c.is_rgb = rgb is not None
        c.is_alternate = False
        c.standard = standard if standard is not None else None
        if rgb is not None:
            rgb_obj = MagicMock()
            rgb_obj.red, rgb_obj.green, rgb_obj.blue = rgb
            c.rgb = rgb_obj
        else:
            c.rgb = None
        return c

    style.fg_color = _color(fg_standard, fg_rgb)
    style.bg_color = _color(bg_standard, None)
    return style


async def test_capture_pane_ansi_emits_basic_16_colour() -> None:
    """fg standard 0-7 → SGR 30-37."""
    red = _style(fg_standard=1)  # ANSI red
    contents = _make_screen_contents([("X", [red])])
    session = MagicMock()
    session.async_get_screen_contents = AsyncMock(return_value=contents)

    mgr = _fresh_manager()
    with _bind_session(mgr, session):
        out = await mgr.capture_pane("UUID", with_ansi=True)

    assert out is not None
    assert "\x1b[31" in out  # 30 + 1 = red
    assert out.endswith("\x1b[0m")


async def test_capture_pane_ansi_emits_bright_palette() -> None:
    """fg standard 8-15 → SGR 90-97."""
    bright_red = _style(fg_standard=9)
    contents = _make_screen_contents([("X", [bright_red])])
    session = MagicMock()
    session.async_get_screen_contents = AsyncMock(return_value=contents)

    mgr = _fresh_manager()
    with _bind_session(mgr, session):
        out = await mgr.capture_pane("UUID", with_ansi=True)

    assert out is not None
    assert "\x1b[91" in out  # 90 + (9 - 8) = 91


async def test_capture_pane_ansi_emits_extended_256() -> None:
    """fg standard ≥ 16 → SGR 38;5;N."""
    s = _style(fg_standard=200)
    contents = _make_screen_contents([("X", [s])])
    session = MagicMock()
    session.async_get_screen_contents = AsyncMock(return_value=contents)

    mgr = _fresh_manager()
    with _bind_session(mgr, session):
        out = await mgr.capture_pane("UUID", with_ansi=True)

    assert out is not None
    assert "38;5;200" in out


async def test_capture_pane_ansi_emits_rgb() -> None:
    """fg rgb → SGR 38;2;R;G;B."""
    s = _style(fg_rgb=(10, 20, 30))
    contents = _make_screen_contents([("X", [s])])
    session = MagicMock()
    session.async_get_screen_contents = AsyncMock(return_value=contents)

    mgr = _fresh_manager()
    with _bind_session(mgr, session):
        out = await mgr.capture_pane("UUID", with_ansi=True)

    assert out is not None
    assert "38;2;10;20;30" in out


async def test_capture_pane_ansi_round_trips_through_screenshot_parser() -> None:
    """Capture-with-ANSI → screenshot._parse_ansi_line preserves the
    foreground colour of each cell. This is the core invariant: the
    output dialect must match what screenshot.py parses."""
    from ccbot.screenshot import _ANSI_COLORS, _parse_ansi_line

    red = _style(fg_standard=1)  # screenshot maps to _ANSI_COLORS[1]
    cyan = _style(fg_standard=6)
    contents = _make_screen_contents([("RC", [red, cyan])])
    session = MagicMock()
    session.async_get_screen_contents = AsyncMock(return_value=contents)

    mgr = _fresh_manager()
    with _bind_session(mgr, session):
        ansi_line = await mgr.capture_pane("UUID", with_ansi=True)

    assert ansi_line is not None
    segments = _parse_ansi_line(ansi_line)
    text_to_fg = {seg.text: seg.style.fg_color for seg in segments if seg.text}

    assert text_to_fg["R"] == _ANSI_COLORS[1]
    assert text_to_fg["C"] == _ANSI_COLORS[6]


def test_color_to_sgr_does_not_touch_typed_accessors_unless_matching() -> None:
    """Regression: iTerm2's CellStyle.Color exposes ``standard`` /
    ``rgb`` as properties that *raise* when the colour isn't of that
    kind. The serializer must probe via ``is_*`` first; otherwise
    the very first cell with a non-standard colour throws
    ``ValueError("Not a standard color")`` and /screenshot dies."""
    from unittest.mock import PropertyMock, patch

    from ccbot.iterm2_manager import _color_to_sgr

    color = MagicMock()
    color.is_standard = False
    color.is_rgb = True
    color.is_alternate = False
    rgb_obj = MagicMock()
    rgb_obj.red, rgb_obj.green, rgb_obj.blue = (1, 2, 3)
    color.rgb = rgb_obj

    # Make ``.standard`` raise on access — as the real iTerm2 class does.
    raising = PropertyMock(side_effect=ValueError("Not a standard color"))
    with patch.object(type(color), "standard", raising, create=True):
        sgr = _color_to_sgr(color, is_fg=True)
    assert sgr == ("38", "2", "1", "2", "3")


async def test_capture_pane_ansi_omits_redundant_codes_for_same_style() -> None:
    """When two adjacent cells share a style, the second cell must NOT
    re-emit the SGR codes — keeps the output compact and round-trips
    cleanly through the parser."""
    s = _style(fg_standard=2)
    contents = _make_screen_contents([("AB", [s, s])])
    session = MagicMock()
    session.async_get_screen_contents = AsyncMock(return_value=contents)

    mgr = _fresh_manager()
    with _bind_session(mgr, session):
        out = await mgr.capture_pane("UUID", with_ansi=True)

    assert out is not None
    # Exactly one colour-change SGR before "A", then plain "B", then reset.
    assert out == "\x1b[32;49mAB\x1b[0m"


# ----------------------------------------------------------------------
# screenshot_session: pixel capture via screencapture(1)
# ----------------------------------------------------------------------


def _patch_subprocess(
    monkeypatch: Any,
    *,
    osascript_stdout: bytes = b"7927\n",
    osascript_returncode: int = 0,
    osascript_raises: type[BaseException] | None = None,
    screencapture_stderr: bytes = b"",
    screencapture_returncode: int = 0,
    png_payload: bytes | None = b"\x89PNG\r\n\x1a\nfake-image-data",
) -> list[list[str]]:
    """Stub out asyncio.create_subprocess_exec to dispatch by the binary
    being invoked.  Returns a list that captures each call's argv."""
    captured: list[list[str]] = []

    async def fake_exec(*args: str, **kwargs: Any) -> Any:
        captured.append(list(args))
        cmd = args[0]
        proc = MagicMock()
        if cmd.endswith("osascript"):
            if osascript_raises is not None:
                raise osascript_raises("simulated")
            proc.communicate = AsyncMock(return_value=(osascript_stdout, b""))
            proc.returncode = osascript_returncode
            return proc
        if cmd.endswith("screencapture"):
            if png_payload is not None and screencapture_returncode == 0:
                Path(args[-1]).write_bytes(png_payload)
            proc.communicate = AsyncMock(return_value=(b"", screencapture_stderr))
            proc.returncode = screencapture_returncode
            return proc
        raise AssertionError(f"unexpected subprocess: {args!r}")

    monkeypatch.setattr(
        "ccbot.iterm2_manager.asyncio.create_subprocess_exec", fake_exec
    )
    return captured


async def test_screenshot_session_returns_png_bytes_on_success(
    monkeypatch: Any,
) -> None:
    """Happy path: osascript yields a CGWindowID, screencapture -l writes
    a PNG, the bytes get returned."""
    fake_png = b"\x89PNG\r\n\x1a\nfake-image-data"
    captured = _patch_subprocess(
        monkeypatch, osascript_stdout=b"7927\n", png_payload=fake_png
    )

    mgr = _fresh_manager()
    out = await mgr.screenshot_session("DEAD-BEEF-1234")

    assert out == fake_png
    # First call: osascript with -e <script-containing-the-uuid>
    assert captured[0][0].endswith("osascript")
    assert captured[0][1] == "-e"
    assert "DEAD-BEEF-1234" in captured[0][2]
    assert "unique ID of s" in captured[0][2]
    # Second call: screencapture -l 7927 ... <out.png>
    assert captured[1][0].endswith("screencapture")
    assert "-l" in captured[1]
    assert "7927" in captured[1]
    assert captured[1][-1].endswith(".png")


async def test_screenshot_session_returns_none_when_session_missing(
    monkeypatch: Any,
) -> None:
    """osascript returns NOT_FOUND when no session matches the UUID."""
    captured = _patch_subprocess(monkeypatch, osascript_stdout=b"NOT_FOUND\n")

    mgr = _fresh_manager()
    out = await mgr.screenshot_session("UUID-MISSING")
    assert out is None
    # screencapture must not be invoked.
    assert all(not c[0].endswith("screencapture") for c in captured)


async def test_screenshot_session_rejects_invalid_uuid(monkeypatch: Any) -> None:
    """A UUID with non-hex/dash characters is refused before osascript
    runs, since it would be interpolated into the AppleScript."""
    captured = _patch_subprocess(monkeypatch)

    mgr = _fresh_manager()
    out = await mgr.screenshot_session('"; do bad things; "')
    assert out is None
    assert captured == []


async def test_screenshot_session_returns_none_when_screencapture_fails(
    monkeypatch: Any,
) -> None:
    """screencapture rc=1 with 'could not create image' is the typical
    signature of macOS Screen Recording permission being denied."""
    _patch_subprocess(
        monkeypatch,
        osascript_stdout=b"7927\n",
        screencapture_stderr=b"could not create image from window",
        screencapture_returncode=1,
    )

    mgr = _fresh_manager()
    out = await mgr.screenshot_session("DEAD-BEEF-1234")
    assert out is None


async def test_screenshot_session_returns_none_when_osascript_missing(
    monkeypatch: Any,
) -> None:
    """If /usr/bin/osascript is absent we can't locate the window."""
    _patch_subprocess(monkeypatch, osascript_raises=FileNotFoundError)

    mgr = _fresh_manager()
    out = await mgr.screenshot_session("DEAD-BEEF-1234")
    assert out is None


# ----------------------------------------------------------------------
# Unit 4: window lifecycle
# ----------------------------------------------------------------------


def _make_tab_with_session(session: MagicMock) -> MagicMock:
    """Build a Tab whose current_session and sessions list reference ``session``."""
    tab = MagicMock()
    tab.current_session = session
    tab.sessions = [session]
    return tab


def _make_window_for_create(new_session: MagicMock) -> MagicMock:
    """Build a Window whose async_create_tab returns a tab containing ``new_session``."""
    window = MagicMock()
    window.tabs = []
    new_tab = _make_tab_with_session(new_session)
    window.async_create_tab = AsyncMock(return_value=new_tab)
    return window


async def test_rename_window_calls_async_set_name() -> None:
    session = MagicMock()
    session.async_set_name = AsyncMock(return_value=None)

    mgr = _fresh_manager()
    with _bind_session(mgr, session):
        ok = await mgr.rename_window("UUID", "newname")

    assert ok is True
    session.async_set_name.assert_awaited_once_with("newname")


async def test_rename_window_returns_false_when_session_missing() -> None:
    mgr = _fresh_manager()
    with patch.object(mgr, "_resolve_session", AsyncMock(return_value=None)):
        ok = await mgr.rename_window("UUID-MISSING", "newname")
    assert ok is False


async def test_rename_window_returns_false_on_error() -> None:
    session = MagicMock()
    session.async_set_name = AsyncMock(side_effect=RuntimeError("boom"))
    mgr = _fresh_manager()
    with _bind_session(mgr, session):
        ok = await mgr.rename_window("UUID", "newname")
    assert ok is False


async def test_kill_window_force_closes_session() -> None:
    session = MagicMock()
    session.async_close = AsyncMock(return_value=None)

    mgr = _fresh_manager()
    with _bind_session(mgr, session):
        ok = await mgr.kill_window("UUID")

    assert ok is True
    session.async_close.assert_awaited_once_with(force=True)


async def test_kill_window_returns_false_when_session_missing() -> None:
    mgr = _fresh_manager()
    with patch.object(mgr, "_resolve_session", AsyncMock(return_value=None)):
        ok = await mgr.kill_window("UUID-MISSING")
    assert ok is False


async def test_create_window_rejects_missing_directory(tmp_path: Any) -> None:
    mgr = _fresh_manager()
    missing = tmp_path / "no-such-dir"
    ok, msg, name, uuid = await mgr.create_window(str(missing))
    assert ok is False
    assert "does not exist" in msg
    assert (name, uuid) == ("", "")


async def test_create_window_rejects_non_directory(tmp_path: Any) -> None:
    f = tmp_path / "afile"
    f.write_text("x")
    mgr = _fresh_manager()
    ok, msg, name, uuid = await mgr.create_window(str(f))
    assert ok is False
    assert "Not a directory" in msg


async def test_create_window_happy_path(tmp_path: Any) -> None:
    """Open tab → tag with ccbot=1 → set name → send cd && exec claude."""
    new_session = MagicMock()
    new_session.session_id = "UUID-NEW"
    sent: list[str] = []
    new_session.async_send_text = AsyncMock(side_effect=lambda t: sent.append(t))
    new_session.async_set_variable = AsyncMock(return_value=None)
    new_session.async_set_name = AsyncMock(return_value=None)

    host_window = _make_window_for_create(new_session)
    app = MagicMock()
    app.windows = [host_window]
    app.current_window = host_window
    app.async_refresh = AsyncMock(return_value=None)

    mgr = _fresh_manager()
    with (
        patch.object(mgr, "_get_app", AsyncMock(return_value=app)),
        # No existing windows have ccbot tabs yet, so _get_target_window
        # falls through to current_window.
    ):
        ok, msg, name, uuid = await mgr.create_window(str(tmp_path), window_name="proj")

    assert ok is True
    assert name == "proj"
    assert uuid == "UUID-NEW"
    new_session.async_set_variable.assert_awaited_once_with("user.ccbot", "1")
    new_session.async_set_name.assert_awaited_once_with("proj")
    # Boot command runs cd && exec claude in one shell line.
    assert len(sent) == 1
    assert "cd " in sent[0]
    assert "exec claude" in sent[0]
    assert sent[0].endswith("\n")


async def test_create_window_with_resume_id(tmp_path: Any) -> None:
    new_session = MagicMock()
    new_session.session_id = "UUID-NEW"
    sent: list[str] = []
    new_session.async_send_text = AsyncMock(side_effect=lambda t: sent.append(t))
    new_session.async_set_variable = AsyncMock(return_value=None)
    new_session.async_set_name = AsyncMock(return_value=None)

    host_window = _make_window_for_create(new_session)
    app = MagicMock()
    app.windows = [host_window]
    app.current_window = host_window
    app.async_refresh = AsyncMock(return_value=None)

    mgr = _fresh_manager()
    with patch.object(mgr, "_get_app", AsyncMock(return_value=app)):
        ok, _, _, _ = await mgr.create_window(
            str(tmp_path), window_name="x", resume_session_id="abc-123"
        )

    assert ok is True
    assert "--resume abc-123" in sent[0]


async def test_create_window_without_claude_skips_exec(tmp_path: Any) -> None:
    new_session = MagicMock()
    new_session.session_id = "UUID-NEW"
    sent: list[str] = []
    new_session.async_send_text = AsyncMock(side_effect=lambda t: sent.append(t))
    new_session.async_set_variable = AsyncMock(return_value=None)
    new_session.async_set_name = AsyncMock(return_value=None)

    host_window = _make_window_for_create(new_session)
    app = MagicMock()
    app.windows = [host_window]
    app.current_window = host_window
    app.async_refresh = AsyncMock(return_value=None)

    mgr = _fresh_manager()
    with patch.object(mgr, "_get_app", AsyncMock(return_value=app)):
        ok, _, _, _ = await mgr.create_window(
            str(tmp_path), window_name="x", start_claude=False
        )

    assert ok is True
    assert "exec claude" not in sent[0]
    assert sent[0].startswith("cd ")


async def test_create_window_dedupes_existing_name(tmp_path: Any) -> None:
    """If a name is taken, the new tab gets a -2 suffix."""
    existing = _make_session("UUID-X", tag="1", name="proj")
    existing_tab = MagicMock()
    existing_tab.sessions = [existing]
    existing_window = MagicMock()
    existing_window.tabs = [existing_tab]

    new_session = MagicMock()
    new_session.session_id = "UUID-NEW"
    new_session.async_send_text = AsyncMock(return_value=None)
    new_session.async_set_variable = AsyncMock(return_value=None)
    new_session.async_set_name = AsyncMock(return_value=None)

    new_tab = _make_tab_with_session(new_session)
    existing_window.async_create_tab = AsyncMock(return_value=new_tab)

    app = MagicMock()
    app.windows = [existing_window]
    app.current_window = existing_window
    app.async_refresh = AsyncMock(return_value=None)

    mgr = _fresh_manager()
    with patch.object(mgr, "_get_app", AsyncMock(return_value=app)):
        ok, _, name, _ = await mgr.create_window(str(tmp_path), window_name="proj")

    assert ok is True
    assert name == "proj-2"
    new_session.async_set_name.assert_awaited_once_with("proj-2")


async def test_create_window_quotes_paths_with_spaces(tmp_path: Any) -> None:
    """Paths with spaces must be shlex-quoted to survive the cd command."""
    spaced = tmp_path / "has spaces"
    spaced.mkdir()

    new_session = MagicMock()
    new_session.session_id = "UUID-NEW"
    sent: list[str] = []
    new_session.async_send_text = AsyncMock(side_effect=lambda t: sent.append(t))
    new_session.async_set_variable = AsyncMock(return_value=None)
    new_session.async_set_name = AsyncMock(return_value=None)

    host_window = _make_window_for_create(new_session)
    app = MagicMock()
    app.windows = [host_window]
    app.current_window = host_window
    app.async_refresh = AsyncMock(return_value=None)

    mgr = _fresh_manager()
    with patch.object(mgr, "_get_app", AsyncMock(return_value=app)):
        ok, _, _, _ = await mgr.create_window(str(spaced), window_name="x")

    assert ok is True
    # The literal cd command must contain a quoted path so the shell
    # parses it as one argument.
    assert "'" in sent[0] or '"' in sent[0]
    assert "has spaces" in sent[0]


async def test_create_window_falls_back_to_default_profile(tmp_path: Any) -> None:
    """If async_create_tab(profile=...) raises (e.g. profile missing),
    we retry with no profile rather than failing the whole call."""
    new_session = MagicMock()
    new_session.session_id = "UUID-NEW"
    new_session.async_send_text = AsyncMock(return_value=None)
    new_session.async_set_variable = AsyncMock(return_value=None)
    new_session.async_set_name = AsyncMock(return_value=None)
    new_tab = _make_tab_with_session(new_session)

    host_window = MagicMock()
    host_window.tabs = []
    create_calls: list[tuple[Any, ...]] = []

    async def flaky_create_tab(
        profile: str | None = None,
        command: str | None = None,
        index: int | None = None,
        profile_customizations: Any = None,
    ) -> Any:
        create_calls.append((profile,))
        if profile is not None:
            raise RuntimeError("Unknown profile")
        return new_tab

    host_window.async_create_tab = flaky_create_tab

    app = MagicMock()
    app.windows = [host_window]
    app.current_window = host_window
    app.async_refresh = AsyncMock(return_value=None)

    mgr = _fresh_manager()
    with patch.object(mgr, "_get_app", AsyncMock(return_value=app)):
        ok, _, _, _ = await mgr.create_window(str(tmp_path), window_name="x")

    assert ok is True
    # First attempt with profile name, then fallback with no profile.
    assert len(create_calls) == 2
    assert create_calls[0] == ("ccbot",)
    assert create_calls[1] == (None,)


async def test_get_target_window_prefers_existing_ccbot_window(tmp_path: Any) -> None:
    """When some window already has ccbot tabs, new tabs go there
    rather than into the user's foreground window."""
    user_session = _make_session("UUID-USER", tag=None)
    user_tab = MagicMock()
    user_tab.sessions = [user_session]
    user_window = MagicMock()
    user_window.tabs = [user_tab]

    ccbot_session = _make_session("UUID-CC", tag="1")
    ccbot_tab = MagicMock()
    ccbot_tab.sessions = [ccbot_session]
    ccbot_window = MagicMock()
    ccbot_window.tabs = [ccbot_tab]

    app = MagicMock()
    app.windows = [user_window, ccbot_window]
    # current_window points at user_window — but the method should
    # prefer ccbot_window because it already hosts ccbot tabs.
    app.current_window = user_window

    mgr = _fresh_manager()
    target = await mgr._get_target_window(app)
    assert target is ccbot_window


async def test_reconnect_listener_can_reenter_get_connection(
    monkeypatch: Any,
) -> None:
    """A reconnect listener that calls back into _get_connection/_get_app
    must not deadlock.

    Regression: reconnect listeners were fired while _get_connection still
    held _connect_lock.  Listeners re-enter _get_connection via _get_app
    (list_windows → resolve_stale_ids → rebind), and asyncio.Lock is not
    reentrant, so the whole event loop froze on every iTerm2 restart.
    """
    monkeypatch.setattr("ccbot.iterm2_manager._RECONNECT_DELAYS", (0.0, 0.0, 0.0))

    mgr = _fresh_manager()
    # Pretend we've connected before, so the next connect is a *re*-connect
    # and reconnect listeners fire.
    mgr._ever_connected = True

    reentrant_conn: dict[str, Any] = {}

    async def listener() -> None:
        # Re-enter the connection path exactly like resolve_stale_ids does.
        reentrant_conn["conn"] = await mgr._get_connection()

    mgr.add_reconnect_listener(listener)

    real_conn = MagicMock(spec=[])
    with patch("iterm2.Connection.async_create", AsyncMock(return_value=real_conn)):
        # Guard against the deadlock hanging the suite.
        conn = await asyncio.wait_for(mgr._get_connection(), timeout=3)

    assert conn is real_conn
    # The listener ran and its re-entrant call returned the cached connection.
    assert reentrant_conn.get("conn") is real_conn


# ----------------------------------------------------------------------
# Hung-RPC guard
# ----------------------------------------------------------------------


async def test_public_call_gives_up_on_hung_rpc(monkeypatch: Any) -> None:
    """A never-answering iTerm2 RPC must not hang a public call forever.

    iTerm2's Python API awaits a bare Future per RPC
    (connection.async_dispatch_until_id).  When the websocket dies
    mid-request its read loop exits without resolving *or* cancelling
    that Future, so an unguarded await blocks for good — which is how a
    single iTerm2 quit silently froze the status poll loop for days.
    """
    monkeypatch.setattr("ccbot.iterm2_manager._CALL_TIMEOUT", 0.05)

    mgr = _fresh_manager()
    mgr._connection = MagicMock(spec=[])
    mgr._app = MagicMock(spec=[])

    async def never_answers(*_a: Any, **_kw: Any) -> Any:
        await asyncio.Future()  # never resolved, never cancelled by iTerm2

    monkeypatch.setattr(mgr, "_resolve_session", never_answers)

    result = await asyncio.wait_for(mgr.capture_pane("UUID-A"), timeout=3)

    assert result is None
    # The connection is dropped so the next call reconnects instead of
    # queueing behind the same dead websocket...
    assert mgr._connection is None
    assert mgr._app is None
    # ...and the breaker is open so polling loops back off meanwhile.
    assert not mgr.is_reachable()


async def test_hung_rpc_falls_back_per_method(monkeypatch: Any) -> None:
    """Each guarded method degrades to its own "unavailable" value."""
    monkeypatch.setattr("ccbot.iterm2_manager._CALL_TIMEOUT", 0.05)

    async def never_answers(*_a: Any, **_kw: Any) -> Any:
        await asyncio.Future()

    mgr = _fresh_manager()
    monkeypatch.setattr(mgr, "_get_app", never_answers)
    assert await asyncio.wait_for(mgr.list_windows(), timeout=3) == []

    mgr = _fresh_manager()
    monkeypatch.setattr(mgr, "_get_app", never_answers)
    assert await asyncio.wait_for(mgr.list_all_sessions(), timeout=3) == []

    mgr = _fresh_manager()
    monkeypatch.setattr(mgr, "_resolve_session", never_answers)
    assert await asyncio.wait_for(mgr.send_keys("UUID-A", "hi"), timeout=3) is False

    mgr = _fresh_manager()
    monkeypatch.setattr(mgr, "_resolve_session", never_answers)
    assert await asyncio.wait_for(mgr.kill_window("UUID-A"), timeout=3) is False

    mgr = _fresh_manager()
    monkeypatch.setattr(mgr, "_get_app", never_answers)
    ok, msg, _name, _uuid = await asyncio.wait_for(
        mgr.create_window("/tmp", start_claude=False), timeout=3
    )
    assert ok is False
    assert "timed out" in msg


class TestStartClaude:
    """iTerm2 needs no session key: it injects ITERM_SESSION_ID into every
    shell, so the hook identifies the session on its own."""

    async def test_sends_the_bare_command(self) -> None:
        from unittest.mock import AsyncMock, patch

        from ccbot.iterm2_manager import ITerm2Manager

        mgr = ITerm2Manager()
        with patch.object(mgr, "send_keys", AsyncMock(return_value=True)) as sk:
            assert await mgr.start_claude("W") is True
        assert sk.await_args.args[1] == "claude"
        assert "CCBOT_SESSION_KEY" not in sk.await_args.args[1]

    async def test_resume_appends_quoted_session_id(self) -> None:
        from unittest.mock import AsyncMock, patch

        from ccbot.iterm2_manager import ITerm2Manager

        mgr = ITerm2Manager()
        with patch.object(mgr, "send_keys", AsyncMock(return_value=True)) as sk:
            await mgr.start_claude("W", resume_session_id="abc-123")
        assert sk.await_args.args[1] == "claude --resume abc-123"

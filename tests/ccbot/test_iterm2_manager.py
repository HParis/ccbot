"""Tests for iterm2_manager — scaffolding parity (Unit 1) plus
connection lifecycle and ccbot-tag-filtered discovery (Unit 2).

Live API calls go through pytest.mark.integration; the rest run
against an in-process fake iTerm2 app so no GUI is required.
"""

from __future__ import annotations

import inspect
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


def test_signature_parity_with_tmux_manager() -> None:
    """Method signatures match the previous TmuxManager 1:1.

    During the migration window tmux_manager.py still exists; once
    Unit 6 deletes it this test is removed in the same change-set.
    """
    try:
        from ccbot.tmux_manager import TmuxManager  # type: ignore[attr-defined]
    except ImportError:
        pytest.skip("tmux_manager already removed")

    for name in (
        "list_windows",
        "find_window_by_name",
        "find_window_by_id",
        "capture_pane",
        "send_keys",
        "rename_window",
        "kill_window",
        "create_window",
    ):
        old = inspect.signature(getattr(TmuxManager, name))
        new = inspect.signature(getattr(ITerm2Manager, name))
        # Compare parameter names and kinds; ignore annotations because
        # the new module uses ITermWindow vs TmuxWindow returns.
        old_params = [(p.name, p.kind, p.default) for p in old.parameters.values()]
        new_params = [(p.name, p.kind, p.default) for p in new.parameters.values()]
        assert old_params == new_params, (
            f"{name}: signature drift — old={old_params} new={new_params}"
        )


@pytest.mark.parametrize(
    "method_name,args",
    [
        # Unit 3
        ("capture_pane", ("UUID",)),
        ("send_keys", ("UUID", "hi")),
        # Unit 4
        ("rename_window", ("UUID", "new")),
        ("kill_window", ("UUID",)),
        ("create_window", ("/tmp",)),
    ],
)
async def test_stubs_raise_not_implemented(
    method_name: str, args: tuple[object, ...]
) -> None:
    """Methods not yet implemented raise NotImplementedError so
    accidental upper-layer use during the migration fails loudly.
    """
    method = getattr(iterm2_manager, method_name)
    with pytest.raises(NotImplementedError):
        await method(*args)


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
    # Speed up backoff so the test doesn't actually sleep 1+2 seconds.
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
    monkeypatch.setattr("ccbot.iterm2_manager._RECONNECT_DELAYS", (0.0, 0.0, 0.0))

    async def always_fail() -> Any:
        raise OSError("iTerm2 missing")

    mgr = _fresh_manager()
    with patch("iterm2.Connection.async_create", always_fail):
        with pytest.raises(ConnectionError, match="iTerm2"):
            await mgr._get_connection()


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

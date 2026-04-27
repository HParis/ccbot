"""Tests for iterm2_manager — scaffolding parity (Unit 1) plus
connection lifecycle and ccbot-tag-filtered discovery (Unit 2).

Live API calls go through pytest.mark.integration; the rest run
against an in-process fake iTerm2 app so no GUI is required.
"""

from __future__ import annotations

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


def _make_app_with_session(
    session_uuid: str,
    frame_origin: tuple[float, float],
    frame_size: tuple[float, float],
) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Build an App graph with one window, one tab, one session."""
    from iterm2.util import Frame, Point, Size

    sess = MagicMock()
    sess.session_id = session_uuid

    tab = MagicMock()
    tab.sessions = [sess]
    tab.async_select = AsyncMock(return_value=None)

    window = MagicMock()
    window.tabs = [tab]
    frame = Frame(Point(*frame_origin), Size(*frame_size))
    window.async_get_frame = AsyncMock(return_value=frame)

    app = MagicMock()
    app.windows = [window]
    app.async_refresh = AsyncMock(return_value=None)
    app.get_session_by_id = MagicMock(
        side_effect=lambda sid, include_buried=True: (
            sess if sid == session_uuid else None
        )
    )
    return app, window, tab


async def test_screenshot_session_returns_png_bytes_on_success(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Happy path: tab gets selected, frame is converted to screen
    coords, screencapture is invoked, the resulting file is returned."""
    app, window, tab = _make_app_with_session(
        "UUID-A", frame_origin=(100.0, 200.0), frame_size=(800.0, 600.0)
    )

    # Pretend main screen is 1080 logical points tall.
    async def fake_screen_height() -> float:
        return 1080.0

    monkeypatch.setattr("ccbot.iterm2_manager._main_screen_height", fake_screen_height)

    captured_args: list[str] = []
    fake_png = b"\x89PNG\r\n\x1a\n" + b"fake-image-data"

    async def fake_subprocess_exec(*args: str, **kwargs: Any) -> Any:
        # Record the screencapture invocation.
        captured_args.extend(args)
        # Find the output path (last positional arg after -t png).
        out_path = Path(args[-1])
        out_path.write_bytes(fake_png)

        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        return proc

    monkeypatch.setattr(
        "ccbot.iterm2_manager.asyncio.create_subprocess_exec",
        fake_subprocess_exec,
    )

    mgr = _fresh_manager()
    with patch.object(mgr, "_get_app", AsyncMock(return_value=app)):
        out = await mgr.screenshot_session("UUID-A")

    assert out == fake_png
    # Tab was selected and screencapture was called with -R rect.
    tab.async_select.assert_awaited_once()
    assert "screencapture" in captured_args[0]
    assert "-R" in captured_args
    rect_idx = captured_args.index("-R") + 1
    rect = captured_args[rect_idx]
    # Cocoa origin (100, 200), size (800, 600), screen height 1080
    # screen_y = 1080 - 200 - 600 = 280
    assert rect == "100,280,800,600"


async def test_screenshot_session_returns_none_when_session_missing(
    monkeypatch: Any,
) -> None:
    app = MagicMock()
    app.windows = []
    app.async_refresh = AsyncMock(return_value=None)
    app.get_session_by_id = MagicMock(return_value=None)

    mgr = _fresh_manager()
    with patch.object(mgr, "_get_app", AsyncMock(return_value=app)):
        out = await mgr.screenshot_session("UUID-MISSING")
    assert out is None


async def test_screenshot_session_returns_none_when_screencapture_fails(
    monkeypatch: Any,
) -> None:
    """Most likely cause of a non-zero return: macOS Screen Recording
    permission isn't granted to the bot's executable."""
    app, window, tab = _make_app_with_session(
        "UUID-A", frame_origin=(0.0, 0.0), frame_size=(100.0, 100.0)
    )

    async def fake_screen_height() -> float:
        return 1080.0

    monkeypatch.setattr("ccbot.iterm2_manager._main_screen_height", fake_screen_height)

    async def failing_subprocess_exec(*args: str, **kwargs: Any) -> Any:
        proc = MagicMock()
        proc.communicate = AsyncMock(
            return_value=(b"", b"could not create image from rect")
        )
        proc.returncode = 1
        return proc

    monkeypatch.setattr(
        "ccbot.iterm2_manager.asyncio.create_subprocess_exec",
        failing_subprocess_exec,
    )

    mgr = _fresh_manager()
    with patch.object(mgr, "_get_app", AsyncMock(return_value=app)):
        out = await mgr.screenshot_session("UUID-A")
    assert out is None


async def test_screenshot_session_returns_none_when_screen_height_unknown(
    monkeypatch: Any,
) -> None:
    app, _, _ = _make_app_with_session(
        "UUID-A", frame_origin=(0.0, 0.0), frame_size=(100.0, 100.0)
    )

    async def no_height() -> float | None:
        return None

    monkeypatch.setattr("ccbot.iterm2_manager._main_screen_height", no_height)

    mgr = _fresh_manager()
    with patch.object(mgr, "_get_app", AsyncMock(return_value=app)):
        out = await mgr.screenshot_session("UUID-A")
    assert out is None


async def test_screenshot_session_survives_failed_tab_select(
    monkeypatch: Any,
) -> None:
    """If async_select() throws (e.g. iTerm2 racing window close),
    the screenshot still proceeds with whatever the window currently
    shows rather than aborting."""
    app, window, tab = _make_app_with_session(
        "UUID-A", frame_origin=(0.0, 0.0), frame_size=(100.0, 100.0)
    )
    tab.async_select = AsyncMock(side_effect=RuntimeError("race"))

    async def fake_screen_height() -> float:
        return 1080.0

    monkeypatch.setattr("ccbot.iterm2_manager._main_screen_height", fake_screen_height)

    fake_png = b"\x89PNG\r\n\x1a\n" + b"x"

    async def fake_subprocess_exec(*args: str, **kwargs: Any) -> Any:
        Path(args[-1]).write_bytes(fake_png)
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        return proc

    monkeypatch.setattr(
        "ccbot.iterm2_manager.asyncio.create_subprocess_exec",
        fake_subprocess_exec,
    )

    mgr = _fresh_manager()
    with patch.object(mgr, "_get_app", AsyncMock(return_value=app)):
        out = await mgr.screenshot_session("UUID-A")
    assert out == fake_png


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

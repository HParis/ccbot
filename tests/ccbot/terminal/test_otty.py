"""Tests for the Otty terminal backend.

Drive ``OttyManager`` through a fake ``CliRunner`` that records invocations
and returns canned otty-cli JSON, so behavior is verified without a live
Otty app: ownership filtering, pane->tab resolution, send-keys part building
and timing phases, create-by-diff, and graceful failure handling.
"""

from __future__ import annotations

import json

import pytest

from ccbot.terminal import Capabilities, TerminalBackend
from ccbot.terminal import registry
from ccbot.terminal.otty import OttyManager


def _ok(data: object) -> tuple[int, str, str]:
    return 0, json.dumps({"ok": True, "data": data}), ""


def _fail(rc: int = 1, err: str = "boom") -> tuple[int, str, str]:
    return rc, "", err


class FakeCli:
    """Records otty-cli arg lists and replies from a programmable script.

    ``panes`` is the current pane table returned by ``pane list``; mutate it
    between calls to simulate tabs appearing/closing.
    """

    def __init__(self, panes: list[dict] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.panes: list[dict] = panes or []
        self.capture_text = "hello world"

    async def __call__(self, args: list[str]) -> tuple[int, str, str]:
        self.calls.append(args)
        # Strip the leading cli path and global flags (incl. flag values) so
        # toks[:2] is the subcommand pair regardless of --json/--timeout/--socket.
        rest = args[1:]
        toks: list[str] = []
        skip = False
        for a in rest:
            if skip:
                skip = False
                continue
            if a == "--json":
                continue
            if a in ("--timeout", "--socket"):
                skip = True
                continue
            toks.append(a)

        if toks[:2] == ["window", "list"]:
            return _ok([{"id": "w_1"}])
        if toks[:2] == ["pane", "list"]:
            return _ok(self.panes)
        if toks[:2] == ["pane", "capture"]:
            return 0, self.capture_text, ""
        if toks[:2] == ["pane", "send-keys"]:
            return _ok("sent")
        if toks[:2] == ["tab", "rename"]:
            return _ok("renamed")
        if toks[:2] == ["tab", "close"]:
            return _ok("closed")
        if toks[:2] == ["tab", "new"]:
            return _ok("Tab created")
        return _fail(err=f"unhandled: {toks}")

    def last_send_keys(self) -> list[list[str]]:
        return [c for c in self.calls if c[1:][:2] == ["pane", "send-keys"]] or [
            c for c in self.calls if "send-keys" in c
        ]


def _mgr(fake: FakeCli) -> OttyManager:
    return OttyManager(cli_path="/fake/otty-cli", socket_path="", runner=fake)


async def test_capabilities_declare_otty_limits() -> None:
    caps = _mgr(FakeCli()).capabilities
    assert isinstance(caps, Capabilities)
    assert caps.ansi_capture is False
    assert caps.native_tagging is False
    assert caps.reconnect_events is False
    assert caps.screenshot is True


async def test_satisfies_backend_protocol() -> None:
    assert isinstance(_mgr(FakeCli()), TerminalBackend)


def test_registered_in_registry() -> None:
    assert "otty" in registry.available()


async def test_preflight_raises_when_unreachable(monkeypatch) -> None:
    monkeypatch.setattr("ccbot.terminal.otty._LAUNCH_DELAYS", (0.0,))

    async def dead(args: list[str]) -> tuple[int, str, str]:
        return _fail(rc=1, err="no app")

    mgr = OttyManager(cli_path="/fake/otty-cli", socket_path="", runner=dead)
    with pytest.raises(ConnectionError):
        await mgr.preflight()
    assert mgr.is_reachable() is False


async def test_preflight_auto_launches_then_succeeds(monkeypatch) -> None:
    monkeypatch.setattr("ccbot.terminal.otty._LAUNCH_DELAYS", (0.0, 0.0))
    state = {"up": False, "opened": False}

    async def runner(args: list[str]) -> tuple[int, str, str]:
        if args[:2] == ["/usr/bin/open", "-g"]:
            state["opened"] = True
            state["up"] = True  # app comes up after launch
            return 0, "", ""
        # window list ping
        if not state["up"]:
            return _fail(rc=1, err="no app")
        return _ok([{"id": "w_1"}])

    mgr = OttyManager(cli_path="/fake/otty-cli", socket_path="", runner=runner)
    await mgr.preflight()  # must not raise
    assert state["opened"] is True


async def test_list_windows_only_returns_owned_panes() -> None:
    fake = FakeCli(
        panes=[
            {
                "id": "p_a",
                "tab_id": "t_a",
                "cwd": "/x",
                "process": "claude",
                "title": "A",
            },
            {"id": "p_b", "tab_id": "t_b", "cwd": "/y", "process": "zsh", "title": "B"},
        ]
    )
    mgr = _mgr(fake)
    # Nothing owned yet -> empty, even though panes exist.
    assert await mgr.list_windows() == []

    mgr._owned.add("p_a")
    owned = await mgr.list_windows()
    assert [w.window_id for w in owned] == ["p_a"]
    assert owned[0].cwd == "/x"
    assert owned[0].is_ccbot is True


async def test_list_all_sessions_flags_ownership_and_claude() -> None:
    fake = FakeCli(
        panes=[
            {
                "id": "p_a",
                "tab_id": "t_a",
                "cwd": "/x",
                "process": "claude",
                "title": "A",
            },
            {"id": "p_b", "tab_id": "t_b", "cwd": "/y", "process": "zsh", "title": "B"},
        ]
    )
    mgr = _mgr(fake)
    mgr._owned.add("p_a")
    sessions = await mgr.list_all_sessions(claude_session_uuids={"p_b"})
    by_id = {s.window_id: s for s in sessions}
    assert by_id["p_a"].is_ccbot is True and by_id["p_a"].has_claude is False
    assert by_id["p_b"].is_ccbot is False and by_id["p_b"].has_claude is True


async def test_capture_pane_returns_plain_text() -> None:
    fake = FakeCli(panes=[{"id": "p_a", "tab_id": "t_a"}])
    fake.capture_text = "line1\nline2"
    out = await _mgr(fake).capture_pane("p_a", with_ansi=True)
    assert out == "line1\nline2"
    cap = [c for c in fake.calls if "capture" in c][0]
    assert "--trim" in cap and "--pane" in cap and "p_a" in cap
    assert "--json" not in cap  # raw text, not JSON


async def test_send_keys_literal_enter_is_two_phase() -> None:
    fake = FakeCli(panes=[{"id": "p_a", "tab_id": "t_a"}])
    assert await _mgr(fake).send_keys("p_a", "hello", enter=True, literal=True)
    sends = [c for c in fake.calls if "send-keys" in c]
    assert len(sends) == 2  # text, then Enter, as separate events
    assert "hello" in sends[0] and "key:Enter" not in sends[0]
    assert "key:Enter" in sends[1]


async def test_send_keys_bash_prefix_splits_bang() -> None:
    fake = FakeCli(panes=[{"id": "p_a", "tab_id": "t_a"}])
    assert await _mgr(fake).send_keys("p_a", "!ls", enter=True, literal=True)
    sends = [c for c in fake.calls if "send-keys" in c]
    # "!" first, then "ls", then Enter.
    assert "!" in sends[0]
    assert "ls" in sends[1]
    assert "key:Enter" in sends[2]


async def test_send_keys_named_key_maps_to_key_part() -> None:
    fake = FakeCli(panes=[{"id": "p_a", "tab_id": "t_a"}])
    assert await _mgr(fake).send_keys("p_a", "Escape", enter=False, literal=False)
    send = [c for c in fake.calls if "send-keys" in c][0]
    assert "key:Escape" in send


async def test_rename_resolves_tab_id_from_pane() -> None:
    fake = FakeCli(panes=[{"id": "p_a", "tab_id": "t_zzz"}])
    assert await _mgr(fake).rename_window("p_a", "newname")
    rename = [c for c in fake.calls if "rename" in c][0]
    assert "t_zzz" in rename and "newname" in rename


async def test_kill_window_closes_tab_and_unowns() -> None:
    fake = FakeCli(panes=[{"id": "p_a", "tab_id": "t_a"}])
    mgr = _mgr(fake)
    mgr._owned.add("p_a")
    assert await mgr.kill_window("p_a")
    close = [c for c in fake.calls if "close" in c][0]
    assert "t_a" in close and "--force" in close
    # Closing a busy TUI tab needs a longer IPC timeout than the 3s default.
    assert "--timeout" in close
    assert "p_a" not in mgr._owned


async def test_create_window_resolves_new_pane_by_diff(tmp_path) -> None:
    work = tmp_path
    existing = {"id": "p_old", "tab_id": "t_old", "cwd": "/other"}

    class CreatingCli(FakeCli):
        async def __call__(self, args: list[str]) -> tuple[int, str, str]:
            toks = [a for a in args[1:] if a != "--json"]
            if toks[:2] == ["tab", "new"]:
                # Simulate the new pane appearing after creation.
                self.panes = self.panes + [
                    {"id": "p_new", "tab_id": "t_new", "cwd": str(work)}
                ]
            return await super().__call__(args)

    fake = CreatingCli(panes=[existing])
    mgr = OttyManager(cli_path="/fake/otty-cli", socket_path="", runner=fake)
    ok, msg, name, wid = await mgr.create_window(
        str(work), window_name="proj", start_claude=False
    )
    assert ok is True
    assert wid == "p_new"
    assert name == "proj"
    assert "p_new" in mgr._owned


async def test_create_window_rejects_missing_dir() -> None:
    fake = FakeCli()
    ok, msg, name, wid = await _mgr(fake).create_window("/nope/not/here")
    assert ok is False and wid == ""
    assert "does not exist" in msg


async def test_create_window_fails_gracefully_when_otty_down(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr("ccbot.terminal.otty._LAUNCH_DELAYS", (0.0,))

    async def dead(args: list[str]) -> tuple[int, str, str]:
        return _fail(rc=3, err="Cannot connect to Otty. Is Otty running?")

    mgr = OttyManager(cli_path="/fake/otty-cli", socket_path="", runner=dead)
    ok, msg, name, wid = await mgr.create_window(str(tmp_path))
    assert ok is False and wid == ""
    assert "could not be launched" in msg


async def test_create_window_injects_session_key_when_starting_claude(
    tmp_path,
) -> None:
    work = tmp_path

    class CreatingCli(FakeCli):
        async def __call__(self, args: list[str]) -> tuple[int, str, str]:
            toks = [a for a in args[1:] if a != "--json"]
            if toks[:2] == ["tab", "new"]:
                self.panes = self.panes + [
                    {"id": "p_new", "tab_id": "t_new", "cwd": str(work)}
                ]
            return await super().__call__(args)

    fake = CreatingCli(panes=[])
    mgr = OttyManager(cli_path="/fake/otty-cli", socket_path="", runner=fake)
    ok, _msg, _name, wid = await mgr.create_window(str(work), start_claude=True)
    assert ok and wid == "p_new"

    # The claude launch is sent via send-keys and carries the session_map key
    # the hook must write (otty:<pane_id>). Two-phase send means the launch
    # text and the Enter are separate calls, so scan all of them.
    sends = [c for c in fake.calls if "send-keys" in c]
    launch_parts = [c for c in sends if "CCBOT_SESSION_KEY=otty:p_new claude" in c]
    assert launch_parts, f"launch not sent; calls={sends}"
    # tab new must NOT carry --command (2-phase: shell first, then claude).
    tab_new = next(c for c in fake.calls if "tab" in c and "new" in c)
    assert "--command" not in tab_new

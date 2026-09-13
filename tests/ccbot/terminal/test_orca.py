"""Tests for the Orca terminal backend.

Drive ``OrcaManager`` through a fake ``CliRunner`` that records invocations
and returns canned orca CLI envelopes, so behavior is verified without a
live Orca app: ownership filtering, the worktree-only workspace model,
screen-vs-stream capture, byte-level key mapping, and the session-key
injection the SessionStart hook depends on.

The canned payloads mirror what the real CLI returned on a live instance
(orca 1.4.200).
"""

from __future__ import annotations

import json

import pytest

from ccbot.terminal import Capabilities, TerminalBackend, registry
from ccbot.terminal.base import is_running_claude
from ccbot.terminal.orca import OrcaManager

HANDLE = "term_b562359d-824f-4c97-b755-bc0ef914cffc"
OTHER = "term_3bd32ef3-fed3-4809-b342-662f60934e84"


def _ok(result: object) -> tuple[int, str, str]:
    return 0, json.dumps({"id": "x", "ok": True, "result": result}), ""


def _err(code: str = "selector_not_found", rc: int = 1) -> tuple[int, str, str]:
    return rc, json.dumps({"id": "x", "ok": False, "error": {"code": code}}), ""


class FakeCli:
    """Records orca arg lists and replies from a programmable script."""

    def __init__(self, terminals: list[dict] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.terminals: list[dict] = terminals or []
        self.worktrees: list[dict] = [
            {
                "path": "/p/ccbot",
                "name": "ccbot",
                "branch": "refs/heads/refactor/iterm2-backend",
            },
            {"path": "/p/app", "branch": "main"},
        ]
        self.reachable = True
        self.screen_tail = ["line one", "line two"]
        self.create_ok = True

    async def __call__(self, args: list[str]) -> tuple[int, str, str]:
        self.calls.append(args)
        toks = [a for a in args[1:] if a != "--json"]

        if toks[:1] == ["status"]:
            return _ok({"runtime": {"reachable": self.reachable}})
        if toks[:1] == ["open"]:
            self.reachable = True
            return _ok({})
        if toks[:2] == ["terminal", "list"]:
            return _ok({"terminals": self.terminals})
        if toks[:2] == ["worktree", "list"]:
            return _ok({"worktrees": self.worktrees})
        if toks[:2] == ["terminal", "read"]:
            return _ok({"terminal": {"tail": self.screen_tail, "source": "screen"}})
        if toks[:2] == ["terminal", "send"]:
            return _ok({"accepted": True})
        if toks[:2] == ["terminal", "rename"]:
            return _ok({})
        if toks[:2] == ["terminal", "close"]:
            return _ok({})
        if toks[:2] == ["terminal", "create"]:
            if not self.create_ok:
                return _err()
            self.terminals.append(
                {"handle": HANDLE, "worktreePath": "/p/ccbot", "title": "new"}
            )
            return _ok({"terminal": {"handle": HANDLE, "title": "new"}})
        return _err(code=f"unhandled:{toks}")

    def sends(self) -> list[list[str]]:
        return [c for c in self.calls if c[1:3] == ["terminal", "send"]]


def _mgr(fake: FakeCli) -> OrcaManager:
    return OrcaManager(cli_path="/fake/orca", runner=fake)


def _text_of(call: list[str]) -> str:
    return call[call.index("--text") + 1]


class TestContract:
    async def test_capabilities_declare_orca_limits(self) -> None:
        caps = _mgr(FakeCli()).capabilities
        assert isinstance(caps, Capabilities)
        assert caps.ansi_capture is False
        assert caps.native_tagging is False
        assert caps.reconnect_events is False
        # The one that changes upper-layer UI: no filesystem browsing.
        assert caps.arbitrary_cwd is False

    async def test_satisfies_backend_protocol(self) -> None:
        assert isinstance(_mgr(FakeCli()), TerminalBackend)

    def test_registered_in_registry(self) -> None:
        assert "orca" in registry.available()

    def test_session_id_shape(self) -> None:
        m = _mgr(FakeCli())
        assert m.is_session_id(HANDLE)
        assert not m.is_session_id("term_not-a-uuid")
        assert not m.is_session_id("9F2E3A1B-DEAD-BEEF-CAFE-0123456789AB")

    def test_session_map_prefix(self) -> None:
        assert _mgr(FakeCli()).session_map_prefix == "orca:"


class TestReachability:
    async def test_ping_reads_nested_runtime_flag(self) -> None:
        """The app process can be up while its runtime is still starting, so
        runtime.reachable — not ok/app.running — is the signal."""
        fake = FakeCli()
        fake.reachable = False
        m = _mgr(fake)
        assert await m._ping() is False
        fake.reachable = True
        assert await m._ping() is True

    async def test_preflight_launches_then_succeeds(self) -> None:
        fake = FakeCli()
        fake.reachable = False
        m = _mgr(fake)
        await m.preflight()  # `orca open` flips reachable in the fake
        assert ["open"] == [a for a in fake.calls[1][1:] if a != "--json"]

    async def test_ensure_running_reports_failure_without_raising(self) -> None:
        class Dead(FakeCli):
            async def __call__(self, args):
                self.calls.append(args)
                return 1, "", "not running"

        assert await _mgr(Dead()).ensure_running() is False


class TestDiscovery:
    def _terms(self) -> list[dict]:
        return [
            {
                "handle": HANDLE,
                "worktreePath": "/p/ccbot",
                "title": "✳ ccbot",
                "agentIdentity": "claude",
            },
            {"handle": OTHER, "worktreePath": "/p/app", "title": "shell"},
        ]

    async def test_list_windows_only_returns_owned(self) -> None:
        m = _mgr(FakeCli(self._terms()))
        assert await m.list_windows() == []
        m._owned.add(HANDLE)
        assert [w.window_id for w in await m.list_windows()] == [HANDLE]

    async def test_list_all_sessions_maps_worktree_path_to_cwd(self) -> None:
        m = _mgr(FakeCli(self._terms()))
        sessions = await m.list_all_sessions({HANDLE})
        assert [s.cwd for s in sessions] == ["/p/ccbot", "/p/app"]
        assert [s.has_claude for s in sessions] == [True, False]

    async def test_agentless_terminal_reads_as_not_claude(self) -> None:
        """Orca omits agentIdentity for a plain shell. Leaving the job fields
        empty would hit is_running_claude's "can't tell" path and report that
        shell as a live Claude, which would let a rebind bind a topic to it.
        """
        m = _mgr(FakeCli(self._terms()))
        by_id = {s.window_id: s for s in await m.list_all_sessions()}
        assert is_running_claude(by_id[HANDLE]) is True
        assert is_running_claude(by_id[OTHER]) is False

    async def test_find_window_by_id_ignores_unowned(self) -> None:
        m = _mgr(FakeCli(self._terms()))
        assert await m.find_window_by_id(HANDLE) is None
        m._owned.add(HANDLE)
        found = await m.find_window_by_id(HANDLE)
        assert found is not None and found.window_name == "✳ ccbot"


class TestWorkspaces:
    async def test_lists_worktrees_with_short_branch(self) -> None:
        ws = await _mgr(FakeCli()).list_workspaces()
        assert [w.path for w in ws] == ["/p/ccbot", "/p/app"]
        assert ws[0].label == "ccbot"
        assert ws[0].detail == "refactor/iterm2-backend"  # refs/heads/ stripped

    async def test_label_falls_back_to_directory_name(self) -> None:
        ws = await _mgr(FakeCli()).list_workspaces()
        assert ws[1].label == "app"  # no "name" in the payload

    async def test_unreachable_backend_lists_nothing(self) -> None:
        class Dead(FakeCli):
            async def __call__(self, args):
                return 1, "", "down"

        assert await _mgr(Dead()).list_workspaces() == []


class TestIO:
    async def test_capture_requests_the_rendered_screen(self) -> None:
        """The default read returns accumulated output, where every repaint is
        stacked into unusable fragments; --screen is what a TUI needs."""
        fake = FakeCli()
        text = await _mgr(fake).capture_pane(HANDLE)
        assert text == "line one\nline two"
        assert "--screen" in fake.calls[0]

    async def test_literal_enter_is_two_phase(self) -> None:
        """Claude's TUI must see the text and the submitting Enter as separate
        events, so the Enter goes out as its own empty send."""
        fake = FakeCli()
        assert await _mgr(fake).send_keys(HANDLE, "hello") is True
        sends = fake.sends()
        assert [_text_of(c) for c in sends] == ["hello", ""]
        assert "--enter" not in sends[0]
        assert "--enter" in sends[1]

    async def test_bash_prefix_splits_the_bang(self) -> None:
        fake = FakeCli()
        await _mgr(fake).send_keys(HANDLE, "!ls -la")
        assert [_text_of(c) for c in fake.sends()] == ["!", "ls -la", ""]

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("Up", "\x1b[A"),
            ("Down", "\x1b[B"),
            ("Escape", "\x1b"),
            ("Tab", "\t"),
            ("Enter", "\r"),
            # The picker's ␣ button: a real space, not the word "Space".
            ("Space", " "),
        ],
    )
    async def test_named_keys_map_to_raw_bytes(self, name, expected) -> None:
        """`terminal send --text` is byte-transparent (verified against
        `cat -v`), so keys are escape sequences rather than CLI key names."""
        fake = FakeCli()
        await _mgr(fake).send_keys(HANDLE, name, enter=False, literal=False)
        assert _text_of(fake.sends()[0]) == expected

    async def test_screenshot_is_not_native(self) -> None:
        assert await _mgr(FakeCli()).screenshot_session(HANDLE) is None


class TestLifecycle:
    async def test_kill_window_unowns(self) -> None:
        fake = FakeCli([{"handle": HANDLE, "worktreePath": "/p/ccbot"}])
        m = _mgr(fake)
        m._owned.add(HANDLE)
        assert await m.kill_window(HANDLE) is True
        assert HANDLE not in m._owned

    async def test_create_window_injects_session_key(self) -> None:
        """Orca has no per-terminal env id, so the hook can only learn which
        terminal it is from CCBOT_SESSION_KEY typed into the launch command.
        """
        fake = FakeCli()
        m = _mgr(fake)
        ok, err, name, handle = await m.create_window("/p/ccbot", window_name="ccbot")
        assert (ok, err, handle) == (True, "", HANDLE)
        launch = _text_of(fake.sends()[0])
        assert launch.startswith(f"CCBOT_SESSION_KEY=orca:{HANDLE} ")
        assert handle in m._owned

    async def test_create_window_passes_a_path_selector(self) -> None:
        fake = FakeCli()
        await _mgr(fake).create_window("/p/ccbot", start_claude=False)
        create = next(c for c in fake.calls if c[1:3] == ["terminal", "create"])
        assert "--worktree" in create
        assert create[create.index("--worktree") + 1] == "path:/p/ccbot"
        # Never --focus: creating a session must not steal the user's tab.
        assert "--focus" not in create

    async def test_create_window_resume_passes_session_id(self) -> None:
        fake = FakeCli()
        await _mgr(fake).create_window("/p/ccbot", resume_session_id="abc-123")
        assert "--resume abc-123" in _text_of(fake.sends()[0])

    async def test_unregistered_directory_fails_with_a_usable_message(self) -> None:
        """Orca refuses any path it doesn't know as a worktree — including a
        subdirectory of a registered project — so the error has to tell the
        user what to do rather than read as a crash."""
        fake = FakeCli()
        fake.create_ok = False
        ok, err, _, _ = await _mgr(fake).create_window("/tmp/not-a-project")
        assert ok is False
        assert "registered projects only" in err

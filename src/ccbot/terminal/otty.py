"""Otty terminal backend.

Drives a running Otty app (v1.1.0+) through its bundled ``otty-cli`` over the
IPC control socket, implementing the ``TerminalBackend`` contract so ccbot can
host Claude Code sessions in Otty tabs instead of iTerm2.

The session identifier (``TerminalSession.window_id``) is the Otty **pane id**
(``p_*``); tab-level operations (rename/close) resolve the owning tab id via
``pane list``. 1 tab = 1 pane = 1 session.

Differences from iTerm2, declared via ``capabilities`` so upper layers degrade
instead of failing:
  - ``ansi_capture=False`` — ``pane capture --ansi`` emits no SGR color, so
    screenshots are monochrome (text content is still complete).
  - ``native_tagging=False`` — Otty has no per-session user variable and the
    tab title is overwritten by the shell/Claude via OSC; ccbot-owned panes are
    tracked in-process and re-resolved by cwd through the normal bind flow.
  - ``reconnect_events=False`` — CLI is poll-only; ``add_reconnect_listener``
    is accepted but never fired.

Requires ``ipc-allow-send-keys = true`` in the user's Otty config for
``send_keys`` to work (Otty disables remote key injection by default).

The CLI is invoked through an injectable async runner (``CliRunner``) so the
manager is unit-testable without a live Otty app.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from asyncio import sleep as _sleep
from asyncio import subprocess as _subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from shlex import quote

from .base import Capabilities, ReconnectListener, TerminalSession
from .registry import register

logger = logging.getLogger(__name__)

# Bundled CLI location used when neither config nor PATH resolves one.
_BUNDLE_CLI = "/Applications/Otty.app/Contents/MacOS/otty-cli"

# Used to auto-launch the app (backgrounded) when it isn't running, mirroring
# the iTerm2 backend. Waits between `open` and re-pinging the control socket.
_OTTY_BUNDLE_ID = "io.appmakes.otty"
_LAUNCH_DELAYS: tuple[float, ...] = (1.5, 2.0, 3.0, 4.0)

# Longer IPC timeout (ms) for closing a tab whose foreground TUI is busy;
# the default 3s often elapses before otty-cli gets the close ack.
_CLOSE_TIMEOUT_MS = 12000

# Otty pane ids look like ``p_19ef87d6b65_1`` (used as session_map suffixes).
_PANE_ID_RE = re.compile(r"^p_[0-9a-z]+_\d+$")

# Timing semantics mirrored from the iTerm2 backend: Claude Code's TUI needs
# text and the submitting Enter to arrive as separate events, and the ``!``
# bash-mode prefix needs the TUI to switch modes first.
_ENTER_DELAY = 0.5
_BASH_PREFIX_DELAY = 1.0

# ccbot named keys -> Otty ``key:`` parts (anything unlisted is sent verbatim).
_SPECIAL_KEYS: dict[str, str] = {
    "Up": "key:Up",
    "Down": "key:Down",
    "Right": "key:Right",
    "Left": "key:Left",
    "Escape": "key:Escape",
    "Tab": "key:Tab",
    "Enter": "key:Enter",
    # A literal space, not a guessed ``key:Space`` name: unlisted values are
    # sent verbatim anyway, and a space character is unambiguous. Without an
    # entry the picker's ␣ button typed the word "Space" into the TUI.
    "Space": " ",
}

# (returncode, stdout, stderr)
CliRunner = Callable[[list[str]], Awaitable[tuple[int, str, str]]]


async def _default_runner(args: list[str]) -> tuple[int, str, str]:
    """Run ``args`` as a subprocess and return (rc, stdout, stderr)."""
    proc = await _subprocess.create_subprocess_exec(
        *args,
        stdout=_subprocess.PIPE,
        stderr=_subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return (
        proc.returncode or 0,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


def _resolve_cli() -> str:
    """Locate otty-cli: config env > PATH > app bundle."""
    from ..config import config

    if config.otty_cli:
        return config.otty_cli
    for name in ("otty-cli", "otty"):
        found = shutil.which(name)
        if found:
            return found
    return _BUNDLE_CLI


class OttyManager:
    """TerminalBackend backed by the Otty control CLI.

    ccbot-owned panes are tracked in ``_owned``; the set is seeded by
    ``create_window`` / ``bind_existing_session`` and repopulated after a
    restart through ccbot's normal rebind-by-cwd flow (Otty exposes no
    persistent ownership marker).
    """

    def __init__(
        self,
        cli_path: str | None = None,
        socket_path: str | None = None,
        runner: CliRunner | None = None,
    ) -> None:
        from ..config import config

        self._cli = cli_path or _resolve_cli()
        self._socket = socket_path if socket_path is not None else config.otty_socket
        self._runner: CliRunner = runner or _default_runner
        self._owned: set[str] = set()
        self._reachable = True
        # Accepted for contract parity; never fired (no reconnect events).
        self._reconnect_listeners: list[ReconnectListener] = []

    # ------------------------------------------------------------------
    # Capabilities + connection lifecycle
    # ------------------------------------------------------------------

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            ansi_capture=False,
            native_tagging=False,
            reconnect_events=False,
            screenshot=True,
        )

    @property
    def session_map_prefix(self) -> str:
        return "otty:"

    def is_session_id(self, candidate: str) -> bool:
        """An Otty session id is a pane id like ``p_19ef87d6b65_1``."""
        return bool(_PANE_ID_RE.match(candidate))

    async def _ping(self) -> bool:
        """Cheap reachability probe against the control socket."""
        return await self._run_json("window", "list") is not None

    async def _launch_app(self) -> None:
        """Launch the Otty app in the background (no focus steal)."""
        try:
            await self._runner(["/usr/bin/open", "-g", "-b", _OTTY_BUNDLE_ID])
        except Exception as e:
            logger.warning("Failed to launch Otty: %s", e)

    async def preflight(self) -> None:
        """Verify Otty is reachable, auto-launching it if needed.

        Mirrors the iTerm2 backend: if the app isn't up, ``open`` it
        (backgrounded) and retry the control-socket ping before giving up.
        """
        if await self._ping():
            return
        logger.info("Otty not reachable; attempting to launch it…")
        await self._launch_app()
        for delay in _LAUNCH_DELAYS:
            await _sleep(delay)
            if await self._ping():
                logger.info("Otty became reachable after launch")
                return
        raise ConnectionError(
            "Otty is not reachable. Make sure Otty is installed and otty-cli "
            f"is available at {self._cli!r}, and that ipc-allow-send-keys = true "
            "in ~/.config/otty/config.toml."
        )

    def reset_connection(self) -> None:
        """No persistent connection; nothing to reset."""
        self._reachable = True

    async def ensure_running(self) -> bool:
        """Ensure Otty is up, launching it on demand; report reachability."""
        try:
            await self.preflight()
            return True
        except ConnectionError:
            return False

    def is_reachable(self) -> bool:
        return self._reachable

    def add_reconnect_listener(self, callback: ReconnectListener) -> None:
        # Otty has no reconnect event stream (capabilities.reconnect_events
        # is False); stored only so the contract call is a safe no-op.
        self._reconnect_listeners.append(callback)

    # ------------------------------------------------------------------
    # CLI plumbing
    # ------------------------------------------------------------------

    def _base(self, *args: str) -> list[str]:
        cmd = [self._cli]
        if self._socket:
            cmd += ["--socket", self._socket]
        cmd += list(args)
        return cmd

    async def _run_json(self, *args: str, timeout_ms: int | None = None) -> dict | None:
        """Run an otty-cli command with --json; return the parsed object.

        Returns None on process error, non-JSON output, or ``ok: false``.
        Updates the reachability flag as a side effect. ``timeout_ms`` overrides
        otty-cli's default IPC timeout (3s) for slow ops like closing a tab
        that's running a busy TUI.
        """
        extra = ["--timeout", str(timeout_ms)] if timeout_ms else []
        cmd = self._base("--json", *extra, *args)
        try:
            rc, out, err = await self._runner(cmd)
        except Exception as e:
            logger.warning("otty-cli invocation failed (%s): %s", args, e)
            self._reachable = False
            return None
        if rc != 0:
            logger.warning("otty-cli %s exited %d: %s", args, rc, err.strip())
            self._reachable = False
            return None
        self._reachable = True
        try:
            parsed = json.loads(out)
        except json.JSONDecodeError:
            logger.error("otty-cli %s returned non-JSON: %s", args, out[:200])
            return None
        if not parsed.get("ok", False):
            logger.warning("otty-cli %s not ok: %s", args, out[:200])
            return None
        return parsed

    async def _run_text(self, *args: str) -> str | None:
        """Run an otty-cli command expecting raw text output (e.g. capture)."""
        cmd = self._base(*args)
        try:
            rc, out, err = await self._runner(cmd)
        except Exception as e:
            logger.warning("otty-cli invocation failed (%s): %s", args, e)
            self._reachable = False
            return None
        if rc != 0:
            logger.warning("otty-cli %s exited %d: %s", args, rc, err.strip())
            self._reachable = False
            return None
        self._reachable = True
        return out

    async def _panes(self) -> list[dict]:
        res = await self._run_json("pane", "list")
        if res is None:
            return []
        data = res.get("data")
        return data if isinstance(data, list) else []

    async def _tab_id_for_pane(self, window_id: str) -> str | None:
        for p in await self._panes():
            if p.get("id") == window_id:
                return p.get("tab_id")
        return None

    def _to_session(
        self, pane: dict, claude_uuids: set[str] | None = None
    ) -> TerminalSession:
        pid = pane.get("id", "")
        return TerminalSession(
            window_id=pid,
            window_name=pane.get("title", "") or "",
            cwd=pane.get("cwd", "") or "",
            pane_current_command=pane.get("process", "") or "",
            # Otty reports one process name; it serves as both signals.
            job_title=pane.get("process", "") or "",
            is_ccbot=pid in self._owned,
            has_claude=bool(claude_uuids and pid in claude_uuids),
        )

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def list_windows(self) -> list[TerminalSession]:
        return [
            self._to_session(p)
            for p in await self._panes()
            if p.get("id") in self._owned
        ]

    async def find_window_by_name(self, window_name: str) -> TerminalSession | None:
        for w in await self.list_windows():
            if w.window_name == window_name:
                return w
        logger.debug("Otty: window not found by name: %s", window_name)
        return None

    async def find_window_by_id(self, window_id: str) -> TerminalSession | None:
        if window_id not in self._owned:
            return None
        for p in await self._panes():
            if p.get("id") == window_id:
                return self._to_session(p)
        return None

    async def list_all_sessions(
        self, claude_session_uuids: set[str] | None = None
    ) -> list[TerminalSession]:
        return [self._to_session(p, claude_session_uuids) for p in await self._panes()]

    async def bind_existing_session(self, window_id: str, name: str) -> bool:
        if not any(p.get("id") == window_id for p in await self._panes()):
            logger.debug("Otty: bind target pane gone: %s", window_id)
            return False
        self._owned.add(window_id)
        await self.rename_window(window_id, name)
        return True

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    async def capture_pane(self, window_id: str, with_ansi: bool = False) -> str | None:
        # Otty's --ansi emits no usable color, so capture is always plain text;
        # with_ansi is accepted for contract parity (see capabilities).
        return await self._run_text("pane", "capture", "--pane", window_id, "--trim")

    async def _send_parts(self, window_id: str, *parts: str) -> bool:
        """Send key/text parts to a pane. ``--`` guards leading-dash/``!`` text."""
        res = await self._run_json(
            "pane", "send-keys", "--pane", window_id, "--", *parts
        )
        return res is not None

    async def send_keys(
        self,
        window_id: str,
        text: str,
        enter: bool = True,
        literal: bool = True,
    ) -> bool:
        try:
            if literal and enter:
                # Two-phase: text, gap, then Enter as a separate event so the
                # TUI doesn't read a batched newline as a literal newline.
                if text.startswith("!"):
                    await self._send_parts(window_id, "!")
                    rest = text[1:]
                    if rest:
                        await _sleep(_BASH_PREFIX_DELAY)
                        await self._send_parts(window_id, rest)
                else:
                    await self._send_parts(window_id, text)
                await _sleep(_ENTER_DELAY)
                return await self._send_parts(window_id, "key:Enter")

            if literal:
                parts = [text] + (["key:Enter"] if enter else [])
            else:
                mapped = _SPECIAL_KEYS.get(text, text)
                parts = [mapped] + (["key:Enter"] if enter else [])
            return await self._send_parts(window_id, *parts)
        except Exception as e:
            logger.error("Otty send_keys to %s failed: %s", window_id, e)
            return False

    async def screenshot_session(self, window_id: str) -> bytes | None:
        # Native pixel screenshot is not wired for Otty yet; upper layers
        # render captured text to a PNG instead (monochrome — see
        # capabilities.ansi_capture). Returning None signals "no native shot".
        return None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def rename_window(self, window_id: str, new_name: str) -> bool:
        tab_id = await self._tab_id_for_pane(window_id)
        if tab_id is None:
            logger.error("Otty rename: pane not found: %s", window_id)
            return False
        res = await self._run_json("tab", "rename", "--tab", tab_id, "--", new_name)
        if res is not None:
            logger.info("Otty renamed pane %s to '%s'", window_id, new_name)
        return res is not None

    async def kill_window(self, window_id: str) -> bool:
        tab_id = await self._tab_id_for_pane(window_id)
        if tab_id is None:
            logger.debug("Otty kill: pane %s already gone", window_id)
            self._owned.discard(window_id)
            return False
        # Closing a tab running a busy TUI (claude) can exceed the default 3s
        # IPC window; the close still happens but the response is slow.
        res = await self._run_json(
            "tab", "close", "--tab", tab_id, "--force", timeout_ms=_CLOSE_TIMEOUT_MS
        )
        ok = res is not None
        if ok:
            self._owned.discard(window_id)
            logger.info("Otty killed pane %s (tab %s)", window_id, tab_id)
        return ok

    async def create_window(
        self,
        work_dir: str,
        window_name: str | None = None,
        start_claude: bool = True,
        resume_session_id: str | None = None,
    ) -> tuple[bool, str, str, str]:
        from ..config import config

        path = Path(work_dir).expanduser().resolve()
        if not path.exists():
            return False, f"Directory does not exist: {work_dir}", "", ""
        if not path.is_dir():
            return False, f"Not a directory: {work_dir}", "", ""

        # Otty may have been closed since startup; ensure it's up (preflight
        # pings and auto-launches) before creating a tab, so a closed app is
        # recovered rather than surfacing as "Failed to create Otty tab".
        try:
            await self.preflight()
        except ConnectionError as e:
            return False, f"Otty is not running and could not be launched: {e}", "", ""

        # De-dup the display name against currently-owned sessions.
        final_name = window_name or path.name
        base = final_name
        counter = 2
        while await self.find_window_by_name(final_name) is not None:
            final_name = f"{base}-{counter}"
            counter += 1

        # Snapshot pane ids so we can identify the one Otty creates (``tab new``
        # returns no id). We open a bare shell first — not ``--command claude``
        # — because the SessionStart hook needs ccbot's correlation key in the
        # Claude process env, and we only learn the pane id *after* creation.
        before = {p.get("id") for p in await self._panes()}

        args = ["tab", "new", "--cwd", str(path), "--title", final_name, "--no-focus"]
        if await self._run_json(*args) is None:
            return False, "Failed to create Otty tab", "", ""

        new_panes = [p for p in await self._panes() if p.get("id") not in before]
        match = next(
            (p for p in new_panes if p.get("cwd") == str(path)),
            new_panes[0] if new_panes else None,
        )
        if match is None:
            return False, "Created tab but could not resolve its id", final_name, ""

        pid = match.get("id", "")
        self._owned.add(pid)
        await self.rename_window(pid, final_name)

        if start_claude:
            # Inject the session_map key into Claude's env so the SessionStart
            # hook can write ``<prefix><pane_id>`` — the key the bot waits on
            # (Otty exposes no per-pane env id like iTerm2's ITERM_SESSION_ID).
            session_key = f"{self.session_map_prefix}{pid}"
            cmd = config.claude_command
            if resume_session_id:
                cmd = f"{cmd} --resume {quote(resume_session_id)}"
            launch = f"CCBOT_SESSION_KEY={quote(session_key)} {cmd}"
            await self.send_keys(pid, launch, enter=True, literal=True)

        return True, "", final_name, pid


# Register under the "otty" backend name. The factory is cheap (no app
# contact); the manager is instantiated lazily on first selection.
register("otty")(lambda: OttyManager())

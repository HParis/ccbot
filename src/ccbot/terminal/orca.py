"""Orca terminal backend.

Drives a running Orca app through its bundled ``orca`` CLI, implementing the
``TerminalBackend`` contract so ccbot can host Claude Code sessions in Orca
terminals instead of iTerm2.

The session identifier (``TerminalSession.window_id``) is the Orca terminal
**handle** (``term_<uuid>``).

Differences from iTerm2, declared via ``capabilities`` so upper layers degrade
instead of failing:
  - ``arbitrary_cwd=False`` — this is the big one. Orca hosts terminals inside
    registered worktrees, never in a bare directory: ``terminal create``
    rejects an unregistered path, and even a *subdirectory* of a registered
    project, with ``selector_not_found``. Upper layers therefore offer
    ``list_workspaces`` (the worktrees Orca knows) instead of a filesystem
    browser.
  - ``ansi_capture=False`` — ``terminal read --screen`` returns rendered text
    without SGR color, so screenshots are monochrome.
  - ``native_tagging=False`` — Orca has no per-terminal user variable, so
    ccbot-owned terminals are tracked in-process and re-resolved by cwd
    through the normal rebind flow.
  - ``reconnect_events=False`` — the CLI is poll-only.

Two Orca specifics worth knowing when reading this module:
  - ``terminal read`` defaults to *accumulated output*, which stacks repainted
    frames ("clear" typed one key at a time reads as "cclclecleaclear"). Every
    capture here passes ``--screen`` to get the rendered frame instead.
  - ``terminal send --text`` passes bytes through verbatim, so named keys are
    mapped to their escape sequences rather than to CLI key names.

The CLI is invoked through an injectable async runner (``CliRunner``) so the
manager is unit-testable without a live Orca app.
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

from .base import Capabilities, ReconnectListener, TerminalSession, Workspace
from .registry import register

logger = logging.getLogger(__name__)

# Bundled CLI location used when PATH doesn't resolve one.
_BUNDLE_CLI = "/Applications/Orca.app/Contents/Resources/bin/orca"

# Used to auto-launch the app when it isn't running. ``orca open`` waits for
# the runtime to become reachable, so no polling loop is needed here.
_LAUNCH_TIMEOUT = 30.0

# Orca terminal handles look like ``term_<uuid>``.
_HANDLE_RE = re.compile(
    r"^term_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

# Timing semantics mirrored from the iTerm2 backend: Claude Code's TUI needs
# text and the submitting Enter to arrive as separate events, and the ``!``
# bash-mode prefix needs the TUI to switch modes first.
_ENTER_DELAY = 0.5
_BASH_PREFIX_DELAY = 1.0

# ccbot named keys -> raw bytes. ``terminal send --text`` is byte-transparent
# (verified against ``cat -v``: ESC arrives as ^[, Up as ^[[A), so the keys a
# picker needs are expressed as escape sequences rather than CLI key names.
_SPECIAL_KEYS: dict[str, str] = {
    "Up": "\x1b[A",
    "Down": "\x1b[B",
    "Right": "\x1b[C",
    "Left": "\x1b[D",
    "Escape": "\x1b",
    "Tab": "\t",
    "Enter": "\r",
    "Space": " ",
}

# Stands in for a terminal Orca reports no agent for — a plain shell.
_NO_AGENT = "no-agent"

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
    """Locate the orca CLI: PATH > app bundle."""
    return shutil.which("orca") or _BUNDLE_CLI


class OrcaManager:
    """TerminalBackend backed by the Orca CLI.

    ccbot-owned terminals are tracked in ``_owned``; the set is seeded by
    ``create_window`` / ``bind_existing_session`` and repopulated after a
    restart through ccbot's normal rebind-by-cwd flow (Orca exposes no
    persistent ownership marker).
    """

    def __init__(
        self,
        cli_path: str | None = None,
        runner: CliRunner | None = None,
    ) -> None:
        self._cli = cli_path or _resolve_cli()
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
            # Sessions live in registered worktrees only — see module docstring.
            arbitrary_cwd=False,
        )

    @property
    def session_map_prefix(self) -> str:
        return "orca:"

    def is_session_id(self, candidate: str) -> bool:
        """An Orca session id is a terminal handle like ``term_<uuid>``."""
        return bool(_HANDLE_RE.match(candidate))

    async def _ping(self) -> bool:
        """Cheap reachability probe against the Orca runtime.

        ``runtime.reachable`` is the authoritative flag: the app process can
        be running while its runtime is still starting, and a terminal call
        against a half-started runtime fails.
        """
        res = await self._run_json("status")
        if not res:
            return False
        runtime = res.get("runtime")
        if isinstance(runtime, dict):
            return bool(runtime.get("reachable"))
        # Older hosts flattened the fields (see _parse_status_text).
        return bool(res.get("runtimeReachable"))

    async def preflight(self) -> None:
        """Verify Orca is reachable, auto-launching it if needed."""
        if await self._ping():
            return
        logger.info("Orca not reachable; attempting to launch it…")
        # `orca open` returns once the runtime is reachable, so there is
        # nothing to poll afterwards.
        await self._run_json("open")
        if await self._ping():
            logger.info("Orca became reachable after launch")
            return
        raise ConnectionError(
            "Orca is not reachable. Make sure Orca is installed and its CLI "
            f"is available at {self._cli!r}."
        )

    def reset_connection(self) -> None:
        """No persistent connection; nothing to reset."""
        self._reachable = True

    async def ensure_running(self) -> bool:
        """Ensure Orca is up, launching it on demand; report reachability."""
        try:
            await self.preflight()
            return True
        except ConnectionError:
            return False

    def is_reachable(self) -> bool:
        return self._reachable

    def add_reconnect_listener(self, callback: ReconnectListener) -> None:
        # Orca has no reconnect event stream (capabilities.reconnect_events
        # is False); stored only so the contract call is a safe no-op.
        self._reconnect_listeners.append(callback)

    # ------------------------------------------------------------------
    # CLI plumbing
    # ------------------------------------------------------------------

    async def _run_json(self, *args: str) -> dict | None:
        """Run an orca command with --json; return its ``result`` payload.

        Returns None on process error, non-JSON output, or ``ok: false``.
        Updates the reachability flag as a side effect.
        """
        cmd = [self._cli, *args, "--json"]
        try:
            rc, out, err = await self._runner(cmd)
        except Exception as e:
            logger.warning("orca invocation failed (%s): %s", args, e)
            self._reachable = False
            return None
        try:
            parsed = json.loads(out)
        except json.JSONDecodeError:
            # `orca status` predates the envelope and prints plain key: value.
            if rc == 0 and args[:1] == ("status",):
                self._reachable = True
                return _parse_status_text(out)
            logger.error("orca %s returned non-JSON: %s", args, out[:200])
            self._reachable = rc == 0
            return None
        if rc != 0 and not parsed.get("ok", False):
            err_obj = parsed.get("error", {})
            logger.warning(
                "orca %s failed: %s %s",
                args,
                err_obj.get("code", rc),
                err_obj.get("message", err.strip()),
            )
            self._reachable = False
            return None
        if not parsed.get("ok", False):
            err_obj = parsed.get("error", {})
            logger.warning("orca %s not ok: %s", args, err_obj.get("code") or out[:200])
            # A rejected request still proves the runtime answered.
            self._reachable = True
            return None
        self._reachable = True
        result = parsed.get("result")
        return result if isinstance(result, dict) else {}

    async def _terminals(self) -> list[dict]:
        res = await self._run_json("terminal", "list")
        if res is None:
            return []
        terms = res.get("terminals")
        return terms if isinstance(terms, list) else []

    def _to_session(
        self, term: dict, claude_uuids: set[str] | None = None
    ) -> TerminalSession:
        handle = term.get("handle", "")
        # Orca names the agent running in the terminal; that is a cleaner
        # "is Claude here" signal than any process name, so it feeds both
        # job fields (see terminal.base.is_running_claude).
        # Orca reports which agent owns the terminal and omits the key when
        # none does. Encode that absence explicitly: leaving both job fields
        # empty would take is_running_claude's "backend can't tell" path and
        # report a plain shell as a live Claude.
        agent = term.get("agentIdentity") or _NO_AGENT
        return TerminalSession(
            window_id=handle,
            window_name=term.get("title", "") or "",
            # worktreePath is the session's directory; Orca has no separate
            # per-terminal cwd because a terminal belongs to a worktree.
            cwd=term.get("worktreePath", "") or "",
            pane_current_command=agent,
            job_title=agent,
            is_ccbot=handle in self._owned,
            has_claude=bool(claude_uuids and handle in claude_uuids),
        )

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def list_windows(self) -> list[TerminalSession]:
        return [
            self._to_session(t)
            for t in await self._terminals()
            if t.get("handle") in self._owned
        ]

    async def find_window_by_name(self, window_name: str) -> TerminalSession | None:
        for w in await self.list_windows():
            if w.window_name == window_name:
                return w
        logger.debug("Orca: terminal not found by name: %s", window_name)
        return None

    async def find_window_by_id(self, window_id: str) -> TerminalSession | None:
        if window_id not in self._owned:
            return None
        for t in await self._terminals():
            if t.get("handle") == window_id:
                return self._to_session(t)
        return None

    async def list_all_sessions(
        self, claude_session_uuids: set[str] | None = None
    ) -> list[TerminalSession]:
        return [
            self._to_session(t, claude_session_uuids) for t in await self._terminals()
        ]

    async def list_workspaces(self) -> list[Workspace]:
        """The worktrees Orca knows — the only places it can open a terminal."""
        res = await self._run_json("worktree", "list")
        if res is None:
            return []
        entries = res.get("worktrees")
        if not isinstance(entries, list):
            return []
        out: list[Workspace] = []
        for w in entries:
            path = w.get("path") or w.get("worktreePath") or ""
            if not path:
                continue
            out.append(
                Workspace(
                    path=path,
                    label=w.get("name") or Path(path).name,
                    detail=_short_branch(w.get("branch") or ""),
                )
            )
        return out

    async def bind_existing_session(self, window_id: str, name: str) -> bool:
        if not any(t.get("handle") == window_id for t in await self._terminals()):
            logger.debug("Orca: bind target terminal gone: %s", window_id)
            return False
        self._owned.add(window_id)
        await self.rename_window(window_id, name)
        return True

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    async def capture_pane(self, window_id: str, with_ansi: bool = False) -> str | None:
        """Return the rendered screen.

        ``--screen`` is mandatory, not an optimisation: the default read
        returns accumulated output in which every repaint is stacked, so a
        TUI frame comes back as unusable fragments. ``with_ansi`` is accepted
        for contract parity — the screen carries no color (see capabilities).
        """
        res = await self._run_json(
            "terminal", "read", "--terminal", window_id, "--screen"
        )
        if res is None:
            return None
        term = res.get("terminal", res)
        tail = term.get("tail")
        if isinstance(tail, list):
            return "\n".join(str(line) for line in tail)
        text = term.get("screen") or term.get("text")
        return str(text) if text is not None else None

    async def _send_text(self, window_id: str, text: str, enter: bool = False) -> bool:
        args = ["terminal", "send", "--terminal", window_id, "--text", text]
        if enter:
            args.append("--enter")
        return await self._run_json(*args) is not None

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
                    await self._send_text(window_id, "!")
                    rest = text[1:]
                    if rest:
                        await _sleep(_BASH_PREFIX_DELAY)
                        await self._send_text(window_id, rest)
                else:
                    await self._send_text(window_id, text)
                await _sleep(_ENTER_DELAY)
                # An empty --text with --enter submits what is already there.
                return await self._send_text(window_id, "", enter=True)

            if literal:
                return await self._send_text(window_id, text, enter=enter)
            mapped = _SPECIAL_KEYS.get(text, text)
            return await self._send_text(window_id, mapped, enter=enter)
        except Exception as e:
            logger.error("Orca send_keys to %s failed: %s", window_id, e)
            return False

    async def screenshot_session(self, window_id: str) -> bytes | None:
        # Orca exposes no pixel capture; upper layers render captured text to
        # a PNG instead (monochrome — see capabilities.ansi_capture).
        return None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def rename_window(self, window_id: str, new_name: str) -> bool:
        res = await self._run_json(
            "terminal", "rename", "--terminal", window_id, "--title", new_name
        )
        if res is not None:
            logger.info("Orca renamed terminal %s to '%s'", window_id, new_name)
        return res is not None

    async def kill_window(self, window_id: str) -> bool:
        res = await self._run_json("terminal", "close", "--terminal", window_id)
        ok = res is not None
        if ok:
            self._owned.discard(window_id)
            logger.info("Orca closed terminal %s", window_id)
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

        try:
            await self.preflight()
        except ConnectionError as e:
            return False, f"Orca is not running and could not be launched: {e}", "", ""

        # De-dup the display name against currently-owned sessions.
        final_name = window_name or path.name
        base = final_name
        counter = 2
        while await self.find_window_by_name(final_name) is not None:
            final_name = f"{base}-{counter}"
            counter += 1

        # No --command: the SessionStart hook needs ccbot's correlation key in
        # Claude's env, and the handle only exists after creation. Also no
        # --focus, so the user's foreground tab is left alone.
        res = await self._run_json(
            "terminal",
            "create",
            "--worktree",
            f"path:{path}",
            "--title",
            final_name,
        )
        if res is None:
            return (
                False,
                f"Orca could not open a terminal in {path}. Orca hosts sessions "
                "in registered projects only — add it in Orca first.",
                "",
                "",
            )

        handle = (res.get("terminal") or {}).get("handle", "")
        if not handle:
            return (
                False,
                "Created terminal but could not resolve its handle",
                final_name,
                "",
            )

        self._owned.add(handle)

        if start_claude:
            # Inject the session_map key into Claude's env so the SessionStart
            # hook can write ``<prefix><handle>`` — the key the bot waits on
            # (Orca exposes no per-terminal env id like ITERM_SESSION_ID).
            session_key = f"{self.session_map_prefix}{handle}"
            cmd = config.claude_command
            if resume_session_id:
                cmd = f"{cmd} --resume {quote(resume_session_id)}"
            launch = f"CCBOT_SESSION_KEY={quote(session_key)} {cmd}"
            await self.send_keys(handle, launch, enter=True, literal=True)

        return True, "", final_name, handle


def _short_branch(ref: str) -> str:
    """``refs/heads/feat/x`` -> ``feat/x``; anything else is passed through."""
    return ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref


def _parse_status_text(out: str) -> dict:
    """Parse ``orca status`` plain output into a dict of string values."""
    data: dict[str, str | bool] = {}
    for line in out.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        v = value.strip()
        data[key.strip()] = True if v == "true" else False if v == "false" else v
    return data


# Register under the "orca" backend name. The factory is cheap (no app
# contact); the manager is instantiated lazily on first selection.
register("orca")(lambda: OrcaManager())

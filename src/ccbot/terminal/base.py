"""Terminal-backend contract and neutral domain types.

This module is pure (no ccbot or terminal-vendor imports) so backends and
upper layers can both depend on it without import cycles.

Key components:
  - ``TerminalSession``: vendor-neutral record for one ccbot-hosted session.
  - ``Capabilities``: feature flags a backend declares so upper layers can
    degrade gracefully when a terminal lacks a capability (e.g. no ANSI
    capture, no native ownership tagging, no reconnect events).
  - ``TerminalBackend``: the structural contract every backend implements.
    Mirrors the original ITerm2Manager public surface 1:1 so swapping the
    backend requires no call-site changes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# Coroutine fired after a backend re-establishes its connection (not the
# first-ever connect). Used to re-resolve session ids that change across
# terminal restarts.
ReconnectListener = Callable[[], Awaitable[None]]


@dataclass
class TerminalSession:
    """One ccbot-hosted terminal session, vendor-neutral.

    ``window_id`` is the backend's opaque session identifier (for iTerm2,
    the session UUID). Field names are kept stable for backward
    compatibility with the original ``ITermWindow``.

    ``is_ccbot`` and ``has_claude`` are populated by ``list_all_sessions``
    for the bind-existing-tab picker; the lifecycle methods leave them at
    their defaults because they only return ccbot-owned sessions.
    """

    window_id: str  # backend session identifier
    window_name: str  # display name
    cwd: str  # current working directory (or "")
    pane_current_command: str = ""  # foreground job name (or "")
    # Foreground process title, when the backend can report one separately
    # from the job name. The two diverge: iTerm2's jobName for a running
    # Claude Code reads as its version string ("2.1.269"), while the process
    # title reads "claude". Empty means the backend cannot tell us.
    job_title: str = ""
    is_ccbot: bool = False  # owned/tagged by ccbot
    has_claude: bool = False  # has a session_map.json entry


# Foreground-process names that mean "Claude Code is running here".
# iTerm2's jobName reads as Claude's version string ("2.1.269") while its
# processTitle reads "claude"; older builds showed "node-runtime" as the job.
# Matched case-insensitively as substrings against both signals.
_CLAUDE_JOB_NAMES = ("claude", "node-runtime")


def is_running_claude(session: TerminalSession) -> bool:
    """Whether a live session's foreground process looks like Claude Code.

    Needed wherever session_map is not proof: a session_map entry (and so
    ``has_claude``) outlives the Claude process that created it, staying True
    for a tab whose Claude has exited and which is now a plain shell.

    A backend that reports neither signal gets the benefit of the doubt —
    treating "can't tell" as "not Claude" would break every caller on a
    backend that can't introspect its jobs.
    """
    signals = [
        x.lower() for x in (session.job_title, session.pane_current_command) if x
    ]
    if not signals:
        return True
    return any(any(n in sig for n in _CLAUDE_JOB_NAMES) for sig in signals)


@dataclass(frozen=True)
class Capabilities:
    """Feature flags a backend declares so upper layers can degrade.

    A backend that returns False for a flag promises only that the
    corresponding method is a safe no-op / best-effort, never that it
    raises. Upper layers branch on these instead of catching errors.
    """

    ansi_capture: bool  # capture_pane(with_ansi=True) emits SGR color
    native_tagging: bool  # ownership via a native per-session variable
    reconnect_events: bool  # add_reconnect_listener fires on reconnect
    screenshot: bool  # screenshot_session returns real pixels (not None)


@runtime_checkable
class TerminalBackend(Protocol):
    """Contract for a GUI terminal that hosts ccbot Claude Code sessions.

    Structural (Protocol) so backends need not inherit from it; pyright
    verifies each backend matches this surface where it is assigned to a
    ``TerminalBackend`` slot.
    """

    @property
    def capabilities(self) -> Capabilities: ...

    # --- session-map identity -------------------------------------------
    @property
    def session_map_prefix(self) -> str:
        """Prefix for this backend's keys in session_map.json (e.g. "iterm:").

        The SessionStart hook writes ``<prefix><session_id>`` and the bot
        polls for the same key, so the prefix must agree on both sides.
        """
        ...

    def is_session_id(self, candidate: str) -> bool:
        """Whether ``candidate`` looks like a valid session id for this backend.

        Used to tell live/recognized session_map keys apart from stale or
        old-format ones during cleanup (e.g. iTerm2 = UUID, Otty = ``p_*``).
        """
        ...

    # --- connection lifecycle -------------------------------------------
    async def preflight(self) -> None:
        """Verify the terminal is reachable, raising ConnectionError if not.

        Used at startup to fail fast. Implementations must leave no
        long-lived connection bound to the calling event loop (pair with
        ``reset_connection``).
        """
        ...

    def reset_connection(self) -> None:
        """Drop any cached connection so the next call reconnects fresh."""
        ...

    async def ensure_running(self) -> bool:
        """Best-effort: ensure the terminal app is up, launching it if needed.

        Returns whether the terminal is reachable afterwards. Unlike background
        queries (which fail passively so they don't fight a deliberate close),
        this is called on user-driven actions like sending a message, so the
        app gets relaunched on demand.
        """
        ...

    def is_reachable(self) -> bool:
        """Cheap, side-effect-free check for whether the terminal is up."""
        ...

    def add_reconnect_listener(self, callback: ReconnectListener) -> None:
        """Register a coroutine to run after each reconnect (no-op if the
        backend lacks reconnect events; see ``capabilities``)."""
        ...

    # --- discovery ------------------------------------------------------
    async def list_windows(self) -> list[TerminalSession]:
        """List ccbot-owned sessions."""
        ...

    async def find_window_by_name(self, window_name: str) -> TerminalSession | None:
        """Find a ccbot-owned session by display name."""
        ...

    async def find_window_by_id(self, window_id: str) -> TerminalSession | None:
        """Find a ccbot-owned session by backend session id."""
        ...

    async def list_all_sessions(
        self, claude_session_uuids: set[str] | None = None
    ) -> list[TerminalSession]:
        """List all sessions (owned or not) for the bind-existing picker."""
        ...

    async def bind_existing_session(self, window_id: str, name: str) -> bool:
        """Adopt an existing session into ccbot's pool (tag + rename)."""
        ...

    # --- I/O ------------------------------------------------------------
    async def capture_pane(self, window_id: str, with_ansi: bool = False) -> str | None:
        """Capture the visible screen text of a session (optionally ANSI)."""
        ...

    async def send_keys(
        self,
        window_id: str,
        text: str,
        enter: bool = True,
        literal: bool = True,
    ) -> bool:
        """Send text / named keys to a session, preserving TUI timing."""
        ...

    async def screenshot_session(self, window_id: str) -> bytes | None:
        """Return a PNG screenshot of the session, or None if unsupported."""
        ...

    # --- lifecycle ------------------------------------------------------
    async def create_window(
        self,
        work_dir: str,
        window_name: str | None = None,
        start_claude: bool = True,
        resume_session_id: str | None = None,
    ) -> tuple[bool, str, str, str]:
        """Create a ccbot-owned session and optionally start Claude Code.

        Returns ``(ok, message, created_name, created_id)``.
        """
        ...

    async def rename_window(self, window_id: str, new_name: str) -> bool:
        """Rename a ccbot-owned session."""
        ...

    async def kill_window(self, window_id: str) -> bool:
        """Close a ccbot-owned session (and its tab)."""
        ...

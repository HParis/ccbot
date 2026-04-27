"""iTerm2 session/tab management via the iTerm2 Python API.

Drives a running iTerm2 instance to host Claude Code sessions, replacing
the previous tmux-based backend. iTerm2 is required because tmux's PTY
layer prevents Claude Code's Computer Use feature from interacting with
the host GUI.

Public surface mirrors the previous TmuxManager 1:1 so upper-layer call
sites (bot.py / session.py / handlers/) need only swap their import:
  - list_windows / find_window_by_id / find_window_by_name
  - capture_pane (plain or ANSI-colored)
  - send_keys (text + special keys, with !-mode and Enter timing)
  - create_window / kill_window / rename_window

ccbot-owned tabs are tagged with the iTerm2 user variable
``user.ccbot=1`` so the user's own iTerm2 tabs stay invisible to the bot.

Key class: ITerm2Manager (singleton instantiated as ``iterm2_manager``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ITermWindow:
    """Information about a ccbot-owned iTerm2 tab/session.

    Field names match the previous TmuxWindow dataclass so callers don't
    need to change. ``window_id`` carries the iTerm2 session UUID.
    """

    window_id: str  # iTerm2 session UUID
    window_name: str  # iTerm2 session name (set via async_set_name)
    cwd: str  # session's current working directory (or "")
    pane_current_command: str = ""  # foreground job name (or "")


class ITerm2Manager:
    """Manages iTerm2 tabs hosting Claude Code sessions.

    Holds a single long-lived iTerm2 Python API connection (lazy, with
    reconnect backoff). All ccbot-owned tabs live inside one dedicated
    iTerm2 window; the manager creates that window on demand.
    """

    def __init__(self, profile_name: str | None = None) -> None:
        """Initialize the manager.

        Args:
            profile_name: iTerm2 profile to use for new tabs. Defaults
                to the value of ``CCBOT_ITERM2_PROFILE`` (read from
                config). If the named profile is missing at runtime,
                falls back to the default profile and logs a warning.
        """
        from .config import config

        self.profile_name = profile_name or config.iterm2_profile_name

    async def list_windows(self) -> list[ITermWindow]:
        """List ccbot-owned tabs (sessions tagged with ``user.ccbot=1``).

        Returns:
            One ``ITermWindow`` per tagged session. Empty if iTerm2 has
            no ccbot tabs open.
        """
        raise NotImplementedError("Unit 2")

    async def find_window_by_name(self, window_name: str) -> ITermWindow | None:
        """Find a ccbot-owned session by its display name.

        Args:
            window_name: The session name to match (set via
                ``Session.async_set_name``).

        Returns:
            The matching ``ITermWindow``, or ``None`` if not found.
        """
        raise NotImplementedError("Unit 2")

    async def find_window_by_id(self, window_id: str) -> ITermWindow | None:
        """Find a ccbot-owned session by its iTerm2 session UUID.

        Args:
            window_id: iTerm2 session UUID (e.g.
                ``9F2E3A1B-DEAD-BEEF-CAFE-0123456789AB``).

        Returns:
            The matching ``ITermWindow``, or ``None`` if not found.
        """
        raise NotImplementedError("Unit 2")

    async def capture_pane(self, window_id: str, with_ansi: bool = False) -> str | None:
        """Capture the visible text content of a session's screen.

        Args:
            window_id: iTerm2 session UUID.
            with_ansi: If True, emit SGR escape codes (16/256/RGB color
                + bold) in the dialect ``screenshot.py`` understands. If
                False, return plain text only.

        Returns:
            Captured text (one ``\\n``-joined string), or ``None`` on
            failure.
        """
        raise NotImplementedError("Unit 3")

    async def send_keys(
        self,
        window_id: str,
        text: str,
        enter: bool = True,
        literal: bool = True,
    ) -> bool:
        """Send keys to a ccbot-owned session.

        Preserves the timing semantics required by Claude Code's TUI:
        literal text + 500ms gap + Enter; if ``text`` starts with
        ``!`` (bash-mode prefix) the manager sends ``!`` first, waits
        1s, then sends the rest before the Enter gap.

        Args:
            window_id: iTerm2 session UUID.
            text: Text to send. When ``literal=False``, named keys
                (Up/Down/Left/Right/Escape/Tab/Enter) are translated to
                their escape sequences.
            enter: If True, append Enter after the text (with the
                500ms gap when ``literal=True``).
            literal: If True, send text verbatim. If False, expand
                named keys to escape sequences.

        Returns:
            True on success, False otherwise.
        """
        raise NotImplementedError("Unit 3")

    async def rename_window(self, window_id: str, new_name: str) -> bool:
        """Rename a ccbot-owned session.

        Args:
            window_id: iTerm2 session UUID.
            new_name: New session name.

        Returns:
            True on success, False otherwise.
        """
        raise NotImplementedError("Unit 4")

    async def kill_window(self, window_id: str) -> bool:
        """Close a ccbot-owned session.

        Args:
            window_id: iTerm2 session UUID.

        Returns:
            True on success, False otherwise.
        """
        raise NotImplementedError("Unit 4")

    async def create_window(
        self,
        work_dir: str,
        window_name: str | None = None,
        start_claude: bool = True,
        resume_session_id: str | None = None,
    ) -> tuple[bool, str, str, str]:
        """Create a new ccbot-owned tab and optionally start Claude Code.

        Opens the tab inside the dedicated ccbot iTerm2 window (created
        lazily on first call), tags it with ``user.ccbot=1``, names it,
        ``cd``s into ``work_dir``, and (if ``start_claude``) launches
        ``claude`` with optional ``--resume <id>``.

        Args:
            work_dir: Absolute path to the working directory.
            window_name: Optional display name (defaults to the
                directory's basename). Conflicts get ``-2``/``-3``
                suffixes.
            start_claude: Whether to launch ``claude`` after ``cd``.
            resume_session_id: If set, append ``--resume <id>`` to the
                claude command.

        Returns:
            Tuple of (success, message, final_window_name, session_uuid).
            On failure, name and uuid are empty strings.
        """
        raise NotImplementedError("Unit 4")


iterm2_manager = ITerm2Manager()

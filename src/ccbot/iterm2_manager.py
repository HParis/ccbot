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

import asyncio
import logging
from dataclasses import dataclass

import iterm2

logger = logging.getLogger(__name__)


# Reconnect backoff (seconds). Three attempts before giving up.
_RECONNECT_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)

# Marker variable used to identify ccbot-owned sessions. Stored as the
# iTerm2 user-variable ``user.ccbot``; value is the literal "1".
_CCBOT_TAG_NAME = "user.ccbot"
_CCBOT_TAG_VALUE = "1"


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
        self._connection: iterm2.Connection | None = None
        self._app: iterm2.App | None = None
        # Lock ensures two concurrent callers don't open two connections.
        self._connect_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def _get_connection(self) -> iterm2.Connection:
        """Return a live iTerm2 connection, reconnecting if needed.

        Raises:
            ConnectionError: After exhausting the reconnect backoff.
                Callers should treat this as fatal at startup and as a
                transient at runtime (next call retries).
        """
        async with self._connect_lock:
            if self._connection is not None:
                return self._connection

            last_err: BaseException | None = None
            for attempt, delay in enumerate(_RECONNECT_DELAYS):
                if attempt > 0:
                    logger.debug(
                        "Retrying iTerm2 connection in %.1fs (attempt %d)",
                        delay,
                        attempt + 1,
                    )
                    await asyncio.sleep(delay)
                try:
                    conn = await iterm2.Connection.async_create()
                    self._connection = conn
                    self._app = None  # force re-fetch on next _get_app
                    logger.info("Connected to iTerm2 Python API")
                    return conn
                except Exception as e:
                    last_err = e
                    logger.debug(
                        "iTerm2 connection attempt %d failed: %s", attempt + 1, e
                    )

            raise ConnectionError(
                "Cannot connect to iTerm2. Ensure iTerm2 is running and "
                "the Python API is enabled (Preferences → General → "
                "Magic → Enable Python API)."
            ) from last_err

    async def _get_app(self) -> iterm2.App:
        """Return a refreshed App handle, reconnecting on disconnect."""
        try:
            conn = await self._get_connection()
            if self._app is None:
                app = await iterm2.async_get_app(conn)
                if app is None:
                    raise ConnectionError("iTerm2 returned no App instance")
                self._app = app
            await self._app.async_refresh()
            return self._app
        except (ConnectionError, OSError):
            # Drop cached state so the next call retries from scratch.
            self._connection = None
            self._app = None
            raise

    def _invalidate_connection(self) -> None:
        """Drop cached connection so the next call reconnects."""
        self._connection = None
        self._app = None

    # ------------------------------------------------------------------
    # Read-only discovery
    # ------------------------------------------------------------------

    async def list_windows(self) -> list[ITermWindow]:
        """List ccbot-owned tabs (sessions tagged with ``user.ccbot=1``).

        Returns:
            One ``ITermWindow`` per tagged session. Empty if iTerm2 has
            no ccbot tabs open or the connection is down.
        """
        try:
            app = await self._get_app()
        except ConnectionError as e:
            logger.warning("list_windows: iTerm2 unreachable: %s", e)
            return []

        results: list[ITermWindow] = []
        for window in app.windows:
            for tab in window.tabs:
                for session in tab.sessions:
                    info = await self._session_to_window(session)
                    if info is not None:
                        results.append(info)
        return results

    async def find_window_by_name(self, window_name: str) -> ITermWindow | None:
        """Find a ccbot-owned session by its display name."""
        for w in await self.list_windows():
            if w.window_name == window_name:
                return w
        logger.debug("Window not found by name: %s", window_name)
        return None

    async def find_window_by_id(self, window_id: str) -> ITermWindow | None:
        """Find a ccbot-owned session by its iTerm2 session UUID."""
        try:
            app = await self._get_app()
        except ConnectionError as e:
            logger.warning("find_window_by_id: iTerm2 unreachable: %s", e)
            return None

        session = app.get_session_by_id(window_id)
        if session is None:
            logger.debug("Window not found by id: %s", window_id)
            return None
        return await self._session_to_window(session)

    # ------------------------------------------------------------------
    # Implementation helpers
    # ------------------------------------------------------------------

    async def _session_to_window(self, session: iterm2.Session) -> ITermWindow | None:
        """Convert a Session to ITermWindow, or None if not ccbot-owned.

        Filters out sessions without the ``user.ccbot=1`` tag so the
        user's own iTerm2 tabs stay invisible to the bot.
        """
        try:
            tag = await session.async_get_variable(_CCBOT_TAG_NAME)
        except Exception as e:
            logger.debug("Failed to read tag for session %s: %s", session.session_id, e)
            return None
        if str(tag) != _CCBOT_TAG_VALUE:
            return None

        # name / path / jobName are best-effort; missing values become "".
        name = await self._get_var(session, "session.name") or ""
        cwd = await self._get_var(session, "session.path") or ""
        job = await self._get_var(session, "session.jobName") or ""

        return ITermWindow(
            window_id=session.session_id,
            window_name=name,
            cwd=cwd,
            pane_current_command=job,
        )

    @staticmethod
    async def _get_var(session: iterm2.Session, name: str) -> str:
        """Read an iTerm2 session variable, returning "" on any error."""
        try:
            value = await session.async_get_variable(name)
        except Exception:
            return ""
        return "" if value is None else str(value)

    # ------------------------------------------------------------------
    # Stubs for later units
    # ------------------------------------------------------------------

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
        """Rename a ccbot-owned session."""
        raise NotImplementedError("Unit 4")

    async def kill_window(self, window_id: str) -> bool:
        """Close a ccbot-owned session."""
        raise NotImplementedError("Unit 4")

    async def create_window(
        self,
        work_dir: str,
        window_name: str | None = None,
        start_claude: bool = True,
        resume_session_id: str | None = None,
    ) -> tuple[bool, str, str, str]:
        """Create a new ccbot-owned tab and optionally start Claude Code."""
        raise NotImplementedError("Unit 4")


iterm2_manager = ITerm2Manager()

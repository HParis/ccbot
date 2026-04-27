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
import tempfile
from asyncio import sleep as _sleep
from dataclasses import dataclass
from pathlib import Path

import iterm2
import iterm2.screen as iterm2_screen

logger = logging.getLogger(__name__)


# Cached main-screen height (logical points, Cocoa coords).  Used to
# convert iTerm2 frame coordinates into ``screencapture -R`` rectangle
# coordinates.  Cached because querying via osascript spawns a process.
_main_screen_height_cache: float | None = None


async def _main_screen_height() -> float | None:
    """Return the main screen's height in logical points, cached.

    Uses JavaScript-for-Automation to read ``NSScreen.mainScreen.frame``
    so we don't add a PyObjC dependency.  Returns None on failure.
    """
    global _main_screen_height_cache
    if _main_screen_height_cache is not None:
        return _main_screen_height_cache
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript",
            "-l",
            "JavaScript",
            "-e",
            'ObjC.import("Cocoa"); $.NSScreen.mainScreen.frame.size.height',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
        _main_screen_height_cache = float(stdout.decode("utf-8").strip())
        return _main_screen_height_cache
    except Exception as e:
        logger.error("Failed to read main screen height: %s", e)
        return None


# Reconnect backoff (seconds). Three attempts before giving up.
_RECONNECT_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)

# Marker variable used to identify ccbot-owned sessions. Stored as the
# iTerm2 user-variable ``user.ccbot``; value is the literal "1".
_CCBOT_TAG_NAME = "user.ccbot"
_CCBOT_TAG_VALUE = "1"

# Named-key escape sequences used when ``send_keys(..., literal=False)``.
# Anything not listed here is sent verbatim, matching the previous
# tmux backend's behaviour for unrecognised key names.
_SPECIAL_KEYS: dict[str, str] = {
    "Up": "\x1b[A",
    "Down": "\x1b[B",
    "Right": "\x1b[C",
    "Left": "\x1b[D",
    "Escape": "\x1b",
    "Tab": "\t",
    "Enter": "\r",
}

# Claude Code's TUI sometimes interprets a rapid Enter that arrives in
# the same input batch as the surrounding text as a newline rather than
# submit. The 500ms gap between text and Enter lets the TUI settle.
_ENTER_DELAY = 0.5

# Claude Code's ``!`` bash-mode prefix needs the TUI to switch modes
# before the rest of the line arrives.
_BASH_PREFIX_DELAY = 1.0


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
                    await _sleep(delay)
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
        """Return a refreshed App handle, reconnecting on disconnect.

        Catches broadly because iTerm2's WebSocket layer raises
        ``websockets.exceptions.ConnectionClosedError`` (subclass of
        ``Exception``, NOT ``ConnectionError``) when iTerm2 quits or
        the system sleeps.  If we only catch ``ConnectionError``, the
        cached dead connection sticks around forever and every
        subsequent call re-throws — observed in production after a
        sleep/wake cycle.  Drop the cache on any failure here and let
        the next call reconnect cleanly.
        """
        try:
            conn = await self._get_connection()
            if self._app is None:
                app = await iterm2.async_get_app(conn)
                if app is None:
                    raise ConnectionError("iTerm2 returned no App instance")
                self._app = app
            await self._app.async_refresh()
            return self._app
        except Exception as e:
            self._connection = None
            self._app = None
            # Re-raise as ConnectionError so callers can use one
            # exception class for "iTerm2 unreachable" handling.
            if isinstance(e, ConnectionError):
                raise
            raise ConnectionError(
                f"Lost iTerm2 connection: {type(e).__name__}: {e}"
            ) from e

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
    # Input / output
    # ------------------------------------------------------------------

    async def _resolve_session(self, window_id: str) -> iterm2.Session | None:
        """Return the live Session for ``window_id``, or None if missing."""
        try:
            app = await self._get_app()
        except ConnectionError as e:
            logger.warning("iTerm2 unreachable: %s", e)
            return None
        return app.get_session_by_id(window_id)

    async def screenshot_session(self, window_id: str) -> bytes | None:
        """Capture a real pixel screenshot of the ccbot session's tab.

        Brings the target tab to the front of its iTerm2 window (without
        activating iTerm2 across apps), reads the window's frame, then
        shells out to ``screencapture -R x,y,w,h`` to grab a PNG of
        just that window region.  This is preferred over the
        ANSI-rebuild-and-render path because it preserves Nerd Font
        glyphs, emoji, ligatures, and any other rendering iTerm2 does.

        Returns PNG bytes on success.  Returns None if:
          - the session is gone
          - macOS Screen Recording permission isn't granted to the
            bot's executable (one-time grant in System Settings →
            Privacy & Security → Screen Recording)
          - the iTerm2 window is fully off-screen
        """
        try:
            app = await self._get_app()
        except ConnectionError as e:
            logger.warning("screenshot_session: iTerm2 unreachable: %s", e)
            return None

        session = app.get_session_by_id(window_id)
        if session is None:
            logger.debug("screenshot_session: session not found: %s", window_id)
            return None

        # Locate the iTerm2 Window + Tab containing this session.
        tab = window = None
        for w in app.windows:
            for t in w.tabs:
                for s in t.sessions:
                    if s.session_id == window_id:
                        tab, window = t, w
                        break
                if tab is not None:
                    break
            if tab is not None:
                break
        if tab is None or window is None:
            logger.debug("screenshot_session: tab/window not located for %s", window_id)
            return None

        # Bring the target tab to front of its iTerm2 window so the
        # rectangle we capture actually shows it. order_window_front=True
        # raises the iTerm2 window above other windows of the same app
        # but does not switch app focus globally.
        try:
            await tab.async_select(order_window_front=True)
        except Exception as e:
            logger.debug("async_select failed (continuing anyway): %s", e)

        try:
            frame = await window.async_get_frame()
        except Exception as e:
            logger.error("Failed to read window frame: %s", e)
            return None

        screen_h = await _main_screen_height()
        if screen_h is None:
            logger.error("Could not determine main screen height")
            return None

        # Cocoa frame (origin at bottom-left of main screen) →
        # screencapture rect (origin at top-left of main screen).
        cocoa_x = float(frame.origin.x)
        cocoa_y = float(frame.origin.y)
        w_px = float(frame.size.width)
        h_px = float(frame.size.height)
        screen_y = screen_h - cocoa_y - h_px
        rect = f"{int(cocoa_x)},{int(screen_y)},{int(w_px)},{int(h_px)}"

        out_path = Path(tempfile.mkstemp(prefix="ccbot-shot-", suffix=".png")[1])
        try:
            # Absolute path: launchd's default PATH excludes /usr/sbin,
            # so a bare "screencapture" lookup fails when ccbot is run
            # as a LaunchAgent.  /usr/sbin/screencapture has been the
            # canonical location since macOS 10.x.
            proc = await asyncio.create_subprocess_exec(
                "/usr/sbin/screencapture",
                "-x",  # silent (no shutter sound)
                "-o",  # exclude window shadow when in window mode (harmless for -R)
                "-R",
                rect,
                "-t",
                "png",
                str(out_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.error(
                    "screencapture failed (rc=%s): %s",
                    proc.returncode,
                    stderr.decode("utf-8", errors="replace").strip(),
                )
                return None
            try:
                return out_path.read_bytes()
            except OSError as e:
                logger.error("Failed to read screenshot tempfile: %s", e)
                return None
        finally:
            try:
                out_path.unlink(missing_ok=True)
            except OSError:
                pass

    async def capture_pane(self, window_id: str, with_ansi: bool = False) -> str | None:
        """Capture the visible text content of a session's screen.

        Args:
            window_id: iTerm2 session UUID.
            with_ansi: If True, emit SGR escape codes (16/256/RGB
                color) in the dialect ``screenshot.py`` understands. If
                False, return plain text only.

        Returns:
            Captured text (one ``\\n``-joined string), or ``None`` on
            failure.
        """
        session = await self._resolve_session(window_id)
        if session is None:
            logger.debug("capture_pane: session not found: %s", window_id)
            return None

        try:
            contents = await session.async_get_screen_contents()
        except Exception as e:
            logger.error("Failed to get screen contents for %s: %s", window_id, e)
            return None

        lines: list[str] = []
        for i in range(contents.number_of_lines):
            line = contents.line(i)
            if with_ansi:
                lines.append(_line_to_ansi(line))
            else:
                lines.append(line.string)
        return "\n".join(lines)

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
        session = await self._resolve_session(window_id)
        if session is None:
            logger.error("send_keys: session not found: %s", window_id)
            return False

        try:
            if literal and enter:
                # Two-phase send so the TUI sees text and Enter as
                # separate events. !-prefix needs an extra 1s gap so
                # the TUI switches into bash mode first.
                if text.startswith("!"):
                    await session.async_send_text("!")
                    rest = text[1:]
                    if rest:
                        await _sleep(_BASH_PREFIX_DELAY)
                        await session.async_send_text(rest)
                else:
                    await session.async_send_text(text)
                await _sleep(_ENTER_DELAY)
                await session.async_send_text("\r")
                return True

            # Single-shot send for special keys or no-Enter cases.
            payload = text if literal else _SPECIAL_KEYS.get(text, text)
            if enter:
                payload += "\r"
            await session.async_send_text(payload)
            return True
        except Exception as e:
            logger.error("send_keys to %s failed: %s", window_id, e)
            return False

    # ------------------------------------------------------------------
    # Window lifecycle
    # ------------------------------------------------------------------

    async def rename_window(self, window_id: str, new_name: str) -> bool:
        """Rename a ccbot-owned session."""
        session = await self._resolve_session(window_id)
        if session is None:
            logger.error("rename_window: session not found: %s", window_id)
            return False
        try:
            await session.async_set_name(new_name)
            logger.info("Renamed session %s to '%s'", window_id, new_name)
            return True
        except Exception as e:
            logger.error("Failed to rename session %s: %s", window_id, e)
            return False

    async def kill_window(self, window_id: str) -> bool:
        """Close a ccbot-owned session.

        ccbot creates one session per tab, so closing the session also
        closes the tab.
        """
        session = await self._resolve_session(window_id)
        if session is None:
            logger.debug("kill_window: session %s already gone", window_id)
            return False
        try:
            await session.async_close(force=True)
            logger.info("Killed session %s", window_id)
            return True
        except Exception as e:
            logger.error("Failed to close session %s: %s", window_id, e)
            return False

    async def _get_target_window(self, app: iterm2.App) -> iterm2.Window | None:
        """Return the iTerm2 window that should host new ccbot tabs.

        Preference order:
          1. A window that already contains a ccbot-tagged session.
          2. The user's currently active window.
          3. None — caller must create a new window.
        """
        for window in app.windows:
            for tab in window.tabs:
                for session in tab.sessions:
                    try:
                        tag = await session.async_get_variable(_CCBOT_TAG_NAME)
                    except Exception:
                        tag = None
                    if str(tag) == _CCBOT_TAG_VALUE:
                        return window

        return app.current_window

    async def _create_tab_with_profile(
        self, window: iterm2.Window
    ) -> iterm2.Tab | None:
        """Create a tab in ``window`` using the configured profile, or
        the default profile if the configured one doesn't exist."""
        try:
            tab = await window.async_create_tab(profile=self.profile_name)
            if tab is not None:
                return tab
        except Exception as e:
            logger.warning(
                "Failed to create tab with profile '%s' (%s); "
                "falling back to default profile",
                self.profile_name,
                e,
            )

        try:
            return await window.async_create_tab()
        except Exception as e:
            logger.error("Failed to create tab with default profile: %s", e)
            return None

    async def create_window(
        self,
        work_dir: str,
        window_name: str | None = None,
        start_claude: bool = True,
        resume_session_id: str | None = None,
    ) -> tuple[bool, str, str, str]:
        """Create a new ccbot-owned tab and optionally start Claude Code.

        See class docstring for the full semantics. The actual shell
        runs ``cd <work_dir> && claude [--resume <id>]`` so a failed
        ``cd`` won't drop the user into a wrong-directory Claude.
        """
        from pathlib import Path
        from shlex import quote

        from .config import config

        path = Path(work_dir).expanduser().resolve()
        if not path.exists():
            return False, f"Directory does not exist: {work_dir}", "", ""
        if not path.is_dir():
            return False, f"Not a directory: {work_dir}", "", ""

        # De-dup the display name against currently-tagged sessions.
        final_name = window_name or path.name
        base = final_name
        counter = 2
        while await self.find_window_by_name(final_name) is not None:
            final_name = f"{base}-{counter}"
            counter += 1

        try:
            app = await self._get_app()
        except ConnectionError as e:
            return False, f"iTerm2 unreachable: {e}", "", ""

        host = await self._get_target_window(app)
        if host is None:
            try:
                conn = await self._get_connection()
                host = await iterm2.Window.async_create(conn, profile=self.profile_name)
            except Exception as e:
                logger.warning(
                    "Failed to create iTerm2 window with profile '%s' (%s); "
                    "falling back to default profile",
                    self.profile_name,
                    e,
                )
                try:
                    conn = await self._get_connection()
                    host = await iterm2.Window.async_create(conn)
                except Exception as e2:
                    return False, f"Failed to create iTerm2 window: {e2}", "", ""
            if host is None:
                return False, "iTerm2 returned no window", "", ""

        tab = await self._create_tab_with_profile(host)
        if tab is None:
            return False, "Failed to create tab", "", ""

        session = tab.current_session
        if session is None:
            return False, "New tab has no session", "", ""

        try:
            await session.async_set_variable(_CCBOT_TAG_NAME, _CCBOT_TAG_VALUE)
            await session.async_set_name(final_name)
        except Exception as e:
            logger.error("Failed to tag/name new session: %s", e)
            return False, f"Failed to tag/name session: {e}", "", ""

        # Build the boot command.  ``exec`` replaces the shell so Claude
        # owns the PTY directly — closing claude closes the session,
        # matching the previous tmux behaviour.
        cd_quoted = quote(str(path))
        if start_claude:
            cmd = config.claude_command
            if resume_session_id:
                cmd = f"{cmd} --resume {resume_session_id}"
            boot = f"cd {cd_quoted} && exec {cmd}\n"
        else:
            boot = f"cd {cd_quoted}\n"

        try:
            await session.async_send_text(boot)
        except Exception as e:
            logger.error("Failed to send boot command: %s", e)
            return False, f"Failed to start session: {e}", "", ""

        logger.info(
            "Created ccbot tab '%s' (uuid=%s) at %s",
            final_name,
            session.session_id,
            path,
        )
        return (
            True,
            f"Created tab '{final_name}' at {path}",
            final_name,
            session.session_id,
        )


# ----------------------------------------------------------------------
# ANSI reconstruction from iTerm2 cell styles
# ----------------------------------------------------------------------


def _color_to_sgr(
    color: iterm2_screen.CellStyle.Color | None, is_fg: bool
) -> tuple[str, ...]:
    """Convert a CellStyle.Color to SGR parameter parts.

    Output dialect matches ``screenshot.py:_apply_ansi_codes``:
      - basic 16 → 30-37 / 40-47 / 90-97 / 100-107
      - extended 256 → 38;5;N / 48;5;N
      - RGB → 38;2;R;G;B / 48;2;R;G;B
      - default / alternate → 39 / 49

    Returns a tuple of stringified parameters (so the caller can
    diff them between cells before joining with ``;``).

    Note: iTerm2's ``CellStyle.Color`` exposes ``standard`` / ``rgb``
    / ``alternate`` as properties that **raise** when the colour
    isn't of that kind — it does not return None. Always probe via
    the ``is_*`` boolean properties first.
    """
    default = ("39",) if is_fg else ("49",)

    if color is None:
        return default

    if color.is_standard:
        n = int(color.standard)
        if n < 8:
            return (str((30 if is_fg else 40) + n),)
        if n < 16:
            return (str((90 if is_fg else 100) + n - 8),)
        return ("38" if is_fg else "48", "5", str(n))

    if color.is_rgb:
        rgb = color.rgb
        return (
            "38" if is_fg else "48",
            "2",
            str(rgb.red),
            str(rgb.green),
            str(rgb.blue),
        )

    # alternate (DEFAULT / REVERSED_DEFAULT / SYSTEM_MESSAGE) and
    # placement both fall back to the default colour.
    return default


def _line_to_ansi(line: iterm2_screen.LineContents) -> str:
    """Re-serialize a screen line into ANSI-coloured text.

    Emits SGR codes only when the colour changes from the previous cell
    and resets at end-of-line so the next line starts clean.
    """
    text = line.string
    parts: list[str] = []
    last_fg: tuple[str, ...] | None = None
    last_bg: tuple[str, ...] | None = None

    for x, ch in enumerate(text):
        try:
            style = line.style_at(x)
        except Exception:
            style = None

        if style is not None:
            fg = _color_to_sgr(style.fg_color, is_fg=True)
            bg = _color_to_sgr(style.bg_color, is_fg=False)
        else:
            fg = ("39",)
            bg = ("49",)

        codes: list[str] = []
        if fg != last_fg:
            codes.extend(fg)
            last_fg = fg
        if bg != last_bg:
            codes.extend(bg)
            last_bg = bg
        if codes:
            parts.append(f"\x1b[{';'.join(codes)}m")
        parts.append(ch)

    parts.append("\x1b[0m")
    return "".join(parts)


iterm2_manager = ITerm2Manager()

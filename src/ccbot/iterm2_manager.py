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
import functools
import logging
import re
import tempfile
from asyncio import sleep as _sleep
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar, cast

import iterm2
import iterm2.screen as iterm2_screen

from .terminal.base import Capabilities, ReconnectListener, TerminalSession
from .terminal.registry import register

logger = logging.getLogger(__name__)


# Screenshot pipeline.  ``screencapture -l <CGWindowID>`` is robust
# across multi-display and Retina setups (no hand-rolled coordinate
# math), but the iTerm2 Python API does not expose CGWindowID.  We
# resolve it via an osascript that matches the session by ``unique ID``
# (= iTerm2 session UUID), brings that tab to the front of its window
# without ``activate`` (so app focus does not switch globally), and
# returns ``id of w`` — that is the macOS CGWindowID.
_SCREENCAPTURE_BIN = "/usr/sbin/screencapture"
_OSASCRIPT_BIN = "/usr/bin/osascript"
# Empirically, iTerm2 needs ~0.3-0.4s to repaint after a tab select
# before the offscreen window buffer reflects the new tab's contents.
_SCREENSHOT_REDRAW_DELAY = 0.4


# Reconnect backoff (seconds). Three attempts before giving up.
# First entry is "wait before attempt 1" — 0.0 means try immediately.
_RECONNECT_DELAYS: tuple[float, ...] = (0.0, 1.0, 2.0, 4.0)

# Wait pattern after auto-launching iTerm2 via ``open -a iTerm``.
# iTerm2 needs a moment to start the WebSocket server; first attempt
# is delayed 1.5s to give it a head start.
_LAUNCH_DELAYS: tuple[float, ...] = (1.5, 2.0, 3.0, 5.0)

# Hard ceiling on any single public backend call.  iTerm2's Python API
# awaits a bare Future for each RPC response
# (``connection.async_dispatch_until_id``); its read loop
# (``_async_dispatch_forever``) is the only thing that ever resolves it.
# When the websocket dies mid-request that read loop exits on the socket
# error WITHOUT resolving or cancelling the pending Future, so an
# unguarded ``await`` blocks forever — a single iTerm2 quit is enough to
# freeze a caller permanently.  That is what silently killed the status
# poll loop (and with it the per-second topic auto-rebind) for days.
#
# Sized above the worst legitimate path: connect backoff (0+1+2+4 = 7s)
# plus auto-launch plus post-launch backoff (1.5+2+3+5 = 11.5s).
_CALL_TIMEOUT = 45.0

# Marker variable used to identify ccbot-owned sessions. Stored as the
# iTerm2 user-variable ``user.ccbot``; value is the literal "1".
_CCBOT_TAG_NAME = "user.ccbot"
_CCBOT_TAG_VALUE = "1"

# An iTerm2 session id is a UUID (used by is_session_id for session_map keys).
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

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


# ``ITermWindow`` is retained as a backward-compatible alias for the
# vendor-neutral ``TerminalSession`` (identical fields). Existing call
# sites and tests import ``ITermWindow``; new code should prefer
# ``TerminalSession``. ``window_id`` carries the iTerm2 session UUID.
ITermWindow = TerminalSession


_AsyncMethod = TypeVar("_AsyncMethod", bound=Callable[..., Awaitable[Any]])

# Sentinel: re-raise as ConnectionError instead of returning a value.
_RAISE = object()


def _bounded(fallback: Any = _RAISE) -> Callable[[_AsyncMethod], _AsyncMethod]:
    """Cap a public backend call at ``_CALL_TIMEOUT`` seconds.

    Guarantees no caller can wedge on a half-dead iTerm2 websocket (see
    ``_CALL_TIMEOUT``).  On timeout the connection is abandoned and the
    circuit breaker trips, so polling loops back off instead of queueing
    behind the same dead socket.

    ``fallback`` is the method's own "iTerm2 is unavailable" value — the
    same one it already returns when ``_get_app`` raises ConnectionError.
    The default re-raises the timeout as ConnectionError.
    """

    def decorate(fn: _AsyncMethod) -> _AsyncMethod:
        @functools.wraps(fn)
        async def wrapper(self: ITerm2Manager, *args: Any, **kwargs: Any) -> Any:
            try:
                return await asyncio.wait_for(fn(self, *args, **kwargs), _CALL_TIMEOUT)
            except TimeoutError:
                await self._abandon_connection(fn.__name__)
                if fallback is _RAISE:
                    raise ConnectionError(
                        f"iTerm2 call {fn.__name__} timed out "
                        f"after {_CALL_TIMEOUT:.0f}s"
                    ) from None
                return fallback

        return cast(_AsyncMethod, wrapper)

    return decorate


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
        # Circuit breaker: when the API has rejected us recently, stop
        # opening fresh websockets for a while.  iTerm2 throttles
        # clients that reconnect aggressively; without a breaker, the
        # bot's polling loops (status 1s, monitor 2s, ...) thunder on
        # iTerm2 and accelerate the failure into a permanent loop.
        self._circuit_open_until: float = 0.0
        self._consecutive_failures: int = 0
        # Listeners fired after a *re*-connection (not the first ever).
        # Used by upper layers to re-resolve stale UUIDs: when iTerm2
        # quits and restarts every session UUID changes, so cached
        # bindings in SessionManager must be re-mapped against live
        # tabs.  Listeners are awaited in registration order and may
        # not raise — exceptions are logged and swallowed so a buggy
        # listener can't break the connect path.
        self._reconnect_listeners: list[ReconnectListener] = []
        self._ever_connected: bool = False

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def add_reconnect_listener(self, callback: ReconnectListener) -> None:
        """Register a coroutine to run after every iTerm2 reconnection.

        The callback is NOT invoked for the first-ever connection — only
        when an existing connection had to be re-established (e.g. iTerm2
        was quit and relaunched). Use this to refresh state that depends
        on iTerm2 session UUIDs, which change across iTerm2 restarts.
        """
        self._reconnect_listeners.append(callback)

    async def _fire_reconnect_listeners(self) -> None:
        for cb in self._reconnect_listeners:
            try:
                await cb()
            except Exception as e:
                logger.error("iTerm2 reconnect listener failed: %s", e)

    async def _try_connect_once(
        self,
    ) -> tuple[iterm2.Connection | None, Exception | None]:
        """Attempt one ``Connection.async_create`` call.

        Returns (conn, None) on success, (None, exc) on failure.
        Caller decides whether to retry / launch iTerm2 / give up.
        """
        try:
            conn = await iterm2.Connection.async_create()
            return conn, None
        except Exception as e:
            return None, e

    async def _try_connect_with_backoff(
        self, delays: tuple[float, ...]
    ) -> tuple[iterm2.Connection | None, Exception | None]:
        """Run a sequence of connection attempts separated by sleeps.

        ``delays[0]`` is applied BEFORE the first attempt (use 0.0
        for "try immediately") so callers can tune the timing of an
        initial wait (e.g. just after launching iTerm2).
        """
        last_err: Exception | None = None
        for attempt, delay in enumerate(delays):
            if delay > 0:
                logger.debug(
                    "iTerm2 connect: waiting %.1fs (attempt %d)",
                    delay,
                    attempt + 1,
                )
                await _sleep(delay)
            conn, err = await self._try_connect_once()
            if conn is not None:
                return conn, None
            last_err = err
            logger.debug("iTerm2 connect attempt %d failed: %s", attempt + 1, err)
        return None, last_err

    async def _launch_iterm2(self) -> bool:
        """Shell out to ``open -a iTerm`` to start iTerm2 in the
        background.  Returns False if ``open`` itself fails (rare —
        usually means /usr/bin/open is missing or iTerm2 isn't
        installed under any known name)."""
        for app_name in ("iTerm", "iTerm2"):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "/usr/bin/open",
                    "-a",
                    app_name,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await proc.communicate()
                if proc.returncode == 0:
                    logger.info("Launched iTerm2 via 'open -a %s'", app_name)
                    return True
                logger.debug(
                    "open -a %s failed: %s",
                    app_name,
                    stderr.decode("utf-8", errors="replace").strip(),
                )
            except FileNotFoundError:
                # /usr/bin/open missing — extremely unlikely on macOS
                logger.error("/usr/bin/open not found; cannot auto-launch iTerm2")
                return False
            except Exception as e:
                logger.debug("Failed to launch %s: %s", app_name, e)
        return False

    async def _get_connection(self, allow_launch: bool = False) -> iterm2.Connection:
        """Return a live iTerm2 connection, reconnecting if needed.

        With ``allow_launch=True``, a failure across the standard
        backoff shells out to ``open -a iTerm`` to launch iTerm2 in
        the background, then retries.  The user no longer has to
        manually start iTerm2 before sending a message in Telegram —
        the bot resurrects the dependency on demand.

        ``allow_launch`` defaults to False so that only user-driven
        actions can resurrect iTerm2.  Background work (status
        polling every second, screenshots, discovery) must stay
        passive: during macOS shutdown/logout the system quits
        iTerm2, and a polling loop that immediately relaunches it
        registers as a newly-started app and cancels the shutdown.

        Honours the circuit breaker: if recent attempts failed
        repeatedly, raises immediately without touching iTerm2.

        Raises:
            ConnectionError: After the initial backoff (plus the
                post-launch retry when launching was allowed) has
                failed, or while the breaker is open.  Likely causes:
                iTerm2 isn't running, isn't installed, or the Python
                API is disabled.
        """
        # Fast-fail while breaker is open.  Many polling loops call
        # this every second; without the breaker, every call opens a
        # fresh websocket and iTerm2 throttles the whole bot into a
        # permanent failure state.
        loop = asyncio.get_event_loop()
        now = loop.time()
        if now < self._circuit_open_until:
            wait = self._circuit_open_until - now
            raise ConnectionError(
                f"iTerm2 backoff: not retrying for {wait:.1f}s"
                f" (after {self._consecutive_failures} consecutive failures)"
            )

        async with self._connect_lock:
            if self._connection is not None:
                return self._connection

            # Phase 1: assume iTerm2 is already running.  Standard
            # backoff (1s / 2s / 4s) — fast path for the common case.
            conn, err = await self._try_connect_with_backoff(_RECONNECT_DELAYS)
            if conn is not None:
                logger.info("Connected to iTerm2 Python API")
            else:
                if not allow_launch:
                    # Passive caller: report the failure and let the
                    # breaker back us off.  iTerm2 stays closed —
                    # including while macOS is shutting down.
                    self._trip_breaker()
                    raise ConnectionError(
                        "iTerm2 is not reachable (not running, or the Python "
                        "API is disabled).  Not auto-launching it for a "
                        "background operation."
                    ) from err

                # Phase 2: probably not running — try to launch it.
                logger.info(
                    "iTerm2 unreachable after %d attempts; launching via "
                    "'open -a iTerm'",
                    len(_RECONNECT_DELAYS),
                )
                launched = await self._launch_iterm2()
                if not launched:
                    self._trip_breaker()
                    raise ConnectionError(
                        "Cannot reach iTerm2 and could not launch it via "
                        "'open -a iTerm'.  Make sure iTerm2 is installed and "
                        "the Python API is enabled (Preferences → General → "
                        "Magic → Enable Python API)."
                    ) from err

                # Phase 3: iTerm2 takes a moment to start serving the API
                # after launch.  Slightly longer waits than phase 1.
                conn, err = await self._try_connect_with_backoff(_LAUNCH_DELAYS)
                if conn is None:
                    self._trip_breaker()
                    raise ConnectionError(
                        "iTerm2 launched but the Python API is still "
                        "unreachable. Verify Preferences → General → Magic → "
                        "Enable Python API is on, then retry."
                    ) from err
                logger.info("Connected to iTerm2 Python API after auto-launch")

            self._connection = conn
            self._app = None
            # Decide whether this is a *re*-connection INSIDE the lock, but
            # defer firing reconnect listeners until AFTER the lock is
            # released.  Listeners re-enter _get_connection()/_get_app()
            # (list_windows → resolve_stale_ids → rebind) and asyncio.Lock
            # is NOT reentrant — firing them while still holding the lock
            # self-deadlocks the whole event loop on every iTerm2 restart.
            # self._connection is already set above, so the re-entrant call
            # hits the cached-connection fast path and returns immediately.
            fire_reconnect = self._ever_connected
            self._ever_connected = True

        if fire_reconnect:
            await self._fire_reconnect_listeners()
        return conn

    async def _close_connection(self, conn: iterm2.Connection | None) -> None:
        """Close an iTerm2 Connection's underlying websocket, cancel
        its dispatch task, and invalidate the cached auth cookie.

        Three jobs done at once because they share the same trigger
        ("the cached connection is dead, throw it away"):

        1. Close the websocket: the iterm2 lib's Connection holds
           the websocket + a background dispatcher task on its own
           instance; dropping our reference doesn't tear them down.
           Without explicit close, every reconnect leaves a phantom
           client that iTerm2 counts against its throttle limit.

        2. Cancel the dispatcher task: same reason.

        3. Clear ``ITERM2_COOKIE`` / ``ITERM2_KEY`` from the env so
           the next ``Connection.async_create`` re-runs AppleScript
           to fetch a fresh cookie.  When iTerm2 quits and relaunches,
           the cookie inherited from the old process is invalid.  The
           lib only re-auths on HTTP 401, but iTerm2 closes the
           websocket silently instead — so without this, every
           reconnect succeeds at the handshake and then the first
           RPC dies with ConnectionClosedError, in a permanent loop.
        """
        if conn is not None:
            ws = getattr(conn, "websocket", None)
            if ws is not None:
                try:
                    close = getattr(ws, "close", None)
                    if close is not None:
                        result = close()
                        if asyncio.iscoroutine(result):
                            await result
                except Exception as e:
                    logger.debug("Error closing iTerm2 websocket: %s", e)
            future = getattr(conn, "_Connection__dispatch_forever_future", None)
            if future is not None and not future.done():
                future.cancel()
            tasks = getattr(conn, "_Connection__tasks", None) or []
            for task in tasks:
                if not task.done():
                    task.cancel()

        # Clear cached auth so next connect re-runs AppleScript.
        import os as _os

        for var in ("ITERM2_COOKIE", "ITERM2_KEY"):
            _os.environ.pop(var, None)

        # CRITICAL: iterm2.app.App.instance is a MODULE-LEVEL singleton
        # in the iterm2 library.  Once async_get_app() succeeds, the
        # lib stores the App on App.instance and registers a disconnect
        # callback to clear it.  But that callback only fires if the
        # dispatcher task processes a clean disconnect — abrupt
        # ConnectionClosedError doesn't always trigger it.  When iTerm2
        # quits and relaunches, the bot's process ends up with a stale
        # App.instance pointing at the old dead connection's session
        # graph; every subsequent async_get_app() returns that stale
        # App and async_refresh() against it fails forever.  Force-clear
        # the singleton ourselves on every connection invalidation.
        try:
            import iterm2.app as _app_mod

            _app_mod.invalidate_app()
        except Exception as e:
            logger.debug("Failed to invalidate App.instance: %s", e)

    def is_reachable(self) -> bool:
        """Cheap, side-effect-free check for whether iTerm2 is likely up.

        Returns False while the circuit breaker is open (recent connect
        attempts have failed). True doesn't guarantee the next call will
        succeed, but False is a strong "definitely don't take destructive
        action that assumes the absence of a tab means it was closed".

        Polling loops use this before unbinding stale threads — without
        the check, a transient WebSocket drop reads as "every tab is
        gone" and the bot wipes every binding while iTerm2 is restarting.
        """
        if self._circuit_open_until <= 0:
            return True
        loop = asyncio.get_event_loop()
        return loop.time() >= self._circuit_open_until

    def _trip_breaker(self) -> None:
        """Record a failed connection attempt and open the breaker.

        Backoff schedule (in seconds, indexed by consecutive_failures):
            1 →  2s,  2 →  5s,  3 → 15s,  4 → 30s,  5+ → 60s.
        """
        self._consecutive_failures += 1
        delays = (2.0, 5.0, 15.0, 30.0, 60.0)
        idx = min(self._consecutive_failures - 1, len(delays) - 1)
        delay = delays[idx]
        loop = asyncio.get_event_loop()
        self._circuit_open_until = loop.time() + delay
        if self._consecutive_failures <= 3:
            logger.warning(
                "iTerm2 connection failed (#%d); breaker open for %.0fs",
                self._consecutive_failures,
                delay,
            )

    def _reset_breaker(self) -> None:
        """Clear the breaker after a successful operation."""
        if self._consecutive_failures:
            logger.info(
                "iTerm2 connection healthy again after %d failures",
                self._consecutive_failures,
            )
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

    async def _get_app(self, allow_launch: bool = False) -> iterm2.App:
        """Return a refreshed App handle, reconnecting on disconnect.

        ``allow_launch`` is forwarded to ``_get_connection``: only
        user-driven callers may start iTerm2 if it isn't running.

        Catches broadly because iTerm2's WebSocket layer raises
        ``websockets.exceptions.ConnectionClosedError`` (subclass of
        ``Exception``, NOT ``ConnectionError``) when iTerm2 quits or
        the system sleeps.  Drop the cache on any failure here and
        let the next call reconnect cleanly.

        Failures of post-connect operations (async_get_app /
        async_refresh) trip the circuit breaker so successive
        polling-loop calls don't flood iTerm2 with reconnect attempts.
        ``_get_connection`` failures are NOT re-tripped here — that
        function already manages its own breaker timer.
        """
        # Phase 1: get a connection.  If this fails, the breaker is
        # either already open (and we just propagate) or the failure
        # was inside _get_connection's launch path.  Either way,
        # don't double-trip.
        conn = await self._get_connection(allow_launch=allow_launch)

        # Phase 2: query iTerm2.  Failures here are real (websocket
        # alive but RPC didn't work, e.g. iTerm2 throttled us);
        # trip the breaker so the next call backs off.
        try:
            if self._app is None:
                app = await iterm2.async_get_app(conn)
                if app is None:
                    raise ConnectionError("iTerm2 returned no App instance")
                self._app = app
            await self._app.async_refresh()
            self._reset_breaker()
            return self._app
        except Exception as e:
            stale_conn = self._connection
            self._connection = None
            self._app = None
            # Close the stale websocket + cancel dispatch tasks so
            # they don't accumulate as phantom clients on iTerm2's
            # side and trigger its connection throttle.
            await self._close_connection(stale_conn)
            self._trip_breaker()
            if isinstance(e, ConnectionError):
                raise
            raise ConnectionError(
                f"Lost iTerm2 connection: {type(e).__name__}: {e}"
            ) from e

    def _invalidate_connection(self) -> None:
        """Drop cached connection so the next call reconnects."""
        self._connection = None
        self._app = None

    async def _abandon_connection(self, label: str) -> None:
        """Tear down a connection whose RPC never came back.

        Called by ``_bounded`` on timeout: the websocket is (at best)
        half-dead, so drop and close it, and trip the breaker so the
        1s/2s polling loops back off rather than piling more doomed
        calls onto it.
        """
        logger.warning(
            "iTerm2 call %s timed out after %.0fs; dropping the connection",
            label,
            _CALL_TIMEOUT,
        )
        stale = self._connection
        self._invalidate_connection()
        self._trip_breaker()
        if stale is None:
            return
        try:
            await asyncio.wait_for(self._close_connection(stale), timeout=5.0)
        except Exception as e:
            logger.debug("Failed to close timed-out iTerm2 connection: %s", e)

    # ------------------------------------------------------------------
    # TerminalBackend contract: capabilities + neutral lifecycle
    # ------------------------------------------------------------------

    @property
    def capabilities(self) -> Capabilities:
        """iTerm2 supports the full feature set."""
        return Capabilities(
            ansi_capture=True,
            native_tagging=True,
            reconnect_events=True,
            screenshot=True,
        )

    @property
    def session_map_prefix(self) -> str:
        return "iterm:"

    def is_session_id(self, candidate: str) -> bool:
        """An iTerm2 session id is a UUID."""
        return bool(_UUID_RE.match(candidate))

    @_bounded()
    async def preflight(self) -> None:
        """Verify iTerm2 is reachable, raising ConnectionError if not.

        Opens (and the caller should ``reset_connection``) a connection
        bound to the current event loop. Mirrors the startup check that
        previously called ``_get_connection`` directly.

        Startup is a user-driven action, so this may launch iTerm2.
        """
        await self._get_connection(allow_launch=True)

    def reset_connection(self) -> None:
        """Drop the cached connection so the next call reconnects fresh."""
        self._invalidate_connection()

    @_bounded(fallback=False)
    async def ensure_running(self) -> bool:
        """Ensure iTerm2 is up, auto-launching it; report reachability.

        The one entry point that deliberately resurrects iTerm2. Call
        it from user-driven paths only (sending a message, creating a
        session) — never from background polling, which must not undo
        a macOS shutdown by relaunching the app.
        """
        try:
            await self._get_app(allow_launch=True)
            return True
        except ConnectionError:
            return False

    # ------------------------------------------------------------------
    # Read-only discovery
    # ------------------------------------------------------------------

    @_bounded(fallback=[])
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

    @_bounded(fallback=None)
    async def find_window_by_name(self, window_name: str) -> ITermWindow | None:
        """Find a ccbot-owned session by its display name."""
        for w in await self.list_windows():
            if w.window_name == window_name:
                return w
        logger.debug("Window not found by name: %s", window_name)
        return None

    @_bounded(fallback=[])
    async def list_all_sessions(
        self, claude_session_uuids: set[str] | None = None
    ) -> list[ITermWindow]:
        """List **all** iTerm2 sessions, including ones the bot doesn't
        own.  Powers the "bind existing tab" picker.

        Each returned ITermWindow carries:
          - ``is_ccbot``: True if the session has ``user.ccbot=1``
          - ``has_claude``: True if ``window_id`` appears in
            ``claude_session_uuids`` (the caller should pass the set
            of UUIDs that have a session_map.json entry).  If the
            argument is None, ``has_claude`` is left False — the
            caller is responsible for the lookup.

        Empty list when iTerm2 is unreachable (graceful degradation
        consistent with ``list_windows``).
        """
        try:
            app = await self._get_app()
        except ConnectionError as e:
            logger.warning("list_all_sessions: iTerm2 unreachable: %s", e)
            return []

        known = claude_session_uuids or set()
        results: list[ITermWindow] = []
        for window in app.windows:
            for tab in window.tabs:
                for session in tab.sessions:
                    info = await self._session_full_info(session, known)
                    results.append(info)
        return results

    async def _session_full_info(
        self, session: iterm2.Session, known: set[str]
    ) -> ITermWindow:
        """Build an ITermWindow with is_ccbot / has_claude populated."""
        try:
            tag = await session.async_get_variable(_CCBOT_TAG_NAME)
        except Exception:
            tag = None
        is_ccbot = str(tag) == _CCBOT_TAG_VALUE

        name = await self._get_var(session, "session.name") or ""
        cwd = await self._get_var(session, "session.path") or ""
        job = await self._get_var(session, "session.jobName") or ""

        return ITermWindow(
            window_id=session.session_id,
            window_name=name,
            cwd=cwd,
            pane_current_command=job,
            is_ccbot=is_ccbot,
            has_claude=session.session_id in known,
        )

    @_bounded(fallback=False)
    async def bind_existing_session(self, window_id: str, name: str) -> bool:
        """Adopt an existing iTerm2 session into ccbot's pool.

        Tags the session with ``user.ccbot=1`` and sets its display
        name.  After this call, the session is visible to
        ``list_windows`` / ``find_window_by_id`` and the rest of the
        ccbot pipeline can drive it.

        Returns True on success, False if the session is gone.
        """
        try:
            app = await self._get_app()
        except ConnectionError as e:
            logger.warning("bind_existing_session: iTerm2 unreachable: %s", e)
            return False

        session = app.get_session_by_id(window_id)
        if session is None:
            logger.debug("bind_existing_session: session not found: %s", window_id)
            return False

        try:
            await session.async_set_variable(_CCBOT_TAG_NAME, _CCBOT_TAG_VALUE)
            await session.async_set_name(name)
        except Exception as e:
            logger.error("Failed to tag/name session %s: %s", window_id, e)
            return False

        logger.info("Bound existing iTerm2 session %s as '%s'", window_id, name)
        return True

    @_bounded(fallback=None)
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

    @_bounded(fallback=None)
    async def screenshot_session(self, window_id: str) -> bytes | None:
        """Capture a real pixel screenshot of the ccbot session's tab.

        Pipeline:
          1. ``osascript`` finds the iTerm2 window/tab whose session has
             ``unique ID == window_id``, brings that tab to the front of
             its window (no ``activate`` — global app focus stays put),
             and returns the window's CGWindowID.
          2. Brief sleep lets iTerm2 finish redrawing the now-frontmost
             tab into its offscreen window buffer.
          3. ``screencapture -l <CGWindowID>`` reads that buffer.  This
             mode is robust across multi-display and Retina setups with
             no coordinate math.

        Returns PNG bytes on success.  Returns None if:
          - the session is gone (osascript returns NOT_FOUND)
          - macOS Screen Recording permission isn't granted to the
            ccbot Python binary (one-time grant in System Settings →
            Privacy & Security → Screen Recording).  LaunchAgent-spawned
            processes never trigger the TCC prompt; you must add the
            binary manually and reload the agent.
        """
        cgwindowid = await self._get_iterm2_cgwindowid(window_id)
        if cgwindowid is None:
            return None

        await asyncio.sleep(_SCREENSHOT_REDRAW_DELAY)

        out_path = Path(tempfile.mkstemp(prefix="ccbot-shot-", suffix=".png")[1])
        try:
            proc = await asyncio.create_subprocess_exec(
                _SCREENCAPTURE_BIN,
                "-x",  # silent (no shutter sound)
                "-o",  # exclude window shadow
                "-l",
                str(cgwindowid),
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

    async def _get_iterm2_cgwindowid(self, session_uuid: str) -> int | None:
        """Resolve a session UUID to its iTerm2 window's CGWindowID via
        AppleScript, also bringing that tab to the front of its window.

        Returns None if osascript can't be run, the iTerm2 lookup fails,
        or no session matches the UUID.
        """
        # session UUIDs are hex + dashes, safe to interpolate; reject any
        # other shape just in case the caller hands us garbage.
        if not all(c.isalnum() or c == "-" for c in session_uuid):
            logger.error("invalid session UUID for screenshot: %r", session_uuid)
            return None

        script = (
            'tell application "iTerm2"\n'
            "  repeat with w in windows\n"
            "    repeat with t in tabs of w\n"
            "      repeat with s in sessions of t\n"
            f'        if unique ID of s is "{session_uuid}" then\n'
            "          tell w to select t\n"
            "          return id of w as string\n"
            "        end if\n"
            "      end repeat\n"
            "    end repeat\n"
            "  end repeat\n"
            '  return "NOT_FOUND"\n'
            "end tell\n"
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                _OSASCRIPT_BIN,
                "-e",
                script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.error("osascript not found at %s", _OSASCRIPT_BIN)
            return None

        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(
                "osascript failed (rc=%s): %s",
                proc.returncode,
                stderr.decode("utf-8", errors="replace").strip(),
            )
            return None

        out = stdout.decode("utf-8", errors="replace").strip()
        if out == "NOT_FOUND" or not out:
            logger.debug(
                "screenshot_session: no iTerm2 session matches %s", session_uuid
            )
            return None
        try:
            return int(out)
        except ValueError:
            logger.error("osascript returned non-numeric window id: %r", out)
            return None

    @_bounded(fallback=None)
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

    @_bounded(fallback=False)
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

    @_bounded(fallback=False)
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

    @_bounded(fallback=False)
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

    @_bounded(fallback=(False, "iTerm2 call timed out", "", ""))
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
            # User asked for a new session: launching iTerm2 is in scope.
            app = await self._get_app(allow_launch=True)
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


# Register under the "iterm2" backend name. The factory returns the
# module singleton so every consumer (and the test suite, which imports
# ``iterm2_manager`` directly) shares one instance and its connection.
register("iterm2")(lambda: iterm2_manager)

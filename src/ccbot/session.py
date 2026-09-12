"""Claude Code session management — the core state hub.

Manages the key mappings:
  Window→Session (window_states): which Claude session_id a window holds (keyed by window_id).
  User→Thread→Window (thread_bindings): topic-to-window bindings (1 topic = 1 window_id).

Responsibilities:
  - Persist/load state to ~/.ccbot/state.json.
  - Sync window↔session bindings from session_map.json (written by hook).
  - Resolve window IDs to ClaudeSession objects (JSONL file reading).
  - Track per-user read offsets for unread-message detection.
  - Manage thread↔window bindings for Telegram topic routing.
  - Send keystrokes to iTerm2 tabs and retrieve message history.
  - Maintain window_id→display name mapping for UI display.
  - Re-resolve stale window IDs on startup (iTerm2 restart recovery).

Key class: SessionManager (singleton instantiated as `session_manager`).
Key methods for thread binding access:
  - resolve_window_for_thread: Get window_id for a user's thread
  - iter_thread_bindings: Generator for iterating all (user_id, thread_id, window_id)
  - find_users_for_session: Find all users bound to a session_id
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Iterator
from typing import Any

import aiofiles

from .config import config
from .terminal.base import is_running_claude
from .terminal.manager import terminal_manager
from .transcript_parser import TranscriptParser
from .utils import atomic_write_json

logger = logging.getLogger(__name__)

# Match iTerm2 session UUIDs (case-insensitive: iTerm2 emits them in upper).
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# session_map.json key prefix for the active terminal backend (e.g.
# ``iterm:`` for iTerm2, ``otty:`` for Otty). Derived from the selected
# backend so the hook and the bot agree on the key. Legacy/foreign-prefix
# entries are filtered out at read time.
_SESSION_MAP_PREFIX = terminal_manager.session_map_prefix


@dataclass
class WindowState:
    """Persistent state for one iTerm2 tab (one Claude Code session).

    Attributes:
        session_id: Associated Claude session ID (empty if not yet detected)
        cwd: Working directory for direct file path construction
        window_name: Display name of the window
    """

    session_id: str = ""
    cwd: str = ""
    window_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "session_id": self.session_id,
            "cwd": self.cwd,
        }
        if self.window_name:
            d["window_name"] = self.window_name
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WindowState":
        return cls(
            session_id=data.get("session_id", ""),
            cwd=data.get("cwd", ""),
            window_name=data.get("window_name", ""),
        )


@dataclass
class ClaudeSession:
    """Information about a Claude Code session."""

    session_id: str
    summary: str
    message_count: int
    file_path: str


@dataclass
class SessionManager:
    """Manages session state for Claude Code.

    All internal keys use ``window_id`` (an iTerm2 session UUID) for
    uniqueness.  Display names (window_name) are stored separately for
    UI presentation.

    window_states: window_id -> WindowState (session_id, cwd, window_name)
    user_window_offsets: user_id -> {window_id -> byte_offset}
    thread_bindings: user_id -> {thread_id -> window_id}
    window_display_names: window_id -> window_name (for display)
    group_chat_ids: "user_id:thread_id" -> group chat_id (for supergroup routing)
    """

    window_states: dict[str, WindowState] = field(default_factory=dict)
    user_window_offsets: dict[int, dict[str, int]] = field(default_factory=dict)
    thread_bindings: dict[int, dict[int, str]] = field(default_factory=dict)
    # user_id -> {thread_id -> desired tab display name}.  Durable intent that
    # OUTLIVES the volatile thread_bindings: when iTerm2/the machine restarts,
    # every session UUID dies and the bindings get cleaned up, but the target
    # name persists so rebind_unresolved() can re-attach each topic to a live
    # tab of the same name.  Only cleared on explicit topic close.
    thread_targets: dict[int, dict[int, str]] = field(default_factory=dict)
    # window_id -> display name (window_name)
    window_display_names: dict[str, str] = field(default_factory=dict)
    # "user_id:thread_id" -> group chat_id (for supergroup forum topic routing)
    # IMPORTANT: This mapping is essential for supergroup/forum topic support.
    # Telegram Bot API requires group chat_id (negative number like -100xxx)
    # as the chat_id parameter when sending messages to forum topics.
    # Using user_id as chat_id will fail with "Message thread not found".
    # See: https://core.telegram.org/bots/api#sendmessage
    # History: originally added in 5afc111, erroneously removed in 26cb81f,
    # restored in PR #23.
    group_chat_ids: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._load_state()

    def _save_state(self) -> None:
        state: dict[str, Any] = {
            "window_states": {k: v.to_dict() for k, v in self.window_states.items()},
            "user_window_offsets": {
                str(uid): offsets for uid, offsets in self.user_window_offsets.items()
            },
            "thread_bindings": {
                str(uid): {str(tid): wid for tid, wid in bindings.items()}
                for uid, bindings in self.thread_bindings.items()
            },
            "thread_targets": {
                str(uid): {str(tid): name for tid, name in targets.items()}
                for uid, targets in self.thread_targets.items()
            },
            "window_display_names": self.window_display_names,
            "group_chat_ids": self.group_chat_ids,
        }
        atomic_write_json(config.state_file, state)
        logger.debug("State saved to %s", config.state_file)

    def _is_window_id(self, key: str) -> bool:
        """Check if a key looks like a window ID we recognise.

        Accepts:
          - the active backend's session-id shape (iTerm2 UUID, Otty
            ``p_*``, ...) — current format.
          - tmux window ID like ``@0`` / ``@12`` — legacy.  Returning
            True here lets ``resolve_stale_ids`` re-key these via
            display-name lookup against live sessions on startup;
            otherwise the binding would be dropped as an unrecognised
            old-format key.
        """
        if key.startswith("@") and len(key) > 1 and key[1:].isdigit():
            return True
        return terminal_manager.is_session_id(key)

    def _load_state(self) -> None:
        """Load state synchronously during initialization.

        Detects old-format state (window_name keys without '@' prefix) and
        marks for migration on next startup re-resolution.
        """
        if config.state_file.exists():
            try:
                state = json.loads(config.state_file.read_text())
                self.window_states = {
                    k: WindowState.from_dict(v)
                    for k, v in state.get("window_states", {}).items()
                }
                self.user_window_offsets = {
                    int(uid): offsets
                    for uid, offsets in state.get("user_window_offsets", {}).items()
                }
                self.thread_bindings = {
                    int(uid): {int(tid): wid for tid, wid in bindings.items()}
                    for uid, bindings in state.get("thread_bindings", {}).items()
                }
                self.thread_targets = {
                    int(uid): {int(tid): name for tid, name in targets.items()}
                    for uid, targets in state.get("thread_targets", {}).items()
                }
                self.window_display_names = state.get("window_display_names", {})
                self.group_chat_ids = {
                    k: int(v) for k, v in state.get("group_chat_ids", {}).items()
                }

                # Detect old format: keys that don't look like window IDs
                needs_migration = False
                for k in self.window_states:
                    if not self._is_window_id(k):
                        needs_migration = True
                        break
                if not needs_migration:
                    for bindings in self.thread_bindings.values():
                        for wid in bindings.values():
                            if not self._is_window_id(wid):
                                needs_migration = True
                                break
                        if needs_migration:
                            break

                if needs_migration:
                    logger.info(
                        "Detected old-format state (window_name keys), "
                        "will re-resolve on startup"
                    )
                    pass

            except (json.JSONDecodeError, ValueError) as e:
                logger.warning("Failed to load state: %s", e)
                self.window_states = {}
                self.user_window_offsets = {}
                self.thread_bindings = {}
                self.thread_targets = {}
                self.window_display_names = {}
                self.group_chat_ids = {}
                pass

    async def resolve_stale_ids(self) -> None:
        """Re-resolve persisted window IDs against live iTerm2 tabs.

        Called on startup. Handles two cases:
        1. Old-format migration: window_name keys → window_id keys
        2. Stale IDs: window_id no longer exists but display name matches a live window

        Builds {window_name: window_id} from live windows, then remaps or drops entries.

        Every pass below is destructive: an ID absent from the live set is
        dropped. ``list_windows`` returns [] both for "no tabs" and for
        "backend unreachable", so a transient WebSocket drop at startup would
        read as "every tab was closed" and wipe all bindings plus every
        session_map entry — leaving Claude→Telegram silently dead until each
        session's SessionStart hook fires again. Refuse to act on an empty
        live set that we can't trust.
        """
        windows = await terminal_manager.list_windows()
        if not windows and not terminal_manager.is_reachable():
            logger.warning(
                "Terminal backend unreachable; skipping stale-ID re-resolution "
                "(state left intact)"
            )
            return
        if not windows and (self.window_states or any(self.thread_bindings.values())):
            # Reachable but reporting zero tabs while we still hold state.
            # Keeping stale entries is harmless (status polling prunes them
            # once the backend is confirmed healthy); wiping them is not.
            logger.warning(
                "Backend reports zero sessions but state is non-empty; "
                "skipping stale-ID re-resolution"
            )
            return
        live_by_name: dict[str, str] = {}  # window_name -> window_id
        live_ids: set[str] = set()
        for w in windows:
            live_by_name[w.window_name] = w.window_id
            live_ids.add(w.window_id)

        # Snapshot the display-name map BEFORE any of the migration
        # passes mutate it.  The window_states pass below removes
        # entries for old IDs as it re-keys them, but the
        # thread_bindings and offsets passes still need to look up
        # the display name for those same old IDs.
        display_snapshot = dict(self.window_display_names)

        changed = False

        # --- Migrate window_states ---
        new_window_states: dict[str, WindowState] = {}
        for key, ws in self.window_states.items():
            if self._is_window_id(key):
                if key in live_ids:
                    new_window_states[key] = ws
                else:
                    # Stale ID — try re-resolve by display name
                    display = self.window_display_names.get(key, ws.window_name or key)
                    new_id = live_by_name.get(display)
                    if new_id:
                        logger.info(
                            "Re-resolved stale window_id %s -> %s (name=%s)",
                            key,
                            new_id,
                            display,
                        )
                        new_window_states[new_id] = ws
                        ws.window_name = display
                        self.window_display_names[new_id] = display
                        self.window_display_names.pop(key, None)
                        changed = True
                    else:
                        logger.info(
                            "Dropping stale window_state: %s (name=%s)", key, display
                        )
                        changed = True
            else:
                # Old format: key is window_name
                new_id = live_by_name.get(key)
                if new_id:
                    logger.info("Migrating window_state key %s -> %s", key, new_id)
                    ws.window_name = key
                    new_window_states[new_id] = ws
                    self.window_display_names[new_id] = key
                    changed = True
                else:
                    logger.info(
                        "Dropping old-format window_state: %s (no live window)", key
                    )
                    changed = True
        self.window_states = new_window_states

        # --- Migrate thread_bindings ---
        for uid, bindings in self.thread_bindings.items():
            new_bindings: dict[int, str] = {}
            for tid, val in bindings.items():
                if self._is_window_id(val):
                    if val in live_ids:
                        new_bindings[tid] = val
                    else:
                        display = display_snapshot.get(val, val)
                        new_id = live_by_name.get(display)
                        if new_id:
                            logger.info(
                                "Re-resolved thread binding %s -> %s (name=%s)",
                                val,
                                new_id,
                                display,
                            )
                            new_bindings[tid] = new_id
                            self.window_display_names[new_id] = display
                            changed = True
                        else:
                            logger.info(
                                "Dropping stale thread binding: user=%d, thread=%d, wid=%s",
                                uid,
                                tid,
                                val,
                            )
                            changed = True
                else:
                    # Old format: val is window_name
                    new_id = live_by_name.get(val)
                    if new_id:
                        logger.info("Migrating thread binding %s -> %s", val, new_id)
                        new_bindings[tid] = new_id
                        self.window_display_names[new_id] = val
                        changed = True
                    else:
                        logger.info(
                            "Dropping old-format thread binding: user=%d, thread=%d, name=%s",
                            uid,
                            tid,
                            val,
                        )
                        changed = True
            self.thread_bindings[uid] = new_bindings

        # Remove empty user entries
        empty_users = [uid for uid, b in self.thread_bindings.items() if not b]
        for uid in empty_users:
            del self.thread_bindings[uid]

        # --- Migrate user_window_offsets ---
        for uid, offsets in self.user_window_offsets.items():
            new_offsets: dict[str, int] = {}
            for key, offset in offsets.items():
                if self._is_window_id(key):
                    if key in live_ids:
                        new_offsets[key] = offset
                    else:
                        display = display_snapshot.get(key, key)
                        new_id = live_by_name.get(display)
                        if new_id:
                            new_offsets[new_id] = offset
                            changed = True
                        else:
                            changed = True
                else:
                    new_id = live_by_name.get(key)
                    if new_id:
                        new_offsets[new_id] = offset
                        changed = True
                    else:
                        changed = True
            self.user_window_offsets[uid] = new_offsets

        if changed:
            self._save_state()
            logger.info("Startup re-resolution complete")

        # Clean up session_map.json: stale window IDs and old-format keys.
        # The hook writes session_map for any iTerm2 tab running Claude,
        # tagged-as-ccbot or not (e.g. before the user binds a tab via the
        # picker). live_ids above only includes ccbot-tagged sessions, so
        # passing it here would wrongly drop entries for untagged tabs that
        # are alive — the next time the user binds that tab the cached
        # session_id is gone, the picker thinks Claude isn't running, and
        # `claude` gets typed into a tab that already has Claude open.
        # Use the unfiltered live set instead.
        all_live = await terminal_manager.list_all_sessions()
        if not all_live:
            # Same ambiguity as list_windows above: [] means "unreachable" as
            # often as "no sessions". Purging session_map on a bad read kills
            # the monitor's watch list for sessions that are very much alive.
            logger.warning("Backend returned no sessions; skipping session_map cleanup")
            return
        all_live_ids = {w.window_id for w in all_live}
        await self._cleanup_stale_session_map_entries(all_live_ids)
        await self._cleanup_old_format_session_map_keys()

    async def _cleanup_old_format_session_map_keys(self) -> None:
        """Remove old-format keys (window_name instead of @window_id) from session_map.json."""
        if not config.session_map_file.exists():
            return
        try:
            async with aiofiles.open(config.session_map_file, "r") as f:
                content = await f.read()
            session_map = json.loads(content)
        except (json.JSONDecodeError, OSError):
            return

        prefix = _SESSION_MAP_PREFIX
        old_keys = [
            key
            for key in session_map
            if key.startswith(prefix) and not self._is_window_id(key[len(prefix) :])
        ]
        if not old_keys:
            return

        for key in old_keys:
            del session_map[key]
        atomic_write_json(config.session_map_file, session_map)
        logger.info(
            "Cleaned up %d old-format session_map keys: %s", len(old_keys), old_keys
        )

    async def _cleanup_stale_session_map_entries(self, live_ids: set[str]) -> None:
        """Remove entries for iTerm2 tabs that no longer exist.

        When windows are closed externally (outside ccbot), session_map.json
        retains orphan references. This cleanup removes entries whose window_id
        is not in the current set of live iTerm2 tabs.
        """
        if not config.session_map_file.exists():
            return
        try:
            async with aiofiles.open(config.session_map_file, "r") as f:
                content = await f.read()
            session_map = json.loads(content)
        except (json.JSONDecodeError, OSError):
            return

        prefix = _SESSION_MAP_PREFIX
        stale_keys = [
            key
            for key in session_map
            if key.startswith(prefix)
            and self._is_window_id(key[len(prefix) :])
            and key[len(prefix) :] not in live_ids
        ]
        if not stale_keys:
            return

        for key in stale_keys:
            del session_map[key]
            logger.info("Removed stale session_map entry: %s", key)

        atomic_write_json(config.session_map_file, session_map)
        logger.info(
            "Cleaned up %d stale session_map entries (sessions no longer in iTerm2)",
            len(stale_keys),
        )

    async def override_session_map_entry(
        self, window_id: str, session_id: str, cwd: str = "", window_name: str = ""
    ) -> None:
        """Force a window's session_map entry to a specific session_id.

        Used after `--resume`: session_map drives both the monitor's watch
        list and load_session_map()'s sync into window_states, so overriding
        window_state alone would be reverted on the next poll cycle. Creates
        the entry if missing (hook timed out); no-op if already consistent.
        """
        key = f"{_SESSION_MAP_PREFIX}{window_id}"
        session_map: dict = {}
        if config.session_map_file.exists():
            try:
                async with aiofiles.open(config.session_map_file, "r") as f:
                    content = await f.read()
                session_map = json.loads(content)
            except (json.JSONDecodeError, OSError):
                session_map = {}

        info = session_map.get(key)
        if info is not None and info.get("session_id") == session_id:
            return  # already consistent
        if info is None:
            session_map[key] = {
                "session_id": session_id,
                "cwd": cwd,
                "window_name": window_name,
            }
        else:
            info["session_id"] = session_id

        atomic_write_json(config.session_map_file, session_map)
        logger.info("session_map override: %s -> session_id=%s", key, session_id)

    # --- Display name management ---

    def get_display_name(self, window_id: str) -> str:
        """Get display name for a window_id, fallback to window_id itself."""
        return self.window_display_names.get(window_id, window_id)

    def update_display_name(self, window_id: str, new_name: str) -> None:
        """Update the display name for a window and persist state."""
        self.window_display_names[window_id] = new_name
        # Also update WindowState.window_name if it exists
        if window_id in self.window_states:
            self.window_states[window_id].window_name = new_name
        self._save_state()
        logger.info("Updated display name: window_id %s -> '%s'", window_id, new_name)

    # --- Group chat ID management (supergroup forum topic routing) ---

    def set_group_chat_id(
        self, user_id: int, thread_id: int | None, chat_id: int
    ) -> None:
        """Store the group chat_id for a user+thread combination.

        In supergroups with forum topics, messages must be sent to the group's
        chat_id (negative number like -100xxx) rather than the user's personal ID.
        Telegram's Bot API rejects message_thread_id when chat_id is a private
        user ID — the thread only exists within the group context.

        DO NOT REMOVE this method or the group_chat_ids mapping.
        Without it, all outbound messages in forum topics fail with
        "Message thread not found". See commit history: 5afc111 → 26cb81f → PR #23.
        """
        tid = thread_id or 0
        key = f"{user_id}:{tid}"
        if self.group_chat_ids.get(key) != chat_id:
            self.group_chat_ids[key] = chat_id
            self._save_state()
            logger.debug(
                "Stored group chat_id: user=%d, thread=%s, chat_id=%d",
                user_id,
                thread_id,
                chat_id,
            )

    def resolve_chat_id(self, user_id: int, thread_id: int | None = None) -> int:
        """Resolve the correct chat_id for sending messages.

        Returns the stored group chat_id when a thread_id is present and a
        mapping exists, otherwise falls back to user_id (for private chats).

        Every outbound Telegram API call (send_message, edit_message_text,
        delete_message, send_chat_action, edit_forum_topic, etc.) MUST use
        this method instead of raw user_id. Using user_id directly breaks
        supergroup forum topic routing.
        """
        if thread_id is not None:
            key = f"{user_id}:{thread_id}"
            group_id = self.group_chat_ids.get(key)
            if group_id is not None:
                return group_id
        return user_id

    async def wait_for_session_map_entry(
        self, window_id: str, timeout: float = 5.0, interval: float = 0.5
    ) -> bool:
        """Poll session_map.json until an entry for window_id appears.

        Returns True if the entry was found within timeout, False otherwise.
        """
        logger.debug(
            "Waiting for session_map entry: window_id=%s, timeout=%.1f",
            window_id,
            timeout,
        )
        key = f"{_SESSION_MAP_PREFIX}{window_id}"
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                if config.session_map_file.exists():
                    async with aiofiles.open(config.session_map_file, "r") as f:
                        content = await f.read()
                    session_map = json.loads(content)
                    info = session_map.get(key, {})
                    if info.get("session_id"):
                        # Found — load into window_states immediately
                        logger.debug(
                            "session_map entry found for window_id %s", window_id
                        )
                        await self.load_session_map()
                        return True
            except (json.JSONDecodeError, OSError):
                pass
            await asyncio.sleep(interval)
        logger.warning(
            "Timed out waiting for session_map entry: window_id=%s", window_id
        )
        return False

    async def load_session_map(self) -> None:
        """Read session_map.json and update window_states with new session associations.

        Keys in session_map are formatted as ``iterm:<UUID>``.
        Only entries with the iTerm2 prefix are processed; legacy ``ccbot:`` keys are silently ignored.
        Also cleans up window_states entries not in current session_map.
        Updates window_display_names from the "window_name" field in values.
        """
        if not config.session_map_file.exists():
            return
        try:
            async with aiofiles.open(config.session_map_file, "r") as f:
                content = await f.read()
            session_map = json.loads(content)
        except (json.JSONDecodeError, OSError):
            return

        prefix = _SESSION_MAP_PREFIX
        valid_wids: set[str] = set()
        changed = False

        for key, info in session_map.items():
            # Only process entries for our iTerm2 session
            if not key.startswith(prefix):
                continue
            window_id = key[len(prefix) :]
            if not self._is_window_id(window_id):
                continue
            valid_wids.add(window_id)
            new_sid = info.get("session_id", "")
            new_cwd = info.get("cwd", "")
            new_wname = info.get("window_name", "")
            if not new_sid:
                continue
            state = self.get_window_state(window_id)
            if state.session_id != new_sid or state.cwd != new_cwd:
                logger.info(
                    "Session map: window_id %s updated sid=%s, cwd=%s",
                    window_id,
                    new_sid,
                    new_cwd,
                )
                state.session_id = new_sid
                state.cwd = new_cwd
                changed = True
            # Update display name
            if new_wname:
                state.window_name = new_wname
                if self.window_display_names.get(window_id) != new_wname:
                    self.window_display_names[window_id] = new_wname
                    changed = True

        # Clean up window_states entries not in current session_map.
        stale_wids = [w for w in self.window_states if w and w not in valid_wids]
        for wid in stale_wids:
            logger.info("Removing stale window_state: %s", wid)
            del self.window_states[wid]
            changed = True

        if changed:
            self._save_state()

    # --- Window state management ---

    def get_window_state(self, window_id: str) -> WindowState:
        """Get or create window state."""
        if window_id not in self.window_states:
            self.window_states[window_id] = WindowState()
        return self.window_states[window_id]

    def clear_window_session(self, window_id: str) -> None:
        """Clear session association for a window (e.g., after /clear command)."""
        state = self.get_window_state(window_id)
        state.session_id = ""
        self._save_state()
        logger.info("Cleared session for window_id %s", window_id)

    @staticmethod
    def _encode_cwd(cwd: str) -> str:
        """Encode a cwd path to match Claude Code's project directory naming.

        Replaces all non-alphanumeric characters (except dash) with dashes.
        E.g. /home/user_name/Code/project -> -home-user-name-Code-project
        """
        return re.sub(r"[^a-zA-Z0-9-]", "-", cwd)

    def _build_session_file_path(self, session_id: str, cwd: str) -> Path | None:
        """Build the direct file path for a session from session_id and cwd."""
        if not session_id or not cwd:
            return None
        encoded_cwd = self._encode_cwd(cwd)
        return config.claude_projects_path / encoded_cwd / f"{session_id}.jsonl"

    async def _get_session_direct(
        self, session_id: str, cwd: str
    ) -> ClaudeSession | None:
        """Get a ClaudeSession directly from session_id and cwd (no scanning)."""
        file_path = self._build_session_file_path(session_id, cwd)

        # Fallback: glob search if direct path doesn't exist
        if not file_path or not file_path.exists():
            pattern = f"*/{session_id}.jsonl"
            matches = list(config.claude_projects_path.glob(pattern))
            if matches:
                file_path = matches[0]
                logger.debug("Found session via glob: %s", file_path)
            else:
                return None

        # Single pass: read file once, extract summary + count messages
        summary = ""
        last_user_msg = ""
        message_count = 0
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                async for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    message_count += 1
                    try:
                        data = json.loads(line)
                        # Check for summary
                        if data.get("type") == "summary":
                            s = data.get("summary", "")
                            if s:
                                summary = s
                        # Track last user message as fallback
                        elif TranscriptParser.is_user_message(data):
                            parsed = TranscriptParser.parse_message(data)
                            if parsed and parsed.text.strip():
                                last_user_msg = parsed.text.strip()
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return None

        if not summary:
            summary = last_user_msg[:50] if last_user_msg else "Untitled"

        return ClaudeSession(
            session_id=session_id,
            summary=summary,
            message_count=message_count,
            file_path=str(file_path),
        )

    # --- Directory session listing ---

    async def list_sessions_for_directory(self, cwd: str) -> list[ClaudeSession]:
        """List existing Claude sessions for a directory.

        Encodes the cwd path to find the project directory under
        ~/.claude/projects/{encoded_cwd}/, globs *.jsonl files, and
        extracts summary info from each.

        Returns a list sorted by mtime (most recent first), capped at 10.
        """
        encoded_cwd = self._encode_cwd(cwd)
        project_dir = config.claude_projects_path / encoded_cwd
        if not project_dir.is_dir():
            return []

        # Collect JSONL files sorted by mtime (newest first)
        jsonl_files = sorted(
            project_dir.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        # Skip sessions-index and cap at 10
        sessions: list[ClaudeSession] = []
        for f in jsonl_files:
            if f.stem == "sessions-index":
                continue
            if len(sessions) >= 10:
                break
            session_id = f.stem
            session = await self._get_session_direct(session_id, cwd)
            if session and session.message_count > 0:
                sessions.append(session)
        return sessions

    # --- Window → Session resolution ---

    async def resolve_session_for_window(self, window_id: str) -> ClaudeSession | None:
        """Resolve a iTerm2 tab to the best matching Claude session.

        Uses persisted session_id + cwd to construct file path directly.
        Returns None if no session is associated with this window.
        """
        state = self.get_window_state(window_id)

        if not state.session_id or not state.cwd:
            return None

        session = await self._get_session_direct(state.session_id, state.cwd)
        if session:
            return session

        # JSONL file doesn't exist for this session_id.  This is normal
        # after --resume (hook reports new id, Claude keeps old JSONL).
        # Log at debug, do NOT clear state — find_users_for_session uses
        # the session_map directly and doesn't depend on the file.
        logger.debug(
            "Session file not found for window_id %s (sid=%s, cwd=%s)",
            window_id,
            state.session_id,
            state.cwd,
        )
        return None

    # --- User window offset management ---

    def update_user_window_offset(
        self, user_id: int, window_id: str, offset: int
    ) -> None:
        """Update the user's last read offset for a window."""
        if user_id not in self.user_window_offsets:
            self.user_window_offsets[user_id] = {}
        self.user_window_offsets[user_id][window_id] = offset
        self._save_state()

    # --- Thread binding management ---

    def bind_thread(
        self,
        user_id: int,
        thread_id: int,
        window_id: str,
        window_name: str = "",
        cwd: str | None = None,
    ) -> None:
        """Bind a Telegram topic thread to a iTerm2 tab.

        Args:
            user_id: Telegram user ID
            thread_id: Telegram topic thread ID
            window_id: iTerm2 session UUID
            window_name: Display name for the window (optional)
            cwd: Working directory of the tab.  Recorded as the topic's durable
                rebind target; falls back to the window's known WindowState cwd.
        """
        if user_id not in self.thread_bindings:
            self.thread_bindings[user_id] = {}
        self.thread_bindings[user_id][thread_id] = window_id
        if window_name:
            self.window_display_names[window_id] = window_name
        display = window_name or self.get_display_name(window_id)
        # Record the durable target by CWD — stable across reboots, unlike the
        # session UUID (regenerated) and the iTerm2 tab name (case/format may
        # differ from our stored name).  rebind_unresolved() matches on it.
        target_cwd = cwd
        if not target_cwd:
            ws = self.window_states.get(window_id)
            if ws and ws.cwd:
                target_cwd = ws.cwd
        if target_cwd:
            self.thread_targets.setdefault(user_id, {})[thread_id] = target_cwd
        self._save_state()
        logger.info(
            "Bound thread %d -> window_id %s (%s) for user %d",
            thread_id,
            window_id,
            display,
            user_id,
        )

    def unbind_thread(self, user_id: int, thread_id: int) -> str | None:
        """Remove a thread binding. Returns the previously bound window_id, or None."""
        bindings = self.thread_bindings.get(user_id)
        if not bindings or thread_id not in bindings:
            return None
        window_id = bindings.pop(thread_id)
        if not bindings:
            del self.thread_bindings[user_id]
        self._save_state()
        logger.info(
            "Unbound thread %d (was %s) for user %d",
            thread_id,
            window_id,
            user_id,
        )
        return window_id

    def clear_thread_target(self, user_id: int, thread_id: int) -> None:
        """Forget a topic's durable rebind target (on explicit topic close).

        Unlike unbind_thread, this stops the topic from auto-rebinding to a
        same-named tab on the next restart — used when the user closes/deletes
        the Telegram topic, not when a tab merely went stale.
        """
        targets = self.thread_targets.get(user_id)
        if not targets or thread_id not in targets:
            return
        targets.pop(thread_id, None)
        if not targets:
            del self.thread_targets[user_id]
        self._save_state()

    def refresh_thread_targets(self) -> None:
        """Backfill cwd targets for currently-bound threads from window_states.

        bind_thread records the cwd target when it's known, but on the create
        flow the cwd may not be in window_states yet (the SessionStart hook is
        async).  Calling this each healthy poll cycle guarantees every bound
        topic ends up with a durable cwd target so a later reboot can rebind it.
        """
        changed = False
        for uid, bindings in self.thread_bindings.items():
            for tid, wid in bindings.items():
                ws = self.window_states.get(wid)
                if not ws or not ws.cwd:
                    continue
                if self.thread_targets.get(uid, {}).get(tid) != ws.cwd:
                    self.thread_targets.setdefault(uid, {})[tid] = ws.cwd
                    changed = True
        if changed:
            self._save_state()

    async def rebind_unresolved(self) -> int:
        """Re-bind topics to live tabs by their durable cwd target.

        After an iTerm2/device restart every session UUID dies AND the
        ``user.ccbot=1`` tag is lost (iTerm2 user variables don't persist), so
        the restored tabs are untagged and invisible to list_windows.  We list
        ALL sessions (tagged or not), match each unresolved topic to a session
        whose cwd equals its target — only when EXACTLY ONE candidate exists, to
        avoid grabbing the wrong tab — re-tag it (adopt) and bind.  Returns the
        number of topics rebound.

        The cwd compared is the hook's, not the terminal's.  iTerm2 reports
        ``session.path`` from shell integration, which only fires when a
        prompt is drawn: a tab started with ``cd <dir> && claude`` — the form
        ccbot itself uses — never draws another prompt, so the terminal keeps
        reporting the directory the user was in *before* pressing return
        (usually ``~``) for the whole life of the session.  Measured on a live
        instance, 5 of 7 tabs reported ``/Users/paris``, so matching on it
        silently rebound nothing.  The SessionStart hook records the directory
        Claude itself reported, which matched every running process's real cwd.
        ``session.cwd`` stays the fallback for sessions with no hook entry.
        """

        # Cheap pre-check (no network): a targeted topic needs rebinding if it
        # has NO binding, OR its binding points at a window_id we no longer know
        # to be live.  After a bare iTerm2 restart the old UUIDs are dead but the
        # bindings still reference them (non-None) — keying off None alone missed
        # that and left topics permanently unbound.  window_states is reconciled
        # against the live session_map (load_session_map), so "wid not in
        # window_states" is a sound stale signal.  Over-firing is harmless: the
        # authoritative live_ids check below still prevents wrong rebinds.
        def _binding_is_live(uid: int, tid: int) -> bool:
            wid = self.get_window_for_thread(uid, tid)
            return wid is not None and wid in self.window_states

        has_unresolved = any(
            not _binding_is_live(uid, tid)
            for uid, targets in self.thread_targets.items()
            for tid in targets
        )
        if not has_unresolved:
            return 0

        sessions = await terminal_manager.list_all_sessions()
        hook_cwds = self.load_session_map_cwds()
        live_ids = {s.window_id for s in sessions}
        bound = {wid for _, _, wid in self.iter_thread_bindings()}
        claimed: set[str] = set()
        count = 0
        for uid, targets in self.thread_targets.items():
            for tid, target_cwd in targets.items():
                current = self.get_window_for_thread(uid, tid)
                if current is not None and current in live_ids:
                    continue  # already resolved to a live session
                candidates = [
                    s
                    for s in sessions
                    if (hook_cwds.get(s.window_id) or s.cwd) == target_cwd
                    and s.window_id not in bound
                    and s.window_id not in claimed
                    and is_running_claude(s)
                ]
                if len(candidates) != 1:
                    continue  # 0 = not open yet; >1 = ambiguous, wait it out
                sess = candidates[0]
                name = sess.window_name or Path(target_cwd).name
                via = "hook" if hook_cwds.get(sess.window_id) else "terminal"
                # Re-tag untagged (reboot-orphaned) tabs so the rest of the
                # pipeline can drive them again; already-tagged ones skip this.
                if not sess.is_ccbot:
                    if not await terminal_manager.bind_existing_session(
                        sess.window_id, name
                    ):
                        continue
                self.bind_thread(
                    uid, tid, sess.window_id, window_name=name, cwd=target_cwd
                )
                claimed.add(sess.window_id)
                bound.add(sess.window_id)
                count += 1
                logger.info(
                    "Auto-rebound topic by cwd: user=%d thread=%d -> %s "
                    "(cwd=%s via %s)",
                    uid,
                    tid,
                    sess.window_id,
                    target_cwd,
                    via,
                )
        return count

    def get_window_for_thread(self, user_id: int, thread_id: int) -> str | None:
        """Look up the window_id bound to a thread."""
        bindings = self.thread_bindings.get(user_id)
        if not bindings:
            return None
        return bindings.get(thread_id)

    def resolve_window_for_thread(
        self,
        user_id: int,
        thread_id: int | None,
    ) -> str | None:
        """Resolve the iTerm2 tab_id for a user's thread.

        Returns None if thread_id is None or the thread is not bound.
        """
        if thread_id is None:
            return None
        return self.get_window_for_thread(user_id, thread_id)

    def iter_thread_bindings(self) -> Iterator[tuple[int, int, str]]:
        """Iterate all thread bindings as (user_id, thread_id, window_id).

        Provides encapsulated access to thread_bindings without exposing
        the internal data structure directly.
        """
        for user_id, bindings in self.thread_bindings.items():
            for thread_id, window_id in bindings.items():
                yield user_id, thread_id, window_id

    async def find_users_for_session(
        self,
        session_id: str,
    ) -> list[tuple[int, str, int]]:
        """Find all users whose thread-bound window maps to the given session_id.

        Matches via the session_map (window_id → session_id) rather than
        validating the JSONL file on disk, because ``--resume`` can change
        the session_id while Claude keeps writing to the original JSONL.

        Returns list of (user_id, window_id, thread_id) tuples.
        """
        # Build window_id → session_id from session_map
        window_to_session = self._load_session_map_by_window()

        result: list[tuple[int, str, int]] = []
        for user_id, thread_id, window_id in self.iter_thread_bindings():
            if window_to_session.get(window_id) == session_id:
                result.append((user_id, window_id, thread_id))
        return result

    def _load_session_map_by_window(self) -> dict[str, str]:
        """Return {window_id: session_id} from session_map.json."""
        try:
            data = json.loads(config.session_map_file.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
        prefix = _SESSION_MAP_PREFIX
        result: dict[str, str] = {}
        for key, info in data.items():
            if key.startswith(prefix):
                wid = key[len(prefix) :]
                result[wid] = info.get("session_id", "")
        return result

    def load_session_map_cwds(self) -> dict[str, str]:
        """Return {window_id: cwd} from session_map.json.

        The hook records Claude's real working directory; iTerm2's own
        ``session.path`` only tracks ``cd`` when shell integration is
        installed and otherwise reports the profile's start directory
        (typically ``~``), so the hook value is the authoritative one.
        """
        try:
            data = json.loads(config.session_map_file.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
        prefix = _SESSION_MAP_PREFIX
        result: dict[str, str] = {}
        for key, info in data.items():
            if key.startswith(prefix) and isinstance(info, dict):
                cwd = info.get("cwd") or ""
                if cwd:
                    result[key[len(prefix) :]] = cwd
        return result

    # --- Tmux helpers ---

    async def claim_running_claude(self, window_id: str, cwd: str) -> str | None:
        """Adopt a Claude that's already running in an iTerm2 tab.

        Used when the picker is about to bind a tab whose session_map
        entry was wiped earlier (e.g. by the pre-fix cleanup bug) but
        Claude is actually still running there. Without this, ccbot
        would type `claude` into the live Claude — which is treated as
        user input — and never recover the session_id needed by the
        monitor, so responses never reach Telegram.

        Discovers the active JSONL by scanning Claude's per-project
        transcript directory for the most recently modified file. If
        nothing fresh is found (e.g. JSONL hasn't been written yet),
        returns None and lets the caller fall back to typing `claude`.
        Returns the discovered session_id on success.
        """
        if not cwd:
            return None
        # Claude Code maps every cwd to a single dir under
        # ~/.claude/projects by replacing '/', ' ', and '~' with '-'.
        sanitized = re.sub(r"[/ ~]", "-", cwd)
        project_dir = Path.home() / ".claude" / "projects" / sanitized
        if not project_dir.is_dir():
            return None
        candidates: list[tuple[float, Path]] = []
        for jsonl in project_dir.glob("*.jsonl"):
            try:
                mtime = jsonl.stat().st_mtime
            except OSError:
                continue
            candidates.append((mtime, jsonl))
        if not candidates:
            return None
        candidates.sort(reverse=True)
        _, jsonl = candidates[0]
        # The filename stem IS the session_id Claude writes to its
        # JSONL header — same UUID the hook would have reported.
        session_id = jsonl.stem
        if not _UUID_RE.match(session_id):
            return None

        # Atomic read-modify-write through the same lock the hook uses.
        # We import fcntl lazily so this module stays import-safe on
        # non-POSIX in case the bot is ever exercised outside macOS.
        import fcntl

        map_file = config.session_map_file
        map_file.parent.mkdir(parents=True, exist_ok=True)
        lock_path = map_file.with_suffix(".lock")
        try:
            with open(lock_path, "w") as lock_f:
                fcntl.flock(lock_f, fcntl.LOCK_EX)
                try:
                    session_map: dict[str, dict[str, str]] = {}
                    if map_file.exists():
                        try:
                            session_map = json.loads(map_file.read_text())
                        except (json.JSONDecodeError, OSError):
                            pass
                    key = f"{_SESSION_MAP_PREFIX}{window_id}"
                    session_map[key] = {
                        "session_id": session_id,
                        "cwd": cwd,
                        "window_name": "",
                    }
                    atomic_write_json(map_file, session_map)
                finally:
                    fcntl.flock(lock_f, fcntl.LOCK_UN)
        except OSError as e:
            logger.error("Failed to write session_map during claim: %s", e)
            return None

        logger.info(
            "Claimed already-running Claude: window=%s session_id=%s (via JSONL discovery)",
            window_id,
            session_id,
        )
        return session_id

    async def send_to_window(self, window_id: str, text: str) -> tuple[bool, str]:
        """Send text to a iTerm2 tab by ID.

        On a UUID miss, tries a display-name lookup and migrates state in
        place — iTerm2 reassigns session UUIDs after a restart, so the
        cached one can be stale even though a tab with the same name is
        still alive. The connection-level reconnect listener normally
        beats us to this, but the fallback covers the gap between iTerm2
        coming back up and the listener firing.
        """
        # User-driven send: make sure the terminal app is up (auto-launch if
        # it was closed). Background polling stays passive and won't relaunch.
        await terminal_manager.ensure_running()
        display = self.get_display_name(window_id)
        logger.debug(
            "send_to_window: window_id=%s (%s), text_len=%d",
            window_id,
            display,
            len(text),
        )
        window = await terminal_manager.find_window_by_id(window_id)
        if window is None:
            new_id = await self._migrate_stale_window_id(window_id)
            if new_id and new_id != window_id:
                window = await terminal_manager.find_window_by_id(new_id)
                if window is not None:
                    window_id = new_id
        if window is None:
            return False, "Window not found (may have been closed)"
        success = await terminal_manager.send_keys(window.window_id, text)
        if success:
            return True, f"Sent to {display}"
        return False, "Failed to send keys"

    async def _migrate_stale_window_id(self, old_id: str) -> str | None:
        """Re-key state from a stale UUID to the live one with the same name.

        Returns the new window_id, or None if no live tab matches the
        display name we had recorded for old_id. Updates thread_bindings,
        window_display_names, window_states, and user_window_offsets
        atomically, then persists.
        """
        display = self.window_display_names.get(old_id)
        if not display:
            return None
        window = await terminal_manager.find_window_by_name(display)
        if window is None:
            return None
        new_id = window.window_id
        if new_id == old_id:
            return new_id

        for bindings in self.thread_bindings.values():
            for tid, val in list(bindings.items()):
                if val == old_id:
                    bindings[tid] = new_id

        self.window_display_names[new_id] = display
        self.window_display_names.pop(old_id, None)

        if old_id in self.window_states:
            ws = self.window_states.pop(old_id)
            ws.window_name = display
            self.window_states[new_id] = ws

        for offsets in self.user_window_offsets.values():
            if old_id in offsets:
                offsets[new_id] = offsets.pop(old_id)

        self._save_state()
        logger.info(
            "Migrated stale window_id %s -> %s (name=%s)", old_id, new_id, display
        )
        return new_id

    # --- Message history ---

    async def get_recent_messages(
        self,
        window_id: str,
        *,
        start_byte: int = 0,
        end_byte: int | None = None,
    ) -> tuple[list[dict], int]:
        """Get user/assistant messages for a window's session.

        Resolves window → session, then reads the JSONL.
        Supports byte range filtering via start_byte/end_byte.
        Returns (messages, total_count).
        """
        session = await self.resolve_session_for_window(window_id)
        if not session or not session.file_path:
            return [], 0

        file_path = Path(session.file_path)
        if not file_path.exists():
            return [], 0

        # Read JSONL entries (optionally filtered by byte range)
        entries: list[dict] = []
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                if start_byte > 0:
                    await f.seek(start_byte)

                while True:
                    # Check byte limit before reading
                    if end_byte is not None:
                        current_pos = await f.tell()
                        if current_pos >= end_byte:
                            break

                    line = await f.readline()
                    if not line:
                        break

                    data = TranscriptParser.parse_line(line)
                    if data:
                        entries.append(data)
        except OSError as e:
            logger.error("Error reading session file %s: %s", file_path, e)
            return [], 0

        parsed_entries, _ = TranscriptParser.parse_entries(entries)
        all_messages = [
            {
                "role": e.role,
                "text": e.text,
                "content_type": e.content_type,
                "timestamp": e.timestamp,
            }
            for e in parsed_entries
        ]

        return all_messages, len(all_messages)


session_manager = SessionManager()

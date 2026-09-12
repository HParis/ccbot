"""Directory browser and window picker UI for session creation.

Provides UIs in Telegram for:
  - Window picker: list unbound iTerm2 sessions (incl. user-opened
    tabs that aren't yet ccbot-tagged) for quick adoption.
  - Directory browser: navigate directory hierarchies to create new sessions
  - Workspace picker: for backends that cannot host a session in an arbitrary
    directory (``Capabilities.arbitrary_cwd=False``), pick one of the
    projects the backend already knows instead of browsing the filesystem

Key components:
  - DIRS_PER_PAGE / WINDOWS_PER_PAGE: pagination sizes
  - User state keys for tracking browse/picker session
  - build_window_picker: Build candidate-session picker UI
  - build_directory_browser: Build directory browser UI
  - build_workspace_picker: Build workspace picker UI (arbitrary_cwd=False)
  - clear_window_picker_state: Clear picker state from user_data
  - clear_browse_state: Clear browsing state from user_data
"""

import os
import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..session import ClaudeSession

from ..config import config
from ..terminal.base import Workspace
from ..terminal.manager import terminal_manager
from .callback_data import (
    CB_DIR_CANCEL,
    CB_DIR_CONFIRM,
    CB_DIR_PAGE,
    CB_DIR_SELECT,
    CB_DIR_UP,
    CB_SESSION_CANCEL,
    CB_SESSION_NEW,
    CB_SESSION_SELECT,
    CB_WIN_BIND,
    CB_WIN_CANCEL,
    CB_WIN_NEW,
    CB_WIN_PAGE,
    CB_WS_CANCEL,
    CB_WS_PAGE,
    CB_WS_SELECT,
)

# Directories per page in directory browser
DIRS_PER_PAGE = 6
# Sessions per page in tab picker
WINDOWS_PER_PAGE = 6

# User state keys
STATE_KEY = "state"
STATE_BROWSING_DIRECTORY = "browsing_directory"
STATE_SELECTING_WINDOW = "selecting_window"
BROWSE_PATH_KEY = "browse_path"
BROWSE_PAGE_KEY = "browse_page"
BROWSE_DIRS_KEY = "browse_dirs"  # Cache of subdirs for current path
UNBOUND_WINDOWS_KEY = "unbound_windows"  # Cache of window_id list (paginated)
UNBOUND_WINDOWS_FULL_KEY = (
    "unbound_windows_full"  # Full list of (wid, name, cwd, has_claude)
)
WINDOW_PAGE_KEY = "window_page"  # Current page in window picker
STATE_SELECTING_WORKSPACE = "selecting_workspace"
WORKSPACES_KEY = "workspaces"  # Cache of Workspace path list (paginated)
WORKSPACE_PAGE_KEY = "workspace_page"
STATE_SELECTING_SESSION = "selecting_session"
SESSIONS_KEY = "cached_sessions"  # Cache of ClaudeSession list


def clear_browse_state(user_data: dict | None) -> None:
    """Clear directory browsing state keys from user_data."""
    if user_data is not None:
        user_data.pop(STATE_KEY, None)
        user_data.pop(BROWSE_PATH_KEY, None)
        user_data.pop(BROWSE_PAGE_KEY, None)
        user_data.pop(BROWSE_DIRS_KEY, None)


def clear_window_picker_state(user_data: dict | None) -> None:
    """Clear window picker state keys from user_data."""
    if user_data is not None:
        user_data.pop(STATE_KEY, None)
        user_data.pop(UNBOUND_WINDOWS_KEY, None)
        user_data.pop(UNBOUND_WINDOWS_FULL_KEY, None)
        user_data.pop(WINDOW_PAGE_KEY, None)


def clear_workspace_picker_state(user_data: dict | None) -> None:
    """Clear workspace picker state keys from user_data."""
    if user_data is not None:
        user_data.pop(STATE_KEY, None)
        user_data.pop(WORKSPACES_KEY, None)
        user_data.pop(WORKSPACE_PAGE_KEY, None)


def clear_session_picker_state(user_data: dict | None) -> None:
    """Clear session picker state keys from user_data."""
    if user_data is not None:
        user_data.pop(STATE_KEY, None)
        user_data.pop(SESSIONS_KEY, None)


# iTerm2 session names that carry no information: the profile default,
# a bare shell name, or the user's login name.
_GENERIC_TAB_NAMES = frozenset(
    {"", "default", "zsh", "bash", "fish", "sh", "login", "shell", "-zsh", "-bash"}
)


def picker_label(name: str, cwd: str) -> str:
    """Pick a display label for a tab in the adopt-tab picker.

    Prefers the tab's own name — a user who named a tab "Surge" wants
    to see "Surge".  Falls back to the cwd basename only when the name
    is a profile default (e.g. "Default", "zsh") or missing, and to
    "(unnamed)" when neither is usable.  iTerm2's ``session.path`` is
    often stale (``~``) without shell integration, which is why the
    cwd is the fallback rather than the primary source.
    """
    clean = (name or "").strip()
    if clean.lower() not in _GENERIC_TAB_NAMES and clean != Path.home().name:
        return clean
    if cwd:
        base = Path(cwd).name
        if base and base != Path.home().name:
            return base
    return clean or "(unnamed)"


def build_window_picker(
    windows: list[tuple[str, str, str, bool]],
    page: int = 0,
) -> tuple[str, InlineKeyboardMarkup, list[str]]:
    """Build window picker UI for adoptable iTerm2 sessions.

    Args:
        windows: List of (window_id, display_name, cwd, has_claude)
            tuples.  ``has_claude`` indicates whether session_map.json
            already has an entry for this UUID (meaning Claude is
            running in that tab); used to surface the running state
            via emoji and to decide whether to send `claude\\n` after
            binding.
        page: Zero-based page index for pagination.

    Returns: (text, keyboard, window_ids) where window_ids is the
    full ordered list of UUIDs (caller caches this so callbacks can
    map button index → UUID even across page flips).
    """
    window_ids = [wid for wid, _, _, _ in windows]

    total_pages = max(1, (len(windows) + WINDOWS_PER_PAGE - 1) // WINDOWS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * WINDOWS_PER_PAGE
    page_windows = windows[start : start + WINDOWS_PER_PAGE]

    lines = [
        "*Bind to Existing iTerm2 Tab*\n",
        "Tabs detected in iTerm2 that aren't bound to a topic.",
        "Pick one to adopt it, or browse for a directory to start fresh.",
        "",
        "🤖 = Claude already running   💻 = shell only",
        "",
    ]
    for wid, name, cwd, has_claude in page_windows:
        display_cwd = cwd.replace(str(Path.home()), "~") if cwd else "?"
        prefix = "🤖" if has_claude else "💻"
        lines.append(f"{prefix} `{name}` — {display_cwd}")

    buttons: list[list[InlineKeyboardButton]] = []
    # One button per row so the name + status icon both fit on phone.
    for offset, (_wid, name, _cwd, has_claude) in enumerate(page_windows):
        global_idx = start + offset
        prefix = "🤖" if has_claude else "💻"
        display = name[:30] + "…" if len(name) > 31 else name
        buttons.append(
            [
                InlineKeyboardButton(
                    f"{prefix} {display}", callback_data=f"{CB_WIN_BIND}{global_idx}"
                )
            ]
        )

    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton("◀", callback_data=f"{CB_WIN_PAGE}{page - 1}")
            )
        nav.append(
            InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop")
        )
        if page < total_pages - 1:
            nav.append(
                InlineKeyboardButton("▶", callback_data=f"{CB_WIN_PAGE}{page + 1}")
            )
        buttons.append(nav)

    # Label the escape hatch for what it actually opens: a backend that only
    # hosts sessions in registered projects has no filesystem to browse.
    new_label = (
        "📁 Browse directories instead"
        if terminal_manager.capabilities.arbitrary_cwd
        else "📦 Pick a project instead"
    )
    buttons.append([InlineKeyboardButton(new_label, callback_data=CB_WIN_NEW)])
    buttons.append([InlineKeyboardButton("Cancel", callback_data=CB_WIN_CANCEL)])

    text = "\n".join(lines)
    return text, InlineKeyboardMarkup(buttons), window_ids


def build_directory_browser(
    current_path: str, page: int = 0
) -> tuple[str, InlineKeyboardMarkup, list[str]]:
    """Build directory browser UI.

    Returns: (text, keyboard, subdirs) where subdirs is the full list for caching.
    """
    path = Path(current_path).expanduser().resolve()
    if not path.exists() or not path.is_dir():
        path = Path.cwd()

    try:
        subdirs = sorted(
            [
                d.name
                for d in path.iterdir()
                if d.is_dir()
                and (config.show_hidden_dirs or not d.name.startswith("."))
            ]
        )
    except (PermissionError, OSError):
        subdirs = []

    total_pages = max(1, (len(subdirs) + DIRS_PER_PAGE - 1) // DIRS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * DIRS_PER_PAGE
    page_dirs = subdirs[start : start + DIRS_PER_PAGE]

    buttons: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(page_dirs), 2):
        row = []
        for j, name in enumerate(page_dirs[i : i + 2]):
            display = name[:12] + "…" if len(name) > 13 else name
            # Use global index (start + i + j) to avoid long dir names in callback_data
            idx = start + i + j
            row.append(
                InlineKeyboardButton(
                    f"📁 {display}", callback_data=f"{CB_DIR_SELECT}{idx}"
                )
            )
        buttons.append(row)

    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton("◀", callback_data=f"{CB_DIR_PAGE}{page - 1}")
            )
        nav.append(
            InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop")
        )
        if page < total_pages - 1:
            nav.append(
                InlineKeyboardButton("▶", callback_data=f"{CB_DIR_PAGE}{page + 1}")
            )
        buttons.append(nav)

    action_row: list[InlineKeyboardButton] = []
    # Allow going up unless at filesystem root
    if path != path.parent:
        action_row.append(InlineKeyboardButton("..", callback_data=CB_DIR_UP))
    action_row.append(InlineKeyboardButton("Select", callback_data=CB_DIR_CONFIRM))
    action_row.append(InlineKeyboardButton("Cancel", callback_data=CB_DIR_CANCEL))
    buttons.append(action_row)

    display_path = str(path).replace(str(Path.home()), "~")
    if not subdirs:
        text = f"*Select Working Directory*\n\nCurrent: `{display_path}`\n\n_(No subdirectories)_"
    else:
        text = f"*Select Working Directory*\n\nCurrent: `{display_path}`\n\nTap a folder to enter, or select current directory"

    return text, InlineKeyboardMarkup(buttons), subdirs


WORKSPACES_PER_PAGE = 6


def build_workspace_picker(
    workspaces: list[Workspace], page: int = 0
) -> tuple[str, InlineKeyboardMarkup, list[str]]:
    """Build the workspace picker UI.

    Shown instead of the directory browser when the backend declares
    ``arbitrary_cwd=False``: there is no point letting the user walk the
    filesystem when the backend will only open a session inside a project it
    already knows, and rejecting the choice afterwards is a worse experience
    than not offering it.

    Returns: (text, keyboard, paths) where paths is the full ordered list for
    caching — the callback carries an index, not a path, to stay inside
    Telegram's 64-byte callback_data limit.
    """
    paths = [w.path for w in workspaces]
    total_pages = max(
        1, (len(workspaces) + WORKSPACES_PER_PAGE - 1) // WORKSPACES_PER_PAGE
    )
    page = max(0, min(page, total_pages - 1))
    start = page * WORKSPACES_PER_PAGE
    page_items = workspaces[start : start + WORKSPACES_PER_PAGE]

    buttons: list[list[InlineKeyboardButton]] = []
    for offset, ws in enumerate(page_items):
        label = ws.label or Path(ws.path).name or ws.path
        if ws.detail:
            label = f"{label} · {ws.detail}"
        if len(label) > 34:
            label = label[:33] + "…"
        buttons.append(
            [
                InlineKeyboardButton(
                    f"📦 {label}", callback_data=f"{CB_WS_SELECT}{start + offset}"
                )
            ]
        )

    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton("◀", callback_data=f"{CB_WS_PAGE}{page - 1}")
            )
        nav.append(
            InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop")
        )
        if page < total_pages - 1:
            nav.append(
                InlineKeyboardButton("▶", callback_data=f"{CB_WS_PAGE}{page + 1}")
            )
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("Cancel", callback_data=CB_WS_CANCEL)])

    if workspaces:
        text = "*Select a Project*\n\nSessions run inside a project the terminal already knows."
    else:
        text = (
            "*No projects available*\n\n"
            "This terminal hosts sessions inside registered projects only. "
            "Add the project in the terminal app first, then send a message here again."
        )
    return text, InlineKeyboardMarkup(buttons), paths


def _relative_time(file_path: str) -> str:
    """Format file mtime as a human-readable relative time string."""
    try:
        mtime = os.path.getmtime(file_path)
    except OSError:
        return ""
    delta = int(time.time() - mtime)
    if delta < 60:
        return "just now"
    if delta < 3600:
        m = delta // 60
        return f"{m}m ago"
    if delta < 86400:
        h = delta // 3600
        return f"{h}h ago"
    d = delta // 86400
    return f"{d}d ago"


def build_session_picker(
    sessions: list[ClaudeSession],
) -> tuple[str, InlineKeyboardMarkup]:
    """Build session picker UI for resuming an existing Claude session.

    Args:
        sessions: List of ClaudeSession objects (sorted by recency).

    Returns: (text, keyboard).
    """
    lines = [
        "*Resume Session?*\n",
        "Existing sessions found in this directory.\n",
    ]
    for i, s in enumerate(sessions):
        summary = s.summary[:40] + "…" if len(s.summary) > 40 else s.summary
        rel = _relative_time(s.file_path)
        time_str = f" ({rel})" if rel else ""
        lines.append(f"{i + 1}. {summary} — {s.message_count} msgs{time_str}")

    buttons: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(sessions), 2):
        row = []
        for j in range(min(2, len(sessions) - i)):
            s = sessions[i + j]
            label = s.summary[:14] + "…" if len(s.summary) > 14 else s.summary
            row.append(
                InlineKeyboardButton(
                    f"▶ {label}", callback_data=f"{CB_SESSION_SELECT}{i + j}"
                )
            )
        buttons.append(row)

    buttons.append(
        [
            InlineKeyboardButton("➕ New Session", callback_data=CB_SESSION_NEW),
            InlineKeyboardButton("Cancel", callback_data=CB_SESSION_CANCEL),
        ]
    )

    text = "\n".join(lines)
    return text, InlineKeyboardMarkup(buttons)

"""Hook subcommand for Claude Code session tracking.

Called by Claude Code's SessionStart hook to maintain a terminal-session
↔ Claude-session mapping in <CCBOT_DIR>/session_map.json. Also provides
`--install` to auto-configure the hook in ~/.claude/settings.json.

To identify which terminal session this Claude instance runs in, the hook
resolves a session_map key (see ``_resolve_session_key``):
  - ``CCBOT_SESSION_KEY`` — injected by ccbot for backends without a
    per-session env id (e.g. Orca); used verbatim.
  - ``ITERM_SESSION_ID`` — iTerm2 injects it into every shell; keyed as
    ``iterm:<UUID>``.

This module must NOT import config.py (which requires TELEGRAM_BOT_TOKEN),
since hooks run inside terminal sessions where the bot env vars are not
set. Config directory resolution uses utils.ccbot_dir() (shared with
config.py).

Key functions: hook_main() (CLI entry), _install_hook().
"""

import argparse
import fcntl
import json
import logging
import os
import re
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Validate session_id looks like a UUID
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# A ccbot-injected session_map key: "<backend>:<id>" (e.g. "orca:term_1f2e…").
_CCBOT_KEY_RE = re.compile(r"^[a-z0-9]+:[A-Za-z0-9._-]+$")

_CLAUDE_SETTINGS_FILE = Path.home() / ".claude" / "settings.json"


def _resolve_session_key() -> str | None:
    """Determine the session_map.json key for the terminal hosting this hook.

    Priority:
      1. ``CCBOT_SESSION_KEY`` — injected by ccbot at launch for backends with
         no per-session env id (Orca, ...). Already a full ``<prefix><id>``
         key; written verbatim so it matches what the bot polls for.
      2. ``ITERM_SESSION_ID`` — iTerm2 injects ``wXtYpZ:UUID`` into every
         shell; key on the UUID as ``iterm:<UUID>``.

    Returns None (with a warning) when neither identifies a session.
    """
    ccbot_key = os.environ.get("CCBOT_SESSION_KEY", "")
    if ccbot_key:
        if _CCBOT_KEY_RE.match(ccbot_key):
            return ccbot_key
        logger.warning("CCBOT_SESSION_KEY is malformed: %r", ccbot_key)
        return None

    iterm_var = os.environ.get("ITERM_SESSION_ID", "")
    _, _, uuid = iterm_var.partition(":")
    if not uuid:
        logger.warning(
            "No CCBOT_SESSION_KEY, and ITERM_SESSION_ID not set or malformed "
            "(got %r); cannot determine terminal session",
            iterm_var,
        )
        return None
    if not _UUID_RE.match(uuid.lower()):
        logger.warning("ITERM_SESSION_ID UUID is malformed: %s", uuid)
        return None
    return f"iterm:{uuid}"


# The hook command suffix for detection
_HOOK_COMMAND_SUFFIX = "ccbot hook"


def _find_ccbot_path() -> str:
    """Find the full path to the ccbot executable.

    Priority:
    1. shutil.which("ccbot") - if ccbot is in PATH
    2. Same directory as the Python interpreter (for venv installs)
    """
    # Try PATH first
    ccbot_path = shutil.which("ccbot")
    if ccbot_path:
        return ccbot_path

    # Fall back to the directory containing the Python interpreter
    # This handles the case where ccbot is installed in a venv
    python_dir = Path(sys.executable).parent
    ccbot_in_venv = python_dir / "ccbot"
    if ccbot_in_venv.exists():
        return str(ccbot_in_venv)

    # Last resort: assume it will be in PATH
    return "ccbot"


def _is_hook_installed(settings: dict) -> bool:
    """Check if ccbot hook is already installed in the settings.

    Detects both 'ccbot hook' and full paths like '/path/to/ccbot hook'.
    """
    hooks = settings.get("hooks", {})
    session_start = hooks.get("SessionStart", [])

    for entry in session_start:
        if not isinstance(entry, dict):
            continue
        inner_hooks = entry.get("hooks", [])
        for h in inner_hooks:
            if not isinstance(h, dict):
                continue
            cmd = h.get("command", "")
            # Match 'ccbot hook' or paths ending with 'ccbot hook'
            if cmd == _HOOK_COMMAND_SUFFIX or cmd.endswith("/" + _HOOK_COMMAND_SUFFIX):
                return True
    return False


def _install_hook() -> int:
    """Install the ccbot hook into Claude's settings.json.

    Returns 0 on success, 1 on error.
    """
    settings_file = _CLAUDE_SETTINGS_FILE
    settings_file.parent.mkdir(parents=True, exist_ok=True)

    # Read existing settings
    settings: dict = {}
    if settings_file.exists():
        try:
            settings = json.loads(settings_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Error reading %s: %s", settings_file, e)
            print(f"Error reading {settings_file}: {e}", file=sys.stderr)
            return 1

    # Check if already installed
    if _is_hook_installed(settings):
        logger.info("Hook already installed in %s", settings_file)
        print(f"Hook already installed in {settings_file}")
        return 0

    # Find the full path to ccbot
    ccbot_path = _find_ccbot_path()
    hook_command = f"{ccbot_path} hook"
    hook_config = {"type": "command", "command": hook_command, "timeout": 5}
    logger.info("Installing hook command: %s", hook_command)

    # Install the hook
    if "hooks" not in settings:
        settings["hooks"] = {}
    if "SessionStart" not in settings["hooks"]:
        settings["hooks"]["SessionStart"] = []

    settings["hooks"]["SessionStart"].append({"hooks": [hook_config]})

    # Write back
    try:
        settings_file.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
        )
    except OSError as e:
        logger.error("Error writing %s: %s", settings_file, e)
        print(f"Error writing {settings_file}: {e}", file=sys.stderr)
        return 1

    logger.info("Hook installed successfully in %s", settings_file)
    print(f"Hook installed successfully in {settings_file}")
    return 0


def hook_main() -> None:
    """Process a Claude Code hook event from stdin, or install the hook."""
    # Configure logging for the hook subprocess (main.py logging doesn't apply here)
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.DEBUG,
        stream=sys.stderr,
    )

    parser = argparse.ArgumentParser(
        prog="ccbot hook",
        description="Claude Code session tracking hook",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Install the hook into ~/.claude/settings.json",
    )
    # Parse only known args to avoid conflicts with stdin JSON
    args, _ = parser.parse_known_args(sys.argv[2:])

    if args.install:
        logger.info("Hook install requested")
        sys.exit(_install_hook())

    # Normal hook processing: read JSON from stdin
    logger.debug("Processing hook event from stdin")
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("Failed to parse stdin JSON: %s", e)
        return

    session_id = payload.get("session_id", "")
    cwd = payload.get("cwd", "")
    event = payload.get("hook_event_name", "")

    if not session_id or not event:
        logger.debug("Empty session_id or event, ignoring")
        return

    # Validate session_id format
    if not _UUID_RE.match(session_id):
        logger.warning("Invalid session_id format: %s", session_id)
        return

    # Validate cwd is an absolute path (if provided)
    if cwd and not os.path.isabs(cwd):
        logger.warning("cwd is not absolute: %s", cwd)
        return

    if event != "SessionStart":
        logger.debug("Ignoring non-SessionStart event: %s", event)
        return

    # Resolve which terminal session hosts this hook (CCBOT_SESSION_KEY for
    # ccbot-launched backends like Orca, else ITERM_SESSION_ID for iTerm2).
    # The window_name field is left empty — the bot looks up the live name
    # via its own terminal connection at read time, which is more reliable
    # than running a query from this short-lived hook subprocess.
    session_window_key = _resolve_session_key()
    if session_window_key is None:
        return

    logger.debug(
        "iterm key=%s, session_id=%s, cwd=%s",
        session_window_key,
        session_id,
        cwd,
    )

    # Read-modify-write with file locking to prevent concurrent hook races
    from .utils import ccbot_dir

    map_file = ccbot_dir() / "session_map.json"
    map_file.parent.mkdir(parents=True, exist_ok=True)

    lock_path = map_file.with_suffix(".lock")
    try:
        with open(lock_path, "w") as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
            logger.debug("Acquired lock on %s", lock_path)
            try:
                session_map: dict[str, dict[str, str]] = {}
                if map_file.exists():
                    try:
                        session_map = json.loads(map_file.read_text())
                    except (json.JSONDecodeError, OSError):
                        logger.warning(
                            "Failed to read existing session_map, starting fresh"
                        )

                session_map[session_window_key] = {
                    "session_id": session_id,
                    "cwd": cwd,
                    "window_name": "",
                }

                from .utils import atomic_write_json

                atomic_write_json(map_file, session_map)
                logger.info(
                    "Updated session_map: %s -> session_id=%s, cwd=%s",
                    session_window_key,
                    session_id,
                    cwd,
                )
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
    except OSError as e:
        logger.error("Failed to write session_map: %s", e)

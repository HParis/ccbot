# CLAUDE.md

ccbot — Telegram bot that bridges Telegram Forum topics to Claude Code sessions via iTerm2 tabs. Each topic is bound to one iTerm2 session (UUID) running one Claude Code instance.

Tech stack: Python, python-telegram-bot, iTerm2 Python API, uv.

**Platform: macOS only.** iTerm2 must be running and the iTerm2 Python API must be enabled (Preferences → General → Magic → Enable Python API).

## Common Commands

```bash
uv run ruff check src/ tests/         # Lint — MUST pass before committing
uv run ruff format src/ tests/        # Format — auto-fix, then verify with --check
uv run pyright src/ccbot/             # Type check — MUST be 0 errors before committing
./scripts/restart.sh                  # Restart the ccbot service after code changes
ccbot hook --install                  # Auto-install Claude Code SessionStart hook
```

## Core Design Constraints

- **1 Topic = 1 Tab = 1 Session** — all internal routing keyed by iTerm2 session UUID, not tab name. Tab names are stored separately as display names. Same directory can have multiple tabs.
- **ccbot tabs are tagged** with the iTerm2 user variable `user.ccbot=1`. Untagged tabs (the user's own shells) are invisible to the bot.
- **iTerm2 must stay open** — closing iTerm2 kills every Claude Code session the bot is managing. The bot reconnects automatically when iTerm2 starts again, but in-flight Claude work is lost.
- **Topic-only** — no backward-compat for non-topic mode. No `active_sessions`, no `/list`, no General topic routing.
- **No message truncation** at parse layer — splitting only at send layer (`split_message`, 4096 char limit).
- **MarkdownV2 only** — use `safe_reply`/`safe_edit`/`safe_send` helpers (auto fallback to plain text). Internal queue/UI code calls bot API directly with its own fallback.
- **Hook-based session tracking** — `SessionStart` hook reads `ITERM_SESSION_ID`, writes `session_map.json` keyed `iterm:<UUID>`; monitor polls it to detect session changes.
- **Message queue per user** — FIFO ordering, message merging (3800 char limit), tool_use/tool_result pairing.
- **Rate limiting** — `AIORateLimiter(max_retries=5)` on the Application (30/s global). On restart, the global bucket is pre-filled to avoid burst against Telegram's server-side counter.

## Code Conventions

- Every `.py` file starts with a module-level docstring: purpose clear within 10 lines, one-sentence summary first line, then core responsibilities and key components.
- Telegram interaction: prefer inline keyboards over reply keyboards; use `edit_message_text` for in-place updates; keep callback data under 64 bytes; use `answer_callback_query` for instant feedback.

## Terminal Backends

The host terminal is pluggable (`src/ccbot/terminal/`): a `TerminalBackend`
Protocol + neutral `TerminalSession` + `Capabilities` flags, backends registered
by name, selected at startup via `CCBOT_BACKEND` (default `iterm2`). Consumers
import `terminal_manager` from `ccbot.terminal.manager` — never a concrete backend.

- **iterm2** (default): full capabilities; ownership via `user.ccbot=1`; hook
  keys on `ITERM_SESSION_ID` → `iterm:<UUID>`.
- **otty** (`CCBOT_BACKEND=otty`): drives `otty-cli`. Requires the Otty app
  running and `ipc-allow-send-keys = true` in `~/.config/otty/config.toml`.
  Capabilities: no ANSI-color capture (monochrome screenshots), no native
  ownership tag (owned panes tracked in-process + re-resolved by cwd), no
  reconnect events. Session id = pane id (`p_*`). `create_window` opens a shell
  tab, then sends `CCBOT_SESSION_KEY=otty:<pane_id> claude …` so the hook can
  write the key the bot waits on (Otty has no per-pane env id).

Adding a terminal = one backend file + `register("name")`. Capability flags
drive graceful degradation; never branch on the backend name in upper layers.
The SessionStart hook is backend-agnostic: it writes `CCBOT_SESSION_KEY` if set,
else derives the key from `ITERM_SESSION_ID`.

## Configuration

- Config directory: `~/.ccbot/` by default, override with `CCBOT_DIR` env var.
- `.env` loading priority: local `.env` > config dir `.env`.
- State files: `state.json` (thread bindings), `session_map.json` (hook-generated), `monitor_state.json` (byte offsets).
- iTerm2 profile: `CCBOT_ITERM2_PROFILE` (default `ccbot`). Set the profile's Title field to "Session Name" so Claude Code's TUI cannot override the tab title via OSC.

## Migration

If you're upgrading from the tmux-backed version, see `docs/migration-iterm2.md` for the one-time iTerm2 setup and what happens to existing topic bindings.

## Hook Configuration

Auto-install: `ccbot hook --install`

Or manually in `~/.claude/settings.json`:
```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [{ "type": "command", "command": "ccbot hook", "timeout": 5 }]
      }
    ]
  }
}
```

## Architecture Details

See @.claude/rules/architecture.md for full system diagram and module inventory.
See @.claude/rules/topic-architecture.md for topic→window→session mapping details.
See @.claude/rules/message-handling.md for message queue, merging, and rate limiting.

# System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Telegram Bot (bot.py)                       │
│  - Topic-based routing: 1 topic = 1 tab = 1 session                │
│  - /history: Paginated message history (default: latest page)      │
│  - /screenshot: Capture iTerm2 session screen as PNG               │
│  - /esc: Send Escape to interrupt Claude                           │
│  - Send text → Claude Code via iTerm2 keystrokes                   │
│  - Forward /commands to Claude Code                                │
│  - Create sessions via directory browser in unbound topics         │
│  - Tool use → tool result: edit message in-place                   │
│  - Interactive UI: AskUserQuestion / ExitPlanMode / Permission     │
│  - Per-user message queue + worker (merge, rate limit)             │
│  - MarkdownV2 output with auto fallback to plain text              │
├──────────────────────┬──────────────────────────────────────────────┤
│  markdown_v2.py      │  telegram_sender.py                         │
│  MD → MarkdownV2     │  split_message (4096 limit)                 │
│  + expandable quotes │                                             │
├──────────────────────┴──────────────────────────────────────────────┤
│  terminal_parser.py                                                 │
│  - Detect interactive UIs (AskUserQuestion, ExitPlanMode, etc.)    │
│  - Parse status line (spinner + working text)                      │
└──────────┬──────────────────────────────────────────────────────────┘
           │                              │
           │ Notify (NewMessage callback) │ Send (iTerm2 keys)
           │                              │
┌──────────┴──────────────┐    ┌──────────┴────────────────────────┐
│  SessionMonitor         │    │  ITerm2Manager (iterm2_manager.py)│
│  (session_monitor.py)   │    │  - list/find/create/kill tabs     │
│  - Poll JSONL every 2s  │    │  - send_keys (text + special keys)│
│  - Detect mtime changes │    │  - capture_pane (plain or ANSI)   │
│  - Parse new lines      │    │  - lazy iterm2.Connection +       │
│  - Track pending tools  │    │    1/2/4s reconnect backoff       │
│    across poll cycles   │    │  - filters by user.ccbot=1 tag    │
└──────────┬──────────────┘    └──────────────┬───────────────────┘
           │                                  │
           ▼                                  ▼ WebSocket (iterm2 API)
┌────────────────────────┐         ┌─────────────────────────┐
│  TranscriptParser      │         │  iTerm2 (running app)   │
│  (transcript_parser.py)│         │  - One tab per topic    │
│  - Parse JSONL entries │         │  - tagged user.ccbot=1  │
│  - Pair tool_use ↔     │         │  - tab name locked via  │
│    tool_result         │         │    profile Title=       │
│  - Format expandable   │         │    Session Name         │
│    quotes for thinking │         └────────────┬────────────┘
│  - Extract history     │                      │
└────────────────────────┘              SessionStart hook
                                                │
                                                ▼
                                    ┌────────────────────────┐
┌────────────────────────┐         │  Hook (hook.py)        │
│  SessionManager        │◄────────│  - Reads stdin payload │
│  (session.py)          │  reads  │  - Reads ITERM_SESSION │
│  - Window ↔ Session    │  map    │    _ID env var (UUID)  │
│    resolution          │         │  - Writes session_map  │
│  - Thread bindings     │         │    keyed iterm:<UUID>  │
│    (topic → UUID)      │         └────────────────────────┘
│  - Message history     │
│    retrieval           │         ┌────────────────────────┐
└────────────────────────┘────────►│  Claude Sessions       │
                            reads  │  ~/.claude/projects/   │
                            JSONL  │  - sessions-index      │
┌────────────────────────┐         │  - *.jsonl files       │
│  MonitorState          │         └────────────────────────┘
│  (monitor_state.py)    │
│  - Track byte offset   │
│  - Prevent duplicates  │
│    after restart       │
└────────────────────────┘

Additional modules:
  screenshot.py       ─ Terminal text → PNG rendering (ANSI color, font fallback)
  transcribe.py       ─ Voice-to-text transcription via OpenAI API (gpt-4o-transcribe)
  main.py             ─ CLI entry point + iTerm2 connectivity probe at startup
  utils.py            ─ Shared utilities (ccbot_dir, atomic_write_json)

Handler modules (handlers/):
  message_sender.py   ─ safe_reply/safe_edit/safe_send + rate_limit_send
  message_queue.py    ─ Per-user queue + worker (merge, status dedup)
  status_polling.py   ─ Background status line polling (1s interval)
  response_builder.py ─ Response pagination and formatting
  interactive_ui.py   ─ AskUserQuestion / ExitPlanMode / Permission UI
  directory_browser.py─ Directory selection + session picker UI for new topics
  cleanup.py          ─ Topic state cleanup on close/delete
  callback_data.py    ─ Callback data constants

State files (~/.ccbot/ or $CCBOT_DIR/):
  state.json         ─ thread bindings + window states + display names + read offsets
  session_map.json   ─ hook-generated UUID→session mapping (keys: iterm:<UUID>)
  monitor_state.json ─ poll progress (byte offset) per JSONL file
```

## Key Design Decisions

- **Topic-centric** — Each Telegram topic binds to one iTerm2 tab. No centralized session list; topics *are* the session list.
- **UUID-centric** — All internal state keyed by iTerm2 session UUID, not tab names. UUIDs are stable for the lifetime of the iTerm2 process. Tab display names are kept separately via `window_display_names`. Same directory can have multiple tabs.
- **ccbot tab tagging** — New tabs created by ccbot are tagged with the iTerm2 user variable `user.ccbot=1`. Untagged tabs (the user's own shells) are invisible to `list_windows` / `find_window_*`.
- **Hook-based session tracking** — Claude Code `SessionStart` hook reads `ITERM_SESSION_ID`, writes `session_map.json` with `iterm:<UUID>` keys; monitor reads it each poll cycle to auto-detect session changes. Legacy `ccbot:` keys (tmux era) are silently ignored on read.
- **Tool use ↔ tool result pairing** — `tool_use_id` tracked across poll cycles; tool result edits the original tool_use Telegram message in-place.
- **MarkdownV2 with fallback** — All messages go through `safe_reply`/`safe_edit`/`safe_send` which convert via `telegramify-markdown` and fall back to plain text on parse failure.
- **No truncation at parse layer** — Full content preserved; splitting at send layer respects Telegram's 4096 char limit with expandable quote atomicity.
- Only sessions registered in `session_map.json` (via hook) are monitored.
- Notifications delivered to users via thread bindings (topic → UUID → session).
- **Startup re-resolution** — UUIDs change when iTerm2 restarts. On startup, `resolve_stale_ids()` matches persisted display names against live tabs to re-map UUIDs. Legacy state files keyed by tmux `@N` IDs are migrated via the same display-name lookup.
- **iTerm2 connectivity probe at startup** — `main.py` opens a short-lived connection via `asyncio.run(iterm2_manager._get_connection())` so the bot fails fast with a clear error if iTerm2 isn't running or the Python API isn't enabled.

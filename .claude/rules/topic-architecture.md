# Topic-Only Architecture

The bot operates exclusively in Telegram Forum (topics) mode. There is **no** `active_sessions` mapping, **no** `/list` command, **no** General topic routing, and **no** backward-compatibility logic for older non-topic modes. Every code path assumes named topics.

## 1 Topic = 1 Tab = 1 Session

```
┌─────────────┐      ┌─────────────┐      ┌─────────────┐
│  Topic ID   │ ───▶ │ Window ID   │ ───▶ │ Session ID  │
│  (Telegram) │      │ (iTerm UUID)│      │  (Claude)   │
└─────────────┘      └─────────────┘      └─────────────┘
     thread_bindings      session_map.json
     (state.json)         (written by hook)
```

`window_id` is an iTerm2 session UUID (e.g. `9F2E3A1B-DEAD-BEEF-CAFE-0123456789AB`). Tab display names are stored separately as `window_display_names`. UUIDs are stable for the lifetime of the iTerm2 process; they change when iTerm2 quits and reopens, at which point `resolve_stale_ids()` re-maps via display name.

## Mapping 1: Topic → Window ID (thread_bindings)

```python
# session.py: SessionManager
thread_bindings: dict[int, dict[int, str]]  # user_id → {thread_id → window_id}
window_display_names: dict[str, str]        # window_id → tab name (for display)
```

- Storage: memory + `state.json`
- Written when: user creates a new session via the directory browser in a topic
- Purpose: route user messages to the correct iTerm2 tab

## Mapping 2: Window ID → Session (session_map.json)

```python
# session_map.json (key format: "iterm:<UUID>")
{
  "iterm:9F2E3A1B-...": {"session_id": "uuid-xxx", "cwd": "/path/to/project", "window_name": ""},
  "iterm:5C8B1F2A-...": {"session_id": "uuid-yyy", "cwd": "/path/to/project", "window_name": ""}
}
```

- Storage: `session_map.json`
- Written when: Claude Code's `SessionStart` hook fires inside an iTerm2 shell. The hook reads `ITERM_SESSION_ID` (format `wXtYpZ:UUID`) and uses the UUID portion as the key. `window_name` is left empty — the bot resolves the live name through its own iTerm2 connection at read time.
- Legacy `ccbot:` keys (tmux era) are silently ignored on read; they're harmless and get overwritten on the next SessionStart fire.
- Property: one tab maps to one session; session_id changes after `/clear`.
- Purpose: SessionMonitor uses this mapping to decide which sessions to watch.

## Message Flows

**Outbound** (user → Claude):
```
User sends "hello" in topic (thread_id=42)
  → thread_bindings[user_id][42] → "<UUID>"
  → send_to_window("<UUID>", "hello")   # resolves via find_window_by_id
```

**Inbound** (Claude → user):
```
SessionMonitor reads new message (session_id = "uuid-xxx")
  → Iterate thread_bindings, find (user, thread) whose window_id maps to this session
  → Deliver message to user in the correct topic (thread_id)
```

**New topic flow**: First message in an unbound topic → directory browser → select directory → session picker (if existing sessions found) or create tab → bind topic → forward pending message.

**Resume session flow**: When selecting a directory with existing Claude sessions, a session picker UI is shown. Choosing a session runs `claude --resume <session_id>`. Note: `--resume` makes the hook report a new session_id but messages continue writing to the original JSONL file; the bot overrides window_state to track the original session_id.

**Topic lifecycle**: Closing/deleting a topic auto-closes the associated iTerm2 session and unbinds the thread. Stale bindings (tab deleted externally) are cleaned up by the status polling loop.

## Session Lifecycle

**Startup cleanup**: On bot startup, all tracked sessions not present in session_map are cleaned up, preventing monitoring of closed sessions.

**Runtime change detection**: Each polling cycle checks for session_map changes:
- A tab's session_id changed (e.g., after `/clear`) → clean up old session
- Tab deleted → clean up corresponding session

**iTerm2 quit/restart**: When iTerm2 quits, every ccbot session dies. When the user reopens iTerm2, the bot's `_get_connection()` reconnects on the next call. Persisted topic bindings still reference the old (now-dead) UUIDs; users either re-bind by sending a new message in the topic, or `resolve_stale_ids()` re-maps via display name on the next bot restart.

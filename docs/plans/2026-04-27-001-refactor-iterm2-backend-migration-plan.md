---
title: "refactor: Migrate ccbot terminal backend from tmux to iTerm2"
type: refactor
status: completed
date: 2026-04-27
---

# refactor: Migrate ccbot terminal backend from tmux to iTerm2

## Overview

ccbot currently runs Claude Code instances inside tmux windows and drives them via `libtmux`. This breaks Claude Code's Computer Use feature, which needs to interact with the host GUI directly — tmux's PTY layer prevents that. This plan replaces the tmux backend with iTerm2 (via the official `iterm2` Python API) and removes tmux from the project entirely. The Telegram-facing layer (`bot.py`, `handlers/`, `session_monitor.py`, `transcript_parser.py`, `screenshot.py`) keeps its existing call shape; only the terminal management module, the SessionStart hook, and the persisted state schema change.

## Problem Frame

- **Why now:** Computer Use is unusable while Claude Code runs inside tmux. The user has decided to drop tmux entirely rather than maintain dual backends.
- **Constraints accepted by the user:**
  - macOS-only deployment (iTerm2 is macOS-exclusive).
  - iTerm2 must be running for ccbot to operate; if iTerm2 quits, all bound sessions die — same blast radius as the user closing all tmux panes.
  - No `TerminalBackend` abstraction with two implementations. Replace, don't dual-track.
- **Non-negotiable upper-layer behaviors that must survive the migration:**
  - 1 Topic ↔ 1 Window/Tab ↔ 1 Claude session, keyed internally by a stable terminal-side ID.
  - `send_keys` semantics: literal text + 500ms gap + Enter; `!` bash-mode prefix sends `!` then waits 1s; special keys (Up/Down/Left/Right/Escape/Tab/Enter) supported.
  - `capture_pane` returns plain text or ANSI-colored text (latter feeds `screenshot.py`).
  - `create_window` opens a new terminal at a given cwd, runs `claude` (optionally with `--resume`), prevents the window name from being overwritten by the TUI.
  - `kill_window`, `rename_window`, `list_windows`, `find_window_by_id`/`by_name` keep current return shapes.
  - SessionStart hook continues to write `session_map.json` so the monitor can map terminal-window → Claude session.

## Requirements Trace

- **R1.** Drop all tmux runtime dependencies (`libtmux`, the `tmux` CLI subprocess in hook + capture). Project should run on a machine without tmux installed.
- **R2.** New `iterm2_manager.py` exposes the same public surface that `tmux_manager.TmuxManager` exposes today, so upper-layer call sites change at most their import + module name.
- **R3.** `hook.py` works inside an iTerm2 shell using `ITERM_SESSION_ID` instead of `TMUX_PANE` + `tmux display-message`.
- **R4.** `screenshot.py` continues to render correctly from `capture_pane(with_ansi=True)`. The ANSI escape format produced by the new backend matches what `screenshot.py:_parse_ansi_line` already understands (16/256/RGB color, bold).
- **R5.** Existing topic↔window bindings in `state.json` either migrate cleanly or fail loudly with a documented one-shot reset path. No silent data loss.
- **R6.** Window names stay stable across the Claude Code session lifetime — the TUI cannot override them (tmux's `allow-rename off` analog).
- **R7.** All current bot commands keep working: `/screenshot`, `/esc`, `/usage`, `/history`, directory browser → create session, `--resume`, kill on topic close, status polling, message queue, interactive UIs.
- **R8.** When iTerm2 is not running at startup, ccbot fails fast with a clear log message; if iTerm2 quits at runtime, the bot logs and reconnects rather than crashing.

## Scope Boundaries

- Out of scope: supporting Linux or any non-iTerm2 macOS terminal (Terminal.app, Alacritty, kitty, WezTerm). Re-introducing a backend abstraction later would be a separate plan.
- Out of scope: changing the JSONL transcript reader, the message queue worker, MarkdownV2 conversion, rate-limiting, the message merge/pairing logic, or the Telegram command surface.
- Out of scope: a guided UI to install/configure the iTerm2 profile — text instructions in README + the migration doc are enough.
- Out of scope: live state migration UI. If the auto-migration doesn't recover a binding, the user re-binds the topic by sending a message (existing directory-browser flow handles unbound topics).

## Context & Research

### Relevant Code and Patterns

- `src/ccbot/tmux_manager.py` — current backend, all 9 public methods are the contract to mirror (`list_windows`, `find_window_by_name`, `find_window_by_id`, `capture_pane`, `send_keys`, `rename_window`, `kill_window`, `create_window`, plus the `TmuxWindow` dataclass and singleton `tmux_manager`).
- `src/ccbot/hook.py:191-220` — uses `os.environ["TMUX_PANE"]` then `tmux display-message` to derive `session_name:window_id:window_name`. This is the only iTerm2-specific hook change.
- `src/ccbot/session.py:194` — `resolve_stale_ids()` already does name-based re-resolution at startup; extend the same pattern to handle the one-time tmux-ID → iTerm2-UUID re-keying.
- `src/ccbot/screenshot.py:48-225` — already parses ANSI 16/256/RGB color + bold/dim/etc. The new backend must emit ANSI escapes in this dialect; no changes to `screenshot.py` itself.
- `src/ccbot/session_monitor.py` — reads `session_map.json` keyed by `tmux_session:window_id`. The key shape changes (see Key Technical Decisions); the read site does not care about the key format as long as it's consistent.
- `src/ccbot/config.py:61-62` — `tmux_session_name` and `tmux_main_window_name` are tmux-specific; replaced with iTerm2 equivalents.
- `pyproject.toml` — `libtmux>=0.37.0` removed, `iterm2>=2.7` added.
- `scripts/restart.sh` — does not touch tmux directly today (just `pkill -f ccbot` + relaunch). No expected change.
- `.claude/rules/architecture.md`, `.claude/rules/topic-architecture.md`, `CLAUDE.md` — references to tmux must be updated.

### Institutional Learnings

- `~/.claude/projects/<repo>/memory/project_terminal_backend_migration.md` — captures the "no dual backend, Computer Use is the reason" context. Already saved.
- The repo's docstring conventions (every `.py` starts with a one-sentence module summary, then responsibilities) — follow in the new module.

### External References

- iTerm2 Python API docs: https://iterm2.com/python-api/ (`iterm2.Connection`, `iterm2.async_get_app`, `Window.async_create_tab`, `Session.async_send_text`, `Session.async_get_screen_contents`, `Session.async_set_name`).
- `ITERM_SESSION_ID` env var format: `w<W>t<T>p<P>:<UUID>` — iTerm2 injects this into every shell launched inside a session.
- iTerm2 profile "Title" setting: when set to "Session Name", `async_set_name()` is authoritative and OSC title-set sequences from the running TUI are ignored.

## Key Technical Decisions

- **Single backend module, drop-in name.** New file `src/ccbot/iterm2_manager.py` exposing class `ITerm2Manager` and module-level singleton `iterm2_manager`. Public methods match `tmux_manager.TmuxManager` 1:1 (same names, same signatures, same return types). Old file `tmux_manager.py` is **deleted** in the same change-set, with imports across `bot.py` / `session.py` rewritten to `from .iterm2_manager import iterm2_manager`. *Rationale:* keeps the diff in upper-layer files mechanical (rename only), avoids a transition window with two backends.
- **Window identity = iTerm2 session UUID.** The persisted "window_id" continues to be a string, but its concrete shape changes from tmux `@N` to iTerm2 session UUID (e.g. `9F2E3A1B-...`). Upper layers treat the value as opaque — no code parses `@N` anywhere (verified by grep: only `session.py:resolve_stale_ids` heuristically detects `@`). *Rationale:* iTerm2 session UUID is the only stable handle the iTerm2 API provides; tab/window indices renumber as users reorder tabs.
- **session_map.json key shape: `iterm:<UUID>`.** Replaces `ccbot:@N`. The `ccbot:` prefix loses meaning (no tmux-session concept in iTerm2), so we hard-code `iterm:` to keep keys recognizable in JSON dumps and to leave room for a future backend prefix. *Rationale:* explicit prefix prevents accidental collision and makes migration detection trivial (`key.startswith("ccbot:")` ⇒ legacy entry to drop).
- **State migration: best-effort by display name, otherwise drop.** On bot startup, `resolve_stale_ids()` is extended: any `thread_bindings` value that looks like `@N` (tmux ID) is treated as stale; the persisted display name is looked up against live iTerm2 sessions; on match, the binding is re-keyed to the new UUID; on miss, the binding is dropped and a warning is logged so the user knows they need to re-bind that topic. `session_map.json` is unconditionally rewritten by hooks fired in newly-launched Claude sessions, so legacy `ccbot:@N` entries are simply ignored at read time and pruned on first write. *Rationale:* avoids a destructive wipe while staying simple. The user can recover any dropped binding by sending a message in that topic (existing unbound-topic flow opens the directory browser).
- **Hook switches to `ITERM_SESSION_ID`.** `hook.py` reads `os.environ["ITERM_SESSION_ID"]`, parses out the UUID after the colon, and writes `iterm:<UUID>` as the session_map key. The `cwd` and `window_name` fields stay (cwd from hook payload; window_name fetched via a short-lived iTerm2 API connection inside the hook subprocess). *Rationale:* iTerm2 always sets this env var; no extra installation needed. The hook fetching window_name via API is the one new external dependency — if the API connection fails, the hook still writes session_map with `window_name=""` and the bot falls back to UUID-as-display-name (matches today's tmux failure mode).
- **`send_keys` keeps the 500ms / 1s timing.** Identical async sleeps. Special keys map to literal escape sequences sent via `Session.async_send_text(...)`: Up=`\x1b[A`, Down=`\x1b[B`, Right=`\x1b[C`, Left=`\x1b[D`, Escape=`\x1b`, Tab=`\t`, Enter=`\r`. *Rationale:* iTerm2's `async_send_text` is the equivalent of `tmux send-keys -l` — it doesn't interpret named keys, so we translate them ourselves. The `literal=False` branch in the current API maps to "expand named key → escape sequence" and then send.
- **`capture_pane(with_ansi=True)` reconstructs ANSI from `ScreenContents`.** `Session.async_get_screen_contents()` returns lines with per-character style ranges. We walk the cells, emit SGR escapes (`\x1b[...m`) on style transitions, and emit a reset at line ends. Color mapping: indexed colors → `38;5;N` / `48;5;N`; RGB → `38;2;R;G;B` / `48;2;R;G;B`; bold via `1`, reset via `0`. The output dialect matches what `screenshot.py:_apply_ansi_codes` already parses. `with_ansi=False` returns the joined plain `LineContents.string` values. *Rationale:* lets `screenshot.py` stay untouched.
- **Window name lock via profile + `async_set_name`.** New windows are created with a dedicated iTerm2 profile (default name: `ccbot`); the migration doc instructs the user to set that profile's "Title" to "Session Name" once. After `Window.async_create_tab(profile="ccbot")`, the bot calls `Session.async_set_name(final_window_name)`. This is the analog of tmux `allow-rename off`. If the profile doesn't exist, the bot logs a warning and falls back to the default profile (TUI may then occasionally rename the tab; degrades gracefully). *Rationale:* avoids requiring users to manually create a profile to start, while still giving them a path to a fully locked title.
- **`list_windows` filters to ccbot-owned tabs.** Today, `list_windows` filters out the `__main__` placeholder. With iTerm2, ccbot-created tabs are tagged via a custom user variable (`Session.async_set_variable("user.ccbot", "1")`) at creation time; `list_windows` only returns tabs where this variable is set. *Rationale:* the user's other iTerm2 windows/tabs (their daily-driver shells) must be invisible to ccbot. Tab tagging is more reliable than name-based filtering.
- **iTerm2 connection lifecycle: lazy connect, auto-reconnect.** `ITerm2Manager` lazily creates one `iterm2.Connection` on first use, kept open for the bot's lifetime. On API errors that indicate disconnection, the next call attempts reconnect with a 1s/2s/4s backoff (max 3 retries). On startup, if connection fails, log a clear error pointing to "is iTerm2 running?" and exit non-zero. *Rationale:* iTerm2's WebSocket is stable when iTerm2 is up; brief reconnects cover sleep/wake transitions. Hard fail at startup matches current tmux behavior (no tmux server = bot crashes early).
- **iTerm2 main-window placeholder is gone.** tmux needed a `__main__` window so the session wasn't empty. iTerm2 windows can have arbitrary tabs without a placeholder; ccbot creates a single dedicated iTerm2 *window* (named `ccbot`) lazily on first `create_window` call, and adds tabs into it. If the user closes that window, the next `create_window` recreates it. *Rationale:* simpler model, no `tmux_main_window_name` concept to filter.

## Open Questions

### Resolved During Planning

- **Does iTerm2 expose a stable per-tab name we can lock?** Yes — `Session.async_set_name()` combined with profile "Title = Session Name" setting. Documented in the user-facing migration note. (See Key Technical Decisions.)
- **Should we keep a `TerminalBackend` abstraction for future Linux support?** No — explicit user decision in the request: "不保留双后端抽象，直接替换".
- **What identifier replaces tmux `@N` window IDs internally?** iTerm2 session UUID. Upper layers already treat the value as opaque, so no API ripple beyond renaming `tmux_window_id` doc strings.
- **Does `screenshot.py` need rework?** No — the new backend emits the same ANSI dialect. The work is in the backend's serializer, not in `screenshot.py`.
- **What happens to existing `state.json`?** Best-effort re-resolution by display name; unrecoverable bindings are dropped with a logged warning. No destructive wipe.

### Deferred to Implementation

- Exact iTerm2 Python API method names (e.g. whether the screen-contents accessor is `async_get_screen_contents` or `async_get_contents` for the version we install) — verify against the pinned `iterm2` package version when writing the module.
- Whether the iTerm2 connection survives macOS sleep/wake without explicit reconnect — confirmed empirically during implementation; if it doesn't, the existing 3-retry backoff covers it.
- The exact set of SGR codes that round-trip cleanly through both the iTerm2 `Style` model and `screenshot.py`'s parser — start with bold + 16-color + 256-color + RGB; add italic/underline only if `screenshot.py` already handles them (it does for bold; verify others against `_parse_ansi_line`).
- Whether the hook subprocess can hold an iTerm2 API connection within its 5-second timeout — if not, fall back to writing `window_name=""` and resolve from session_map's neighboring entries on read.

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

```
┌────────────────────────────────────────────────────────────────────┐
│ Telegram bot (bot.py / handlers / session_monitor)                │
│   import: from .iterm2_manager import iterm2_manager              │
│   calls:  list_windows / find_window_by_id / send_keys /          │
│           capture_pane / create_window / kill_window /            │
│           rename_window / find_window_by_name                     │
└────────────────────────┬──────────────────────────────────────────┘
                         │  (interface unchanged from TmuxManager)
                         ▼
┌────────────────────────────────────────────────────────────────────┐
│ iterm2_manager.py — ITerm2Manager                                 │
│                                                                    │
│   _connection: iterm2.Connection (lazy, reconnect-on-error)       │
│   _ccbot_window: iterm2.Window | None  (lazy main window)         │
│                                                                    │
│   list_windows()      → walk app.windows[*].tabs[*]; keep tabs    │
│                         where session.user.ccbot == "1"           │
│   find_window_by_id() → resolve UUID → Session                    │
│   create_window()     → ensure ccbot window; create_tab(profile,  │
│                         start_directory); send_text(claude cmd);  │
│                         set_name(name); set user.ccbot=1          │
│   send_keys()         → translate special keys → escape seq;      │
│                         async_send_text; sleeps for !-mode/Enter  │
│   capture_pane()      → async_get_screen_contents; if with_ansi   │
│                         → cells_to_ansi(); else → joined .string  │
│   kill_window()       → session.async_close()                     │
│   rename_window()     → session.async_set_name()                  │
└────────────────────────┬──────────────────────────────────────────┘
                         │ WebSocket (iterm2 Python API)
                         ▼
                 ┌──────────────┐
                 │  iTerm2 app  │  ← user runs Claude Code here
                 └──────────────┘
                         ▲
                         │ ITERM_SESSION_ID env var
                         │
┌────────────────────────┴──────────────────────────────────────────┐
│ hook.py (runs as Claude Code SessionStart hook)                  │
│   reads os.environ["ITERM_SESSION_ID"] → "wXtYpZ:UUID"           │
│   parses UUID; key = f"iterm:{UUID}"                             │
│   writes session_map.json[key] = {session_id, cwd, window_name}  │
└──────────────────────────────────────────────────────────────────┘
```

Migration touch-points (files that change):
```
src/ccbot/
  iterm2_manager.py        NEW  (~400 lines, mirrors TmuxManager API)
  tmux_manager.py          DELETE
  hook.py                  EDIT (~30 lines: TMUX_PANE → ITERM_SESSION_ID)
  session.py               EDIT (~20 lines: resolve_stale_ids handles @N legacy + import rename)
  bot.py                   EDIT (~5 lines: import rename + 1-2 user-facing string updates)
  config.py                EDIT (~10 lines: tmux_* → iterm2_* config keys)
  __init__.py              EDIT (1 line: docstring tmux → iTerm2)
  handlers/*.py            EDIT (only if they import tmux_manager — none directly today)
pyproject.toml             EDIT (deps: -libtmux, +iterm2)
.claude/rules/*.md         EDIT (architecture wording: tmux → iTerm2)
CLAUDE.md                  EDIT (rule wording)
README.md                  EDIT (install + iTerm2 profile setup)
docs/migration-iterm2.md   NEW (one-shot user instructions)
tests/test_iterm2_manager.py NEW (unit + integration coverage)
```

## Implementation Units

- [ ] **Unit 1: Add iTerm2 dependency, drop libtmux, scaffold module**

**Goal:** Establish the new dependency and an empty `iterm2_manager.py` whose public surface matches `TmuxManager` (signatures only — methods raise `NotImplementedError`). Fail-fast importable so the rest of the migration can proceed unit by unit.

**Requirements:** R1, R2

**Dependencies:** none

**Files:**
- Modify: `pyproject.toml` (remove `libtmux>=0.37.0`, add `iterm2>=2.7`)
- Create: `src/ccbot/iterm2_manager.py`
- Create: `tests/test_iterm2_manager.py`

**Approach:**
- Define `@dataclass class ITermWindow` mirroring `TmuxWindow` fields (`window_id`, `window_name`, `cwd`, `pane_current_command`).
- Define `class ITerm2Manager` with method stubs matching `TmuxManager` 1:1.
- Module-level singleton `iterm2_manager = ITerm2Manager()`.
- Module docstring follows repo convention (one-sentence summary + responsibilities + key class).
- Run `uv sync` post-edit to confirm dep resolution.

**Patterns to follow:**
- `src/ccbot/tmux_manager.py` for module layout and docstring.

**Test scenarios:**
- Happy path: import `iterm2_manager` → singleton exists, all 9 methods present, types match `TmuxManager`'s public API.
- Edge case: each stub method raises `NotImplementedError` so accidental upper-layer use during migration fails loudly.

**Verification:**
- `uv run ruff check` and `uv run pyright src/ccbot/` pass on the new file.
- Importing the singleton in a Python REPL does not require iTerm2 to be running.

---

- [ ] **Unit 2: Implement iTerm2 connection lifecycle + window/tab discovery**

**Goal:** Wire the live iTerm2 connection and implement read-only methods (`list_windows`, `find_window_by_id`, `find_window_by_name`).

**Requirements:** R2, R8

**Dependencies:** Unit 1

**Files:**
- Modify: `src/ccbot/iterm2_manager.py`
- Modify: `tests/test_iterm2_manager.py`

**Approach:**
- Lazy `_get_connection()` returning a cached `iterm2.Connection`. On `ConnectionRefusedError` / WebSocket errors, retry with 1/2/4s backoff (max 3) before raising.
- Identify ccbot-owned tabs by checking `session.async_get_variable("user.ccbot") == "1"`; skip tabs without this marker.
- `list_windows()` walks `app.windows[*].tabs[*]` and returns one `ITermWindow` per ccbot-tagged session; `window_id` = session UUID, `window_name` = session name, `cwd` = session variable `path` (or shell-integration `pwd` if available; empty string if not), `pane_current_command` = session variable `jobName` (or empty).
- `find_window_by_id(uuid)` looks up by session UUID; `find_window_by_name(name)` walks tagged sessions and matches by name.
- All blocking iTerm2 API calls are already async, so no `asyncio.to_thread` wrapping needed.

**Patterns to follow:**
- `src/ccbot/tmux_manager.py:list_windows` for filtering pattern (skip non-ccbot windows).

**Test scenarios:**
- Integration (requires iTerm2 + the `iterm2` daemon enabled): manually tag a tab with `user.ccbot=1`, assert `list_windows()` returns it.
- Edge case: no ccbot-tagged tabs → empty list, no exception.
- Error path: iTerm2 not running → first call retries 3× then raises with a clear "iTerm2 not running" message.
- Edge case: `find_window_by_id` with unknown UUID → returns `None`, logs at debug level.

**Verification:**
- Mocked unit tests pass without iTerm2; integration tests are gated behind `pytest.mark.integration`.

---

- [ ] **Unit 3: Implement `send_keys` and `capture_pane` with ANSI reconstruction**

**Goal:** Drive the terminal: input (text + special keys + `!`-mode timing) and output (plain + ANSI text matching `screenshot.py`'s parser dialect).

**Requirements:** R2, R4, R6 (partial — name lock comes in Unit 4), R7

**Dependencies:** Unit 2

**Files:**
- Modify: `src/ccbot/iterm2_manager.py`
- Modify: `tests/test_iterm2_manager.py`

**Approach:**
- `send_keys(window_id, text, enter, literal)`:
  - Resolve UUID → Session.
  - If `literal=False`, map named keys to escape sequences (Up/Down/Right/Left → `\x1b[A`/`B`/`C`/`D`, Escape → `\x1b`, Tab → `\t`, Enter → `\r`); otherwise send `text` verbatim.
  - Preserve current 500ms/1s timing: literal-with-enter sends text → 500ms sleep → `\r`; if text starts with `!`, send `!` → 1s sleep → rest, then 500ms → `\r`.
- `capture_pane(window_id, with_ansi)`:
  - Call `Session.async_get_screen_contents()`.
  - `with_ansi=False`: join `LineContents.string` per line with `\n`.
  - `with_ansi=True`: walk each line cell-by-cell, emit SGR codes on style transitions (bold, fg, bg), emit `\x1b[0m` at line end. Color encoding:
    - indexed (0-15) → `38;5;N` / `48;5;N`
    - extended (16-255) → `38;5;N` / `48;5;N`
    - RGB → `38;2;R;G;B` / `48;2;R;G;B`
    - default → `39` / `49`
  - Output must round-trip through `screenshot.py:_parse_ansi_line` without losing color info — verified by a test that captures, parses back, and compares cell colors.

**Patterns to follow:**
- `src/ccbot/tmux_manager.py:send_keys` for the `!` and Enter-delay logic.
- `src/ccbot/screenshot.py:_apply_ansi_codes` for the reverse mapping (target dialect).

**Test scenarios:**
- Happy path: `send_keys(uuid, "hello", enter=True, literal=True)` writes `hello\r` to the session with the 500ms gap.
- Happy path: `send_keys(uuid, "Up", enter=False, literal=False)` writes `\x1b[A`.
- Edge case: `send_keys(uuid, "!ls", enter=True, literal=True)` sends `!`, waits 1s, sends `ls`, waits 500ms, sends `\r`.
- Happy path: `capture_pane(uuid, with_ansi=False)` returns plain text without escape codes.
- Integration: `capture_pane(uuid, with_ansi=True)` on a session displaying colored output → rendering through `screenshot.text_to_image` produces a non-blank PNG with non-default colors (assert at least one non-default-color pixel).
- Error path: `send_keys` to a UUID that no longer exists → returns `False`, logs error.

**Verification:**
- Round-trip test: capture-with-ansi → parse with `screenshot._parse_ansi_line` → reconstruct foreground colors → matches input.

---

- [ ] **Unit 4: Implement `create_window`, `kill_window`, `rename_window`, name lock**

**Goal:** Lifecycle methods: spawn a tab in the ccbot window with a given cwd, optionally start `claude [--resume <id>]`, lock the tab name, kill on demand, rename in place.

**Requirements:** R2, R6, R7

**Dependencies:** Unit 2

**Files:**
- Modify: `src/ccbot/iterm2_manager.py`
- Modify: `tests/test_iterm2_manager.py`

**Approach:**
- `_get_or_create_ccbot_window()`: find an iTerm2 window whose first session has `user.ccbot_main=1`; if absent, create a new iTerm2 window with profile `ccbot` (fall back to default if missing, log warning), tag the initial session, and cache the `Window` reference.
- `create_window(work_dir, window_name, start_claude, resume_session_id)`:
  - Validate `work_dir` exists.
  - De-duplicate name with `-2`/`-3` suffix (existing logic).
  - Get or create the ccbot iTerm2 window.
  - `window.async_create_tab(profile="ccbot", command=None)` — we don't pass `command` because we want to set vars first, then run claude.
  - On the new tab's session: `async_set_variable("user.ccbot", "1")`, `async_set_name(final_window_name)`, then `async_send_text(f"cd {shlex.quote(work_dir)}\n")`, then if `start_claude`: `async_send_text(f"{config.claude_command}{' --resume ' + resume_id if resume_id else ''}\n")`.
  - Return `(True, msg, final_window_name, session_uuid)`.
- `kill_window(uuid)`: resolve session → `await session.async_close(force=True)`.
- `rename_window(uuid, new_name)`: resolve session → `await session.async_set_name(new_name)`.
- Name-lock guidance: implementation of the lock is via the `ccbot` profile having Title=Session Name. This is a one-time user setup; the bot does not auto-create the profile but logs a warning if Title field of the active profile isn't set to "Session Name" (best-effort check via profile API, fall back to "couldn't verify, see migration doc").

**Patterns to follow:**
- `src/ccbot/tmux_manager.py:create_window` for name de-duplication and resume-id handling.

**Test scenarios:**
- Happy path: `create_window("/tmp", "x", start_claude=False)` opens a new tab in cwd `/tmp` with name `x`, returns the UUID, the tab is tagged with `user.ccbot=1`.
- Edge case: calling `create_window` with a name that already exists yields `x-2`.
- Edge case: `start_claude=True, resume_session_id="abc-123"` → the shell receives `claude --resume abc-123\n`.
- Integration: `kill_window(uuid)` closes the tab; subsequent `find_window_by_id(uuid)` returns `None`.
- Integration: `rename_window(uuid, "newname")` updates the displayed tab title and `find_window_by_name("newname")` finds it.
- Error path: `create_window` with non-existent dir → `(False, "...does not exist...", "", "")`.
- Error path: `kill_window` with stale UUID → returns `False`, logs at error.

**Verification:**
- After `create_window`, `list_windows()` includes the new tab with the expected fields.

---

- [ ] **Unit 5: Rewrite `hook.py` to use `ITERM_SESSION_ID`**

**Goal:** SessionStart hook works inside any iTerm2 shell with no tmux involvement, writing the new `iterm:<UUID>` keyed `session_map.json`.

**Requirements:** R3

**Dependencies:** Unit 1 (deps available); Unit 4 (window_name lookup uses the same connection logic, but optional — see fallback below)

**Files:**
- Modify: `src/ccbot/hook.py`
- Modify: `tests/test_hook.py` (or `tests/test_hook_iterm.py` if no existing)

**Approach:**
- Replace the `TMUX_PANE` + `tmux display-message` block (`hook.py:191-220`) with:
  - `iterm_var = os.environ.get("ITERM_SESSION_ID", "")` (format `wXtYpZ:UUID`).
  - Parse: `_, _, uuid = iterm_var.partition(":")`; reject if uuid is empty or not a valid UUID.
  - Key = `f"iterm:{uuid}"`.
  - `window_name`: best-effort fetch via a short-lived `iterm2.Connection` inside the hook subprocess (timeout-bounded to avoid blocking past the hook's 5s budget). On any error, set to `""` — bot read path already tolerates empty names.
- Drop the `_HOOK_COMMAND_SUFFIX` install logic? Keep it; the install path is unchanged, only the runtime code path changes.
- Old session_map entries with `ccbot:` prefix: harmless — bot read path filters them out (Unit 6).

**Patterns to follow:**
- `src/ccbot/hook.py` existing flow for argparse, stdin parsing, file locking.

**Test scenarios:**
- Happy path: `ITERM_SESSION_ID="w0t1p0:9F2E3A1B-DEAD-BEEF-CAFE-0123456789AB"` + valid stdin → `session_map.json` contains `"iterm:9F2E3A1B-..."`.
- Edge case: `ITERM_SESSION_ID` empty → hook logs warning and exits cleanly (no map mutation).
- Edge case: malformed env var (no colon) → same as above.
- Edge case: iTerm2 API unreachable inside the hook process → window_name is `""`, key still written.
- Error path: stdin not JSON → existing graceful exit unchanged.
- Concurrency: two hooks racing for the lock → both writes land, no corruption (existing fcntl logic).

**Verification:**
- Run the hook manually: `ITERM_SESSION_ID=w0t0p0:UUID echo '{"session_id":"...","cwd":"/tmp","hook_event_name":"SessionStart"}' | ccbot hook` → check `session_map.json`.

---

- [ ] **Unit 6: State migration in `session.py:resolve_stale_ids`, config rename, upper-layer import rewrite**

**Goal:** Replace all `tmux_manager` imports/references in `bot.py` and `session.py`, extend `resolve_stale_ids` to handle legacy `@N` window IDs, rename tmux config keys, and prune legacy `ccbot:` entries from `session_map.json` on read.

**Requirements:** R2, R5

**Dependencies:** Unit 4 (live iTerm2 lookup), Unit 5 (new key format)

**Files:**
- Modify: `src/ccbot/session.py`
- Modify: `src/ccbot/bot.py`
- Modify: `src/ccbot/config.py`
- Modify: `src/ccbot/__init__.py` (docstring)
- Modify: `src/ccbot/session_monitor.py` (only if it parses keys; check during impl)
- Delete: `src/ccbot/tmux_manager.py`
- Modify: `tests/test_session.py`

**Approach:**
- Mechanical rename: `from .tmux_manager import tmux_manager` → `from .iterm2_manager import iterm2_manager`. Then `s/tmux_manager\./iterm2_manager./g`. Verified safe by grep — all 40+ call sites use `tmux_manager.<method>` and the new module exposes the same methods.
- `session.py:resolve_stale_ids`: add a branch that detects `window_id.startswith("@")` (tmux ID) → use `window_display_names[id]` to look up by name against live `iterm2_manager.list_windows()`; if found, re-key all references; if not, drop the binding and log `WARNING: stale tmux binding for topic <thread_id> in <name>; user must re-bind`.
- `session_monitor.py` (if it iterates `session_map`): on read, skip keys starting with `ccbot:` (legacy tmux entries); they will be overwritten on next session start anyway.
- `config.py`: rename `tmux_session_name` → unused (drop), `tmux_main_window_name` → unused (drop). Add `iterm2_profile_name = os.getenv("CCBOT_ITERM2_PROFILE", "ccbot")`. Update startup log line.
- User-facing text in `bot.py:280` ("The Claude session is still running in tmux.\n") → update to "The Claude session is still running in iTerm2.\n".

**Patterns to follow:**
- `src/ccbot/session.py:194` existing `resolve_stale_ids` heuristic for name-based re-resolution.

**Test scenarios:**
- Happy path: load a `state.json` with no stale entries → no warnings, all bindings preserved.
- Migration: load a `state.json` with a binding `thread_bindings[u][t] = "@5"` and `window_display_names["@5"] = "myproj"`; live iTerm2 has a tagged tab named "myproj" with UUID U → after `resolve_stale_ids`, `thread_bindings[u][t] == "U"` and `window_display_names["U"] == "myproj"`.
- Migration: same setup but no live "myproj" tab → binding is dropped, warning logged, `thread_bindings[u]` no longer contains `t`.
- Edge case: `session_map.json` contains both `iterm:UUID-A` (current) and `ccbot:@3` (legacy) → monitor only tracks the iterm entry.
- Integration: full bot startup with a migrated state file proceeds without crashing and existing valid bindings keep working.

**Verification:**
- After running with a hand-crafted legacy state.json, the file is rewritten with UUID keys; legacy `@N` entries are gone; warnings list any dropped bindings.
- `grep -r "tmux" src/ccbot/` returns only docstring/comment hits in updated wording — no live code references.

---

- [ ] **Unit 7: Documentation, migration guide, restart script audit**

**Goal:** Update repo docs and produce a one-time user-facing migration note explaining the iTerm2 profile setup and the `state.json` migration behavior.

**Requirements:** R6 (user-side profile setup), R8 (clear failure messages documented)

**Dependencies:** Units 1-6 complete

**Files:**
- Modify: `CLAUDE.md`
- Modify: `.claude/rules/architecture.md`
- Modify: `.claude/rules/topic-architecture.md`
- Modify: `.claude/rules/message-handling.md` (only if it mentions tmux)
- Modify: `README.md`
- Create: `docs/migration-iterm2.md`
- Modify: `scripts/restart.sh` (audit only — likely no change)

**Approach:**
- Update architecture diagrams: tmux boxes → iTerm2 boxes; `tmux_manager.py` → `iterm2_manager.py`; `TMUX_PANE` → `ITERM_SESSION_ID`; key format `ccbot:@N` → `iterm:UUID`.
- README install section: add "macOS only", "iTerm2 must be running", "enable iTerm2 Python API" (Preferences → General → Magic → Enable Python API), "create the `ccbot` profile with Title = Session Name" (with screenshot or prefs path).
- `docs/migration-iterm2.md`: covers (a) one-shot iTerm2 profile setup, (b) what happens to existing topic bindings on first launch, (c) how to recover dropped bindings (just send a message in the topic).
- Audit `scripts/restart.sh` for any tmux-aware logic; current file just kills/restarts the bot process — no change expected, but verify.

**Patterns to follow:**
- Existing `CLAUDE.md` and `.claude/rules/*.md` style.

**Test scenarios:**
- Test expectation: none — documentation-only unit.

**Verification:**
- `grep -ir "tmux" docs/ README.md CLAUDE.md .claude/` returns only intentional historical mentions (e.g. "previously used tmux" in the migration doc).

---

- [ ] **Unit 8: End-to-end smoke test on real iTerm2 + commit**

**Goal:** Run the bot against a live iTerm2 with the migrated state and exercise every Telegram-side command path that touched tmux.

**Requirements:** R7 (functional parity)

**Dependencies:** Units 1-7

**Files:**
- No code changes; smoke test is manual.

**Approach:**
- Manual smoke checklist (executed by user after self-review):
  - Bot starts, connects to iTerm2, no errors.
  - Send a message in an unbound topic → directory browser appears → pick dir → tab is created in iTerm2 with locked name → Claude starts.
  - Send text → appears in the iTerm2 tab.
  - `/screenshot` → returns a PNG matching the iTerm2 tab content (color preserved).
  - `/esc` → interrupts Claude.
  - `/usage` → returns usage info.
  - Tool-use → tool-result message edit pairing works.
  - `/history` pagination works.
  - Close the topic in Telegram → iTerm2 tab is closed.
  - Quit iTerm2 mid-session → bot logs disconnect → re-launch iTerm2 → bot reconnects on next call.
- Run `uv run ruff check src/ tests/`, `uv run ruff format --check src/ tests/`, `uv run pyright src/ccbot/`.

**Test scenarios:**
- Test expectation: covered by Units 1-6 unit tests + this manual smoke.

**Verification:**
- All checklist items pass; lint/format/type pass; commit is created with a single commit message describing the migration.

## System-Wide Impact

- **Interaction graph:** The terminal-management module is the only swap point. `bot.py` (40+ call sites), `session.py` (3 call sites), `handlers/*` (no direct tmux imports today — confirm during Unit 6) all depend on this surface. Hook subprocess + bot main process now both depend on the `iterm2` Python package.
- **Error propagation:** `iterm2`-side errors (disconnection, missing session, profile missing) propagate as `False` returns or `None` from manager methods — same pattern as today's `libtmux` error swallowing. Connection-level retries are localized in `_get_connection`.
- **State lifecycle risks:**
  - `state.json` migration is best-effort; explicitly accept the possibility of dropping bindings whose tabs no longer exist in iTerm2. Documented in the migration note.
  - `session_map.json` legacy entries (`ccbot:` prefix) coexist transiently and are pruned on next overwrite — no race because each window/UUID is independent.
- **API surface parity:** Public method names and signatures of the manager are deliberately preserved so upper-layer call sites need no semantic changes. This is a hard invariant — any deviation breaks the migration's "rename only" property in `bot.py`.
- **Integration coverage:** Hook → session_map → monitor → bot full path must be exercised in Unit 8 smoke (unit tests don't cover the hook subprocess writing then bot reading).
- **Unchanged invariants:** `~/.claude/projects/*.jsonl` reading (transcript_parser, session_monitor) is independent of terminal backend — Claude Code writes there regardless. `screenshot.py`'s ANSI parser is unchanged; the new backend produces a compatible dialect by design. Telegram message queue, MarkdownV2 conversion, rate limiting, AskUserQuestion / ExitPlanMode UI: all unaffected.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| iTerm2 Python API version drift between dev machine and minor release | Pin `iterm2>=2.7,<3` in pyproject; verify on the version actually installed during Unit 1. |
| Hook subprocess can't open an iTerm2 connection within 5s timeout | Hook tolerates `window_name=""`; bot read path already handles empty names by falling back to the UUID. |
| User forgets the "Title = Session Name" profile step → tab names get overwritten by Claude TUI | Bot logs a warning on startup if the active profile's title format isn't detectable as "Session Name"; migration doc has the exact preferences path. Functional impact is cosmetic only. |
| iTerm2 quits mid-session → all Claude processes die | Same blast radius as user closing all tmux panes today; documented as accepted constraint. Bot logs disconnect and reconnects when iTerm2 is back. |
| Legacy `state.json` has a binding whose name collides with an unrelated existing iTerm2 tab the user has open (e.g. they have a tab named "myproj" but it's their personal shell, not a ccbot tab) | `list_windows` filters by `user.ccbot=1` tag, so untagged user tabs are invisible to the migration. Worst case: the binding is dropped (treated as unrecoverable), user re-binds. |
| Computer Use needs not just "no tmux" but also iTerm2's accessibility permissions | Document in README; outside the scope of this code migration (user grants permissions once in System Settings). |

## Documentation / Operational Notes

- New user-facing setup: enable iTerm2 Python API (one-time toggle in iTerm2 Preferences → General → Magic), create the `ccbot` profile (Title = Session Name).
- Operational change: ccbot now has a hard dependency on iTerm2 being running. Consider adding a startup check that surfaces "iTerm2 is not running — please launch iTerm2 and retry" in the bot's first log line.
- Restart script (`scripts/restart.sh`) is unchanged but should be re-tested.
- The `CCBOT_ITERM2_PROFILE` env var (default `ccbot`) is a new escape hatch for users who already have a profile they prefer.

## Sources & References

- iTerm2 Python API documentation: https://iterm2.com/python-api/
- Claude Code SessionStart hook contract (existing in `hook.py:160-188`)
- Project memory: `~/.claude/projects/<repo>/memory/project_terminal_backend_migration.md`
- Existing reference implementation: `src/ccbot/tmux_manager.py` (the surface to mirror)
- Existing ANSI parser dialect: `src/ccbot/screenshot.py:_parse_ansi_line`

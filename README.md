# CCBot

Control Claude Code sessions remotely via Telegram — monitor, interact, and manage AI coding sessions running in a macOS terminal (iTerm2 by default, or Orca).

https://github.com/user-attachments/assets/15ffb38e-5eb9-4720-93b9-412e4961dc93

## Why CCBot?

Claude Code runs in your terminal. When you step away from your computer — commuting, on the couch, or just away from your desk — the session keeps working, but you lose visibility and control.

CCBot solves this by letting you **seamlessly continue the same session from Telegram**. The key insight is that it operates on **iTerm2**, not the Claude Code SDK. Your Claude Code process stays exactly where it is, in an iTerm2 tab on your machine. CCBot simply reads its output and sends keystrokes to it. This means:

- **Switch from desktop to phone mid-conversation** — Claude is working on a refactor? Walk away, keep monitoring and responding from Telegram.
- **Switch back to desktop anytime** — Bring iTerm2 to the foreground and you're back in the terminal with full scrollback and context.
- **Run multiple sessions in parallel** — Each Telegram topic maps to a separate iTerm2 tab, so you can juggle multiple projects from one chat group.
- **Computer Use works** — Because Claude runs directly in iTerm2 (not under a tmux PTY layer), Claude Code's Computer Use feature can drive the host GUI normally.

Other Telegram bots for Claude Code typically wrap the Claude Code SDK to create separate API sessions. Those sessions are isolated — you can't resume them in your terminal. CCBot takes a different approach: it's just a thin control layer over iTerm2, so the terminal remains the source of truth and you never lose the ability to switch back.

In fact, CCBot itself was built this way — iterating on itself through Claude Code sessions monitored and driven from Telegram via CCBot.

## Features

- **Topic-based sessions** — Each Telegram topic maps 1:1 to an iTerm2 tab and Claude session
- **Real-time notifications** — Get Telegram messages for assistant responses, thinking content, tool use/result, and local command output
- **Interactive UI** — Navigate AskUserQuestion, ExitPlanMode, and Permission Prompts via inline keyboard
- **Voice messages** — Voice messages are transcribed via OpenAI and forwarded as text
- **Send messages** — Forward text to Claude Code via iTerm2 keystrokes
- **Slash command forwarding** — Send any `/command` directly to Claude Code (e.g. `/clear`, `/compact`, `/cost`)
- **Create new sessions** — Start Claude Code sessions from Telegram via directory browser
- **Resume sessions** — Pick up where you left off by resuming an existing Claude session in a directory
- **Kill sessions** — Close a topic to auto-close the associated iTerm2 tab
- **Message history** — Browse conversation history with pagination (newest first)
- **Hook-based session tracking** — Auto-associates iTerm2 tabs with Claude sessions via `SessionStart` hook
- **Persistent state** — Thread bindings and read offsets survive restarts

## Prerequisites

- **macOS** — the supported terminal backends (iTerm2, Orca) are macOS apps
- **A terminal backend** — one of:
  - **iTerm2** (default): installed, running, with the Python API enabled
    (Preferences → General → Magic → Enable Python API). Recommended: an
    iTerm2 profile named `ccbot` with its Title field set to "Session Name"
    so Claude Code's TUI cannot override the tab name. See
    `docs/migration-iterm2.md`.
  - **Orca** (`CCBOT_BACKEND=orca`): installed, with its CLI available
    (CCBot launches the app itself when needed). Note that Orca hosts
    sessions inside registered projects only — see below.
- **Claude Code** — the CLI tool (`claude`) must be installed

See [Terminal Backend](#terminal-backend) below for how to choose and configure one.

## Installation

### Option 1: Install from GitHub (Recommended)

```bash
# Using uv (recommended)
uv tool install git+https://github.com/six-ddc/ccmux.git

# Or using pipx
pipx install git+https://github.com/six-ddc/ccmux.git
```

### Option 2: Install from source

```bash
git clone https://github.com/six-ddc/ccmux.git
cd ccmux
uv sync
```

## Configuration

**1. Create a Telegram bot and enable Threaded Mode:**

1. Chat with [@BotFather](https://t.me/BotFather) to create a new bot and get your bot token
2. Open @BotFather's profile page, tap **Open App** to launch the mini app
3. Select your bot, then go to **Settings** > **Bot Settings**
4. Enable **Threaded Mode**

**2. Configure environment variables:**

Create `~/.ccbot/.env`:

```ini
TELEGRAM_BOT_TOKEN=your_bot_token_here
ALLOWED_USERS=your_telegram_user_id
```

**Required:**

| Variable             | Description                       |
| -------------------- | --------------------------------- |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather         |
| `ALLOWED_USERS`      | Comma-separated Telegram user IDs |

**Optional:**

| Variable                | Default    | Description                                      |
| ----------------------- | ---------- | ------------------------------------------------ |
| `CCBOT_DIR`             | `~/.ccbot` | Config/state directory (`.env` loaded from here) |
| `CCBOT_BACKEND`         | `iterm2`   | Terminal backend hosting sessions: `iterm2` or `orca` |
| `CCBOT_ITERM2_PROFILE`  | `ccbot`    | iTerm2 profile used for new tabs (iTerm2 backend) |
| `CLAUDE_COMMAND`        | `claude`   | Command to run in new windows                    |
| `MONITOR_POLL_INTERVAL` | `2.0`      | Polling interval in seconds                      |
| `CCBOT_SHOW_HIDDEN_DIRS` | `false` | Show hidden (dot) directories in directory browser |
| `OPENAI_API_KEY` | _(none)_ | OpenAI API key for voice message transcription |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI API base URL (for proxies or compatible APIs) |

Message formatting is always HTML via `chatgpt-md-converter` (`chatgpt_md_converter` package).
There is no runtime formatter switch to MarkdownV2.

> If running on a VPS where there's no interactive terminal to approve permissions, consider:
>
> ```
> CLAUDE_COMMAND=IS_SANDBOX=1 claude --dangerously-skip-permissions
> ```

## Terminal Backend

CCBot hosts each Claude Code session in a GUI terminal tab and drives it
(reads output, sends keystrokes, creates/closes tabs). The backend is
selected once at startup with `CCBOT_BACKEND`; the default is `iterm2`.

### iTerm2 (default)

1. Enable the Python API: iTerm2 → Preferences → General → Magic → **Enable Python API**.
2. (Recommended) Create a profile named `ccbot` and set its **Title** field to
   "Session Name" so Claude Code's TUI can't rename the tab. See
   `docs/migration-iterm2.md`.
3. Leave `CCBOT_BACKEND` unset (or `iterm2`). iTerm2 must stay running — quitting
   it kills every managed session.

### Orca

```ini
CCBOT_BACKEND=orca
```

1. Install [Orca](https://orca.computer/). CCBot locates the `orca` CLI on
   PATH or in the app bundle, and starts the app itself when it isn't running.

Differences from iTerm2 to be aware of:

- **Sessions run in registered projects, not arbitrary directories** — an Orca
  terminal belongs to a worktree, so CCBot offers a project picker instead of
  the directory browser. A folder Orca doesn't know can't host a session; add
  it in Orca first.
- **Screenshots are monochrome** — Orca's screen capture carries no color.
- **No native tab tagging** — ownership is tracked in-process and re-resolved by
  working directory; a CCBot restart re-adopts terminals as topics are used again.
- **No reconnect events** — recovery is poll-based.

Adding another terminal is a small effort: implement one backend in
`src/ccbot/terminal/` and register it. See `CLAUDE.md` → "Terminal Backends".

## Hook Setup (Recommended)

Auto-install via CLI:

```bash
ccbot hook --install
```

Or manually add to `~/.claude/settings.json`:

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

This writes terminal-session-keyed mappings to `$CCBOT_DIR/session_map.json` (`~/.ccbot/` by default), so the bot automatically tracks which Claude session is running in each terminal tab — even after `/clear` or session restarts. The hook works for any backend: it keys on `ITERM_SESSION_ID` for iTerm2, or on the `CCBOT_SESSION_KEY` that CCBot injects at launch for backends like Orca.

## Usage

```bash
# If installed via uv tool / pipx
ccbot

# If installed from source
uv run ccbot
```

### Commands

**Bot commands:**

| Command       | Description                     |
| ------------- | ------------------------------- |
| `/start`      | Show welcome message            |
| `/history`    | Message history for this topic  |
| `/screenshot` | Capture terminal screenshot     |
| `/esc`        | Send Escape to interrupt Claude |

**Claude Code commands (forwarded as keystrokes):**

| Command    | Description                  |
| ---------- | ---------------------------- |
| `/clear`   | Clear conversation history   |
| `/compact` | Compact conversation context |
| `/cost`    | Show token/cost usage        |
| `/help`    | Show Claude Code help        |
| `/memory`  | Edit CLAUDE.md               |

Any unrecognized `/command` is also forwarded to Claude Code as-is (e.g. `/review`, `/doctor`, `/init`).

### Topic Workflow

**1 Topic = 1 Tab = 1 Session.** The bot runs in Telegram Forum (topics) mode.

**Creating a new session:**

1. Create a new topic in the Telegram group
2. Send any message in the topic
3. A directory browser appears — select the project directory
4. If the directory has existing Claude sessions, a session picker appears — choose one to resume or start fresh
5. An iTerm2 tab is created (tagged with `user.ccbot=1`), `claude` starts (with `--resume` if resuming), and your pending message is forwarded

**Sending messages:**

Once a topic is bound to a session, just send text or voice messages in that topic — text gets forwarded to Claude Code via iTerm2 keystrokes, and voice messages are automatically transcribed and forwarded as text.

**Killing a session:**

Close (or delete) the topic in Telegram. The associated iTerm2 tab is automatically closed and the binding is removed.

### Message History

Navigate with inline buttons:

```
📋 [project-name] Messages (42 total)

───── 14:32 ─────

👤 fix the login bug

───── 14:33 ─────

I'll look into the login bug...

[◀ Older]    [2/9]    [Newer ▶]
```

### Notifications

The monitor polls session JSONL files every 2 seconds and sends notifications for:

- **Assistant responses** — Claude's text replies
- **Thinking content** — Shown as expandable blockquotes
- **Tool use/result** — Summarized with stats (e.g. "Read 42 lines", "Found 5 matches")
- **Local command output** — stdout from commands like `git status`, prefixed with `❯ command_name`

Notifications are delivered to the topic bound to the session's tab.

Formatting note:
- Telegram messages are rendered with parse mode `HTML` using `chatgpt-md-converter`
- Long messages are split with HTML tag awareness to preserve code blocks and formatting

## Running Claude Code in iTerm2

### Option 1: Create via Telegram (Recommended)

1. Create a new topic in the Telegram group
2. Send any message
3. Select the project directory from the browser

### Option 2: Create Manually

Open a tab in iTerm2 yourself, mark it as ccbot-owned, then start Claude Code:

```bash
# Tag the tab so the bot can see it (uses iTerm2's `it2setvar` if installed,
# or use the iTerm2 Python API from another shell). Without this tag the
# tab is invisible to ccbot.
printf '\x1b]1337;SetUserVar=ccbot=%s\x07' "$(printf 1 | base64)"
claude
```

In practice, creating tabs via Telegram is much simpler — the bot tags + names them automatically and runs the right `cd && claude` for you.

The Claude Code SessionStart hook reads `ITERM_SESSION_ID` and registers the tab in `session_map.json` regardless of how the tab was created. See `docs/migration-iterm2.md` for details on iTerm2 profile setup and the upgrade-from-tmux flow.

## Data Storage

| Path                            | Description                                                             |
| ------------------------------- | ----------------------------------------------------------------------- |
| `$CCBOT_DIR/state.json`         | Thread bindings, window states, display names, and per-user read offsets |
| `$CCBOT_DIR/session_map.json`   | Hook-generated `{iterm:<UUID>: {session_id, cwd, window_name}}` mappings |
| `$CCBOT_DIR/monitor_state.json` | Monitor byte offsets per session (prevents duplicate notifications)     |
| `~/.claude/projects/`           | Claude Code session data (read-only)                                    |

## File Structure

```
src/ccbot/
├── __init__.py            # Package entry point
├── main.py                # CLI dispatcher (hook subcommand + bot bootstrap)
├── hook.py                # Hook subcommand for session tracking (+ --install)
├── config.py              # Configuration from environment variables
├── bot.py                 # Telegram bot setup, command handlers, topic routing
├── session.py             # Session management, state persistence, message history
├── session_monitor.py     # JSONL file monitoring (polling + change detection)
├── monitor_state.py       # Monitor state persistence (byte offsets)
├── transcript_parser.py   # Claude Code JSONL transcript parsing
├── terminal_parser.py     # Terminal pane parsing (interactive UI + status line)
├── html_converter.py      # Markdown → Telegram HTML conversion + HTML-aware splitting
├── screenshot.py          # Terminal text → PNG image with ANSI color support
├── transcribe.py          # Voice-to-text transcription via OpenAI API
├── utils.py               # Shared utilities (atomic JSON writes, JSONL helpers)
├── tmux_manager.py        # Tmux window management (list, create, send keys, kill)
├── fonts/                 # Bundled fonts for screenshot rendering
└── handlers/
    ├── __init__.py        # Handler module exports
    ├── callback_data.py   # Callback data constants (CB_* prefixes)
    ├── directory_browser.py # Directory browser inline keyboard UI
    ├── history.py         # Message history pagination
    ├── interactive_ui.py  # Interactive UI handling (AskUser, ExitPlan, Permissions)
    ├── message_queue.py   # Per-user message queue + worker (merge, rate limit)
    ├── message_sender.py  # safe_reply / safe_edit / safe_send helpers
    ├── response_builder.py # Response message building (format tool_use, thinking, etc.)
    └── status_polling.py  # Terminal status line polling
```

## Contributors

Thanks to all the people who contribute! We encourage using Claude Code to collaborate on contributions.

<a href="https://github.com/six-ddc/ccmux/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=six-ddc/ccmux" />
</a>

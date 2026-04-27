# Migrating ccbot from tmux to iTerm2

This is a one-time guide for upgrading from the tmux-backed version of ccbot to the iTerm2-backed version. Skip it if you're a new user.

## Why the change

tmux's PTY layer prevented Claude Code's Computer Use feature from interacting with the host GUI. Running Claude Code directly inside iTerm2 sessions (no PTY in the way) fixes that. The trade-off is that ccbot is now macOS-only and depends on iTerm2 being open.

## One-time setup

### 1. Install / verify iTerm2

iTerm2 must be installed and running. The bot will fail to start if it can't reach the iTerm2 Python API.

### 2. Enable the iTerm2 Python API

Preferences → General → Magic → check **Enable Python API**. The first time the bot connects, iTerm2 will prompt you to allow the connection — accept it.

### 3. Create the `ccbot` profile (recommended)

Without a dedicated profile, ccbot tabs use your default profile. That works, but Claude Code's TUI may try to override the tab title via OSC sequences. Locking the title requires a profile setting:

1. Preferences → Profiles → click `+` (new profile)
2. Name the profile `ccbot` (or anything else; set `CCBOT_ITERM2_PROFILE` if you choose another name)
3. Profile → General → **Title** → choose **Session Name** (not "Job" or "Profile")

When ccbot creates a new tab via this profile and calls `Session.async_set_name(...)`, the tab title is locked: any OSC title sequence Claude emits is ignored.

### 4. (Optional) Set `CCBOT_ITERM2_PROFILE`

If you used a different profile name, add this to `~/.ccbot/.env`:

```
CCBOT_ITERM2_PROFILE=your-profile-name
```

The default is `ccbot`. When the configured profile is missing at runtime, ccbot falls back to the default profile and logs a warning — the bot stays functional, but tab titles aren't locked.

### 5. Reinstall ccbot

```bash
# If you installed via uv tool:
uv tool install --reinstall git+https://github.com/six-ddc/ccmux.git

# From source:
cd /path/to/ccbot
uv sync --extra dev
./scripts/restart.sh
```

The Claude Code SessionStart hook (`ccbot hook`) is unchanged — it now reads `ITERM_SESSION_ID` instead of `TMUX_PANE`, but the install command (`ccbot hook --install`) and the entry in `~/.claude/settings.json` look the same.

## What happens to your existing topic bindings

Your `~/.ccbot/state.json` carries topic↔window bindings keyed by tmux IDs (`@0`, `@5`, …). On the first startup after the upgrade:

1. ccbot calls `iterm2_manager.list_windows()` which returns iTerm2 sessions tagged with `user.ccbot=1`. Right after the upgrade, this is the empty set (you haven't created any ccbot tabs yet under the new backend).
2. For each persisted `@N` binding, ccbot looks up the persisted display name in the live iTerm2 set. Nothing matches, so the binding is **dropped** with a warning like:

   ```
   Dropping stale thread binding: user=12345, thread=42, wid=@5
   ```

3. The dropped topic falls back to the unbound-topic flow: when you next send a message in that topic, ccbot shows the directory browser and you re-bind the topic to a new tab.

This is intentional — there's no automated way to know which iTerm2 tab corresponds to a deleted tmux window. If you preserved the tmux session and want to keep the same Claude session, follow the Claude session resume flow:

1. Open the topic in Telegram
2. Send any message
3. Pick the project directory in the browser
4. The session picker shows existing Claude sessions for that directory — pick the one you were working on

Claude Code resumes via `--resume <session_id>` and continues from the same conversation state.

## What happens to `session_map.json`

The hook now writes `iterm:<UUID>` keys instead of `ccbot:@N`. Legacy `ccbot:`-prefixed entries are silently ignored on read; they're harmless and get pruned naturally as the user creates new sessions.

If you want to wipe the file by hand:

```bash
rm ~/.ccbot/session_map.json
```

It will be re-created by the next SessionStart hook fire.

## Troubleshooting

**"Cannot connect to iTerm2. Ensure iTerm2 is running and the Python API is enabled."**

iTerm2 is not running, or the Python API toggle is off. See steps 1 and 2 above.

**Tab titles keep changing while Claude is running.**

The active iTerm2 profile's Title field is set to "Job" or "Profile" instead of "Session Name". See step 3.

**`list_windows()` returns nothing even though I have ccbot tabs open.**

The tabs aren't tagged. ccbot tags only the tabs it creates itself; tabs you open manually need to be tagged via the iTerm2 API or recreated through the Telegram directory browser.

**iTerm2 quit and now my Claude sessions are gone.**

That's expected. iTerm2's process owns the shell and Claude Code; quitting iTerm2 kills both. Reopen iTerm2, send a message in the topic, and re-bind via the directory browser.

**"libtmux not found" / pre-upgrade ccbot processes still running.**

The new ccbot doesn't depend on libtmux. If `which ccbot` still resolves to the pre-upgrade install, run `uv tool install --reinstall ...` (or `pipx reinstall ccmux`) to refresh.

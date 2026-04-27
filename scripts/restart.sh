#!/usr/bin/env bash
set -euo pipefail

TMUX_SESSION="ccbot"
TMUX_WINDOW="__main__"
TARGET="${TMUX_SESSION}:${TMUX_WINDOW}"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LAUNCHD_LABEL="com.user.ccbot"
LAUNCHD_PLIST="$HOME/Library/LaunchAgents/${LAUNCHD_LABEL}.plist"
MAX_WAIT=10  # seconds to wait for process to exit

# --- Stop ALL running ccbot instances ---

# 1. Stop launchd-managed instance (if any)
if launchctl list 2>/dev/null | grep -q "$LAUNCHD_LABEL"; then
    echo "Stopping launchd-managed ccbot..."
    launchctl unload "$LAUNCHD_PLIST" 2>/dev/null || true
    sleep 1
fi

# 2. Kill any orphaned ccbot processes
ORPHAN_PIDS=$(pgrep -f 'ccbot' 2>/dev/null | xargs -I{} sh -c 'ps -p {} -o command= 2>/dev/null | grep -q "bin/ccbot" && echo {}' || true)
if [ -n "$ORPHAN_PIDS" ]; then
    echo "Killing orphaned ccbot processes: $ORPHAN_PIDS"
    echo "$ORPHAN_PIDS" | xargs kill 2>/dev/null || true
    sleep 2
    # Force kill survivors
    for pid in $ORPHAN_PIDS; do
        if ps -p "$pid" > /dev/null 2>&1; then
            kill -9 "$pid" 2>/dev/null || true
        fi
    done
fi

# Verify no ccbot processes remain
if pgrep -f 'bin/ccbot' > /dev/null 2>&1; then
    echo "Warning: ccbot processes still running after cleanup"
    pgrep -af 'bin/ccbot'
fi

# Rebuild and install ccbot from source
echo "Building ccbot from source..."
if uv tool install "${PROJECT_DIR}" --force --reinstall 2>&1; then
    echo "Build successful."
else
    echo "Error: build failed"
    exit 1
fi

# Brief pause to let the shell settle
sleep 1

# Start ccbot via launchd (preferred) or tmux fallback
if [ -f "$LAUNCHD_PLIST" ]; then
    echo "Starting ccbot via launchd..."
    launchctl load "$LAUNCHD_PLIST"
    sleep 3
    if launchctl list 2>/dev/null | grep -q "$LAUNCHD_LABEL"; then
        echo "ccbot started via launchd (PID: $(launchctl list | grep "$LAUNCHD_LABEL" | awk '{print $1}'))"
    else
        echo "Warning: launchd failed to start ccbot"
        exit 1
    fi
else
    # Fallback: start in tmux window
    # Check if tmux session and window exist
    if ! tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
        echo "Error: tmux session '$TMUX_SESSION' does not exist"
        exit 1
    fi
    if ! tmux list-windows -t "$TMUX_SESSION" -F '#{window_name}' 2>/dev/null | grep -qx "$TMUX_WINDOW"; then
        echo "Error: window '$TMUX_WINDOW' not found in session '$TMUX_SESSION'"
        exit 1
    fi

    echo "Starting ccbot in $TARGET..."
    tmux send-keys -t "$TARGET" "ccbot" Enter

    sleep 3
    PANE_PID=$(tmux list-panes -t "$TARGET" -F '#{pane_pid}')
    if pstree -a "$PANE_PID" 2>/dev/null | grep -q 'ccbot'; then
        echo "ccbot restarted successfully. Recent logs:"
        echo "----------------------------------------"
        tmux capture-pane -t "$TARGET" -p | tail -20
        echo "----------------------------------------"
    else
        echo "Warning: ccbot may not have started. Pane output:"
        echo "----------------------------------------"
        tmux capture-pane -t "$TARGET" -p | tail -30
        echo "----------------------------------------"
        exit 1
    fi
fi

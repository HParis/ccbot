#!/usr/bin/env bash
set -euo pipefail

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

# Start ccbot via launchd
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
    echo "No launchd plist at ${LAUNCHD_PLIST}."
    echo "Set up a LaunchAgent or start the bot manually:"
    echo "  ccbot                # foreground"
    echo "  nohup ccbot &        # background, logs to nohup.out"
    exit 1
fi

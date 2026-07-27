#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LAUNCHD_LABEL="com.user.ccbot"
LAUNCHD_PLIST="$HOME/Library/LaunchAgents/${LAUNCHD_LABEL}.plist"
LAUNCHD_DOMAIN="gui/$(id -u)"
LAUNCHD_TARGET="${LAUNCHD_DOMAIN}/${LAUNCHD_LABEL}"
MAX_WAIT=10  # seconds to wait for process to exit

# Current PID of the launchd-managed job, or empty when not running.
service_pid() {
    launchctl list 2>/dev/null \
        | awk -v label="$LAUNCHD_LABEL" '$3 == label && $1 ~ /^[0-9]+$/ { print $1 }'
}

OLD_PID="$(service_pid)"

# --- Stop ALL running ccbot instances ---

# 1. Stop launchd-managed instance (if any).
# `bootout` (not the legacy `unload`) is what actually removes the job from
# the gui domain. With the job still bootstrapped, KeepAlive respawns the
# process the moment we kill it below, and the later `load` then fails with
# a bogus "Input/output error" even though the bot is running fine.
if launchctl print "$LAUNCHD_TARGET" >/dev/null 2>&1; then
    echo "Stopping launchd-managed ccbot..."
    launchctl bootout "$LAUNCHD_TARGET" 2>/dev/null \
        || launchctl unload "$LAUNCHD_PLIST" 2>/dev/null \
        || true
    for _ in $(seq "$MAX_WAIT"); do
        launchctl print "$LAUNCHD_TARGET" >/dev/null 2>&1 || break
        sleep 1
    done
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
    # Bootstrap if the job is gone; if something re-bootstrapped it in the
    # meantime (KeepAlive races), kickstart -k restarts it in place. Either
    # way the check below is what decides success — a failed bootstrap on an
    # already-running job is not an error.
    launchctl bootstrap "$LAUNCHD_DOMAIN" "$LAUNCHD_PLIST" 2>/dev/null \
        || launchctl kickstart -k "$LAUNCHD_TARGET" 2>/dev/null \
        || true

    # Success = a live PID that isn't the one we started with.
    NEW_PID=""
    for _ in $(seq "$MAX_WAIT"); do
        NEW_PID="$(service_pid)"
        if [ -n "$NEW_PID" ] && [ "$NEW_PID" != "$OLD_PID" ]; then
            break
        fi
        sleep 1
    done

    if [ -n "$NEW_PID" ] && [ "$NEW_PID" != "$OLD_PID" ]; then
        echo "ccbot started via launchd (PID: $NEW_PID)"
    else
        echo "Error: launchd did not start ccbot (PID: ${NEW_PID:-none})"
        echo "Diagnose with: launchctl print $LAUNCHD_TARGET"
        exit 1
    fi
else
    echo "No launchd plist at ${LAUNCHD_PLIST}."
    echo "Set up a LaunchAgent or start the bot manually:"
    echo "  ccbot                # foreground"
    echo "  nohup ccbot &        # background, logs to nohup.out"
    exit 1
fi

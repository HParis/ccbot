#!/usr/bin/env bash
# Rotate ccbot's launchd stderr/stdout logs using copytruncate semantics.
#
# launchd holds an open file descriptor on these logs (StandardErrorPath /
# StandardOutPath), so a rename+create rotation would leave launchd writing to
# the renamed inode while the fresh file stays empty.  We instead gzip a
# snapshot and TRUNCATE THE ORIGINAL IN PLACE (`: > file`) — same inode, so
# launchd's fd keeps working and new logs land in the now-empty file.
#
# Driven by the com.user.ccbot.logrotate LaunchAgent (daily).  Safe to run
# manually any time: `bash scripts/rotate-logs.sh`.
set -euo pipefail

LOG_DIR="${CCBOT_DIR:-$HOME/.ccbot}"
MAX_BYTES=$((20 * 1024 * 1024)) # rotate a log once it exceeds 20 MB
KEEP=5                          # keep this many gzipped archives per log

rotate_one() {
    local log="$1"
    [ -f "$log" ] || return 0

    local size
    size=$(stat -f%z "$log" 2>/dev/null || echo 0)
    [ "$size" -gt "$MAX_BYTES" ] || return 0

    local ts
    ts=$(date +%Y%m%d-%H%M%S)
    gzip -c "$log" >"${log}.${ts}.gz"
    : >"$log" # truncate in place — preserves inode / launchd fd

    # Prune archives beyond the most recent $KEEP.
    # shellcheck disable=SC2012
    ls -1t "${log}".*.gz 2>/dev/null | tail -n +$((KEEP + 1)) |
        while IFS= read -r old; do rm -f "$old"; done
    echo "rotated $log (was $((size / 1024 / 1024)) MB) -> ${log}.${ts}.gz"
}

rotate_one "$LOG_DIR/ccbot.stderr.log"
rotate_one "$LOG_DIR/ccbot.stdout.log"

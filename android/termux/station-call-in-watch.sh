#!/data/data/com.termux/files/usr/bin/bash
# Claim call-in ring triggers and hand each one to the recorder script.
# Key Mapper cannot hold Termux's RUN_COMMAND permission, so the long press
# only touches a file inside the station root; this loop owns the hand-off.

set -u

CONFIG="$HOME/.config/audience-of-one/phone.env"
[ -r "$CONFIG" ] && . "$CONFIG"

ROOT="${STATION_PHONE_ROOT:-$HOME/storage/shared/Download/AudienceOfOne}"
RING="$ROOT/${STATION_CALLIN_RING:-call-in/ring}"
RECORD="${STATION_CALLIN_RECORD_SCRIPT:-$HOME/.local/lib/audience-of-one/station-call-in-record.sh}"
POLL_SECONDS="${STATION_CALLIN_POLL_SECONDS:-1}"
STATE="$HOME/.local/state/audience-of-one-phone"
LOG="$STATE/call-in.log"

log() { printf '%s %s\n' "$(date '+%H:%M:%S')" "$1" >> "$LOG"; }

mkdir -p "$(dirname "$RING")" "$STATE"
trap 'exit 0' INT TERM

log "call-in watcher started (pid $$)"
while true; do
    if [ -e "$RING" ]; then
        claim="$RING.claimed.$$"
        if mv "$RING" "$claim" 2>/dev/null; then
            rm -f "$claim"
            if [ -x "$RECORD" ]; then
                log "ring claimed"
                nohup "$RECORD" >> "$LOG" 2>&1 &
            else
                log "recorder script missing or not executable: $RECORD"
            fi
        fi
    fi
    sleep "$POLL_SECONDS"
done

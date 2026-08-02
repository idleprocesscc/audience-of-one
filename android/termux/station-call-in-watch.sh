#!/data/data/com.termux/files/usr/bin/bash
# Claim call-in ring triggers and hand each one to the recorder script.
# Key Mapper cannot hold Termux's RUN_COMMAND permission, so the long press
# only touches a file inside the station root; this loop owns the hand-off.

set -u

CONFIG="$HOME/.config/audience-of-one/phone.env"
[ -r "$CONFIG" ] && . "$CONFIG"

ROOT="${STATION_PHONE_ROOT:-$HOME/storage/shared/Download/AudienceOfOne}"
RING="$ROOT/${STATION_CALLIN_RING:-call-in/ring}"
OUTBOX="$ROOT/${STATION_CALLIN_OUTBOX:-call-in-outbox}"
WORK_DIR="$ROOT/${STATION_CALLIN_WORK:-call-in-work}"
RECORD="${STATION_CALLIN_RECORD_SCRIPT:-$HOME/.local/lib/audience-of-one/station-call-in-record.sh}"
POLL_SECONDS="${STATION_CALLIN_POLL_SECONDS:-1}"
STATE="$HOME/.local/state/audience-of-one-phone"
LOG="$STATE/call-in.log"
RECORD_LOCK="$STATE/call-in-record.lock"
B64_COLUMNS=1024

log() { printf '%s %s\n' "$(date '+%H:%M:%S')" "$1" >> "$LOG"; }

mkdir -p "$(dirname "$RING")" "$OUTBOX" "$WORK_DIR" "$STATE"
trap 'exit 0' INT TERM

stage_direct_clip() {
    clip="$1"
    name="$(basename "$clip" .m4a)"
    claimed="$WORK_DIR/.$name.claimed.$$"
    part="$OUTBOX/$name.b64.part.$$"
    if ! mv "$clip" "$claimed" 2>/dev/null; then
        return
    fi
    if ! base64 -w "$B64_COLUMNS" "$claimed" > "$part" 2>>"$LOG"; then
        base64 "$claimed" | fold -w "$B64_COLUMNS" > "$part" 2>>"$LOG"
    fi
    printf 'END.\n' >> "$part"
    mv "$part" "$OUTBOX/$name.b64"
    rm -f "$claimed"
    log "staged direct call-in $name"
}

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
    # A normal VIEW intent from Key Mapper records straight into call-in-work.
    # The shell-command route keeps using the lock and packages its own clip.
    if ! { [ -f "$RECORD_LOCK" ] && kill -0 "$(cat "$RECORD_LOCK" 2>/dev/null)" 2>/dev/null; }; then
        for clip in "$WORK_DIR"/*.m4a; do
            [ -s "$clip" ] || continue
            stage_direct_clip "$clip"
        done
    fi
    sleep "$POLL_SECONDS"
done

#!/data/data/com.termux/files/usr/bin/bash
# Record one call-in clip through the focus-helper recorder service, then
# stage it as wrapped base64 with an END. sentinel so the Mac can page it
# through android_read_file. The raw clip is deleted after staging.

set -u

CONFIG="$HOME/.config/audience-of-one/phone.env"
[ -r "$CONFIG" ] && . "$CONFIG"

ROOT="${STATION_PHONE_ROOT:-$HOME/.local/share/audience-of-one-phone/transport}"
MEDIA_ROOT="${STATION_CALLIN_MEDIA_ROOT:-$HOME/storage/shared/Download/AudienceOfOne}"
export STATION_PHONE_EVENT_SOCKET="${STATION_PHONE_EVENT_SOCKET:-$HOME/.local/state/audience-of-one-phone/events.sock}"
OUTBOX="$ROOT/${STATION_CALLIN_OUTBOX:-call-in-outbox}"
WORK_DIR="${STATION_CALLIN_WORK:-call-in-work}"
RECORD_SECONDS="${STATION_CALLIN_SECONDS:-8}"
B64_COLUMNS=1024
RECORD_PACKAGE="io.github.audienceofone.djcontrol"
LOCK="$HOME/.local/state/audience-of-one-phone/call-in-record.lock"
LOG="$HOME/.local/state/audience-of-one-phone/call-in.log"
M4A=""
B64_PART=""

log() { printf '%s %s\n' "$(date '+%H:%M:%S')" "$1" >> "$LOG"; }

mkdir -p "$OUTBOX" "$MEDIA_ROOT/$WORK_DIR" "$(dirname "$LOCK")"

# single-flight: a second ring while recording is ignored
if [ -f "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
    exit 0
fi
printf '%s\n' "$$" > "$LOCK"
cleanup() {
    rm -f "$LOCK"
    [ -n "$M4A" ] && rm -f "$M4A"
    [ -n "$B64_PART" ] && rm -f "$B64_PART"
}
trap cleanup EXIT

TS=$(date +%s%3N 2>/dev/null)
case "$TS" in *N*|"") TS="$(date +%s)000";; esac
M4A="$MEDIA_ROOT/$WORK_DIR/$TS.m4a"

# The recorder writes relative to shared Downloads; derive its root from ours.
case "$MEDIA_ROOT" in
    */Download/*) RECORD_ROOT="${MEDIA_ROOT##*/Download/}" ;;
    *) RECORD_ROOT="AudienceOfOne" ;;
esac

# cue: double buzz = the microphone is about to go live
termux-vibrate -d 120 >/dev/null 2>&1; sleep 0.2
termux-vibrate -d 120 >/dev/null 2>&1 || true

URI="djrecord://record?request_id=callin-$TS&output=$WORK_DIR/$TS.m4a"
URI="$URI&root=$RECORD_ROOT&duration_seconds=$RECORD_SECONDS"
if ! /system/bin/am start -W -a android.intent.action.VIEW \
    -n "$RECORD_PACKAGE/.RecordActivity" -d "$URI" >/dev/null 2>>"$LOG"; then
    command -v termux-open-url >/dev/null 2>&1 && \
        termux-open-url "$URI" >/dev/null 2>>"$LOG"
fi

# The service publishes the file only after the recording is complete, so
# appearance is the finished receipt (record.state carries the running one).
deadline=$((RECORD_SECONDS + 25))
count=0
while [ ! -s "$M4A" ]; do
    count=$((count + 1))
    if [ "$count" -gt "$deadline" ]; then
        log "clip never appeared: $M4A"
        termux-vibrate -d 600 >/dev/null 2>&1 || true  # one very long buzz = failed
        exit 1
    fi
    sleep 1
done
log "recorded $M4A"

# Keep the file line-addressable for android_read_file without forcing the
# Mac to pull one eight-second clip in dozens of tiny MCP round trips.
B64="$OUTBOX/$TS.b64"
B64_PART="$B64.part"
if ! base64 -w "$B64_COLUMNS" "$M4A" > "$B64_PART" 2>>"$LOG"; then
    base64 "$M4A" | fold -w "$B64_COLUMNS" > "$B64_PART" 2>>"$LOG"
fi
printf 'END.\n' >> "$B64_PART"
mv "$B64_PART" "$B64"
B64_PART=""
rm -f "$M4A"
M4A=""
log "staged $B64"
python3 - "$TS" <<'PY' >/dev/null 2>&1 || true
import os, socket, sys
target = os.path.expanduser(os.environ.get(
    "STATION_PHONE_EVENT_SOCKET", "~/.local/state/audience-of-one-phone/events.sock"
))
client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
try:
    client.sendto(("call-in:" + sys.argv[1]).encode(), target)
finally:
    client.close()
PY

# confirmation cue: one long buzz = the clip is on its way
termux-vibrate -d 300 >/dev/null 2>&1 || true

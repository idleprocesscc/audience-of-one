#!/data/data/com.termux/files/usr/bin/bash
# Receipt-driven phone voice player for Audience of One.

set -u

ROOT="${STATION_PHONE_ROOT:-$HOME/storage/shared/Download/AudienceOfOne}"
INBOX="$ROOT/station-inbox"
MUSIC_INBOX="$ROOT/station-music-inbox"
ACKS="$ROOT/station-acks"
CLAIMED="$ROOT/.claimed"
MUSIC_CLAIMED="$ROOT/.claimed-music"
STALE="$ROOT/.stale"
WORK="$HOME/.local/state/audience-of-one-phone/work"
LOG="$HOME/.local/state/audience-of-one-phone/player.log"
HEALTH="$ROOT/phone-health.json"
PIDFILE="$HOME/.local/state/audience-of-one-phone/player.pid"
LOCKDIR="$HOME/.local/state/audience-of-one-phone/player.lock"
FOCUS_SERVER="$HOME/.local/lib/audience-of-one/station_focus_receipt.py"
FOCUS_STATE="$HOME/.local/state/audience-of-one-phone/focus.state"
FOCUS_PIDFILE="$HOME/.local/state/audience-of-one-phone/focus.pid"
FOCUS_PACKAGE="io.github.audienceofone.djcontrol"
MUSIC_HELPER="$HOME/.local/lib/audience-of-one/station_phone_mpv.py"
MUSIC_SOCKET="$HOME/.local/state/audience-of-one-phone/music-mpv.sock"
MUSIC_PIDFILE="$HOME/.local/state/audience-of-one-phone/music-mpv.pid"
POLL_SECONDS="${STATION_PHONE_POLL_SECONDS:-0.5}"
PLAYBACK_SETTLE_SECONDS="${STATION_PHONE_PLAYBACK_SETTLE_SECONDS:-0.25}"

mkdir -p "$INBOX" "$MUSIC_INBOX" "$ACKS" "$CLAIMED" "$MUSIC_CLAIMED" \
    "$STALE" "$WORK" \
    "$(dirname "$LOG")" "$(dirname "$PIDFILE")"

log() { printf '%s %s\n' "$(date '+%H:%M:%S')" "$1" >> "$LOG"; }

safe_id() { printf '%s' "$1" | tr -cd 'A-Za-z0-9._-'; }

process_matches() {
    local pid="$1" pattern="$2" command=""
    [ -r "/proc/$pid/cmdline" ] && command=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)
    printf '%s' "$command" | grep -q "$pattern"
}

write_health() {
    local state="$1" detail="${2:-}" temporary
    temporary=$(mktemp "$HEALTH.XXXXXX") || return 0
    printf '{"state":"%s","pid":%s,"updated_at":%s,"detail":"%s"}\n' \
        "$state" "$$" "$(date +%s)" "$(printf '%s' "$detail" | tr -cd 'A-Za-z0-9._ -')" \
        > "$temporary"
    mv -f "$temporary" "$HEALTH"
}

write_ack() {
    local item event detail
    item=$(safe_id "$1")
    event=$(safe_id "$2")
    detail=$(printf '%s' "${3:-}" | tr -cd 'A-Za-z0-9._ -')
    [ -n "$item" ] && [ -n "$event" ] || return 1
    printf '{"item":"%s","event":"%s","at":%s,"detail":"%s"}\n' \
        "$item" "$event" "$(date +%s)" "$detail" >> "$ACKS/$item.jsonl"
    log "ack $item $event${detail:+ ($detail)}"
}

manifest_field() {
    sed -n "s/^$2=//p" "$1" 2>/dev/null | head -1
}

music_process_alive() {
    local pid=""
    pid=$(cat "$MUSIC_PIDFILE" 2>/dev/null)
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && \
        process_matches "$pid" 'mpv.*music-mpv.sock'
}

stop_music_player() {
    local pid=""
    pid=$(cat "$MUSIC_PIDFILE" 2>/dev/null)
    if music_process_alive; then
        python3 "$MUSIC_HELPER" --socket "$MUSIC_SOCKET" quit >/dev/null 2>&1 || \
            kill "$pid" 2>/dev/null || true
    fi
    rm -f "$MUSIC_PIDFILE" "$MUSIC_SOCKET"
}

ensure_music_player() {
    local count pid
    if music_process_alive && [ -S "$MUSIC_SOCKET" ]; then
        return 0
    fi
    stop_music_player
    rm -f "$MUSIC_SOCKET"
    mpv --idle=yes --no-terminal --no-video --force-window=no --audio-display=no \
        --volume=100 --input-ipc-server="$MUSIC_SOCKET" \
        </dev/null >/dev/null 2>&1 &
    pid=$!
    printf '%s\n' "$pid" > "$MUSIC_PIDFILE"
    for count in $(seq 1 40); do
        [ -S "$MUSIC_SOCKET" ] && kill -0 "$pid" 2>/dev/null && return 0
        sleep 0.1
    done
    return 1
}

music_snapshot() {
    music_process_alive && [ -S "$MUSIC_SOCKET" ] || return 1
    python3 "$MUSIC_HELPER" --socket "$MUSIC_SOCKET" snapshot 2>/dev/null
}

music_fade() {
    music_process_alive && [ -S "$MUSIC_SOCKET" ] || return 1
    python3 "$MUSIC_HELPER" --socket "$MUSIC_SOCKET" fade "$1" "$2" \
        >/dev/null 2>&1
}

open_focus_uri() {
    local uri="$1"
    if /system/bin/am start -W -a android.intent.action.VIEW \
        -n "$FOCUS_PACKAGE/.FocusActivity" -d "$uri" >/dev/null 2>>"$LOG"; then
        return 0
    fi
    command -v termux-open-url >/dev/null 2>&1 && \
        termux-open-url "$uri" >/dev/null 2>>"$LOG"
}

start_focus_server() {
    local old_pid=""
    command -v python3 >/dev/null 2>&1 || return 1
    [ -s "$FOCUS_SERVER" ] || return 1
    old_pid=$(cat "$FOCUS_PIDFILE" 2>/dev/null)
    if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
        kill "$old_pid" 2>/dev/null || true
    fi
    rm -f "$FOCUS_PIDFILE" "$FOCUS_STATE"
    STATION_FOCUS_STATE="$FOCUS_STATE" python3 "$FOCUS_SERVER" >> "$LOG" 2>&1 &
    FOCUS_PID=$!
    printf '%s\n' "$FOCUS_PID" > "$FOCUS_PIDFILE"
    sleep 0.2
    kill -0 "$FOCUS_PID" 2>/dev/null
}

focus_state() { [ -s "$FOCUS_STATE" ] && cat "$FOCUS_STATE"; }

focus_hold() {
    local item="$1" duration="$2" request_id duration_ms state
    [ -n "${FOCUS_PID:-}" ] && kill -0 "$FOCUS_PID" 2>/dev/null || return 1
    request_id="$(safe_id "$item")-focus"
    duration_ms=$(awk -v value="$duration" 'BEGIN {
        value=(value+2.0)*1000; if (value<2000) value=2000;
        if (value>120000) value=120000; printf "%d", value+0.5
    }')
    open_focus_uri "djfocus://hold?request_id=$request_id&duration_ms=$duration_ms" || return 1
    for _ in $(seq 1 30); do
        state=$(focus_state || true)
        if printf '%s' "$state" | grep -Fq "request_id=$request_id" && \
           printf '%s' "$state" | grep -Fq 'state=held' && \
           printf '%s' "$state" | grep -Fq 'result=1'; then
            return 0
        fi
        printf '%s' "$state" | grep -Fq "request_id=$request_id state=failed" && return 1
        sleep 0.1
    done
    return 1
}

focus_release() {
    local item="$1" request_id state
    request_id="$(safe_id "$item")-focus"
    open_focus_uri "djfocus://release?request_id=$request_id" || return 1
    for _ in $(seq 1 30); do
        state=$(focus_state || true)
        if printf '%s' "$state" | grep -Fq "request_id=$request_id" && \
           printf '%s' "$state" | grep -Eq 'state=(released|expired|destroyed)'; then
            return 0
        fi
        sleep 0.1
    done
    return 1
}

probe_duration() {
    local value
    value=$(mpv --no-config --frames=0 --term-playing-msg='${=duration}' "$1" \
        2>/dev/null | tail -1)
    if [[ "$value" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
        printf '%s\n' "$value"
    else
        printf '15\n'
    fi
}

play_clip() {
    local path="$1" item="$2" duck_enabled="$3" duration whole focus_held=0
    local duck_percent="${4:-35}" fade_down="${5:-0.8}" fade_up="${6:-1.0}"
    local music_restore="" snapshot=""
    duration=$(probe_duration "$path")
    whole=${duration%%.*}
    whole=$((10#$whole))
    if [ "$duck_enabled" = 1 ]; then
        snapshot=$(music_snapshot 2>/dev/null || true)
        music_restore=$(printf '%s' "$snapshot" | python3 -c \
            'import json,sys; d=json.load(sys.stdin); print(d.get("volume_percent", "") if d.get("ok") and d.get("is_playing") else "")' \
            2>/dev/null || true)
        if [ -n "$music_restore" ] && music_fade "$duck_percent" "$fade_down"; then
            write_ack "$item" duck_started mpv-fader
        elif focus_hold "$item" "$duration"; then
            focus_held=1
            write_ack "$item" duck_started audio-focus-may-duck
        else
            write_ack "$item" duck_failed focus-unavailable
        fi
    fi

    if command -v termux-media-player >/dev/null 2>&1; then
        if ! termux-media-player play "$path" >> "$LOG" 2>&1; then
            [ "$focus_held" = 1 ] && focus_release "$item" || true
            write_ack "$item" voice_failed media-player-start
            return 1
        fi
        write_ack "$item" voice_started termux-media-player
        local deadline seen=0 info
        deadline=$(( $(date +%s) + whole + 10 ))
        while [ "$(date +%s)" -le "$deadline" ]; do
            info=$(termux-media-player info 2>>"$LOG" || true)
            if printf '%s' "$info" | grep -qi 'playing'; then
                seen=1
                sleep 0.2
                continue
            fi
            [ "$seen" = 1 ] && break
            sleep 0.2
        done
        if [ "$seen" != 1 ] || [ "$(date +%s)" -gt "$deadline" ]; then
            [ "$focus_held" = 1 ] && focus_release "$item" || true
            write_ack "$item" voice_failed media-player-timeout
            return 1
        fi
    else
        mpv --no-video --really-quiet "$path" &
        local player_pid=$!
        sleep 0.15
        if ! kill -0 "$player_pid" 2>/dev/null; then
            wait "$player_pid" 2>/dev/null || true
            [ "$focus_held" = 1 ] && focus_release "$item" || true
            write_ack "$item" voice_failed mpv-start
            return 1
        fi
        write_ack "$item" voice_started mpv
        if ! wait "$player_pid"; then
            [ "$focus_held" = 1 ] && focus_release "$item" || true
            write_ack "$item" voice_failed mpv-exit
            return 1
        fi
    fi

    sleep "$PLAYBACK_SETTLE_SECONDS"
    if [ "$focus_held" = 1 ]; then
        if focus_release "$item"; then
            write_ack "$item" duck_finished audio-focus-released
        else
            write_ack "$item" duck_release_failed focus-release-timeout
        fi
    elif [ -n "$music_restore" ]; then
        if music_fade "$music_restore" "$fade_up"; then
            write_ack "$item" duck_finished mpv-fader
        else
            write_ack "$item" duck_release_failed mpv-fader
        fi
    fi
    write_ack "$item" voice_finished phone-player
}

process_one() {
    local source="$1" name claimed manifest expected_version expected_size expected_sha ready duck
    local decoded actual_size actual_sha duck_percent fade_down fade_up
    name=$(safe_id "$(basename "$source" .b64)")
    [ -n "$name" ] || return 0
    [ "$(tail -n 1 "$source" 2>/dev/null)" = "END." ] || return 0
    if grep -q '"event":"voice_finished"' "$ACKS/$name.jsonl" 2>/dev/null; then
        rm -f "$source" "$INBOX/$name.integrity"
        return 0
    fi
    claimed="$CLAIMED/$name.b64"
    manifest="$CLAIMED/$name.integrity"
    mv "$source" "$claimed" 2>/dev/null || return 0
    if ! mv "$INBOX/$name.integrity" "$manifest" 2>/dev/null; then
        write_ack "$name" voice_failed integrity-manifest
        mv -f "$claimed" "$STALE/$name.b64.no-manifest"
        return 0
    fi
    expected_version=$(manifest_field "$manifest" version)
    expected_size=$(manifest_field "$manifest" size)
    expected_sha=$(manifest_field "$manifest" sha256)
    duck=$(manifest_field "$manifest" duck)
    duck_percent=$(manifest_field "$manifest" duck_percent)
    fade_down=$(manifest_field "$manifest" fade_down_seconds)
    fade_up=$(manifest_field "$manifest" fade_up_seconds)
    ready=$(manifest_field "$manifest" ready)
    if [ "$expected_version" != 1 ] || [ "$ready" != 1 ] || \
       ! [[ "$expected_size" =~ ^[0-9]+$ ]] || ! [[ "$expected_sha" =~ ^[0-9a-f]{64}$ ]]; then
        write_ack "$name" voice_failed integrity-manifest
        mv -f "$claimed" "$STALE/$name.b64.bad-manifest"
        mv -f "$manifest" "$STALE/$name.integrity.bad-manifest"
        return 0
    fi
    [ "$duck" = 0 ] || duck=1
    [[ "$duck_percent" =~ ^[0-9]+$ ]] || duck_percent=35
    [ "$duck_percent" -le 100 ] || duck_percent=35
    [[ "$fade_down" =~ ^[0-9]+([.][0-9]+)?$ ]] || fade_down=0.8
    [[ "$fade_up" =~ ^[0-9]+([.][0-9]+)?$ ]] || fade_up=1.0
    decoded="$WORK/$name.mp3"
    sed '/^END\.$/d' "$claimed" | tr -d '\r\n' | base64 -d > "$decoded" 2>>"$LOG" || true
    actual_size=$(wc -c < "$decoded" 2>/dev/null | tr -d '[:space:]')
    actual_sha=$(sha256sum "$decoded" 2>/dev/null | awk '{print $1}')
    if [ "$actual_size" != "$expected_size" ] || [ "$actual_sha" != "$expected_sha" ]; then
        write_ack "$name" voice_failed integrity-mismatch
        mv -f "$claimed" "$STALE/$name.b64.integrity"
        mv -f "$manifest" "$STALE/$name.integrity.invalid"
        rm -f "$decoded"
        return 0
    fi
    log "playing $name"
    play_clip "$decoded" "$name" "$duck" "$duck_percent" "$fade_down" "$fade_up" || true
    rm -f "$claimed" "$manifest" "$decoded"
}

process_music_one() {
    local source="$1" name claimed manifest decoded expected_version expected_size
    local expected_sha ready actual_size actual_sha result url_sha progress duration volume
    name=$(safe_id "$(basename "$source" .b64)")
    [ -n "$name" ] || return 0
    [ "$(tail -n 1 "$source" 2>/dev/null)" = "END." ] || return 0
    if grep -q '"event":"track_position"' "$ACKS/$name.jsonl" 2>/dev/null; then
        rm -f "$source" "$MUSIC_INBOX/$name.integrity"
        return 0
    fi
    claimed="$MUSIC_CLAIMED/$name.b64"
    manifest="$MUSIC_CLAIMED/$name.integrity"
    mv "$source" "$claimed" 2>/dev/null || return 0
    if ! mv "$MUSIC_INBOX/$name.integrity" "$manifest" 2>/dev/null; then
        write_ack "$name" track_failed integrity-manifest
        rm -f "$claimed"
        return 0
    fi
    expected_version=$(manifest_field "$manifest" version)
    expected_size=$(manifest_field "$manifest" size)
    expected_sha=$(manifest_field "$manifest" sha256)
    ready=$(manifest_field "$manifest" ready)
    if [ "$expected_version" != 1 ] || [ "$ready" != 1 ] || \
       ! [[ "$expected_size" =~ ^[0-9]+$ ]] || ! [[ "$expected_sha" =~ ^[0-9a-f]{64}$ ]]; then
        write_ack "$name" track_failed integrity-manifest
        rm -f "$claimed" "$manifest"
        return 0
    fi
    decoded="$WORK/$name.music.json"
    sed '/^END\.$/d' "$claimed" | tr -d '\r\n' | base64 -d > "$decoded" 2>>"$LOG" || true
    actual_size=$(wc -c < "$decoded" 2>/dev/null | tr -d '[:space:]')
    actual_sha=$(sha256sum "$decoded" 2>/dev/null | awk '{print $1}')
    rm -f "$claimed" "$manifest"
    if [ "$actual_size" != "$expected_size" ] || [ "$actual_sha" != "$expected_sha" ]; then
        write_ack "$name" track_failed integrity-mismatch
        rm -f "$decoded"
        return 0
    fi
    if ! ensure_music_player; then
        write_ack "$name" track_failed mpv-start
        rm -f "$decoded"
        return 0
    fi
    result=$(python3 "$MUSIC_HELPER" --socket "$MUSIC_SOCKET" load "$decoded" 2>/dev/null) || {
        stop_music_player
        if ensure_music_player; then
            result=$(python3 "$MUSIC_HELPER" --socket "$MUSIC_SOCKET" load "$decoded" 2>/dev/null) || result=""
        fi
    }
    rm -f "$decoded"
    url_sha=$(printf '%s' "$result" | python3 -c \
        'import json,sys; d=json.load(sys.stdin); print(d.get("url_sha256", "") if d.get("ok") else "")' \
        2>/dev/null || true)
    progress=$(printf '%s' "$result" | python3 -c \
        'import json,sys; d=json.load(sys.stdin); print(d.get("progress_ms", ""))' \
        2>/dev/null || true)
    duration=$(printf '%s' "$result" | python3 -c \
        'import json,sys; d=json.load(sys.stdin); print(d.get("duration_ms", ""))' \
        2>/dev/null || true)
    volume=$(printf '%s' "$result" | python3 -c \
        'import json,sys; d=json.load(sys.stdin); print(d.get("volume_percent", ""))' \
        2>/dev/null || true)
    if ! [[ "$url_sha" =~ ^[0-9a-f]{64}$ ]] || ! [[ "$progress" =~ ^[0-9]+$ ]] || \
       ! [[ "$duration" =~ ^[0-9]+$ ]]; then
        write_ack "$name" track_failed mpv-load
        return 0
    fi
    write_ack "$name" track_started "url-$url_sha"
    write_ack "$name" track_position \
        "url-$url_sha progress-$progress duration-$duration volume-${volume:-100} repeat-off"
}

self_test() {
    command -v base64 >/dev/null 2>&1 || { echo "missing base64"; return 1; }
    command -v sha256sum >/dev/null 2>&1 || { echo "missing sha256sum"; return 1; }
    command -v mpv >/dev/null 2>&1 || { echo "missing mpv"; return 1; }
    command -v pulseaudio >/dev/null 2>&1 || { echo "missing pulseaudio"; return 1; }
    [ -s "$MUSIC_HELPER" ] || { echo "missing station_phone_mpv.py"; return 1; }
    echo "phone player self-test: ok"
}

if [ "${1:-}" = "--self-test" ]; then
    self_test
    exit $?
fi

if ! mkdir "$LOCKDIR" 2>/dev/null; then
    old_pid=$(cat "$LOCKDIR/pid" 2>/dev/null)
    if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null && \
       process_matches "$old_pid" station-phone-player.sh; then
        exit 0
    fi
    rm -rf "$LOCKDIR"
    mkdir "$LOCKDIR" || exit 2
fi
printf '%s\n' "$$" > "$LOCKDIR/pid"
printf '%s\n' "$$" > "$PIDFILE"

# Claimed files are evidence of a prior uncertain playout. Never replay them.
find "$CLAIMED" -maxdepth 1 -type f -exec mv -f {} "$STALE/" \; 2>/dev/null || true
find "$MUSIC_CLAIMED" -maxdepth 1 -type f -delete 2>/dev/null || true
find "$ACKS" "$STALE" -type f -mtime +7 -delete 2>/dev/null || true
find "$WORK" -type f -mtime +1 -delete 2>/dev/null || true
pulseaudio --start --exit-idle-time=-1 >> "$LOG" 2>&1 || true

FOCUS_PID=""
start_focus_server || log "audio focus helper unavailable; voice remains receipted"
write_health running started

cleanup() {
    local code=$?
    [ -n "$FOCUS_PID" ] && kill "$FOCUS_PID" 2>/dev/null || true
    stop_music_player
    [ "$(cat "$PIDFILE" 2>/dev/null)" = "$$" ] && rm -f "$PIDFILE"
    if [ "$(cat "$LOCKDIR/pid" 2>/dev/null)" = "$$" ]; then
        rm -f "$LOCKDIR/pid"
        rmdir "$LOCKDIR" 2>/dev/null || true
    fi
    write_health stopped "exit-$code"
}
trap cleanup EXIT
trap 'exit 0' INT TERM

while true; do
    for candidate in "$INBOX"/*.b64; do
        [ -e "$candidate" ] || continue
        process_one "$candidate"
    done
    for candidate in "$MUSIC_INBOX"/*.b64; do
        [ -e "$candidate" ] || continue
        process_music_one "$candidate"
    done
    write_health running
    sleep "$POLL_SECONDS"
done

#!/data/data/com.termux/files/usr/bin/bash
# Receipt-driven phone voice player for Audience of One.

set -u

ROOT="${STATION_PHONE_ROOT:-$HOME/.local/share/audience-of-one-phone/transport}"
INBOX="$ROOT/station-inbox"
MUSIC_INBOX="$ROOT/station-music-inbox"
ACKS="$ROOT/station-acks"
CLAIMED="$ROOT/.claimed"
MUSIC_CLAIMED="$ROOT/.claimed-music"
STALE="$ROOT/.stale"
EXPIRED="$ROOT/.expired"
FADER_INBOX="$ROOT/station-fader-inbox"
FADER_CLAIMED="$ROOT/.claimed-fader"
FADER_ACKS="$ROOT/station-fader-acks"
FADER_STATE="$HOME/.local/state/audience-of-one-phone/fader.state"
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
EVENT_FIFO="${STATION_PHONE_EVENT_FIFO:-$HOME/.local/state/audience-of-one-phone/player.events}"
HEARTBEAT_SECONDS="${STATION_PHONE_HEARTBEAT_SECONDS:-60}"
SAFETY_RESCAN_SECONDS="${STATION_PHONE_RESCAN_SECONDS:-900}"
PLAYBACK_SETTLE_SECONDS="${STATION_PHONE_PLAYBACK_SETTLE_SECONDS:-0.25}"

mkdir -p "$INBOX" "$MUSIC_INBOX" "$ACKS" "$CLAIMED" "$MUSIC_CLAIMED" \
    "$STALE" "$EXPIRED" "$FADER_INBOX" "$FADER_CLAIMED" "$FADER_ACKS" "$WORK" \
    "$(dirname "$LOG")" "$(dirname "$PIDFILE")"
if [ -e "$EVENT_FIFO" ] && [ ! -p "$EVENT_FIFO" ]; then
    rm -f "$EVENT_FIFO"
fi
[ -p "$EVENT_FIFO" ] || mkfifo "$EVENT_FIFO"
exec 9<>"$EVENT_FIFO"

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

tombstone_capsule() {
    local name="$1" event="$2" detail="$3"
    printf 'event=%s\nat=%s\ndetail=%s\n' "$event" "$(date +%s)" "$detail" \
        > "$EXPIRED/$name"
    rm -f "$INBOX/$name.b64" "$INBOX/$name.capsule" "$INBOX/$name.integrity"
    write_ack "$name" "$event" "$detail"
}

expire_due_capsules() {
    local capsule name expires now
    now=$(date +%s)
    for capsule in "$INBOX"/*.capsule; do
        [ -e "$capsule" ] || continue
        [ "$(manifest_field "$capsule" ready)" = 1 ] || continue
        name=$(safe_id "$(basename "$capsule" .capsule)")
        expires=$(manifest_field "$capsule" expires_at)
        if ! [[ "$expires" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
            tombstone_capsule "$name" voice_failed capsule-invalid
        elif awk -v now="$now" -v expires="$expires" \
            'BEGIN { exit !(now > expires) }'; then
            tombstone_capsule "$name" voice_expired capsule-expired
        fi
    done
}

capsule_allows_playout() {
    local name="$1" capsule="$INBOX/$1.capsule" expires now
    if [ -f "$EXPIRED/$name" ]; then
        rm -f "$INBOX/$name.b64" "$capsule" "$INBOX/$name.integrity"
        return 1
    fi
    [ -f "$capsule" ] || return 0
    [ "$(manifest_field "$capsule" ready)" = 1 ] || return 1
    expires=$(manifest_field "$capsule" expires_at)
    if ! [[ "$expires" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
        tombstone_capsule "$name" voice_failed capsule-invalid
        return 1
    fi
    now=$(date +%s)
    if awk -v now="$now" -v expires="$expires" 'BEGIN { exit !(now > expires) }'; then
        tombstone_capsule "$name" voice_expired capsule-expired
        return 1
    fi
}

phone_music_volume() {
    command -v termux-volume >/dev/null 2>&1 || return 1
    termux-volume 2>/dev/null | awk '
        /"stream"[[:space:]]*:[[:space:]]*"music"/ { music=1; next }
        music && /"volume"[[:space:]]*:/ && ! /max_volume/ {
            line=$0; gsub(/[^0-9]/, "", line); volume=line; next
        }
        music && /"max_volume"[[:space:]]*:/ {
            line=$0; gsub(/[^0-9]/, "", line)
            if (volume != "" && line != "") { print volume, line; exit }
        }
    '
}

phone_volume_percent() {
    awk -v value="$1" -v maximum="$2" \
        'BEGIN { printf "%d", maximum > 0 ? (value*100/maximum)+0.5 : 0 }'
}

phone_fade_raw() {
    local from="$1" target="$2" seconds="$3" steps delay step value duration_ms actual
    local snapshot maximum target_percent
    duration_ms=$(awk -v s="$seconds" 'BEGIN { printf "%d", s*1000+0.5 }')
    snapshot=$(phone_music_volume 2>/dev/null) || snapshot=""
    maximum=$(printf '%s\n' "$snapshot" | awk '{print $2}')
    if [ -n "$maximum" ] && [ "$maximum" -gt 0 ] 2>/dev/null; then
        target_percent=$(phone_volume_percent "$target" "$maximum")
    else
        target_percent=-1
    fi
    if /system/bin/am start --user 0 -W --activity-no-animation \
        -n "$FOCUS_PACKAGE/.FaderActivity" \
        --ei target_percent "$target_percent" --ei duration_ms "$duration_ms" \
        >/dev/null 2>>"$LOG"; then
        delay=$(awk -v s="$seconds" 'BEGIN { print s+0.25 }')
        sleep "$delay"
        actual=$(phone_music_volume 2>/dev/null | awk '{print $1}')
        if [ -n "$actual" ] && awk -v a="$actual" -v t="$target" \
            'BEGIN { d=a-t; if(d<0)d=-d; exit !(d<=1) }'; then
            return 0
        fi
    fi
    steps=$(awk -v s="$seconds" 'BEGIN { n=int(s*6+0.5); if(n<1)n=1; if(n>30)n=30; print n }')
    delay=$(awk -v s="$seconds" -v n="$steps" 'BEGIN { print n ? s/n : 0 }')
    for ((step=1; step<=steps; step++)); do
        value=$(awk -v f="$from" -v t="$target" -v i="$step" -v n="$steps" \
            'BEGIN { printf "%d", f+(t-f)*i/n+0.5 }')
        termux-volume music "$value" >/dev/null 2>&1 || return 1
        [ "$step" -eq "$steps" ] || sleep "$delay"
    done
}

write_fader_ack() {
    local id="$1" event="$2" ok="$3" from="$4" to="$5" original="$6" detail="$7"
    [[ "$from" =~ ^[0-9]+$ ]] || from=0
    [[ "$to" =~ ^[0-9]+$ ]] || to=0
    [[ "$original" =~ ^[0-9]+$ ]] || original=0
    printf '{"id":"%s","event":"%s","ok":%s,"at":%s,"from_percent":%s,"to_percent":%s,"original_percent":%s,"detail":"%s"}\n' \
        "$id" "$event" "$ok" "$(date +%s)" "$from" "$to" "$original" \
        "$(printf '%s' "$detail" | tr -cd 'A-Za-z0-9._ -')" > "$FADER_ACKS/$id.json"
}

handle_fader_request() {
    local request="$1" id action target seconds snapshot current maximum from original
    local target_raw target_percent steps delay step value actual actual_raw actual_max
    id=$(safe_id "$(basename "$request" .request)")
    action=$(manifest_field "$request" action)
    target=$(manifest_field "$request" target)
    seconds=$(manifest_field "$request" seconds)
    [[ "$seconds" =~ ^[0-9]+([.][0-9]+)?$ ]] || seconds=0
    snapshot=$(phone_music_volume) || snapshot=""
    if [ -z "$snapshot" ]; then
        write_fader_ack "$id" fader_failed false 0 0 0 termux-volume-unavailable
        return 1
    fi
    read -r current maximum <<< "$snapshot"
    from=$(phone_volume_percent "$current" "$maximum")
    if [ "$action" = set ]; then
        [[ "$target" =~ ^[0-9]+$ ]] && [ "$target" -le 100 ] || return 1
        [ -s "$FADER_STATE" ] || printf 'raw=%s\nmax=%s\n' "$current" "$maximum" > "$FADER_STATE"
        target_percent=$target
        target_raw=$(awk -v p="$target" -v m="$maximum" 'BEGIN { printf "%d", p*m/100+0.5 }')
    elif [ "$action" = restore ] && [ -s "$FADER_STATE" ]; then
        original=$(manifest_field "$FADER_STATE" raw)
        old_max=$(manifest_field "$FADER_STATE" max)
        target_raw=$(awk -v v="$original" -v o="$old_max" -v m="$maximum" \
            'BEGIN { printf "%d", o > 0 ? v*m/o+0.5 : v }')
        target_percent=$(phone_volume_percent "$target_raw" "$maximum")
    else
        write_fader_ack "$id" fader_failed false "$from" "$from" 0 invalid-request
        return 1
    fi
    original_percent=$(if [ -s "$FADER_STATE" ]; then \
        phone_volume_percent "$(manifest_field "$FADER_STATE" raw)" \
            "$(manifest_field "$FADER_STATE" max)"; else printf '%s' "$from"; fi)
    phone_fade_raw "$current" "$target_raw" "$seconds" || true
    actual=$(phone_music_volume) || actual=""
    read -r actual_raw actual_max <<< "$actual"
    actual_percent=$(phone_volume_percent "${actual_raw:-0}" "${actual_max:-1}")
    if ! awk -v a="$actual_percent" -v t="$target_percent" \
        'BEGIN { d=a-t; if(d<0)d=-d; exit !(d<=1) }'; then
        write_fader_ack "$id" fader_failed false "$from" "$actual_percent" \
            "$original_percent" target-not-applied
        return 1
    fi
    [ "$action" = restore ] && rm -f "$FADER_STATE"
    write_fader_ack "$id" fader_finished true "$from" "$actual_percent" \
        "$original_percent" android-music-fader
}

process_fader_requests() {
    local request claimed
    for request in "$FADER_INBOX"/*.request; do
        [ -e "$request" ] || continue
        claimed="$FADER_CLAIMED/$(basename "$request")"
        mv "$request" "$claimed" 2>/dev/null || continue
        handle_fader_request "$claimed" || true
        rm -f "$claimed"
    done
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
    # The hold carries its own duration watchdog. Waiting for that receipt
    # avoids opening a second background Activity just to release it.
    for _ in $(seq 1 40); do
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
    local music_restore="" snapshot="" system_restore="" system_max="" system_target=""
    local current=""
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
        else
            snapshot=$(phone_music_volume 2>/dev/null || true)
            if [ -n "$snapshot" ]; then
                read -r system_restore system_max <<< "$snapshot"
                system_target=$(awk -v p="$duck_percent" -v m="$system_max" \
                    'BEGIN { printf "%d", p*m/100+0.5 }')
                if [ "${system_restore:-0}" -gt 0 ] 2>/dev/null && \
                   phone_fade_raw "$system_restore" "$system_target" "$fade_down"; then
                    write_ack "$item" duck_started system-music-fader
                else
                    system_restore=""
                fi
            fi
            if [ -z "$system_restore" ] && focus_hold "$item" "$duration"; then
                focus_held=1
                write_ack "$item" duck_started audio-focus-may-duck
            elif [ -z "$system_restore" ]; then
                write_ack "$item" duck_failed focus-unavailable
            fi
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
    elif [ -n "$system_restore" ]; then
        current=$(phone_music_volume 2>/dev/null | awk '{print $1}')
        if [ -n "$current" ] && phone_fade_raw "$current" "$system_restore" "$fade_up"; then
            write_ack "$item" duck_finished system-music-fader
        else
            write_ack "$item" duck_release_failed system-music-fader
        fi
    fi
    write_ack "$item" voice_finished phone-player
}

process_one() {
    local source="$1" name claimed manifest capsule expected_version expected_size expected_sha ready duck
    local decoded actual_size actual_sha duck_percent fade_down fade_up
    name=$(safe_id "$(basename "$source" .b64)")
    [ -n "$name" ] || return 0
    [ "$(tail -n 1 "$source" 2>/dev/null)" = "END." ] || return 0
    capsule_allows_playout "$name" || return 0
    if grep -q '"event":"voice_finished"' "$ACKS/$name.jsonl" 2>/dev/null; then
        rm -f "$source" "$INBOX/$name.integrity"
        return 0
    fi
    claimed="$CLAIMED/$name.b64"
    manifest="$CLAIMED/$name.integrity"
    capsule="$CLAIMED/$name.capsule"
    mv "$source" "$claimed" 2>/dev/null || return 0
    if ! mv "$INBOX/$name.integrity" "$manifest" 2>/dev/null; then
        write_ack "$name" voice_failed integrity-manifest
        mv -f "$claimed" "$STALE/$name.b64.no-manifest"
        return 0
    fi
    if [ -f "$INBOX/$name.capsule" ]; then
        mv "$INBOX/$name.capsule" "$capsule" 2>/dev/null || {
            write_ack "$name" voice_failed capsule-claim
            rm -f "$claimed" "$manifest"
            return 0
        }
    else
        capsule=""
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
    rm -f "$claimed" "$manifest" "$decoded" ${capsule:+"$capsule"}
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

drain_ready_items() {
    expire_due_capsules
    process_fader_requests
    for candidate in "$INBOX"/*.b64; do
        [ -e "$candidate" ] || continue
        process_one "$candidate"
    done
    for candidate in "$MUSIC_INBOX"/*.b64; do
        [ -e "$candidate" ] || continue
        process_music_one "$candidate"
    done
}

drain_ready_items
last_rescan=$(date +%s)
while true; do
    write_health running
    if IFS= read -r -t "$HEARTBEAT_SECONDS" _event <&9; then
        drain_ready_items
        last_rescan=$(date +%s)
        continue
    fi
    now=$(date +%s)
    if [ $((now - last_rescan)) -ge "$SAFETY_RESCAN_SECONDS" ] 2>/dev/null; then
        drain_ready_items
        last_rescan=$now
    fi
done

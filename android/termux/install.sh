#!/data/data/com.termux/files/usr/bin/bash
# Install the Audience of One phone receiver without touching unrelated Termux state.

set -eu

SOURCE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
LIB="$HOME/.local/lib/audience-of-one"
BIN="$PREFIX/bin"
CONFIG_DIR="$HOME/.config/audience-of-one"
STATE="$HOME/.local/state/audience-of-one-phone"
ROOT="$HOME/storage/shared/Download/AudienceOfOne"
TOKEN_FILE="$CONFIG_DIR/phone-token"
CONFIG_FILE="$CONFIG_DIR/phone.env"
BOOT_DIR="$HOME/.termux/boot"
BOOT_FILE="$BOOT_DIR/audience-of-one-phone"

missing=""
for command in python3 mpv pulseaudio curl base64 sha256sum termux-wake-lock; do
    command -v "$command" >/dev/null 2>&1 || missing="$missing $command"
done
if [ -n "$missing" ]; then
    printf 'Missing commands:%s\n' "$missing" >&2
    printf 'Install the F-Droid Termux:API app, then run:\n' >&2
    printf '  pkg install python mpv pulseaudio curl coreutils termux-api\n' >&2
    exit 2
fi
if [ ! -d "$HOME/storage/shared" ]; then
    printf 'Shared storage is unavailable. Run termux-setup-storage, allow access, then retry.\n' >&2
    exit 2
fi

mkdir -p "$LIB" "$CONFIG_DIR" "$STATE" "$ROOT" "$BOOT_DIR"
install -m 700 "$SOURCE/station-phone" "$BIN/station-phone"
ln -sf "$BIN/station-phone" "$BIN/fm"
install -m 700 "$SOURCE/station-phone-player.sh" "$LIB/station-phone-player.sh"
install -m 700 "$SOURCE/station_phone_mcp.py" "$LIB/station_phone_mcp.py"
install -m 700 "$SOURCE/station_phone_mpv.py" "$LIB/station_phone_mpv.py"
install -m 700 "$SOURCE/station_focus_receipt.py" "$LIB/station_focus_receipt.py"

if [ ! -s "$TOKEN_FILE" ]; then
    python3 -c 'import secrets; print(secrets.token_urlsafe(36))' > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
fi
if [ ! -s "$CONFIG_FILE" ]; then
    {
        printf 'STATION_PHONE_ROOT=%q\n' "$ROOT"
        printf 'STATION_PHONE_BIND=127.0.0.1\n'
        printf 'STATION_PHONE_PORT=8787\n'
        printf 'STATION_PHONE_LOCATION_ID=station\n'
        printf 'STATION_PHONE_TOKEN_FILE=%q\n' "$TOKEN_FILE"
    } > "$CONFIG_FILE"
    chmod 600 "$CONFIG_FILE"
elif grep -Fq "$HOME/storage/shared/AudienceOfOne" "$CONFIG_FILE"; then
    sed -i "s#$HOME/storage/shared/AudienceOfOne#$ROOT#g" "$CONFIG_FILE"
fi
{
    printf '#!/data/data/com.termux/files/usr/bin/bash\n'
    printf 'termux-wake-lock >/dev/null 2>&1 || true\n'
    printf 'station-phone start >> "$HOME/.local/state/audience-of-one-phone/boot.log" 2>&1\n'
} > "$BOOT_FILE"
chmod 700 "$BOOT_FILE"

"$LIB/station-phone-player.sh" --self-test
station-phone restart

printf '\nInstalled. Configure the Mac with:\n'
printf '  android.location_id = "station"\n'
printf '  STATION_PHONE_MCP_TOKEN=%s\n' "$(cat "$TOKEN_FILE")"
printf '\nHuman controls in Termux: fm (pause/resume), fm off (stop), fm s (status).\n'
printf '\nThe receiver listens on loopback only. See android/README.md before exposing it.\n'

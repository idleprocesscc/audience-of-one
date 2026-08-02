# QQ Music companion (macOS + mpv)

QQ Music can supply the record; mpv remains the station turntable. This keeps
search and account rights in QQMusicApi while the proven station engine owns
playback identity, position, repeat, transitions, pause, and the live fader.

The official QQ Music app may stay installed for ordinary listening and account
management. It does not expose the control or stream interface used here, and
station does not read its private files. QQMusicApi runs as a separate local
service.

## 1. Prepare the turntable

Complete [Local record box](LOCAL-RECORD-BOX.md) first. QQ Music uses the same
mpv process, so `music.backend` remains `local` and `music.library_root` remains
the home for files you already own.

Add the companion to the same config:

```toml
[qqmusic]
enabled = true
base_url = "http://127.0.0.1:8080"
timeout_seconds = 8.0
retries = 2
```

## 2. Run QQMusicApi locally

QQMusicApi is installed alongside station, not vendored into it:

```sh
git clone https://github.com/L-1124/QQMusicApi.git
cd QQMusicApi
uv sync --group web --no-dev
uv run --no-sync web/run.py
```

The upstream service binds to `127.0.0.1:8080` by default. Keep that loopback
address unless another machine genuinely needs access. Its Swagger and API
documentation are available at `http://127.0.0.1:8080/swagger` while it runs.

Anonymous access is enough for catalog search and some playable records. For a
listener collection or music requiring account rights, authorize once through
station:

```sh
station qqmusic login
```

The QR opens on the Mac; scan it with QQ or WeChat and confirm on the phone.
Station saves the returned credential under its private state directory with
mode `0600` and sends it only as a Cookie to the configured loopback service.
The official QQ Music desktop app's existing login is intentionally not
scraped, and credentials never enter a programme item or receipt.

## 3. Prove the companion before sound

```sh
station qqmusic doctor
station qqmusic search "Something Stupid Lola Marsh"
station qqmusic list
station qqmusic list --playlist "Morning records"
```

`doctor` verifies the exact search, authorization, and CDN routes station needs.
Search is read-only and returns stable `qqmusic:MID` identities. It does not
start mpv or touch the rundown. Private LIST places **My Favorites** first,
followed by created and collected playlist summaries. `--playlist` expands one
exact name or numeric ID into tracks without adding any of them to the rundown.

## 4. Open one programme

Use either a title-and-artist query or the exact MID returned by search:

```sh
station qqmusic open "Something Stupid Lola Marsh" \
  --say "One record from the QQ Music shelf." \
  --transition overlap \
  --device local_mac

station qqmusic open "qqmusic:004KWIe52XhNKl" --device local_mac
```

The resolver tests QQ Music's CDN candidates before playout. One unreachable
candidate cannot turn an accepted URL into a false start. The full playback URL
is kept in memory only; receipts record the MID, metadata, authorization result,
expiry, and selected CDN host, while local state stores only a hash needed to
recognize the same mpv stream from a later shortcut or terminal command.

Once it is playing, the ordinary station controls apply:

```sh
station fader 35 --seconds 0.8
station fader restore --seconds 1.0
station toggle --device local_mac
station off --device local_mac
```

If the account cannot play a record, station stops before mpv and names the
authorization failure. It never substitutes a different search result after the
requested MID has been chosen.

A long-running mpv instance can occasionally accept a remote `loadfile` command
without retaining a path even though the authorized URL is independently
readable. Station recognizes that exact boundary, rebuilds mpv once, and repeats
the same MID; ordinary network or authorization failures do not trigger a
restart.

## 5. Send the same record to Android

Install the [Android receiver](../android/README.md), configure its authenticated
transport, then add `--phone`. The phone target comes from `android.device`; it
is not the official QQ Music app and does not use Connect.

```sh
station qqmusic open "qqmusic:004KWIe52XhNKl" \
  --phone \
  --device "Android Phone" \
  --say "One record from the QQ shelf." \
  --transition overlap \
  --json
```

The Mac keeps the QQ credential. It resolves one short-lived CDN URL just before
airtime, sends a hashed and integrity-checked task through the authenticated
phone transport, and never places the credential in Android storage. The
receiver claims and deletes the shared inbox copy before asking its persistent
mpv to load the stream. Success requires the matching URL hash, duration and
position motion from the phone; a successful file write is not an on-air receipt.

For an overlap, the receiver moves its own mpv fader down before voice and
restores it afterward. This route streams from QQ Music and does not require a
local song file on Android. Owned local-file delivery is a separate port and is
not implied by this command.

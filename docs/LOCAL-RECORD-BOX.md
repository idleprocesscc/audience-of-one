# Local record box (macOS)

This backend turns a listener-owned or licensed music folder into the station's
record box. One persistent `mpv` process supplies exact file, progress, repeat,
pause, and volume evidence through its IPC socket; catalog discovery remains a
separate, pluggable concern.

## Prepare the box

```sh
brew install mpv ffmpeg
mkdir -p ~/Music/AudienceOfOne/tracks
cp examples/config.local-mpv.toml ~/.config/audience-of-one/config.toml
chmod 600 ~/.config/audience-of-one/config.toml
```

Put an MP3, M4A, FLAC, AAC, Ogg, Opus, or WAV from your collection below the
tracks directory. Subdirectories are allowed. The configured
`music.library_root` is the record-box boundary; paths outside it and unsupported
file types are rejected.

Run the read-only checks:

```sh
station config validate
station doctor
station list
```

`station list` detects the local backend and scans only `music.library_root`.
Embedded title, artist, album, date, and duration tags come from `ffprobe`; a
file without tags remains usable under its filename and exact `local:` URI. The
scan neither starts mpv nor changes the rundown. `station mpv list --json`
returns the same shelf for an agent or script.

## First local programme

Use a path relative to the library root. The `local:` prefix is explicit but
optional for direct input:

```sh
station open "local:Artist/Album/01 - Song.flac" \
  --say "One record from the station box." \
  --device local_mac \
  --transition overlap \
  --json
```

`overlap` and `intro` move mpv's own volume, not the Mac system volume. `clean`,
`blackout`, and `hard` keep their existing meanings. The same durable fader
surface works outside a programme:

```sh
station fader 40 --seconds 0.8
station fader restore --seconds 1.0
station off --device local_mac
station toggle --device local_mac
station mpv quit
```

`off` pauses the record and keeps the quiet mpv process ready. `toggle` is the
single play/pause surface suitable for a keyboard shortcut. `mpv quit` is the
rare full exit; the next local programme will start a fresh process.

Install the macOS shortcut surface after the command itself works:

```sh
station shortcut install
station shortcut doctor
```

Import the workflow when Shortcuts opens, then assign Control-Command-P (or a
key that suits the keyboard). The launcher captures this station installation
and config path, so Shortcuts does not depend on a login shell or PATH. The
installer never changes macOS's **Allow Running Scripts** preference; Shortcuts
will ask for that permission when it first needs it.

The backend starts mpv in idle mode and reuses its Unix IPC socket. Process
creation is not playout proof: a programme succeeds only after the requested
relative file is current and its position advances through the configured
stability window.

## Current reach

The macOS route has passed real listening tests with FLAC and MP3 records,
metadata receipts, repeat normalization, position motion, natural TTS over a
continuous music bed, and a `100 → 35 → 100` fader ride. The QQMusicApi
companion has also passed a real search → CDN selection → mpv identity → fader →
pause transaction; see [QQ Music companion](QQMUSIC-COMPANION.md). Android
local-file playout is the next port; the current phone route pairs Spotify
Premium music with a separate spoken clip.

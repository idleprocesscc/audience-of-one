# audience-of-one

> Built for an audience of one.

Audience of One is an agent-run personal station with a transition grammar, a
live rundown, and an optional Android listening endpoint.

The DJ is an LLM agent. It doesn't announce tracks from metadata — it writes each
line for the one person listening: what kind of day she had, what the last song
meant, why the next one is coming. Voice, timing, transitions, and live fader
moves turn ordinary playback into a personal FM.

It is built around the music player the listener already uses. Spotify keeps its
library, devices, lock-screen controls, and familiar play/pause behavior; local
collections can use mpv. Put the phone away, lock the screen, or keep using it—
the AI companion stays behind the console rather than asking for a new music
habit or another foreground app.

**Keep your player. Hand the booth to your AI.**

## Choose the music source

| Source | What it gives the station | Current status |
| --- | --- | --- |
| Spotify Premium | Online catalog, search, recommendations, exact Connect targeting | macOS runnable; Android real-device path accepted |
| Local files + mpv | Owned MP3/FLAC/M4A, no music account, direct and deterministic fader control | macOS runnable; Windows and Android local-file ports are Phase 2 |
| QQ Music + mpv | QQ Music search and account-authorized streams, with mpv transitions and receipts | macOS and Android real-device paths accepted |
| Spotify Free | No subscription, but ads and nondeterministic app context | Experimental assisted mode on Mac only |

The programme, TTS, transitions and receipts stay the same whichever record
shelf is selected. mpv is the open-source player engine, not a music catalog;
the listener supplies the files.

## Status

The macOS control room and Android receiver have completed real-device
rehearsals. A clean-clone install and multi-track scheduler rehearsal have also
passed. The v0.1 scheduler consumes a live rundown, prepares the next item
before the current record ends, and fires it from real position and duration
evidence. Voice, music, transition, fader, recovery, and after-song policy stay
in receipted transactions. Later directions live in [ROADMAP.md](ROADMAP.md).

## Architecture status

- shipped: `desktop` — receipt-driven direct playout on macOS;
- shipped: `station` — init, config, doctor, Spotify and QQ Music inspection, open,
  backend-aware list, say, retry, queue, rundown, start/stop, status, fader,
  recovery-cover build, and
  wildcard-liner cache build;
- shipped: `skills/station/SKILL.md` — a portable desktop-preview host guide;
- shipped: a singleton position-watching scheduler with early preparation,
  song-boundary firing, autoplay interception, failure skip, and repeat/stop
  after-song policies;
- runnable from source: Android Premium path — authenticated Termux receiver,
  integrity and claim-once playout, voice/duck receipts, 5G transport recipes,
  and an audio-focus helper source tree, accepted on a real device;
- runnable from source: Android QQ Music path — the Mac resolves one expiring
  account-authorized stream, Termux mpv proves position motion, and the URL is
  removed from the transport inbox before playout; real-device music, duck,
  restore, and pause receipts have passed;
- optional shipped toy: a physical-key call-in can remain a ring-only signal or
  use the Android foreground recorder, authenticated phone transport, and a
  listener-chosen STT endpoint to hand one short voice clip to the agent;
- runnable: deterministic local record box through mpv, with root-confined file
  resolution, exact metadata/progress/pause/repeat receipts, live transitions,
  and a durable fader;
- runnable preview: a separately installed QQMusicApi service supplies anonymous
  search, QR-authorized private shelves, and account-authorized streams to the
  same mpv transaction/fader contract; playback URLs and credentials are not
  written to receipts;

*The private build's voice lines are love letters and stay home. This repo ships the instrument, not the song.*

## Desktop quickstart

Start with [the ten-minute macOS guide](docs/DESKTOP-QUICKSTART.md). The shortest
safe order is install → config → doctor → voice → track → combined opener. The
quality profile uses MiniMax and ElevenLabs; macOS `say` is the zero-account
voice check. Premium Web API is the exact Connect route. Spotify Free uses a
narrower, receipt-driven Mac adapter; the guide records its observed behavior.

For Spotify Premium music and speech on the same Android phone, hand the
[Android guide](android/README.md) to an AI agent. It covers both 5G
routes and the standalone `station say "..." --phone` intercom. The optional
[physical call-in](docs/CALL-IN.md) stays outside the main installation path.

For the no-subscription local route, use the
[local record-box preview](docs/LOCAL-RECORD-BOX.md). It shares the programme and
fader contract and requires only owned audio files plus mpv.

For a QQ Music collection with the same mpv programme effects, continue with the
[QQ Music companion](docs/QQMUSIC-COMPANION.md). The official QQ Music app can
remain the listener's everyday player; QQMusicApi supplies station's local
catalog and stream interface.

`station list` opens only the shelf selected by `music.backend`. The explicit
`station spotify list` and `station mpv list` commands expose the same two
read-only shelves without combining their rankings or changing the rundown.
`station qqmusic list` is a third independent shelf: My Favorites plus created
and collected playlist summaries, with one optional `--playlist` expansion.

On macOS, `station shortcut install` creates a stable launcher and opens an
importable Shortcut workflow. The suggested cross-keyboard binding is
Control-Command-P; `station shortcut doctor` checks the launcher, workflow, and
imported Shortcut without toggling playback.

## Development checks

The source tree uses a `src/` layout. Install it before ordinary test discovery:

```sh
python -m pip install -e ".[dev]"
python -m unittest discover
ruff check src tests
```

## License and provenance

Audience of One is **source-available**, not OSI open source. It is licensed
under [PolyForm Noncommercial 1.0.0](LICENSE.md): personal use, modification,
and redistribution are welcome for noncommercial purposes; commercial use is
not granted. Copies retain the required provenance notice
`audience-of-one:kcg:2026`. See [PROVENANCE.md](PROVENANCE.md) for the human and
machine-readable origin mark.

---

Audience of One · Koshi × Claude × GPT · 2026

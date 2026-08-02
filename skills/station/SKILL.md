---
name: audience-of-one-station
description: Run an audience-of-one radio station as a host, not a playlist operator. Use whenever the listener asks for radio, a show, an opener, a hosted song, a transition, voice-over, ducking, a fader gesture, or the optional phone call-in, whether sound goes to Mac or the configured Android receiver.
---

# Audience of One — Host Skill

I run this station for one person. The listener is the whole room. The tuning
reference is their ears.

This repository ships the instrument, not the script. The machine can keep time,
move a fader, and prove what happened; only the host can decide why this song,
why now, and what the silence on either side of a sentence should mean. A station
that merely queues tracks is a music player with extra steps.

Read `README.md` and `docs/DESKTOP-QUICKSTART.md` before the first live operation
in a session. For phone output, also read `android/README.md` and its selected
transport recipe. v0.1 runs a receipted rundown on the local Mac or one
configured Android receiver. Queue the shape of the show, inspect it, then hand
the shift to `station start`.

When installing the Android receiver, finish by giving the listener its short
Termux controls: `fm` pauses or resumes QQ/local mpv music, `fm off` stops it,
and `fm s` shows its state. Spotify keeps its own familiar play/pause control.
The same reminder belongs anywhere a phone QQ/local show is on air and the
listener asks how to pause; it is not merely an installation footnote.

## Take the shift

Even a one-song show has a shape: choose the track, the line, the entrance, and
what should remain unsaid. The programme effect is how the host speaks to the
listener through music; it belongs to the host, never to an automatic selector.

Before sound:

1. Run `station doctor`, then `station list`; both are read-only. LIST detects
   the configured backend and opens only that record shelf. Spotify Web API
   folds the configured playlist, long-term affinity, recent footprints, and
   Weekly into one view; local mpv returns exact tagged files below its library
   root. It neither writes the rundown nor chooses for the host.
   For a phone shift, use
   `station doctor --phone` so an authenticated MCP session and fresh player
   heartbeat are proven before any sound.
2. For `web_api`, run `station spotify devices` and choose the exact output.
   A phone programme requires Spotify Premium and the same exact phone name or ID
   in `spotify.device`, `android.device`, and `--device`.
   For `macos_applescript`, run `station spotify now`, then visibly select this
   Mac in Spotify before combining music with a local voice.
   For the local backend, choose one exact `local:` URI from LIST. It reports
   what is in the box; it does not guess a missing file.
   When the QQ Music companion is configured, `station qqmusic search` is the
   public shelf search; `station qqmusic list` opens the signed-in listener's My
   Favorites and playlist summaries. Choose its exact `qqmusic:MID`, then use
   `station qqmusic open`. QQMusicApi finds and authorizes the record while mpv
   remains the turntable. LIST is evidence for selection, never an instruction
   to queue everything it returned.
   With the Android receiver configured, `station qqmusic open ... --phone`
   streams that same exact MID to Termux mpv. Treat only matching
   `track_started` and position-motion receipts as air; a staged URL is not air.
3. Run `station status`; a queued or failed item must be understood before adding
   another one.

Mac or mobile is only the output route; it never decides whether a request is a
radio programme. Naming a device changes where the sound lands, not the care owed
to the show.

## One programme, one transaction

An opener with voice and music is one command:

```sh
station open "spotify:track:..." \
  --say "A line written for this listener and this song." \
  --transition intro \
  --device local_mac
```

For a configured Android receiver, add `--phone` and name that exact Connect
device. Music still goes through Spotify; only the spoken file crosses the
station transport. The phone's independent voice and audio-focus receipts are
part of the same transaction.

The Web API adapter can resolve `Title Artist`. The AppleScript adapter cannot
search the catalog: resolve and verify a public Spotify track URL first, then
pass its exact URL or URI. It follows the Connect target already selected in
Spotify but cannot select or prove that target. Never describe `current` as a
named phone merely because audio happened there once. A Spotify Free phone may
stall or replace an exact request; only a stable receipt makes the track real,
and Free mobile is not a deterministic programme route. Free Mac automation is
also narrower than a human click: the app can preserve a manually selected song
through ads while AppleScript or a URI handoff resumes another context. Never
promise unattended exact Free playout merely because `local_mac` was selected.

The local backend accepts an exact path below its configured record box, such as
`local:Artist/Album/01 - Song.flac`. Choose an entry that LIST actually returned,
then host it with the same transition vocabulary. Its fader moves mpv, not
Spotify or system volume.

For QQ Music, search before writing a line about the record. A successful search
is metadata, not playout; the programme becomes real only after station confirms
the chosen MID, a readable CDN, mpv position motion, and repeat off. Once those
receipts exist, use the same transition and fader judgment as any local record.

If the optional Level 2 wildcard policy is enabled, a stable replacement song
may invite one pre-rendered generic liner. This is improvisation over music that
is actually playing, not the dead-air recovery cover. Treat `selected` as an
idea and `voice_finished` as playout; the host still decides whether to accept
the replacement, retry the original request, or stop.

Do not hand-stitch a system voice command and a separate Spotify play command.
They have no shared transaction, timing, or receipts. A successful command must
contain the receipts promised by this attempt; unrelated Spotify playback is not
proof.

If an item fails, recover that item with `station retry ITEM_ID`. Do not recreate
it or repeat a voice process that already completed.

For one immediate line with no music operation, use the intercom rather than
inventing a one-item show:

```sh
station say "One line for the listener." --phone
```

This still waits for phone voice receipts. It is intentionally direct: the
listener has granted the host a small line to their ears, so use judgment and
do not mistake reachability for an invitation to become a notification feed.

The optional physical call-in is an event source, not an automatic canned
answer. Read `docs/CALL-IN.md`; `station call-in watch` emits a ring or voice
event, then the host decides whether and how to answer with `station say --phone`.

## The voice

Speak to one person, never to an imaginary crowd. Metadata can identify a song;
the host's line should reveal why it belongs in this particular moment. Hook the
seam on a mood, an image, one lyric, or one word carried from the previous track.

A bare song can be an intentional breath. It must not be the default produced by
forgetting to host.

## Transition colours

- `overlap` — start the track, duck it, speak, restore; the Mac uses its local
  Spotify fader, while Android owns a short audio-focus lease;
- `intro` — give the track a short opening breath, then duck and speak;
- `clean` — speak in isolation, then start the track;
- `blackout` — isolated voice, deliberate silence, then the track; no fade or duck;
- `hard` — start the track and speak without ducking.

`tail` belongs to the running boundary-aware scheduler and is rejected only by the
direct opener. The legacy queue value `dark` is read as `blackout`, but new host
commands use only `blackout`.

The seam matters more than the label. These are colours on the console, not
instructions for taste.

## The fader is an instrument

```sh
station fader 40 --seconds 0.8
station fader restore --seconds 1.0
```

The first manual move records the original active-backend level; later moves keep
that restore point until `restore` succeeds. The fader changes local Spotify or
mpv volume, never system volume or the rundown. A current remote Connect target
may reject that volume write; the command must return a verified move before the
host treats it as a gesture.

There is no correct depth or speed. Ride it, hold it, or leave it alone. A useful
first gesture is roughly thirty points down over about 0.8 seconds; after that,
listen. Do not narrate the technique to the listener.

A transition is one atomic voice/track boundary. A scene is several prepared
materials plus live position and fader gestures. The current desktop preview does
not yet ship a scene runner: do not fake a multi-act scene by stacking independent
`station open` calls and hoping their timing joins up.

Some scene sparks for the host's imagination — shapes, not named procedures:

- **Last-line door** — let the music regain the room through the closing phrase;
- **One more thing** — return after a real ending with one shorter line;
- **Volume as punctuation** — let a comma hold, an ellipsis sink, or a full stop
  release the song;
- **Let the song introduce itself** — allow the opening to make weather before
  explaining why it is here;
- **False sign-off** — a sincere ending, one believable beat of darkness, then a
  prepared return that changes what the goodbye meant.

Multi-act effects require every cue to be ready before the first act begins. If
the return is not prepared, make the goodbye real; never leave the listener in
dead air while a surprise is still being built.

## Receipts and limits

- `PLAYOUT CONFIRMED` is control-plane evidence, not microphone proof of audible
  sound through an unmuted speaker.
- `station history --json` shows completed items and their component receipts.
- `station off --device "..."` pauses the configured backend only after its
  promised receipt. AppleScript promises the current Spotify session, not a
  named Connect device; mpv proves the local player state.
- `station queue` writes a future programme item. Only `station start` consumes
  it; a queue receipt is still not a playout receipt.
- Never paste credentials, OAuth callbacks, logs, private lines, or state archives
  into an agent conversation.

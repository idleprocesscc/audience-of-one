# Station clips (idents and musical echoes)

A clip is a short recording the host made ahead of time: a station ident at the
top of a show, or a few bars that answer the record that just ended before the
host starts to speak. The host might write the notes, render them in a desktop
music app, and listen back with the listener before the clip goes on the shelf.
The station does not play instruments live; it plays what was prepared, with the
same receipts as any other programme item.

A clip travels on the voice channel. At preparation time the station renders the
clip — alone, or with the synthesized line entering at a chosen second — into
the single voice file that the desktop and phone voice paths already play, duck
under or over the record, and receipt. Transitions, recovery, retry, and phone
delivery therefore behave exactly as they do for a spoken line.

## Prepare the shelf

```sh
brew install ffmpeg
mkdir -p ~/Music/AudienceOfOne/clips
```

```toml
[clips]
root = "~/Music/AudienceOfOne/clips"
```

`clips.root` is the shelf boundary. Clips are named by a relative path below it;
absolute paths, `..`, symlinks that leave the shelf, and unsupported types are
rejected. WAV, AIFF, CAF, FLAC, M4A, and MP3 are accepted. `station doctor`
reports the shelf and requires `ffmpeg` (4.4 or newer) once `[clips]` exists.

Master each clip against the station voice as the TTS provider actually
delivers it. Nothing is normalized: the clip is mixed at its own level, and a
limiter only keeps an unlucky sum below full scale. Measure one synthesized line
first — provider output is often hotter than −16 LUFS — then keep a piano phrase
roughly 4 LU below it; against a voice near −13 LUFS, that is an echo near
−17 LUFS. Measure the rendered file in `prepared/` rather than either part alone.

## Programme shapes

An ident alone, then the first record:

```sh
station queue --clip idents/station.wav --transition clean
station queue "local:Artist/Album/01 - Song.flac" "The first line tonight."
```

An echo after a record: the rundown fires this item at the song boundary, plays
the echo, and lets the line enter over its last chord before the next record:

```sh
station queue "This Song"
station queue "Next Song" "A line written for this seam." \
  --clip echoes/after-this-song.wav --clip-voice-at 5.7 --transition clean \
  --follows previous
```

`--follows` ties the seam to the record it was written for: `previous` names the
item queued right before it (or, with an empty rundown, the last one played), and
an explicit item id works too. If that item did not play — a catalog version the
account cannot stream, say — the scheduler still moves on, but this item drops
its clip and line and starts only its own record; with no record, it fails and
names the item it was waiting for. The recovery receipt records what was dropped.

Without `--clip-voice-at`, the line starts when the clip ends. With `tail`, the
clip and line play over the end of the current record, ducked, exactly as a
spoken tail would.

The intercom can play a clip immediately, with or without a line:

```sh
station say --clip idents/station.wav
station say "One line after the ident." --clip idents/station.wav --phone
```

## Receipts

The voice receipt gains a `clip` object: the requested name, the resolved file,
its duration, and — when a line is mixed in — the offset and the spoken part's
duration. `duration_seconds` on the voice receipt is the rendered file the
listener actually heard, so a `tail` boundary allows for the whole echo.

A missing or escaping clip fails the item before any speech is synthesized. If
the clip already played and only the record failed, `station retry` starts the
record without replaying the clip.

## Limits

`--follows` checks that the record's item played, not that it is still the
record on air; a manual skip in between is not detected.

Measure the record the listener will actually hear. Catalog versions of one
song differ: in rehearsal, the duet a lookup placed in F major was unavailable
to the account, and the two playable versions ended in C♯ and E minor.

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

Master each clip to sit where it will be heard. Nothing is normalized: a clip is
mixed at its own level against the station voice as synthesized, and a limiter
only keeps an unlucky sum below full scale. A piano phrase around −20 LUFS under
a voice near −16 LUFS is a reasonable first balance; measure the result rather
than trusting a meter reading of either part alone.

## Programme shapes

An ident alone, then the first record:

```sh
station queue --clip idents/station.wav --transition clean
station queue "local:Artist/Album/01 - Song.flac" "The first line tonight."
```

An echo after a record: the rundown fires this item at the song boundary, plays
the echo, and lets the line enter over its last chord before the next record:

```sh
station queue "Next Song" "A line written for this seam." \
  --clip echoes/after-this-song.wav --clip-voice-at 5.7 --transition clean
```

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

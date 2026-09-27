# Station ears

A host that cannot hear the room still has to know what it is walking into. A
lookup describes *a* version of a song; the listener hears one particular file or
stream. `station ears` decodes that exact version and measures it:

```sh
station ears "local:Nujabes/Counting Stars.mp3" qqmusic:002nWUQ438fyh1
```

```text
City Of Stars (Humming) — Justin Hurwitz, Emma Stone · qqmusic:002nWUQ438fyh1
  key       E minor  r=0.78  (also G major 0.60, D major 0.56)
  ends      ring on Em · winds down 2:36 · last sound 2:40 · 0.0 s silence after
  loudness  -23.6 LUFS · true peak -8.8 dBFS
```

- **key** — a Krumhansl–Kessler estimate over the whole record, with the next two
  candidates. Relative major and minor keys share every note and often score
  almost the same; the note says so when they are nearly tied.
- **ends** — `cut` (stops at full level), `ring` (the last sound decays within
  about six seconds of dropping 6 dB), or `fade` (a longer fade-out); the chord of the last five seconds
  of sound; when the record falls 6 dB below its body level, when its last sound
  ends, and how much silence follows.
- **loudness** — integrated EBU R128 loudness and true peak, useful when a clip
  or a line must sit against this record. The humming version above measures
  −23.6 LUFS; an echo mastered near −17 LUFS and a synthesized line near
  −13 LUFS arrive well above it, a step worth hearing coming.

Measurements are cached below the station state by track URI; `--refresh`
measures again and `--json` returns the full record for an agent.

## What it can hear

The local record box and the QQ Music companion expose decodable audio: a file
below `music.library_root`, or the account-authorized stream just resolved for
this command. The stream URL is used once for decoding; it is neither cached nor
echoed in errors. Spotify audio is protected and cannot be measured.

Loudness and the ending need only `ffmpeg`. The key and final chord need numpy:

```sh
python -m pip install -e ".[ears]"
```

These are estimates from a spectrum, not a transcription. Use them the way a
musician uses a first listen: to choose the key an echo answers, the chord it
starts from, and the second a line may enter over a tail — then check the result
in the rendered file before it goes on air.

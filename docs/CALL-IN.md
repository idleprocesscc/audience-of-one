# Physical call-in (Key Mapper hotline)

An optional extra: long-press the phone's Volume Up key and the station
treats it as a listener calling in. The agent notices within seconds and
can answer on air through `station say --phone`. Nothing here is required
for normal radio operation.

The shipped path records a short clip and hands the agent a transcript. It
uses the same focus-helper APK and authenticated phone transport as the
mobile station. Key Mapper launches the recorder by explicit package and
class; it does not need Expert Mode, Shizuku, or a shell action.

## How it fits together

The Android helper briefly becomes foreground, records eight seconds, and
publishes the finished clip into the station's work directory. Termux stages
it for the Mac, and the Mac consumes it through the same authenticated MCP
transport it already uses for playback.

    long-press Volume Up
      → Key Mapper starts djrecord://record?cue=true
      → helper records into <root>/call-in-work/
      → Termux stages <root>/call-in-outbox/<epoch>.b64
      → Mac retrieves, transcribes, and notifies the agent
      → agent answers with `station say --phone`

## Phone setup

1. Install Key Mapper (F-Droid or Play) and enable its accessibility service.
2. Create a trigger: press Volume Up once so Key Mapper records it,
   then tick **Long press**. The normal accessibility route requires the
   screen to be on; detecting hardware keys with the screen fully off is a
   root-only Key Mapper feature.
3. Add **Send intent** with these values:

       Type: Activity
       Description: Audience of One call-in
       Action: android.intent.action.VIEW
       Data: djrecord://record?cue=true
       Package: io.github.audienceofone.djcontrol
       Class: io.github.audienceofone.djcontrol.RecordActivity

4. Exempt Key Mapper, the helper, and Termux from battery optimisation, or the
   mapping dies quietly after a few hours.

On first use, ColorOS/OxygenOS may ask whether Key Mapper may start the audio
helper. Allow it once; denying that system prompt prevents the recorder from
ever reaching the microphone.

Two short vibrations mean the microphone is live. One longer vibration means
the helper saved the clip; one very long vibration means it failed. The
Termux watcher packages direct helper clips automatically.

## Mac setup

Use the shipped watcher; it speaks the authenticated phone protocol, keeps
local event state, and leaves incomplete uploads for the next pass:

    station call-in watch            # loop; one JSON event per line
    station call-in watch --once     # single pass

Your agent harness consumes the JSON events and decides the on-air reply.

## Voice call-in

Recording the caller from a background trigger is genuinely hard on
modern Android: Termux's `RECORD_AUDIO` app-op is restricted to the
foreground on several OEM builds, and a background recording silently
captures digital silence — which speech-to-text models then transcribe
into confident, identical hallucinations. The shipped answer is a real
microphone foreground service inside the focus helper: briefly visible,
notification and all, exactly as Android intends.

The chain, end to end:

    long-press Volume Up
      → Key Mapper starts djrecord://record?cue=true
      → RecorderService records N seconds of AAC into <root>/call-in-work/
      → station-call-in-watch.sh base64-wraps the clip (1024 columns + END.)
        into <root>/call-in-outbox/<epoch>.b64 and deletes the raw clip
      → `station call-in watch` on the Mac pages the clip down over the
        authenticated /mcp endpoint, decodes it, sends it to the STT
        endpoint, deletes the remote copy, and prints a "call" event

### Phone setup (voice tier)

1. Build and install the focus helper 0.2.0 or later (see the
   [Android guide](../android/README.md) section 8; the same APK now
   carries `RecorderService`). Grant its microphone permission once —
   the app has no launcher icon, so use App info → Permissions, or:

       adb shell pm grant io.github.audienceofone.djcontrol android.permission.RECORD_AUDIO

   The recorder has no browser intent filter. Key Mapper sends the
   `djrecord://record` data directly to the exported recorder activity by
   package and class.

2. Keep the Key Mapper trigger and intent action above.
3. Enable the watcher in `~/.config/audience-of-one/phone.env`:

       STATION_CALLIN_ENABLED=1
       # optional overrides, with their defaults:
       # STATION_CALLIN_SECONDS=8
       # STATION_CALLIN_OUTBOX=call-in-outbox
       # STATION_CALLIN_RING=call-in/ring
       # STATION_CALLIN_POLL_SECONDS=1

   Then `station-phone restart`. The wrapper starts
   `station-call-in-watch.sh` alongside the receiver and reports
   `callin=up` in `station-phone status`. Without the flag the scripts
   stay installed but dormant and the ring tier keeps working as before.

The vibration is the interface: two short buzzes mean the microphone is
live, one long buzz means the clip is staged, one very long buzz means
the recording failed. The recorder also posts `recording`/`saved`
receipts to the same loopback listener the focus helper uses
(`record.state` next to `focus.state`).

### Mac setup (voice tier)

Configure `[call_in]` in `config.toml`:

    [call_in]
    stt_url = "http://127.0.0.1:8792"
    outbox = "call-in-outbox"
    ring = "call-in/ring"
    poll_seconds = 5.0

The STT contract is one POST of raw audio bytes answered with
`{"text": "..."}`. Any local or remote transcription service that
speaks it will do; `examples/stt-funasr.py` is an optional ~50-line
reference using FunASR's paraformer-zh model. With no `stt_url` the
watcher still stages the audio locally and reports
`"stt_error": "no STT endpoint configured"`.

Run the watcher and feed its stdout to your agent harness:

    station call-in watch

Each completed call prints one JSON line:

    {"version": 1, "event": "call", "id": "1722000000000",
     "audio_path": ".../state/call-in/clips/1722000000000.m4a",
     "bytes": 132480, "transcript": "...", "at": 1722000000}

Events are also appended to `<state>/call-in/events.jsonl`, and a
marker per clip prevents replays across restarts. Incomplete uploads
(no `END.` sentinel yet) are simply left for the next pass.

### What still holds this tier honest

The recorder is triggered through a brief transparent activity so the
app counts as foreground when the service asks for the microphone;
some OEM builds are stricter about background activity starts, and Key
Mapper-style battery exemptions apply to the helper app too. If clips
arrive as pure silence, check the helper's notification appeared during
the eight seconds — no notification means the start was swallowed, not
that the microphone failed.

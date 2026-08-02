# Physical call-in (Key Mapper hotline)

An optional extra: long-press the phone's Volume Up key and the station
treats it as a listener calling in. The agent notices within seconds and
can answer on air through `station say --phone`. Nothing here is required
for normal radio operation.

Two tiers share the same trigger. The **ring** tier needs no app build:
one bit crosses, no audio. The **voice** tier records a short clip of
the caller and hands the agent a transcript; it needs the focus-helper
APK (which now contains the recorder) and the Termux call-in scripts.

## How it fits together

For the ring-only tier, Key Mapper turns the long press into a file touch
inside the phone receiver's storage root. The Mac polls that file through
the authenticated MCP file tools it already uses for playback, so this tier
adds no server, port, or microphone permission.

    long-press Volume Up
      → Key Mapper (ADB action): touch <root>/call-in/ring
      → Mac poll loop sees the file, deletes it, notifies the agent
      → agent answers with `station say --phone`

## Phone setup

1. Install Key Mapper (F-Droid or Play).
2. Pair it in **Expert Mode** (enable wireless debugging in Android
   settings, then pair Key Mapper over ADB). This step is not optional
   on many phones: volume keys are often wired to a power-management
   input device (for example `pmic_resin`) that accessibility-based key
   listeners cannot see at all. Only the ADB-backed mode captures them
   reliably.
3. Create a trigger: press Volume Up once so Key Mapper records it,
   then tick **Long press**.
4. Add the action **Execute with ADB** with the command:

       touch /storage/emulated/0/Download/AudienceOfOne/call-in/ring

   Adjust the path to the receiver root you configured
   (`STATION_PHONE_ROOT`); files outside it are rejected by the
   receiver's path jail. Create the `call-in` directory once first.
5. Exempt Key Mapper (and Termux) from battery optimisation, or the
   mapping dies quietly after a few hours.

Routes that look simpler but do not work on current Android builds, so
you can skip re-discovering them: sending Termux a `RUN_COMMAND` intent
from Key Mapper is denied with a permission error on recent releases,
and a Termux:Widget shortcut action degrades to placing an icon on the
home screen under some launcher/Key Mapper combinations. The
shared-storage touch file is boring and survives all of it.

## Mac setup

Poll for the ring with the same authenticated `/mcp` endpoint the
station already uses. Each call is one JSON-RPC `tools/call` request
with the phone's bearer token:

    ring() {
      curl -fsS --max-time 3 "$PHONE_URL/mcp" \
        -H "Authorization: Bearer $PHONE_TOKEN" \
        -H 'Content-Type: application/json' \
        -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"'"$1"'","arguments":{"path":"call-in/ring"}}}'
    }

    while true; do
      if ring android_read_file 2>/dev/null | grep -qv error; then
        ring android_delete_file >/dev/null
        station say --phone "You rang? I'm here."
      fi
      sleep 5
    done

Deleting the file before speaking makes the claim atomic: a second
long press during the reply simply creates the next ring. In practice
you will want the loop above replaced by the shipped watcher, which
speaks the same protocol with receipts and state:

    station call-in watch            # loop; one JSON event per line
    station call-in watch --once     # single pass

A claimed ring prints `{"event": "ring", ...}`. Your agent loop decides
the on-air reply.

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
      → Key Mapper touches <root>/call-in/ring
      → station-call-in-watch.sh claims the ring (atomic mv)
      → station-call-in-record.sh buzzes twice, fires djrecord://record
      → RecorderService records N seconds of AAC into <root>/call-in-work/
      → the script base64-wraps the clip (1024 columns + END. sentinel)
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

   Termux launches the recorder by explicit Android component. The activity
   is not registered as a browser URL handler, so a web page cannot turn a
   `djrecord://` link into a microphone trigger.

2. Keep the Key Mapper trigger from the ring tier unchanged.
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

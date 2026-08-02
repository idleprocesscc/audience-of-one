# Desktop quickstart (macOS)

This is the shortest supported test path for v0.1. Spotify
continues playing through the listener's official Connect client while station
sends control commands and plays the host voice separately with macOS `afplay`.

An AI agent can follow this guide directly. The human usually steps in
only for Spotify sign-in/OAuth and to confirm what actually reached the speakers.

## 1. Prerequisites

- macOS with the Spotify desktop app installed and signed in;
- Python 3.11 or newer;
- `ffmpeg`/`ffprobe` (`brew install ffmpeg`);
- one Spotify control route:
  - **macOS AppleScript:** Spotify Free or Premium; no developer app or OAuth;
  - **Web API:** Spotify Premium plus a developer application whose redirect URI
    is exactly `http://127.0.0.1:8899/callback`.

For a development-mode Web API app, the app owner must remain Premium, each
listener must be on the app allowlist, and Spotify currently allows up to five
authenticated users. Read the current
[quota-mode rules](https://developer.spotify.com/documentation/web-api/concepts/quota-modes)
and [playback endpoint](https://developer.spotify.com/documentation/web-api/reference/start-a-users-playback)
before relying on that adapter.

For the combined test, Spotify and the spoken voice must come out of the same
Mac. A remote phone or speaker is not a desktop mix target. The Web API route
proves the exact Connect device name or ID. The AppleScript route cannot inspect
or select a Connect target: `device = "current"` follows whatever Spotify already
selected, while `device = "local_mac"` records the operator's assertion that this
Mac is selected. Use `local_mac` only after checking the Spotify device menu.

Audience of One is designed for personal listening through the listener's own
official client. Spotify's current player references distinguish that use from
non-interactive broadcasting, altered content, and synchronized recordings;
review the current policy again before a distributed release.

## 2. Install into an isolated environment

```sh
git clone https://github.com/idleprocesscc/audience-of-one.git
cd audience-of-one
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
station init
```

The default files are:

- config: `~/.config/audience-of-one/config.toml`
- state: `~/.local/state/audience-of-one/`

They never reuse another station installation's paths.

## 3. Choose a TTS profile

For the zero-account smoke test, edit the generated config so both language
routes use the included `macos` provider:

```toml
[tts]
chinese = "macos"
english = "macos"
```

For the recommended quality profile, keep the generated MiniMax/ElevenLabs
routing, set your own `voice_id` values in the config, and export your own keys:

```sh
export MINIMAX_API_KEY='...'
export ELEVENLABS_API_KEY='...'
```

A small practical reminder: keys are easiest to keep in your shell or key
manager. Before sharing a transcript, issue, or diagnostic archive, give it one
quick glance for credentials. Repository examples use placeholders.

The quality profile sends the complete spoken line to the selected TTS provider.
That may include personal or intimate text and is governed by the provider's
retention/account policy. Use the local macOS provider when text must stay on the
Mac. See [PRIVACY.md](PRIVACY.md).

## 4. Choose Spotify control

### Spotify Free or local app control

Use the included local profile as the shape for your generated config:

```toml
[spotify]
adapter = "macos_applescript"
device = "local_mac"
stability_seconds = 8.0
ad_wait_seconds = 120.0
```

No Client ID, OAuth, or Spotify developer app is used. The first control command
may prompt macOS to let Terminal or Python automate Spotify. Exact
`spotify:track:...` URIs and `https://open.spotify.com/track/...` URLs are
accepted. A title/artist string is rejected because AppleScript has no catalog
search; let the host resolve and verify a public Spotify track URL first.

The eight-second stability receipt is deliberate. A free-account test showed a
requested track begin correctly and then get replaced by another track several
seconds later. Initial playback is not yet stable playback.

Free ad breaks are not playback failures and are never skipped. When Spotify is
playing but exposes no current track, the adapter records an interstitial and
extends the pending identity window by at most `ad_wait_seconds`. A target is
still successful only when that exact URI later advances through its stability
window.

`current` may follow a phone or speaker already selected in Spotify, but the
receipt says that routing proof is unavailable. `local_mac` is required for a
voice/music transition because the spoken file plays on this Mac; it does not
make Free automation deterministic. In live testing, manual selection in the Mac
app waited through two ads and then played the requested song completely, while
AppleScript and Spotify URI deep links sometimes resumed another song from the
existing context. Those automated attempts fail closed rather than claiming the
wrong track. Use Premium Web API control when exact unattended selection is a
requirement.

The manual fader is proven only when Spotify is playing on this Mac. A remote
Connect target may ignore an AppleScript volume write; `station fader` reads the
level back and fails rather than reporting a move that did not stick.

### Premium Web API and exact Connect targeting

Open the official [Spotify Developer Dashboard](https://developer.spotify.com/dashboard),
create an app, select **Web API**, and add this exact Redirect URI in the app settings:

```text
http://127.0.0.1:8899/callback
```

Copy the app's **Client ID**. This PKCE client has no use for the Client Secret.
Spotify requires the authorization request to use a registered redirect URI,
and the value must match exactly.

```sh
export SPOTIFY_CLIENT_ID='your public client id'
station spotify auth
station spotify devices
station list
```

The authorization includes read-only playlist, long-term affinity, and recent
playback scopes for the Spotify shelf. `station list` detects the Spotify
backend and shows one primary playlist in long-term Top 50 order, the remaining
playlist tracks in their saved order, recent footprints, then Discover Weekly
when Spotify exposes that playlist to the app. A followed Weekly playlist that
the listener does not own or collaborate on may be marked unavailable without
hiding the primary shelf. LIST never plays a track or writes the rundown. If
several ordinary playlists are available, set one exact name, URI, URL, or ID:

```toml
[spotify]
primary_playlist = "My station shelf"
weekly_playlist = ""
```

`station spotify list --json` exposes exact URIs and evidence to an agent. An
older token without the shelf scopes fails with one action: run
`station spotify auth` again. The AppleScript/Free adapter has no account
catalog access, so its LIST is intentionally unavailable.

No client secret is requested or stored. Keep Spotify open on the target Mac.
An explicitly named but inactive Connect device is allowed: the play request
activates that exact device before repeat mode is normalized.

## 5. Read-only preflight

```sh
station doctor
```

`doctor` validates tools, config, required token structure/scopes, and file
permissions without playing audio. For `web_api`, continue once
`station spotify devices` returns the exact target Mac. For
`macos_applescript`, run `station spotify now`; it reports the current session
but deliberately cannot prove the selected Connect target.

## 6. Prove each link in order

Voice only:

```sh
station open --say "Audience of One voice check."
```

Track only:

```sh
station open "spotify:track:1ZgMsA55GIY7ICkQh5MILA" --device local_mac
```

The AppleScript adapter needs an exact URI or public track URL. The Web API
adapter also accepts `Title Artist` and fails closed when catalog evidence
conflicts. A URI is the only identity input that does not depend on search
ranking.

Combined opener with the default overlap:

```sh
station open "spotify:track:1ZgMsA55GIY7ICkQh5MILA" \
  --say "Good evening. This is Audience of One." \
  --transition overlap \
  --device local_mac \
  --json
```

The direct opener supports:

- `overlap`: prove the music bed, fade Spotify to 35, speak, then restore;
- `intro`: like overlap, with a short breath before speech;
- `clean`: finish speech, then start the track;
- `blackout`: speech, a short deliberate silence, then the track; no fade or duck;
- `hard`: start the track and speak without ducking.

`tail` is intentionally rejected by direct `station open`; it belongs to the
running scheduler below, where voice is prepared early and timed against the
current record's remaining duration. Transition depth and speed are creative
choices, not correctness rules; edit `[desktop]` after the default path works.
Old queued data using `dark` is normalized to `blackout`; the public CLI exposes
only the unambiguous name.

Manual local Spotify rides are separate from programme transitions:

```sh
station fader 40 --seconds 0.8
station fader restore --seconds 1.0
```

The first move saves the original Spotify app volume as a durable restore point.
Later moves keep that point until `restore` succeeds. The command never changes
system volume, and the shared desktop lock prevents it from fighting an active
direct playout.

## 7. Hand the rundown to the scheduler

Queue at least two exact tracks, inspect the order, then start the shift:

```sh
station queue "spotify:track:first" "Welcome in." --transition overlap
station queue "spotify:track:second" "One more before we go." --transition tail
station rundown
station start
station status
```

The first item cold-starts when no music is playing. While a record is on air,
the scheduler prepares the next item inside the configured 75-second window and
fires from current position and duration evidence. A `tail` voice is timed so it
rides the old record before the new track begins. If Spotify autoplay changes
the URI first, the queued programme takes the console back immediately.

Failed items remain inspectable and do not block later queued work. `--after repeat`
loops the last scheduled track only while the rundown is empty; a new
queued item clears repeat and takes the next boundary. `--after stop` pauses at
the last boundary when no programme follows.

```sh
station stop   # stop scheduling; current music keeps playing
station off    # pause the current music after the scheduler is stopped
```

## 8. Receipts and failures

A successful direct item moves from `queue/` to `played/`. Its transaction JSON
contains independent voice-process, track, repeat, position-motion, duck, and
restore evidence. A failed item stays in `queue/` with state `failed`, even when
Spotify happens to be playing a different track.

These are control-plane receipts, not microphone measurements. `afplay` staying
alive and exiting zero does not detect muted system output; Spotify's advancing
position does not detect a muted app, device, or disconnected acoustic route.
The CLI therefore says `PLAYOUT CONFIRMED`, not “audibly heard.”

Retry the same failed item and preserve its previous attempt identity:

```sh
station retry ITEM_ID --json
```

When the previous attempt completed its voice process but never confirmed the
promised track, retry resumes the track only; it does not repeat the announcement.
The same command can reclaim an item left in `preparing`, `ready`, or `firing`
after an interrupted process. A non-blocking station lock prevents two desktop
playouts from owning the state at once.

Inspect completed items or stop the current Spotify playback with a confirmed
pause on the same target:

```sh
station history --json
station off --device local_mac
```

If a clean/blackout voice process completed but its promised track could
not be confirmed, a verified local `cover-1.mp3` is played once when available:

```sh
station covers build
```

Both configured recovery lines must synthesize and pass `ffprobe` before an
atomic generation pointer makes the pair current. A crash cannot publish one new
cover alongside one old cover.

Recovery covers and wildcard liners are different instruments. A cover is used
only when the promised music did not materialize. When Spotify Free substitutes
a different real track on a phone, the transaction records requested A and
actual B, then proves that B advances through a second stability window. An
optional cached liner may be selected only after that proof:

```sh
station liners build
```

`[improv].liner_chance` controls the retry-stable chance; `0.25` means one chance
in four, not every substitution. The previous phone-confirmed liner is excluded
when another is available. `selected=true` is only a programming decision.
`played=true` requires a phone `voice_finished` receipt, while duck start/finish
and event order are recorded separately.

v0.1 includes the Mac-side delivery and ACK client,
plus the matching public Termux receiver. Keep `[improv].enabled = false` in the
desktop quickstart; enabling it remains a Spotify Free experiment, not a
workaround for nondeterministic mobile control.

For the supported Spotify Premium phone path, follow
[the Android receiver guide](../android/README.md). The direct form is:

```sh
station open "spotify:track:..." \
  --say "A line for the phone." \
  --phone --device "Android Phone" --transition overlap --json
```

`android.device` and the resolved Spotify Connect target must be the same exact
name or ID. The Android helper owns the music duck, so the Mac does not move its
local Spotify fader for a phone item.

## Uninstall

Deactivate and delete the virtual environment, then remove only the public
paths if you no longer want their config or local receipts:

```sh
deactivate
rm -rf .venv
rm -rf ~/.config/audience-of-one ~/.local/state/audience-of-one
```

This does not remove Spotify, ffmpeg, provider accounts, or the source checkout.

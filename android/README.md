# Android mobile station (Spotify Premium)

Hand this guide to an AI agent. The agent can perform the installation
and should stop only for account login, Android permission dialogs, or a domain
decision. The human does not need to study every command below.

This is the 5G listening route: Spotify plays in the official Android client and
Termux plays the host voice. The Mac remains the powered, online control room;
its scheduler owns the rundown while phone-only hosting remains a later port. Track,
voice, and audio-focus receipts remain separate while music stays in Spotify's
own playback path.

This guide installs the proven Spotify route and the receiver used by the QQ
Music + Termux mpv preview. Owned local-file delivery is still a separate port;
QQ Music streams do not need a pre-copied file on Android.

## The honest bill before installation

| Item | Default stance | Can it be skipped? |
| --- | --- | --- |
| Powered, awake, online macOS control room with Python 3.11+, ffmpeg/ffprobe, an AI agent | Required today | Windows and phone-only hosting are later ports. |
| Spotify Premium, official Android Spotify app, Developer app and Client ID | Required for deterministic phone music | Free Spotify is not a reliable unattended mobile route. |
| MiniMax for Chinese and ElevenLabs for English | Recommended voice | Yes. Route both languages to macOS `say` for a zero-account smoke test. |
| F-Droid Termux, Termux:API and Termux:Boot from one signing source | Required | Keep all three in the same signing family. |
| One 5G transport | Required away from home | Choose Tailscale or a persistent Cloudflare Tunnel. A LAN is only the fallback bench. |
| Android focus helper | Required for verified phone ducking | Speech still works without it, but the receipt honestly says `duck_failed`. |
| Key Mapper / physical call-in | Optional shipped toy | Entirely. It is not part of first installation or radio reliability. |

## 1. Prepare the Mac first

Install `station` from the [desktop quickstart](../docs/DESKTOP-QUICKSTART.md),
but choose the mobile profile **before** filling in configuration.

For a fresh install, this command refuses to overwrite an existing file:

```sh
mkdir -p ~/.config/audience-of-one
test ! -e ~/.config/audience-of-one/config.toml || {
  echo "Config already exists; edit it in place instead of overwriting it."
  exit 1
}
cp examples/config.android-premium.toml ~/.config/audience-of-one/config.toml
chmod 600 ~/.config/audience-of-one/config.toml
```

If desktop radio already works, keep that file. Use
`examples/config.android-premium.toml` only as a reference and add/edit its
`[android]` table alongside the working Spotify/TTS settings.

Now complete the Premium Web API route: create the Spotify developer app,
authorize it, and make a Mac voice-only test succeed. With desktop OAuth and TTS
proven, move on to Termux.

The profile recommends MiniMax plus ElevenLabs. Keep secrets outside TOML:

```sh
umask 077
cat > ~/.config/audience-of-one/secrets.env <<'EOF'
SPOTIFY_CLIENT_ID='replace-me'
MINIMAX_API_KEY='replace-me'
ELEVENLABS_API_KEY='replace-me'
STATION_PHONE_MCP_URL='replace-after-choosing-a-transport'
STATION_PHONE_MCP_TOKEN='replace-after-installing-the-phone'
EOF
```

Edit the placeholders locally, and put your own MiniMax and ElevenLabs
`voice_id` values into `config.toml`. Provider accounts and synthesis usage may
cost money. If you want only one API provider, point both `chinese` and `english`
at that provider; two vendors are a house preference, not a protocol law.

Before starting the AI agent or running a station command from a new shell,
load that file:

```sh
set -a
. ~/.config/audience-of-one/secrets.env
set +a
```

For the free smoke voice, change only the TTS routing and leave the recommended
providers available for later:

```toml
[tts]
chinese = "macos"
english = "macos"
```

This fallback sounds like a Mac because it is a Mac. Its job is to prove the
wire, not win the voice-acting award.

## 2. Install one known-good Termux family

The station was migrated and verified with these F-Droid-signed APKs. The
filenames are F-Droid version codes, not a promise that no newer compatible
release will appear.

| Package | Tested APK | SHA-256 |
| --- | --- | --- |
| Termux | `com.termux_1002.apk` | `e6265a57eb5ca363808488e3b01955958bed93bc0c8a0d281849b363b11027ec` |
| Termux:API | `com.termux.api_1002.apk` | `4497dbbf81906df52e59ed387a5223d225aa0de3aca817cc557a621e4dadda44` |
| Termux:Boot | `com.termux.boot_1000.apk` | `6f7cf9b94f539d3efd4af3544ff819947b49395275d8cfa7e5f80de14f3d9cf8` |

For a reproducible install, download the exact official F-Droid files on the Mac
and verify them before transferring them to Android:

```sh
mkdir -p ~/Downloads/audience-of-one-termux
cd ~/Downloads/audience-of-one-termux
curl -fLO https://f-droid.org/repo/com.termux_1002.apk
curl -fLO https://f-droid.org/repo/com.termux.api_1002.apk
curl -fLO https://f-droid.org/repo/com.termux.boot_1000.apk
printf '%s  %s\n' \
  e6265a57eb5ca363808488e3b01955958bed93bc0c8a0d281849b363b11027ec com.termux_1002.apk \
  4497dbbf81906df52e59ed387a5223d225aa0de3aca817cc557a621e4dadda44 com.termux.api_1002.apk \
  6f7cf9b94f539d3efd4af3544ff819947b49395275d8cfa7e5f80de14f3d9cf8 com.termux.boot_1000.apk \
  | shasum -a 256 -c -
```

Alternatively, download [Termux](https://f-droid.org/packages/com.termux/),
[Termux:API](https://f-droid.org/packages/com.termux.api/), and
[Termux:Boot](https://f-droid.org/packages/com.termux.boot/) from F-Droid, or
install all three through the F-Droid client. Termux uses a shared signing
identity with its plugins, so every member must come from the same source.
Newer all-F-Droid versions may work, but they are not this tested baseline until
they pass the clean-device, lock-screen and reboot checks below.

If Google Play Termux or another signing family is already installed, back up
anything you own first, uninstall Termux **and all Termux plugins**, then install
the F-Droid family. Changing signing source is a destructive reinstall and
clears Termux `$HOME`; it is not an in-place update.

Open Termux and Termux:Boot once after installation. Opening Boot is what
authorizes it to receive the next device boot. Termux:API may have no launcher;
Android asks for its relevant permission when a `termux-*` command first needs it.

## 3. Give Android permission to keep the radio alive

Android and OEM wording varies. Judge the resulting state, not the menu label.

For Termux, Termux:API and Termux:Boot:

- allow notifications;
- set battery use to unrestricted / not optimized;
- allow background activity and automatic launch if the phone exposes those
  controls;
- keep mobile data and background data enabled.

For Spotify, allow mobile/background data and let it remain available in the
background. Open it,
sign in to the Premium account, play one song manually, then pause it. That gives
Spotify Connect a real Android device to discover; an installed-but-never-opened
Spotify icon is not a playback target.

In Termux:

```sh
termux-setup-storage
pkg update
pkg install python mpv pulseaudio curl coreutils termux-api git
```

Accept Android's shared-storage prompt. The station uses shared storage only for
its own authenticated inbox, acknowledgements and health file.

## 4. Install the phone receiver

Clone the public repository on the phone:

```sh
git clone https://github.com/idleprocesscc/audience-of-one.git ~/audience-of-one
bash ~/audience-of-one/android/termux/install.sh
station-phone status
```

Expected core state:

```text
mcp=up
player=up
player_heartbeat={..."state":"running"...}
music=idle
```

The installer prints one random phone token. Put it in the Mac's
`STATION_PHONE_MCP_TOKEN` value. It is worth keeping that token local and taking
a quick look before publishing logs or screenshots; no hazmat suit is required.

The phone also keeps its own human controls. They work locally in Termux even
when the Mac, tunnel, or agent is unavailable:

```sh
fm       # pause or resume
fm off   # stop
fm s     # show playing, paused, or idle
```

These commands control the receiver's mpv route. Spotify remains an ordinary
Spotify session, so its own play/pause control remains the human control there.

The installer changes only:

```text
~/.config/audience-of-one/
~/.local/lib/audience-of-one/
~/.local/state/audience-of-one-phone/
~/storage/shared/Download/AudienceOfOne/
~/.termux/boot/audience-of-one-phone
$PREFIX/bin/station-phone
```

## 5. Choose the normal 5G route

Follow one recipe in [Transport choices](TRANSPORTS.md). The installation agent
uses Tailscale when no other phone VPN must remain, or Cloudflare when one must;
LAN is only the diagnostic bench. It writes the explicit choice into the blank
`[android].transport` field and the resulting `/mcp` URL into
`STATION_PHONE_MCP_URL`. The transport field records the choice; `station`
itself does not create the external account or network.

## 6. Match music and voice to the same phone

On the Mac, with Spotify open on Android:

```sh
station spotify devices
```

Copy the exact returned Android name or ID into both of these fields:

```toml
[spotify]
device = "the exact Android device"

[android]
enabled = true
device = "the exact Android device"
location_id = "station"
```

This equality is intentional. Music routed to one Connect device plus speech
sent to another phone is two successful tools and one failed radio.

## 7. Test in increasing order of consequence

First prove the phone health endpoint from the Mac as described in the selected
transport recipe. Then load the secret environment and run:

```sh
station config validate
station doctor --phone
station say "Phone line check." --phone --json
```

`station say --phone` is the intercom. It does not start, pause, or change music;
it only synthesizes one line, sends it to the phone, and waits for real start and
finish receipts. By installing it, you are giving your agent a small direct line
to your ears. This is useful for “bring an umbrella” and equally capable of
shouting nonsense during lunch. Configure agents accordingly and consider
yourself formally warned.

Now prove track-only and then the combined programme:

```sh
station open "spotify:track:..." \
  --phone --device "the exact Android device" --json

station open "spotify:track:..." \
  --say "This line should ride over the music." \
  --phone \
  --device "the exact Android device" \
  --transition overlap \
  --json
```

Trust the URI/device, position-motion, `voice_started`, and `voice_finished`
receipts. `duck_failed` means the voice may have played at full volume; it does
not mean the voice vanished.

For QQ Music, complete the Mac-side companion login first, keep `music.backend =
"local"`, then target the same receiver explicitly:

```sh
station qqmusic open "qqmusic:exact-mid-from-list" \
  --phone --device "the configured Android receiver" --json
```

This does not open the official QQ Music Android app. The Mac resolves one
short-lived authorized stream and the receiver loads it into a persistent mpv.
Require `track_started` plus a matching `track_position` receipt before saying
it played. The transient URL is deleted from the shared inbox before sound and
is never copied into the transaction journal.

At the end of an agent-assisted installation, hand these three short controls
to the listener instead of treating a healthy background service as the end.

## 8. Optional focus helper

Without the helper, all phone speech remains receipted but Android may not lower
Spotify. The repository publishes reproducible source under `focus-helper/`;
each installation builds its own APK.

Install Android Studio with Android SDK 35 and Platform Tools, plus a separate
JDK 17 (`brew install openjdk@17` is the reference path). On the phone, enable
Developer options and USB debugging, connect it once, and accept that Mac's
debugging key. The repository ships Gradle Wrapper 8.10.2; no system Gradle
installation is required. The project uses Android Gradle Plugin 8.7.3,
compile/target SDK 35 and minimum SDK 26.

Command-line build from the repository root:

```sh
cd android/focus-helper
export JAVA_HOME="$(brew --prefix openjdk@17)/libexec/openjdk.jdk/Contents/Home"
export ANDROID_HOME="$HOME/Library/Android/sdk"
"$JAVA_HOME/bin/java" -version
./gradlew --no-daemon :app:assembleDebug
cd ../..
adb devices
adb install -r android/focus-helper/app/build/outputs/apk/debug/app-debug.apk
```

Android may ask once whether the receiver may open the transparent helper.
Allow that relationship, then rerun the combined programme and require
`duck_started` followed by `duck_finished`. A successful APK installation alone
is not a successful fader.

Capability note: the helper's exported `djfocus://` activity lets another
Android app on the same phone request a temporary audio-focus duck. It reads no
files and caps a lease at 120 seconds. Since 0.2.0 the same APK also exports a
`djrecord://` activity backing the optional voice call-in: it can record up to
60 seconds of microphone audio into the station folder under shared Downloads,
but only after the microphone permission is granted by hand (the details and
the privacy path live in [docs/CALL-IN.md](../docs/CALL-IN.md)). Install the
helper when verified ducking is worth exposing that narrow local control; the
microphone stays dead until you grant it.

## 9. Lock-screen and reboot acceptance

Termux:Boot runs `~/.termux/boot/audience-of-one-phone` after reboot. Test rather
than assume:

1. lock the phone for five minutes and repeat `station say --phone`;
2. switch Wi-Fi off so the phone is genuinely on mobile data and repeat it;
3. reboot, unlock once if Android requires it, wait one minute, then run
   `station-phone status` and repeat the intercom;
4. open Spotify and confirm the same Connect device before the first combined
   cold opener.

Manual recovery remains short:

```sh
station-phone status
station-phone restart
```

The player claims each item before decoding it. If it crashes after claiming,
the next start quarantines the uncertain clip rather than replaying a sentence
that may already have reached your ears.

## Optional toy shelf

A physical call-in — long-press Volume Up and the agent answers on air —
is described in [docs/CALL-IN.md](../docs/CALL-IN.md). It is deliberately
outside the first installation so the core path stays short. The ring-only
tier works with the shipped receiver and no app build; the voice tier now
ships too — the focus helper 0.2.0 APK carries a microphone foreground
service, the Termux scripts stage each clip, and `station call-in watch`
turns it into a transcript on the Mac. Voice needs the APK build, one
microphone grant, and an STT endpoint you choose; when any of that is
missing, the ring remains the honest fallback: one bit and no microphone
audio.

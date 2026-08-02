# Privacy map

Audience of One keeps configuration, OAuth tokens, generated audio, queues, and
receipts in user-owned local paths with restrictive permissions. The repository
contains placeholders only.

Each adapter has its own data path:

- MiniMax and ElevenLabs receive the full text submitted for synthesis. Personal
  lines therefore leave the Mac and are subject to the provider's current terms,
  logging, retention, account, and deletion controls.
- The Web API Spotify adapter sends search and playback-control requests under
  the listener's own account. OAuth tokens remain local, but Spotify processes
  the request metadata. The desktop branch requests only playback read and
  playback modify scopes.
- The macOS AppleScript adapter uses no developer app or OAuth token. Commands go
  to the installed Spotify app, which still communicates with Spotify under the
  listener's signed-in account. Public track URLs may be resolved by the host's
  own search tool before they reach this package.
- The macOS `say` smoke provider stays on the Mac and suits lines the listener
  prefers to keep local.
- The Android receiver accepts only a small authenticated file-tool surface
  below its configured station root. Its bearer token is generated on the phone
  and belongs with the listener's other local credentials. A public endpoint
  uses TLS plus the listener's tunnel or access policy.
- QQ Music credentials remain on the Mac. Android receives one expiring CDN
  task through that authenticated inbox; the receiver claims and deletes the
  transport copy before mpv playout, while receipts retain only a URL hash and
  position evidence.

Before sharing an issue or diagnostic archive, remove credentials, authorization
callbacks, token files, generated voices, device names, and personal programme
text. The useful engineering evidence is usually the event order, status codes,
and redacted receipts.

The Android focus helper posts only a playout request ID and focus state to a
Termux loopback listener. The repository carries its reproducible source; each
installation supplies its own APK, tunnel, token, and generated clips.

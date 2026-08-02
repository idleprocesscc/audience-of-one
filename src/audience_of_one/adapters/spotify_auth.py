"""Spotify Authorization Code with PKCE for a local desktop CLI."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from .spotify import TOKEN_URL, SpotifyClient, SpotifyError

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
DEFAULT_SCOPES = (
    "playlist-read-collaborative",
    "playlist-read-private",
    "user-modify-playback-state",
    "user-read-recently-played",
    "user-read-playback-state",
    "user-top-read",
)


def validate_redirect_uri(value: str) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
        raise SpotifyError(
            "Spotify redirect_uri must use an explicit loopback address such as "
            "http://127.0.0.1:8899/callback"
        )
    if not parsed.port or not parsed.path.startswith("/"):
        raise SpotifyError("Spotify redirect_uri needs an explicit port and callback path")
    return parsed


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def authorization_url(client_id: str, redirect_uri: str, state: str,
                      challenge: str, scopes: tuple[str, ...] = DEFAULT_SCOPES) -> str:
    validate_redirect_uri(redirect_uri)
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": " ".join(scopes),
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    })


def exchange_code(client: SpotifyClient, code: str, verifier: str) -> dict:
    request = urllib.request.Request(
        client.config.get("token_endpoint") or TOKEN_URL,
        data=urllib.parse.urlencode({
            "client_id": client._credential("client_id_env"),
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": client.config["redirect_uri"],
            "code_verifier": verifier,
        }).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            tokens = json.loads(response.read())
    except Exception as error:
        raise SpotifyError(f"Spotify authorization exchange failed: {error}") from error
    if not tokens.get("access_token") or not tokens.get("refresh_token"):
        raise SpotifyError("Spotify authorization response omitted required tokens")
    tokens["expires_at"] = time.time() + int(tokens["expires_in"])
    tokens["authorized_at"] = time.time()
    client._save_tokens(tokens)
    return {
        "authorized": True,
        "scope": tokens.get("scope", ""),
        "expires_in": int(tokens["expires_in"]),
    }


def authorize_interactive(client: SpotifyClient, *, open_browser: bool = True,
                          timeout: float = 240) -> dict:
    redirect_uri = client.config.get("redirect_uri") or ""
    parsed = validate_redirect_uri(redirect_uri)
    verifier, challenge = pkce_pair()
    expected_state = secrets.token_urlsafe(24)
    url = authorization_url(
        client._credential("client_id_env"), redirect_uri,
        expected_state, challenge,
    )
    result: dict[str, str] = {}
    callback_path = parsed.path

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            incoming = urllib.parse.urlparse(self.path)
            if incoming.path != callback_path:
                self.send_response(404)
                self.end_headers()
                return
            values = urllib.parse.parse_qs(incoming.query)
            result.update({key: value[0] for key, value in values.items() if value})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Audience of One: Spotify authorization received. You may close this tab.")

        def log_message(self, *_args):
            return

    server = HTTPServer((parsed.hostname, parsed.port), Handler)
    server.timeout = max(1, float(timeout))
    print(f"Open this Spotify authorization URL:\n{url}")
    if open_browser:
        webbrowser.open(url)
    server.handle_request()
    server.server_close()
    if not result:
        raise SpotifyError("Spotify authorization timed out")
    if result.get("state") != expected_state:
        raise SpotifyError("Spotify authorization state mismatch")
    if result.get("error"):
        raise SpotifyError(f"Spotify authorization denied: {result['error']}")
    if not result.get("code"):
        raise SpotifyError("Spotify callback did not contain an authorization code")
    return exchange_code(client, result["code"], verifier)

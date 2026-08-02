from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from audience_of_one.adapters import spotify_auth
from audience_of_one.adapters.spotify import SpotifyClient, SpotifyError


class SpotifyPKCETests(unittest.TestCase):
    def test_redirect_requires_explicit_loopback_not_localhost(self):
        with self.assertRaises(SpotifyError):
            spotify_auth.validate_redirect_uri("http://localhost:8899/callback")
        parsed = spotify_auth.validate_redirect_uri("http://127.0.0.1:8899/callback")
        self.assertEqual(parsed.port, 8899)

    def test_authorization_url_has_pkce_state_and_minimum_scopes(self):
        url = spotify_auth.authorization_url(
            "example-client", "http://127.0.0.1:8899/callback",
            "state-value", "challenge-value",
        )
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["code_challenge"], ["challenge-value"])
        self.assertEqual(query["state"], ["state-value"])
        self.assertIn("user-modify-playback-state", query["scope"][0])
        self.assertIn("user-read-playback-state", query["scope"][0])
        self.assertIn("user-top-read", query["scope"][0])
        self.assertIn("user-read-recently-played", query["scope"][0])
        self.assertIn("playlist-read-private", query["scope"][0])
        self.assertNotIn("client_secret", query)

    @mock.patch("audience_of_one.adapters.spotify_auth.urllib.request.urlopen")
    def test_exchange_writes_private_token_file_without_client_secret(self, urlopen):
        response = mock.MagicMock()
        response.read.return_value = json.dumps({
            "access_token": "test-access",
            "refresh_token": "test-refresh",
            "expires_in": 3600,
            "scope": "user-read-playback-state",
        }).encode()
        urlopen.return_value.__enter__.return_value = response
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(
            os.environ, {"SPOTIFY_CLIENT_ID": "example-client"}
        ):
            client = SpotifyClient({
                "client_id_env": "SPOTIFY_CLIENT_ID",
                "redirect_uri": "http://127.0.0.1:8899/callback",
            }, Path(raw))
            receipt = spotify_auth.exchange_code(client, "code", "verifier")
            saved = json.loads(client.token_path.read_text())
            self.assertTrue(receipt["authorized"])
            self.assertEqual(saved["refresh_token"], "test-refresh")
            self.assertEqual(client.token_path.stat().st_mode & 0o777, 0o600)
            request = urlopen.call_args.args[0]
            body = urllib.parse.parse_qs(request.data.decode())
            self.assertEqual(body["client_id"], ["example-client"])
            self.assertNotIn("client_secret", body)


if __name__ == "__main__":
    unittest.main()

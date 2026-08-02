from __future__ import annotations

import unittest

from audience_of_one.adapters import spotify_resolver as resolver


def track(name: str, artist: str, uri: str) -> dict:
    return {
        "name": name,
        "artists": [{"name": artist, "uri": f"spotify:artist:{artist}"}],
        "uri": uri,
    }


class ResolverTests(unittest.TestCase):
    def test_rejects_unrelated_search_recommendations(self):
        wrong = [
            track("West Coast", "Lana Del Rey", "spotify:track:west"),
            track("The Boys of Summer", "The Ataris", "spotify:track:ataris"),
        ]
        self.assertIsNone(resolver.select_track("Two Punks In Love Ataris", wrong))

    def test_wrong_artist_suffix_is_rejected_even_when_title_matches(self):
        intended = track("Two Punks In Love", "bülow", "spotify:track:intended")
        self.assertIsNone(resolver.select_track("Two Punks In Love Ataris", [intended]))

    def test_artist_breaks_same_title_tie(self):
        cover = track("The Book of Love", "The Magnetic Fields", "spotify:track:cover")
        intended = track("The Book of Love", "Peter Gabriel", "spotify:track:intended")
        self.assertIs(
            resolver.select_track("The Book of Love Peter Gabriel", [cover, intended]),
            intended,
        )

    def test_extended_title_requires_artist_anchor(self):
        intended = track(
            "The Moon Song - Studio Version Duet", "Karen O", "spotify:track:moon"
        )
        self.assertIs(resolver.select_track("Moon Song Karen O", [intended]), intended)

    def test_relaxed_title_search_still_validates_original_request(self):
        wrong = track("West Coast", "Lana Del Rey", "spotify:track:wrong")
        intended = track("Two Punks In Love", "bülow", "spotify:track:intended")

        def api(method, path, params=None, body=None):
            self.assertEqual((method, path), ("GET", "/v1/search"))
            items = [intended] if params["q"] == 'track:"Two Punks In Love"' else [wrong]
            return {"tracks": {"items": items}}

        uri, receipt = resolver.resolve_uri("Two Punks In Love bülow", api)
        self.assertEqual(uri, "spotify:track:intended")
        self.assertIs(receipt, intended)

    def test_artist_search_breaks_catalog_tie(self):
        wrong = track("Clair de Lune", "Johann Debussy", "spotify:track:wrong")
        intended = track("Clair de lune", "Claude Debussy", "spotify:track:intended")

        def api(method, path, params=None, body=None):
            if params["type"] == "track":
                return {"tracks": {"items": [wrong, intended]}}
            return {"artists": {"items": [
                intended["artists"][0], wrong["artists"][0],
            ]}}

        uri, _ = resolver.resolve_uri("Clair de Lune Debussy", api)
        self.assertEqual(uri, "spotify:track:intended")

    def test_explicit_uri_bypasses_api(self):
        def forbidden(*_args, **_kwargs):
            raise AssertionError("API should not be called")

        uri, receipt = resolver.resolve_uri("spotify:track:known", forbidden)
        self.assertEqual(uri, "spotify:track:known")
        self.assertIsNone(receipt)

    def test_rejected_result_is_reported_not_accepted(self):
        wrong = track("West Coast", "Lana Del Rey", "spotify:track:wrong")

        def api(_method, _path, params=None, body=None):
            return {"tracks": {"items": [wrong]}}

        with self.assertRaisesRegex(resolver.SpotifyResolveError, "rejected top result"):
            resolver.resolve_uri("Two Punks In Love Ataris", api)

    def test_title_only_query_does_not_invent_an_artist_requirement(self):
        intended = track("Space Song", "Beach House", "spotify:track:space")
        self.assertIs(resolver.select_track("Space Song", [intended]), intended)

    def test_title_only_query_rejects_equal_matches_by_different_artists(self):
        first = track("Hello", "Artist A", "spotify:track:a")
        second = track("Hello", "Artist B", "spotify:track:b")
        self.assertIsNone(resolver.select_track("Hello", [first, second]))

        def api(_method, _path, params=None, body=None):
            return {"tracks": {"items": [first, second]}}

        with self.assertRaises(resolver.SpotifyResolveError):
            resolver.resolve_uri("Hello", api)

    def test_by_artist_syntax_requires_that_artist(self):
        wrong = track("The Book of Love", "The Magnetic Fields", "spotify:track:wrong")
        intended = track("The Book of Love", "Peter Gabriel", "spotify:track:intended")
        self.assertIsNone(resolver.select_track("The Book of Love by Peter Gabriel", [wrong]))
        self.assertIs(
            resolver.select_track("The Book of Love by Peter Gabriel", [intended]), intended
        )


if __name__ == "__main__":
    unittest.main()

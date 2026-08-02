"""Conservative Spotify search result validation."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from typing import Any

SEARCH_LIMIT = 10
MAX_TITLE_SUFFIX_WORDS = 4
TITLE_METADATA_MARKERS = {
    "acoustic", "demo", "edit", "extended", "feat", "featuring", "instrumental",
    "live", "mix", "mono", "remaster", "remastered", "remix", "session", "studio",
    "version",
}


class SpotifyResolveError(RuntimeError):
    pass


def text_tokens(value: Any) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKD", str(value).casefold())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return tuple(part for part in re.sub(r"[^\w]+", " ", normalized).split() if part)


def contains_run(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    width = len(needle)
    return any(haystack[index:index + width] == needle
               for index in range(len(haystack) - width + 1))


def outside_run(haystack: tuple[str, ...], needle: tuple[str, ...]) -> tuple[str, ...]:
    if not needle:
        return haystack
    width = len(needle)
    for index in range(len(haystack) - width + 1):
        if haystack[index:index + width] == needle:
            remainder = haystack[:index] + haystack[index + width:]
            return remainder[1:] if remainder[:1] == ("by",) else remainder
    return haystack


def longest_common_run(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    previous = [0] * (len(right) + 1)
    longest = 0
    for left_token in left:
        current = [0]
        for index, right_token in enumerate(right, 1):
            value = previous[index - 1] + 1 if left_token == right_token else 0
            current.append(value)
            longest = max(longest, value)
        previous = current
    return longest


def title_core_tokens(name: str) -> tuple[str, ...]:
    tokens = list(text_tokens(name))
    for index, token in enumerate(tokens):
        if token in TITLE_METADATA_MARKERS:
            tokens = tokens[:index]
            break
    while len(tokens) > 1 and tokens[0] in {"a", "an", "the"}:
        tokens.pop(0)
    return tuple(tokens)


def track_match(query: str, track: dict) -> tuple | None:
    query_tokens = text_tokens(query)
    title_tokens = text_tokens(track.get("name", ""))
    core_tokens = title_core_tokens(track.get("name", "")) or title_tokens
    artist_tokens = tuple(
        token
        for artist in track.get("artists", [])
        for token in text_tokens(artist.get("name", ""))
    )
    if not query_tokens or not core_tokens:
        return None

    exact_title = contains_run(query_tokens, title_tokens)
    exact_core = contains_run(query_tokens, core_tokens)
    title_run = longest_common_run(query_tokens, core_tokens)
    query_set = set(query_tokens)
    title_coverage = len(set(core_tokens) & query_set) / len(set(core_tokens))
    artist_hits = len(set(artist_tokens) & query_set)

    confident = exact_title or exact_core
    if not confident and title_run >= 2 and title_coverage >= 0.45 and artist_hits:
        confident = True
    if confident and len(core_tokens) == 1:
        confident = bool(artist_hits or query_tokens == core_tokens)
    if not confident:
        return None
    matched_title = title_tokens if exact_title else core_tokens
    artist_hint_tokens = outside_run(query_tokens, matched_title)
    if artist_hint_tokens and not contains_run(artist_tokens, artist_hint_tokens):
        return None
    return (
        100 if exact_title else 0,
        80 if exact_core else 0,
        artist_hits,
        title_run,
        title_coverage,
    )


def rank_tracks(query: str, items: list[dict]) -> list[tuple]:
    matches = []
    for index, track in enumerate(items):
        confidence = track_match(query, track)
        if confidence is not None:
            matches.append((confidence, -index, track))
    return sorted(matches, key=lambda entry: entry[:2], reverse=True)


def select_track(query: str, items: list[dict]) -> dict | None:
    matches = rank_tracks(query, items)
    if not matches:
        return None
    best_confidence = matches[0][0]
    tied = [entry[2] for entry in matches if entry[0] == best_confidence]
    artist_sets = {
        tuple(artist.get("uri") or artist.get("name") for artist in track.get("artists", []))
        for track in tied
    }
    if len(artist_sets) > 1:
        return None
    return tied[0]


def artist_hint(query: str, track: dict) -> str:
    query_tokens = text_tokens(query)
    core_tokens = title_core_tokens(track.get("name", ""))
    if not core_tokens:
        return ""
    for index in range(len(query_tokens) - len(core_tokens) + 1):
        if query_tokens[index:index + len(core_tokens)] == core_tokens:
            remainder = query_tokens[:index] + query_tokens[index + len(core_tokens):]
            return " ".join(remainder)
    return ""


def _search(api: Callable, query: str, item_type: str) -> list[dict]:
    result = api("GET", "/v1/search", {
        "q": query,
        "type": item_type,
        "limit": SEARCH_LIMIT,
    }) or {}
    return (result.get(f"{item_type}s") or {}).get("items") or []


def select_with_artist_tiebreak(query: str, items: list[dict], api: Callable) -> dict | None:
    ranked = rank_tracks(query, items)
    if not ranked:
        return None
    best_confidence = ranked[0][0]
    tied = [entry[2] for entry in ranked if entry[0] == best_confidence]
    if len(tied) < 2:
        return tied[0]
    artist_sets = {
        tuple(artist.get("uri") or artist.get("name") for artist in track.get("artists", []))
        for track in tied
    }
    if len(artist_sets) < 2:
        return tied[0]
    hint = artist_hint(query, tied[0])
    if not hint:
        return None
    artist_rank = {
        artist.get("uri"): index
        for index, artist in enumerate(_search(api, hint, "artist"))
        if artist.get("uri")
    }
    ranked_ties = []
    for original_index, track in enumerate(tied):
        ranks = [
            artist_rank[artist.get("uri")]
            for artist in track.get("artists", [])
            if artist.get("uri") in artist_rank
        ]
        if ranks:
            ranked_ties.append((min(ranks), original_index, track))
    return min(ranked_ties, default=(0, 0, tied[0]))[2]


def resolve_uri(value: str, api: Callable) -> tuple[str, dict | None]:
    if value.startswith("spotify:"):
        return value, None

    items = _search(api, value, "track")
    track = select_with_artist_tiebreak(value, items, api)
    words = value.split()
    for removed in range(1, min(MAX_TITLE_SUFFIX_WORDS, len(words) - 1) + 1):
        if track is not None:
            break
        title_guess = " ".join(words[:-removed])
        if len(text_tokens(title_guess)) < 2:
            break
        relaxed = _search(api, f'track:"{title_guess}"', "track")
        track = select_with_artist_tiebreak(value, relaxed, api)
    if track is None:
        top = items[0] if items else None
        detail = ""
        if top:
            artists = ", ".join(artist.get("name", "?") for artist in top.get("artists", []))
            detail = f"; rejected top result {top.get('name', '?')} — {artists}"
        raise SpotifyResolveError(f"no confident track match for {value!r}{detail}")
    return track["uri"], track

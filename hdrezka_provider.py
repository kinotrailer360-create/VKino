from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

try:
    from HDrezka import HDrezka
except Exception:  # pragma: no cover - provider is optional
    HDrezka = None  # type: ignore[assignment]


class HDRezkaProviderError(RuntimeError):
    pass


@dataclass
class HDRezkaResolved:
    source_url: str
    matched_title: str
    matched_year: int | None
    sources: list[dict[str, Any]]


def _norm(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold().replace("ё", "е")
    return "".join(ch for ch in text if ch.isalnum())


def _year(value: Any) -> int | None:
    match = re.search(r"(?:19|20)\d{2}", str(value or ""))
    return int(match.group(0)) if match else None


def _quality_number(value: Any) -> int | None:
    match = re.search(r"(360|480|720|1080)p", str(value or ""), re.I)
    return int(match.group(1)) if match else None


class HDRezkaProvider:
    """Synchronous adapter around kristal374/hdrezka-api.

    The Telegram bot calls this adapter through asyncio.to_thread(), because the
    upstream library uses the synchronous requests package.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        mirror: str = "",
        default_voice: str = "",
        max_voices: int = 8,
    ) -> None:
        self.enabled = bool(enabled)
        self.mirror = mirror.strip().rstrip("/")
        self.default_voice = default_voice.strip()
        self.max_voices = max(1, min(int(max_voices or 8), 24))

    def _client(self):
        if not self.enabled:
            raise HDRezkaProviderError("HDRezka provider is disabled")
        if HDrezka is None:
            raise HDRezkaProviderError("hdrezka-api package is not installed")
        return HDrezka(mirror=self.mirror or None)

    def _base_url(self) -> str:
        return self.mirror or "https://rezka.ag"

    def _rewrite_page_url(self, value: str) -> str:
        if not value:
            return value
        if not self.mirror:
            return urljoin(self._base_url() + "/", value)

        target = urlsplit(value)
        base = urlsplit(self.mirror)
        if not target.netloc:
            return urljoin(self.mirror + "/", value)
        return urlunsplit(
            (
                base.scheme or "https",
                base.netloc,
                target.path,
                target.query,
                target.fragment,
            )
        )

    @staticmethod
    def _media_matches(entity: str | None, media_type: str) -> bool:
        value = str(entity or "").casefold()
        serial = any(token in value for token in ("сериал", "series", "tv"))
        return serial if media_type == "series" else not serial

    def _score_poster(
        self,
        poster: Any,
        titles: Iterable[str],
        target_year: int | None,
        media_type: str,
    ) -> int:
        candidate = _norm(getattr(poster, "title", ""))
        if not candidate:
            return -1000

        score = 0
        title_norms = [_norm(x) for x in titles if _norm(x)]
        for target in title_norms:
            if candidate == target:
                score = max(score, 120)
            elif candidate in target or target in candidate:
                score = max(score, 80)
            else:
                common = set(re.findall(r"\w+", str(getattr(poster, "title", "")).casefold()))
                requested = set(re.findall(r"\w+", " ".join(titles).casefold()))
                if common and requested:
                    overlap = len(common & requested) / max(1, len(requested))
                    score = max(score, int(overlap * 50))

        candidate_year = _year(getattr(poster, "year", None))
        if target_year and candidate_year:
            score += 45 if candidate_year == target_year else -30

        score += 20 if self._media_matches(getattr(poster, "entity", None), media_type) else -20
        return score

    def _search_page(
        self,
        *,
        titles: list[str],
        year: int | None,
        media_type: str,
    ) -> tuple[str, str, int | None]:
        client = self._client()
        candidates: dict[str, tuple[int, Any]] = {}

        for query in [x.strip() for x in titles if x and x.strip()]:
            try:
                posters = client.search(query).get()
            except Exception:
                continue

            for poster in posters or []:
                raw_url = str(getattr(poster, "url", "") or "")
                if not raw_url:
                    continue
                page_url = self._rewrite_page_url(raw_url)
                score = self._score_poster(poster, titles, year, media_type)
                previous = candidates.get(page_url)
                if previous is None or score > previous[0]:
                    candidates[page_url] = (score, poster)

        if not candidates:
            raise HDRezkaProviderError("No matching title found")

        page_url, (score, poster) = max(candidates.items(), key=lambda item: item[1][0])
        if score < 55:
            raise HDRezkaProviderError("No sufficiently close title match")

        return (
            page_url,
            str(getattr(poster, "title", "") or ""),
            _year(getattr(poster, "year", None)),
        )

    @staticmethod
    def _match_season(seasons: list[Any], wanted: int) -> Any:
        for season in seasons:
            if int(getattr(season, "id", -1)) == wanted:
                return season
        for season in seasons:
            match = re.search(r"\d+", str(getattr(season, "title", "")))
            if match and int(match.group(0)) == wanted:
                return season
        raise HDRezkaProviderError(f"Season {wanted} is unavailable")

    @staticmethod
    def _match_episode(episodes: list[Any], wanted: int) -> Any:
        for episode in episodes:
            if int(getattr(episode, "id", -1)) == wanted:
                return episode
        for episode in episodes:
            match = re.search(r"\d+", str(getattr(episode, "title", "")))
            if match and int(match.group(0)) == wanted:
                return episode
        raise HDRezkaProviderError(f"Episode {wanted} is unavailable")

    def _translator_order(self, player: Any) -> list[Any]:
        translators = [t for t in list(getattr(player, "translate_list", []) or []) if not getattr(t, "premium", False)]
        popularity = dict(getattr(player, "popularity_translate", {}) or {})
        preferred = self.default_voice.casefold()

        def score(translator: Any) -> tuple[int, float]:
            names = [
                str(getattr(translator, "title", "") or ""),
                str(getattr(translator, "original_title", "") or ""),
                str(getattr(translator, "full_title", "") or ""),
            ]
            pref_score = 0
            if preferred:
                folded = [x.casefold() for x in names]
                if preferred in folded:
                    pref_score = 2
                elif any(preferred in x for x in folded):
                    pref_score = 1
            pop = float(popularity.get(getattr(translator, "full_title", ""), 0.0) or 0.0)
            return pref_score, pop

        translators.sort(key=score, reverse=True)
        return translators[: self.max_voices]

    @staticmethod
    def _voice_name(translator: Any) -> str:
        return str(
            getattr(translator, "full_title", None)
            or getattr(translator, "title", None)
            or getattr(translator, "original_title", None)
            or "Озвучка"
        ).strip()[:80]

    @staticmethod
    def _source_rows(player: Any, voice_name: str) -> list[dict[str, Any]]:
        try:
            urls = player.get_video_url()
        except Exception:
            return []
        if not isinstance(urls, dict):
            return []

        by_quality: dict[int, dict[str, Any]] = {}
        for raw_quality, raw_urls in urls.items():
            quality = _quality_number(raw_quality)
            if quality not in {360, 480, 720, 1080}:
                continue
            values = raw_urls if isinstance(raw_urls, (list, tuple)) else [raw_urls]
            direct = next(
                (
                    str(url)
                    for url in values
                    if str(url).startswith("https://")
                ),
                None,
            )
            if not direct:
                continue
            by_quality.setdefault(
                quality,
                {
                    "voice_name": voice_name,
                    "quality": quality,
                    "playback_url": direct,
                    "source_type": "mp4",
                },
            )
        return [by_quality[q] for q in sorted(by_quality)]

    def _extract_sources(
        self,
        movie: Any,
        *,
        season: int | None,
        episode: int | None,
    ) -> list[dict[str, Any]]:
        player = getattr(movie, "player", None)
        if player is None:
            raise HDRezkaProviderError("Player is unavailable")

        is_serial = hasattr(player, "seasons_tabs")
        if is_serial and (not season or not episode):
            raise HDRezkaProviderError("Season and episode are required for a series")

        sources: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()

        for translator in self._translator_order(player):
            try:
                if is_serial:
                    player.set_translate(translator)
                    season_obj = self._match_season(list(player.seasons_tabs), int(season))
                    episode_obj = self._match_episode(list(season_obj.episodes), int(episode))
                    player.set_params(
                        season_id=int(season_obj.id),
                        episode_id=int(episode_obj.id),
                    )
                else:
                    player.set_translate(translator)

                voice_name = self._voice_name(translator)
                for row in self._source_rows(player, voice_name):
                    key = (row["voice_name"], int(row["quality"]))
                    if key not in seen:
                        seen.add(key)
                        sources.append(row)
            except Exception:
                continue

        if not sources:
            raise HDRezkaProviderError("No playable qualities were returned")
        return sources

    def find_page(
        self,
        *,
        titles: list[str],
        year: int | None,
        media_type: str,
        cached_url: str = "",
    ) -> tuple[str, str, int | None]:
        """Return the matched HDRezka page without resolving video streams."""
        if not self.enabled:
            raise HDRezkaProviderError("HDRezka provider is disabled")

        if cached_url:
            page_url = self._rewrite_page_url(cached_url)
            return (
                page_url,
                titles[0] if titles else "",
                year,
            )

        return self._search_page(
            titles=titles,
            year=year,
            media_type=media_type,
        )

    def resolve(
        self,
        *,
        titles: list[str],
        year: int | None,
        media_type: str,
        season: int | None = None,
        episode: int | None = None,
        cached_url: str = "",
    ) -> HDRezkaResolved:
        if not self.enabled:
            raise HDRezkaProviderError("HDRezka provider is disabled")

        page_url = ""
        matched_title = ""
        matched_year: int | None = None

        if cached_url:
            try:
                page_url = self._rewrite_page_url(cached_url)
                movie = self._client().get(url=page_url)
                sources = self._extract_sources(movie, season=season, episode=episode)
                return HDRezkaResolved(
                    source_url=page_url,
                    matched_title=str(getattr(movie, "title", "") or titles[0]),
                    matched_year=year,
                    sources=sources,
                )
            except Exception:
                page_url = ""

        page_url, matched_title, matched_year = self._search_page(
            titles=titles,
            year=year,
            media_type=media_type,
        )
        movie = self._client().get(url=page_url)
        sources = self._extract_sources(movie, season=season, episode=episode)
        return HDRezkaResolved(
            source_url=page_url,
            matched_title=matched_title or str(getattr(movie, "title", "") or titles[0]),
            matched_year=matched_year,
            sources=sources,
        )

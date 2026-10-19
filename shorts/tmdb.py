"""Картинки с TMDB: кадры без надписей в высоком качестве и постеры."""

from __future__ import annotations

import logging
import random
from typing import Any

import aiohttp

API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/"

log = logging.getLogger("shorts")


async def _get(session: aiohttp.ClientSession, key: str, path: str, **params: Any) -> dict[str, Any]:
    async with session.get(f"{API}{path}", params={"api_key": key, **params}) as resp:
        resp.raise_for_status()
        return await resp.json()


async def _resolve(session: aiohttp.ClientSession, key: str, item: dict[str, Any]) -> tuple[str, int] | None:
    """Фильм Кинопоиска -> ("movie"|"tv", id на TMDB): по tmdb id, по imdb id, по названию и году."""
    kind = "tv" if item.get("isSeries") or "series" in str(item.get("type") or "") else "movie"
    ext = item.get("externalId") or {}
    try:
        if ext.get("tmdb"):
            return kind, int(ext["tmdb"])
    except (TypeError, ValueError):
        pass
    if ext.get("imdb"):
        data = await _get(session, key, f"/find/{ext['imdb']}", external_source="imdb_id")
        for k in ("movie", "tv"):
            if data.get(f"{k}_results"):
                return k, int(data[f"{k}_results"][0]["id"])
    title = item.get("enName") or item.get("alternativeName") or item.get("name")
    if not title:
        return None
    params: dict[str, Any] = {"query": title}
    if item.get("year"):
        params["first_air_date_year" if kind == "tv" else "year"] = item["year"]
    data = await _get(session, key, f"/search/{kind}", **params)
    results = data.get("results") or []
    return (kind, int(results[0]["id"])) if results else None


async def images(key: str, item: dict[str, Any], limit: int = 8) -> tuple[list[str], list[str]]:
    """(кадры без надписей, постеры) — лучшие по оценкам; пустые списки, если не нашлось."""
    if not key:
        return [], []
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            found = await _resolve(session, key, item)
            if not found:
                return [], []
            kind, tmdb_id = found
            data = await _get(session, key, f"/{kind}/{tmdb_id}/images",
                              include_image_language="null,ru,en")
    except Exception as exc:  # noqa: BLE001 — без TMDB берём картинки Кинопоиска
        log.info("shorts: TMDB images for %s failed: %s", item.get("id"), exc)
        return [], []

    def rank(img: dict[str, Any]) -> float:
        return float(img.get("vote_average") or 0) * 100 + float(img.get("vote_count") or 0)

    # кадры без текста (iso_639_1 = null) и не меньше 1280 по ширине
    backdrops = [b for b in data.get("backdrops") or []
                 if not b.get("iso_639_1") and (b.get("width") or 0) >= 1280]
    backdrops.sort(key=rank, reverse=True)
    # постеры: сначала русские, потом остальные
    posters = sorted(data.get("posters") or [],
                     key=lambda p: (p.get("iso_639_1") != "ru", -rank(p)))
    return ([f"{IMG}w1280{b['file_path']}" for b in backdrops[:limit] if b.get("file_path")],
            [f"{IMG}original{p['file_path']}" for p in posters[:3] if p.get("file_path")])


def pick_varied(urls: list[str], n: int) -> list[str]:
    """Для фактов: n разных кадров из лучших, в случайном порядке."""
    top = urls[: max(n * 2, n)]
    return random.sample(top, min(n, len(top)))


async def people(key: str, item: dict[str, Any]) -> dict[str, str]:
    """Фото актёров и режиссёров фильма с TMDB: {английское имя в нижнем регистре: url портрета}."""
    if not key:
        return {}
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            found = await _resolve(session, key, item)
            if not found:
                return {}
            kind, tmdb_id = found
            data = await _get(session, key, f"/{kind}/{tmdb_id}/credits")
    except Exception as exc:  # noqa: BLE001
        log.info("shorts: TMDB credits for %s failed: %s", item.get("id"), exc)
        return {}
    out: dict[str, str] = {}
    for person in (data.get("cast") or [])[:20] + (data.get("crew") or []):
        if person.get("profile_path") and person.get("name"):
            out.setdefault(str(person["name"]).lower(), f"{IMG}h632{person['profile_path']}")
    return out

"""Картинки с TMDB: кадры без надписей в высоком качестве и постеры."""

from __future__ import annotations

import logging
import random
import re
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
    # только точное совпадение названия: первый результат поиска бывает другим фильмом
    # («Переводчик» — The Covenant, а первым TMDB отдавал The Blood Covenant)
    names = {_norm(t) for t in (item.get("enName"), item.get("alternativeName"), item.get("name")) if t}
    for r in data.get("results") or []:
        found = {_norm(r.get(k)) for k in ("title", "original_title", "name", "original_name") if r.get(k)}
        if names & found:
            return kind, int(r["id"])
    return None


def _norm(text: Any) -> str:
    return re.sub(r"[^\w]+", "", str(text or "").lower().replace("ё", "е"))


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


async def person(key: str, imdb: str) -> dict[str, Any] | None:
    """Человек по IMDb ID: профиль (на русском) с фильмографией и фото, или None."""
    if not key:
        return None
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        found = await _get(session, key, f"/find/{imdb}", external_source="imdb_id")
        people = found.get("person_results") or []
        if not people:
            return None
        return await _get(session, key, f"/person/{people[0]['id']}", language="ru-RU",
                          append_to_response="movie_credits,images")


def best_movies(info: dict[str, Any], n: int = 5) -> list[dict[str, Any]]:
    """Самые известные фильмы человека: роли (или режиссура), без документалок и камео «в роли себя»."""
    credits = info.get("movie_credits") or {}
    directing = info.get("known_for_department") == "Directing"
    pool = ([c for c in credits.get("crew") or [] if c.get("job") == "Director"] if directing
            else [c for c in credits.get("cast") or []
                  if not re.search(r"\b(self|himself|herself|себя)\b", str(c.get("character") or ""), re.I)])
    films: dict[int, dict[str, Any]] = {}
    for c in pool:
        if 99 in (c.get("genre_ids") or []) or not c.get("release_date") or not c.get("id"):
            continue  # документальные и ещё не вышедшие не берём
        if (c.get("vote_count") or 0) < 50:
            continue
        films.setdefault(int(c["id"]), c)
    top = sorted(films.values(), key=lambda c: c.get("vote_count") or 0, reverse=True)[:n]
    return top


def still_url(film: dict[str, Any]) -> str | None:
    if film.get("backdrop_path"):
        return f"{IMG}w1280{film['backdrop_path']}"
    if film.get("poster_path"):
        return f"{IMG}w780{film['poster_path']}"
    return None


def profile_urls(info: dict[str, Any]) -> list[str]:
    """Портреты: основной и ещё несколько из галереи — для обложки и первого слайда."""
    urls = [f"{IMG}h632{info['profile_path']}"] if info.get("profile_path") else []
    for p in ((info.get("images") or {}).get("profiles") or [])[:4]:
        if p.get("file_path"):
            url = f"{IMG}h632{p['file_path']}"
            if url not in urls:
                urls.append(url)
    return urls

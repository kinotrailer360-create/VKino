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


async def person_by_name(key: str, name: str) -> dict[str, Any] | None:
    """Человек по имени (лучше английскому): профиль на русском с фильмографией и фото, или None."""
    if not key or not name.strip():
        return None
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        found = await _get(session, key, "/search/person", query=name.strip())
        people = [p for p in found.get("results") or [] if p.get("profile_path")]
        if not people:
            return None
        return await _get(session, key, f"/person/{people[0]['id']}", language="ru-RU",
                          append_to_response="movie_credits,tv_credits,images")


async def backdrops(key: str, kind: str, tmdb_id: int, n: int = 2) -> list[str]:
    """Лучшие кадры фильма/сериала без надписей по его id на TMDB."""
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        data = await _get(session, key, f"/{kind}/{tmdb_id}/images", include_image_language="null,ru,en")
    shots = sorted((b for b in data.get("backdrops") or [] if not b.get("iso_639_1") and b.get("file_path")),
                   key=lambda b: float(b.get("vote_average") or 0) * 100 + float(b.get("vote_count") or 0),
                   reverse=True)
    return [f"{IMG}w1280{b['file_path']}" for b in shots[:n]]


async def company_logo(key: str, name: str) -> str | None:
    """Логотип студии/компании (PNG с прозрачным фоном) по названию."""
    if not key or not name.strip():
        return None
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        found = await _get(session, key, "/search/company", query=name.strip())
    want = _norm(name)
    hits = [c for c in found.get("results") or [] if c.get("logo_path")]
    hits.sort(key=lambda c: _norm(c.get("name")) != want)  # точное совпадение названия — первым
    return f"{IMG}w780{hits[0]['logo_path']}" if hits else None


async def poster_by_title(key: str, names: list[str]) -> str | None:
    """Постер фильма/сериала по точному названию (русскому или оригинальному) — когда Кинопоиск не нашёл."""
    if not key:
        return None
    wanted = {_norm(n) for n in names if _norm(n)}
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for name in names:
            data = await _get(session, key, "/search/multi", query=name, language="ru-RU")
            for r in data.get("results") or []:
                found = {_norm(r.get(k)) for k in ("title", "original_title", "name", "original_name") if r.get(k)}
                if r.get("poster_path") and wanted & found:
                    return f"{IMG}w780{r['poster_path']}"
    return None


async def tv_aired(key: str, since: str, until: str, n: int = 12) -> list[dict[str, Any]]:
    """Популярные сериалы, у которых в период вышли серии: название (ru), описание, последняя вышедшая серия.
    [{"name", "original", "year", "overview", "rating", "votes", "season", "episode", "aired", "poster"}]"""
    if not key:
        return []
    timeout = aiohttp.ClientTimeout(total=40)
    out: list[dict[str, Any]] = []
    async with aiohttp.ClientSession(timeout=timeout) as session:
        data = await _get(session, key, "/discover/tv", language="ru-RU", sort_by="popularity.desc",
                          **{"air_date.gte": since, "air_date.lte": until, "vote_count.gte": 30,
                             "without_genres": "10763,10764,10767"})  # без новостей, реалити и ток-шоу
        for r in (data.get("results") or [])[: n + 4]:
            try:
                tv = await _get(session, key, f"/tv/{r['id']}", language="ru-RU")
            except Exception:  # noqa: BLE001
                continue
            last = tv.get("last_episode_to_air") or {}
            aired = str(last.get("air_date") or "")
            if not (since <= aired <= until):
                continue
            out.append({"name": tv.get("name") or r.get("name"), "original": tv.get("original_name") or "",
                        "year": str(tv.get("first_air_date") or "")[:4], "overview": str(tv.get("overview") or ""),
                        "rating": float(tv.get("vote_average") or 0), "votes": int(tv.get("vote_count") or 0),
                        "season": last.get("season_number"), "episode": last.get("episode_number"), "aired": aired,
                        "genres": [g.get("name") for g in tv.get("genres") or [] if g.get("name")],
                        "poster": f"{IMG}w780{tv['poster_path']}" if tv.get("poster_path") else None})
            if len(out) >= n:
                break
    return out


async def movies_digital(key: str, since: str, until: str, n: int = 12) -> list[dict[str, Any]]:
    """Популярные фильмы, вышедшие в цифре (онлайн) за период — по данным TMDB (релизы США)."""
    if not key:
        return []
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        data = await _get(session, key, "/discover/movie", language="ru-RU", sort_by="popularity.desc",
                          region="US", with_release_type="4",
                          **{"release_date.gte": since, "release_date.lte": until, "vote_count.gte": 10})
    return [{"name": r.get("title"), "original": r.get("original_title") or "",
             "year": str(r.get("release_date") or "")[:4], "overview": str(r.get("overview") or ""),
             "rating": float(r.get("vote_average") or 0), "votes": int(r.get("vote_count") or 0),
             "poster": f"{IMG}w780{r['poster_path']}" if r.get("poster_path") else None}
            for r in (data.get("results") or [])[:n] if r.get("title")]


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

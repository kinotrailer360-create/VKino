"""Картинки с TMDB: кадры без надписей в высоком качестве и постеры."""

from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import date, timedelta
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
                          append_to_response="movie_credits,tv_credits,images")


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


def similar(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    a, b = _norm(a), _norm(b)
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


async def poster_by_title(key: str, names: list[str], year: int | None = None) -> str | None:
    """Постер фильма/сериала по названию (русскому или оригинальному) — когда Кинопоиск не нашёл.
    Точное совпадение; иначе очень похожее (≥ 0.85) и того же года (±1) — чужой постер хуже, чем никакого."""
    if not key:
        return None
    wanted = {_norm(n) for n in names if _norm(n)}
    timeout = aiohttp.ClientTimeout(total=20)
    close: str | None = None
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for name in names:
            data = await _get(session, key, "/search/multi", query=name, language="ru-RU")
            for r in data.get("results") or []:
                if not r.get("poster_path"):
                    continue
                titles = [r.get(k) for k in ("title", "original_title", "name", "original_name") if r.get(k)]
                if wanted & {_norm(t) for t in titles}:
                    return f"{IMG}w780{r['poster_path']}"
                r_year = int(str(r.get("release_date") or r.get("first_air_date") or "0")[:4] or 0)
                if (not close and year and r_year and abs(r_year - year) <= 1
                        and max(similar(n, t) for n in names for t in titles) >= 0.85):
                    close = f"{IMG}w780{r['poster_path']}"
    return close


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
    top = sorted(films.values(), key=lambda c: (c.get("vote_count") or 0) * role_weight(c), reverse=True)[:n]
    return top


def is_voice(credit: dict[str, Any]) -> bool:
    return bool(re.search(r"\(voice\)|\bvoice\b|озвуч", str(credit.get("character") or ""), re.I))


def role_weight(credit: dict[str, Any]) -> float:
    """Насколько это роль самого человека, а не эпизод: главные роли (первые в титрах) важнее, озвучка — вдвое
    меньше, камео и «без указания в титрах» почти не считаются. Иначе у Макконахи в «лучших» оказались
    «Зверопой» (только голос) и «Дэдпул и Росомаха» (эпизод), а не «Настоящий детектив» и «Джентльмены»."""
    if credit.get("job"):  # режиссура
        return 1.0
    order = credit.get("order")
    weight = 1.0 if order is None or order <= 2 else 0.6 if order <= 6 else 0.3 if order <= 15 else 0.12
    char = str(credit.get("character") or "")
    if is_voice(credit):
        weight *= 0.5
    if re.search(r"uncredited|cameo|archive|камео", char, re.I):
        weight *= 0.1
    return weight


def known_for(info: dict[str, Any], n: int = 3) -> list[str]:
    """Чем человек известен зрителям: фильмы и сериалы вместе (у Дэвида Швиммера — «Друзья», а не только
    «Мадагаскар»). Сериал весит по числу серий с его участием, озвучка — вдвое меньше роли в кадре."""
    def weight(c: dict[str, Any], tv: bool) -> float:
        char = str(c.get("character") or "")
        if re.search(r"\b(self|himself|herself|host|narrator|себя|ведущ)", char, re.I):
            return 0
        w = float(c.get("vote_count") or 0)
        if re.search(r"\(voice\)|голос", char, re.I):
            w *= 0.5
        if tv:
            if set(c.get("genre_ids") or []) & {99, 10763, 10764, 10767}:  # док, новости, реалити, ток-шоу
                return 0
            eps = int(c.get("episode_count") or 0)
            w *= 1.6 if eps >= 20 else 0.8 if eps >= 6 else 0.1
        return w if (c.get("vote_count") or 0) >= 50 else 0
    seen: dict[str, float] = {}
    for c in best_movies(info, 12):
        seen.setdefault(str(c.get("title") or c.get("original_title") or ""), weight(c, False))
    for c in (info.get("tv_credits") or {}).get("cast") or []:
        title = str(c.get("name") or c.get("original_name") or "")
        if title:
            seen[title] = max(seen.get(title, 0), weight(c, True))
    return [t for t, w in sorted(seen.items(), key=lambda kv: -kv[1]) if t and w > 0][:n]


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


def _movie_row(r: dict[str, Any]) -> dict[str, Any]:
    return {"id": int(r["id"]), "title": str(r.get("title") or r.get("original_title") or ""),
            "original": str(r.get("original_title") or r.get("title") or ""),
            "date": str(r.get("release_date") or ""), "year": str(r.get("release_date") or "")[:4],
            "poster": f"{IMG}w780{r['poster_path']}" if r.get("poster_path") else None,
            "backdrop": f"{IMG}w1280{r['backdrop_path']}" if r.get("backdrop_path") else None,
            "popularity": float(r.get("popularity") or 0)}


async def releases(key: str, since: str, until: str, n: int = 15) -> list[dict[str, Any]]:
    """Самые ожидаемые и популярные фильмы с премьерой в кино США между датами (для Rotten Tomatoes)."""
    if not key:
        return []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        data = await _get(session, key, "/discover/movie", language="ru-RU", region="US",
                          sort_by="popularity.desc", with_release_type="2|3",
                          **{"release_date.gte": since, "release_date.lte": until})
    return [_movie_row(r) for r in (data.get("results") or [])[:n] if r.get("id")]


async def movie(key: str, tmdb_id: int) -> dict[str, Any] | None:
    if not key:
        return None
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        data = await _get(session, key, f"/movie/{tmdb_id}", language="ru-RU")
    return _movie_row(data) if data.get("id") else None


TMDB_PREFIX = "tmdb:"  # id фильма с TMDB в подборке («tmdb:movie:123»), а не с Кинопоиска


def _kp_like(kind: str, d: dict[str, Any]) -> dict[str, Any]:
    """Карточка TMDB в том же виде, что карточка Кинопоиска (поля, которые читает сборка шортса)."""
    date = str(d.get("release_date") or d.get("first_air_date") or "")
    cast = ((d.get("credits") or {}).get("cast") or [])[:8]
    crew = [c for c in (d.get("credits") or {}).get("crew") or [] if c.get("job") == "Director"][:2]
    overview = re.sub(r"\s+", " ", str(d.get("overview") or "")).strip()
    return {
        "id": f"{TMDB_PREFIX}{kind}:{d['id']}",
        "name": str(d.get("title") or d.get("name") or ""),
        "alternativeName": str(d.get("original_title") or d.get("original_name") or ""),
        "enName": str(d.get("original_title") or d.get("original_name") or ""),
        "year": int(date[:4]) if date[:4].isdigit() else None,
        "poster": {"url": f"{IMG}w780{d['poster_path']}"} if d.get("poster_path") else None,
        "backdrop": {"url": f"{IMG}w1280{d['backdrop_path']}"} if d.get("backdrop_path") else None,
        "rating": {"tmdb": round(float(d.get("vote_average") or 0), 1)} if int(d.get("vote_count") or 0) >= 20 else {},
        "genres": [{"name": str(g.get("name") or "")} for g in d.get("genres") or [] if g.get("name")],
        "persons": [{"name": str(c.get("name") or ""), "enProfession": "actor"} for c in cast if c.get("name")]
        + [{"name": str(c.get("name") or ""), "enProfession": "director"} for c in crew if c.get("name")],
        "description": overview, "shortDescription": overview[:160],
        "externalId": {"tmdb": int(d["id"])}, "isSeries": kind == "tv", "type": "tv-series" if kind == "tv" else "movie",
        "premiere": {"world": date} if date else {},
    }


async def kp_like_by_id(key: str, ref: str) -> dict[str, Any] | None:
    """«tmdb:movie:123» → карточка в виде Кинопоиска (с актёрами и рейтингом TMDB)."""
    try:
        kind, tid = ref[len(TMDB_PREFIX):].split(":", 1)
    except ValueError:
        return None
    if kind not in ("movie", "tv") or not tid.isdigit() or not key:
        return None
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        d = await _get(session, key, f"/{kind}/{tid}", language="ru-RU", append_to_response="credits")
    return _kp_like(kind, d) if d.get("id") else None


async def find_kp_like(key: str, names: list[str], year: int | None = None, series: bool | None = None,
                       fresh: bool = False) -> dict[str, Any] | None:
    """Фильма нет на Кинопоиске — ищем на TMDB (точное название, тип и год, новинка — из новых):
    карточка в виде Кинопоиска или None."""
    names = [n for n in names if n and n.strip()]
    if not key or not names:
        return None
    kinds = ["tv"] if series else ["movie"] if series is False else ["movie", "tv"]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        for name in names:
            for kind in kinds:
                data = await _get(session, key, f"/search/{kind}", query=name, language="ru-RU")
                hits = []
                for r in data.get("results") or []:
                    titles = [r.get(k) for k in ("title", "original_title", "name", "original_name") if r.get(k)]
                    if not any(similar(name, str(t)) >= 0.85 for t in titles):
                        continue
                    y = str(r.get("release_date") or r.get("first_air_date") or "")[:4]
                    y = int(y) if y.isdigit() else 0
                    if year and y and abs(y - year) > 1:
                        continue
                    hits.append((y, r))
                if fresh and not year:
                    hits.sort(key=lambda h: -h[0])  # новинка — самая новая из одноимённых
                else:
                    hits.sort(key=lambda h: -float(h[1].get("popularity") or 0))
                if hits:
                    d = await _get(session, key, f"/{kind}/{hits[0][1]['id']}", language="ru-RU",
                                   append_to_response="credits")
                    if d.get("id"):
                        return _kp_like(kind, d)
    return None


async def popular_pool(key: str, page: int = 1) -> list[dict[str, Any]]:
    """Известные фильмы (много голосов, рейтинг 7+) — запас для викторины, если Кинопоиск не ответил.
    Строки как у _movie_row + "genres" (id жанров TMDB)."""
    if not key:
        return []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        data = await _get(session, key, "/discover/movie", language="ru-RU", sort_by="vote_count.desc",
                          page=page, **{"vote_average.gte": 7, "vote_count.gte": 3000})
    return [{**_movie_row(r), "genres": list(r.get("genre_ids") or [])}
            for r in data.get("results") or [] if r.get("id") and r.get("backdrop_path")]


async def search_movie(key: str, query: str, n: int = 5) -> list[dict[str, Any]]:
    if not key or not query.strip():
        return []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        data = await _get(session, key, "/search/movie", query=query.strip(), language="ru-RU")
    return [_movie_row(r) for r in (data.get("results") or [])[:n] if r.get("id")]


TV_SKIP_GENRES = {10763, 10764, 10766, 10767}  # новости, реалити, мыльные оперы, ток-шоу


def _season_row(show: dict[str, Any], season: dict[str, Any]) -> dict[str, Any]:
    n = int(season.get("season_number") or 0)
    poster = season.get("poster_path") or show.get("poster_path")
    return {"id": int(show["id"]), "season": n, "kind": "tv",
            "title": str(show.get("name") or show.get("original_name") or ""),
            "original": str(show.get("original_name") or show.get("name") or ""),
            "date": str(season.get("air_date") or ""), "year": str(season.get("air_date") or "")[:4],
            "first_year": str(show.get("first_air_date") or "")[:4],
            "poster": f"{IMG}w780{poster}" if poster else None,
            "backdrop": f"{IMG}w1280{show['backdrop_path']}" if show.get("backdrop_path") else None,
            "popularity": float(show.get("popularity") or 0)}


def current_season(show: dict[str, Any], today: str) -> dict[str, Any] | None:
    """Сезон, о котором сейчас пишут: новый, если премьера в ближайшие 30 дней, иначе последний вышедший."""
    seasons = sorted((s for s in show.get("seasons") or [] if (s.get("season_number") or 0) > 0 and s.get("air_date")),
                     key=lambda s: s["air_date"])
    if not seasons:
        return None
    soon_limit = (date.fromisoformat(today) + timedelta(days=30)).isoformat()
    soon = [s for s in seasons if today < s["air_date"] <= soon_limit]
    aired = [s for s in seasons if s["air_date"] <= today]
    return soon[0] if soon else (aired[-1] if aired else seasons[0])


async def tv_details(session: aiohttp.ClientSession, key: str, tv_id: int) -> dict[str, Any]:
    return await _get(session, key, f"/tv/{tv_id}", language="ru-RU")


async def tv_seasons(key: str, since: str, until: str, n: int = 10) -> list[dict[str, Any]]:
    """Популярные сериалы, у которых новый сезон стартует (или стартовал) между датами: по строке на сезон."""
    if not key:
        return []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as session:
        data = await _get(session, key, "/discover/tv", language="ru-RU", sort_by="popularity.desc",
                          without_genres="10763,10764,10766,10767", **{"air_date.gte": since, "air_date.lte": until,
                                                                       "vote_count.gte": 30})
        shows = [r for r in (data.get("results") or [])[:max(n + 4, 12)]
                 if r.get("id") and not set(r.get("genre_ids") or []) & TV_SKIP_GENRES]
        details = await asyncio.gather(*(tv_details(session, key, int(r["id"])) for r in shows),
                                       return_exceptions=True)
    out = []
    failed = [d for d in details if isinstance(d, BaseException)]
    if failed:
        log.info("shorts: tmdb tv details: %d of %d failed (%s)", len(failed), len(details), type(failed[0]).__name__)
    for show in details:
        if isinstance(show, BaseException) or not show.get("id"):
            continue
        for season in show.get("seasons") or []:
            if (season.get("season_number") or 0) > 0 and since <= str(season.get("air_date") or "") <= until:
                out.append(_season_row(show, season))
    return out[:n]


async def tv_season(key: str, tv_id: int, season: int = 0, today: str = "") -> dict[str, Any] | None:
    """Сезон сериала; season=0 — текущий (скоро выходит или последний вышедший)."""
    if not key:
        return None
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        show = await tv_details(session, key, tv_id)
    if not show.get("id"):
        return None
    if season:
        found = next((s for s in show.get("seasons") or [] if s.get("season_number") == season), None)
    else:
        found = current_season(show, today or date.today().isoformat())
    return _season_row(show, found) if found else None


async def search_tv(key: str, query: str, n: int = 5) -> list[dict[str, Any]]:
    if not key or not query.strip():
        return []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        data = await _get(session, key, "/search/tv", query=query.strip(), language="ru-RU")
    return [{"id": int(r["id"]), "title": str(r.get("name") or r.get("original_name") or ""),
             "original": str(r.get("original_name") or ""), "year": str(r.get("first_air_date") or "")[:4],
             "kind": "tv", "poster": f"{IMG}w780{r['poster_path']}" if r.get("poster_path") else None}
            for r in (data.get("results") or [])[:n] if r.get("id")
            and not set(r.get("genre_ids") or []) & TV_SKIP_GENRES]


_EN_TITLES: dict[tuple[str, int], str] = {}


async def english_title(key: str, kind: str, tmdb_id: int) -> str:
    """Английское название фильма или сериала (для сайтов вроде Rotten Tomatoes); запоминаем."""
    if not key:
        return ""
    if (kind, tmdb_id) in _EN_TITLES:
        return _EN_TITLES[(kind, tmdb_id)]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        data = await _get(session, key, f"/{'tv' if kind == 'tv' else 'movie'}/{tmdb_id}", language="en-US")
    _EN_TITLES[(kind, tmdb_id)] = str(data.get("name") or data.get("title") or "")
    return _EN_TITLES[(kind, tmdb_id)]


async def box_office(key: str, names: list[str], year: int = 0) -> dict[str, Any] | None:
    """Деньги фильма для графики в выпуске: бюджет и сборы (TMDB, мировые, $) — его и прошлых фильмов франшизы.
    {"title", "year", "budget", "revenue", "parts": [{"title", "year", "budget", "revenue"}]} или None.
    Только точное совпадение названия (и года, если он известен): чужая франшиза на графике — хуже, чем никакой."""
    if not key:
        return None
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        best: dict[str, Any] | None = None
        for name in [n for n in names if n and len(n) >= 2]:
            data = await _get(session, key, "/search/movie", query=name, language="ru-RU")
            for r in (data.get("results") or [])[:8]:
                titles = [r.get("title") or "", r.get("original_title") or ""]
                r_year = int(str(r.get("release_date") or "0")[:4] or 0)
                if max(similar(name, t) for t in titles) < 0.93:
                    continue
                if year and r_year and abs(r_year - year) > 1:
                    continue  # одноимённый старый фильм («Обитель зла» 2002 вместо 2026)
                if best is None or (r.get("popularity") or 0) > (best.get("popularity") or 0):
                    best = r
            if best:
                break
        if not best:
            return None
        info = await _get(session, key, f"/movie/{best['id']}", language="ru-RU")
        parts: list[dict[str, Any]] = []
        coll = info.get("belongs_to_collection") or {}
        if coll.get("id"):
            try:
                cdata = await _get(session, key, f"/collection/{coll['id']}", language="ru-RU")
                ids = [int(p["id"]) for p in cdata.get("parts") or [] if p.get("id") and p.get("release_date")]
                details = await asyncio.gather(*(_get(session, key, f"/movie/{i}", language="ru-RU") for i in ids),
                                               return_exceptions=True)
                for d in details:
                    if isinstance(d, dict) and d.get("id"):
                        parts.append({"id": int(d["id"]), "title": str(d.get("title") or d.get("original_title")),
                                      "year": str(d.get("release_date") or "")[:4],
                                      "budget": int(d.get("budget") or 0), "revenue": int(d.get("revenue") or 0)})
            except Exception as exc:  # noqa: BLE001
                log.info("shorts: tmdb collection %s failed: %s", coll.get("id"), exc)
        parts.sort(key=lambda p: p["year"])
        return {"id": int(info["id"]), "title": str(info.get("title") or info.get("original_title") or ""),
                "year": str(info.get("release_date") or "")[:4], "budget": int(info.get("budget") or 0),
                "revenue": int(info.get("revenue") or 0), "parts": parts}


def _credit_ids(info: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Фильмы человека: роли и режиссура/сценарий/продюсирование (у режиссёра роли в кадре почти нет)."""
    credits = info.get("movie_credits") or {}
    rows = list(credits.get("cast") or []) + [c for c in credits.get("crew") or []
                                               if c.get("job") in ("Director", "Screenplay", "Writer", "Producer")]
    return {int(c["id"]): c for c in rows if c.get("id") and c.get("release_date") and 99 not in (c.get("genre_ids") or [])}


async def joint_movies(key: str, a: str, b: str) -> tuple[list[str], list[dict[str, Any]]]:
    """Вышедшие фильмы, где работали оба (Гай Ричи и Джейсон Стейтем), по годам: ([имена по-русски],
    [{title, year, poster}])."""
    first, second = await asyncio.gather(person_by_name(key, a), person_by_name(key, b))
    if not first or not second:
        return [], []
    one, two = _credit_ids(first), _credit_ids(second)
    today = date.today().isoformat()
    films = [one[i] for i in one.keys() & two.keys() if str(one[i].get("release_date")) <= today]
    films.sort(key=lambda c: str(c.get("release_date")))
    return [str(first.get("name") or a), str(second.get("name") or b)], [
        {"title": str(c.get("title") or c.get("original_title") or ""), "year": str(c["release_date"])[:4],
         "poster": f"{IMG}w342{c['poster_path']}" if c.get("poster_path") else None} for c in films]


async def top_movies(key: str, name: str, n: int = 6) -> tuple[str, list[dict[str, Any]]]:
    """Главные фильмы человека по годам (для дня рождения, памяти): (имя по-русски, [{title, year, poster}])."""
    info = await person_by_name(key, name)
    if not info:
        return "", []
    films = sorted(best_movies(info, n), key=lambda c: str(c.get("release_date")))
    return str(info.get("name") or name), [
        {"title": str(c.get("title") or c.get("original_title") or ""), "year": str(c.get("release_date"))[:4],
         "poster": f"{IMG}w342{c['poster_path']}" if c.get("poster_path") else None} for c in films]


async def youtube_trailer(key: str, kind: str, tmdb_id: int) -> str | None:
    """Официальный трейлер фильма/сериала на YouTube по его id на TMDB: свежий, сначала русский, потом английский.
    (Rutube бывает со старым трейлером — у TMDB трейлер привязан к карточке именно этого фильма.)"""
    if not key:
        return None
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for lang in ("ru-RU", "en-US"):
            data = await _get(session, key, f"/{'tv' if kind == 'tv' else 'movie'}/{tmdb_id}/videos", language=lang)
            vids = [v for v in data.get("results") or [] if v.get("site") == "YouTube" and v.get("key")
                    and v.get("type") in ("Trailer", "Teaser")]
            vids.sort(key=lambda v: str(v.get("published_at") or ""), reverse=True)
            vids.sort(key=lambda v: v.get("type") != "Trailer")
            if vids:
                return f"https://www.youtube.com/watch?v={vids[0]['key']}"
    return None

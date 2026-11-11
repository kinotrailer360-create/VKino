"""Ежедневный шортс: тема -> фильмы с Кинопоиска -> карточки, голос, видео, SEO."""

from __future__ import annotations

import asyncio
import hashlib
import html
import io
import json
import logging
import os
import random
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp
from PIL import Image

from . import ai, clips, lumean, news, pronounce, project, scene_planner, stressdb, tgsearch, tmdb, wiki
from . import music as musiclib
from .cards import card_overlay, make_card, make_cover

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
FILMS = 5
VOICE = "ru-RU-DmitryNeural"
VOICE_RATE = "+22%"
# запасной голос — Piper, работает прямо на сервере без интернета (модель кладёт Dockerfile)
PIPER_MODEL = Path(os.getenv("PIPER_MODEL", "/opt/piper/voice.onnx"))
PIPER_SPEED = 0.8  # length_scale: меньше — быстрее, ~как +22% у основного голоса
GAP = 0.2  # пауза между фразами, сек
MIN_RATING = 6.0  # ниже — рейтинг не показываем, он только отпугнёт
SKIP_RATING = 5.5  # ниже — фильм в подборку не берём

YOUTUBE_URL = "https://youtube.com/@kinomir360"
# в описании YouTube ссылки ставить нельзя (у канала нет такого права) — адрес Telegram без https://
TELEGRAM_URL = "t.me/KinoMir360"

# По дням недели (пн..вс)
SCHEDULE = ["online", "cinema", "facts_new", "genre", "news", "upcoming", "facts"]

GENRES = {
    "фантастика": ("ТОП ФАНТАСТИКИ", "фантастических фильмов, которые взрывают мозг", "фантастика"),
    "ужасы": ("ТОП УЖАСОВ", "фильмов ужасов, после которых не уснёшь", "ужасы"),
    "триллер": ("ТОП ТРИЛЛЕРОВ", "триллеров, которые держат до последней минуты", "триллеры"),
    "комедия": ("ТОП КОМЕДИЙ", "комедий для отличного вечера", "комедии"),
    "боевик": ("ТОП БОЕВИКОВ", "боевиков, от которых захватывает дух", "боевики"),
    "детектив": ("ТОП ДЕТЕКТИВОВ", "детективов с неожиданной развязкой", "детективы"),
}

Fetch = Callable[..., Awaitable[dict[str, Any]]]
log = logging.getLogger("shorts")

ProgressFn = Callable[[int, str], Awaitable[None]]
on_progress: ProgressFn | None = None  # бот ставит перед сборкой — двигает полоску «▓▓░░ 35%»


last_stage = ""  # последний этап сборки — если она зависнет, бот скажет, на чём


async def _progress(pct: int, stage: str) -> None:
    global last_stage
    last_stage = f"{pct}% — {stage}"
    log.info("shorts: %s", last_stage)
    if on_progress is None:
        return
    try:
        await on_progress(max(0, min(pct, 100)), stage)
    except Exception as exc:  # noqa: BLE001 — полоска не должна ломать сборку
        log.debug("shorts: progress update failed: %s", exc)


@dataclass
class Theme:
    key: str
    tag: str
    header: str
    hook: str  # «5 ...» — начало фразы для обложки и голоса
    seo_title: str
    tags: list[str]
    upcoming: bool = False


@dataclass
class ShortResult:
    video: Path
    cover: Path
    theme: Theme
    films: list[dict[str, Any]]
    seo: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------- данные

def _range(start: date, end: date) -> str:
    return f"{start:%d.%m.%Y}-{end:%d.%m.%Y}"


def _title(item: dict[str, Any]) -> str:
    return str(item.get("name") or item.get("alternativeName") or item.get("enName") or "").strip()


def _image_url(item: dict[str, Any]) -> str | None:
    for key in ("backdrop", "poster"):
        val = item.get(key)
        if isinstance(val, dict) and val.get("url"):
            return str(val["url"])
    return None


def _poster_url(item: dict[str, Any]) -> str | None:
    val = item.get("poster")
    if isinstance(val, dict) and val.get("url"):
        return str(val["url"])
    return _image_url(item)


RATING_SOURCE = "tmdb"  # какой рейтинг показывать в карточках и постах: tmdb — TMDB, если он известен
TMDB_MIN_VOTES = 30  # у TMDB голосов меньше, чем у Кинопоиска; меньше 30 — рейтинг случайный


def _best_rating(item: dict[str, Any]) -> tuple[float, str]:
    """Рейтинг для карточки: TMDB (если он есть и за ним хватает голосов), иначе больший из IMDb и Кинопоиска
    (0 — рейтинга нет)."""
    r = item.get("rating") or {}
    if RATING_SOURCE == "tmdb":
        try:
            v = float(r.get("tmdb") or 0)
            votes = int((item.get("votes") or {}).get("tmdb") or TMDB_MIN_VOTES)
        except (TypeError, ValueError):
            v, votes = 0.0, 0
        if v > 0 and votes >= TMDB_MIN_VOTES:
            return v, "TMDB"
    best, label = 0.0, ""
    for key, name in (("imdb", "IMDb"), ("kp", "КП")):
        try:
            v = float(r.get(key) or 0)
        except (TypeError, ValueError):
            v = 0.0
        if v > best:
            best, label = v, name
    return best, label


def _rating(item: dict[str, Any]) -> tuple[float | None, str]:
    """Рейтинг для карточки и описания: только если он достаточно высокий."""
    v, label = _best_rating(item)
    return (v, label) if v >= MIN_RATING else (None, "")


def _actors(item: dict[str, Any], n: int = 3) -> list[str]:
    out = []
    for p in item.get("persons") or []:
        if not isinstance(p, dict):
            continue
        if str(p.get("enProfession") or p.get("profession") or "").lower() not in ("actor", "актеры", "актёры"):
            continue
        name = str(p.get("name") or p.get("enName") or "").strip()
        if name and name not in out:
            out.append(name)
        if len(out) >= n:
            break
    return out


def _genres(item: dict[str, Any]) -> list[str]:
    return [str(g.get("name")) for g in item.get("genres") or [] if isinstance(g, dict) and g.get("name")]


MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]


def _premiere(item: dict[str, Any]) -> date | None:
    prem = item.get("premiere") or {}
    for key in ("russia", "digital", "world"):
        raw = str(prem.get(key) or "")[:10]
        try:
            return date.fromisoformat(raw)
        except ValueError:
            continue
    return None


def _date_ru(d: date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]}"


def _pdate(item: dict[str, Any], key: str) -> date | None:
    raw = str((item.get("premiere") or {}).get(key) or "")[:10]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _date_full(d: date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def _money(obj: Any) -> str | None:
    """{"value": 150000000, "currency": "$"} -> "$150 млн"."""
    if not isinstance(obj, dict):
        return None
    try:
        v = float(obj.get("value") or 0)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    if v >= 1e9:
        num = f"{v / 1e9:.1f}".rstrip("0").rstrip(".").replace(".", ",") + " млрд"
    elif v >= 1e6:
        num = f"{v / 1e6:.0f} млн"
    else:
        num = f"{v / 1e3:.0f} тыс"
    cur = str(obj.get("currency") or "$").strip()
    if cur in ("$", "€"):
        return f"{cur}{num}"
    return f"{num} руб." if cur in ("₽", "руб", "руб.", "RUB") else f"{num} {cur}"


def info_rows(item: dict[str, Any]) -> list[list[tuple[str, str]]]:
    """Строки под карточкой: даты выхода, бюджет и сборы — что есть в данных."""
    dates: list[tuple[str, str]] = []
    if item.get("isSeries"):
        if d := _pdate(item, "world"):
            dates.append(("Премьера", _date_full(d)))
    else:
        if d := _pdate(item, "russia"):
            dates.append(("В кино", _date_full(d)))
        if d := _pdate(item, "digital"):
            dates.append(("Онлайн", _date_full(d)))
        if not dates and (d := _pdate(item, "world")):
            dates.append(("Премьера", _date_full(d)))
    money: list[tuple[str, str]] = []
    if b := _money(item.get("budget")):
        money.append(("Бюджет", b))
    if f := _money((item.get("fees") or {}).get("world")):
        money.append(("Сборы", f))
    return [row for row in (dates, money) if row]


async def _query(fetch: Fetch, **params: Any) -> list[dict[str, Any]]:
    params.setdefault("page", 1)
    params.setdefault("limit", 40)
    try:
        data = await fetch("/movie", **params)
    except Exception as exc:  # noqa: BLE001 — одна неудачная выборка не должна ронять сборку
        log.warning("shorts: query %s failed: %s", params, exc)
        return []
    return [x for x in data.get("docs") or [] if isinstance(x, dict)]


def theme_for(key: str | None, today: date) -> Theme:
    key = key or SCHEDULE[today.weekday()]
    n = FILMS
    if key == "online":
        return Theme("online", "НОВИНКИ", "ОНЛАЙН-ПРЕМЬЕРЫ",
                     f"{n} свежих онлайн-премьер, которые стоит посмотреть",
                     f"{n} свежих онлайн-премьер 🔥 Что посмотреть",
                     ["онлайн премьеры", "новинки онлайн", "что посмотреть", "новые фильмы", "новые сериалы"])
    if key == "cinema":
        return Theme("cinema", "В КИНО", "СЕЙЧАС В КИНО",
                     f"{n} фильмов, которые идут в кино прямо сейчас",
                     f"Что посмотреть в кино: {n} фильмов в прокате 🍿",
                     ["кинопрокат", "сейчас в кино", "новинки кино", "что посмотреть в кино", "афиша кино"])
    if key == "series":
        return Theme("series", "СЕРИАЛЫ", "НОВЫЕ СЕРИАЛЫ",
                     f"{n} новых сериалов, от которых сложно оторваться",
                     f"{n} новых сериалов, от которых сложно оторваться 📺",
                     ["новые сериалы", "сериалы новинки", "что посмотреть", "лучшие сериалы", "сериал на вечер"])
    if key == "upcoming":
        return Theme("upcoming", "СКОРО", "СКОРО В КИНО",
                     f"{n} самых ожидаемых премьер ближайших недель",
                     f"{n} самых ожидаемых премьер ⏳ Скоро в кино",
                     ["скоро в кино", "ожидаемые фильмы", "премьеры", "новинки кино", "афиша"],
                     upcoming=True)
    if key == "week":  # для постов: премьеры этой недели в кино
        return Theme("week", "ПРЕМЬЕРЫ", "ПРЕМЬЕРЫ НЕДЕЛИ", "Премьеры недели в кино", "Премьеры недели в кино 🍿",
                     ["премьеры недели", "новинки кино", "что посмотреть в кино", "афиша кино", "кинопрокат"])
    if key == "fresh":  # для постов: только что вышло онлайн
        return Theme("fresh", "ТОЛЬКО ВЫШЛО", "ТОЛЬКО ВЫШЛО ОНЛАЙН",
                     "Только что вышло онлайн: что посмотреть", "Только что вышло онлайн 🔥",
                     ["новинки онлайн", "только вышло", "что посмотреть", "новые фильмы", "новые сериалы"])
    if key == "films":  # для постов: новые фильмы (вышли онлайн или в кино за полтора месяца)
        return Theme("films", "НОВИНКИ", "НОВЫЕ ФИЛЬМЫ", f"{n} новых фильмов, которые стоит посмотреть",
                     f"{n} новых фильмов 🎬 Что посмотреть",
                     ["новые фильмы", "новинки кино", "что посмотреть", "фильмы 2026", "фильм на вечер"])
    if key in ("weekfilms", "weekseries"):  # для постов: что вышло онлайн за эту неделю
        what = "сериалы" if key == "weekseries" else "фильмы"
        return Theme(key, "ЗА НЕДЕЛЮ", f"ВЫШЛО ОНЛАЙН ЗА НЕДЕЛЮ: {what.upper()}",
                     f"Вышло онлайн за неделю: {what}", f"Что посмотреть: {what}, вышедшие онлайн за неделю 🆕",
                     [f"новые {what}", "новинки онлайн", "что посмотреть", "вышло на этой неделе", "онлайн премьеры"])
    if key in ("topfilms", "topseries"):  # для постов: лучшее за последние годы
        what = "сериалов" if key == "topseries" else "фильмов"
        return Theme(key, "ТОП", f"ТОП {what.upper()}", f"{n} лучших {what} последних лет",
                     f"{n} лучших {what} последних лет 🏆",
                     [f"лучшие {what}", "что посмотреть", "топ", f"{what} на вечер"])
    genre = key.split(":", 1)[1] if key.startswith("genre:") else random.choice(list(GENRES))
    header, hook, plural = GENRES.get(genre, GENRES["фантастика"])
    return Theme(f"genre:{genre}", "ПОДБОРКА", header, f"{n} {hook}",
                 f"{n} {hook} 🎬",
                 [f"лучшие {plural}", plural, "что посмотреть", "подборка фильмов", "топ фильмов"])


def _is_russian(doc: dict[str, Any]) -> bool:
    return any(str(c.get("name") or "") in ("Россия", "СССР") for c in doc.get("countries") or [])


async def _foreign_online(fetch: Fetch, since: date, until: date) -> list[dict[str, Any]]:
    """Зарубежные фильмы и сериалы, вышедшие онлайн за период (по TMDB: цифровые релизы США и свежие серии), —
    карточками Кинопоиска (русское название, рейтинг, актёры). На Кинопоиске «онлайн-премьеры» — это в основном
    российские онлайн-кинотеатры, и подборка выходила целиком из российских фильмов."""
    key = os.getenv("TMDB_API_KEY", "").strip()
    if not key:
        return []
    try:
        films, shows = await asyncio.gather(tmdb.movies_digital(key, since.isoformat(), until.isoformat(), 10),
                                            tmdb.tv_aired(key, since.isoformat(), until.isoformat(), 6),
                                            return_exceptions=True)
    except Exception as exc:  # noqa: BLE001
        log.info("shorts: foreign online failed: %s", exc)
        return []
    found: list[dict[str, Any]] = []
    for item in [*(films if isinstance(films, list) else []), *(shows if isinstance(shows, list) else [])]:
        name = item.get("original") or item.get("name") or ""
        year = int(item.get("year") or 0)
        if not name:
            continue
        try:
            data = await fetch("/movie/search", query=name, page=1, limit=5)
        except Exception as exc:  # noqa: BLE001
            log.info("shorts: KP search %s failed: %s", name, exc)
            continue
        for d in data.get("docs") or []:
            titles = [t for t in (str(d.get(k) or "") for k in ("alternativeName", "enName", "name")) if t]
            if not d.get("id") or not titles or _is_russian(d):
                continue
            if year and abs(int(d.get("year") or 0) - year) > 1:
                continue
            if max(tmdb.similar(name, t) for t in titles) >= 0.85:
                found.append(d)
                break
    return found


def _mix(foreign: list[dict[str, Any]], local: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Зарубежные и российские через одну — подборка не из одних российских и не из одних голливудских."""
    out: list[dict[str, Any]] = []
    for k in range(max(len(foreign), len(local))):
        out += [x[k] for x in (foreign, local) if k < len(x)]
    return out


async def pick_films(fetch: Fetch, theme: Theme, today: date, exclude: set[int]) -> list[dict[str, Any]]:
    common = {"sortField": "votes.kp", "sortType": "-1", "notNullFields": "name"}
    if theme.key == "online":
        docs = await _query(fetch, isSeries="false",
                            **{"premiere.digital": _range(today - timedelta(days=30), today)}, **common)
        docs += await _query(fetch, isSeries="true",
                             **{"premiere.world": _range(today - timedelta(days=30), today)}, **common)
        docs.sort(key=lambda x: -int((x.get("votes") or {}).get("kp") or 0))
        docs = _mix(await _foreign_online(fetch, today - timedelta(days=30), today), docs)
    elif theme.key == "fresh":  # вышло в цифре (фильмы) или стартовало (сериалы) за последние 10 дней
        docs = await _query(fetch, isSeries="false",
                            **{"premiere.digital": _range(today - timedelta(days=10), today)}, **common)
        docs += await _query(fetch, isSeries="true",
                             **{"premiere.world": _range(today - timedelta(days=10), today)}, **common)
        docs.sort(key=lambda x: -int((x.get("votes") or {}).get("kp") or 0))
        docs = _mix(await _foreign_online(fetch, today - timedelta(days=14), today), docs)
    elif theme.key == "cinema":  # год — отсекаем повторный прокат старых фильмов
        docs = await _query(fetch, isSeries="false", year=f"{today.year - 1}-{today.year + 1}",
                            **{"premiere.russia": _range(today - timedelta(days=35), today)}, **common)
    elif theme.key == "week":  # премьеры этой недели: вышли за последние дни или выходят в ближайшие
        docs = await _query(fetch, isSeries="false", year=f"{today.year - 1}-{today.year + 1}",
                            **{"premiere.russia": _range(today - timedelta(days=4), today + timedelta(days=4))},
                            **common)
    elif theme.key == "films":
        years = f"{today.year - 1}-{today.year}"
        docs = await _query(fetch, isSeries="false", year=years,
                            **{"premiere.digital": _range(today - timedelta(days=45), today)}, **common)
        docs += await _query(fetch, isSeries="false", year=years,
                             **{"premiere.russia": _range(today - timedelta(days=45), today)}, **common)
        docs.sort(key=lambda x: -int((x.get("votes") or {}).get("kp") or 0))
    elif theme.key in ("weekfilms", "weekseries"):  # вышло онлайн за последние 10 дней
        series = theme.key == "weekseries"
        week = _range(today - timedelta(days=10), today)
        docs = []
        for field in (("digital", "world", "russia") if series else ("digital",)):
            docs += await _query(fetch, isSeries=str(series).lower(), year=f"{today.year - 1}-{today.year}",
                                 **{f"premiere.{field}": week}, **common)
        docs.sort(key=lambda x: -int((x.get("votes") or {}).get("kp") or 0))
    elif theme.key == "series":
        docs = await _query(fetch, isSeries="true",
                            **{"premiere.world": _range(today - timedelta(days=60), today)}, **common)
    elif theme.key == "upcoming":
        docs = await _query(fetch, isSeries="false", sortField="votes.await", sortType="-1",
                            notNullFields="name",
                            **{"premiere.russia": _range(today + timedelta(days=1), today + timedelta(days=60))})
    elif theme.key in ("topfilms", "topseries"):
        docs = await _query(fetch, page=random.randint(1, 3), isSeries=str(theme.key == "topseries").lower(),
                            year=f"{today.year - 3}-{today.year}",
                            **{"rating.kp": "7.3-10", "votes.kp": "20000-100000000"}, **common)
        random.shuffle(docs)
    else:
        genre = theme.key.split(":", 1)[1]
        docs = await _query(fetch, page=random.randint(1, 3), isSeries="false",
                            **{"genres.name": genre, "rating.kp": "7.2-10", "votes.kp": "30000-100000000"},
                            **common)
        random.shuffle(docs)

    picked, seen = [], set()
    for d in docs:
        mid = d.get("id")
        if not mid or mid in exclude or mid in seen or not _title(d) or not _image_url(d):
            continue
        if 0 < _best_rating(d)[0] < SKIP_RATING:
            continue
        seen.add(mid)
        picked.append(d)
        if len(picked) >= FILMS:
            break
    # подробности (актёры, жанры, рейтинг) — в списках они бывают неполными
    full = []
    for d in picked:
        try:
            full.append(await fetch(f"/movie/{d['id']}"))
        except Exception:  # noqa: BLE001
            full.append(d)
    return full


async def _download(session: aiohttp.ClientSession, url: str) -> Image.Image:
    async with session.get(url) as resp:
        resp.raise_for_status()
        return Image.open(io.BytesIO(await resp.read())).convert("RGB")


async def _download_first(session: aiohttp.ClientSession, urls: list[str | None]) -> Image.Image:
    """Первая картинка, которая скачалась (TMDB, потом Кинопоиск)."""
    last: Exception | None = None
    for url in urls:
        if not url:
            continue
        try:
            return await _download(session, url)
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise RuntimeError(f"Не скачалась ни одна картинка: {last}")


# ---------------------------------------------------------------- факты о фильме

FACTS = 5
FACT_MIN, FACT_MAX = 40, 170  # длиннее — не влезает на экран и затягивает ролик


def _clean_fact(raw: Any) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", "", str(raw or "")))
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def usable_facts(item: dict[str, Any]) -> list[str]:
    """Факты без спойлеров и подходящей длины (сначала FACT, потом ляпы)."""
    facts, bloopers = [], []
    for f in item.get("facts") or []:
        if not isinstance(f, dict) or f.get("spoiler"):
            continue
        text = _clean_fact(f.get("value"))
        if not FACT_MIN <= len(text) <= FACT_MAX:
            continue
        (bloopers if str(f.get("type") or "").upper() == "BLOOPER" else facts).append(text)
    return facts + bloopers


def _kind(item: dict[str, Any]) -> str:
    kind = str(item.get("type") or "")
    if item.get("isSeries") or "series" in kind:
        return "сериале"
    if kind in ("cartoon", "anime"):
        return "мультфильме"
    return "фильме"


def _facts_word(n: int) -> str:
    return "факта" if 2 <= n <= 4 else "фактов"


NEW_FROM = 2015  # «Факты: новое» — фильмы и сериалы с этого года, «Факты: классика» — до него


async def pick_fact_film(fetch: Fetch, query: str, exclude: set[int],
                         web: bool = False, era: str = "") -> tuple[dict[str, Any], list[str]]:
    """Фильм и его факты с Кинопоиска (все подходящие). web=True — факты найдёт ИИ в интернете,
    поэтому названный автором фильм берём, даже если на Кинопоиске фактов мало.
    era — для случайного: "new" — с NEW_FROM года, "old" — до него, "" — любой (пополам)."""
    if query.startswith("id:") and query[3:].isdigit():
        # выбран из подсказок поиска — точный фильм по id Кинопоиска
        cands = [{"id": int(query[3:])}]
        need = 0 if web else 3
    elif query:
        # «Мастер и Маргарита 2024» — год в конце уточняет, какой из одноимённых фильмов
        year_m = re.search(r"\s(19\d\d|20\d\d)$", query)
        year = int(year_m.group(1)) if year_m else None
        name = query[: year_m.start()].strip() if year_m else query
        try:
            data = await fetch("/movie/search", query=name, page=1, limit=10)
            cands = [x for x in data.get("docs") or [] if isinstance(x, dict)]
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Поиск «{query}» не удался: {exc}") from exc
        if year:
            cands = [c for c in cands if c.get("year") == year] or cands
        # одноимённые (фильм 2024 и сериал 2005) — берём самый известный, по числу оценок
        cands.sort(key=lambda c: -int((c.get("votes") or {}).get("kp") or 0))
        need = 0 if web else 3
    else:
        era = era or random.choice(("new", "old"))
        if era == "new":  # у новых оценок меньше, а фактов на Кинопоиске — тоже, их добирает ИИ
            years, votes, need = f"{NEW_FROM}-{date.today().year}", "40000-100000000", 3 if web else FACTS
        else:
            years, votes, need = f"1950-{NEW_FROM - 1}", "150000-100000000", FACTS
        cands = await _query(fetch, page=random.randint(1, 5), sortField="votes.kp", sortType="-1",
                             notNullFields="name", year=years,
                             **{"rating.kp": "7-10", "votes.kp": votes})
        random.shuffle(cands)
    for d in cands[:12]:
        if not d.get("id") or (not query and d["id"] in exclude):
            continue
        try:
            full = await fetch(f"/movie/{d['id']}")
        except Exception:  # noqa: BLE001
            continue
        facts = usable_facts(full)
        if len(facts) >= need and _image_url(full):
            return full, facts
    raise RuntimeError(f"Не нашёл фильм с {need}+ подходящими фактами" + (f" по запросу «{query}»" if query else ""))


fact_channels: list[str] = list(tgsearch.DEFAULT_CHANNELS)  # бот подставляет список из настроек


async def web_facts(film: dict[str, Any], kp_facts: list[str], gemini_key: str,
                    n: int = FACTS, rumors: bool = False,
                    media_out: list[dict[str, Any]] | None = None,
                    progress: ProgressFn | None = None) -> tuple[list[str], list[str]]:
    """Факты из интернета (поиск Google через Gemini, Википедия, Telegram-каналы) и с Кинопоиска.
    rumors=True — фильм ещё не вышел: новости и слухи о нём.
    media_out — сюда добавляются посты каналов с фото или видео (материал для ролика).
    progress(процент, этап) — для полоски в боте."""
    en = str(film.get("alternativeName") or film.get("enName") or "")
    if progress:
        await progress(15, "Читаю Википедию и Telegram-каналы")
    article, posts = await asyncio.gather(
        wiki.film_article(_title(film), en, film.get("year")),
        tgsearch.search(fact_channels, [_title(film), en], per_channel=6 if rumors else tgsearch.POSTS_PER_CHANNEL))
    if progress:
        await progress(40, f"Нашёл постов в каналах: {len(posts)}. ИИ ищет в Google и выбирает лучшее")
    facts, sources = await ai.web_facts(gemini_key, _title(film), en, film.get("year"), _kind(film), kp_facts,
                                        article, n, tgsearch.as_text(posts, 14000 if rumors else 9000), rumors)
    if media_out is not None:
        media_out.extend(p for p in posts if p.get("image") or p.get("video"))
    if progress:
        await progress(85, "Корректор проверяет падежи и имена")
    facts = await ai.proofread(gemini_key, facts)
    channels = list(dict.fromkeys(f"t.me/{p['channel']}" for p in posts))
    return facts, [s for s in sources if s != "Telegram-каналы"] + channels


async def gather_facts(film: dict[str, Any], kp_facts: list[str], gemini_key: str,
                       media_out: list[dict[str, Any]] | None = None, rumors: bool = False) -> tuple[list[str], str]:
    """(факты для ролика, заметка автору). Без ИИ или при сбое — факты Кинопоиска, как раньше.
    media_out — посты Telegram-каналов с фото и видео о фильме."""
    if ai.available(gemini_key):
        await _progress(7, "ИИ ищет факты в интернете")
        try:
            facts, sources = await web_facts(film, kp_facts, gemini_key, media_out=media_out, rumors=rumors)
            for f in kp_facts:  # ИИ нашёл меньше пяти — добираем Кинопоиском
                if len(facts) >= FACTS:
                    break
                if f not in facts:
                    facts.append(f)
            src = ", ".join(sources[:6]) or "интернет"
            return facts, f"🔎 Факты нашёл ИИ в интернете ({src}) — проверь перед публикацией."
        except Exception as exc:  # noqa: BLE001
            log.warning("shorts: web facts for %s failed: %s", _title(film), exc)
            note = f"⚠️ Факты в интернете не нашлись ({str(exc)[:200]}) — взял с Кинопоиска."
    else:
        note = ""
    if len(kp_facts) < 3:
        raise RuntimeError(f"У «{_title(film)}» мало фактов на Кинопоиске, а ИИ их в интернете не нашёл")
    return random.sample(kp_facts[:12], min(FACTS, len(kp_facts[:12]))), note


async def _stills(fetch: Fetch, item: dict[str, Any]) -> list[str]:
    """Кадры из фильма; если API их не отдаёт — фон и постер."""
    urls: list[str] = []
    try:
        data = await fetch("/image", movieId=item["id"], type="still", limit=20)
        for img in data.get("docs") or []:
            w, h = img.get("width") or 0, img.get("height") or 0
            if img.get("url") and (not w or w >= 700) and (not h or w >= h):
                urls.append(str(img["url"]))
    except Exception as exc:  # noqa: BLE001
        log.info("shorts: no stills for %s: %s", item.get("id"), exc)
    random.shuffle(urls)
    for fallback in (_image_url(item), _poster_url(item)):
        if fallback and fallback not in urls:
            urls.append(fallback)
    return urls


# ---------------------------------------------------------------- фрагменты трейлеров

CLIP_SECONDS = 5.0


TRAILER_BUDGET = 75  # сек на трейлер одного фильма: не уложились — карточка с кадром


async def _trailer_clip(item: dict[str, Any], out: Path) -> bool:
    """Скачать 5 секунд трейлера с Rutube в out; False — не нашлось или не скачалось."""
    titles = [str(item.get(k) or "") for k in ("name", "alternativeName", "enName")]

    async def fetch() -> bool:
        found = await clips.rutube_trailer([t for t in titles if t], item.get("year"))
        if not found:
            log.info("shorts: no Rutube trailer for %s", _title(item))
            return False
        await clips.download_clip(found["url"], out, clips.clip_start(found["duration"], CLIP_SECONDS),
                                  CLIP_SECONDS, timeout=60)
        return out.exists()

    try:
        if await asyncio.wait_for(fetch(), timeout=TRAILER_BUDGET):
            return True
    except Exception as exc:  # noqa: BLE001 — без трейлера останется кадр
        log.warning("shorts: trailer clip for %s failed: %s", _title(item), exc)
    # трейлера на Rutube нет (частое у новинок) — видео из постов Telegram-каналов о фильме
    try:
        return await asyncio.wait_for(_tg_clip(item, out), timeout=TG_CLIP_BUDGET)
    except Exception as exc:  # noqa: BLE001
        log.warning("shorts: tg clip for %s failed: %s", _title(item), exc)
        return False


TG_CLIP_BUDGET = 60


async def _tg_clip(item: dict[str, Any], out: Path) -> bool:
    """5 секунд из видео поста Telegram-канала, где упомянут фильм (свежие посты — первыми)."""
    names = [str(item.get(k) or "") for k in ("name", "alternativeName", "enName")]
    names = [n for n in dict.fromkeys(names) if len(n) >= 3]
    if not names or not fact_channels:
        return False
    posts = [p for p in await tgsearch.search(fact_channels, names, per_channel=2) if p.get("video")]
    for post in posts[:3]:
        files = await tgsearch.download_media([{"video": post["video"]}], out.parent, 1, prefix=f"{out.stem}_tg")
        if not files:
            continue
        try:
            dur = await _duration(files[0])
            await clips.cut_clip(files[0], out, max(0.0, dur / 2 - CLIP_SECONDS / 2), min(CLIP_SECONDS, dur))
        finally:
            files[0].unlink(missing_ok=True)
        if out.exists():
            log.info("shorts: clip for %s from %s", _title(item), post["url"])
            return True
    return False


async def _fact_clips(item: dict[str, Any], facts_by_idx: list[tuple[int, str]], work: Path,
                     gemini_key: str) -> tuple[dict[int, str], str]:
    """Подбирает только подтверждённые ИИ фрагменты трейлера и не допускает почти одинаковые отрезки."""
    titles = [str(item.get(k) or "") for k in ("name", "alternativeName", "enName")]
    trailer_dir = work / "trailer"
    try:
        found = await clips.rutube_trailer([t for t in titles if t], item.get("year"))
        if not found:
            log.info("shorts: no Rutube trailer for facts about %s", _title(item))
            return {}, "трейлер на Rutube не нашёлся"
        trailer_dir.mkdir(exist_ok=True)
        src = await clips.download_full(found["url"], trailer_dir)
        frames = await clips.sample_frames(src, trailer_dir, every=5.0, duration=found["duration"])
        if not frames:
            return {}, "не удалось нарезать кадры из трейлера"
        thumbs = [Image.open(path).convert("RGB") for _, path in frames]
        ranked = await ai.rank_image_candidates(gemini_key, [text for _, text in facts_by_idx], thumbs)
        if ranked is None:
            return {}, f"ИИ не ответил ({ai.last_error or 'без подробностей'})"

        # Берём только прямое соответствие (>=75/100). Соседние моменты одного эпизода
        # не должны превращаться в несколько почти одинаковых клипов.
        proposals: list[tuple[int, int, int]] = []
        for (fact_index, _), match in zip(facts_by_idx, ranked):
            frame_index = int(match.get("index", -1))
            score = int(match.get("score", 0))
            if 0 <= frame_index < len(frames) and score >= 75:
                proposals.append((score, fact_index, frame_index))

        out: dict[int, str] = {}
        used_times: list[float] = []
        min_separation = CLIP_SECONDS + 2.0
        for score, fact_index, frame_index in sorted(proposals, reverse=True):
            timestamp = float(frames[frame_index][0])
            if any(abs(timestamp - used) < min_separation for used in used_times):
                log.info("shorts: skip near-duplicate trailer moment for fact %s (score %s)",
                         fact_index + 1, score)
                continue
            clip = work / f"fclip{fact_index}.mp4"
            await clips.cut_clip(src, clip, max(0.0, timestamp - 1.5), CLIP_SECONDS)
            if clip.exists() and clip.stat().st_size:
                out[fact_index] = clip.name
                used_times.append(timestamp)
        note = "" if out else f"в трейлере «{found['title'][:60]}» нет достаточно точных и разных моментов"
        return out, note
    except Exception as exc:  # noqa: BLE001 — без трейлера будут фото и кадры
        log.warning("shorts: trailer clips for facts about %s failed: %s", _title(item), exc)
        return {}, f"ошибка: {type(exc).__name__}: {str(exc)[:150]}"
    finally:
        import shutil
        shutil.rmtree(trailer_dir, ignore_errors=True)


# ---------------------------------------------------------------- новости кино

NEWS = 5


async def _news_film(fetch: Fetch, item: news.NewsItem) -> dict[str, Any] | None:
    """Фильм из «кавычек» заголовка — через поиск Кинопоиска (poiskkino)."""
    if not item.film_query:
        return None
    try:
        data = await fetch("/movie/search", query=item.film_query, page=1, limit=3)
    except Exception:  # noqa: BLE001
        return None
    want = item.film_query.lower()
    for d in data.get("docs") or []:
        names = {str(d.get(k) or "").lower() for k in ("name", "alternativeName", "enName")}
        if want in names and _image_url(d):
            return d
    return None


def _placeholder() -> Image.Image:
    """Фон для новости без картинки: тёмный градиент с логотипом."""
    bg = Image.linear_gradient("L").resize((1280, 720)).point(lambda v: 20 + v // 6)
    img = Image.merge("RGB", (bg, bg.point(lambda v: v * 3 // 4), bg.point(lambda v: v // 2)))
    logo = Image.open(ASSETS / "logo.png").convert("RGBA")
    logo.thumbnail((520, 520))
    img.paste(logo, ((1280 - logo.width) // 2, (720 - logo.height) // 2), logo)
    return img


def _tg_news(posts: list[dict[str, Any]], exclude: set[int]) -> list[news.NewsItem]:
    """Посты каналов как новости: первая фраза — заголовок, дальше — подробности."""
    out = []
    for p in posts[:20]:
        first, _, rest = re.sub(r"\s+", " ", p["text"]).partition(". ")
        title = first[:140].rstrip(" .") if len(first) <= 160 else first[:140].rsplit(" ", 1)[0] + "…"
        item = news.NewsItem(title=title, lead=rest[:300], source=f"t.me/{p['channel']}", link=p["url"],
                             published=p["time"], image=p.get("image"), video=p.get("video"))
        quoted = re.search(r"«([^»]{2,60})»", p["text"])
        item.film_query = quoted.group(1) if quoted else None
        if item.uid not in exclude:
            out.append(item)
    return out


async def make_news_short(fetch: Fetch, work: Path, feeds: list[str], exclude: set[int] | None = None,
                          music: Path | None = None, tmdb_key: str = "",
                          today: date | None = None, gemini_key: str = "") -> ShortResult:
    today = today or date.today()
    await _progress(5, "Читаю ленты новостей")
    items = await news.latest_news(feeds, exclude or set())
    # ИИ-редактор: главные новости без местных событий и повторов, пересказ понятными словами
    ai.last_error = None
    ai.last_engine = ""
    retold: list[str] | None = None
    if ai.available(gemini_key):
        # свежие посты Telegram-каналов — ИИ сверяет их с новостями СМИ и берёт то, чего в лентах ещё нет
        await _progress(7, "Читаю Telegram-каналы")
        items = items[:25] + _tg_news(await tgsearch.latest(fact_channels, news.MAX_AGE_HOURS), exclude or set())
    if ai.available(gemini_key) and len(items) >= 3:
        await _progress(8, "ИИ выбирает главные новости")
        candidates = items[:45]
        picked = await ai.pick_news(gemini_key, [(it.title, it.lead, it.source) for it in candidates], NEWS)
        if picked:
            items = [candidates[p["i"]] for p in picked]
            await _progress(10, "Корректор проверяет падежи и имена")
            retold = await ai.proofread(gemini_key, [p["text"] for p in picked])
            for it, p in zip(items, picked):
                it.film_query = p["film"] or it.film_query  # по-русски — картинку найдём на Кинопоиске
    await _progress(12, "Качаю картинки к новостям")
    # сначала новости с картинкой (кадр фильма или фото из ленты), потом остальные
    with_img: list[tuple[news.NewsItem, list[str]]] = []
    without: list[tuple[news.NewsItem, list[str]]] = []
    for item in items[:25]:
        urls: list[str] = []
        film = await _news_film(fetch, item)
        if film:
            item.film_name = _title(film)
            backdrops, _ = await tmdb.images(tmdb_key, film, limit=1)
            urls = backdrops + [u for u in [_image_url(film)] if u]
        if item.image:
            urls.append(item.image)
        (with_img if urls else without).append((item, urls))
        if len(with_img) >= NEWS and not retold:
            break
    if retold:  # порядок редактора: самое интересное первым; картинки нет — будет заставка
        chosen = [(it, next(u for i2, u in with_img + without if i2 is it)) for it in items]
    else:
        chosen = sorted((with_img + without)[:NEWS], key=lambda x: -x[0].published)
    if len(chosen) < 3:
        raise RuntimeError(f"Мало свежих новостей ({len(chosen)}). Проверь ленты: /feeds")
    n = len(chosen)

    cards = []
    images: list[Image.Image] = []
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for item, urls in chosen:
            try:
                images.append(await _download_first(session, urls))
            except Exception:  # noqa: BLE001
                images.append(_placeholder())
    for i, ((item, _), img) in enumerate(zip(chosen, images), 1):
        card = await asyncio.to_thread(make_card, img, "", i, n, None, "IMDb", None, f"НОВОСТЬ №{i}", FACT_CARD_H)
        path = work / f"card{i}.jpg"
        card.save(path, quality=93)
        video: dict[str, str] = {}
        if item.video:  # видео из поста канала — 5 секунд из середины
            try:
                files = await tgsearch.download_media([{"video": item.video}], work, 1, prefix=f"nv{i}_")
                if files:
                    dur = await _duration(files[0])
                    clip = work / f"newsclip{i}.mp4"
                    await clips.cut_clip(files[0], clip, max(0.0, dur / 2 - CLIP_SECONDS / 2), min(CLIP_SECONDS, dur))
                    card_overlay("", i, n, None, "IMDb", None, f"НОВОСТЬ №{i}", FACT_CARD_H).save(
                        work / f"overlay{i}.png")
                    video = {"clip": clip.name, "overlay": f"overlay{i}.png"}
            except Exception as exc:  # noqa: BLE001 — останется картинка
                log.warning("shorts: news video %s skipped: %s", item.video[:80], exc)
        cards.append({"image": path.name, "caption": item.title, "info": [[("Источник", item.source)]],
                      "karaoke": True, **video})

    day = _date_ru(today)
    cover = await asyncio.to_thread(make_cover, images[0], "НОВОСТИ", f"Главные новости кино\n{day}")
    cover_path = work / "cover.jpg"
    cover.save(cover_path, quality=93)

    def say(item: news.NewsItem) -> str:
        title = item.title if item.title[-1] in ".!?…" else item.title + "."
        return f"{title} {item.lead}".strip()

    # пересказ редактора; не вышло — Gemini пересказывает по заголовкам; без ИИ — заголовок и лид как есть
    if not retold:
        await _progress(30, "ИИ пересказывает новости")
        retold = await ai.retell_news(gemini_key, [(it.title, it.lead, it.source) for it, _ in chosen])
    spoken = retold or [say(it) for it, _ in chosen]
    text_engine = ai.last_engine  # кто писал пересказ (до озвучки: там свои запросы к ИИ)
    # дата — только на обложке: голосом «на двадцать третье октября» звучит затянуто
    lines = (["Главные новости кино!"] + spoken
             + [f"Какая новость удивила? Пиши в комментариях! {SAVE_AND_SUBSCRIBE}"])
    voiced = await _render(work, cover_path, cards, lines, "НОВОСТИ", "НОВОСТИ КИНО",
                           "КАКАЯ НОВОСТЬ\nУДИВИЛА БОЛЬШЕ?\nПИШИ В КОММЕНТАРИЯХ", music)

    first = chosen[0][0].title
    title = f"Новости кино {day}: {first}"
    title = (title if len(title) <= 88 else title[:85].rstrip() + "…") + " #shorts"
    description = "\n".join([
        f"Главные новости кино на {day} 🎬", "",
        *[f"{i}. {it.title} ({it.source})" for i, (it, _) in enumerate(chosen, 1)], "",
        "Какая новость удивила больше всего? Пиши в комментариях 👇", "",
        f"✈️ Telegram: {TELEGRAM_URL} — новинки, трейлеры и подборки каждый день", "",
        "#новостикино #кино #KinoMir360",
    ])
    tags: list[str] = []
    for tag in ["новости кино", "новости кино сегодня", "новости сериалов", "кино новости", "что нового в кино"] + \
            [it.film_name.lower() for it, _ in chosen if it.film_name] + ["KinoMir360", "киномир360", "шортс кино"]:
        if tag not in tags and len(", ".join(tags + [tag])) <= 480:
            tags.append(tag)
    seo = {"title": title, "description": description, "tags": ", ".join(tags),
           "pinned": f"Новости кино каждый день — в нашем Telegram 👉 {TELEGRAM_URL}\nКакая новость главная? 👇"}
    theme = Theme("news", "НОВОСТИ", "НОВОСТИ КИНО", f"Главные новости кино на {day}", title, [])
    result = ShortResult(work / "short.mp4", cover_path, theme,
                         [{"id": it.uid, "name": it.title} for it, _ in chosen], seo)
    if retold and text_engine == "reserve":  # ни Gemini, ни Lumean не ответили — писал слабый запасной ИИ
        result.seo["warning"] = ("⚠️ Gemini на лимите, Lumean не ответил — текст писал запасной ИИ, он может звучать "
                                 "коряво. Послушай перед загрузкой; лучше пересобрать позже (лимит Gemini "
                                 "обновляется около 13:00 по Екатеринбургу) или пополнить Lumean.")
    if not retold:  # пересказа нет — в ролике заголовки как есть; ошибка Gemini при удачном запасном ИИ — не повод
        result.seo["warning"] = (f"⚠️ ИИ не пересказал новости (в ролике заголовки как есть): "
                                 f"{ai.last_error or 'ИИ не ответил'}")
    if not voiced or tts_note:
        result.seo["warning"] = no_voice_note(voiced)
    return result


# ---------------------------------------------------------------- голос

NUMBERS = {1: "один", 2: "два", 3: "три", 4: "четыре", 5: "пять", 6: "шесть", 7: "семь", 8: "восемь",
           9: "девять", 10: "десять"}
SAVE_AND_SUBSCRIBE = "Сохрани и подпишись на КиноМир триста шестьдесят!"
# если в последней фразе уже есть «сохраняй…» — только «подпишись», без двойного «Сохраняй… Сохрани…»
SUBSCRIBE_ONLY = "Подпишись на КиноМир триста шестьдесят!"


def with_outro(line: str) -> str:
    """Последняя фраза ролика + призыв подписаться (не повторяя «сохрани»/«подпишись», если они уже есть)."""
    low = line.lower()
    if "подпиш" in low or "подписыв" in low:
        return line
    return f"{line} {SUBSCRIBE_ONLY if 'сохран' in low else SAVE_AND_SUBSCRIBE}"


def _say_film(item: dict[str, Any], theme: Theme, place: int) -> str:
    """Обратный отсчёт: «Номер пять — «X». Комедия.» … «И номер один — «X»!»."""
    title = f"«{_title(item)}»"
    if theme.key.startswith("genre:"):
        # в жанровой подборке жанр и так ясен («Ужасы.» на каждом фильме звучит глупо) — называем год
        year = f", {item['year']} год" if item.get("year") else ""
        return f"И номер один — {title}{year}!" if place == 1 else f"Номер {NUMBERS.get(place, place)} — {title}{year}."
    parts = [f"И номер один — {title}!" if place == 1 else f"Номер {NUMBERS.get(place, place)} — {title}."]
    genres = _genres(item)
    if theme.upcoming and _premiere(item):
        parts.append(f"В кино с {_date_ru(_premiere(item))}.")
    elif genres:
        parts.append(f"{genres[0].capitalize()}.")
    return " ".join(parts)


def voice_lines(theme: Theme, films: list[dict[str, Any]]) -> list[str]:
    # короткое начало — первые секунды решают, досмотрят ли ролик; «сохрани» — в конце
    n = len(films)
    return ([f"{theme.hook}!"]
            + [_say_film(f, theme, n - i) for i, f in enumerate(films)]
            + [f"Какой посмотришь первым? Пиши в комментариях! {SAVE_AND_SUBSCRIBE}"])


def _rank_key(item: dict[str, Any]) -> tuple[float, int]:
    """Чем выше, тем ближе к №1: лучший рейтинг, при равенстве — больше оценок."""
    return _best_rating(item)[0], int((item.get("votes") or {}).get("kp") or 0)


async def _run(*cmd: str) -> str:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, cwd=ROOT.parent)
    try:
        out, err = await proc.communicate()
    except asyncio.CancelledError:
        proc.kill()  # сборку прервали по таймауту — не оставляем рендер жрать память
        raise
    if proc.returncode:
        raise RuntimeError(f"{cmd[0]} failed: {err.decode(errors='ignore')[-800:]}")
    return out.decode(errors="ignore")


async def _duration(path: Path) -> float:
    out = await _run("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path))
    return float(out.strip())


def no_voice_note(voiced: bool = False) -> str:
    reason = f" Причина: {tts_error}." if tts_error else ""
    if voiced:
        return tts_note or ""
    return f"⚠️ Голос не синтезировался — ролик собран только с музыкой.{reason} Попробуй собрать ещё раз."


TTS_TIMEOUT = 20  # сек на фразу (обычно отвечает за 1–3 с): без лимита запрос к сервису голоса может висеть бесконечно


async def _tts(text: str, path: Path) -> list[float]:
    """Синтез в mp3; возвращает время начала каждого слова (сек) — для субтитров-караоке."""
    import edge_tts

    words: list[float] = []
    with open(path, "wb") as out:
        async for chunk in edge_tts.Communicate(text, VOICE, rate=VOICE_RATE, boundary="WordBoundary").stream():
            if chunk["type"] == "audio":
                out.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                words.append(chunk["offset"] / 10_000_000)  # единицы по 100 нс
    return words


TTS_RETRY_DELAYS = (4, 12)  # сервис голоса иногда сбоит/ограничивает частые запросы — повторяем, а не сдаёмся
TTS_PAUSE = 0.5  # пауза между фразами: частые запросы подряд сервис иногда отвергает «No audio»
TTS_COOLDOWN = 20  # фраза не прошла — ждём подольше и пробуем её целиком ещё раз
tts_error: str | None = None  # причина, если голос так и не получился — показываем админу
tts_note: str | None = None  # пометка, если ролик озвучен запасным голосом


async def _say_once(text: str, path: Path) -> list[float]:
    """Одна попытка; не принял знак ударения — сразу повторяем без него."""
    try:
        return await asyncio.wait_for(_tts(text, path), timeout=TTS_TIMEOUT)
    except Exception:
        if pronounce.STRESS not in text:
            raise
        log.warning("shorts: TTS failed with stress marks, retrying without them")
        return await asyncio.wait_for(_tts(text.replace(pronounce.STRESS, ""), path), timeout=TTS_TIMEOUT)


async def _edge_to_file(text: str, path: Path) -> list[float]:
    """Основной голос (Microsoft Edge) с повторами при сбое сервиса."""
    for delay in (*TTS_RETRY_DELAYS, None):
        try:
            return await _say_once(text, path)
        except Exception as exc:  # noqa: BLE001
            if delay is None:
                raise
            log.warning("shorts: TTS attempt failed (%r), retry in %ss", exc, delay)
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


def piper_ready() -> bool:
    import importlib.util

    return importlib.util.find_spec("piper") is not None and PIPER_MODEL.exists()


async def _piper_batch(items: list[tuple[str, Path]]) -> None:
    """Запасной голос Piper в mp3 для нескольких фраз — отдельным процессом (память освобождается сразу)."""
    wavs = [(text.replace(pronounce.STRESS, ""), str(path.with_suffix(".piper.wav"))) for text, path in items]
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "shorts.piper_tts", str(PIPER_MODEL), str(PIPER_SPEED),
        stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=ROOT.parent)
    try:
        _, err = await asyncio.wait_for(proc.communicate(json.dumps(wavs).encode()), timeout=180)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        proc.kill()
        raise
    if proc.returncode:
        raise RuntimeError(f"piper: {err.decode(errors='ignore')[-300:]}")
    for (_, wav), (_, path) in zip(wavs, items):
        await _run("ffmpeg", "-v", "error", "-y", "-i", wav, "-c:a", "libmp3lame", "-q:a", "3", str(path))
        Path(wav).unlink(missing_ok=True)


async def _piper_to_file(text: str, path: Path) -> list[float]:
    """Одна фраза запасным голосом; времени слов нет — субтитры идут равномерно по фразе."""
    await _piper_batch([(text, path)])
    return []


last_engine = "lumean"  # каким голосом озвучена последняя фраза (для /voice)
lumean_error = ""  # почему не сработал голос ElevenLabs (Lumean) — для /voice


async def say_to_file(text: str, path: Path) -> list[float]:
    """Озвучить фразу в mp3 голосом ElevenLabs (Lumean) — других голосов больше нет."""
    global last_engine, lumean_error
    if not lumean.enabled():
        lumean_error = "нет переменной LUMEAN_API_KEY"
        raise RuntimeError("голос Lumean не подключён: бот не видит LUMEAN_API_KEY в .env")
    try:
        await lumean.synthesize([_for_lumean(text)], [path], path.parent)
    except Exception as exc:
        lumean_error = str(exc)[:300]
        raise
    lumean_error, last_engine = "", "lumean"
    return []


stressed: dict[str, int] = {}  # слова со знаком ударения в последней озвучке (выпуск шлёт их автору на проверку)


async def spoken_lines(lines: list[str], careful: bool = False) -> list[str]:
    """Текст для голоса: словарь произношения, затем ИИ пишет числа словами в нужном падеже и латиницу
    русскими буквами; что ИИ не исправил (или он недоступен) — простая замена без ИИ."""
    # даты и порядковые («4 сезон» → «четвёртый сезон», «2-й» → «второй») — сразу, до ИИ: ИИ их иногда
    # читает количественными («четыре сезон»)
    texts = [pronounce.dates(pronounce.numbers(pronounce.apply(text))) for text in lines]
    # ИИ правит все фразы: числа и латиница — словами, а ещё ставит ударения в именах и редких словах.
    # Ответ ИИ запоминается по фразе: при переозвучке та же фраза даёт тот же текст для голоса — и берётся из
    # кэша озвучки бесплатно, а заново ИИ и голос делают только фразы, которые исправили (/say, правка текста)
    cache = _spoken_cache()
    keys = [_spoken_key(text, careful) for text in texts]
    todo = [i for i, key in enumerate(keys) if key not in cache]
    for i, key in enumerate(keys):
        if key in cache:
            texts[i] = cache[key]
    if todo:
        try:  # не задерживаем сборку, если ИИ тормозит
            fixed = await asyncio.wait_for(ai.spoken_form([texts[i] for i in todo], premium=careful),
                                           timeout=120 if careful else 60)
        except asyncio.TimeoutError:
            fixed = None
        for i, text in zip(todo, fixed or []):
            texts[i] = pronounce.apply(text)  # ИИ мог потерять знак ударения из словаря — ставим снова
            cache[keys[i]] = texts[i]
        if fixed:
            _save_spoken_cache(cache)
    out = [_speakable(pronounce.plain_russian(text)) for text in texts]
    # на проверку автору — ударения ИИ и словаря (имена, редкие слова); от нейросети во всех словах — не шлём
    for text in out:
        for word in re.findall(rf"[\w{pronounce.STRESS}-]*{pronounce.STRESS}[\w{pronounce.STRESS}-]*", text):
            stressed[word] = stressed.get(word, 0) + 1
    return await _accent(out)


DATA_DIR = Path(os.getenv("VKINO_DATA", "/tmp/kinomir_data"))  # в Docker — том, переживает обновления бота
VOICE_CACHE = DATA_DIR / "voice_cache"
VOICE_CACHE_MAX = 4000  # фраз (~150 МБ); старые удаляем
voice_cache_hits = 0  # сколько фраз последней озвучки взято из кэша (не оплачено заново)
voice_fresh: list[int] = []  # номера фраз последней озвучки, озвученных заново (не из кэша)
last_voice: dict[str, Any] = {}  # разметка последней озвучки (synthesize) — сохраняется в проект ролика
SPOKEN_CACHE = DATA_DIR / "spoken_cache.json"
SPOKEN_CACHE_MAX = 6000  # фраз; старые выкидываем


def _spoken_key(text: str, careful: bool) -> str:
    return hashlib.sha1(f"s1|{int(careful)}|{text}".encode("utf-8")).hexdigest()


def _spoken_cache() -> dict[str, str]:
    try:
        data = json.loads(SPOKEN_CACHE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_spoken_cache(cache: dict[str, str]) -> None:
    items = list(cache.items())[-SPOKEN_CACHE_MAX:]  # словарь хранит порядок добавления — старые в начале
    try:
        SPOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SPOKEN_CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(items), ensure_ascii=False), encoding="utf-8")
        tmp.replace(SPOKEN_CACHE)
    except OSError as exc:
        log.info("shorts: spoken cache write failed: %s", exc)


def _voice_key(text: str, pause: float) -> Path:
    # настройки голоса тоже в ключе: сменили скорость или живость — фраза озвучивается заново
    raw = (f"v2|{lumean.SERVICE}|{lumean.VOICE_ID}|{lumean.MODEL}|{lumean.SPEED:g}|{lumean.STABILITY:g}|"
           f"{lumean.STYLE:g}|{pause:g}|{text}")
    return VOICE_CACHE / (hashlib.sha1(raw.encode("utf-8")).hexdigest() + ".mp3")


async def _lumean_cached(speak: list[str], parts: list[Path], work: Path, pause: float) -> None:
    """Озвучка Lumean с кэшем по фразам: уже оплаченная фраза (тот же текст, голос и ударения) берётся из кэша,
    заказываются только новые или исправленные. Перемонтаж выпуска после правки одного слова — это одна-две
    фразы, а не весь выпуск."""
    global voice_cache_hits, voice_fresh
    texts = [_for_lumean(t) for t in speak]
    keys = [_voice_key(t, pause) for t in texts]
    todo = []
    for i, key in enumerate(keys):
        if key.exists() and key.stat().st_size > 1000:
            shutil.copyfile(key, parts[i])
            os.utime(key)  # нужная фраза — свежая, при чистке кэша удаляются давно не нужные
        else:
            todo.append(i)
    voice_cache_hits = len(speak) - len(todo)
    voice_fresh = list(todo)
    if not todo:
        return
    await lumean.synthesize([texts[i] for i in todo], [parts[i] for i in todo], work, pause)
    doubtful = await _verify_parts([speak[i] for i in todo], [parts[i] for i in todo], work, pause)
    skip = {todo[k] for k in doubtful}  # сомнительный кусок в кэш не кладём — в следующий раз озвучится заново
    try:
        VOICE_CACHE.mkdir(parents=True, exist_ok=True)
        for i in todo:
            if i not in skip and parts[i].exists() and parts[i].stat().st_size > 1000:
                shutil.copyfile(parts[i], keys[i])
        files = sorted(VOICE_CACHE.glob("*.mp3"), key=lambda f: f.stat().st_mtime)
        for old in files[:max(0, len(files) - VOICE_CACHE_MAX)]:
            old.unlink(missing_ok=True)
    except OSError as exc:
        log.info("shorts: voice cache write failed: %s", exc)


def accent_url() -> str:
    """Сервис ударений RUAccent: свой адрес из .env или контейнер accent (COMPOSE_PROFILES=accent)."""
    url = os.getenv("ACCENT_URL", "").strip()
    if url:
        return url.rstrip("/")
    profiles = {p.strip() for p in os.getenv("COMPOSE_PROFILES", "").split(",")}
    return "http://accent:8000" if "accent" in profiles else ""


accent_note = ""  # почему сервис ударений не сработал (для /voice и отчёта)


async def _accent(texts: list[str]) -> list[str]:
    """Ударения во всех обычных словах от нейросети RUAccent (если сервис включён). Не ответил — без них."""
    global accent_note
    url = accent_url()
    if not texts:
        return texts
    if not url and (stress_mode != "all" or os.getenv("STRESS_DICT", "1") == "0"):
        return texts  # ударения во всех словах — только в режиме all (голос от них сбивается)
    if not url:  # без сервиса — словарь ударений на диске (3 млн словоформ, почти без памяти)
        if not await stressdb.ready(DATA_DIR):
            accent_note = stressdb.status
            return texts
        plain = [t.replace(pronounce.STRESS, "") for t in texts]
        found = stressdb.lookup([w.lower() for t in plain for w in re.findall(r"[А-Яа-яЁё]+", t)])
        return [pronounce.merge_stress(t, stressdb.accented(p, found)) for t, p in zip(texts, plain)]
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
            async with session.post(f"{url}/accent",
                                    json={"texts": [t.replace(pronounce.STRESS, "") for t in texts]}) as resp:
                resp.raise_for_status()
                got = (await resp.json()).get("texts") or []
    except Exception as exc:  # noqa: BLE001
        accent_note = f"сервис ударений не ответил: {str(exc)[:150] or type(exc).__name__}"
        log.warning("shorts: accent service failed: %s", exc)
        return texts
    accent_note = ""
    if len(got) != len(texts):
        return texts
    return [pronounce.merge_stress(t, str(a)) for t, a in zip(texts, got)]


# Знаки ударения для голоса (меняется в боте: /stress):
#   names — только имена, названия и слова из словаря /say (ударения ИИ и автора);
#   all   — ещё и во всех обычных словах по словарю ударений (ElevenLabs от этого сбивается: «сэйчес», «январт»);
#   off   — без знаков, голос ставит ударения сам.
STRESS_MODES = ("names", "all", "off")
# по умолчанию без знаков: в выпусках 4 ноября голос коверкал именно слова со знаком («Мэйть Тю», «войтить и»)
stress_mode = "names" if os.getenv("LUMEAN_STRESS", "0") == "1" else "off"


stress_style = os.getenv("STRESS_STYLE", "caps")  # как отдать ударение голосу: caps / plus / acute (/stress)


def _for_lumean(text: str) -> str:
    # режим off — ударения ИИ голосу не отдаём (он от них сбивался), но ударения из словаря (/say) — всегда
    text = pronounce.keep_dictionary_stress(text) if stress_mode == "off" else text
    return pronounce.render_stress(text, stress_style)


def _speakable(text: str) -> str:
    """Убираем эмодзи и редкие символы — на них сервис голоса может ответить «No audio»."""
    text = re.sub(rf"[^\w\s.,!?:;«»\"'()%+\-–—{pronounce.STRESS}]", " ", text)
    text = re.sub(r"\s+([.,!?:;])", r"\1", re.sub(r"\s+", " ", text))
    return text.strip() or "…"


def _pieces(text: str) -> list[str]:
    """Фраза по предложениям, длинные — ещё и по запятым."""
    out: list[str] = []
    for sent in re.split(r"(?<=[.!?…])\s+", text):
        if len(sent) > 90 and "," in sent:
            out += [p.strip() for p in re.split(r"(?<=,)\s+", sent) if p.strip()]
        elif sent.strip():
            out.append(sent.strip())
    return out


async def _edge_pieces(text: str, path: Path, stem: Path) -> tuple[list[float], list[str]]:
    """Озвучить фразу кусками основным голосом и склеить; куски, которые не вышли, пропускаем.

    Возвращает (время слов во всей фразе, тексты пропущенных кусков). Если не вышло ничего — тишина.
    """
    files: list[Path] = []
    words: list[float] = []
    lost: list[str] = []
    t = 0.0
    for k, piece in enumerate(_pieces(text)):
        out = stem.with_name(f"{stem.name}_p{k}.mp3")
        if k:
            await asyncio.sleep(TTS_PAUSE * 2)
        try:
            got = await _say_once(piece, out)
        except Exception as exc:  # noqa: BLE001
            log.warning("shorts: Edge TTS piece failed %r: %r", piece[:60], exc)
            lost.append(piece)
            continue
        words += [t + w for w in got]
        t += await _duration(out) + 0.1
        files.append(out)
    if not files:
        await _run("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                   "-t", f"{max(2.5, min(len(text) / 14, 8.0)):.2f}", str(path))
        return [], lost
    inputs = [arg for f in files for arg in ("-i", str(f))]
    chain = "".join(f"[{i}:a]aresample=24000,apad=pad_dur=0.1[a{i}];" for i in range(len(files)))
    chain += "".join(f"[a{i}]" for i in range(len(files))) + f"concat=n={len(files)}:v=0:a=1[out]"
    await _run("ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", chain, "-map", "[out]",
               "-c:a", "libmp3lame", "-q:a", "3", str(path))
    return words, lost


async def synthesize(lines: list[str], work: Path, pause: float | None = None, careful: bool = False,
                     prepared: bool = False) -> tuple[Path, list[float], bool, list[dict[str, Any]]]:
    """Озвучка по фразам: файл, начало каждой фразы, есть ли голос, и по фразе {длительность, время слов}."""
    global tts_error, tts_note
    tts_error, tts_note = None, None
    work.mkdir(parents=True, exist_ok=True)  # папку для фраз создаём здесь — вызывающий мог не создать
    speak = list(lines) if prepared else await spoken_lines(lines, careful)  # prepared — уже текст для голоса
    parts: list[Path] = [work / f"voice_{i}.mp3" for i in range(len(lines))]
    words: list[list[float]] = []
    failed: list[int] = []
    dropped: list[str] = []
    # голос — только ElevenLabs через Lumean: запасные (Microsoft, Piper) звучали заметно хуже,
    # и ролик с роботизированным голосом всё равно приходилось пересобирать
    if not lumean.enabled():
        raise RuntimeError("голос Lumean не подключён: бот не видит LUMEAN_API_KEY в .env")
    try:
        await _lumean_cached(speak, parts, work, pause or lumean.PAUSE)
        words = [[] for _ in speak]
    except Exception as exc:  # noqa: BLE001
        log.warning("shorts: Lumean TTS failed: %s", exc)
        raise RuntimeError(f"голос Lumean не сработал ({str(exc)[:200]}). Обычно это очередь или сбой у "
                           "Lumean — попробуй собрать ещё раз через 30–60 минут") from exc
    voiced = not failed
    if dropped:
        tts_note = ("🗣 Голос не смог озвучить кусок текста — в ролике он без голоса (на экране есть): "
                    + "; ".join(f"«{d[:60]}»" for d in dropped[:3]))

    starts, spans, t = [], [], 0.0
    inputs, chains = [], []
    for i, part in enumerate(parts):
        starts.append(round(t, 3))
        dur = await _duration(part)
        spans.append({"speech": round(dur, 3), "words": [round(w, 3) for w in words[i]],
                      "text": speak[i].replace(pronounce.STRESS, "")})
        t += dur + GAP
        inputs += ["-i", str(part)]
        chains.append(f"[{i}:a]aresample=48000,apad=pad_dur={GAP}[a{i}]")
    filt = ";".join(chains) + ";" + "".join(f"[a{i}]" for i in range(len(parts))) + \
        f"concat=n={len(parts)}:v=0:a=1[out]"
    voice = work / "voice.wav"
    await _run("ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", filt, "-map", "[out]", str(voice))
    global last_voice
    # разметка озвучки — для «🎙 Переозвучить только звук»: какие фразы, где начинаются и сколько звучат
    last_voice = {"file": voice.name, "lines": list(lines), "prepared": prepared, "careful": careful,
                  "pause": pause, "starts": starts, "speech": [sp["speech"] for sp in spans]}
    return voice, starts, voiced, spans


def _extra_words(expected: str, heard: list[dict[str, Any]]) -> list[str]:
    """Слова, которые голос произнёс сверх текста (вставки, а не ошибки распознавания в наших словах)."""
    from difflib import SequenceMatcher
    want = [_norm_word(w) for w in expected.split() if _norm_word(w)]
    got = [_norm_word(h["w"]) for h in heard if _norm_word(h["w"])]
    extra: list[str] = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, want, got, autojunk=False).get_opcodes():
        if tag == "insert":
            extra += got[j1:j2]
        elif tag == "replace" and (j2 - j1) - (i2 - i1) >= 2:  # услышали заметно больше слов, чем было
            extra += got[j1 + (i2 - i1):j2]
    return extra


def _missing_words(expected: str, heard: list[dict[str, Any]]) -> list[str]:
    """Слова текста, которых в озвучке фразы нет вовсе: фраза обрезана или к ней приклеился кусок соседней,
    а свой ушёл в другую фразу (тогда звук съезжает относительно субтитров и картинок)."""
    from difflib import SequenceMatcher
    want = [_norm_word(w) for w in expected.replace(pronounce.STRESS, "").split() if _norm_word(w)]
    got = [_norm_word(h["w"]) for h in heard if _norm_word(h["w"])]
    lost: list[str] = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, want, got, autojunk=False).get_opcodes():
        if tag == "delete":
            lost += want[i1:i2]
        elif tag == "replace" and (i2 - i1) - (j2 - j1) >= 2:  # услышали заметно меньше слов, чем было
            lost += want[i1 + (j2 - j1):i2]
    return lost


def _bad_part(expected: str, heard: list[dict[str, Any]]) -> int:
    """Насколько кусок озвучки не совпал с фразой: лишние слова (бормотание, кусок соседней фразы) и пропавшие.
    0–1 — норма (распознавание само ошибается), 2+ — переозвучиваем."""
    extra, lost = len(_extra_words(expected, heard)), len(_missing_words(expected, heard))
    words = len([w for w in expected.split() if _norm_word(w)])
    return extra + (lost if lost >= max(2, words // 4) else 0)


async def _verify_parts(speak: list[str], parts: list[Path], work: Path, pause: float) -> list[int]:
    """ElevenLabs иногда «бормочет» на месте паузы между фразами («Май. Май. Пиро.»), а нарезка общей дорожки
    может ошибиться границей: распознаём каждую фразу и, если в ней лишние слова или не хватает своих,
    переозвучиваем её отдельным заказом (лучший из двух вариантов). Возвращает номера фраз, которые так и
    остались сомнительными (их не кладём в кэш)."""
    if not (ai.RESERVE_KEY and "groq.com" in ai.RESERVE_URL):
        return []
    doubtful: list[int] = []
    for i, (text, part) in enumerate(zip(speak, parts)):
        try:
            heard = await ai.transcribe_words(part.read_bytes())
            bad = _bad_part(text, heard)
            for want, got in _misheard(text, heard):  # контроль озвучки: что голос, похоже, сказал не так
                voice_issues.append({"text": text, "want": want, "got": got})
        except Exception as exc:  # noqa: BLE001 — проверка не должна ломать сборку
            log.warning("shorts: voice check of phrase %d failed: %s", i + 1, exc)
            return doubtful
        if bad < 2:
            continue
        log.warning("shorts: phrase %d does not match its text (%d) — re-voicing it", i + 1, bad)
        redo = work / f"voice_redo_{i}.mp3"
        try:
            await lumean.synthesize([_for_lumean(text)], [redo], work, pause)
            again = _bad_part(text, await ai.transcribe_words(redo.read_bytes()))
            if again < bad:
                redo.replace(part)
            if min(again, bad) >= 2:
                doubtful.append(i)
        except Exception as exc:  # noqa: BLE001
            log.warning("shorts: re-voicing phrase %d failed: %s", i + 1, exc)
            doubtful.append(i)
    return doubtful


voice_issues: list[dict[str, str]] = []  # слова, которые распознавание услышало иначе (на проверку автору)


def _misheard(expected: str, heard: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Слова текста, которые в готовой озвучке распознались иначе: [(как в тексте, как услышано)]. Это не всегда
    ошибка голоса (распознавание путает имена), но именно эти места стоит послушать: искажённое имя, проглоченное
    окончание, английское чтение. Короткие слова и расхождения в одну букву не считаем."""
    from difflib import SequenceMatcher
    want = [w for w in (re.sub(r"[^\w-]", "", x) for x in expected.replace(pronounce.STRESS, "").split()) if w]
    got = [h["w"] for h in heard if _norm_word(h["w"])]
    a, b = [_norm_word(w) for w in want], [_norm_word(w) for w in got]
    out = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag not in ("replace", "delete"):
            continue
        src = " ".join(want[i1:i2])
        dst = " ".join(got[j1:j2])
        if len(src.replace(" ", "")) < 5 or any(ch.isdigit() for ch in src):
            continue
        if re.search(r"\d", dst) or (dst and re.fullmatch(r"[A-Za-z\s'.,-]+", dst)):
            continue  # распознавание пишет числа цифрами («360»), а имена — латиницей («Rotten Tomatoes»)
        if dst and (SequenceMatcher(None, _norm_word(src), _norm_word(dst)).ratio() >= 0.85 or (
                _sound(src) == _sound(dst) and len(src.split()) == len(dst.split()))):
            continue  # «режиссёры» / «режиссеры», «кобуре» / «кабуре» (безударное «о» звучит как «а») — то же
        out.append((src, dst or "—"))
    return out[:3]


def _sound(text: str) -> str:
    """Слово «как слышится»: безударные о/а и е/и в русской речи совпадают, твёрдый/мягкий знак не слышен."""
    return _norm_word(text).translate(str.maketrans({"о": "а", "е": "и", "ъ": None, "ь": None}))


def _norm_word(w: str) -> str:
    return re.sub(r"[^\w]", "", w.lower().replace("ё", "е"))


def _align_words(script: list[tuple[str, float, float]], heard: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Слова субтитров — из нашего текста (script: слово, начало и конец его фразы), время — из распознанной
    дорожки (heard). Распознавание ошибается в написании («Дисси», «7 из 10», «мухи»), а время у него точное:
    сопоставляем последовательности слов, несовпавшим словам время делим поровну между соседями."""
    from difflib import SequenceMatcher
    times: list[tuple[float, float] | None] = [None] * len(script)
    sm = SequenceMatcher(None, [_norm_word(w) for w, _, _ in script], [_norm_word(h["w"]) for h in heard],
                         autojunk=False)
    for a, b, size in sm.get_matching_blocks():
        for i in range(size):
            h = heard[b + i]
            times[a + i] = (float(h["s"]), float(h["e"]))
    k = 0
    while k < len(script):
        if times[k] is not None:
            k += 1
            continue
        j = k
        while j < len(script) and times[j] is None and script[j][1] == script[k][1]:
            j += 1  # пропуск внутри одной фразы
        left = times[k - 1][1] if k and times[k - 1] and script[k - 1][1] == script[k][1] else script[k][1]
        right = times[j][0] if j < len(script) and times[j] and script[j][1] == script[k][1] else script[k][2]
        right = max(right, left + 0.2 * (j - k))
        step = (right - left) / (j - k)
        for i in range(k, j):
            times[i] = (left + step * (i - k), left + step * (i - k + 1))
        k = j
    out = []
    for (w, _, _), t in zip(script, times):
        s0, e0 = t  # type: ignore[misc]
        if out and s0 < out[-1]["s"]:
            s0 = out[-1]["s"]
        out.append({"w": w, "s": round(s0, 2), "e": round(max(e0, s0 + 0.05), 2)})
    return out


async def word_timeline(voice: Path, starts: list[float], spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Субтитры по одному слову: [{"w", "s", "e"}] на всю озвучку.

    Слова — из нашего текста; время — распознаванием готовой дорожки (Whisper на Groq), а без ключа или
    при сбое — из синтеза (голос Microsoft его даёт) или пропорционально длине слова.
    """
    script = [(w, start, start + float(span.get("speech") or 0))
              for start, span in zip(starts, spans)
              for w in str(span.get("text") or "").split() if re.search(r"\w", w)]
    if ai.RESERVE_KEY and "groq.com" in ai.RESERVE_URL:
        mp3 = voice.with_suffix(".words.mp3")
        try:
            await _run("ffmpeg", "-v", "error", "-y", "-i", str(voice), "-ac", "1", "-ar", "16000",
                       "-c:a", "libmp3lame", "-b:a", "48k", str(mp3))
            words = await ai.transcribe_words(mp3.read_bytes())
            if words:
                return _align_words(script, words) if script else words
        except Exception as exc:  # noqa: BLE001 — тогда по тексту
            log.warning("shorts: word timestamps failed: %s", exc)
        finally:
            mp3.unlink(missing_ok=True)
    out: list[dict[str, Any]] = []
    for start, span in zip(starts, spans):
        tokens = [w for w in str(span.get("text") or "").split() if re.search(r"\w", w)]
        if not tokens:
            continue
        times = span.get("words") or []
        if len(times) >= len(tokens):
            marks = [start + t for t in times[:len(tokens)]]
        else:
            total = sum(len(w) + 1 for w in tokens)
            acc, marks = 0, []
            for w in tokens:
                marks.append(start + span["speech"] * acc / total)
                acc += len(w) + 1
        for k, (w, m) in enumerate(zip(tokens, marks)):
            end = marks[k + 1] if k + 1 < len(marks) else start + span["speech"]
            out.append({"w": w, "s": round(m, 2), "e": round(end, 2)})
    return out


# ---------------------------------------------------------------- SEO

def build_seo(theme: Theme, films: list[dict[str, Any]]) -> dict[str, str]:
    names = [_title(f) for f in films]
    title = f"{theme.seo_title} #shorts"
    if len(title) > 100:
        title = title[:90].rstrip() + "… #shorts"

    rows = []
    for i, f in enumerate(films):
        year = f.get("year")
        line = f"{len(films) - i}. {_title(f)}" + (f" ({year})" if year else "")
        rating, src = _rating(f)
        if theme.upcoming and _premiere(f):
            line += f" — в кино с {_date_ru(_premiere(f))}"
        elif rating:
            line += f" — {src} {rating:.1f}"
        if f.get("_awards"):
            line += f" 🏆 {f['_awards']}"
        rows.append(line)

    hashtags = ["#чтопосмотреть", "#новинкикино", "#KinoMir360"]
    if theme.key == "series":
        hashtags[1] = "#сериалы"
    elif theme.key == "upcoming":
        hashtags[1] = "#скоровкино"
    elif theme.key.startswith("genre:") or theme.key == "custom":
        hashtags[1] = "#подборкафильмов"

    description = "\n".join([
        f"{theme.hook} — сохрани, чтобы не потерять! 🍿", "", *rows, "",
        "Какой посмотришь первым? Пиши в комментариях 👇", "",
        f"✈️ Telegram: {TELEGRAM_URL} — новинки, трейлеры и подборки каждый день", "",
        " ".join(hashtags),
    ])

    tags: list[str] = []
    for t in theme.tags + [n.lower() for n in names] + \
            [a.lower() for f in films for a in _actors(f, 1)] + ["KinoMir360", "киномир360", "шортс кино"]:
        if t and t not in tags and len(", ".join(tags + [t])) <= 480:
            tags.append(t)

    pinned = (f"Все трейлеры и где смотреть — в нашем Telegram 👉 {TELEGRAM_URL}\n"
              "А какой фильм ты посмотришь первым? 👇")
    return {"title": title, "description": description, "tags": ", ".join(tags), "pinned": pinned}


def build_facts_seo(film: dict[str, Any], facts: list[str]) -> dict[str, str]:
    t, n, kind = _title(film), len(facts), _kind(film)
    title = f"{n} {_facts_word(n)} о {kind} «{t}», которые ты не знал 🤯 #shorts"
    if len(title) > 100:
        title = f"{n} {_facts_word(n)} о «{t}» 🤯 #shorts"[:100]
    year = film.get("year")
    description = "\n".join([
        f"{n} {_facts_word(n)} о {kind} «{t}»" + (f" ({year})" if year else "") + " — сохрани! 🍿", "",
        *[f"{i}. {f}" for i, f in enumerate(facts, 1)], "",
        "Какой факт удивил тебя больше всего? Пиши в комментариях 👇", "",
        f"✈️ Telegram: {TELEGRAM_URL} — новинки, трейлеры и подборки каждый день", "",
        "#фактыокино #кино #KinoMir360",
    ])
    tags: list[str] = []
    for tag in ["факты о фильме", "интересные факты о кино", t.lower(), f"{t.lower()} факты",
                f"{t.lower()} интересные факты", "как снимали", "съемки фильма"] + \
            [a.lower() for a in _actors(film, 3)] + ["KinoMir360", "киномир360", "шортс кино"]:
        if tag and tag not in tags and len(", ".join(tags + [tag])) <= 480:
            tags.append(tag)
    pinned = (f"Какой факт удивил больше? 👇\n"
              f"Ещё больше фактов и подборок — в нашем Telegram 👉 {TELEGRAM_URL}")
    return {"title": title, "description": description, "tags": ", ".join(tags), "pinned": pinned}


def build_review_seo(film: dict[str, Any], points: list[str]) -> dict[str, str]:
    t = _title(film)
    title = f"«{t}» — стоит ли смотреть? Короткий обзор 🎬 #shorts"
    if len(title) > 100:
        title = f"«{t}»: обзор 🎬 #shorts"[:100]
    year = film.get("year")
    description = "\n".join([
        f"«{t}»" + (f" ({year})" if year else "") + " — стоит ли смотреть? Короткий обзор 🎬", "",
        *[f"• {p}" for p in points], "",
        "А ты посмотришь? Пиши в комментариях 👇", "",
        f"✈️ Telegram: {TELEGRAM_URL} — новинки, трейлеры и подборки каждый день", "",
        "#обзорфильма #кино #KinoMir360",
    ])
    tags: list[str] = []
    for tag in [t.lower(), f"{t.lower()} обзор", f"{t.lower()} стоит ли смотреть", f"{t.lower()} отзывы",
                "обзор фильма", "стоит ли смотреть", "что посмотреть"] + \
            [a.lower() for a in _actors(film, 3)] + ["KinoMir360", "киномир360", "шортс кино"]:
        if tag and tag not in tags and len(", ".join(tags + [tag])) <= 480:
            tags.append(tag)
    pinned = f"А ты посмотришь «{t}»? 👇\nБольше обзоров — в нашем Telegram 👉 {TELEGRAM_URL}"
    return {"title": title, "description": description, "tags": ", ".join(tags), "pinned": pinned}


def build_rumors_seo(film: dict[str, Any], facts: list[str]) -> dict[str, str]:
    t, kind = _title(film), _kind(film)
    title = f"Что известно о {kind} «{t}»: новости и слухи 🔥 #shorts"
    if len(title) > 100:
        title = f"«{t}»: новости и слухи 🔥 #shorts"[:100]
    year = film.get("year")
    description = "\n".join([
        f"Что известно о {kind} «{t}»" + (f" ({year})" if year else "") + " — главные новости и слухи 🔥", "",
        *[f"{i}. {f}" for i, f in enumerate(facts, 1)], "",
        "Ждёшь премьеру? Пиши в комментариях 👇", "",
        f"✈️ Telegram: {TELEGRAM_URL} — новинки, трейлеры и подборки каждый день", "",
        "#новостикино #слухи #KinoMir360",
    ])
    tags: list[str] = []
    for tag in [t.lower(), f"{t.lower()} дата выхода", f"{t.lower()} новости", f"{t.lower()} слухи",
                f"{t.lower()} трейлер", "новости кино", "скоро выйдет"] + \
            [a.lower() for a in _actors(film, 3)] + ["KinoMir360", "киномир360", "шортс кино"]:
        if tag and tag not in tags and len(", ".join(tags + [tag])) <= 480:
            tags.append(tag)
    pinned = (f"Ждёшь премьеру «{t}»? 👇\n"
              f"Все новости о новинках — в нашем Telegram 👉 {TELEGRAM_URL}")
    return {"title": title, "description": description, "tags": ", ".join(tags), "pinned": pinned}


# ---------------------------------------------------------------- TikTok

# в TikTok нет названия и кликабельных ссылок: короткая подпись с ключевыми словами + хэштеги
# на русском (для русскоязычной ленты) и на английском (их TikTok часто продвигает шире)
TIKTOK_BASE = ["#кино", "#фильмы", "#чтопосмотреть", "#киноманы", "#KinoMir360"]
TIKTOK_BASE_EN = ["#movie", "#movies", "#film", "#cinema", "#fyp"]
TIKTOK_THEME = {
    "online": ["#новинкикино", "#онлайнкинотеатр", "#newmovies", "#streaming"],
    "cinema": ["#кинопрокат", "#вкино", "#boxoffice", "#nowshowing"],
    "upcoming": ["#скоровкино", "#премьеры", "#comingsoon", "#trailer"],
    "series": ["#сериалы", "#новыесериалы", "#tvseries", "#series"],
    "genre": ["#топфильмов", "#подборкафильмов", "#movierecommendations", "#top5"],
    "facts": ["#фактыокино", "#интересныефакты", "#moviefacts", "#didyouknow"],
    "rumors": ["#новостикино", "#слухи", "#скоровыйдет", "#comingsoon"],
    "news": ["#новостикино", "#киноновости", "#movienews", "#filmnews"],
    "birthday": ["#деньрождения", "#актеры", "#happybirthday", "#actor"],
    "lore": ["#историяперсонажа", "#комиксы", "#lore", "#comics"],
    "custom": ["#подборкафильмов", "#чтопосмотреть", "#movierecommendations", "#top"],
}
TIKTOK_MAX_TAGS = 15


def _hashtag(text: str) -> str | None:
    """«Мастер и Маргарита» -> #МастерИМаргарита; слишком длинное — не берём."""
    words = re.findall(r"[0-9A-Za-zА-Яа-яЁё]+", text)
    tag = "".join(w[:1].upper() + w[1:] for w in words)
    return f"#{tag}" if 2 < len(tag) <= 24 else None


def _names(result: ShortResult) -> tuple[list[str], list[str]]:
    """Названия (или имя именинника) по-русски и по-английски — для хэштегов; №1 первым."""
    key, films = result.theme.key, result.films
    if key == "news":  # здесь «фильмы» — заголовки новостей
        return [], []
    if key in ("birthday", "lore"):
        return [str(f.get("name") or "") for f in films], [str(f.get("en") or "") for f in films]
    films = films[::-1]  # в подборке №1 — последний
    ru = [_title(f) for f in films]
    en = [str(f.get("enName") or f.get("alternativeName") or "") for f in films]
    return ru, [t for t in en if t and re.search(r"[A-Za-z]", t)]


def _hashtags(result: ShortResult) -> list[str]:
    """Хэштеги ролика, самые точные первыми: тема, названия/имя (рус и англ), затем общие."""
    ru, en = _names(result)
    theme = TIKTOK_THEME.get(result.theme.key, [])
    tags = theme[:2] + [t for t in map(_hashtag, ru) if t][:2] + [t for t in map(_hashtag, en) if t][:2] \
        + theme[2:] + TIKTOK_BASE + TIKTOK_BASE_EN
    return list(dict.fromkeys(tags))


def tiktok_caption(result: ShortResult) -> str:
    """Подпись для TikTok: заголовок, вопрос для комментариев, призыв подписаться и хэштеги."""
    seo = result.seo
    title = re.sub(r"\s*#shorts\s*", " ", seo.get("title", "")).strip()
    question = next((ln.strip() for ln in seo.get("pinned", "").splitlines() if ln.strip().endswith("👇")),
                    "Пиши в комментариях 👇")
    tags = _hashtags(result)[:TIKTOK_MAX_TAGS]
    return "\n".join([title, "", question, "Подпишись, чтобы не пропустить новые ролики 🔔",
                      "Ещё больше — в нашем Telegram KinoMir360 (ссылка в профиле)", "", " ".join(tags)])


# YouTube: больше 15 хэштегов в описании — он игнорирует все, поэтому берём 14 + #shorts
YOUTUBE_MAX_TAGS = 14
EN_KEYWORDS = {
    "online": ["new movies", "streaming premieres"],
    "cinema": ["box office", "movies in theaters"],
    "upcoming": ["coming soon movies", "movie trailers"],
    "series": ["new tv series", "tv shows"],
    "genre": ["movie recommendations", "best movies"],
    "facts": ["movie facts", "behind the scenes"],
    "rumors": ["upcoming movies", "movie news"],
    "news": ["movie news", "film news"],
    "birthday": ["happy birthday", "best movies"],
    "lore": ["character origin", "comics lore"],
    "custom": ["movie recommendations", "best movies"],
}


def add_english_seo(result: ShortResult) -> None:
    """YouTube: хэштеги на русском и английском в конце описания и английские ключевые слова в тегах."""
    seo = result.seo
    hashtags = ["#shorts"] + [t for t in _hashtags(result) if t != "#fyp"][:YOUTUBE_MAX_TAGS]
    lines = seo.get("description", "").rstrip().splitlines()
    if lines and lines[-1].startswith("#"):
        lines = lines[:-1]  # прежняя строка хэштегов (только русские) — заменяем
    seo["description"] = "\n".join(lines + [" ".join(hashtags)])
    _, en = _names(result)
    words = [w.strip() for w in seo.get("tags", "").split(",") if w.strip()]
    for w in [e.lower() for e in en[:3]] + EN_KEYWORDS.get(result.theme.key, []) + ["movies", "film", "shorts"]:
        if w and w not in words and len(", ".join(words + [w])) <= 480:
            words.append(w)
    seo["tags"] = ", ".join(words)


# ---------------------------------------------------------------- сборка

async def _render(work: Path, cover_path: Path, cards: list[dict[str, Any]], lines: list[str],
                  tag: str, header: str, question: str, music: Path | None,
                  boom_slide: int | None = None, scene_plan: bool = False) -> bool:
    """Голос + рендер видео work/short.mp4; возвращает, удалось ли озвучить.

    Карточки с "karaoke": True получают время слов для подсветки текста.
    Звуки: «вжух» на каждом переходе, «бум» перед слайдом boom_slide (номер один в отсчёте).
    """
    await _progress(40, "Озвучка")
    # для «🔁 Полностью»: те же карточки и текст — до того, как ниже их поменяем
    rerun = {"fn": "render", "cover": cover_path.name, "cards": json.loads(json.dumps(cards)), "lines": list(lines),
             "tag": tag, "header": header, "question": question, "music": project._rel(music, work) if music else None,
             "boom_slide": boom_slide}
    voice, starts, voiced, spans = await synthesize(lines, work)
    voice_meta = dict(last_voice)
    plan_path: str | None = None
    if scene_plan:
        rerun["scene_plan_enabled"] = True
        plan = scene_planner.build_scene_plan(lines, starts, spans, cards)
        plan_file = work / "scene_plan.json"
        plan_file.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        plan_path = plan_file.name
    # субтитры по слову; текст, который и так звучит (караоке), на карточке тогда не дублируем
    words = await word_timeline(voice, starts, spans) if voiced else []
    for i, card in enumerate(cards, 1):  # слайд 0 — обложка
        if card.pop("karaoke", False):
            if words:
                card["caption"] = ""
            else:
                card["karaoke"] = spans[i]
    sfx = [[max(st - 0.15, 0), str(ASSETS / "whoosh.wav"), -6] for st in starts[1:]]
    if boom_slide is not None and boom_slide < len(starts):
        sfx.append([max(starts[boom_slide] - 0.05, 0), str(ASSETS / "boom.wav"), -5])
    cfg = {
        "output": "short.mp4",
        "intro": cover_path.name,
        "logo": str(ASSETS / "logo.png"),
        "voice": voice.name,
        "music": str(music or musiclib.pick(header, tag, " ".join(lines)) or ASSETS / "music.mp3"),
        "music_db": -16 if voiced else -6,
        "tail": 1.2,
        "fonts": {"head": str(ASSETS / "Oswald.ttf"), "sans": str(ASSETS / "Montserrat.ttf")},
        "tag": tag,
        "header": header,
        "cards": cards,
        "starts": starts,
        "sfx": sfx,
        "words": words,
        "outro": {
            "question": question,
            "channel": "KinoMir360",
            "youtube": "@kinomir360",
            "telegram": "@KinoMir360",
            "note": "ссылки в описании",
        },
    }
    cfg_path = work / "short.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    await _progress(50, "Монтаж видео")
    try:
        await _run_render(cfg_path)
    except RenderStalled:
        # монтаж встал (не хватает памяти/процессора на отрывки трейлеров) — собираем ещё раз без видео,
        # из карточек: так ролик всё равно получится
        log.warning("shorts: render stalled — retrying without trailer clips")
        for c in cfg["cards"]:
            c.pop("clip", None)
            c.pop("overlay", None)
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
        await _progress(50, "Монтаж встал — собираю без отрывков трейлеров")
        await _run_render(cfg_path, "Монтаж без отрывков")
    if plan_path:
        rerun["scene_plan"] = plan_path
    project.save(work, "short", "shorts.make_short", cfg_path.name, cfg["output"], voice_meta, rerun)
    await _progress(98, "Видео готово")
    return voiced


RENDER_STALL = 240  # монтаж не двигается столько секунд — он встал


class RenderStalled(RuntimeError):
    pass


async def _run_render(cfg_path: Path, stage: str = "Монтаж видео") -> None:
    """Рендер — отдельным процессом, чтобы не блокировать бота; его строки PROGRESS двигают полоску.
    Нет новой строки RENDER_STALL секунд — процесс убиваем (RenderStalled)."""
    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "shorts.make_short", str(cfg_path),
                                                stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, cwd=ROOT.parent)
    errors = asyncio.create_task(proc.stderr.read())  # читаем параллельно, иначе процесс может встать
    started = False  # до первой строки рендер готовит кадры трейлеров и звук — даём вдвое больше времени
    try:
        while True:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), RENDER_STALL * (1 if started else 2))
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise RenderStalled(f"монтаж не двигался {RENDER_STALL // 60} минуты") from None
            if not line:
                break
            m = re.match(rb"PROGRESS ([\d.]+)", line)
            if m:
                started = True
                await _progress(50 + round(48 * float(m.group(1))), stage)
        await proc.wait()
    except asyncio.CancelledError:
        proc.kill()  # сборку прервали по таймауту — не оставляем рендер жрать память
        raise
    err = await errors
    if proc.returncode:
        raise RuntimeError(f"{sys.executable} failed: {err.decode(errors='ignore')[-800:]}")


def _mentioned_person(text: str, item: dict[str, Any]) -> dict[str, Any] | None:
    """Актёр или режиссёр, чья фамилия есть в тексте (с учётом падежей: Хиддлстона, Нолану)."""
    for p in (item.get("persons") or [])[:25]:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        if str(p.get("enProfession") or "") not in ("actor", "director"):
            continue
        surname = str(p["name"]).split()[-1]
        if len(surname) < 4:
            continue
        stem = surname[:-1] if len(surname) >= 5 and surname[-1].lower() in "аеёиоуыэюяйь" else surname
        if re.search(rf"(?<!\w){re.escape(stem)}\w{{0,3}}(?!\w)", text, re.IGNORECASE):
            return p
    return None


FACT_CARD_H = 780  # высота картинки на слайдах фактов и новостей (как CARD_H)


async def _own_for_facts(files: list[Path], facts: list[str], work: Path, gemini_key: str,
                         strict: bool = False, prefix: str = "own") -> tuple[dict[int, Image.Image], dict[int, str]]:
    """Свои фото/видео автора к фактам: ИИ — по смыслу, остальные — по порядку; один файл — один факт.
    strict=True (материал из Telegram-каналов) — только то, что ИИ счёл подходящим, без раздачи по порядку.

    Возвращает ({факт: картинка}, {факт: файл клипа}); у видео картинка — его кадр (запасной).
    """
    items: list[tuple[Image.Image, str | None]] = []
    for n, path in enumerate(files):
        try:
            if path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                items.append((Image.open(path).convert("RGB"), None))
                continue
            dur = await _duration(path)
            clip = work / f"{prefix}{n}.mp4"
            await clips.cut_clip(path, clip, max(0.0, dur / 2 - CLIP_SECONDS / 2) if dur > CLIP_SECONDS else 0.0,
                                 min(CLIP_SECONDS, dur))
            still = work / f"{prefix}{n}.jpg"
            await _run("ffmpeg", "-v", "error", "-y", "-ss", "1", "-i", str(clip), "-frames:v", "1", str(still))
            items.append((Image.open(still).convert("RGB"), clip.name))
        except Exception as exc:  # noqa: BLE001 — битый файл пропускаем
            log.warning("shorts: own material %s skipped: %s", path.name, exc)
    if not items:
        return {}, {}
    ranked = await ai.rank_image_candidates(gemini_key, facts, [im for im, _ in items]) if gemini_key else None
    used: set[int] = set()
    at: dict[int, int] = {}
    for i, item in enumerate(ranked or []):
        c = int(item.get("index", -1))
        if c >= 0 and c not in used:
            at[i] = c
            used.add(c)
    free = [] if strict else [i for i in range(len(facts)) if i not in at]
    for c in range(len(items)):
        if c not in used and free:
            at[free.pop(0)] = c
            used.add(c)
    own: dict[int, Image.Image] = {}
    videos: dict[int, str] = {}
    for i, c in at.items():
        img, clip = items[c]
        own[i] = img if img.width >= img.height * 1.2 else _portrait(img)  # вертикальное — целиком на фоне
        if clip:
            videos[i] = clip
    return own, videos


def _portrait(img: Image.Image, w: int = 1080, h: int = FACT_CARD_H) -> Image.Image:
    """Портрет целиком по центру на размытом фоне из него же (иначе обрезка срежет лицо)."""
    from PIL import ImageFilter
    s = max(w / img.width, h / img.height)
    bg = img.resize((round(img.width * s), round(img.height * s)))
    bg = bg.crop(((bg.width - w) // 2, (bg.height - h) // 2, (bg.width - w) // 2 + w, (bg.height - h) // 2 + h))
    bg = Image.blend(bg.filter(ImageFilter.GaussianBlur(30)), Image.new("RGB", (w, h)), 0.45)
    fg = img.copy()
    fg.thumbnail((w, h - 40))
    bg.paste(fg, ((w - fg.width) // 2, (h - fg.height) // 2 + 20))
    return bg


async def make_facts_short(fetch: Fetch, work: Path, query: str = "", exclude: set[int] | None = None,
                           music: Path | None = None, tmdb_key: str = "",
                           gemini_key: str = "", trailer_clips: bool = True,
                           custom_facts: list[str] | None = None,
                           user_media: list[Path] | None = None, rumors: bool = False, review: bool = False,
                           display_title: str = "",
                           tg_media: list[dict[str, Any]] | None = None, era: str = "") -> ShortResult:
    """era — для случайного фильма: "new" (с 2015 года), "old" (до 2015), "" — любой.
    custom_facts — факты автора (кнопка «Факты: свой текст»): фильм ищем по query, факты не меняем.
    user_media — свои фото и видео автора: идут к фактам первыми.
    rumors=True — фильм ещё не вышел: ролик «Что известно о …» из новостей и слухов.
    review=True — короткий обзор: custom_facts — тезисы обзора автора, ролик «Стоит ли смотреть?».
    display_title — название в ролике (голос, обложка, SEO), если на Кинопоиске фильм называется иначе.
    tg_media — посты Telegram-каналов с фото и видео о фильме: ИИ ставит их к фактам, к которым они подходят."""
    await _progress(5, "Ищу фильм и факты")
    if custom_facts:
        film, note = await find_film_smart(fetch, query, gemini_key, upcoming=rumors)
        if not film:
            raise RuntimeError(f"Кинопоиск не нашёл «{query}»")
        if note and not display_title:  # нашёлся под другим названием — в ролике оставляем название автора
            display_title = display_name(query)
        facts, web_note = list(custom_facts), ""
    else:
        if query and not query.startswith("id:") and ai.available(gemini_key):
            # название с ошибкой или неофициальное — ИИ узнаёт настоящее в интернете
            found, _ = await find_film_smart(fetch, query, gemini_key)
            query = f"id:{found['id']}" if found else query
        film, kp_facts = await pick_fact_film(fetch, query, exclude or set(), web=ai.available(gemini_key), era=era)
        # ещё не вышел — вместо фактов «Что известно о …» из новостей и слухов (если есть ИИ)
        rumors = rumors or (ai.available(gemini_key) and is_upcoming(film))
        tg_media = []
        facts, web_note = await gather_facts(film, kp_facts, gemini_key, media_out=tg_media, rumors=rumors)
    film = renamed(film, display_title)
    await _progress(10, f"«{_title(film)}»: качаю кадры")
    # награды из Викиданных — первым фактом (в фактах автора — только его текст)
    award_fact = None if custom_facts else wiki.awards_fact(_title(film), await wiki.awards(film))
    if award_fact:
        facts = [award_fact] + facts[: FACTS - 1]
    t, n, kind = _title(film), len(facts), _kind(film)
    if review:
        label, card_label, hook = "ОБЗОР", "", f"«{t}» — стоит ли смотреть?"
        intro, cover_text = f"{hook} Короткий обзор!", f"«{t}»: стоит ли смотреть?"
        outro_line, outro = "А ты посмотришь? Пиши в комментариях!", "А ТЫ\nПОСМОТРИШЬ?\nПИШИ В КОММЕНТАРИЯХ"
    elif rumors:
        label, card_label, hook = "СЛУХИ", "НОВОСТЬ", f"Что известно о {kind} «{t}»"
        intro, cover_text = f"{hook}? Главные новости и слухи!", f"{hook}: новости и слухи"
        outro_line, outro = "Ждёшь премьеру? Пиши в комментариях!", "ЖДЁШЬ\nПРЕМЬЕРУ?\nПИШИ В КОММЕНТАРИЯХ"
    else:
        label, card_label, hook = "ФАКТЫ", "ФАКТ", f"{n} {_facts_word(n)} о {kind} «{t}»"
        intro, cover_text = f"{hook}, которые ты точно не знал!", f"{hook}, которые ты не знал"
        outro_line, outro = "Какой факт удивил? Пиши в комментариях!", "КАКОЙ ФАКТ\nУДИВИЛ БОЛЬШЕ?\nПИШИ В КОММЕНТАРИЯХ"
    theme = Theme("review" if review else "rumors" if rumors else "facts", label, t.upper(), hook, hook, [])

    cards = []
    # пул кадров: без надписей с TMDB, затем кадры/фон/постер с Кинопоиска
    backdrops, _ = await tmdb.images(tmdb_key, film, limit=12)
    urls = tmdb.pick_varied(backdrops, 12) + await _stills(fetch, film)
    photos = await tmdb.people(tmdb_key, film)
    pool_size = 12 if ai.available(gemini_key) else n
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # у свежих фильмов на Кинопоиске может не быть постера — тогда кадр TMDB
        cover_src = await _download_first(session, [_poster_url(film)] + backdrops[:2])
        images: list[Image.Image] = []
        for url in urls:
            if len(images) >= pool_size:
                break
            try:
                images.append(await _download(session, url))
            except Exception:  # noqa: BLE001
                continue
        # свои фото и видео автора — в первую очередь, к фактам, о которых они
        own: dict[int, Image.Image] = {}
        own_videos: dict[int, str] = {}
        if user_media:
            await _progress(14, "Разбираю ваши фото и видео")
            own, own_videos = await _own_for_facts(user_media, facts, work, gemini_key)
        # фото и видео из постов Telegram-каналов — к фактам, которым они подходят по смыслу
        rest_tg = [i for i in range(len(facts)) if i not in own]
        if tg_media and rest_tg and ai.available(gemini_key):
            await _progress(16, "Фото и видео из Telegram-каналов")
            try:
                files = await tgsearch.download_media(tg_media, work)
                tg_own, tg_videos = await _own_for_facts(files, [facts[i] for i in rest_tg], work, gemini_key,
                                                         strict=True, prefix="tgc")
                own.update({rest_tg[k]: img for k, img in tg_own.items()})
                own_videos.update({rest_tg[k]: clip for k, clip in tg_videos.items()})
            except Exception as exc:  # noqa: BLE001 — без материала из каналов ролик всё равно соберём
                log.warning("shorts: tg media for facts failed: %s", exc)
        # факт про актёра/режиссёра — его фото (TMDB, запасной — Кинопоиск)
        portraits: dict[int, Image.Image] = {}
        for i, fact in enumerate(facts):
            if i in own:
                continue
            person = _mentioned_person(fact, film)
            if not person:
                continue
            urls_p = [photos.get(str(person.get("enName") or "").lower()), person.get("photo")]
            try:
                portraits[i] = _portrait(await _download_first(session, urls_p))
            except Exception:  # noqa: BLE001
                continue
    rest = [i for i in range(n) if i not in portraits and i not in own]
    if not images and rest:
        raise RuntimeError(f"Не скачались картинки для «{t}»")

    ai.last_error = None
    # факты без фото — сначала момент из трейлера, который Gemini счёл подходящим
    videos: dict[int, str] = dict(own_videos)
    video_note = ""
    if trailer_clips and rest:
        await _progress(18, "Ищу моменты в трейлере")
        if ai.available(gemini_key):
            found, video_note = await _fact_clips(film, [(i, facts[i]) for i in rest], work, gemini_key)
            videos.update(found)
        else:
            video_note = "нет ключа GEMINI_API_KEY или RESERVE_AI_KEY — без ИИ моменты не подобрать"
    # остальные — кадр, который Gemini выбрал по смыслу; без ИИ — по очереди
    stills = [i for i in rest if i not in videos]
    await _progress(32, "ИИ подбирает кадры")
    ranked = await ai.rank_image_candidates(gemini_key, [facts[i] for i in stills], images) if stills else None
    used: set[int] = set()
    picked: dict[int, Image.Image] = {**portraits, **own}
    visual_matches: dict[int, dict[str, Any]] = {}
    visual_sources: dict[int, str] = {i: "user_or_channel_media" for i in own}
    visual_sources.update({i: "person_portrait" for i in portraits})
    for k, i in enumerate(stills):
        match = ranked[k] if ranked and k < len(ranked) else {"index": -1, "score": 0, "reason": ""}
        c = int(match.get("index", -1))
        score = int(match.get("score", 0))
        if c < 0 or c >= len(images) or c in used or score < 70:
            # Не подставляем случайный кадр только ради заполнения сцены.
            # Нейтральный постер фильма лучше, чем визуал, противоречащий озвучке.
            picked[i] = cover_src.copy()
            visual_matches[i] = {
                "status": "fallback_unverified", "score": score or None,
                "reason": "Нет кадра с подтверждённым соответствием этой фразе; использован постер фильма",
            }
            visual_sources[i] = "film_poster_fallback"
            continue
        used.add(c)
        picked[i] = images[c]
        visual_matches[i] = {
            "status": "matched", "score": score,
            "reason": str(match.get("reason") or "")[:160],
        }
        visual_sources[i] = "film_frame_pool"

    for i in videos:
        if i in own:
            continue
        # Сам видеоклип — основной материал; постер используется только как запасной стоп-кадр.
        picked[i] = cover_src.copy()
        visual_matches[i] = {
            "status": "trailer_clip_review", "score": None,
            "reason": "Проверьте, что выбранный фрагмент трейлера действительно иллюстрирует фразу",
        }
        visual_sources[i] = "trailer_clip"

    # Если ни один исходный кадр не подошёл, создаём отдельную символическую иллюстрацию.
    # Генерация ограничена только фактами без подтверждённого кадра; чужие лица/постеры не имитируем.
    if not review and not rumors and ai.available(gemini_key):
        missing = [i for i, meta in visual_matches.items() if meta.get("status") == "fallback_unverified"]
        # Ограничиваем число платных генераций на один ролик; остальные кадры останутся на ручную замену.
        for i in missing[:5]:
            try:
                await _progress(34, f"Создаю иллюстрацию к факту {i + 1}/{n}")
                raw_image = await ai.generate_illustration(gemini_key, facts[i], _title(film))
                if not raw_image:
                    continue
                illustration = Image.open(io.BytesIO(raw_image)).convert("RGB")
                if illustration.width < 32 or illustration.height < 32:
                    continue
                picked[i] = illustration if illustration.width >= illustration.height * 1.2 else _portrait(illustration)
                visual_sources[i] = "ai_generated_illustration"
                visual_matches[i] = {
                    "status": "generated_illustration_review", "score": None,
                    "reason": "Создана оригинальная символическая иллюстрация; это не кадр из фильма",
                }
            except Exception as exc:  # noqa: BLE001 — генерация не должна ломать сборку ролика
                log.warning("shorts: illustration for fact %s failed: %s", i + 1, exc)

    for i, fact in enumerate(facts, 1):
        # название уже в заголовке слайда — на карточке не повторяем
        badge = f"{card_label} №{i}" if card_label else None  # в обзоре — без плашки, только n/N
        card = await asyncio.to_thread(make_card, picked[i - 1], "", i, n,
                                       None, "IMDb", None, badge, FACT_CARD_H)
        path = work / f"card{i}.jpg"
        card.save(path, quality=93)
        video: dict[str, str] = {}
        if (i - 1) in videos:
            card_overlay("", i, n, None, "IMDb", None, badge, FACT_CARD_H).save(work / f"overlay{i}.png")
            video = {"clip": videos[i - 1], "overlay": f"overlay{i}.png"}
        match_meta = visual_matches.get(i - 1, {
            "status": "special_source_unverified", "score": None,
            "reason": "Портрет, пользовательский или видеоматериал требует проверки источника и соответствия",
        })
        cards.append({"image": path.name, "caption": fact, "karaoke": True,
                      "visual_match": match_meta, "asset_source": visual_sources.get(i - 1, "unknown"), **video})

    cover = await asyncio.to_thread(make_cover, cover_src, label, cover_text)
    cover_path = work / "cover.jpg"
    cover.save(cover_path, quality=93)

    lines = [intro] + facts + [f"{outro_line} {SAVE_AND_SUBSCRIBE}"]
    voiced = await _render(work, cover_path, cards, lines, label, t.upper(), outro, music, scene_plan=not review and not rumors)
    seo = build_review_seo(film, facts) if review else build_rumors_seo(film, facts) if rumors \
        else build_facts_seo(film, facts)
    result = ShortResult(work / "short.mp4", cover_path, theme, [film], seo)
    notes = [web_note] if web_note else []
    if video_note:
        notes.append(f"🎞 Видео из трейлера не вставлено: {video_note}.")
    if ai.last_error:
        notes.append(f"⚠️ ИИ не помог подобрать кадры: {ai.last_error}")
    if not voiced or tts_note:
        notes.append(no_voice_note(voiced))
    if notes:
        result.seo["warning"] = "\n".join(notes)
    return result


# ---------------------------------------------------------------- своя подборка (тема и список от автора)

CUSTOM_MIN, CUSTOM_MAX = 3, 10


UPCOMING_STATUS = ("announced", "pre-production", "filming", "post-production")


def is_upcoming(item: dict[str, Any]) -> bool:
    """Ещё не вышел: на Кинопоиске «в производстве», год впереди или премьера позже сегодняшнего дня."""
    if str(item.get("status") or "") in UPCOMING_STATUS:
        return True
    today = date.today()
    if int(item.get("year") or 0) > today.year:
        return True
    dates = [d for d in (_pdate(item, k) for k in ("world", "russia", "digital")) if d]
    return bool(dates) and min(dates) > today


def _same_name(name: str, film: dict[str, Any]) -> bool:
    """Название совпадает с одним из названий фильма (с учётом мелких отличий написания): «Черные голуби» ≠
    «Черные паруса», «Что, если любовь» ≠ «Что, если...?»."""
    from .tmdb import similar
    titles = [film.get("name"), film.get("alternativeName"), film.get("enName")]
    titles += [x.get("name") for x in film.get("names") or [] if isinstance(x, dict)]
    return any(similar(name, str(t)) >= 0.85 for t in titles if t)


async def find_film(fetch: Fetch, query: str, upcoming: bool = False, exact: bool = False) -> dict[str, Any] | None:
    """Фильм по «Название» или «Название 2018»: самый известный из найденных (полная карточка).
    upcoming=True — сначала те, что выходят в этом году или позже (новый сериал, а не старый фильм).
    Слово «сериал» или «фильм» в запросе — уточнение: ищем без него, но такой тип — первым."""
    query = query.strip()
    kind_m = re.search(r"(?<!\w)(мультсериал|сериал|мультфильм|фильм)(?!\w)", query, re.I)
    want_series = bool(kind_m) and "сериал" in kind_m.group(1).lower()
    if kind_m:
        query = re.sub(r"\s+", " ", query[: kind_m.start()] + query[kind_m.end():]).strip(" ,.-—")
    if query.startswith("id:") and query[3:].isdigit():
        return await fetch(f"/movie/{int(query[3:])}")
    year_m = re.search(r"[\s(]+(19\d\d|20\d\d)\)?$", query)
    year = int(year_m.group(1)) if year_m else None
    name = query[: year_m.start()].strip() if year_m else query
    data = await fetch("/movie/search", query=name, page=1, limit=10)
    cands = [x for x in data.get("docs") or [] if isinstance(x, dict) and x.get("id")]
    if exact:  # только с тем же названием: самый известный из найденных бывает совсем другим фильмом
        cands = [c for c in cands if _same_name(name, c)]
    if year:
        cands = [c for c in cands if c.get("year") in (year - 1, year, year + 1)] or cands
    if not cands:
        return None
    this_year = date.today().year

    def series(c: dict[str, Any]) -> bool:
        return bool(c.get("isSeries")) or "series" in str(c.get("type") or "")

    cands.sort(key=lambda c: (c.get("year") != year if year else False,
                              bool(kind_m) and series(c) != want_series,
                              upcoming and int(c.get("year") or 0) < this_year,
                              -int((c.get("votes") or {}).get("kp") or 0)))
    return await fetch(f"/movie/{cands[0]['id']}")


def display_name(query: str) -> str:
    """Название, как его ввёл автор, для ролика: без года и слов «фильм»/«сериал», с заглавной буквы."""
    name = re.sub(r"(?<!\w)(мультсериал|сериал|мультфильм|фильм)(?!\w)", "", query, flags=re.I)
    name = re.sub(r"[\s(]+(19\d\d|20\d\d)\)?\s*$", "", name).strip(" ,.-—«»\"")
    return name[:1].upper() + name[1:]


def renamed(film: dict[str, Any], name: str) -> dict[str, Any]:
    """Фильм под названием, которое знают зрители («Глиноликий» вместо «Клейфейс» с Кинопоиска)."""
    return {**film, "name": name} if name else film


async def find_film_smart(fetch: Fetch, query: str, gemini_key: str = "",
                          upcoming: bool = False, exact: bool = False) -> tuple[dict[str, Any] | None, str]:
    """find_film, а если Кинопоиск не нашёл — ИИ по интернету и Telegram-каналам узнаёт, как фильм называется
    официально («Глиноликий» → «Клейфейс» / Clayface 2026), и ищем снова. (фильм | None, заметка автору)."""
    film = await find_film(fetch, query, upcoming, exact)
    if film or not ai.available(gemini_key) or query.startswith("id:"):
        return film, ""
    name = re.sub(r"[\s(]+(19\d\d|20\d\d)\)?$", "", query).strip()
    posts = await tgsearch.search(fact_channels, [name], per_channel=2)
    for guess in await ai.identify_film(gemini_key, query, tgsearch.as_text(posts, 6000)):
        y = f" {guess['year']}" if guess["year"] else ""
        for q in dict.fromkeys(x for x in (guess["ru"] + y, guess["en"] + y, guess["ru"], guess["en"]) if x.strip()):
            try:
                film = await find_film(fetch, q, upcoming, exact)
            except Exception as exc:  # noqa: BLE001
                log.warning("shorts: film lookup %r failed: %s", q, exc)
                continue
            if film:
                label = guess["ru"] or guess["en"]
                return film, f"🔎 «{query}» на Кинопоиске нет — ИИ нашёл в интернете: «{label}»" + (
                    f" ({guess['en']}, {guess['year']})" if guess["en"] and guess["year"] else "")
    return None, ""


def custom_theme(title: str, n: int) -> Theme:
    title = re.sub(r"\s+", " ", title).strip(" .!") or "Подборка фильмов"
    header = title.upper() if len(title) <= 34 else title.upper()[:33].rsplit(" ", 1)[0] + "…"
    return Theme("custom", "ПОДБОРКА", header, title, f"{title} 🎬",
                 [title.lower(), "подборка фильмов", "что посмотреть", "топ фильмов", "фильмы на вечер"])


async def make_custom_short(fetch: Fetch, work: Path, title: str, film_ids: list[int], music: Path | None = None,
                            tmdb_key: str = "", trailer_clips: bool = True,
                            notes: list[str] | None = None, user_media: list[Path] | None = None,
                            gemini_key: str = "") -> ShortResult:
    """Своя подборка: тема и фильмы — от автора (или из чужого ролика), порядок — как в списке.
    notes — фраза для озвучки к каждому фильму (из описаний автора) вместо «Жанр.».
    user_media — свои фото и видео автора: ИИ ставит каждый к фильму, о котором он."""
    work.mkdir(parents=True, exist_ok=True)
    if not CUSTOM_MIN <= len(film_ids) <= CUSTOM_MAX:
        raise RuntimeError(f"в подборке должно быть от {CUSTOM_MIN} до {CUSTOM_MAX} фильмов")
    await _progress(5, "Загружаю фильмы")
    films = []
    for i in film_ids:  # фильм с TMDB («tmdb:movie:123») — его нет на Кинопоиске
        film = (await tmdb.kp_like_by_id(tmdb_key, str(i)) if str(i).startswith(tmdb.TMDB_PREFIX)
                else await fetch(f"/movie/{i}"))
        if film:
            films.append(film)
    if len(films) < CUSTOM_MIN:
        raise RuntimeError("не загрузились карточки фильмов — попробуй ещё раз")
    result = await _list_short(work, custom_theme(title, len(films)), films, music, tmdb_key, trailer_clips,
                               notes, user_media, gemini_key)
    add_english_seo(result)
    return result


# ---------------------------------------------------------------- день рождения

BIRTHDAY_FILMS = 7  # автору у Макконахи пяти фильмов было мало


def _years(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "год"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "года"
    return "лет"


def person_uid(imdb: str) -> int:
    """Отрицательный id для истории роликов — не пересекается с фильмами Кинопоиска и новостями."""
    return -(10**12 + int(re.sub(r"\D", "", imdb) or 0))


async def pick_birthday(today: date, exclude: set[int], tmdb_key: str,
                        pick: str = "") -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """(человек из Викиданных, профиль TMDB, лучшие фильмы) — самый известный именинник с фильмами; pick —
    IMDb ID того, кого выбрал автор из списка (тогда только он, даже если о нём уже был ролик)."""
    people = await wiki.birthdays(today.month, today.day, limit=20 if pick else 10)
    if pick:
        people = [p for p in people if p["imdb"] == pick]
    if not people:
        raise RuntimeError("Сегодня в Викиданных нет известных именинников из кино" if not pick
                           else "этого человека нет среди сегодняшних именинников — открой список заново")
    for who in people:
        if not pick and person_uid(who["imdb"]) in exclude:
            continue
        try:
            info = await tmdb.person(tmdb_key, who["imdb"])
        except Exception as exc:  # noqa: BLE001
            log.info("shorts: TMDB person %s failed: %s", who["imdb"], exc)
            continue
        if not info:
            continue
        films = tmdb.best_movies(info, BIRTHDAY_FILMS)
        if len(films) >= 3:
            return who, info, films
    raise RuntimeError("Не нашлось именинника с фильмами на TMDB (нужен TMDB_API_KEY)" if not pick
                       else "у этого человека на TMDB меньше трёх фильмов — выбери другого")


def build_birthday_seo(name: str, en: str, age: int, films: list[dict[str, Any]], female: bool) -> dict[str, str]:
    title = f"{name} — {age} {_years(age)}! Лучшие фильмы 🎂 #shorts"
    if len(title) > 100:
        title = f"{name}: лучшие фильмы 🎂 #shorts"[:100]
    names = [str(f.get("title") or f.get("original_title")) for f in films]
    description = "\n".join([
        f"🎂 Сегодня день рождения: {name}" + (f" ({en})" if en and en != name else "")
        + f" — {age} {_years(age)}!", "",
        "Лучшие фильмы:",
        *[f"{len(films) - i}. {t}" + (f" ({str(f.get('release_date'))[:4]})" if f.get("release_date") else "")
          for i, (t, f) in enumerate(zip(names, films))], "",
        "А какой фильм твой любимый? Пиши в комментариях 👇", "",
        f"✈️ Telegram: {TELEGRAM_URL} — новинки, трейлеры и подборки каждый день", "",
        "#деньрождения #кино #KinoMir360",
    ])
    tags: list[str] = []
    for tag in [name.lower(), en.lower(), f"{name.lower()} фильмы", f"лучшие фильмы {name.lower()}",
                f"{name.lower()} день рождения", "день рождения звезды", "лучшие фильмы"] + \
            [t.lower() for t in names] + ["KinoMir360", "киномир360", "шортс кино"]:
        if tag and tag not in tags and len(", ".join(tags + [tag])) <= 480:
            tags.append(tag)
    pinned = (f"А какой фильм с {'ней' if female else 'ним'} твой любимый? 👇\n"
              f"Ещё больше подборок — в нашем Telegram 👉 {TELEGRAM_URL}")
    return {"title": title, "description": description, "tags": ", ".join(tags), "pinned": pinned}


def _latin(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", text))


async def _kp_role(fetch: Fetch | None, film: dict[str, Any], who: dict[str, Any]) -> str:
    """Роль по-русски с Кинопоиска (на TMDB роли по-английски, и голос читает их ужасно)."""
    if fetch is None:
        return ""
    title = str(film.get("title") or film.get("original_title") or "")
    year = int(str(film.get("release_date") or "0")[:4] or 0)
    try:
        data = await fetch("/movie/search", query=title, page=1, limit=5)
        doc = next((d for d in data.get("docs") or []
                    if d.get("id") and (not year or abs(int(d.get("year") or 0) - year) <= 1)), None)
        if not doc:
            return ""
        full = await fetch(f"/movie/{doc['id']}")
    except Exception as exc:  # noqa: BLE001 — без роли карточка всё равно собирается
        log.info("shorts: KP role for %s failed: %s", title, exc)
        return ""
    names = {who["name"].lower(), who["en"].lower()} - {""}
    for p in full.get("persons") or []:
        if str(p.get("name") or "").lower() in names or str(p.get("enName") or "").lower() in names:
            role = str(p.get("description") or "").strip()
            if role and not _latin(role):
                return role
    return ""


async def make_birthday_short(work: Path, today: date, exclude: set[int] | None = None,
                              music: Path | None = None, tmdb_key: str = "",
                              trailer_clips: bool = True, fetch: Fetch | None = None, pick: str = "") -> ShortResult:
    await _progress(5, "Ищу именинника")
    who, info, top = await pick_birthday(today, exclude or set(), tmdb_key, pick)
    await _progress(12, f"{who['name']}: качаю кадры и трейлеры")
    name, female = who["name"], who["female"]
    born = date.fromisoformat(who["born"])
    age = today.year - born.year
    he = "ей" if female else "ему"
    # обратный отсчёт: самый известный фильм — последним
    films = top[::-1]
    n = len(films)

    cards = []
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        photo = await _download_first(session, tmdb.profile_urls(info))
        for i, f in enumerate(films, 1):
            title = str(f.get("title") or f.get("original_title") or "")
            await _progress(12 + 25 * (i - 1) // n, f"Фильм {i} из {n}: {title}")
            year = str(f.get("release_date") or "")[:4]
            img = await _download_first(session, [tmdb.still_url(f)])
            rating = float(f.get("vote_average") or 0)
            rating_shown = rating if rating >= MIN_RATING else None
            card = await asyncio.to_thread(make_card, img, title, i, n, rating_shown, "TMDB", None, None,
                                           place=n - i + 1)
            path = work / f"card{i}.jpg"
            card.save(path, quality=93)
            video: dict[str, str] = {}
            item = {"name": title, "alternativeName": f.get("original_title"), "year": int(year) if year else None}
            if trailer_clips and await _trailer_clip(item, work / f"clip{i}.mp4"):
                card_overlay(title, i, n, rating_shown, "TMDB", None, None, place=n - i + 1).save(work / f"overlay{i}.png")
                video = {"clip": f"clip{i}.mp4", "overlay": f"overlay{i}.png"}
            # роль по-русски с Кинопоиска; английскую с TMDB не показываем и не озвучиваем
            role = await _kp_role(fetch, f, who) if f.get("job") != "Director" else ""
            if not role:
                tmdb_role = re.sub(r"\s*\((voice|uncredited)\)", "", str(f.get("character") or ""), flags=re.I).strip()
                role = tmdb_role  # английскую роль ниже переведёт ИИ — без неё зритель не понял, кем он был
            f["_role"] = role
            f["_voice"] = tmdb.is_voice(f)
            rows = [[("Роль", role)]] if role else [[("Режиссёр", name)]] if f.get("job") == "Director" else []
            cards.append({"image": path.name, "caption": " • ".join(x for x in [name, year] if x),
                          "info": rows, **video})

    # роли латиницей («Buster Moon») — по-русски, как в дубляже; не перевелось — без роли, а не по-английски
    latin = [k for k, f in enumerate(films) if f.get("_role") and _latin(f["_role"])]
    if latin:
        ru = await ai.translate_roles([(str(films[k].get("title") or ""), films[k]["_role"]) for k in latin])
        for k, new in zip(latin, ru or [""] * len(latin)):
            films[k]["_role"] = new if new and not _latin(new) else ""
            cards[k]["info"] = [[("Роль", films[k]["_role"])]] if films[k]["_role"] else []
    portrait = _portrait(photo, 1080, 1350)
    cover = await asyncio.to_thread(make_cover, portrait, "ДЕНЬ РОЖДЕНИЯ", f"{name}\n{age} {_years(age)}!")
    cover_path = work / "cover.jpg"
    cover.save(cover_path, quality=93)

    def say(f: dict[str, Any], place: int) -> str:
        title = f"«{f.get('title') or f.get('original_title')}»"
        head = f"И номер один — {title}!" if place == 1 else f"Номер {NUMBERS.get(place, place)} — {title}."
        role = f.get("_role") or ""
        if not role or len(role) >= 40:
            return head
        if f.get("_voice"):
            return f"{head} {'Она озвучила' if female else 'Он озвучил'} — {role}."
        return f"{head} Роль — {role}."

    lines = ([f"Сегодня день рождения празднует {name}! {he.capitalize()} {age} {_years(age)}. Лучшие фильмы:"]
             + [say(f, n - i) for i, f in enumerate(films)]
             + [f"А какой фильм с {'ней' if female else 'ним'} твой любимый? Пиши в комментариях! {SAVE_AND_SUBSCRIBE}"])
    header = name.upper()
    voiced = await _render(work, cover_path, cards, lines, "ДЕНЬ РОЖДЕНИЯ", header,
                           "КАКОЙ ФИЛЬМ\nТВОЙ ЛЮБИМЫЙ?\nПИШИ В КОММЕНТАРИЯХ", music, boom_slide=n)

    hook = f"{name} — {age} {_years(age)}"
    theme = Theme("birthday", "ДЕНЬ РОЖДЕНИЯ", header, hook, hook, [])
    seo = build_birthday_seo(name, who["en"], age, films, female)
    result = ShortResult(work / "short.mp4", cover_path, theme,
                         [{"id": person_uid(who["imdb"]), "name": name, "en": who["en"]}], seo)
    if not voiced or tts_note:
        result.seo["warning"] = no_voice_note(voiced)
    return result


async def make_daily_short(fetch: Fetch, work: Path, theme_key: str | None = None,
                           exclude: set[int] | None = None, music: Path | None = None,
                           today: date | None = None, tmdb_key: str = "",
                           news_feeds: list[str] | None = None, manual: bool = False,
                           gemini_key: str = "", trailer_clips: bool = True) -> ShortResult:
    """fetch(path, **params) -> dict — запрос к API Кинопоиска (PoiskKino._get)."""
    result = await _make_short(fetch, work, theme_key, exclude, music, today, tmdb_key, news_feeds, manual,
                               gemini_key, trailer_clips)
    add_english_seo(result)
    return result


async def _make_short(fetch: Fetch, work: Path, theme_key: str | None = None,
                      exclude: set[int] | None = None, music: Path | None = None,
                      today: date | None = None, tmdb_key: str = "",
                      news_feeds: list[str] | None = None, manual: bool = False,
                      gemini_key: str = "", trailer_clips: bool = True) -> ShortResult:
    today = today or date.today()
    work.mkdir(parents=True, exist_ok=True)
    theme_key = theme_key or SCHEDULE[today.weekday()]
    if theme_key.startswith("birthday"):  # «birthday:nm0000123» — именинник, выбранный автором из списка
        pick = theme_key.partition(":")[2].strip()
        theme_key = "birthday"
        try:
            return await make_birthday_short(work, today, exclude, music, tmdb_key, trailer_clips, fetch, pick)
        except Exception:
            if manual:
                raise
            log.exception("shorts: birthday failed, falling back to genre")
            theme_key = "genre"
    if theme_key == "news":
        try:
            return await make_news_short(fetch, work, news_feeds or news.DEFAULT_FEEDS, exclude, music,
                                         tmdb_key, today, gemini_key)
        except RuntimeError:
            if manual:
                raise
            log.exception("shorts: news failed, falling back to genre")
            theme_key = "genre"
    if theme_key.startswith("facts"):
        query = theme_key.partition(":")[2].strip()
        era = {"facts_new": "new", "facts_old": "old"}.get(theme_key, "")
        try:
            return await make_facts_short(fetch, work, query, exclude, music, tmdb_key, gemini_key,
                                          trailer_clips, era=era)
        except RuntimeError:
            if query:
                raise
            log.exception("shorts: facts failed, falling back to genre")
            theme_key = "genre"
    theme = theme_for(theme_key, today)
    # в текстах зашито «5 ...», поэтому фильмов должно быть ровно FILMS;
    # если тема их не набрала — берём жанровую подборку
    await _progress(5, "Подбираю фильмы")
    films = await pick_films(fetch, theme, today, exclude or set())
    for _ in range(3):
        if len(films) >= FILMS:
            break
        log.info("shorts: theme %s gave %d films, falling back to genre", theme.key, len(films))
        theme = theme_for("genre", today)
        films = await pick_films(fetch, theme, today, exclude or set())
    if len(films) < FILMS:
        raise RuntimeError(f"Не набралось {FILMS} фильмов для подборки (тема {theme.key})")
    # обратный отсчёт: показываем с №5 до №1, лучший — последним (ожидаемые — по порядку ожидания)
    ranked = films if theme.upcoming else sorted(films, key=_rank_key, reverse=True)
    return await _list_short(work, theme, ranked[::-1], music, tmdb_key, trailer_clips)


async def _list_short(work: Path, theme: Theme, films: list[dict[str, Any]], music: Path | None,
                      tmdb_key: str, trailer_clips: bool, notes: list[str] | None = None,
                      user_media: list[Path] | None = None, gemini_key: str = "") -> ShortResult:
    """Подборка-отсчёт по готовому списку: films в порядке показа (последний — №1).
    notes — своя фраза к каждому фильму (из описаний автора); user_media — свои фото/видео автора."""
    n = len(films)
    cards = []
    own: dict[int, Image.Image] = {}
    own_videos: dict[int, str] = {}
    if user_media:
        await _progress(8, "Разбираю ваши фото и видео")
        texts = [f"«{_title(f)}». " + (notes[i] if notes and i < len(notes) else ", ".join(_genres(f)[:2]))
                 for i, f in enumerate(films)]
        own, own_videos = await _own_for_facts(user_media, texts, work, gemini_key, prefix="ownl")
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        posters_tmdb: list[str] = []
        for i, f in enumerate(films, 1):
            await _progress(10 + 28 * (i - 1) // n, f"Фильм {i} из {n}: {_title(f)}")
            # кадр без надписей с TMDB, если нет — фон/постер Кинопоиска
            backdrops, posters = await tmdb.images(tmdb_key, f, limit=1)
            if i == n:
                posters_tmdb = posters
            # свой кадр автора — вместо кадра с TMDB/Кинопоиска
            img = own[i - 1] if (i - 1) in own else await _download_first(session, backdrops + [_image_url(f)])
            rating, src = _rating(f)
            badge = None if rating else "НОВИНКА"
            if theme.upcoming:
                prem = _premiere(f)
                rating, badge = None, (f"С {_date_ru(prem).upper()}" if prem else "СКОРО")
            card = await asyncio.to_thread(make_card, img, _title(f), i, n, rating, src or "IMDb",
                                           _actors(f), badge, place=n - i + 1)
            path = work / f"card{i}.jpg"
            card.save(path, quality=93)
            video: dict[str, str] = {}
            clip = own_videos.get(i - 1)  # своё видео автора — вместо трейлера
            if not clip and (i - 1) not in own and trailer_clips and await _trailer_clip(f, work / f"clip{i}.mp4"):
                clip = f"clip{i}.mp4"
            if clip:
                overlay = card_overlay(_title(f), i, n, rating, src or "IMDb", _actors(f), badge, place=n - i + 1)
                overlay.save(work / f"overlay{i}.png")
                video = {"clip": clip, "overlay": f"overlay{i}.png"}
            # у сериала с новым сезоном под карточкой — «4-й сезон», а не год старта (2022)
            season = re.search(r"(\d{1,2})[-‑]?(?:й|ый|ий)?\s+сезон", notes[i - 1] if notes and i <= len(notes) else "")
            caption = " • ".join(x for x in [", ".join(_genres(f)[:2]).capitalize(),
                                             f"{season.group(1)}-й сезон" if season else str(f.get("year") or "")] if x)
            rows = info_rows(f)
            f["_awards"] = wiki.awards_line(await wiki.awards(f))
            if f["_awards"]:
                rows.append([("Награды", f["_awards"])])
            cards.append({"image": path.name, "caption": caption, "info": rows, **video})
        # обложка — фильм №1: русский постер Кинопоиска, запасной — постер TMDB
        cover_src = await _download_first(session, [_poster_url(films[-1])] + posters_tmdb)

    headline = theme.hook.replace(", ", ",\n", 1)
    cover = await asyncio.to_thread(make_cover, cover_src, theme.tag, headline)
    cover_path = work / "cover.jpg"
    cover.save(cover_path, quality=93)

    lines = voice_lines(theme, films)
    if notes and len(notes) == n:
        for i, (f, note) in enumerate(zip(films, notes), 1):
            if note:
                place = n - i + 1
                head = "И номер один" if place == 1 else f"Номер {NUMBERS.get(place, place)}"
                lines[i] = f"{head} — «{_title(f)}». {note}"
    voiced = await _render(work, cover_path, cards, lines, theme.tag, theme.header,
                           "КАКОЙ ПОСМОТРИШЬ\nПЕРВЫМ?\nПИШИ В КОММЕНТАРИЯХ", music, boom_slide=n)

    result = ShortResult(work / "short.mp4", cover_path, theme, films, build_seo(theme, films))
    if not voiced or tts_note:
        result.seo["warning"] = no_voice_note(voiced)
    return result

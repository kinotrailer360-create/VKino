from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import io
import json
import logging
import os
import random
import re
import secrets
import shutil
import time
from datetime import date, datetime, timedelta, timezone
from html import escape
from types import SimpleNamespace
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urljoin, urlparse

import aiohttp
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BufferedInputFile,
    FSInputFile,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont, ImageOps

from shorts import ai as shorts_ai
from shorts import budget as shorts_budget
from shorts import migrations as shorts_migrations
from shorts import fsm_pg as shorts_fsm
from shorts import narrator as shorts_narrator
from shorts import yttrailers as shorts_yttrailers
from shorts import tgsearch as shorts_tgsearch
from shorts import postcard as shorts_postcard
from shorts import clips as shorts_clips
from shorts import youtube_upload as shorts_yt
from shorts import linkfetch as shorts_linkfetch
from shorts import netsafe
from shorts import diary as shorts_diary
from shorts import kinodl as shorts_kinodl
from shorts.postmedia import DIGEST_MARKERS, _focused, _is_digest, _mentions_title, _title_words  # noqa: F401
from shorts import usage as shorts_usage
from shorts import news as shorts_news
from shorts import fastnews as shorts_fast
from shorts import hype as shorts_hype
from shorts import teaser as shorts_teaser
from shorts import rt as shorts_rt
from shorts import weekly as shorts_weekly
from shorts import lumean as shorts_lumean
from shorts import music as shorts_music
from shorts import pronounce as shorts_pronounce
from shorts.clips import download_clip as shorts_download_clip
from shorts import daily as shorts_daily
from shorts import direct_stats
from shorts import copycat as shorts_copycat
from shorts import dub as shorts_dub
from shorts import lore as shorts_lore
from shorts import review as shorts_review
from shorts import project as shorts_project
from shorts import plan as shorts_plan
from shorts.daily import say_to_file as shorts_say_to_file
from shorts.daily import make_daily_short

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
KINOPOISK_TOKEN = os.getenv("KINOPOISK_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip().rstrip("/")
RAILWAY_PUBLIC_DOMAIN = os.getenv(
    "RAILWAY_PUBLIC_DOMAIN",
    "",
).strip()
if not WEBAPP_URL and RAILWAY_PUBLIC_DOMAIN:
    WEBAPP_URL = f"https://{RAILWAY_PUBLIC_DOMAIN}".rstrip("/")

VIDEO_PROVIDER_API_URL = os.getenv(
    "VIDEO_PROVIDER_API_URL",
    "",
).strip()
VIDEO_PROVIDER_API_TOKEN = os.getenv(
    "VIDEO_PROVIDER_API_TOKEN",
    "",
).strip()

TMDB_API_KEY = os.getenv("TMDB_API_KEY", "").strip()


_SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|AUTH", re.I)
_SECRET_QS = re.compile(r"((?:api_?key|key|token|access_token|secret|auth)=)[^&\s'\"]+", re.I)


class _RedactSecrets(logging.Filter):
    """Журнал сервера тоже без ключей: aiohttp пишет в ошибки полный адрес запроса (?api_key=…)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            clean = _safe_err(msg)
            if clean != msg:
                record.msg, record.args = clean, ()
            if record.exc_info and record.exc_info[1] is not None:
                text = _safe_err(logging.Formatter().formatException(record.exc_info))
                record.exc_info, record.exc_text = None, text
        except Exception:  # noqa: BLE001 — журнал не должен падать из-за фильтра
            pass
        return True


def _safe_err(exc: BaseException | str) -> str:
    """Текст ошибки для Telegram без ключей: aiohttp кладёт в ошибку полный адрес запроса, а у TMDB и других
    ключ стоит в адресе (?api_key=…). Вырезаем значения всех секретов из .env и параметры-ключи."""
    text = str(exc) or type(exc).__name__ if isinstance(exc, BaseException) else str(exc)
    for name, value in os.environ.items():
        if value and len(value) >= 8 and _SECRET_ENV.search(name):
            text = text.replace(value, "***")
    return _SECRET_QS.sub(r"\1***", text)
# основной ключ Gemini; запасные (GEMINI_API_KEY_2, _3 — с других аккаунтов Google) shorts.ai берёт сам,
# когда у основного кончается лимит
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY", "") or os.getenv("GEMINI_API_KEY_2", "")
                  or os.getenv("GEMINI_API_KEY_3", "")).strip()
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "").strip()

RUTUBE_ENABLED = os.getenv(
    "RUTUBE_ENABLED",
    "true",
).strip().lower() in {"1", "true", "yes", "on"}
# Extra Rutube author (channel) IDs trusted as official rights holders,
# in addition to videos Rutube itself marks as official.
RUTUBE_OFFICIAL_AUTHORS = {
    x.strip()
    for x in os.getenv("RUTUBE_OFFICIAL_AUTHORS", "").split(",")
    if x.strip()
}
RUTUBE_SEARCH_API = "https://rutube.ru/api/search/video/"
RUTUBE_MIN_MOVIE_SECONDS = 40 * 60
RUTUBE_RECHECK_DAYS = 7
RUTUBE_MISS_RECHECK_HOURS = 24

try:
    VIDEO_PROVIDER_SYNC_SECONDS = max(
        60,
        int(os.getenv("VIDEO_PROVIDER_SYNC_SECONDS", "900")),
    )
except ValueError:
    VIDEO_PROVIDER_SYNC_SECONDS = 900

BLENDER_OPEN_MOVIES_API = (
    "https://video.blender.org/api/v1/"
    "video-channels/blender_open_movies/videos"
    "?count=100&sort=-publishedAt"
)
BLENDER_VIDEO_API = "https://video.blender.org/api/v1/videos"
BLENDER_SYNC_SECONDS = 21600

# Curated official Blender Open Movies only.
# This prevents importing teasers, making-of videos or unrelated uploads.
BLENDER_OPEN_MOVIE_TITLES: list[tuple[str, str]] = [
    ("elephants dream", "Сон слонов"),
    ("big buck bunny", "Большой Бак"),
    ("sintel", "Синтел"),
    ("tears of steel", "Слёзы стали"),
    ("caminandes 2", "Каминандес: Gran Dillama"),
    ("gran dillama", "Каминандес: Gran Dillama"),
    ("caminandes 3", "Каминандес: Llamigos"),
    ("llamigos", "Каминандес: Llamigos"),
    ("cosmos laundromat", "Космическая прачечная"),
    ("glass half", "Наполовину полный"),
    ("the daily dweebs", "The Daily Dweebs"),
    ("agent 327", "Агент 327: Операция «Барбершоп»"),
    ("hero", "HERO"),
    ("spring", "Весна"),
    ("coffee run", "Coffee Run"),
    ("sprite fright", "Sprite Fright"),
    ("charge", "Charge"),
    ("wing it", "Wing It!"),
]

# админы — только из .env; не задано или пусто — админки нет ни у кого (никакого id по умолчанию)
ADMIN_IDS: set[int] = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
if not ADMIN_IDS:
    logging.warning("ADMIN_IDS is empty — admin panel is closed for everyone. Set ADMIN_IDS in .env")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not configured")
if not KINOPOISK_TOKEN:
    raise RuntimeError("KINOPOISK_TOKEN is not configured")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not configured")

pool: asyncpg.Pool | None = None
router = Router()


@router.errors()
async def quiet_not_modified(event: Any) -> bool | None:
    """Нажали кнопку, а сообщение и так такое же («message is not modified») — это не ошибка, молчим."""
    exc = getattr(event, "exception", None)
    if isinstance(exc, TelegramBadRequest) and "not modified" in str(exc):
        return True
    return None
contact_router = Router()  # ответы в переписке «написать админу» — раньше всех остальных обработчиков

MAIN_MENU_LABELS = {
    "🔎 Найти фильм или сериал",
    "🆕 Новинки",
    "🔥 Популярное",
    "🔜 Скоро",
    "🆓 Смотреть бесплатно",
    "🎲 Что посмотреть?",
    "🎬 Подборки",
    "❤️ Избранное",
    "⭐ Избранное",
    "🕘 История",
    "📊 Статистика",
    "👤 Профиль",
    "ℹ️ О VKino",
    "🎯 Подобрать фильм",
    "🧩 Викторина дня",
    "📔 Кинодневник",
    "👥 Выбрать вдвоём",
    "🎬 Кинодл — угадай фильм",
    "🔮 Прогноз уикенда",
    "🎁 Пригласить друга",
    "💡 Предложить идею",
    "✉️ Написать админу",
}


class WeeklyEditState(StatesGroup):
    text = State()  # автор присылает свой текст новости выпуска (кнопка «📝 Свой текст»)


class SearchState(StatesGroup):
    query = State()
    describe = State()
    follow = State()
    kinodl = State()


class ShortsState(StatesGroup):
    download_url = State()
    facts_title = State()
    say_entry = State()
    voice_text = State()
    trailer_query = State()
    feed_url = State()
    music_file = State()
    music_own = State()
    clone_audio = State()
    fact_channels = State()
    fact_channels_add = State()


class ReviewState(StatesGroup):
    title = State()
    notes = State()
    own = State()
    edit = State()
    media = State()


class FactsState(StatesGroup):
    title = State()
    text = State()
    edit = State()
    media = State()


class SceneReviewState(StatesGroup):
    replacement = State()


class CustomState(StatesGroup):
    text = State()
    video = State()


class LoreState(StatesGroup):
    own = State()
    wwk = State()
    news = State()
    name = State()
    hook = State()
    edit = State()
    media = State()


class DubState(StatesGroup):
    source = State()
    edit = State()


class AdminSourceState(StatesGroup):
    search_media = State()
    voice = State()
    url = State()


class KinopoiskUnavailable(RuntimeError):
    pass


class Unofficial:
    """Запасной Кинопоиск: kinopoiskapiunofficial.tech. Отдаёт поиск и карточку фильма в формате poiskkino
    (те же id Кинопоиска) — этого хватает поиску, карточкам и постерам, пока основные ключи на лимите."""

    URL = "https://kinopoiskapiunofficial.tech/api"
    TYPES = {"FILM": "movie", "VIDEO": "movie", "TV_SERIES": "tv-series", "MINI_SERIES": "tv-series",
             "TV_SHOW": "tv-show"}

    def __init__(self, key: str) -> None:
        self.key = key

    async def _fetch(self, path: str, **params: Any) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(headers={"X-API-KEY": self.key, "accept": "application/json"},
                                         timeout=timeout) as session:
            async with session.get(f"{self.URL}{path}", params=params) as resp:
                resp.raise_for_status()
                return await resp.json()

    @classmethod
    def convert(cls, f: dict[str, Any]) -> dict[str, Any]:
        """Фильм kinopoiskapiunofficial → документ в формате poiskkino."""
        def num(x: Any) -> float:
            try:
                return float(str(x).replace("%", ""))
            except (TypeError, ValueError):
                return 0.0

        kind = cls.TYPES.get(str(f.get("type") or ""), "movie")
        age = str(f.get("ratingAgeLimits") or "").replace("age", "")
        return {
            "id": int(f.get("kinopoiskId") or f.get("filmId") or 0),
            "name": f.get("nameRu") or f.get("nameEn") or f.get("nameOriginal"),
            "enName": f.get("nameEn"), "alternativeName": f.get("nameOriginal") or f.get("nameEn"),
            "year": int(str(f.get("year") or "0")[:4] or 0) or None,
            "description": f.get("description"), "shortDescription": f.get("shortDescription"),
            "type": kind, "isSeries": kind != "movie" or bool(f.get("serial")),
            "rating": {"kp": num(f.get("ratingKinopoisk") or f.get("rating")), "imdb": num(f.get("ratingImdb"))},
            "votes": {"kp": int(num(f.get("ratingKinopoiskVoteCount") or f.get("ratingVoteCount"))),
                      "imdb": int(num(f.get("ratingImdbVoteCount")))},
            "poster": {"url": f.get("posterUrl"), "previewUrl": f.get("posterUrlPreview")},
            "genres": [{"name": g.get("genre")} for g in f.get("genres") or [] if g.get("genre")],
            "countries": [{"name": c.get("country")} for c in f.get("countries") or [] if c.get("country")],
            "movieLength": f.get("filmLength") if isinstance(f.get("filmLength"), int) else None,
            "ageRating": int(age) if age.isdigit() else None,
            "externalId": {"imdb": f.get("imdbId")},
        }

    async def get(self, path: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """Ответ в формате poiskkino или None — такой запрос запасной API не умеет."""
        if path == "/movie/search" and params.get("query"):
            data = await self._fetch("/v2.1/films/search-by-keyword", keyword=params["query"], page=1)
            docs = [self.convert(f) for f in data.get("films") or [] if f.get("filmId")]
            return {"docs": docs[: int(params.get("limit") or 10)], "pages": 1, "total": len(docs)}
        m = re.fullmatch(r"/movie/(\d+)", path)
        if m:
            return self.convert(await self._fetch(f"/v2.2/films/{m.group(1)}"))
        return None


class PoiskKino:
    """Кинопоиск через poiskkino.dev. Несколько ключей (KINOPOISK_TOKEN, KINOPOISK_TOKEN_2, … или через
    запятую): упёрся в лимит (401/403/429) — следующий ключ, исчерпанный отдыхает до конца суток (UTC).
    Ответы запоминаем на несколько часов — лимит тратится втрое медленнее. Все ключи исчерпаны — поиск
    и карточка фильма через запасной kinopoiskapiunofficial.tech (KP_UNOFFICIAL_KEY), если он задан."""

    BASE_URL = "https://api.poiskkino.dev/v1.5"
    CACHE_TTL = 6 * 3600
    CACHE_MAX = 3000
    NO_CACHE = ("/movie/random",)

    def __init__(self, tokens: list[str], unofficial_key: str = "") -> None:
        self.tokens = [t for t in tokens if t]
        self.token = self.tokens[0] if self.tokens else ""
        self.unofficial = Unofficial(unofficial_key) if unofficial_key else None
        self.resting: dict[str, float] = {}  # ключ → до какого времени не трогаем
        self.calls: dict[str, int] = {}  # ключ → запросов сегодня (для статистики)
        self.calls_day = ""
        self.cache: dict[str, tuple[float, Any]] = {}
        self.hits = self.misses = self.fallbacks = 0
        self.last_error = ""
        self.tmdb_ratings: dict[int, tuple[float, tuple[float, int] | None]] = {}

    def _count(self, token: str) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        if today != self.calls_day:
            self.calls_day, self.calls = today, {}
        self.calls[token] = self.calls.get(token, 0) + 1

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        data = await self._get_raw(path, **params)
        if re.fullmatch(r"/movie/\d+", path) and isinstance(data, dict) and data.get("id"):
            await self._add_tmdb_rating(data)
        return data

    async def _add_tmdb_rating(self, film: dict[str, Any]) -> None:
        """В карточку фильма — рейтинг TMDB (rating.tmdb, votes.tmdb): его бот показывает в карточках и постах.
        Запоминаем на сутки по id фильма, чтобы не спрашивать TMDB каждый раз."""
        fid = int(film["id"])
        cached = self.tmdb_ratings.get(fid)
        if not cached or time.time() - cached[0] > 86400:
            try:
                got = await shorts_daily.tmdb.rating_for(os.getenv("TMDB_API_KEY", "").strip(), film)
            except Exception as exc:  # noqa: BLE001 — без рейтинга TMDB останется Кинопоиск/IMDb
                logging.info("TMDB rating for %s failed: %s", fid, exc)
                got = None
            if len(self.tmdb_ratings) > 5000:
                self.tmdb_ratings.clear()
            cached = self.tmdb_ratings[fid] = (time.time(), got)
        if cached[1]:
            value, votes = cached[1]
            film.setdefault("rating", {})["tmdb"] = value
            if not isinstance(film.get("votes"), dict):
                film["votes"] = {}
            film["votes"]["tmdb"] = votes

    async def _get_raw(self, path: str, **params: Any) -> dict[str, Any]:
        key = path + "?" + urlencode(sorted((k, str(v)) for k, v in params.items()))
        cached = self.cache.get(key)
        if cached and time.time() - cached[0] < self.CACHE_TTL and not path.startswith(self.NO_CACHE):
            self.hits += 1
            return copy.deepcopy(cached[1])  # копия: вызывающий код может менять ответ
        self.misses += 1
        errors = []
        for token in self.tokens:
            if self.resting.get(token, 0) > time.time():
                continue
            headers = {"X-API-KEY": token, "accept": "application/json"}
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
                async with session.get(f"{self.BASE_URL}{path}", params=params) as resp:
                    self._count(token)
                    if resp.status in (401, 403, 429):  # лимит ключа — отдыхает до конца суток UTC
                        tomorrow = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0) + timedelta(days=1)
                        self.resting[token] = tomorrow.timestamp()
                        self.last_error = f"ключ …{token[-4:]}: HTTP {resp.status} ({datetime.now(MSK):%d.%m %H:%M})"
                        errors.append(self.last_error)
                        logging.warning("Kinopoisk key …%s rejected (%s), switching", token[-4:], resp.status)
                        continue
                    resp.raise_for_status()
                    data = await resp.json()
            if not path.startswith(self.NO_CACHE):
                if len(self.cache) >= self.CACHE_MAX:
                    for k in sorted(self.cache, key=lambda x: self.cache[x][0])[: self.CACHE_MAX // 5]:
                        self.cache.pop(k, None)
                self.cache[key] = (time.time(), data)
            return data
        if self.unofficial:
            try:
                data = await self.unofficial.get(path, params)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"запасной API: {exc}")
            else:
                if data is not None:
                    self.fallbacks += 1
                    self.cache[key] = (time.time(), data)
                    return data
        raise KinopoiskUnavailable("Кинопоиск недоступен: " + ("; ".join(errors) or "все ключи на сегодня исчерпаны"))

    def status(self) -> str:
        now = time.time()
        keys = ", ".join(f"…{t[-4:]} {'⛔ лимит' if self.resting.get(t, 0) > now else '✅'} ({self.calls.get(t, 0)} запр.)"
                         for t in self.tokens)
        total = self.hits + self.misses
        return (f"🎬 Кинопоиск: ключи {keys}"
                + (f" · из памяти {round(100 * self.hits / total)}%" if total else "")
                + (f" · запасной API: {'есть' if self.unofficial else 'нет'}"
                   + (f", выручил {self.fallbacks} раз" if self.fallbacks else ""))
                + (f"\n⚠️ Последний отказ: {self.last_error}" if self.last_error else ""))

    @staticmethod
    def media_type(item: dict[str, Any]) -> str:
        if item.get("isSeries") is True:
            return "series"
        value = str(item.get("type") or "").lower()
        return "series" if "series" in value or "сериал" in value else "movie"

    def normalize(self, item: dict[str, Any]) -> dict[str, Any]:
        result = dict(item)
        result["media_type"] = self.media_type(item)
        return result

    async def search(self, query: str, limit: int = 12) -> list[dict[str, Any]]:
        # Search intentionally has no release-date filter:
        # already released and announced/future projects are both returned.
        data = await self._get(
            "/movie/search",
            query=query,
            page=1,
            limit=limit,
        )
        return [
            self.normalize(x)
            for x in (data.get("docs") or [])[:limit]
        ]

    async def details(self, movie_id: int) -> dict[str, Any]:
        return self.normalize(await self._get(f"/movie/{movie_id}"))

    async def popular(
        self,
        limit: int = 8,
        series: bool | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "page": 1,
            "limit": limit,
            "sortField": "votes.kp",
            "sortType": "-1",
            "rating.kp": "6-10",
        }
        if series is not None:
            params["isSeries"] = str(series).lower()
        data = await self._get("/movie", **params)
        return [self.normalize(x) for x in (data.get("docs") or [])[:limit]]

    async def new_releases(
        self,
        series: bool,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        today = date.today()
        start = today - timedelta(days=180)
        date_range = (
            f"{start.strftime('%d.%m.%Y')}-"
            f"{today.strftime('%d.%m.%Y')}"
        )

        data = await self._get(
            "/movie",
            page=1,
            limit=max(limit, 20),
            isSeries=str(series).lower(),
            **{
                "premiere.world": date_range,
                "sortField": "premiere.world",
                "sortType": "-1",
            },
        )

        items = [
            self.normalize(item)
            for item in (data.get("docs") or [])
            if isinstance(item, dict)
        ]

        def release_key(item: dict[str, Any]) -> str:
            premiere = item.get("premiere") or {}
            if not isinstance(premiere, dict):
                return ""
            return str(premiere.get("world") or "")

        items.sort(key=release_key, reverse=True)
        return items[:limit]

    async def upcoming_releases(
        self,
        series: bool,
        limit: int = 20,
        days_ahead: int = 730,
    ) -> list[dict[str, Any]]:
        today = date.today()
        end = today + timedelta(days=days_ahead)
        date_range = (
            f"{today.strftime('%d.%m.%Y')}-"
            f"{end.strftime('%d.%m.%Y')}"
        )

        data = await self._get(
            "/movie",
            page=1,
            limit=max(limit, 50),
            isSeries=str(series).lower(),
            **{
                "premiere.world": date_range,
                "sortField": "premiere.world",
                "sortType": "1",
            },
        )

        items = [
            self.normalize(item)
            for item in (data.get("docs") or [])
            if isinstance(item, dict)
        ]

        def premiere_key(item: dict[str, Any]) -> str:
            premiere = item.get("premiere") or {}
            if not isinstance(premiere, dict):
                return ""
            return str(premiere.get("world") or "")

        items.sort(key=premiere_key)
        return items[:limit]

    async def seasons(self, movie_id: int) -> list[dict[str, Any]]:
        data = await self._get("/season", page=1, limit=50, movieId=movie_id)
        docs = [x for x in (data.get("docs") or []) if isinstance(x, dict)]

        def season_number(item: dict[str, Any]) -> int:
            try:
                return int(item.get("number"))
            except (TypeError, ValueError):
                return 9999

        docs.sort(key=season_number)
        return docs

    async def season(
        self,
        movie_id: int,
        season_number: int,
    ) -> dict[str, Any] | None:
        for item in await self.seasons(movie_id):
            try:
                if int(item.get("number")) == season_number:
                    return item
            except (TypeError, ValueError):
                continue
        return None

    async def random_pick(self) -> dict[str, Any] | None:
        try:
            item = await self._get("/movie/random", **{"rating.kp": "6-10"})
            if item.get("id"):
                return self.normalize(item)
        except Exception:
            pass
        items = await self.popular(limit=20)
        return random.choice(items) if items else None

    @staticmethod
    def title(item: dict[str, Any]) -> str:
        return (
            item.get("name")
            or item.get("alternativeName")
            or item.get("enName")
            or "Без названия"
        )

    @staticmethod
    def poster(item: dict[str, Any]) -> str | None:
        poster = item.get("poster") or {}
        if isinstance(poster, dict):
            return poster.get("url") or poster.get("previewUrl")
        return None

    @staticmethod
    def trailer(item: dict[str, Any]) -> str | None:
        videos = item.get("videos") or {}
        trailers = videos.get("trailers", []) if isinstance(videos, dict) else []
        for trailer in trailers:
            if isinstance(trailer, dict) and trailer.get("url"):
                return str(trailer["url"])
        return None

    @staticmethod
    def watch_links(item: dict[str, Any]) -> list[tuple[str, str]]:
        watchability = item.get("watchability") or {}
        providers = (
            watchability.get("items", [])
            if isinstance(watchability, dict)
            else []
        )
        result: list[tuple[str, str]] = []
        for provider in providers:
            if isinstance(provider, dict) and provider.get("url"):
                result.append(
                    (
                        str(provider.get("name") or "Площадка"),
                        str(provider["url"]),
                    )
                )
        return result


kp = PoiskKino(
    [t.strip() for t in ",".join([KINOPOISK_TOKEN] + [os.getenv(f"KINOPOISK_TOKEN_{i}", "") for i in range(2, 6)]).split(",")],
    os.getenv("KP_UNOFFICIAL_KEY", "").strip(),
)


def db() -> asyncpg.Pool:
    if pool is None:
        raise RuntimeError("Database pool is not initialized")
    return pool


async def init_db() -> None:
    global pool
    pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=5,
    )
    async with db().acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS favorites (
                user_id BIGINT NOT NULL,
                media_type TEXT NOT NULL,
                movie_id BIGINT NOT NULL,
                title TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (user_id, media_type, movie_id)
            );

            CREATE TABLE IF NOT EXISTS history (
                user_id BIGINT NOT NULL,
                media_type TEXT NOT NULL,
                movie_id BIGINT NOT NULL,
                title TEXT NOT NULL,
                viewed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (user_id, media_type, movie_id)
            );

            CREATE TABLE IF NOT EXISTS search_history (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                query TEXT NOT NULL,
                searched_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_search_history_user_time
            ON search_history(user_id, searched_at DESC);

            CREATE TABLE IF NOT EXISTS user_activity (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                media_type TEXT NOT NULL,
                movie_id BIGINT NOT NULL,
                title TEXT NOT NULL,
                action TEXT NOT NULL
                    CHECK (action IN ('open', 'trailer', 'watch')),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_user_activity_user_time
            ON user_activity(user_id, created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_user_activity_user_movie
            ON user_activity(user_id, movie_id, action);

            CREATE TABLE IF NOT EXISTS playback_sources (
                id BIGSERIAL PRIMARY KEY,
                movie_id BIGINT NOT NULL,
                season_number INTEGER,
                episode_number INTEGER,
                voice_name TEXT NOT NULL,
                language TEXT NOT NULL DEFAULT 'ru',
                quality INTEGER NOT NULL
                    CHECK (quality IN (360, 480, 720, 1080)),
                playback_url TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'hls',
                provider TEXT NOT NULL DEFAULT 'manual',
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            ALTER TABLE playback_sources
            ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'manual';

            CREATE INDEX IF NOT EXISTS idx_playback_sources_media
            ON playback_sources(
                movie_id,
                season_number,
                episode_number,
                is_active
            );

            CREATE TABLE IF NOT EXISTS free_catalog (
                movie_id BIGINT PRIMARY KEY,
                provider TEXT NOT NULL,
                provider_id TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                original_title TEXT NOT NULL,
                description TEXT,
                poster_url TEXT,
                source_page TEXT NOT NULL,
                license_label TEXT NOT NULL,
                attribution TEXT NOT NULL,
                duration_seconds INTEGER,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_free_catalog_provider
            ON free_catalog(provider, provider_id);

            DROP TABLE IF EXISTS hdrezka_cache;

            DELETE FROM playback_sources WHERE provider='hdrezka';

            CREATE TABLE IF NOT EXISTS rutube_cache (
                movie_id BIGINT PRIMARY KEY,
                video_id TEXT,
                title TEXT,
                author TEXT,
                checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            DELETE FROM rutube_cache WHERE video_id IS NULL;

            CREATE TABLE IF NOT EXISTS trailer_cache (
                movie_id BIGINT PRIMARY KEY,
                provider TEXT NOT NULL,
                youtube_id TEXT,
                direct_url TEXT,
                title TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS premiere_reminders (
                user_id BIGINT NOT NULL,
                movie_id BIGINT NOT NULL,
                title TEXT NOT NULL,
                premiere_date DATE NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                sent_at TIMESTAMPTZ,
                PRIMARY KEY (user_id, movie_id)
            );

            CREATE INDEX IF NOT EXISTS idx_premiere_reminders_due
            ON premiere_reminders(premiere_date, sent_at);

            CREATE TABLE IF NOT EXISTS user_playback_preferences (
                user_id BIGINT PRIMARY KEY,
                preferred_voice TEXT,
                preferred_quality INTEGER
                    CHECK (
                        preferred_quality IS NULL
                        OR preferred_quality IN (360, 480, 720, 1080)
                    ),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS playback_progress (
                user_id BIGINT NOT NULL,
                movie_id BIGINT NOT NULL,
                season_number INTEGER NOT NULL DEFAULT 0,
                episode_number INTEGER NOT NULL DEFAULT 0,
                position_seconds INTEGER NOT NULL DEFAULT 0,
                duration_seconds INTEGER,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (
                    user_id,
                    movie_id,
                    season_number,
                    episode_number
                )
            );

            CREATE TABLE IF NOT EXISTS shorts_history (
                movie_id BIGINT PRIMARY KEY,
                used_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS shorts_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bot_events (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT,
                kind TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_bot_events_kind_time ON bot_events(kind, created_at);

            CREATE TABLE IF NOT EXISTS user_prefs (
                user_id BIGINT PRIMARY KEY,
                digest BOOLEAN NOT NULL DEFAULT TRUE,
                alerts BOOLEAN NOT NULL DEFAULT TRUE,
                blocked BOOLEAN NOT NULL DEFAULT FALSE
            );

            CREATE TABLE IF NOT EXISTS fav_alerts (
                movie_id BIGINT NOT NULL,
                key TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (movie_id, key)
            );

            CREATE TABLE IF NOT EXISTS quiz_daily (
                day DATE PRIMARY KEY,
                film_id BIGINT NOT NULL,
                title TEXT NOT NULL,
                image_file TEXT,
                options TEXT NOT NULL,
                answer INT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS quiz_answers (
                day DATE NOT NULL,
                user_id BIGINT NOT NULL,
                correct BOOLEAN NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (day, user_id)
            );

            CREATE TABLE IF NOT EXISTS ratings (
                user_id BIGINT NOT NULL,
                movie_id BIGINT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                value SMALLINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (user_id, movie_id)
            );

            CREATE TABLE IF NOT EXISTS referrals (
                user_id BIGINT PRIMARY KEY,
                referrer_id BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                subscribed BOOLEAN NOT NULL DEFAULT FALSE
            );

            CREATE TABLE IF NOT EXISTS contact_msgs (
                chat_id BIGINT NOT NULL,
                message_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (chat_id, message_id)
            );

            CREATE TABLE IF NOT EXISTS diary (
                user_id BIGINT NOT NULL,
                movie_id BIGINT NOT NULL,
                title TEXT NOT NULL,
                score SMALLINT NOT NULL DEFAULT 0,
                genres TEXT NOT NULL DEFAULT '',
                year_released INT,
                poster TEXT,
                watched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (user_id, movie_id)
            );

            CREATE TABLE IF NOT EXISTS duo (
                id TEXT PRIMARY KEY,
                creator BIGINT NOT NULL,
                films JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS duo_votes (
                id TEXT NOT NULL,
                user_id BIGINT NOT NULL,
                idx INT NOT NULL,
                liked BOOLEAN NOT NULL,
                PRIMARY KEY (id, user_id, idx)
            );

            CREATE TABLE IF NOT EXISTS follows (
                user_id BIGINT NOT NULL,
                word TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (user_id, word)
            );

            CREATE TABLE IF NOT EXISTS kinodl_day (
                day DATE PRIMARY KEY,
                film_id BIGINT NOT NULL,
                film JSONB NOT NULL,
                frame TEXT
            );

            CREATE TABLE IF NOT EXISTS kinodl_plays (
                day DATE NOT NULL,
                user_id BIGINT NOT NULL,
                tries INT NOT NULL DEFAULT 0,
                solved BOOLEAN NOT NULL DEFAULT FALSE,
                done BOOLEAN NOT NULL DEFAULT FALSE,
                PRIMARY KEY (day, user_id)
            );

            CREATE TABLE IF NOT EXISTS predict_round (
                week TEXT PRIMARY KEY,
                options JSONB NOT NULL,
                winner INT,
                winner_title TEXT,
                settled BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS predict_votes (
                week TEXT NOT NULL,
                user_id BIGINT NOT NULL,
                choice INT NOT NULL,
                PRIMARY KEY (week, user_id)
            );

            CREATE TABLE IF NOT EXISTS ideas (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT,
                username TEXT,
                text TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
    # новые таблицы — только нумерованными миграциями (shorts/migrations.py, таблица schema_version)
    applied = await shorts_migrations.migrate(pool)
    if applied:
        logging.info("Migrations applied: %s", ", ".join(f"{v:03d}" for v in applied))
    shorts_budget.attach(db)  # учёт и лимиты платных вызовов — в таблице ai_usage (миграция 006)
    logging.info("PostgreSQL connected")


async def remember(message: Message) -> None:
    if not message.from_user:
        return
    await db().execute(
        """
        INSERT INTO users(user_id, username, first_name)
        VALUES($1, $2, $3)
        ON CONFLICT(user_id) DO UPDATE SET
            username=EXCLUDED.username,
            first_name=EXCLUDED.first_name,
            last_seen_at=NOW()
        """,
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
    )


async def is_favorite(
    user_id: int,
    media_type: str,
    movie_id: int,
) -> bool:
    value = await db().fetchval(
        """
        SELECT 1
        FROM favorites
        WHERE user_id=$1 AND media_type=$2 AND movie_id=$3
        """,
        user_id,
        media_type,
        movie_id,
    )
    return bool(value)


async def add_favorite(
    user_id: int,
    media_type: str,
    movie_id: int,
    title: str,
) -> None:
    await db().execute(
        """
        INSERT INTO favorites(user_id, media_type, movie_id, title)
        VALUES($1, $2, $3, $4)
        ON CONFLICT(user_id, media_type, movie_id)
        DO UPDATE SET title=EXCLUDED.title
        """,
        user_id,
        media_type,
        movie_id,
        title,
    )


async def delete_favorite(
    user_id: int,
    media_type: str,
    movie_id: int,
) -> None:
    await db().execute(
        """
        DELETE FROM favorites
        WHERE user_id=$1 AND media_type=$2 AND movie_id=$3
        """,
        user_id,
        media_type,
        movie_id,
    )


async def list_favorites(
    user_id: int,
    limit: int = 30,
) -> list[dict[str, Any]]:
    rows = await db().fetch(
        """
        SELECT media_type, movie_id, title
        FROM favorites
        WHERE user_id=$1
        ORDER BY created_at DESC
        LIMIT $2
        """,
        user_id,
        limit,
    )
    return [dict(x) for x in rows]


async def record_history(
    user_id: int,
    media_type: str,
    movie_id: int,
    title: str,
) -> None:
    await db().execute(
        """
        INSERT INTO history(user_id, media_type, movie_id, title)
        VALUES($1, $2, $3, $4)
        ON CONFLICT(user_id, media_type, movie_id)
        DO UPDATE SET title=EXCLUDED.title, viewed_at=NOW()
        """,
        user_id,
        media_type,
        movie_id,
        title,
    )


async def list_history(
    user_id: int,
    limit: int = 20,
) -> list[dict[str, Any]]:
    rows = await db().fetch(
        """
        SELECT media_type, movie_id, title
        FROM history
        WHERE user_id=$1
        ORDER BY viewed_at DESC
        LIMIT $2
        """,
        user_id,
        limit,
    )
    return [dict(x) for x in rows]


async def record_search(
    user_id: int,
    query: str,
) -> None:
    clean = query.strip()[:200]
    if not clean:
        return
    await db().execute(
        """
        INSERT INTO search_history(user_id, query)
        SELECT $1, $2
        WHERE NOT EXISTS (
            SELECT 1
            FROM search_history
            WHERE user_id=$1
              AND LOWER(query)=LOWER($2)
              AND searched_at > NOW() - INTERVAL '2 minutes'
        )
        """,
        user_id,
        clean,
    )


async def list_search_history(
    user_id: int,
    limit: int = 8,
) -> list[dict[str, Any]]:
    rows = await db().fetch(
        """
        SELECT query, searched_at
        FROM search_history
        WHERE user_id=$1
        ORDER BY searched_at DESC
        LIMIT $2
        """,
        user_id,
        limit,
    )
    return [dict(row) for row in rows]


async def record_activity(
    user_id: int,
    media_type: str,
    movie_id: int,
    title: str,
    action: str,
) -> None:
    if action not in {"open", "trailer", "watch"}:
        return
    await db().execute(
        """
        INSERT INTO user_activity(
            user_id, media_type, movie_id, title, action
        )
        SELECT $1, $2, $3, $4, $5
        WHERE NOT EXISTS (
            SELECT 1
            FROM user_activity
            WHERE user_id=$1
              AND movie_id=$3
              AND action=$5
              AND created_at > NOW() - INTERVAL '10 minutes'
        )
        """,
        user_id,
        media_type,
        movie_id,
        title[:300],
        action,
    )


async def activity_media_snapshot(
    user_id: int,
    movie_id: int,
) -> tuple[str, str]:
    row = await db().fetchrow(
        """
        SELECT media_type, title
        FROM history
        WHERE user_id=$1 AND movie_id=$2
        ORDER BY viewed_at DESC
        LIMIT 1
        """,
        user_id,
        movie_id,
    )
    if row:
        return str(row["media_type"]), str(row["title"])

    free_row = await db().fetchrow(
        """
        SELECT title
        FROM free_catalog
        WHERE movie_id=$1
        """,
        movie_id,
    )
    if free_row:
        return "movie", str(free_row["title"])

    return "movie", f"ID {movie_id}"


async def user_stats_summary(
    user_id: int,
) -> dict[str, int]:
    row = await db().fetchrow(
        """
        SELECT
            (
                SELECT COUNT(*)
                FROM search_history
                WHERE user_id=$1
            ) AS searches,
            COUNT(*) FILTER (WHERE action='open') AS opens,
            COUNT(*) FILTER (WHERE action='trailer') AS trailers,
            COUNT(*) FILTER (WHERE action='watch') AS watches
        FROM user_activity
        WHERE user_id=$1
        """,
        user_id,
    )
    return {
        "searches": int(row["searches"] or 0) if row else 0,
        "opens": int(row["opens"] or 0) if row else 0,
        "trailers": int(row["trailers"] or 0) if row else 0,
        "watches": int(row["watches"] or 0) if row else 0,
    }


async def top_user_activity(
    user_id: int,
    limit: int = 10,
) -> list[dict[str, Any]]:
    rows = await db().fetch(
        """
        SELECT
            movie_id,
            MAX(title) AS title,
            MAX(media_type) AS media_type,
            COUNT(*) FILTER (WHERE action='open') AS opens,
            COUNT(*) FILTER (WHERE action='trailer') AS trailers,
            COUNT(*) FILTER (WHERE action='watch') AS watches,
            COUNT(*) AS total,
            MAX(created_at) AS last_activity
        FROM user_activity
        WHERE user_id=$1
        GROUP BY movie_id
        ORDER BY total DESC, last_activity DESC
        LIMIT $2
        """,
        user_id,
        limit,
    )
    return [dict(row) for row in rows]


async def playback_voices(
    movie_id: int,
    season_number: int | None = None,
    episode_number: int | None = None,
) -> list[str]:
    rows = await db().fetch(
        """
        SELECT DISTINCT voice_name
        FROM playback_sources
        WHERE movie_id=$1
          AND season_number IS NOT DISTINCT FROM $2
          AND episode_number IS NOT DISTINCT FROM $3
          AND is_active=TRUE
        ORDER BY voice_name
        """,
        movie_id,
        season_number,
        episode_number,
    )
    return [str(row["voice_name"]) for row in rows]


async def playback_qualities(
    movie_id: int,
    voice_name: str,
    season_number: int | None = None,
    episode_number: int | None = None,
) -> list[int]:
    rows = await db().fetch(
        """
        SELECT DISTINCT quality
        FROM playback_sources
        WHERE movie_id=$1
          AND season_number IS NOT DISTINCT FROM $2
          AND episode_number IS NOT DISTINCT FROM $3
          AND voice_name=$4
          AND is_active=TRUE
        ORDER BY quality
        """,
        movie_id,
        season_number,
        episode_number,
        voice_name,
    )
    return [int(row["quality"]) for row in rows]


async def playback_url(
    movie_id: int,
    voice_name: str,
    quality: int,
    season_number: int | None = None,
    episode_number: int | None = None,
) -> str | None:
    value = await db().fetchval(
        """
        SELECT playback_url
        FROM playback_sources
        WHERE movie_id=$1
          AND season_number IS NOT DISTINCT FROM $2
          AND episode_number IS NOT DISTINCT FROM $3
          AND voice_name=$4
          AND quality=$5
          AND is_active=TRUE
        ORDER BY id DESC
        LIMIT 1
        """,
        movie_id,
        season_number,
        episode_number,
        voice_name,
        quality,
    )
    return str(value) if value else None


async def save_playback_preference(
    user_id: int,
    voice_name: str | None = None,
    quality: int | None = None,
) -> None:
    await db().execute(
        """
        INSERT INTO user_playback_preferences(
            user_id,
            preferred_voice,
            preferred_quality
        )
        VALUES($1, $2, $3)
        ON CONFLICT(user_id) DO UPDATE SET
            preferred_voice=COALESCE(
                EXCLUDED.preferred_voice,
                user_playback_preferences.preferred_voice
            ),
            preferred_quality=COALESCE(
                EXCLUDED.preferred_quality,
                user_playback_preferences.preferred_quality
            ),
            updated_at=NOW()
        """,
        user_id,
        voice_name,
        quality,
    )


async def profile_stats(user_id: int) -> tuple[Any, int, int]:
    row = await db().fetchrow(
        """
        SELECT
            u.created_at,
            (
                SELECT COUNT(*)
                FROM favorites f
                WHERE f.user_id=u.user_id
            ) AS favorites_count,
            (
                SELECT COUNT(*)
                FROM history h
                WHERE h.user_id=u.user_id
            ) AS history_count
        FROM users u
        WHERE u.user_id=$1
        """,
        user_id,
    )
    if not row:
        return None, 0, 0
    return (
        row["created_at"],
        int(row["favorites_count"]),
        int(row["history_count"]),
    )


async def global_stats() -> tuple[int, int, int]:
    users = int(await db().fetchval("SELECT COUNT(*) FROM users"))
    favs = int(await db().fetchval("SELECT COUNT(*) FROM favorites"))
    history = int(await db().fetchval("SELECT COUNT(*) FROM history"))
    return users, favs, history


async def all_user_ids() -> list[int]:
    rows = await db().fetch("SELECT user_id FROM users")
    return [int(x["user_id"]) for x in rows]


PLAYER_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <title>VKino360 Player</title>
  <script src="https://telegram.org/js/telegram-web-app.js?63"></script>
  <script src="https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js"></script>
  <style>
    :root {
      color-scheme: light dark;
      --bg: var(--tg-theme-bg-color, #111318);
      --text: var(--tg-theme-text-color, #ffffff);
      --hint: var(--tg-theme-hint-color, #8d96a5);
      --button: var(--tg-theme-button-color, #2aabee);
      --button-text: var(--tg-theme-button-text-color, #ffffff);
      --secondary: var(--tg-theme-secondary-bg-color, #1b1e25);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      padding: max(14px, env(safe-area-inset-top)) 14px max(18px, env(safe-area-inset-bottom));
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .brand { font-weight: 800; font-size: 20px; margin-bottom: 4px; }
    .subtitle { color: var(--hint); font-size: 13px; margin-bottom: 14px; }
    .card { background: var(--secondary); border-radius: 16px; padding: 12px; }
    .player-wrap {
      position: relative;
      width: 100%;
      background: #000;
      border-radius: 12px;
      overflow: hidden;
    }
    video {
      display: block;
      width: 100%;
      background: #000;
      border-radius: 12px;
      max-height: 62vh;
      aspect-ratio: 16 / 9;
      object-fit: contain;
    }
    .player-actions {
      display: flex;
      gap: 10px;
      margin-top: 10px;
    }
    #fullscreen {
      background: var(--button);
      color: var(--button-text);
      font-weight: 700;
    }
    #exitFullscreen {
      display: none;
      position: fixed;
      top: max(10px, env(safe-area-inset-top));
      right: 10px;
      z-index: 10001;
      width: auto;
      padding: 10px 14px;
      background: rgba(0,0,0,.7);
      color: #fff;
      border-radius: 999px;
      font-size: 18px;
    }
    body.cinema-mode {
      overflow: hidden;
      padding: 0;
      background: #000;
    }
    body.cinema-mode .brand,
    body.cinema-mode .subtitle,
    body.cinema-mode .row,
    body.cinema-mode #status,
    body.cinema-mode #next,
    body.cinema-mode .player-actions {
      display: none !important;
    }
    body.cinema-mode .card {
      position: fixed;
      inset: 0;
      z-index: 9999;
      padding: 0;
      border-radius: 0;
      background: #000;
    }
    body.cinema-mode .player-wrap {
      position: fixed;
      inset: 0;
      width: 100vw;
      height: 100vh;
      border-radius: 0;
      background: #000;
    }
    body.cinema-mode video {
      width: 100vw;
      height: 100vh;
      max-height: none;
      aspect-ratio: auto;
      border-radius: 0;
      object-fit: contain;
      background: #000;
    }
    body.cinema-mode #exitFullscreen {
      display: block;
    }
    .player-wrap:fullscreen,
    .player-wrap:-webkit-full-screen {
      width: 100vw;
      height: 100vh;
      background: #000;
    }
    .player-wrap:fullscreen video,
    .player-wrap:-webkit-full-screen video {
      width: 100vw;
      height: 100vh;
      max-height: none;
      object-fit: contain;
      border-radius: 0;
    }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 12px; }
    label { font-size: 12px; color: var(--hint); display: block; margin-bottom: 5px; }
    select, button {
      width: 100%; border: 0; border-radius: 12px; padding: 12px;
      background: var(--bg); color: var(--text); font-size: 15px;
    }
    button.primary { background: var(--button); color: var(--button-text); font-weight: 700; }
    #status { color: var(--hint); font-size: 13px; margin-top: 10px; min-height: 18px; }
    #next { margin-top: 12px; display: none; }
    @media (max-width: 420px) { .row { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <div class="brand">VKino360 🎬</div>
  <div class="subtitle" id="title">Плеер внутри Telegram</div>
  <div class="card">
    <div class="player-wrap" id="playerWrap">
      <video id="video" controls playsinline preload="metadata"></video>
      <button id="exitFullscreen" type="button" aria-label="Выйти из полноэкранного режима">✕</button>
    </div>
    <div class="player-actions">
      <button id="fullscreen" class="primary" type="button">⛶ На весь экран</button>
    </div>
    <div class="row">
      <div><label>Озвучка</label><select id="voice"></select></div>
      <div><label>Качество</label><select id="quality"></select></div>
    </div>
    <div id="status">Загрузка…</div>
    <button id="next" class="primary">Следующая серия ▶</button>
  </div>
<script>
(() => {
  const tg = window.Telegram?.WebApp;
  if (!tg) { document.getElementById('status').textContent = 'Открой плеер из Telegram.'; return; }
  tg.ready();
  tg.expand();
  try { tg.setHeaderColor('bg_color'); } catch (_) {}
  try { tg.setBackgroundColor('#000000'); } catch (_) {}
  const p = new URLSearchParams(location.search);
  const movieId = Number(p.get('movie_id') || 0);
  const season = Number(p.get('season') || 0);
  const episode = Number(p.get('episode') || 0);
  const video = document.getElementById('video');
  const playerWrap = document.getElementById('playerWrap');
  const fullscreen = document.getElementById('fullscreen');
  const exitFullscreen = document.getElementById('exitFullscreen');
  const voice = document.getElementById('voice');
  const quality = document.getElementById('quality');
  const status = document.getElementById('status');
  const next = document.getElementById('next');
  let model = null, hls = null, saveTimer = null;

  function lockLandscape() {
    try {
      if (screen.orientation && screen.orientation.lock) {
        screen.orientation.lock('landscape').catch(() => {});
      }
    } catch (_) {}
  }

  function unlockOrientation() {
    try {
      if (screen.orientation && screen.orientation.unlock) {
        screen.orientation.unlock();
      }
    } catch (_) {}
  }

  async function enterCinemaMode() {
    document.body.classList.add('cinema-mode');
    try { tg.expand(); } catch (_) {}

    // Telegram Mini App fullscreen (supported in newer clients).
    try {
      if (typeof tg.requestFullscreen === 'function') {
        tg.requestFullscreen();
      }
    } catch (_) {}

    lockLandscape();

    // Native element fullscreen where WebView/browser permits it.
    try {
      if (playerWrap.requestFullscreen) {
        await playerWrap.requestFullscreen();
      } else if (playerWrap.webkitRequestFullscreen) {
        playerWrap.webkitRequestFullscreen();
      } else if (video.webkitEnterFullscreen) {
        video.webkitEnterFullscreen();
      }
    } catch (_) {
      // CSS cinema-mode already fills the available Telegram viewport.
    }
  }

  async function leaveCinemaMode() {
    document.body.classList.remove('cinema-mode');

    try {
      if (document.fullscreenElement && document.exitFullscreen) {
        await document.exitFullscreen();
      } else if (document.webkitFullscreenElement && document.webkitExitFullscreen) {
        document.webkitExitFullscreen();
      }
    } catch (_) {}

    try {
      if (typeof tg.exitFullscreen === 'function') {
        tg.exitFullscreen();
      }
    } catch (_) {}

    unlockOrientation();
  }

  fullscreen.addEventListener('click', enterCinemaMode);
  exitFullscreen.addEventListener('click', leaveCinemaMode);

  document.addEventListener('fullscreenchange', () => {
    if (!document.fullscreenElement && document.body.classList.contains('cinema-mode')) {
      // Keep cinema-mode only when Telegram itself is still fullscreen.
      // The visible close button lets the user exit explicitly.
    }
  });

  if (typeof tg.onEvent === 'function') {
    try {
      tg.onEvent('fullscreenChanged', () => {
        if (tg.isFullscreen === false) {
          document.body.classList.remove('cinema-mode');
          unlockOrientation();
        }
      });
    } catch (_) {}
  }

  async function api(path, options = {}) {
    options.headers = Object.assign({}, options.headers || {}, {
      'X-Telegram-Init-Data': tg.initData || ''
    });
    const r = await fetch(path, options);
    if (!r.ok) throw new Error((await r.text()) || ('HTTP ' + r.status));
    return r.json();
  }
  function currentVoice() { return model.voices.find(v => v.name === voice.value) || model.voices[0]; }
  function fillQualities(preferred) {
    const v = currentVoice(); quality.innerHTML = '';
    for (const q of v.qualities) {
      const o = document.createElement('option'); o.value = String(q.quality); o.textContent = q.quality + 'p'; quality.appendChild(o);
    }
    const desired = preferred && v.qualities.some(q => q.quality === preferred) ? preferred : v.qualities[v.qualities.length - 1].quality;
    quality.value = String(desired);
  }
  function selectedSource() {
    const v = currentVoice();
    return v.qualities.find(q => String(q.quality) === quality.value) || v.qualities[0];
  }
  function destroyHls() { if (hls) { hls.destroy(); hls = null; } }
  function loadVideo(keepTime = true) {
    const src = selectedSource(); if (!src) return;
    const t = keepTime ? (video.currentTime || 0) : (model.resume || 0);
    destroyHls();
    const url = src.url;
    if ((src.type === 'hls' || /\.m3u8($|\?)/i.test(url)) && window.Hls && Hls.isSupported()) {
      hls = new Hls({ enableWorker: true }); hls.loadSource(url); hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, () => { if (t > 2) video.currentTime = t; video.play().catch(() => {}); });
    } else {
      video.src = url; video.addEventListener('loadedmetadata', function once() {
        video.removeEventListener('loadedmetadata', once); if (t > 2) video.currentTime = t; video.play().catch(() => {});
      });
    }
    status.textContent = voice.value + ' • ' + quality.value + 'p';
  }
  async function saveProgress() {
    if (!video.duration || !isFinite(video.duration)) return;
    try {
      await api('/api/progress', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({movie_id:movieId, season, episode, position:Math.floor(video.currentTime||0), duration:Math.floor(video.duration||0), voice:voice.value, quality:Number(quality.value||0)})
      });
    } catch (_) {}
  }
  async function boot() {
    try {
      const qs = new URLSearchParams({movie_id:String(movieId), season:String(season), episode:String(episode)});
      model = await api('/api/playback?' + qs.toString());
      if (!model.voices.length) throw new Error('Источники не найдены');
      voice.innerHTML = '';
      for (const v of model.voices) { const o=document.createElement('option'); o.value=v.name; o.textContent=v.name; voice.appendChild(o); }
      if (model.preferred?.voice && model.voices.some(v=>v.name===model.preferred.voice)) voice.value = model.preferred.voice;
      fillQualities(model.preferred?.quality || null);
      loadVideo(false);
      if (model.next) {
        next.style.display='block';
        next.onclick = () => {
          const q = new URLSearchParams({movie_id:String(movieId), season:String(model.next.season), episode:String(model.next.episode)});
          location.href = '/app?' + q.toString();
        };
      }
      saveTimer = setInterval(saveProgress, 10000);
      video.addEventListener('pause', saveProgress);
      video.addEventListener('ended', async () => { await saveProgress(); if (model.next) next.style.display='block'; });
      voice.onchange = () => { fillQualities(model.preferred?.quality || null); loadVideo(true); };
      quality.onchange = () => loadVideo(true);
    } catch (e) { status.textContent = 'Ошибка: ' + e.message; }
  }
  window.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && document.body.classList.contains('cinema-mode')) {
      leaveCinemaMode();
    }
  });
  window.addEventListener('beforeunload', () => {
    if (saveTimer) clearInterval(saveTimer);
    saveProgress();
    destroyHls();
    unlockOrientation();
  });
  boot();
})();
</script>
</body>
</html>"""


def build_webapp_url(
    movie_id: int,
    season_number: int | None = None,
    episode_number: int | None = None,
) -> str:
    if not WEBAPP_URL:
        return ""
    query = urlencode(
        {
            "movie_id": movie_id,
            "season": season_number or 0,
            "episode": episode_number or 0,
        }
    )
    return f"{WEBAPP_URL}/app?{query}"


def build_trailer_url(movie_id: int) -> str:
    if not WEBAPP_URL:
        return ""
    query = urlencode({"movie_id": movie_id})
    return f"{WEBAPP_URL}/trailer?{query}"


RUTUBE_TRAILER_WORDS = (
    "трейлер",
    "тизер",
    "trailer",
    "teaser",
    "обзор",
    "review",
    "фрагмент",
    "отрывок",
    "клип",
    "making of",
    "со съемок",
)


def _rutube_norm(value: Any) -> str:
    text = str(value or "").lower().replace("ё", "е")
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _rutube_is_official(video: dict[str, Any]) -> bool:
    if video.get("is_official") is True:
        return True
    author = video.get("author") or {}
    if not isinstance(author, dict):
        return False
    if author.get("is_official") is True:
        return True
    return str(author.get("id") or "") in RUTUBE_OFFICIAL_AUTHORS


def _rutube_video_id(video: dict[str, Any]) -> str | None:
    value = str(video.get("id") or "").strip()
    if re.fullmatch(r"[A-Za-z0-9]{8,64}", value):
        return value
    return None


def _rutube_score(
    video: dict[str, Any],
    titles: list[str],
    year: int | None,
) -> int | None:
    """Return a match score, or None when the video must not be used."""
    if not _rutube_is_official(video):
        return None
    if (
        video.get("is_hidden")
        or video.get("is_adult")
        or video.get("is_livestream")
        or video.get("is_paid")
    ):
        return None
    if not _rutube_video_id(video):
        return None

    try:
        duration = int(video.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    if duration < RUTUBE_MIN_MOVIE_SECONDS:
        return None

    name = _rutube_norm(video.get("title"))
    if not name or any(word in name for word in RUTUBE_TRAILER_WORDS):
        return None

    padded = f" {name} "
    matched = [t for t in titles if t and f" {t} " in padded]
    if not matched:
        return None

    score = 1
    if any(name.startswith(t) for t in matched):
        score += 1
    if year:
        text = f"{name} {_rutube_norm(video.get('description'))}"
        years = {int(y) for y in re.findall(r"\b(19\d\d|20\d\d)\b", text)}
        if year in years:
            score += 2
        elif years and all(abs(y - year) > 1 for y in years):
            # Mentions another year only: likely a remake or other film.
            return None
    return score


async def search_rutube_movie(
    item: dict[str, Any],
) -> dict[str, Any] | None:
    titles: list[str] = []
    for key in ("name", "alternativeName", "enName"):
        value = _rutube_norm(item.get(key))
        if value and value not in titles:
            titles.append(value)
    if not titles:
        return None

    year_value = item.get("year")
    try:
        year = int(year_value) if year_value else None
    except (TypeError, ValueError):
        year = None

    best: tuple[int, dict[str, Any]] | None = None
    timeout = aiohttp.ClientTimeout(total=6)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for raw_title in titles[:2]:
            query = f"{raw_title} {year}" if year else raw_title
            async with session.get(
                RUTUBE_SEARCH_API,
                params={"query": query, "format": "json"},
                headers={"accept": "application/json"},
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
            for video in (data or {}).get("results") or []:
                if not isinstance(video, dict):
                    continue
                score = _rutube_score(video, titles, year)
                if score is not None and (best is None or score > best[0]):
                    best = (score, video)
            if best and best[0] >= 3:
                break

    if not best:
        logging.info(
            "Rutube: no official full movie for %r (%s)",
            titles[0],
            year,
        )
        return None
    video = best[1]
    author = video.get("author") or {}
    return {
        "video_id": _rutube_video_id(video),
        "title": str(video.get("title") or "")[:300],
        "author": str(
            author.get("name") if isinstance(author, dict) else ""
        )[:200],
    }


async def rutube_movie(
    item: dict[str, Any],
) -> dict[str, Any] | None:
    """Cached official Rutube full movie for a Kinopoisk item."""
    if not RUTUBE_ENABLED:
        return None
    movie_id = int(item["id"])

    cached = await db().fetchrow(
        """
        SELECT video_id, title, author
        FROM rutube_cache
        WHERE movie_id=$1
          AND checked_at > NOW() - (
                CASE
                    WHEN video_id IS NULL
                    THEN $3::int * INTERVAL '1 hour'
                    ELSE $2::int * INTERVAL '1 day'
                END
              )
        """,
        movie_id,
        RUTUBE_RECHECK_DAYS,
        RUTUBE_MISS_RECHECK_HOURS,
    )
    if cached:
        return dict(cached) if cached["video_id"] else None

    try:
        found = await search_rutube_movie(item)
    except Exception as exc:
        # Network errors are not cached so the next open retries.
        logging.info(
            "Rutube search failed for movie %s: %s",
            movie_id,
            type(exc).__name__,
        )
        return None

    await db().execute(
        """
        INSERT INTO rutube_cache(movie_id, video_id, title, author)
        VALUES($1,$2,$3,$4)
        ON CONFLICT(movie_id) DO UPDATE SET
            video_id=EXCLUDED.video_id,
            title=EXCLUDED.title,
            author=EXCLUDED.author,
            checked_at=NOW()
        """,
        movie_id,
        found["video_id"] if found else None,
        found["title"] if found else None,
        found["author"] if found else None,
    )
    return found


def build_rutube_url(movie_id: int) -> str:
    if not WEBAPP_URL:
        return ""
    query = urlencode({"movie_id": movie_id, "kind": "rutube"})
    return f"{WEBAPP_URL}/trailer?{query}"


def youtube_video_id(url: str) -> str | None:
    if not url:
        return None
    patterns = [
        r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([A-Za-z0-9_-]{6,})",
        r"[?&]v=([A-Za-z0-9_-]{6,})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


async def cache_trailer(
    movie_id: int,
    provider: str,
    youtube_id: str | None = None,
    direct_url: str | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    await db().execute(
        """
        INSERT INTO trailer_cache(
            movie_id, provider, youtube_id, direct_url, title
        )
        VALUES($1,$2,$3,$4,$5)
        ON CONFLICT(movie_id) DO UPDATE SET
            provider=EXCLUDED.provider,
            youtube_id=EXCLUDED.youtube_id,
            direct_url=EXCLUDED.direct_url,
            title=EXCLUDED.title,
            updated_at=NOW()
        """,
        movie_id,
        provider,
        youtube_id,
        direct_url,
        title,
    )
    return {
        "provider": provider,
        "youtube_id": youtube_id,
        "direct_url": direct_url,
        "title": title,
    }


async def cached_trailer(movie_id: int) -> dict[str, Any] | None:
    row = await db().fetchrow(
        """
        SELECT provider, youtube_id, direct_url, title, updated_at
        FROM trailer_cache
        WHERE movie_id=$1
          AND updated_at > NOW() - INTERVAL '30 days'
        """,
        movie_id,
    )
    return dict(row) if row else None


async def tmdb_movie_id(item: dict[str, Any]) -> tuple[str, int] | None:
    media_type = item.get("media_type", "movie")
    tmdb_kind = "tv" if media_type == "series" else "movie"

    external = item.get("externalId") or {}
    if isinstance(external, dict):
        raw_tmdb = external.get("tmdb")
        try:
            if raw_tmdb:
                return tmdb_kind, int(raw_tmdb)
        except (TypeError, ValueError):
            pass

    if not TMDB_API_KEY:
        return None

    title = kp.title(item)
    year = item.get("year")
    params: dict[str, Any] = {
        "api_key": TMDB_API_KEY,
        "query": title,
        "language": "ru-RU",
        "include_adult": "false",
    }
    if year:
        if tmdb_kind == "tv":
            params["first_air_date_year"] = int(year)
        else:
            params["year"] = int(year)

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            f"https://api.themoviedb.org/3/search/{tmdb_kind}",
            params=params,
        ) as response:
            if response.status != 200:
                return None
            payload = await response.json()

    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list) or not results:
        return None

    try:
        return tmdb_kind, int(results[0]["id"])
    except (KeyError, TypeError, ValueError):
        return None


async def trailer_from_tmdb_language(
    item: dict[str, Any],
    language: str,
) -> dict[str, Any] | None:
    if not TMDB_API_KEY:
        return None

    resolved = await tmdb_movie_id(item)
    if not resolved:
        return None

    tmdb_kind, tmdb_id = resolved
    timeout = aiohttp.ClientTimeout(total=15)
    params: dict[str, Any] = {
        "api_key": TMDB_API_KEY,
        "language": language,
    }

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            f"https://api.themoviedb.org/3/{tmdb_kind}/{tmdb_id}/videos",
            params=params,
        ) as response:
            if response.status != 200:
                return None
            payload = await response.json()

    videos = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(videos, list):
        return None

    candidates = [
        video
        for video in videos
        if isinstance(video, dict)
        and str(video.get("site") or "").casefold() == "youtube"
        and video.get("key")
    ]
    if not candidates:
        return None

    def score(video: dict[str, Any]) -> tuple[int, int, int]:
        video_type = str(video.get("type") or "").casefold()
        name = str(video.get("name") or "").casefold()
        official = 1 if video.get("official") is True else 0
        trailer = 1 if video_type == "trailer" else 0
        language_hint = 1 if any(
            marker in name
            for marker in (
                "рус",
                "дуб",
                "трейлер",
                "official trailer",
            )
        ) else 0
        return (official, trailer, language_hint)

    candidates.sort(key=score, reverse=True)
    best = candidates[0]

    return {
        "provider": f"tmdb:{language}",
        "youtube_id": str(best["key"]),
        "direct_url": None,
        "title": str(best.get("name") or "Трейлер"),
    }


async def trailer_from_youtube(
    item: dict[str, Any],
    russian: bool = True,
) -> dict[str, Any] | None:
    if not YOUTUBE_API_KEY:
        return None

    title = kp.title(item)
    year = item.get("year") or ""

    if russian:
        query = (
            f"{title} {year} официальный трейлер русский"
        ).strip()
        relevance_language = "ru"
    else:
        query = (
            f"{title} {year} official trailer"
        ).strip()
        relevance_language = "en"

    params = {
        "part": "snippet",
        "type": "video",
        "videoEmbeddable": "true",
        "maxResults": 8,
        "q": query,
        "relevanceLanguage": relevance_language,
        "safeSearch": "moderate",
        "key": YOUTUBE_API_KEY,
    }

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            "https://www.googleapis.com/youtube/v3/search",
            params=params,
        ) as response:
            if response.status != 200:
                return None
            payload = await response.json()

    results = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(results, list) or not results:
        return None

    valid = [
        entry
        for entry in results
        if isinstance(entry, dict)
        and isinstance(entry.get("id"), dict)
        and entry["id"].get("videoId")
    ]
    if not valid:
        return None

    def score(entry: dict[str, Any]) -> tuple[int, int, int]:
        snippet = entry.get("snippet") or {}
        name = str(snippet.get("title") or "").casefold()
        channel = str(
            snippet.get("channelTitle") or ""
        ).casefold()

        official_title = 1 if any(
            marker in name
            for marker in (
                "официальный трейлер",
                "official trailer",
                "official teaser",
            )
        ) else 0

        trailer = 1 if any(
            marker in name
            for marker in (
                "трейлер",
                "trailer",
                "teaser",
            )
        ) else 0

        if russian:
            language_match = 1 if any(
                marker in name
                for marker in (
                    "рус",
                    "дуб",
                    "на русском",
                    "трейлер",
                )
            ) else 0
        else:
            language_match = 1 if any(
                marker in name
                for marker in (
                    "official",
                    "trailer",
                    "teaser",
                )
            ) else 0

        official_channel = 1 if any(
            marker in channel
            for marker in (
                "warner",
                "universal",
                "paramount",
                "sony",
                "disney",
                "marvel",
                "20th century",
                "a24",
                "netflix",
                "hbo",
            )
        ) else 0

        return (
            official_title + official_channel,
            trailer,
            language_match,
        )

    valid.sort(key=score, reverse=True)
    best = valid[0]
    snippet = best.get("snippet") or {}

    return {
        "provider": (
            "youtube:ru"
            if russian
            else "youtube:en"
        ),
        "youtube_id": str(best["id"]["videoId"]),
        "direct_url": None,
        "title": str(
            snippet.get("title") or "Трейлер"
        ),
    }


async def resolve_trailer(
    movie_id: int,
) -> dict[str, Any] | None:
    cached = await cached_trailer(movie_id)

    # Reuse an already cached Russian trailer immediately.
    if cached and str(cached.get("provider") or "") in {
        "tmdb:ru-RU",
        "youtube:ru",
    }:
        return cached

    try:
        item = await kp.details(movie_id)
    except Exception as exc:
        logging.info(
            "Trailer movie lookup failed: %s",
            exc,
        )
        return cached

    # 1. Russian official TMDb video first.
    try:
        result = await trailer_from_tmdb_language(
            item,
            "ru-RU",
        )
        if result:
            return await cache_trailer(
                movie_id,
                **result,
            )
    except Exception as exc:
        logging.info(
            "TMDb RU trailer lookup failed: %s",
            exc,
        )

    # 2. Russian YouTube trailer search.
    try:
        result = await trailer_from_youtube(
            item,
            russian=True,
        )
        if result:
            return await cache_trailer(
                movie_id,
                **result,
            )
    except Exception as exc:
        logging.info(
            "YouTube RU trailer lookup failed: %s",
            exc,
        )

    # Old cached trailer can be used only after Russian attempts.
    if cached:
        return cached

    # 3. Trailer supplied by PoiskKino.
    url = kp.trailer(item)
    if url:
        yt_id = youtube_video_id(url)
        if yt_id:
            return await cache_trailer(
                movie_id,
                "poiskkino",
                youtube_id=yt_id,
                title="Трейлер",
            )

        if str(url).startswith("https://"):
            return await cache_trailer(
                movie_id,
                "poiskkino",
                direct_url=str(url),
                title="Трейлер",
            )

    # 4. English official trailer only when Russian is unavailable.
    try:
        result = await trailer_from_tmdb_language(
            item,
            "en-US",
        )
        if result:
            return await cache_trailer(
                movie_id,
                **result,
            )
    except Exception as exc:
        logging.info(
            "TMDb EN trailer lookup failed: %s",
            exc,
        )

    try:
        result = await trailer_from_youtube(
            item,
            russian=False,
        )
        if result:
            return await cache_trailer(
                movie_id,
                **result,
            )
    except Exception as exc:
        logging.info(
            "YouTube EN trailer lookup failed: %s",
            exc,
        )

    return None


TRAILER_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <title>VKino360 Trailer</title>
  <script src="https://telegram.org/js/telegram-web-app.js?63"></script>
  <style>
    :root {
      color-scheme: dark;
      --bg: #000;
      --text: var(--tg-theme-text-color, #fff);
      --hint: var(--tg-theme-hint-color, #a7a7a7);
      --button: var(--tg-theme-button-color, #2aabee);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      padding: max(10px, env(safe-area-inset-top)) 10px max(12px, env(safe-area-inset-bottom));
      background: #000;
      color: var(--text);
      font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    }
    .head { display:flex; gap:10px; align-items:center; margin-bottom:10px; }
    .title { font-size:16px; font-weight:800; flex:1; }
    button {
      border:0; border-radius:999px; padding:10px 14px;
      background:var(--button); color:#fff; font-weight:700;
    }
    .frame {
      position:relative; width:100%; aspect-ratio:16/9;
      background:#000; overflow:hidden; border-radius:12px;
    }
    iframe, video {
      width:100%; height:100%; border:0; background:#000;
      object-fit:contain;
    }
    #status { color:var(--hint); font-size:13px; margin-top:10px; }
    body.cinema { padding:0; overflow:hidden; }
    body.cinema .head, body.cinema #status { display:none; }
    body.cinema .frame {
      position:fixed; inset:0; width:100vw; height:100vh;
      aspect-ratio:auto; border-radius:0; z-index:9999;
    }
    #exit {
      display:none; position:fixed; z-index:10001;
      right:10px; top:max(10px,env(safe-area-inset-top));
      width:auto; background:rgba(0,0,0,.7);
    }
    body.cinema #exit { display:block; }
  </style>
</head>
<body>
  <div class="head">
    <div class="title" id="title">Трейлер</div>
    <button id="fullscreen">⛶</button>
  </div>
  <div class="frame" id="frame"></div>
  <button id="exit">✕</button>
  <div id="status">Ищем официальный трейлер…</div>
<script>
(() => {
  const tg = window.Telegram?.WebApp;
  if (!tg) return;
  tg.ready(); tg.expand();
  const p = new URLSearchParams(location.search);
  const movieId = Number(p.get('movie_id') || 0);
  const isRutube = p.get('kind') === 'rutube';
  const frame = document.getElementById('frame');
  if (isRutube) {
    document.getElementById('title').textContent = 'Фильм';
    document.getElementById('status').textContent = 'Загружаем видео…';
  }
  const status = document.getElementById('status');
  const title = document.getElementById('title');

  async function api(path) {
    const r = await fetch(path, {
      headers: {'X-Telegram-Init-Data': tg.initData || ''}
    });
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  }

  async function boot() {
    try {
      const data = await api(
        (isRutube ? '/api/rutube' : '/api/trailer') + '?movie_id=' + movieId
      );
      if (data.title) title.textContent = data.title;
      if (data.rutube_id) {
        const id = encodeURIComponent(data.rutube_id);
        frame.innerHTML =
          '<iframe allow="clipboard-write; autoplay; fullscreen" ' +
          'allowfullscreen src="https://rutube.ru/play/embed/' + id + '"></iframe>';
      } else if (data.youtube_id) {
        const id = encodeURIComponent(data.youtube_id);
        frame.innerHTML =
          '<iframe allow="autoplay; encrypted-media; picture-in-picture; fullscreen" ' +
          'allowfullscreen src="https://www.youtube-nocookie.com/embed/' + id +
          '?autoplay=1&rel=0&modestbranding=1"></iframe>';
      } else if (data.direct_url) {
        const v = document.createElement('video');
        v.controls = true; v.autoplay = true; v.playsInline = true;
        v.src = data.direct_url;
        frame.appendChild(v);
      } else {
        throw new Error('Трейлер не найден');
      }
      status.textContent = data.provider ? ('Источник: ' + data.provider) : '';
    } catch (e) {
      status.textContent = isRutube
        ? 'Видео пока недоступно.'
        : 'Трейлер пока не найден.';
    }
  }

  async function enter() {
    document.body.classList.add('cinema');
    try { if (tg.requestFullscreen) tg.requestFullscreen(); } catch (_) {}
    try {
      if (screen.orientation && screen.orientation.lock) {
        screen.orientation.lock('landscape').catch(()=>{});
      }
    } catch (_) {}
  }
  async function leave() {
    document.body.classList.remove('cinema');
    try { if (tg.exitFullscreen) tg.exitFullscreen(); } catch (_) {}
    try {
      if (screen.orientation && screen.orientation.unlock) screen.orientation.unlock();
    } catch (_) {}
  }
  document.getElementById('fullscreen').onclick = enter;
  document.getElementById('exit').onclick = leave;
  boot();
})();
</script>
</body>
</html>"""


async def trailer_page(_: web.Request) -> web.Response:
    return web.Response(
        text=TRAILER_HTML,
        content_type="text/html",
        charset="utf-8",
    )


async def api_trailer(request: web.Request) -> web.Response:
    user = request_telegram_user(request)
    if not user:
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        movie_id = int(request.query.get("movie_id", "0"))
    except ValueError:
        return web.json_response({"error": "bad movie_id"}, status=400)

    result = await resolve_trailer(movie_id)
    if not result:
        return web.json_response({"error": "not found"}, status=404)

    media_type, title = await activity_media_snapshot(
        int(user["id"]),
        movie_id,
    )
    await record_activity(
        int(user["id"]),
        media_type,
        movie_id,
        title,
        "trailer",
    )

    return web.json_response({
        "provider": result.get("provider"),
        "youtube_id": result.get("youtube_id"),
        "direct_url": result.get("direct_url"),
        "title": result.get("title") or "Трейлер",
    })


async def api_rutube(request: web.Request) -> web.Response:
    user = request_telegram_user(request)
    if not user:
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        movie_id = int(request.query.get("movie_id", "0"))
    except ValueError:
        return web.json_response({"error": "bad movie_id"}, status=400)

    row = await db().fetchrow(
        """
        SELECT video_id, title, author
        FROM rutube_cache
        WHERE movie_id=$1 AND video_id IS NOT NULL
        """,
        movie_id,
    )
    if not row:
        return web.json_response({"error": "not found"}, status=404)

    media_type, title = await activity_media_snapshot(
        int(user["id"]),
        movie_id,
    )
    await record_activity(
        int(user["id"]),
        media_type,
        movie_id,
        title,
        "watch",
    )

    return web.json_response({
        "provider": f"Rutube • {row['author']}" if row["author"] else "Rutube",
        "rutube_id": row["video_id"],
        "title": row["title"] or title,
    })


def validate_webapp_init_data(raw: str, max_age: int = 86400) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        pairs = dict(parse_qsl(raw, keep_blank_values=True))
        received_hash = pairs.pop("hash", "")
        if not received_hash:
            return None
        data_check = "\n".join(
            f"{key}={value}"
            for key, value in sorted(pairs.items())
        )
        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        calculated = hmac.new(
            secret_key,
            data_check.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(calculated, received_hash):
            return None
        auth_date = int(pairs.get("auth_date", "0"))
        now = int(time.time())
        # 24 ч: Mini App не обновляет initData, пока открыт (фильм смотрят долго); из будущего — не больше 5 мин
        if auth_date <= 0 or now - auth_date > max_age or auth_date - now > 300:
            return None
        user_raw = pairs.get("user")
        user = json.loads(user_raw) if user_raw else None
        if not isinstance(user, dict) or not user.get("id"):
            return None
        return {"user": user, "fields": pairs}
    except Exception:
        return None


async def miniapp_page(_: web.Request) -> web.Response:
    return web.Response(
        text=PLAYER_HTML,
        content_type="text/html",
        charset="utf-8",
    )


def request_telegram_user(request: web.Request) -> dict[str, Any] | None:
    raw = request.headers.get("X-Telegram-Init-Data", "")
    validated = validate_webapp_init_data(raw)
    return validated["user"] if validated else None


async def api_playback(request: web.Request) -> web.Response:
    user = request_telegram_user(request)
    if not user:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        movie_id = int(request.query.get("movie_id", "0"))
        season = int(request.query.get("season", "0") or 0)
        episode = int(request.query.get("episode", "0") or 0)
    except ValueError:
        return web.json_response({"error": "bad_request"}, status=400)
    season_db = season or None
    episode_db = episode or None

    rows = await db().fetch(
        """
        SELECT voice_name, quality, playback_url, source_type, provider
        FROM playback_sources
        WHERE movie_id=$1
          AND season_number IS NOT DISTINCT FROM $2
          AND episode_number IS NOT DISTINCT FROM $3
          AND is_active=TRUE
        ORDER BY
            voice_name,
            quality,
            CASE
                WHEN provider='manual' THEN 0
                WHEN provider='api' THEN 1
                ELSE 2
            END
        """,
        movie_id,
        season_db,
        episode_db,
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    seen_sources: set[tuple[str, int]] = set()
    for row in rows:
        voice = str(row["voice_name"])
        quality_value = int(row["quality"])
        source_key = (voice, quality_value)
        if source_key in seen_sources:
            continue
        seen_sources.add(source_key)
        grouped.setdefault(voice, []).append(
            {
                "quality": quality_value,
                "url": str(row["playback_url"]),
                "type": str(row["source_type"] or "link"),
            }
        )

    if rows:
        media_type, title = await activity_media_snapshot(
            int(user["id"]),
            movie_id,
        )
        await record_activity(
            int(user["id"]),
            media_type,
            movie_id,
            title,
            "watch",
        )

    pref = await db().fetchrow(
        """
        SELECT preferred_voice, preferred_quality
        FROM user_playback_preferences
        WHERE user_id=$1
        """,
        int(user["id"]),
    )
    resume = await db().fetchval(
        """
        SELECT position_seconds
        FROM playback_progress
        WHERE user_id=$1 AND movie_id=$2
          AND season_number=$3 AND episode_number=$4
        """,
        int(user["id"]), movie_id, season, episode,
    )
    next_item = None
    if season and episode:
        next_row = await db().fetchrow(
            """
            SELECT season_number, episode_number
            FROM playback_sources
            WHERE movie_id=$1
              AND season_number IS NOT NULL
              AND episode_number IS NOT NULL
              AND is_active=TRUE
              AND (
                    season_number > $2
                    OR (season_number=$2 AND episode_number > $3)
                  )
            GROUP BY season_number, episode_number
            ORDER BY season_number, episode_number
            LIMIT 1
            """,
            movie_id, season, episode,
        )
        if next_row:
            next_item = {
                "season": int(next_row["season_number"]),
                "episode": int(next_row["episode_number"]),
            }
    return web.json_response(
        {
            "voices": [
                {"name": name, "qualities": qualities}
                for name, qualities in grouped.items()
            ],
            "preferred": {
                "voice": str(pref["preferred_voice"]) if pref and pref["preferred_voice"] else None,
                "quality": int(pref["preferred_quality"]) if pref and pref["preferred_quality"] else None,
            },
            "resume": int(resume or 0),
            "next": next_item,
        }
    )


_progress_seen: dict[int, float] = {}  # пользователь → время последнего сохранения прогресса


async def api_progress(request: web.Request) -> web.Response:
    user = request_telegram_user(request)
    if not user:
        return web.json_response({"error": "unauthorized"}, status=401)
    # плеер шлёт прогресс раз в 10 с; чаще 3 с с одного пользователя — не пишем в базу
    uid, now = int(user.get("id") or 0), time.monotonic()
    if now - _progress_seen.get(uid, 0) < 3:
        return web.json_response({"ok": True, "throttled": True})
    _progress_seen[uid] = now
    if len(_progress_seen) > 50_000:
        _progress_seen.clear()
    try:
        payload = await request.json()
        movie_id = int(payload.get("movie_id", 0))
        season = int(payload.get("season", 0) or 0)
        episode = int(payload.get("episode", 0) or 0)
        position = max(0, int(payload.get("position", 0) or 0))
        duration = max(0, int(payload.get("duration", 0) or 0))
        voice_name = str(payload.get("voice") or "").strip()[:80]
        quality = int(payload.get("quality", 0) or 0)
    except Exception:
        return web.json_response({"error": "bad_request"}, status=400)
    int_max = 2_147_483_647
    if not (
        0 < movie_id < 2**63
        and 0 <= season <= int_max
        and 0 <= episode <= int_max
        and position <= int_max
        and duration <= int_max
    ):
        return web.json_response({"error": "bad_request"}, status=400)
    if voice_name and quality in {360, 480, 720, 1080}:
        await save_playback_preference(
            int(user["id"]),
            voice_name,
            quality,
        )
    await db().execute(
        """
        INSERT INTO playback_progress(
            user_id, movie_id, season_number, episode_number,
            position_seconds, duration_seconds
        ) VALUES($1,$2,$3,$4,$5,$6)
        ON CONFLICT(user_id,movie_id,season_number,episode_number)
        DO UPDATE SET
            position_seconds=EXCLUDED.position_seconds,
            duration_seconds=EXCLUDED.duration_seconds,
            updated_at=NOW()
        """,
        int(user["id"]), movie_id, season, episode, position, duration,
    )
    return web.json_response({"ok": True})


async def start_miniapp_server() -> web.AppRunner:
    app = web.Application(client_max_size=1024 * 1024)
    app.router.add_get("/", miniapp_page)
    app.router.add_get("/app", miniapp_page)
    app.router.add_get("/trailer", trailer_page)
    app.router.add_get("/api/trailer", api_trailer)
    app.router.add_get("/api/rutube", api_rutube)
    app.router.add_get("/api/playback", api_playback)
    app.router.add_post("/api/progress", api_progress)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info("VKino Mini App server started on port %s", port)
    return runner


async def sync_video_provider_once() -> int:
    if not VIDEO_PROVIDER_API_URL:
        return 0
    headers = {"Accept": "application/json"}
    if VIDEO_PROVIDER_API_TOKEN:
        headers["Authorization"] = f"Bearer {VIDEO_PROVIDER_API_TOKEN}"
    timeout = aiohttp.ClientTimeout(total=45)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        async with session.get(VIDEO_PROVIDER_API_URL) as response:
            response.raise_for_status()
            payload = await response.json()
    items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise ValueError("Provider response must be a list or {'items': [...]} object")
    synced = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            movie_id = int(item.get("kinopoisk_id") or item.get("movie_id"))
            voice_name = str(item.get("voice") or item.get("voice_name") or "Original").strip()[:80]
            quality = int(item.get("quality"))
            playback_url = str(item.get("url") or item.get("playback_url") or "").strip()
            parsed = urlparse(playback_url)
            if quality not in {360, 480, 720, 1080} or parsed.scheme != "https" or not parsed.hostname \
                    or parsed.username or " " in playback_url:
                continue
            season = item.get("season")
            episode = item.get("episode")
            season_number = int(season) if season not in (None, "", 0, "0") else None
            episode_number = int(episode) if episode not in (None, "", 0, "0") else None
            source_type = str(item.get("type") or item.get("source_type") or "link")[:20]
            raw_active = item.get("active", True)  # строка «false» — это выключено, а не True
            active = raw_active if isinstance(raw_active, bool) else str(raw_active).strip().lower() in (
                "1", "true", "yes", "on")
        except Exception:
            continue
        existing_id = await db().fetchval(
            """
            SELECT id FROM playback_sources
            WHERE movie_id=$1
              AND season_number IS NOT DISTINCT FROM $2
              AND episode_number IS NOT DISTINCT FROM $3
              AND voice_name=$4 AND quality=$5
            ORDER BY id DESC LIMIT 1
            """,
            movie_id, season_number, episode_number, voice_name, quality,
        )
        if existing_id:
            await db().execute(
                """
                UPDATE playback_sources
                SET playback_url=$2, source_type=$3, provider='api', is_active=$4
                WHERE id=$1
                """,
                int(existing_id), playback_url, source_type, active,
            )
        else:
            await db().execute(
                """
                INSERT INTO playback_sources(
                    movie_id, season_number, episode_number,
                    voice_name, quality, playback_url, source_type, provider, is_active
                ) VALUES($1,$2,$3,$4,$5,$6,$7,'api',$8)
                """,
                movie_id, season_number, episode_number, voice_name,
                quality, playback_url, source_type, active,
            )
        synced += 1
    return synced


async def video_provider_sync_loop() -> None:
    if not VIDEO_PROVIDER_API_URL:
        logging.info("Automatic video-provider sync is not configured")
        return
    while True:
        try:
            count = await sync_video_provider_once()
            logging.info("Video provider sync complete: %s sources", count)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.exception("Video provider sync failed: %s", exc)
        await asyncio.sleep(VIDEO_PROVIDER_SYNC_SECONDS)


def blender_catalog_title(name: str) -> str | None:
    normalized = name.casefold().strip()
    blocked = (
        "making of",
        "making-of",
        "behind the scenes",
        "behind-the-scenes",
        "trailer",
        "teaser",
        "breakdown",
        "documentary",
    )
    if any(marker in normalized for marker in blocked):
        return None

    for marker, russian_title in BLENDER_OPEN_MOVIE_TITLES:
        if marker in normalized:
            return russian_title
    return None


def blender_synthetic_movie_id(video_id: int) -> int:
    # Negative IDs can never collide with positive Kinopoisk IDs.
    return -2_000_000_000 - int(video_id)


async def sync_blender_open_movies_once() -> int:
    timeout = aiohttp.ClientTimeout(total=45)
    synced_movies = 0

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            BLENDER_OPEN_MOVIES_API,
            headers={"Accept": "application/json"},
        ) as response:
            response.raise_for_status()
            channel_payload = await response.json()

        summaries = (
            channel_payload.get("data", [])
            if isinstance(channel_payload, dict)
            else []
        )

        for summary in summaries:
            if not isinstance(summary, dict):
                continue

            original_title = str(summary.get("name") or "").strip()
            russian_title = blender_catalog_title(original_title)
            if not russian_title:
                continue

            uuid = str(summary.get("uuid") or "").strip()
            if not uuid:
                continue

            try:
                async with session.get(
                    f"{BLENDER_VIDEO_API}/{uuid}",
                    headers={"Accept": "application/json"},
                ) as response:
                    response.raise_for_status()
                    detail = await response.json()
            except Exception as exc:
                logging.info(
                    "Blender detail fetch skipped for %s: %s",
                    uuid,
                    exc,
                )
                continue

            if not isinstance(detail, dict):
                continue

            account = detail.get("account") or {}
            channel = detail.get("channel") or {}
            privacy = detail.get("privacy") or {}

            if (
                not isinstance(account, dict)
                or str(account.get("name") or "") != "blender"
                or not isinstance(channel, dict)
                or str(channel.get("name") or "") != "blender_open_movies"
                or not isinstance(privacy, dict)
                or int(privacy.get("id") or 0) != 1
            ):
                continue

            files = [
                item
                for item in (detail.get("files") or [])
                if isinstance(item, dict)
            ]
            supported_files: list[tuple[int, str]] = []

            for item in files:
                resolution = item.get("resolution") or {}
                if not isinstance(resolution, dict):
                    continue

                try:
                    quality = int(resolution.get("id"))
                except (TypeError, ValueError):
                    continue

                file_url = str(item.get("fileUrl") or "").strip()

                if (
                    quality in {360, 480, 720, 1080}
                    and file_url.startswith("https://")
                    and item.get("hasVideo") is not False
                ):
                    supported_files.append((quality, file_url))

            if not supported_files:
                continue

            peer_id = int(detail.get("id") or 0)
            if peer_id <= 0:
                continue

            movie_id = blender_synthetic_movie_id(peer_id)

            thumbnails = [
                item
                for item in (detail.get("thumbnails") or [])
                if isinstance(item, dict)
                and str(item.get("fileUrl") or "").startswith("https://")
            ]
            poster_url = ""
            if thumbnails:
                thumbnails.sort(
                    key=lambda item: int(item.get("width") or 0),
                    reverse=True,
                )
                poster_url = str(thumbnails[0].get("fileUrl") or "")

            description = str(
                detail.get("description")
                or detail.get("truncatedDescription")
                or ""
            ).strip()

            source_page = str(
                detail.get("url")
                or f"https://video.blender.org/videos/watch/{uuid}"
            )

            licence = detail.get("licence") or {}
            licence_label = (
                str(licence.get("label") or "")
                if isinstance(licence, dict)
                else ""
            )
            if not licence_label or licence_label.casefold() == "unknown":
                licence_label = "Creative Commons Attribution"

            attribution = (
                "Blender Foundation / Blender Studio — "
                "Creative Commons Attribution"
            )

            await db().execute(
                """
                INSERT INTO free_catalog(
                    movie_id,
                    provider,
                    provider_id,
                    title,
                    original_title,
                    description,
                    poster_url,
                    source_page,
                    license_label,
                    attribution,
                    duration_seconds
                )
                VALUES(
                    $1, 'blender', $2, $3, $4, $5,
                    $6, $7, $8, $9, $10
                )
                ON CONFLICT(movie_id) DO UPDATE SET
                    title=EXCLUDED.title,
                    original_title=EXCLUDED.original_title,
                    description=EXCLUDED.description,
                    poster_url=EXCLUDED.poster_url,
                    source_page=EXCLUDED.source_page,
                    license_label=EXCLUDED.license_label,
                    attribution=EXCLUDED.attribution,
                    duration_seconds=EXCLUDED.duration_seconds,
                    updated_at=NOW()
                """,
                movie_id,
                uuid,
                russian_title,
                original_title,
                description,
                poster_url or None,
                source_page,
                licence_label,
                attribution,
                int(detail.get("duration") or 0) or None,
            )

            for quality, file_url in supported_files:
                existing_id = await db().fetchval(
                    """
                    SELECT id
                    FROM playback_sources
                    WHERE movie_id=$1
                      AND season_number IS NULL
                      AND episode_number IS NULL
                      AND voice_name='Original'
                      AND quality=$2
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    movie_id,
                    quality,
                )

                if existing_id:
                    await db().execute(
                        """
                        UPDATE playback_sources
                        SET playback_url=$2,
                            source_type='mp4',
                            language='original',
                            provider='blender',
                            is_active=TRUE
                        WHERE id=$1
                        """,
                        int(existing_id),
                        file_url,
                    )
                else:
                    await db().execute(
                        """
                        INSERT INTO playback_sources(
                            movie_id,
                            season_number,
                            episode_number,
                            voice_name,
                            language,
                            quality,
                            playback_url,
                            source_type,
                            provider,
                            is_active
                        )
                        VALUES(
                            $1, NULL, NULL,
                            'Original', 'original',
                            $2, $3, 'mp4', 'blender', TRUE
                        )
                        """,
                        movie_id,
                        quality,
                        file_url,
                    )

            synced_movies += 1

    return synced_movies


async def blender_open_movies_sync_loop() -> None:
    while True:
        try:
            count = await sync_blender_open_movies_once()
            logging.info(
                "Blender Open Movies sync complete: %s movies",
                count,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.exception(
                "Blender Open Movies sync failed: %s",
                exc,
            )

        await asyncio.sleep(BLENDER_SYNC_SECONDS)


async def premiere_reminder_loop(
    bot: Bot,
) -> None:
    while True:
        try:
            rows = await db().fetch(
                """
                SELECT
                    user_id,
                    movie_id,
                    title,
                    premiere_date
                FROM premiere_reminders
                WHERE sent_at IS NULL
                  AND premiere_date <= CURRENT_DATE
                ORDER BY premiere_date, user_id
                LIMIT 100
                """
            )

            for row in rows:
                user_id = int(row["user_id"])
                movie_id = int(row["movie_id"])
                title = str(row["title"])
                stored_date = row["premiere_date"]

                try:
                    item = await kp.details(movie_id)
                    current_date = premiere_date(item)

                    if (
                        current_date
                        and current_date > date.today()
                    ):
                        await db().execute(
                            """
                            UPDATE premiere_reminders
                            SET premiere_date=$3
                            WHERE user_id=$1
                              AND movie_id=$2
                            """,
                            user_id,
                            movie_id,
                            current_date,
                        )
                        continue

                    release_date = current_date or stored_date

                except Exception:
                    release_date = stored_date

                try:
                    await bot.send_message(
                        user_id,
                        "🎬 <b>Сегодня премьера!</b>\n\n"
                        f"<b>{escape(title)}</b>\n"
                        f"📅 {release_date.strftime('%d.%m.%Y')}\n\n"
                        "Проект уже можно найти через поиск "
                        "или проверить в разделе 🆕 Новинки.",
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [
                                    InlineKeyboardButton(
                                        text="🎬 Открыть карточку",
                                        callback_data=f"open:{movie_id}",
                                    )
                                ]
                            ]
                        ),
                    )

                    await db().execute(
                        """
                        UPDATE premiere_reminders
                        SET sent_at=NOW()
                        WHERE user_id=$1
                          AND movie_id=$2
                        """,
                        user_id,
                        movie_id,
                    )

                except (
                    TelegramForbiddenError,
                    TelegramBadRequest,
                ) as exc:
                    # User blocked the bot or the chat is gone: retrying
                    # would only clog the queue for everyone else.
                    await db().execute(
                        """
                        UPDATE premiere_reminders
                        SET sent_at=NOW()
                        WHERE user_id=$1
                          AND movie_id=$2
                        """,
                        user_id,
                        movie_id,
                    )
                    logging.info(
                        "Premiere reminder dropped "
                        "for user %s movie %s: %s",
                        user_id,
                        movie_id,
                        exc,
                    )

                except Exception as exc:
                    logging.info(
                        "Premiere reminder send failed "
                        "for user %s movie %s: %s",
                        user_id,
                        movie_id,
                        exc,
                    )

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.exception(
                "Premiere reminder loop failed: %s",
                exc,
            )

        await asyncio.sleep(3600)


def admin_menu() -> InlineKeyboardMarkup:
    """Админка: Telegram-канал и YouTube — отдельными ветками, чтобы кнопки не смешивались."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Telegram-канал: посты ▸", callback_data="pt:menu")],
        [InlineKeyboardButton(text="▶️ YouTube и видео: шортсы, выпуски ▸", callback_data="sh:menu")],
        [InlineKeyboardButton(text="📈 Статистика бота", callback_data="st:home"),
         InlineKeyboardButton(text="💵 Расходы", callback_data="ad:spend")],
        [InlineKeyboardButton(text="➕ Фильм", callback_data="admin:add:movie"),
         InlineKeyboardButton(text="➕ Серия", callback_data="admin:add:episode"),
         InlineKeyboardButton(text="📋 Источники", callback_data="admin:list")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="admin:cancel")],
    ])


def premiere_date(item: dict[str, Any]) -> date | None:
    premiere = item.get("premiere") or {}
    if not isinstance(premiere, dict):
        return None

    raw = premiere.get("world")
    if not raw:
        return None

    try:
        return datetime.fromisoformat(
            str(raw).replace("Z", "+00:00")
        ).date()
    except (TypeError, ValueError):
        return None


def is_upcoming(item: dict[str, Any]) -> bool:
    value = premiere_date(item)
    return bool(value and value > date.today())


def premiere_label(item: dict[str, Any]) -> str:
    value = premiere_date(item)
    return value.strftime("%d.%m.%Y") if value else ""


SHORTS_BUTTON = "🎬 Шортсы (админ)"


def main_menu(admin: bool = False) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[*([[KeyboardButton(text=SHORTS_BUTTON)]] if admin else []),
            [KeyboardButton(text="🔎 Найти фильм или сериал")],
            [
                KeyboardButton(text="🆕 Новинки"),
                KeyboardButton(text="🔥 Популярное"),
            ],
            [KeyboardButton(text="🔜 Скоро")],
            [KeyboardButton(text="🆓 Смотреть бесплатно")],
            [KeyboardButton(text="🎲 Что посмотреть?")],
            [KeyboardButton(text="🎯 Подобрать фильм"), KeyboardButton(text="🧩 Викторина дня")],
            [KeyboardButton(text="🎬 Кинодл — угадай фильм"), KeyboardButton(text="🔮 Прогноз уикенда")],
            [KeyboardButton(text="📔 Кинодневник"), KeyboardButton(text="👥 Выбрать вдвоём")],
            [KeyboardButton(text="🎁 Пригласить друга"), KeyboardButton(text="💡 Предложить идею")],
            [KeyboardButton(text="✉️ Написать админу")],
            [
                KeyboardButton(text="🎬 Подборки"),
                KeyboardButton(text="❤️ Избранное"),
            ],
            [
                KeyboardButton(text="🕘 История"),
                KeyboardButton(text="📊 Статистика"),
            ],
            [KeyboardButton(text="👤 Профиль")],
            [KeyboardButton(text="ℹ️ О VKino")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Что будем смотреть? 🍿",
    )


def results_keyboard(
    items: list[dict[str, Any]],
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for item in items:
        title = kp.title(item)
        year = item.get("year") or "—"
        icon = "📺" if item.get("media_type") == "series" else "🎬"

        if is_upcoming(item):
            release = premiere_label(item)
            suffix = f" • 🔜 {release}" if release else " • 🔜 Скоро"
            label = f"{icon} {title} ({year}){suffix}"
        else:
            label = f"{icon} {title} ({year})"

        rows.append(
            [
                InlineKeyboardButton(
                    text=label[:64],
                    callback_data=f"open:{item['id']}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def new_releases_keyboard(
    items: list[dict[str, Any]],
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    for item in items:
        title = kp.title(item)
        icon = (
            "📺"
            if item.get("media_type") == "series"
            else "🎬"
        )

        premiere = item.get("premiere") or {}
        raw_date = (
            premiere.get("world")
            if isinstance(premiere, dict)
            else None
        )

        date_label = ""
        if raw_date:
            try:
                parsed = datetime.fromisoformat(
                    str(raw_date).replace("Z", "+00:00")
                )
                date_label = parsed.strftime("%d.%m")
            except (TypeError, ValueError):
                pass

        label = f"{icon} {title}"
        if date_label:
            label += f" • {date_label}"

        rows.append(
            [
                InlineKeyboardButton(
                    text=label[:60],
                    callback_data=f"open:{item['id']}",
                )
            ]
        )

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


def stored_keyboard(
    items: list[dict[str, Any]],
    icon: str,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{icon} {item['title']}",
                    callback_data=f"open:{item['movie_id']}",
                )
            ]
            for item in items
        ]
    )


async def playback_button_for_movie(
    movie_id: int,
) -> InlineKeyboardButton | None:
    has_source = await db().fetchval(
        """
        SELECT 1
        FROM playback_sources
        WHERE movie_id=$1
          AND season_number IS NULL
          AND episode_number IS NULL
          AND is_active=TRUE
        LIMIT 1
        """,
        movie_id,
    )
    if not has_source:
        return None

    app_url = build_webapp_url(movie_id)
    if app_url:
        return InlineKeyboardButton(
            text="▶️ Смотреть в VKino",
            web_app=WebAppInfo(url=app_url),
        )

    return InlineKeyboardButton(
        text="▶️ Смотреть в VKino",
        callback_data=f"playvoices:{movie_id}:0:0",
    )


def card_keyboard(
    item: dict[str, Any],
    favorite: bool,
) -> InlineKeyboardMarkup:
    media_type = item.get("media_type", "movie")
    movie_id = int(item["id"])
    rows: list[list[InlineKeyboardButton]] = []

    if is_upcoming(item):
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔔 Напомнить о премьере",
                    callback_data=f"premiereremind:{movie_id}",
                )
            ]
        )

    if media_type == "series":
        rows.append(
            [
                InlineKeyboardButton(
                    text="📺 Сезоны и серии",
                    callback_data=f"seasons:{movie_id}",
                )
            ]
        )

    trailer_app_url = build_trailer_url(movie_id)
    if trailer_app_url:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🎞 Трейлер",
                    web_app=WebAppInfo(url=trailer_app_url),
                )
            ]
        )
    else:
        trailer = kp.trailer(item)
        if trailer:
            rows.append(
                [InlineKeyboardButton(text="🎞 Трейлер", url=trailer)]
            )

    rows.append(
        [
            InlineKeyboardButton(
                text="▶️ Где смотреть",
                callback_data=f"watch:{movie_id}",
            )
        ]
    )
    if not is_upcoming(item):  # оценка для «Топа по версии подписчиков»
        rows.append([InlineKeyboardButton(text="👍 Понравилось", callback_data=f"rate:{movie_id}:1"),
                     InlineKeyboardButton(text="👎 Не моё", callback_data=f"rate:{movie_id}:-1")])
        rows.append([InlineKeyboardButton(text="✅ Посмотрел — в кинодневник", callback_data=f"dia:w:{movie_id}")])
    rows.append(
        [
            InlineKeyboardButton(
                text=(
                    "💔 Убрать из избранного"
                    if favorite
                    else "❤️ В избранное"
                ),
                callback_data=(
                    f"{'favdel' if favorite else 'favadd'}:"
                    f"{media_type}:{movie_id}"
                ),
            )
        ]
    )
    if BOT_USERNAME:  # друг по ссылке сразу открывает этот фильм в боте (и подписывается на канал)
        link = f"https://t.me/{BOT_USERNAME}?start=f_{movie_id}"
        share = (f"https://t.me/share/url?url={quote(link)}&text="
                 + quote(f"«{kp.title(item)}» — описание, трейлер и где посмотреть 🍿"))
        rows.append([InlineKeyboardButton(text="📤 Поделиться с другом", url=share)])
    rows.append(
        [
            InlineKeyboardButton(
                text="🔎 Найти ещё",
                callback_data="search_again",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def season_list_keyboard(
    movie_id: int,
    seasons: list[dict[str, Any]],
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    for season in seasons[:30]:
        try:
            season_number = int(season.get("number"))
        except (TypeError, ValueError):
            continue

        episodes = season.get("episodes") or []
        count = season.get("episodesCount")
        try:
            count_value = (
                int(count)
                if count is not None
                else len(episodes)
            )
        except (TypeError, ValueError):
            count_value = len(episodes)

        title = f"Сезон {season_number}"
        if count_value:
            title += f" • {count_value} серий"

        rows.append(
            [
                InlineKeyboardButton(
                    text=title,
                    callback_data=(
                        f"episodes:{movie_id}:{season_number}:0"
                    ),
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ К сериалу",
                callback_data=f"open:{movie_id}",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def episodes_keyboard(
    movie_id: int,
    season_number: int,
    episodes: list[dict[str, Any]],
    page: int = 0,
) -> InlineKeyboardMarkup:
    per_page = 8
    valid: list[tuple[int, dict[str, Any]]] = []

    for episode in episodes:
        if not isinstance(episode, dict):
            continue
        try:
            number = int(episode.get("number"))
        except (TypeError, ValueError):
            continue
        valid.append((number, episode))

    valid.sort(key=lambda item: item[0])

    total_pages = max(
        1,
        (len(valid) + per_page - 1) // per_page,
    )
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    current = valid[start:start + per_page]

    rows: list[list[InlineKeyboardButton]] = []

    for number, episode in current:
        name = str(
            episode.get("name")
            or episode.get("enName")
            or ""
        ).strip()

        if len(name) > 32:
            name = name[:29].rstrip() + "…"

        label = f"{number} серия"
        if name:
            label += f" — {name}"

        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=(
                        f"episode:{movie_id}:"
                        f"{season_number}:{number}"
                    ),
                )
            ]
        )

    nav: list[InlineKeyboardButton] = []

    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=(
                    f"episodes:{movie_id}:"
                    f"{season_number}:{page - 1}"
                ),
            )
        )

    if page + 1 < total_pages:
        nav.append(
            InlineKeyboardButton(
                text="➡️",
                callback_data=(
                    f"episodes:{movie_id}:"
                    f"{season_number}:{page + 1}"
                ),
            )
        )

    if nav:
        rows.append(nav)

    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ К сезонам",
                callback_data=f"seasons:{movie_id}",
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_card(item: dict[str, Any]) -> str:
    rating = item.get("rating") or {}
    genres = ", ".join(
        x.get("name", "")
        for x in (item.get("genres") or [])[:4]
        if x.get("name")
    )
    countries = ", ".join(
        x.get("name", "")
        for x in (item.get("countries") or [])[:3]
        if x.get("name")
    )

    description = (
        item.get("description")
        or item.get("shortDescription")
        or "Описание пока отсутствует."
    ).strip()

    if len(description) > 850:
        description = description[:847].rstrip() + "…"

    kind = (
        "Сериал"
        if item.get("media_type") == "series"
        else "Фильм"
    )

    lines = [
        f"<b>{escape(kp.title(item))}</b>",
        f"{kind} • {item.get('year') or '—'}",
        (
            f"⭐ Кинопоиск: "
            f"<b>{float(rating.get('kp')):.1f}</b>/10"
            if rating.get("kp")
            else "⭐ Кинопоиск: —"
        ),
    ]

    if rating.get("imdb"):
        lines.append(
            f"⭐ IMDb: <b>{float(rating['imdb']):.1f}</b>/10"
        )

    release = premiere_label(item)
    if release:
        if is_upcoming(item):
            lines.append("🔜 <b>Ещё не вышел</b>")
            lines.append(f"📅 Премьера: <b>{release}</b>")
        else:
            lines.append(f"📅 Премьера: {release}")

    if genres:
        lines.append(f"🎭 {escape(genres)}")

    if countries:
        lines.append(f"🌍 {escape(countries)}")

    if item.get("ageRating"):
        lines.append(f"🔞 {item['ageRating']}+")

    lines += ["", escape(description)]
    return "\n".join(lines)


async def rutube_watch_button(
    item: dict[str, Any],
) -> InlineKeyboardButton | None:
    try:
        found = await rutube_movie(item)
    except Exception as exc:
        logging.warning(
            "Rutube lookup failed for movie %s: %s",
            item.get("id"),
            exc,
        )
        return None
    if not found:
        return None

    app_url = build_rutube_url(int(item["id"]))
    if app_url:
        return InlineKeyboardButton(
            text="▶️ Смотреть бесплатно (Rutube)",
            web_app=WebAppInfo(url=app_url),
        )
    return InlineKeyboardButton(
        text="▶️ Смотреть бесплатно (Rutube)",
        url=f"https://rutube.ru/video/{found['video_id']}/",
    )


async def send_card(
    message: Message,
    item: dict[str, Any],
    user_id: int,
) -> None:
    media_type = item.get("media_type", "movie")
    movie_id = int(item["id"])
    title = kp.title(item)

    await record_history(
        user_id,
        media_type,
        movie_id,
        title,
    )
    await record_activity(
        user_id,
        media_type,
        movie_id,
        title,
        "open",
    )

    favorite = await is_favorite(
        user_id,
        media_type,
        movie_id,
    )

    poster = kp.poster(item)
    if not BOT_USERNAME:
        try:
            await _bot_link(message.bot)
        except Exception:  # noqa: BLE001 — без кнопки «Поделиться»
            pass

    keyboard = card_keyboard(
        item,
        favorite,
    )

    if media_type == "movie" and not is_upcoming(item):
        play_button = await playback_button_for_movie(
            movie_id
        )
        if play_button:
            keyboard.inline_keyboard.insert(
                0,
                [play_button],
            )
        else:
            rutube_button = await rutube_watch_button(item)
            if rutube_button:
                keyboard.inline_keyboard.insert(
                    0,
                    [rutube_button],
                )

    if poster:
        try:
            await message.answer_photo(
                poster,
                caption=format_card(item),
                reply_markup=keyboard,
            )
            return
        except Exception:
            pass

    await message.answer(
        format_card(item),
        reply_markup=keyboard,
    )


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    state: FSMContext,
) -> None:
    await state.clear()
    await remember(message)
    arg = (message.text or "").partition(" ")[2].strip()
    if arg.startswith("ref_") and message.from_user:  # пришёл по ссылке друга
        await save_referral(message.from_user.id, arg)
    if arg == "pick":  # кнопка из поста «Рекомендация: что посмотреть»
        await message.answer("🎬 Привет! Внизу — меню бота, а сейчас подберём фильм 👇",
                             reply_markup=main_menu(bool(message.from_user and message.from_user.id in ADMIN_IDS)))
        text, markup = rq_screen("-", "-", "-", "-")
        await message.answer(text, reply_markup=markup)
        return
    if arg == "contact":  # ссылка «связь с админом» (?start=contact) — например, из закрепа канала
        await contact_ask(message, state)
        return
    if arg.startswith("duo_") and message.from_user:  # друг позвал выбрать фильм вместе
        await message.answer("👥 Друг зовёт выбрать фильм вместе! Отмечай 👍 то, что готов смотреть, — "
                             "в конце покажу совпадения.",
                             reply_markup=main_menu(message.from_user.id in ADMIN_IDS))
        await duo_show(message.bot, message.chat.id, message.from_user.id, arg[4:], None)
        return
    if arg.startswith("f_") and arg[2:].isdigit() and message.from_user:  # друг поделился фильмом
        await track(message.from_user.id, "share_open")
        await message.answer("🍿 Друг поделился с тобой фильмом 👇",
                             reply_markup=main_menu(message.from_user.id in ADMIN_IDS))
        try:
            await send_card(message, await kp.details(int(arg[2:])), message.from_user.id)
        except Exception:  # noqa: BLE001
            logging.exception("Shared film open failed")
            await message.answer("Не получилось открыть фильм — найди его через «🔎 Найти фильм или сериал».")
        return
    if arg == "kinodl":
        await message.answer("🎬 Кинодл — фильм дня за 6 подсказок 👇",
                             reply_markup=main_menu(bool(message.from_user and message.from_user.id in ADMIN_IDS)))
        await kinodl_menu(message, state)
        return
    if arg == "quiz":  # кнопка из поста «Знатоки месяца»
        await message.answer("🧩 Викторина дня — жми кнопку в меню или держи кадр 👇",
                             reply_markup=main_menu(bool(message.from_user and message.from_user.id in ADMIN_IDS)))
        await quiz_day_menu(message)
        return

    name = escape(
        message.from_user.first_name
        if message.from_user
        else "друг"
    )

    await message.answer(
        f"🎬 <b>Привет, {name}! Это VKino.</b>\n\n"
        "🔎 Поиск вышедших и будущих фильмов/сериалов\n"
        "🔜 Календарь будущих премьер\n"
        "⭐ Рейтинги Кинопоиска и IMDb\n"
        "🎞 Трейлеры и описания\n"
        "🍿 Подборки и рекомендации",
        reply_markup=main_menu(bool(message.from_user and message.from_user.id in ADMIN_IDS)),
    )


@router.message(F.text == SHORTS_BUTTON)
async def shorts_button(message: Message, state: FSMContext) -> None:
    await shorts_panel_command(message, state)


@router.message(F.text == "🔎 Найти фильм или сериал")
async def ask_search(
    message: Message,
    state: FSMContext,
) -> None:
    await remember(message)
    await state.set_state(SearchState.query)
    await message.answer(
        "Напиши название фильма или сериала 👇\n\n<i>Не помнишь название? Опиши сюжет — найду по описанию.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🤔 Не помню название — опишу сюжет", callback_data="fd:ask")]]),
    )


@router.callback_query(F.data == "search_again")
async def search_again(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    await state.set_state(SearchState.query)
    await callback.answer()

    if callback.message:
        await callback.message.answer(
            "Напиши название 👇"
        )


@router.message(SearchState.query, F.text)
async def do_search(
    message: Message,
    state: FSMContext,
) -> None:
    query = (message.text or "").strip()

    if query in MAIN_MENU_LABELS:
        if query == "🔎 Найти фильм или сериал":
            await message.answer(
                "Напиши название фильма или сериала 👇"
            )
            return
        if query == "✉️ Написать админу":
            await contact_ask(message, state)
            return
        if query == "💡 Предложить идею":
            await idea_ask(message, state)
            return

        await state.clear()
        target_handlers = {
            "🆕 Новинки": "new_releases_menu",
            "🔥 Популярное": "popular",
            "🔜 Скоро": "upcoming_menu",
            "🆓 Смотреть бесплатно": "free_catalog_menu",
            "🎲 Что посмотреть?": "random_movie",
            "🎬 Подборки": "collections",
            "❤️ Избранное": "favorites_menu",
            "⭐ Избранное": "favorites_menu",
            "🕘 История": "history_menu",
            "📊 Статистика": "statistics_menu",
            "👤 Профиль": "profile",
            "ℹ️ О VKino": "about",
            "🎯 Подобрать фильм": "rq_menu",
            "🧩 Викторина дня": "quiz_day_menu",
            "🎁 Пригласить друга": "referral_menu",
            "📔 Кинодневник": "diary_menu",
            "👥 Выбрать вдвоём": "duo_new",
            "🎬 Кинодл — угадай фильм": "kinodl_menu",
            "🔮 Прогноз уикенда": "predict_menu",
        }
        handler_name = target_handlers.get(query)
        handler = globals().get(handler_name or "")
        if handler_name == "kinodl_menu":  # игре нужно состояние — ждёт ответы сообщениями
            await kinodl_menu(message, state)
        elif callable(handler):
            await handler(message)
        else:
            await message.answer(
                "Режим поиска закрыт.",
                reply_markup=main_menu(),
            )
        return

    if len(query) < 2:
        await message.answer(
            "Напиши хотя бы 2 символа."
        )
        return

    if message.from_user:
        await record_search(
            message.from_user.id,
            query,
        )

    try:
        items = await kp.search(query)
    except Exception as exc:
        logging.exception(
            "Search failed: %s",
            exc,
        )
        await message.answer(
            "Не удалось связаться с каталогом. "
            "Попробуй позже."
        )
        return

    if not items:
        if len(query.split()) >= 5:  # похоже на описание сюжета, а не на название
            await state.clear()
            await find_by_description(message, query)
            return
        await message.answer(
            "Ничего не нашёл 😕 "
            "Попробуй другое название — или опиши сюжет.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🤔 Опишу сюжет", callback_data="fd:ask")]]),
        )
        return

    await state.clear()

    await message.answer(
        "Вот что нашёл:",
        reply_markup=results_keyboard(items),
    )


@router.callback_query(F.data == "fd:ask")
async def describe_ask(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SearchState.describe)
    if callback.message:
        await callback.message.answer(
            "🤔 Опиши, что помнишь: сюжет, сцены, актёров, примерный год. Например: «фильм, где мужик застрял в "
            "одном дне и каждый раз просыпается под одну песню».")


@router.message(SearchState.describe, F.text, ~F.text.startswith("/"))
async def describe_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text in MAIN_MENU_LABELS:
        await state.clear()
        await message.answer("Поиск по описанию закрыт.", reply_markup=main_menu())
        return
    await state.clear()
    await find_by_description(message, text)


async def find_by_description(message: Message, text: str) -> None:
    """Фильм по описанию сюжета: ИИ называет до 3 вариантов, бот находит их в каталоге и даёт карточки."""
    if len(text) < 15:
        await message.answer("Маловато 🙂 Опиши подробнее: что происходит, кто играет, когда примерно вышел.")
        return
    wait = await message.answer("🔎 Вспоминаю фильм по описанию…")
    guesses = await shorts_ai.guess_films(GEMINI_API_KEY, text)
    found: list[dict[str, Any]] = []
    notes: list[str] = []
    for g in guesses:
        for q in [x for x in (g["ru"], g["en"]) if x]:
            try:
                items = await kp.search(q, limit=5)
            except Exception:  # noqa: BLE001
                items = []
            year = g.get("year")
            hit = next((it for it in items if not year or abs(int(it.get("year") or 0) - year) <= 1), None)
            if hit and all(int(hit["id"]) != int(f["id"]) for f in found):
                found.append(hit)
                notes.append(f"• «{escape(kp.title(hit))}»" + (f" ({hit.get('year')})" if hit.get("year") else "")
                             + (f" — {escape(g['why'])}" if g.get("why") else ""))
                break
    await wait.delete()
    if not found:
        await message.answer("😕 Не узнал фильм по описанию. Попробуй добавить детали: актёров, страну, год, "
                             "запоминающуюся сцену.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [InlineKeyboardButton(text="🤔 Описать ещё раз", callback_data="fd:ask")]]))
        return
    await message.answer("🎯 Похоже, это:\n" + "\n".join(notes) + "\n\nНажми, чтобы открыть карточку 👇",
                         reply_markup=results_keyboard(found))


@router.callback_query(F.data.startswith("open:"))
async def open_movie(
    callback: CallbackQuery,
) -> None:
    await callback.answer()

    try:
        item = await kp.details(
            int(callback.data.split(":", 1)[1])
        )

        if callback.message:
            await send_card(
                callback.message,
                item,
                callback.from_user.id,
            )

    except Exception as exc:
        logging.exception(
            "Open movie failed: %s",
            exc,
        )

        if callback.message:
            await callback.message.answer(
                "Не удалось открыть карточку."
            )


@router.callback_query(
    F.data.startswith("premiereremind:")
)
async def premiere_remind(
    callback: CallbackQuery,
) -> None:
    if not callback.message:
        await callback.answer()
        return

    try:
        movie_id = int(
            callback.data.split(":", 1)[1]
        )
        item = await kp.details(movie_id)
        release_date = premiere_date(item)

        if not release_date:
            await callback.answer(
                "Дата премьеры пока неизвестна",
                show_alert=True,
            )
            return

        if release_date <= date.today():
            await callback.answer(
                "Проект уже вышел",
                show_alert=True,
            )
            return

        title = kp.title(item)

        existing = await db().fetchval(
            """
            SELECT 1
            FROM premiere_reminders
            WHERE user_id=$1
              AND movie_id=$2
              AND sent_at IS NULL
            """,
            callback.from_user.id,
            movie_id,
        )

        await db().execute(
            """
            INSERT INTO premiere_reminders(
                user_id,
                movie_id,
                title,
                premiere_date,
                sent_at
            )
            VALUES($1,$2,$3,$4,NULL)
            ON CONFLICT(user_id, movie_id)
            DO UPDATE SET
                title=EXCLUDED.title,
                premiere_date=EXCLUDED.premiere_date,
                sent_at=NULL
            """,
            callback.from_user.id,
            movie_id,
            title,
            release_date,
        )

        if existing:
            await callback.answer(
                f"🔔 Уже напомню {release_date.strftime('%d.%m.%Y')}",
                show_alert=True,
            )
        else:
            await callback.answer(
                f"🔔 Напомню {release_date.strftime('%d.%m.%Y')}",
                show_alert=True,
            )

    except Exception as exc:
        logging.exception(
            "Premiere reminder failed: %s",
            exc,
        )
        await callback.answer(
            "Не удалось сохранить напоминание",
            show_alert=True,
        )


@router.callback_query(F.data.startswith("seasons:"))
async def show_seasons(
    callback: CallbackQuery,
) -> None:
    await callback.answer()

    if not callback.message:
        return

    try:
        movie_id = int(
            callback.data.split(":", 1)[1]
        )
        seasons = await kp.seasons(movie_id)

    except Exception as exc:
        logging.exception(
            "Load seasons failed: %s",
            exc,
        )
        await callback.message.answer(
            "Не удалось загрузить сезоны."
        )
        return

    if not seasons:
        await callback.message.answer(
            "Данные о сезонах пока не найдены."
        )
        return

    await callback.message.answer(
        "📺 <b>Выбери сезон:</b>",
        reply_markup=season_list_keyboard(
            movie_id,
            seasons,
        ),
    )


@router.callback_query(F.data.startswith("episodes:"))
async def show_episodes(
    callback: CallbackQuery,
) -> None:
    await callback.answer()

    if not callback.message:
        return

    try:
        _, raw_movie_id, raw_season, raw_page = (
            callback.data.split(":", 3)
        )

        movie_id = int(raw_movie_id)
        season_number = int(raw_season)
        page = int(raw_page)

        season = await kp.season(
            movie_id,
            season_number,
        )

    except Exception as exc:
        logging.exception(
            "Load episodes failed: %s",
            exc,
        )
        await callback.message.answer(
            "Не удалось загрузить серии."
        )
        return

    if not season:
        await callback.message.answer(
            "Сезон не найден."
        )
        return

    episodes = [
        item
        for item in (season.get("episodes") or [])
        if isinstance(item, dict)
    ]

    if not episodes:
        await callback.message.answer(
            "Для этого сезона список серий "
            "пока отсутствует."
        )
        return

    text = (
        f"📺 <b>Сезон {season_number}</b> — "
        f"{len(episodes)} серий"
    )

    keyboard = episodes_keyboard(
        movie_id,
        season_number,
        episodes,
        page,
    )

    try:
        await callback.message.edit_text(
            text,
            reply_markup=keyboard,
        )
    except Exception:
        await callback.message.answer(
            text,
            reply_markup=keyboard,
        )


@router.callback_query(F.data.startswith("episode:"))
async def show_episode(
    callback: CallbackQuery,
) -> None:
    await callback.answer()

    if not callback.message:
        return

    try:
        _, raw_movie_id, raw_season, raw_episode = (
            callback.data.split(":", 3)
        )

        movie_id = int(raw_movie_id)
        season_number = int(raw_season)
        episode_number = int(raw_episode)

        season = await kp.season(
            movie_id,
            season_number,
        )

    except Exception as exc:
        logging.exception(
            "Load episode failed: %s",
            exc,
        )
        await callback.message.answer(
            "Не удалось загрузить серию."
        )
        return

    if not season:
        await callback.message.answer(
            "Сезон не найден."
        )
        return

    episode: dict[str, Any] | None = None

    for item in season.get("episodes") or []:
        if not isinstance(item, dict):
            continue

        try:
            if int(item.get("number")) == episode_number:
                episode = item
                break
        except (TypeError, ValueError):
            continue

    if not episode:
        await callback.message.answer(
            "Серия не найдена."
        )
        return

    name = str(
        episode.get("name")
        or episode.get("enName")
        or ""
    ).strip()

    air_date = str(
        episode.get("airDate")
        or episode.get("date")
        or ""
    ).strip()

    description = str(
        episode.get("description")
        or episode.get("enDescription")
        or "Описание пока отсутствует."
    ).strip()

    if len(description) > 1000:
        description = (
            description[:997].rstrip() + "…"
        )

    lines = [
        (
            f"<b>Сезон {season_number} • "
            f"Серия {episode_number}</b>"
        )
    ]

    if name:
        lines.append(
            f"🎬 {escape(name)}"
        )

    if air_date:
        lines.append(
            f"📅 {escape(air_date)}"
        )

    lines += [
        "",
        escape(description),
    ]

    text = "\n".join(lines)

    episode_rows: list[list[InlineKeyboardButton]] = []

    if await playback_voices(
        movie_id,
        season_number,
        episode_number,
    ):
        episode_rows.append(
            [
                InlineKeyboardButton(
                    text="▶️ Смотреть серию",
                    web_app=(
                        WebAppInfo(
                            url=build_webapp_url(
                                movie_id,
                                season_number,
                                episode_number,
                            )
                        )
                        if WEBAPP_URL
                        else None
                    ),
                    callback_data=(
                        None
                        if WEBAPP_URL
                        else (
                            f"playvoices:{movie_id}:"
                            f"{season_number}:{episode_number}"
                        )
                    ),
                )
            ]
        )

    episode_rows.extend(
        [
            [
                InlineKeyboardButton(
                    text="⬅️ К сериям",
                    callback_data=(
                        f"episodes:{movie_id}:"
                        f"{season_number}:0"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ К сезонам",
                    callback_data=(
                        f"seasons:{movie_id}"
                    ),
                )
            ],
        ]
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=episode_rows
    )

    still = episode.get("still") or {}
    image_url = None

    if isinstance(still, dict):
        image_url = (
            still.get("url")
            or still.get("previewUrl")
        )

    if image_url:
        try:
            await callback.message.answer_photo(
                str(image_url),
                caption=text,
                reply_markup=keyboard,
            )
            return
        except Exception as exc:
            logging.info(
                "Episode still send failed: %s",
                exc,
            )

    await callback.message.answer(
        text,
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("playvoices:"))
async def play_voice_menu(
    callback: CallbackQuery,
) -> None:
    await callback.answer()

    if not callback.message:
        return

    try:
        _, raw_movie, raw_season, raw_episode = (
            callback.data.split(":", 3)
        )
        movie_id = int(raw_movie)
        season_number = int(raw_season) or None
        episode_number = int(raw_episode) or None

        rows = await db().fetch(
            """
            SELECT
                MIN(id) AS source_id,
                voice_name
            FROM playback_sources
            WHERE movie_id=$1
              AND season_number IS NOT DISTINCT FROM $2
              AND episode_number IS NOT DISTINCT FROM $3
              AND is_active=TRUE
            GROUP BY voice_name
            ORDER BY voice_name
            """,
            movie_id,
            season_number,
            episode_number,
        )

    except Exception as exc:
        logging.exception(
            "Load playback voices failed: %s",
            exc,
        )
        await callback.message.answer(
            "Не удалось загрузить варианты озвучки."
        )
        return

    if not rows:
        await callback.message.answer(
            "Для этого видео пока нет доступных источников."
        )
        return

    keyboard_rows: list[list[InlineKeyboardButton]] = []

    for row in rows:
        source_id = int(row["source_id"])
        voice_name = str(row["voice_name"])

        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text=f"🎙 {voice_name}",
                    callback_data=(
                        f"playquality:{source_id}:"
                        f"{movie_id}:"
                        f"{season_number or 0}:"
                        f"{episode_number or 0}"
                    ),
                )
            ]
        )

    await callback.message.answer(
        "🎙 <b>Выбери озвучку:</b>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=keyboard_rows
        ),
    )


@router.callback_query(F.data.startswith("playquality:"))
async def play_quality_menu(
    callback: CallbackQuery,
) -> None:
    await callback.answer()

    if not callback.message:
        return

    try:
        (
            _,
            raw_source_id,
            raw_movie,
            raw_season,
            raw_episode,
        ) = callback.data.split(":", 4)

        source_id = int(raw_source_id)
        movie_id = int(raw_movie)
        season_number = int(raw_season) or None
        episode_number = int(raw_episode) or None

        voice_name = await db().fetchval(
            """
            SELECT voice_name
            FROM playback_sources
            WHERE id=$1
            """,
            source_id,
        )

        if not voice_name:
            raise ValueError("Voice not found")

        qualities = await playback_qualities(
            movie_id,
            str(voice_name),
            season_number,
            episode_number,
        )

    except Exception as exc:
        logging.exception(
            "Load playback qualities failed: %s",
            exc,
        )
        await callback.message.answer(
            "Не удалось загрузить качество видео."
        )
        return

    if not qualities:
        await callback.message.answer(
            "Доступные качества пока не найдены."
        )
        return

    rows: list[list[InlineKeyboardButton]] = []

    for quality in qualities:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📺 {quality}p",
                    callback_data=(
                        f"playopen:{source_id}:"
                        f"{quality}:"
                        f"{movie_id}:"
                        f"{season_number or 0}:"
                        f"{episode_number or 0}"
                    ),
                )
            ]
        )

    await callback.message.answer(
        f"🎙 <b>{escape(str(voice_name))}</b>\n"
        "📺 Выбери качество:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=rows
        ),
    )


@router.callback_query(F.data.startswith("playopen:"))
async def play_open(
    callback: CallbackQuery,
) -> None:
    await callback.answer()

    if not callback.message:
        return

    try:
        (
            _,
            raw_source_id,
            raw_quality,
            raw_movie,
            raw_season,
            raw_episode,
        ) = callback.data.split(":", 5)

        source_id = int(raw_source_id)
        quality = int(raw_quality)
        movie_id = int(raw_movie)
        season_number = int(raw_season) or None
        episode_number = int(raw_episode) or None

        voice_name = await db().fetchval(
            """
            SELECT voice_name
            FROM playback_sources
            WHERE id=$1
            """,
            source_id,
        )

        if not voice_name:
            raise ValueError("Voice not found")

        url = await playback_url(
            movie_id,
            str(voice_name),
            quality,
            season_number,
            episode_number,
        )

        if not url:
            raise ValueError("Playback URL not found")

        await save_playback_preference(
            callback.from_user.id,
            str(voice_name),
            quality,
        )

    except Exception as exc:
        logging.exception(
            "Open playback failed: %s",
            exc,
        )
        await callback.message.answer(
            "Источник видео сейчас недоступен."
        )
        return

    await callback.message.answer(
        f"🎙 <b>{escape(str(voice_name))}</b>\n"
        f"📺 <b>{quality}p</b>\n\n"
        "Выбор сохранён в профиле.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"▶️ Смотреть • {quality}p",
                        url=url,
                    )
                ]
            ]
        ),
    )


@router.callback_query(F.data.startswith("favadd:"))
async def fav_add(
    callback: CallbackQuery,
) -> None:
    _, media_type, raw_id = (
        callback.data.split(":", 2)
    )

    item = await kp.details(
        int(raw_id)
    )

    await add_favorite(
        callback.from_user.id,
        media_type,
        int(raw_id),
        kp.title(item),
    )

    await callback.answer(
        "Добавлено ⭐"
    )

    if callback.message:
        await callback.message.edit_reply_markup(
            reply_markup=card_keyboard(
                item,
                True,
            )
        )


@router.callback_query(F.data.startswith("favdel:"))
async def fav_del(
    callback: CallbackQuery,
) -> None:
    _, media_type, raw_id = (
        callback.data.split(":", 2)
    )

    await delete_favorite(
        callback.from_user.id,
        media_type,
        int(raw_id),
    )

    await callback.answer(
        "Удалено"
    )

    if callback.message:
        item = await kp.details(
            int(raw_id)
        )

        await callback.message.edit_reply_markup(
            reply_markup=card_keyboard(
                item,
                False,
            )
        )


@router.callback_query(F.data.startswith("watch:"))
async def watch(
    callback: CallbackQuery,
) -> None:
    await callback.answer()

    if not callback.message:
        return

    try:
        item = await kp.details(
            int(callback.data.split(":", 1)[1])
        )
        links = kp.watch_links(item)

    except Exception:
        links = []

    # свой бот — всегда первым, даже если Кинопоиск площадок не знает
    bot_name = BOT_URL.rstrip("/").rsplit("/", 1)[-1]
    text = (
        "▶️ <b>Где посмотреть:</b>\n"
        f'• 🤖 <a href="{escape(BOT_URL)}">@{escape(bot_name)}</a> — наш бот\n'
        + "\n".join(
            f'• <a href="{escape(url)}">'
            f"{escape(name)}</a>"
            for name, url in links[:8]
        )
    )

    await callback.message.answer(
        text,
        disable_web_page_preview=True,
    )


@router.message(F.text == "🆓 Смотреть бесплатно")
async def free_catalog_menu(
    message: Message,
) -> None:
    await remember(message)

    rows = await db().fetch(
        """
        SELECT movie_id, title, original_title
        FROM free_catalog
        ORDER BY title
        LIMIT 40
        """
    )

    if not rows:
        await message.answer(
            "🆓 Каталог открытых фильмов ещё синхронизируется. "
            "Попробуй через минуту."
        )
        return

    keyboard_rows: list[list[InlineKeyboardButton]] = []

    for row in rows:
        title = str(row["title"])
        original_title = str(row["original_title"])
        label = title
        if original_title.casefold() != title.casefold():
            label += f" / {original_title}"

        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text=f"🎬 {label}"[:60],
                    callback_data=f"freeopen:{int(row['movie_id'])}",
                )
            ]
        )

    await message.answer(
        "🆓 <b>Смотреть бесплатно и легально</b>\n\n"
        "Официальные Blender Open Movies. "
        "Фильмы распространяются по открытым лицензиям "
        "Creative Commons Attribution.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=keyboard_rows
        ),
    )


@router.callback_query(F.data.startswith("freeopen:"))
async def free_catalog_open(
    callback: CallbackQuery,
) -> None:
    await callback.answer()

    if not callback.message:
        return

    try:
        movie_id = int(
            callback.data.split(":", 1)[1]
        )
    except ValueError:
        return

    row = await db().fetchrow(
        """
        SELECT
            movie_id,
            title,
            original_title,
            description,
            poster_url,
            source_page,
            license_label,
            attribution,
            duration_seconds
        FROM free_catalog
        WHERE movie_id=$1
        """,
        movie_id,
    )

    if not row:
        await callback.message.answer(
            "Фильм не найден в бесплатном каталоге."
        )
        return

    raw_title = str(row["title"])
    await record_history(
        callback.from_user.id,
        "movie",
        movie_id,
        raw_title,
    )
    await record_activity(
        callback.from_user.id,
        "movie",
        movie_id,
        raw_title,
        "open",
    )

    title = escape(raw_title)
    original_title = escape(str(row["original_title"]))

    description = str(row["description"] or "").strip()
    if len(description) > 850:
        description = description[:847].rstrip() + "…"

    duration = row["duration_seconds"]
    duration_text = ""
    if duration:
        minutes = max(1, int(duration) // 60)
        duration_text = f"\n⏱ {minutes} мин."

    text = (
        f"<b>{title}</b>\n"
        f"🎬 {original_title}"
        f"{duration_text}\n\n"
        f"{escape(description) if description else 'Открытый фильм Blender Studio.'}"
        "\n\n"
        f"🆓 <b>{escape(str(row['license_label']))}</b>\n"
        f"© {escape(str(row['attribution']))}"
    )

    play_button = await playback_button_for_movie(
        movie_id
    )

    buttons: list[list[InlineKeyboardButton]] = []

    if play_button:
        buttons.append([play_button])

    source_page = str(row["source_page"] or "")
    if source_page.startswith("https://"):
        buttons.append(
            [
                InlineKeyboardButton(
                    text="ℹ️ Источник и лицензия",
                    url=source_page,
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text="⬅️ К бесплатным фильмам",
                callback_data="free:list",
            )
        ]
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=buttons
    )

    poster_url = str(row["poster_url"] or "")
    if poster_url.startswith("https://"):
        try:
            await callback.message.answer_photo(
                poster_url,
                caption=text,
                reply_markup=keyboard,
            )
            return
        except Exception:
            pass

    await callback.message.answer(
        text,
        reply_markup=keyboard,
    )


@router.callback_query(F.data == "free:list")
async def free_catalog_back(
    callback: CallbackQuery,
) -> None:
    await callback.answer()

    if not callback.message:
        return

    rows = await db().fetch(
        """
        SELECT movie_id, title, original_title
        FROM free_catalog
        ORDER BY title
        LIMIT 40
        """
    )

    keyboard_rows: list[list[InlineKeyboardButton]] = []

    for row in rows:
        title = str(row["title"])
        original_title = str(row["original_title"])
        label = title
        if original_title.casefold() != title.casefold():
            label += f" / {original_title}"

        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text=f"🎬 {label}"[:60],
                    callback_data=f"freeopen:{int(row['movie_id'])}",
                )
            ]
        )

    await callback.message.answer(
        "🆓 <b>Смотреть бесплатно и легально</b>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=keyboard_rows
        ),
    )


@router.message(F.text == "🔜 Скоро")
async def upcoming_menu(
    message: Message,
) -> None:
    await remember(message)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎬 Будущие фильмы",
                    callback_data="upcoming:movies",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📺 Будущие сериалы",
                    callback_data="upcoming:series",
                )
            ],
        ]
    )

    await message.answer(
        "🔜 <b>Скоро выйдет</b>\n\n"
        "Будущие премьеры на ближайшие два года. "
        "Выбери категорию:",
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("upcoming:"))
async def upcoming_list(
    callback: CallbackQuery,
) -> None:
    await callback.answer()

    if not callback.message:
        return

    kind = callback.data.split(":", 1)[1]
    series = kind == "series"

    try:
        items = await kp.upcoming_releases(
            series=series,
            limit=20,
        )
    except Exception as exc:
        logging.exception(
            "Load upcoming releases failed: %s",
            exc,
        )
        await callback.message.answer(
            "Не удалось загрузить будущие премьеры."
        )
        return

    if not items:
        await callback.message.answer(
            "Будущих премьер пока не найдено."
        )
        return

    title = (
        "📺 <b>Скоро: сериалы</b>"
        if series
        else "🎬 <b>Скоро: фильмы</b>"
    )

    await callback.message.answer(
        title,
        reply_markup=new_releases_keyboard(items),
    )


@router.message(F.text == "🆕 Новинки")
async def new_releases_menu(
    message: Message,
) -> None:
    await remember(message)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎬 Новые фильмы",
                    callback_data="new:movies",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📺 Новые сериалы",
                    callback_data="new:series",
                )
            ],
        ]
    )

    await message.answer(
        "🆕 <b>Самые свежие релизы</b>\n\n"
        "Выбери категорию:",
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("new:"))
async def new_releases_list(
    callback: CallbackQuery,
) -> None:
    await callback.answer()

    if not callback.message:
        return

    kind = callback.data.split(":", 1)[1]
    series = kind == "series"

    try:
        items = await kp.new_releases(
            series=series,
            limit=20,
        )
    except Exception as exc:
        logging.exception(
            "Load new releases failed: %s",
            exc,
        )
        await callback.message.answer(
            "Не удалось загрузить новинки."
        )
        return

    if not items:
        await callback.message.answer(
            "Свежих релизов пока не найдено."
        )
        return

    title = (
        "📺 <b>Самые новые сериалы</b>"
        if series
        else "🎬 <b>Самые новые фильмы</b>"
    )

    await callback.message.answer(
        title,
        reply_markup=new_releases_keyboard(
            items
        ),
    )


@router.message(F.text == "🔥 Популярное")
async def popular(
    message: Message,
) -> None:
    await remember(message)

    try:
        items = await kp.popular()

        await message.answer(
            "🔥 Популярное сейчас:",
            reply_markup=results_keyboard(
                items
            ),
        )

    except Exception:
        await message.answer(
            "Не удалось загрузить популярное."
        )


# ---------------------------------------------------------------- «Рекомендация: что посмотреть» (опрос в боте)

# тип — поле type Кинопоиска: «Фильм» и «Сериал» без мультфильмов, анимация — отдельными пунктами
RQ_TYPES = {"f": "🎬 Фильм", "s": "📺 Сериал", "c": "🧸 Мультфильм", "m": "📺 Мультсериал", "j": "🍥 Аниме",
            "a": "🤷 Неважно"}
RQ_KP_TYPE = {"f": "movie", "s": "tv-series", "c": "cartoon", "m": "animated-series", "j": "anime"}
RQ_GENRES = ["комедия", "ужасы", "триллер", "фантастика", "боевик", "драма", "детектив", "мелодрама",
             "приключения", "фэнтези", "семейный", "криминал"]
RQ_COUNTRIES = {"ru": "🇷🇺 Россия", "us": "🇺🇸 США", "gb": "🇬🇧 Англия", "kr": "🇰🇷 Корея", "fr": "🇫🇷 Франция",
                "jp": "🇯🇵 Япония", "es": "🇪🇸 Испания", "tr": "🇹🇷 Турция", "de": "🇩🇪 Германия", "a": "🌍 Неважно"}
RQ_KP_COUNTRY = {"ru": "Россия", "us": "США", "gb": "Великобритания", "kr": "Корея Южная", "fr": "Франция",
                 "jp": "Япония", "es": "Испания", "tr": "Турция", "de": "Германия"}
RQ_ERAS = {"n": "🆕 Новое (2020+)", "m": "📀 2000–2019", "o": "📼 Классика (до 2000)", "a": "🤷 Неважно"}
rq_seen: dict[int, list[int]] = {}  # что уже советовали человеку — чтобы «ещё вариант» не повторялся


def rq_screen(t: str, g: str, c: str, e: str) -> tuple[str, InlineKeyboardMarkup]:
    """Следующий вопрос опроса (ответы — в callback_data: тип:жанр:страна:эпоха, «-» — ещё не отвечено)."""
    def btn(text: str, data: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=data)

    if t == "-":
        types = list(RQ_TYPES.items())
        return ("🎯 <b>Подберу, что посмотреть!</b>\nОтветь на 4 коротких вопроса.\n\n1/4. Что будем смотреть?",
                InlineKeyboardMarkup(inline_keyboard=[[btn(v, f"rq:{k}:-:-:-") for k, v in types[i:i + 3]]
                                                      for i in range(0, len(types), 3)]))
    if g == "-":
        rows = [[btn(RQ_GENRES[i + j].capitalize(), f"rq:{t}:{i + j}:-:-") for j in range(3) if i + j < len(RQ_GENRES)]
                for i in range(0, len(RQ_GENRES), 3)]
        rows.append([btn("🎲 Любой жанр", f"rq:{t}:a:-:-")])
        return "2/4. Какой жанр любишь?", InlineKeyboardMarkup(inline_keyboard=rows)
    if c == "-":
        countries = list(RQ_COUNTRIES.items())
        return ("3/4. Какая страна?",
                InlineKeyboardMarkup(inline_keyboard=[[btn(v, f"rq:{t}:{g}:{k}:-") for k, v in countries[i:i + 3]]
                                                      for i in range(0, len(countries), 3)]))
    return ("4/4. Новое или постарше?", InlineKeyboardMarkup(inline_keyboard=[
        [btn(v, f"rq:{t}:{g}:{c}:{k}") for k, v in list(RQ_ERAS.items())[:2]],
        [btn(v, f"rq:{t}:{g}:{c}:{k}") for k, v in list(RQ_ERAS.items())[2:]]]))


rq_error = ""  # почему не нашлось (видно админу)
RQ_ANIMATED = {"cartoon", "animated-series", "anime"}
# жанры для TMDB (фильм, сериал); у сериалов TMDB свои объединённые жанры
RQ_TMDB_GENRES = {"комедия": (35, 35), "ужасы": (27, 9648), "триллер": (53, 80), "фантастика": (878, 10765),
                  "боевик": (28, 10759), "драма": (18, 18), "детектив": (9648, 9648), "мелодрама": (10749, 18),
                  "приключения": (12, 10759), "фэнтези": (14, 10765), "семейный": (10751, 10751),
                  "криминал": (80, 80)}
RQ_TMDB_COUNTRY = {"ru": "RU", "us": "US", "gb": "GB", "kr": "KR", "fr": "FR", "jp": "JP", "es": "ES", "tr": "TR",
                   "de": "DE"}


def rq_type_ok(t: str, item: dict[str, Any]) -> bool:
    """Тип с Кинопоиска совпал с ответом: «Фильм»/«Сериал» — без анимации, анимация — своего вида."""
    kind = str(item.get("type") or "")
    if t not in RQ_KP_TYPE or not kind:
        return True
    return kind == RQ_KP_TYPE[t] or (t == "f" and kind == "movie") or (t == "s" and kind == "tv-series")


async def rq_pick(t: str, g: str, c: str, e: str, seen: list[int]) -> dict[str, Any] | None:
    """Случайный хороший фильм/сериал под ответы, не из уже показанных: Кинопоиск, а если он не ответил или
    ничего не нашёл — TMDB."""
    global rq_error
    rq_error = ""
    year_now = datetime.now(MSK).year
    base: dict[str, Any] = {"notNullFields": "poster.url"}
    if g.isdigit() and int(g) < len(RQ_GENRES):
        base["genres.name"] = RQ_GENRES[int(g)]
    if c == "ru" and e == "o":
        base["countries.name"] = "СССР"
    elif c in RQ_KP_COUNTRY:
        base["countries.name"] = RQ_KP_COUNTRY[c]
    base["year"] = {"n": f"2020-{year_now}", "m": "2000-2019", "o": "1950-1999"}.get(e, f"1970-{year_now}")
    # тип: сначала полем type, если API его не примет — isSeries и проверка типа у найденного
    kinds: list[dict[str, Any]] = [{}]
    if t in RQ_KP_TYPE:
        kinds = [{"type": RQ_KP_TYPE[t]}, {"isSeries": "true" if t in ("s", "m", "j") else "false"}]
    # у американских фильмов оценок много — порог выше; у остальных стран, сериалов и анимации — ниже
    strict = {"rating.kp": "6.3-10" if c == "ru" else "6.8-10",
              "votes.kp": f"{8000 if t == 'f' and c in ('us', 'a') else 1000}-100000000"}
    soft = {"rating.kp": "6-10", "votes.kp": "300-100000000"}
    for extra in (strict, soft):
        for kind in kinds:
            query = {**base, **kind, **extra}
            try:
                for _ in range(3):  # /movie/random — сразу случайный под фильтры
                    item = await kp._get("/movie/random", **query)
                    if item.get("id") and int(item["id"]) not in seen and rq_type_ok(t, item):
                        return kp.normalize(item)
            except Exception as exc:  # noqa: BLE001
                rq_error = f"Кинопоиск /movie/random: {exc}"
                logging.warning("Recommendation random failed %s: %s", query, exc)
            try:  # запасной путь: случайная страница списка (только из существующих) и случайный из неё
                data = await kp._get("/movie", page=1, limit=50, sortField="votes.kp", sortType="-1", **query)
                pages = int(data.get("pages") or 1)
                if pages > 1:
                    data = await kp._get("/movie", page=random.randint(1, min(pages, 5)), limit=50,
                                         sortField="votes.kp", sortType="-1", **query)
            except Exception as exc:  # noqa: BLE001
                rq_error = f"Кинопоиск /movie: {exc}"
                logging.warning("Recommendation list failed %s: %s", query, exc)
                continue
            docs = [d for d in data.get("docs") or []
                    if d.get("id") and int(d["id"]) not in seen and rq_type_ok(t, d)]
            if docs:
                return kp.normalize(random.choice(docs))
    try:
        return await rq_pick_tmdb(t, g, c, e, seen)
    except Exception as exc:  # noqa: BLE001
        rq_error += f"; TMDB: {exc}"
        logging.warning("Recommendation TMDB failed: %s", exc)
        return None


async def rq_pick_tmdb(t: str, g: str, c: str, e: str, seen: list[int]) -> dict[str, Any] | None:
    """Запасной подбор с TMDB: {"tmdb": True, "id", "title", "year", "rating", "overview", "poster", "tv"}."""
    tv = t in ("s", "m", "j") or (t == "a" and random.random() < 0.4)
    year_now = datetime.now(MSK).year
    lo, hi = {"n": (2020, year_now), "m": (2000, 2019), "o": (1950, 1999)}.get(e, (1970, year_now))
    date_key = "first_air_date" if tv else "primary_release_date"
    params: dict[str, Any] = {"language": "ru-RU", "sort_by": "popularity.desc", "vote_average.gte": 6.5,
                              "vote_count.gte": 300 if c in ("us", "a") else 80,
                              f"{date_key}.gte": f"{lo}-01-01", f"{date_key}.lte": f"{hi}-12-31"}
    genres = []
    if g.isdigit() and int(g) < len(RQ_GENRES):
        genres.append(RQ_TMDB_GENRES[RQ_GENRES[int(g)]][1 if tv else 0])
    if t in ("c", "m", "j"):
        genres.append(16)
    elif t in ("f", "s"):
        params["without_genres"] = "16"
    if genres:
        params["with_genres"] = ",".join(map(str, genres))
    country = RQ_TMDB_COUNTRY.get(c) or ("JP" if t == "j" else "")
    if country:
        params["with_origin_country"] = "SU" if (c == "ru" and e == "o") else country
    kind = "tv" if tv else "movie"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        first = await shorts_daily.tmdb._get(session, TMDB_API_KEY, f"/discover/{kind}", page=1, **params)
        pages = min(int(first.get("total_pages") or 1), 5)
        data = first if pages <= 1 else await shorts_daily.tmdb._get(
            session, TMDB_API_KEY, f"/discover/{kind}", page=random.randint(1, pages), **params)
    pool = [r for r in data.get("results") or [] if r.get("poster_path") and -int(r["id"]) not in seen]
    if not pool:
        return None
    r = random.choice(pool)
    return {"tmdb": True, "id": -int(r["id"]), "tv": tv, "title": r.get("name") or r.get("title") or "",
            "year": str(r.get("first_air_date") or r.get("release_date") or "")[:4],
            "rating": float(r.get("vote_average") or 0), "overview": str(r.get("overview") or ""),
            "poster": f"{shorts_daily.tmdb.IMG}w780{r['poster_path']}"}


@router.callback_query(F.data.startswith("rq:"))
async def rq_buttons(callback: CallbackQuery) -> None:
    await callback.answer()
    message = callback.message
    if not message:
        return
    parts = (callback.data or "").split(":")
    if parts[1] == "start" or len(parts) != 5:
        text, markup = rq_screen("-", "-", "-", "-")
        await message.answer(text, reply_markup=markup)
        return
    t, g, c, e = parts[1:]
    if "-" in (t, g, c, e):
        text, markup = rq_screen(t, g, c, e)
        try:
            await message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await message.answer(text, reply_markup=markup)
        return
    user_id = callback.from_user.id
    seen = rq_seen.setdefault(user_id, [])
    try:
        item = await rq_pick(t, g, c, e, seen)
    except Exception:  # noqa: BLE001
        logging.exception("Recommendation pick failed")
        item = None
    if not item:
        why = f"\n\n<i>Для админа: {escape(rq_error[:300])}</i>" if rq_error and user_id in ADMIN_IDS else ""
        await message.answer("😔 Под такие ответы ничего не нашлось. Попробуй другой жанр или «Неважно»." + why,
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [InlineKeyboardButton(text="🧩 Ответить заново", callback_data="rq:start")]]))
        return
    seen.append(int(item["id"]))
    del seen[:-50]
    await track(user_id, "rq")
    answers = " · ".join(x for x in [RQ_TYPES[t].split(" ", 1)[1] if t != "a" else "",
                                     RQ_GENRES[int(g)] if g.isdigit() else "",
                                     RQ_COUNTRIES[c].split(" ", 1)[1] if c != "a" else "",
                                     RQ_ERAS[e].split(" ", 1)[1] if e != "a" else ""] if x)
    try:
        await message.edit_text(f"🎯 Подбираю: {escape(answers or 'что угодно хорошее')}")
    except TelegramBadRequest:
        pass
    if item.get("tmdb"):  # нашлось на TMDB — ищем тот же фильм на Кинопоиске, чтобы была полная карточка
        try:
            for d in await kp.search(item["title"], limit=8):
                if _same_title(kp.title(d), item["title"]) and (
                        not item["year"] or abs(int(d.get("year") or 0) - int(item["year"])) <= 1):
                    item = await kp.details(int(d["id"]))
                    break
        except Exception as exc:  # noqa: BLE001 — Кинопоиск не отвечает — покажем карточку TMDB
            logging.info("Recommendation KP lookup failed: %s", exc)
    if item.get("tmdb"):
        title_q = quote_plus(item["title"])
        caption = (f"🎬 <b>{escape(item['title'])}</b> ({item['year'] or '?'}, {'сериал' if item['tv'] else 'фильм'})"
                   + (f"\n⭐ TMDB: {item['rating']:.1f}" if item["rating"] else "")
                   + (f"\n\n{escape(item['overview'][:600])}" if item["overview"] else "")
                   + (f"\n\n<i>Для админа: Кинопоиск не ответил — {escape(rq_error[:150])}</i>"
                      if rq_error and user_id in ADMIN_IDS else ""))
        bot_name = BOT_URL.rstrip("/").rsplit("/", 1)[-1]
        buttons = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🤖 Смотреть в @{bot_name}", url=BOT_URL)],
            [InlineKeyboardButton(text="▶️ Где смотреть", url=f"https://www.kinopoisk.ru/index.php?kp_query={title_q}")],
            [InlineKeyboardButton(text="🎞 Трейлер",
                                  url=f"https://www.youtube.com/results?search_query={title_q}+трейлер")],
            [InlineKeyboardButton(text="👍 Понравилось", callback_data=f"rate:{item['id']}:1"),
             InlineKeyboardButton(text="👎 Не моё", callback_data=f"rate:{item['id']}:-1")]])
        try:  # название для «Топа подписчиков»: оценка берёт его из истории просмотров
            await record_history(user_id, "series" if item["tv"] else "movie", int(item["id"]), item["title"])
        except Exception:  # noqa: BLE001
            pass
        try:
            await message.answer_photo(item["poster"], caption=caption[:1024], reply_markup=buttons)
        except Exception:  # noqa: BLE001
            await message.answer(caption, reply_markup=buttons)
    else:
        await send_card(message, item, user_id)
    await message.answer("Не то? Жми ещё 👇", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Ещё вариант", callback_data=f"rq:{t}:{g}:{c}:{e}")],
        [InlineKeyboardButton(text="🧩 Ответить заново", callback_data="rq:start")]]))


@router.message(F.text == "🎲 Что посмотреть?")
async def random_movie(
    message: Message,
) -> None:
    await remember(message)

    try:
        item = await kp.random_pick()

        if item:
            await send_card(
                message,
                item,
                message.from_user.id,
            )
        else:
            await message.answer(
                "Не получилось выбрать фильм. "
                "Попробуй ещё раз."
            )

    except Exception:
        await message.answer(
            "Не получилось выбрать фильм. "
            "Попробуй ещё раз."
        )


@router.message(F.text == "🎬 Подборки")
async def collections(
    message: Message,
) -> None:
    await remember(message)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎬 Популярные фильмы",
                    callback_data=(
                        "collection:movies"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="📺 Популярные сериалы",
                    callback_data=(
                        "collection:series"
                    ),
                )
            ],
        ]
    )

    await message.answer(
        "Что показать?",
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("collection:"))
async def collection(
    callback: CallbackQuery,
) -> None:
    await callback.answer()

    if not callback.message:
        return

    kind = callback.data.split(":", 1)[1]

    try:
        items = await kp.popular(
            series=(kind == "series")
        )

        await callback.message.answer(
            (
                "📺 Популярные сериалы"
                if kind == "series"
                else "🎬 Популярные фильмы"
            ),
            reply_markup=results_keyboard(
                items
            ),
        )

    except Exception:
        await callback.message.answer(
            "Не удалось загрузить подборку."
        )


@router.message(
    (F.text == "⭐ Избранное")
    | (F.text == "❤️ Избранное")
)
async def favorites_menu(
    message: Message,
) -> None:
    await remember(message)

    rows = await list_favorites(
        message.from_user.id
    )

    follow_btn = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="🔔 Подписки на франшизы и людей", callback_data="fl:menu")]])
    if not rows:
        await message.answer(
            "В избранном пока пусто ❤️\n\nА ещё можно подписаться на франшизу или человека (Marvel, Нолан, "
            "«Ведьмак») — новости канала о них придут сюда.",
            reply_markup=follow_btn,
        )
        return

    markup = stored_keyboard(rows, "❤️")
    markup.inline_keyboard.append(follow_btn.inline_keyboard[0])
    await message.answer(
        "❤️ Твоё избранное:",
        reply_markup=markup,
    )


# --- подписки на франшизы и людей: новости канала о них — в личку

FOLLOW_MAX = 15
FOLLOW_DAILY = 6  # не больше стольких новостей по подпискам одному человеку в день
_follow_sent: dict[tuple[int, str], int] = {}


def _follow_menu_markup(words: list[str]) -> InlineKeyboardMarkup:
    # в callback_data — номер, а не слово: кириллица быстро упирается в лимит Telegram 64 байта
    rows = [[InlineKeyboardButton(text=f"❌ {w}"[:64], callback_data=f"fl:del:{k}")] for k, w in enumerate(words)]
    rows.append([InlineKeyboardButton(text="➕ Добавить", callback_data="fl:add")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("fl:"))
async def follow_buttons(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    uid, action = callback.from_user.id, (callback.data or "")[3:]
    if action.startswith("del:") and action[4:].isdigit():
        current = [r["word"] for r in await db().fetch(
            "SELECT word FROM follows WHERE user_id=$1 ORDER BY created_at", uid)]
        if int(action[4:]) < len(current):
            await db().execute("DELETE FROM follows WHERE user_id=$1 AND word=$2", uid, current[int(action[4:])])
    if action == "add":
        await state.set_state(SearchState.follow)
        if callback.message:
            await callback.message.answer("✍️ Напиши франшизу, фильм или человека — одно на сообщение. Например: "
                                          "<code>Marvel</code>, <code>Нолан</code>, <code>Ведьмак</code>.")
        return
    words = [r["word"] for r in await db().fetch("SELECT word FROM follows WHERE user_id=$1 ORDER BY created_at",
                                                 uid)]
    if callback.message:
        await callback.message.answer(
            "🔔 <b>Подписки</b>\n\nКогда в канале " + escape(POST_CHANNEL) + " выйдет новость о том, на что ты "
            "подписан, бот пришлёт её сюда.\n\n" + ("Сейчас: " + ", ".join(escape(w) for w in words) if words
                                                     else "Пока подписок нет."),
            reply_markup=_follow_menu_markup(words))


@router.message(SearchState.follow, F.text, ~F.text.startswith("/"))
async def follow_add(message: Message, state: FSMContext) -> None:
    word = re.sub(r"\s+", " ", (message.text or "").strip().strip("«»\"'"))[:40]
    await state.clear()
    if word in MAIN_MENU_LABELS or len(word) < 2:
        await message.answer("Подписка не добавлена.", reply_markup=main_menu(message.from_user.id in ADMIN_IDS))
        return
    count = await db().fetchval("SELECT COUNT(*) FROM follows WHERE user_id=$1", message.from_user.id)
    if count >= FOLLOW_MAX:
        await message.answer(f"Можно до {FOLLOW_MAX} подписок — убери лишнее в «❤️ Избранное» → «🔔 Подписки».")
        return
    await db().execute("INSERT INTO follows(user_id, word) VALUES($1, $2) ON CONFLICT DO NOTHING",
                       message.from_user.id, word)
    await message.answer(f"🔔 Подписка на «{escape(word)}» есть! Новости канала об этом придут сюда.",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [InlineKeyboardButton(text="➕ Ещё подписка", callback_data="fl:add")],
                             [InlineKeyboardButton(text="🔔 Мои подписки", callback_data="fl:menu")]]))


def _follow_hit(word: str, text: str) -> bool:
    """Слово подписки в тексте поста — с учётом падежей («Нолана», «Marvel’s»): по основе каждого слова."""
    low = text.lower().replace("ё", "е")
    parts = [p for p in re.findall(r"[\wа-яё]+", word.lower().replace("ё", "е")) if len(p) >= 2]
    return bool(parts) and all(re.search(rf"(?<![\wа-яё]){re.escape(p[:max(3, len(p) - 2)])}", low) for p in parts)


@router.channel_post()
async def channel_post_follows(message: Message) -> None:
    """Пост вышел в нашем канале — тем, кто подписан на его тему, пересылаем (не больше FOLLOW_DAILY в день)."""
    if not message.chat.username or f"@{message.chat.username}".lower() != POST_CHANNEL.lower():
        return
    text = message.text or message.caption or ""
    if len(text) < 20:
        return
    rows = await db().fetch("SELECT user_id, word FROM follows")
    hits: dict[int, str] = {}
    for r in rows:
        if int(r["user_id"]) not in hits and _follow_hit(r["word"], text):
            hits[int(r["user_id"])] = r["word"]
    today = datetime.now(MSK).date().isoformat()
    for uid, word in hits.items():
        key = (uid, today)
        if _follow_sent.get(key, 0) >= FOLLOW_DAILY:
            continue
        _follow_sent[key] = _follow_sent.get(key, 0) + 1
        try:
            await message.bot.send_message(uid, f"🔔 Новость по подписке «{escape(word)}»:")
            await message.bot.forward_message(uid, message.chat.id, message.message_id)
        except Exception:  # noqa: BLE001 — человек заблокировал бота и т. п.
            pass
        await asyncio.sleep(0.05)
    for old in [k for k in _follow_sent if k[1] != today]:
        _follow_sent.pop(old, None)


@router.message(F.text == "🕘 История")
async def history_menu(
    message: Message,
) -> None:
    await remember(message)

    rows = await list_history(
        message.from_user.id
    )
    searches = await list_search_history(
        message.from_user.id
    )

    if not rows and not searches:
        await message.answer(
            "История пока пустая. "
            "Найди или открой фильм/сериал 🍿"
        )
        return

    lines = ["🕘 <b>История VKino</b>"]

    if searches:
        lines += ["", "🔎 <b>Последние поиски:</b>"]
        for item in searches[:8]:
            lines.append(
                f"• {escape(str(item['query']))}"
            )

    if rows:
        lines += ["", "🎬 <b>Недавно открывал:</b>"]

    await message.answer(
        "\n".join(lines),
        reply_markup=(
            stored_keyboard(rows, "🕘")
            if rows
            else None
        ),
    )


@router.message(F.text == "📊 Статистика")
async def statistics_menu(
    message: Message,
) -> None:
    await remember(message)

    summary = await user_stats_summary(
        message.from_user.id
    )
    top = await top_user_activity(
        message.from_user.id
    )

    lines = [
        "📊 <b>Твоя статистика VKino</b>",
        "",
        f"🔎 Поисков: <b>{summary['searches']}</b>",
        f"🎬 Открытий карточек: <b>{summary['opens']}</b>",
        f"🎞 Трейлеров: <b>{summary['trailers']}</b>",
        f"▶️ Запусков просмотра: <b>{summary['watches']}</b>",
    ]

    if top:
        lines += ["", "🏆 <b>Чаще всего:</b>"]
        for index, item in enumerate(top[:10], start=1):
            lines.append(
                f"{index}. {escape(str(item['title']))} — "
                f"<b>{int(item['total'])}</b>"
            )
    else:
        lines += [
            "",
            "Пока мало данных. "
            "Поищи несколько фильмов и открой их карточки 🍿",
        ]

    keyboard = None
    if top:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=(
                            f"{index}. {str(item['title'])}"
                        )[:60],
                        callback_data=(
                            f"open:{int(item['movie_id'])}"
                        ),
                    )
                ]
                for index, item in enumerate(
                    top[:10],
                    start=1,
                )
            ]
        )

    await message.answer(
        "\n".join(lines),
        reply_markup=keyboard,
    )


@router.message(F.text == "👤 Профиль")
async def profile(
    message: Message,
) -> None:
    await remember(message)

    created_at, fav_count, history_count = (
        await profile_stats(
            message.from_user.id
        )
    )

    if isinstance(created_at, datetime):
        created = created_at.strftime(
            "%d.%m.%Y"
        )
    elif created_at:
        created = str(created_at)[:10]
    else:
        created = "сегодня"

    username = (
        f"@{escape(message.from_user.username)}"
        if message.from_user.username
        else "не указан"
    )

    preference = await db().fetchrow(
        """
        SELECT preferred_voice, preferred_quality
        FROM user_playback_preferences
        WHERE user_id=$1
        """,
        message.from_user.id,
    )

    preferred_voice = (
        escape(str(preference["preferred_voice"]))
        if preference and preference["preferred_voice"]
        else "не выбрана"
    )
    preferred_quality = (
        f"{int(preference['preferred_quality'])}p"
        if preference and preference["preferred_quality"]
        else "не выбрано"
    )

    await message.answer(
        "👤 <b>Профиль VKino</b>\n\n"
        f"🆔 ID: <code>"
        f"{message.from_user.id}</code>\n"
        f"🔗 Username: {username}\n"
        f"📅 С нами с: <b>{created}</b>\n"
        f"⭐ В избранном: <b>{fav_count}</b>\n"
        f"🕘 В истории: <b>{history_count}</b>\n"
        f"🎙 Любимая озвучка: <b>{preferred_voice}</b>\n"
        f"📺 Качество: <b>{preferred_quality}</b>"
    )


@router.message(F.text == "ℹ️ О VKino")
async def about(
    message: Message,
) -> None:
    await remember(message)

    await message.answer(
        "🎬 <b>VKino</b> — "
        "кино-гид в Telegram.\n\n"
        "Поиск, рейтинги, русские описания, "
        "трейлеры, сезоны и серии, "
        "подборки, история, избранное и статистика.\n"
        "Метаданные: ПоискКино API."
    )


@router.message(Command("admin"))
async def admin(
    message: Message,
    state: FSMContext,
) -> None:
    if (
        not message.from_user
        or message.from_user.id not in ADMIN_IDS
    ):
        return

    await state.clear()

    users, favs, history = (
        await global_stats()
    )
    sources = int(
        await db().fetchval(
            """
            SELECT COUNT(*)
            FROM playback_sources
            WHERE is_active=TRUE
            """
        )
    )

    await message.answer(
        "🛠 <b>VKino — админка</b>\n\n"
        f"👥 Пользователей: <b>{users}</b>\n"
        f"⭐ Избранное: <b>{favs}</b>\n"
        f"🕘 История: <b>{history}</b>\n"
        f"▶️ Активных видеоисточников: <b>{sources}</b>\n\n"
        "Добавляй только видео, на которые у тебя есть право "
        "распространения.\n\n"
        "<code>/broadcast текст</code> — рассылка",
        reply_markup=admin_menu(),
    )


@router.callback_query(F.data == "admin:cancel")
async def admin_cancel(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer()
        return

    await state.clear()
    await callback.answer("Закрыто")

    if callback.message:
        await callback.message.answer(
            "Админ-режим закрыт."
        )


@router.message(Command("cancel"))
async def cancel_flow(
    message: Message,
    state: FSMContext,
) -> None:
    if (
        not message.from_user
        or message.from_user.id not in ADMIN_IDS
    ):
        return

    await state.clear()
    await message.answer(
        "Текущее действие отменено."
    )


@router.callback_query(
    F.data.in_(
        {
            "admin:add:movie",
            "admin:add:episode",
        }
    )
)
async def admin_add_source_start(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer()
        return

    mode = (
        "episode"
        if callback.data.endswith("episode")
        else "movie"
    )

    await state.clear()
    await state.update_data(mode=mode)
    await state.set_state(
        AdminSourceState.search_media
    )

    await callback.answer()

    if callback.message:
        await callback.message.answer(
            (
                "📺 Напиши название сериала:"
                if mode == "episode"
                else "🎬 Напиши название фильма:"
            )
        )


@router.message(
    AdminSourceState.search_media,
    F.text,
)
async def admin_search_media(
    message: Message,
    state: FSMContext,
) -> None:
    if (
        not message.from_user
        or message.from_user.id not in ADMIN_IDS
    ):
        return

    query = (message.text or "").strip()
    if len(query) < 2:
        await message.answer(
            "Напиши хотя бы 2 символа."
        )
        return

    data = await state.get_data()
    mode = data.get("mode", "movie")

    try:
        results = await kp.search(
            query,
            limit=10,
        )
    except Exception as exc:
        logging.exception(
            "Admin search failed: %s",
            exc,
        )
        await message.answer(
            "Не удалось выполнить поиск."
        )
        return

    if mode == "episode":
        results = [
            item
            for item in results
            if item.get("media_type") == "series"
        ]
    else:
        results = [
            item
            for item in results
            if item.get("media_type") == "movie"
        ]

    if not results:
        await message.answer(
            "Ничего подходящего не найдено."
        )
        return

    rows: list[list[InlineKeyboardButton]] = []

    for item in results[:8]:
        title = kp.title(item)
        year = item.get("year") or "—"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{title} ({year})"[:60],
                    callback_data=(
                        f"adminpick:{mode}:{item['id']}"
                    ),
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="❌ Отмена",
                callback_data="admin:cancel",
            )
        ]
    )

    await message.answer(
        "Выбери нужный вариант:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=rows
        ),
    )


@router.callback_query(
    F.data.startswith("adminpick:")
)
async def admin_pick_media(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer()
        return

    await callback.answer()

    if not callback.message:
        return

    try:
        _, mode, raw_movie_id = (
            callback.data.split(":", 2)
        )
        movie_id = int(raw_movie_id)
        item = await kp.details(movie_id)
        title = kp.title(item)
    except Exception as exc:
        logging.exception(
            "Admin pick media failed: %s",
            exc,
        )
        await callback.message.answer(
            "Не удалось открыть выбранный проект."
        )
        return

    await state.update_data(
        mode=mode,
        movie_id=movie_id,
        title=title,
    )

    if mode == "movie":
        await state.update_data(
            season_number=None,
            episode_number=None,
        )
        await state.set_state(
            AdminSourceState.voice
        )
        await callback.message.answer(
            f"🎬 <b>{escape(title)}</b>\n\n"
            "Напиши название озвучки.\n"
            "Например: <code>Дубляж</code>, "
            "<code>LostFilm</code>, "
            "<code>Original</code>."
        )
        return

    try:
        seasons = await kp.seasons(movie_id)
    except Exception as exc:
        logging.exception(
            "Admin load seasons failed: %s",
            exc,
        )
        await callback.message.answer(
            "Не удалось загрузить сезоны."
        )
        return

    if not seasons:
        await callback.message.answer(
            "У сериала не найдены сезоны."
        )
        return

    rows: list[list[InlineKeyboardButton]] = []

    for season in seasons[:30]:
        try:
            number = int(season.get("number"))
        except (TypeError, ValueError):
            continue

        episodes = season.get("episodes") or []
        count = len(episodes)

        rows.append(
            [
                InlineKeyboardButton(
                    text=(
                        f"Сезон {number}"
                        + (
                            f" • {count} серий"
                            if count
                            else ""
                        )
                    ),
                    callback_data=(
                        f"adminseason:{movie_id}:{number}"
                    ),
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="❌ Отмена",
                callback_data="admin:cancel",
            )
        ]
    )

    await callback.message.answer(
        f"📺 <b>{escape(title)}</b>\n"
        "Выбери сезон:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=rows
        ),
    )


@router.callback_query(
    F.data.startswith("adminseason:")
)
async def admin_pick_season(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer()
        return

    await callback.answer()

    if not callback.message:
        return

    try:
        _, raw_movie, raw_season = (
            callback.data.split(":", 2)
        )
        movie_id = int(raw_movie)
        season_number = int(raw_season)
        season = await kp.season(
            movie_id,
            season_number,
        )
    except Exception as exc:
        logging.exception(
            "Admin season failed: %s",
            exc,
        )
        await callback.message.answer(
            "Не удалось загрузить серии."
        )
        return

    if not season:
        await callback.message.answer(
            "Сезон не найден."
        )
        return

    episodes = [
        episode
        for episode in (season.get("episodes") or [])
        if isinstance(episode, dict)
    ]

    if not episodes:
        await callback.message.answer(
            "Список серий отсутствует."
        )
        return

    rows: list[list[InlineKeyboardButton]] = []

    for episode in episodes[:40]:
        try:
            number = int(episode.get("number"))
        except (TypeError, ValueError):
            continue

        name = str(
            episode.get("name")
            or episode.get("enName")
            or ""
        ).strip()

        label = f"{number} серия"
        if name:
            label += f" — {name}"

        rows.append(
            [
                InlineKeyboardButton(
                    text=label[:60],
                    callback_data=(
                        f"adminepisode:{movie_id}:"
                        f"{season_number}:{number}"
                    ),
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="❌ Отмена",
                callback_data="admin:cancel",
            )
        ]
    )

    await callback.message.answer(
        f"📺 Сезон {season_number}\n"
        "Выбери серию:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=rows
        ),
    )


@router.callback_query(
    F.data.startswith("adminepisode:")
)
async def admin_pick_episode(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer()
        return

    await callback.answer()

    if not callback.message:
        return

    try:
        (
            _,
            raw_movie,
            raw_season,
            raw_episode,
        ) = callback.data.split(":", 3)

        movie_id = int(raw_movie)
        season_number = int(raw_season)
        episode_number = int(raw_episode)
    except (TypeError, ValueError):
        await callback.message.answer(
            "Некорректная серия."
        )
        return

    await state.update_data(
        movie_id=movie_id,
        season_number=season_number,
        episode_number=episode_number,
    )
    await state.set_state(
        AdminSourceState.voice
    )

    data = await state.get_data()
    title = data.get("title", "Сериал")

    await callback.message.answer(
        f"📺 <b>{escape(str(title))}</b>\n"
        f"Сезон {season_number}, "
        f"серия {episode_number}\n\n"
        "Напиши название озвучки."
    )


@router.message(
    AdminSourceState.voice,
    F.text,
)
async def admin_voice(
    message: Message,
    state: FSMContext,
) -> None:
    if (
        not message.from_user
        or message.from_user.id not in ADMIN_IDS
    ):
        return

    voice = (message.text or "").strip()

    if not voice or len(voice) > 50:
        await message.answer(
            "Название озвучки должно быть "
            "от 1 до 50 символов."
        )
        return

    await state.update_data(
        voice_name=voice
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="360p",
                    callback_data="adminquality:360",
                ),
                InlineKeyboardButton(
                    text="480p",
                    callback_data="adminquality:480",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="720p",
                    callback_data="adminquality:720",
                ),
                InlineKeyboardButton(
                    text="1080p",
                    callback_data="adminquality:1080",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data="admin:cancel",
                )
            ],
        ]
    )

    await message.answer(
        f"🎙 Озвучка: <b>{escape(voice)}</b>\n"
        "Выбери качество:",
        reply_markup=keyboard,
    )


@router.callback_query(
    F.data.startswith("adminquality:")
)
async def admin_quality(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer()
        return

    await callback.answer()

    if not callback.message:
        return

    try:
        quality = int(
            callback.data.split(":", 1)[1]
        )
    except ValueError:
        return

    if quality not in {
        360,
        480,
        720,
        1080,
    }:
        return

    await state.update_data(
        quality=quality
    )
    await state.set_state(
        AdminSourceState.url
    )

    await callback.message.answer(
        f"📺 Качество: <b>{quality}p</b>\n\n"
        "Теперь пришли прямую HTTPS-ссылку "
        "на разрешённый HLS (.m3u8), MP4 "
        "или другой видеопоток."
    )


@router.message(
    AdminSourceState.url,
    F.text,
)
async def admin_save_source(
    message: Message,
    state: FSMContext,
) -> None:
    if (
        not message.from_user
        or message.from_user.id not in ADMIN_IDS
    ):
        return

    url = (message.text or "").strip()

    if not url.startswith("https://"):
        await message.answer(
            "Нужна HTTPS-ссылка. "
            "Попробуй ещё раз."
        )
        return

    if len(url) > 2000:
        await message.answer(
            "Ссылка слишком длинная."
        )
        return

    data = await state.get_data()

    try:
        movie_id = int(data["movie_id"])
        season_number = data.get("season_number")
        episode_number = data.get("episode_number")
        voice_name = str(data["voice_name"])
        quality = int(data["quality"])

        source_type = "link"
        lowered = url.lower().split("?", 1)[0]
        if lowered.endswith(".m3u8"):
            source_type = "hls"
        elif lowered.endswith(".mp4"):
            source_type = "mp4"

        await db().execute(
            """
            UPDATE playback_sources
            SET is_active=FALSE
            WHERE movie_id=$1
              AND season_number IS NOT DISTINCT FROM $2
              AND episode_number IS NOT DISTINCT FROM $3
              AND voice_name=$4
              AND quality=$5
              AND is_active=TRUE
            """,
            movie_id,
            season_number,
            episode_number,
            voice_name,
            quality,
        )

        source_id = await db().fetchval(
            """
            INSERT INTO playback_sources(
                movie_id,
                season_number,
                episode_number,
                voice_name,
                quality,
                playback_url,
                source_type,
                is_active
            )
            VALUES($1, $2, $3, $4, $5, $6, $7, TRUE)
            RETURNING id
            """,
            movie_id,
            season_number,
            episode_number,
            voice_name,
            quality,
            url,
            source_type,
        )

    except Exception as exc:
        logging.exception(
            "Admin save source failed: %s",
            exc,
        )
        await message.answer(
            "Не удалось сохранить источник."
        )
        return

    title = str(
        data.get("title") or f"ID {movie_id}"
    )

    target = "Фильм"
    if season_number is not None:
        target = (
            f"Сезон {season_number}, "
            f"серия {episode_number}"
        )

    await state.clear()

    await message.answer(
        "✅ <b>Источник сохранён</b>\n\n"
        f"🎬 {escape(title)}\n"
        f"📍 {target}\n"
        f"🎙 {escape(voice_name)}\n"
        f"📺 {quality}p\n"
        f"🆔 Source ID: <code>{source_id}</code>",
        reply_markup=admin_menu(),
    )


@router.callback_query(F.data == "admin:list")
async def admin_list_sources(
    callback: CallbackQuery,
) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer()
        return

    await callback.answer()

    if not callback.message:
        return

    rows = await db().fetch(
        """
        SELECT
            id,
            movie_id,
            season_number,
            episode_number,
            voice_name,
            quality,
            source_type
        FROM playback_sources
        WHERE is_active=TRUE
        ORDER BY id DESC
        LIMIT 20
        """
    )

    if not rows:
        await callback.message.answer(
            "Активных видеоисточников пока нет.",
            reply_markup=admin_menu(),
        )
        return

    lines = [
        "📋 <b>Последние активные источники</b>",
        "",
    ]
    buttons: list[list[InlineKeyboardButton]] = []

    for row in rows:
        source_id = int(row["id"])
        movie_id = int(row["movie_id"])
        season_number = row["season_number"]
        episode_number = row["episode_number"]
        voice_name = str(row["voice_name"])
        quality = int(row["quality"])

        target = f"movie:{movie_id}"
        if season_number is not None:
            target += (
                f" • S{season_number}"
                f"E{episode_number}"
            )

        lines.append(
            f"#{source_id} • {target} • "
            f"{escape(voice_name)} • {quality}p"
        )

        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"🗑 Удалить #{source_id}",
                    callback_data=(
                        f"adminsourcedel:{source_id}"
                    ),
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text="⬅️ В админку",
                callback_data="admin:home",
            )
        ]
    )

    await callback.message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        ),
    )


@router.callback_query(
    F.data.startswith("adminsourcedel:")
)
async def admin_delete_source(
    callback: CallbackQuery,
) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer()
        return

    try:
        source_id = int(
            callback.data.split(":", 1)[1]
        )
    except ValueError:
        await callback.answer(
            "Некорректный ID",
            show_alert=True,
        )
        return

    result = await db().execute(
        """
        UPDATE playback_sources
        SET is_active=FALSE
        WHERE id=$1
          AND is_active=TRUE
        """,
        source_id,
    )

    if result.endswith("0"):
        await callback.answer(
            "Источник уже удалён",
            show_alert=True,
        )
        return

    await callback.answer(
        "Источник удалён"
    )

    if callback.message:
        await callback.message.answer(
            f"🗑 Источник #{source_id} отключён.",
            reply_markup=admin_menu(),
        )


@router.callback_query(F.data == "ad:spend")
async def admin_spend(callback: CallbackQuery) -> None:
    """«💵 Расходы» из админки."""
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS or not callback.message:
        return
    await expenses_action(callback.message)


@router.callback_query(F.data == "admin:home")
async def admin_home(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer()
        return

    await state.clear()
    await callback.answer()

    if not callback.message:
        return

    users, favs, history = (
        await global_stats()
    )
    sources = int(
        await db().fetchval(
            """
            SELECT COUNT(*)
            FROM playback_sources
            WHERE is_active=TRUE
            """
        )
    )

    await callback.message.answer(
        "🛠 <b>VKino — админка</b>\n\n"
        f"👥 Пользователей: <b>{users}</b>\n"
        f"⭐ Избранное: <b>{favs}</b>\n"
        f"🕘 История: <b>{history}</b>\n"
        f"▶️ Источников: <b>{sources}</b>",
        reply_markup=admin_menu(),
    )


@router.message(Command("rutube"))
async def rutube_debug(
    message: Message,
) -> None:
    if (
        not message.from_user
        or message.from_user.id not in ADMIN_IDS
    ):
        return

    query = (message.text or "").partition(" ")[2].strip()
    if not query:
        await message.answer(
            "Использование: /rutube название фильма"
        )
        return

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                RUTUBE_SEARCH_API,
                params={"query": query, "format": "json"},
                headers={"accept": "application/json"},
            ) as resp:
                status = resp.status
                body = await resp.text()
    except Exception as exc:
        await message.answer(
            "❌ Rutube недоступен с сервера:\n"
            f"<code>{escape(type(exc).__name__)}: "
            f"{escape(_safe_err(exc)[:300])}</code>"
        )
        return

    try:
        data = json.loads(body)
    except ValueError:
        await message.answer(
            f"❌ HTTP {status}, ответ не JSON:\n"
            f"<code>{escape(body[:500])}</code>"
        )
        return

    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        keys = list(data)[:20] if isinstance(data, dict) else type(data).__name__
        await message.answer(
            f"⚠️ HTTP {status}, нет поля results.\n"
            f"Ключи: <code>{escape(str(keys))}</code>"
        )
        return

    lines = [f"✅ HTTP {status}, результатов: {len(results)}"]
    if results and isinstance(results[0], dict):
        lines.append(
            "Поля: <code>"
            + escape(", ".join(sorted(results[0]))[:900])
            + "</code>"
        )
    for video in results[:6]:
        if not isinstance(video, dict):
            continue
        author = video.get("author") or {}
        if not isinstance(author, dict):
            author = {}
        lines.append(
            "\n• " + escape(str(video.get("title") or "—")[:120])
            + f"\n  ⏱ {video.get('duration')} с"
            + f" | official={video.get('is_official')}"
            + f" | paid={video.get('is_paid')}"
            + "\n  👤 " + escape(str(author.get("name") or "—")[:60])
            + f" (id {escape(str(author.get('id')))})"
        )
    await message.answer("\n".join(lines))


@router.message(Command("broadcast"))
async def broadcast(
    message: Message,
) -> None:
    if (
        not message.from_user
        or message.from_user.id not in ADMIN_IDS
    ):
        return

    text = (
        (message.text or "")
        .partition(" ")[2]
        .strip()
    )

    if not text:
        await message.answer(
            "Использование: /broadcast текст"
        )
        return

    try:
        await message.answer(text)
    except TelegramBadRequest as exc:
        await message.answer(
            "Рассылка не отправлена: ошибка HTML-разметки.\n"
            "Символы &lt; &gt; &amp; пиши как "
            "<code>&amp;lt;</code> <code>&amp;gt;</code> "
            "<code>&amp;amp;</code>.\n\n"
            f"<code>{escape(_safe_err(exc))}</code>"
        )
        return

    ok = 0
    failed = 0

    for user_id in await all_user_ids():
        if user_id == message.from_user.id:
            # Already received the preview above.
            ok += 1
            continue
        try:
            await message.bot.send_message(
                user_id,
                text,
            )
            ok += 1
        except Exception:
            failed += 1

        await asyncio.sleep(0.05)

    await message.answer(
        f"Рассылка завершена. "
        f"✅ {ok}  ❌ {failed}"
    )


# ---------------------------------------------------------------- шортсы KinoMir360

MSK = timezone(timedelta(hours=3))
SHORTS_HOUR = int(os.getenv("SHORTS_HOUR", "8"))
SHORTS_DIR = Path(os.getenv("SHORTS_DIR", "/tmp/kinomir_shorts"))
if not os.getenv("MUSIC_DIR"):
    shorts_music.DIR = SHORTS_DIR / "music"  # копии треков библиотеки; сами треки хранятся в Telegram
SHORT_THEMES = {
    "online": "онлайн-премьеры",
    "cinema": "сейчас в кино",
    "series": "новые сериалы",
    "upcoming": "скоро в кино",
    "genre": "топ случайного жанра",
    "facts": "5 фактов о фильме",
    "facts_new": "5 фактов: новинки с 2015 года",
    "facts_old": "5 фактов: классика до 2015 года",
    "news": "новости кино",
    "birthday": "день рождения звезды",
}
SHORTS_TIMEOUT = 15 * 60  # сборка дольше — что-то зависло, прерываем и сообщаем
shorts_lock = asyncio.Lock()
shorts_jobs: set[asyncio.Task] = set()


async def shorts_setting(key: str) -> str | None:
    return await db().fetchval(
        "SELECT value FROM shorts_settings WHERE key = $1",
        key,
    )


async def set_shorts_setting(key: str, value: str | None) -> None:
    if value is None:
        await db().execute(
            "DELETE FROM shorts_settings WHERE key = $1",
            key,
        )
        return
    await db().execute(
        """
        INSERT INTO shorts_settings (key, value)
        VALUES ($1, $2)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """,
        key,
        value,
    )


async def load_pronunciations() -> dict[str, str]:
    raw = await shorts_setting("pronounce")
    try:
        table = json.loads(raw) if raw else {}
    except ValueError:
        table = {}
    table = table if isinstance(table, dict) else {}
    fixed = shorts_pronounce.repair(table)
    if fixed != table:  # запись «а = б В = г» одной строкой — разложить на пары и сохранить
        table = fixed
        await set_shorts_setting("pronounce", json.dumps(table, ensure_ascii=False))
    shorts_pronounce.CUSTOM = table
    mode = await shorts_setting("stress_mode")
    if mode in shorts_daily.STRESS_MODES:
        shorts_daily.stress_mode = mode
    style = await shorts_setting("stress_style")
    if style in shorts_pronounce.STRESS_STYLES:
        shorts_daily.stress_style = style
    return shorts_pronounce.CUSTOM


async def load_voice() -> None:
    """Свой голос (клон, команда /clone) — поверх голоса по умолчанию."""
    shorts_lumean.use_voice(await shorts_setting("lumean_service"), await shorts_setting("lumean_voice"))


async def fact_channels() -> list[str]:
    raw = await shorts_setting("fact_channels")
    try:
        channels = json.loads(raw) if raw else None
    except ValueError:
        channels = None
    return channels if isinstance(channels, list) and channels else list(shorts_tgsearch.DEFAULT_CHANNELS)


async def load_fact_channels() -> None:
    """Telegram-каналы, где ИИ ищет факты о фильмах (кнопка «📡 Каналы для фактов»)."""
    shorts_daily.fact_channels = await fact_channels()


async def channels_action(message: Message) -> None:
    channels = await fact_channels()
    await message.answer(f"⏳ Проверяю {len(channels)} каналов…")
    lines = ["📡 <b>Каналы для фактов и новостей</b> — ИИ ищет в них посты о фильме и свежие новости:"]
    for i, (ch, res) in enumerate(await shorts_tgsearch.check(channels), 1):
        mark = "✅" if isinstance(res, int) and res else f"❌ {escape(str(res or 'постов не видно'))}"
        lines.append(f"{i}. {mark} t.me/{escape(ch)}")
    lines.append("\nЧитаются только открытые каналы (с @именем).")
    await message.answer("\n".join(lines), disable_web_page_preview=True,
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [_btn("➕ Добавить каналы", "sh:ask:tgadd"), _btn("✏️ Изменить список", "sh:ask:tgch")],
                             [_btn("↩️ Стандартный список", "sh:tgch_default")],
                             [_btn("⬅️ Настройки", "sh:settings")]]))


async def news_feeds() -> list[str]:
    raw = await shorts_setting("news_feeds")
    try:
        feeds = json.loads(raw) if raw else None
    except ValueError:
        feeds = None
    return feeds if isinstance(feeds, list) and feeds else list(shorts_news.DEFAULT_FEEDS)


async def fast_feeds() -> list[str]:
    """Быстрые зарубежные источники (Reddit, Deadline, инсайдеры, X): свой список или стандартный."""
    raw = await shorts_setting("fast_feeds")
    try:
        feeds = json.loads(raw) if raw else None
    except ValueError:
        feeds = None
    return feeds if isinstance(feeds, list) else list(shorts_fast.DEFAULT_FEEDS)  # [] — автор удалил все


async def send_short(bot: Bot, chat_id: int, result: Any) -> None:
    seo = result.seo
    video = await bot.send_video(
        chat_id,
        FSInputFile(result.video, filename="kinomir360_short.mp4"),
        caption=post_caption(short_caption(result)),  # готово и для Telegram-канала
        width=1080,
        height=1920,
        supports_streaming=True,
    )
    keep_for_upload(chat_id, video.message_id, result)
    has_project = await keep_project(chat_id, video.message_id, result.video.parent,
                                     {"mode": "short", "caption": post_caption(short_caption(result)), "seo": seo})
    source_plan = result.video.parent / "scene_plan.json"
    plan_path = proj_dir(chat_id, video.message_id) / "scene_plan.json"
    if source_plan.exists():
        if has_project and plan_path.exists():
            await bot.send_message(chat_id, "🖼 Перед публикацией проверь визуальный ряд кнопкой ниже.")
        else:
            await bot.send_message(chat_id, "⚠️ План визуальной проверки не удалось сохранить. Публикацию не предлагаю, "
                                 "чтобы не пропустить обязательную проверку сцен.")
    else:
        await offer_post(bot, chat_id, [video.message_id], "caption", what="Шортс готов и для Telegram-канала",
                         extra={"yt": True})
    for label, key in (
        ("Название", "title"),
        ("Описание", "description"),
        ("Теги", "tags"),
        ("Закреплённый комментарий", "pinned"),
    ):
        await bot.send_message(
            chat_id,
            f"<b>{label}:</b>\n<pre>{escape(shorts_yt.no_links(seo[key]) if key == 'description' else seo[key])}</pre>",
        )
    await bot.send_message(
        chat_id,
        f"<b>TikTok — подпись с хэштегами:</b>\n<pre>{escape(shorts_daily.tiktok_caption(result))}</pre>",
    )
    if seo.get("warning"):
        await bot.send_message(chat_id, escape(seo["warning"]))
    if has_project:
        await fix_buttons(bot, chat_id, video.message_id)


def progress_bar(pct: int, stage: str, what: str = "шортс") -> str:
    filled = pct // 10
    return f"⏳ Собираю {what}\n<code>{'▓' * filled}{'░' * (10 - filled)}</code> {pct}%\n{escape(stage)}"


def progress_updater(status: Message | None, what: str = "шортс"):
    """Двигает полоску в сообщении status; правит не чаще раза в 3 с, чтобы не упереться в лимиты Telegram."""
    last = {"pct": -1, "at": 0.0}

    async def update(pct: int, stage: str) -> None:
        if status is None:
            return
        now = time.monotonic()
        if pct < 100 and (pct == last["pct"] or now - last["at"] < 3):
            return
        last.update(pct=pct, at=now)
        try:
            await status.edit_text(progress_bar(pct, stage, what))
        except Exception as exc:  # noqa: BLE001 — «message is not modified», сеть и т.п.
            logging.debug("Progress edit failed: %s", exc)

    return update


async def run_short(
    bot: Bot,
    chat_ids: list[int],
    theme_key: str | None = None,
    status: Message | None = None,
) -> None:
    async with shorts_lock:
        rows = await db().fetch(
            """
            SELECT movie_id FROM shorts_history
            WHERE used_at > NOW() - INTERVAL '30 days'
            """
        )
        exclude = {int(r["movie_id"]) for r in rows}
        await load_pronunciations()
        await load_voice()
        await load_fact_channels()
        now = datetime.now(MSK)
        work = SHORTS_DIR / now.strftime("%Y%m%d_%H%M%S")
        work.mkdir(parents=True, exist_ok=True)
        try:
            music = await custom_music(bot, work)

            # если бот перезапустится посреди сборки (обновление), после старта скажем об этом
            await set_shorts_setting("running", json.dumps({"chats": chat_ids, "theme": theme_key or ""}))
            progress = progress_updater(status)
            shorts_daily.on_progress, shorts_daily.last_stage = progress, ""
            result = await asyncio.wait_for(make_daily_short(
                kp._get,
                work,
                theme_key,
                exclude,
                music,
                now.date(),
                TMDB_API_KEY,
                await news_feeds(),
                manual=theme_key is not None,
                gemini_key=GEMINI_API_KEY,
                trailer_clips=await shorts_setting("clips") != "off",
            ), timeout=SHORTS_TIMEOUT)
            await db().executemany(
                """
                INSERT INTO shorts_history (movie_id) VALUES ($1)
                ON CONFLICT (movie_id) DO UPDATE SET used_at = NOW()
                """,
                [(int(f["id"]),) for f in result.films if f.get("id")],
            )
            await progress(100, "Готово — отправляю ролик")
            for chat_id in chat_ids:
                await send_short(bot, chat_id, result)
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"сборка шла дольше {SHORTS_TIMEOUT // 60} минут и была прервана — "
                f"застряла на этапе: {shorts_daily.last_stage or 'начало'}. Попробуй ещё раз."
            ) from None
        finally:
            shorts_daily.on_progress = None
            await clear_running()
            shutil.rmtree(work, ignore_errors=True)


stopping = False  # бот останавливается (обновление, перезапуск) — сборки обрываются не по своей вине


async def clear_running() -> None:
    """Сборка закончилась — снять отметку. При остановке бота оставляем: после запуска сообщим автору."""
    if not stopping:
        await set_shorts_setting("running", None)


async def report_interrupted_short(bot: Bot) -> None:
    """Сборка оборвалась перезапуском бота — сообщаем, чтобы не ждали вечно."""
    try:
        raw = await shorts_setting("running")
        if not raw:
            return
        await set_shorts_setting("running", None)
        info = json.loads(raw)
        theme = SHORT_THEMES.get(info.get("theme") or "", info.get("theme") or "тема дня")
        for chat_id in info.get("chats") or sorted(ADMIN_IDS):
            await bot.send_message(
                chat_id,
                f"⚠️ Сборка шортса ({escape(theme)}) прервалась: бот перезапустился "
                "(обновление или перезапуск сервера). Нажми тему ещё раз.",
            )
    except Exception:
        logging.exception("Failed to report interrupted short")
    try:  # выпуск по шагам монтировался — продолжаем с недостающих кусков (готовые лежат в томе /data)
        draft = await wd_load()
        if draft and draft.get("stage") == "video" and any(k not in draft["blocks"] for k in wd_keys(draft)):
            await bot.send_message(draft["chat"], "▶️ Бот перезапустился — продолжаю монтаж новостей выпуска.")
            wd_start_video(bot)
    except Exception:  # noqa: BLE001
        logging.exception("Weekly draft resume failed")


PLAN_HOUR = 11  # МСК: напоминание о ролике по контент-плану (публиковать 12:00–15:00)


def plan_text(day: date) -> tuple[str, InlineKeyboardMarkup | None]:
    item, long_video = shorts_plan.today(day), shorts_plan.long_today(day)
    if not item and not long_video:
        return "📅 На сегодня в контент-плане ничего нет.", None
    rows, lines = [], [f"📅 <b>По плану на {day:%d.%m}</b> (публиковать 12:00–15:00 МСК):"]
    ready = shorts_plan.script(day)
    if item:
        kind, topic = item
        label, data, hint = shorts_plan.KINDS[kind]
        if ready:  # готовый сценарий из контент-пакета — монтаж по нему одной кнопкой
            lines.append(f"🎬 Shorts: <b>{escape(ready[0])}</b>\n💡 Сценарий готов. Приложи видео сцены или свои "
                         "кадры, если есть, — ролик соберётся по смыслу фраз.")
            rows.append([_btn("🎬 Собрать по готовому сценарию", f"pl:go:{day.isoformat()}")])
            rows.append([_btn("📄 Показать текст", f"pl:txt:{day.isoformat()}")])
        else:
            lines.append(f"🎬 Shorts: <b>{escape(topic)}</b>" + (f"\n💡 {escape(hint)}" if hint else ""))
            rows.append([_btn(f"{label} — собрать", data)])
    if long_video:
        lines.append(f"🎞 Длинное видео (суббота): <b>{escape(long_video)}</b>")
        rows.append([_btn("🎞 Выпуск «Новости кино»", "pt:wv")])
    lines.append("\nНазвание: запрос + названия фильмов + эмоция. Слухи — «по данным …».")
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def plan_reminder(bot: Bot) -> None:
    """Раз в день в PLAN_HOUR — напоминание админам, что по плану, с кнопкой сборки."""
    now = datetime.now(MSK)
    if now.hour < PLAN_HOUR or await shorts_setting("plan_last") == now.date().isoformat():
        return
    await set_shorts_setting("plan_last", now.date().isoformat())
    text, kb = plan_text(now.date())
    if kb is None:
        return
    for admin_id in sorted(ADMIN_IDS):
        try:
            await bot.send_message(admin_id, text, reply_markup=kb)
        except Exception:  # noqa: BLE001
            logging.exception("Plan reminder to %s failed", admin_id)


@router.callback_query(F.data.startswith("pl:"))
async def plan_buttons(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопки напоминания о контент-плане: собрать ролик по готовому сценарию дня или показать текст."""
    if callback.from_user.id not in ADMIN_IDS or not callback.message:
        await callback.answer()
        return
    _, action, day = (callback.data or "").split(":", 2)
    try:
        ready = shorts_plan.script(date.fromisoformat(day))
    except ValueError:
        ready = None
    await callback.answer()
    if not ready:
        await callback.message.answer("Готового сценария на этот день нет.")
        return
    title, body = ready
    if action == "txt":
        await callback.message.answer(f"📄 <b>{escape(title)}</b>\n\n{escape(body)}")
        return
    # дальше — как «🎬 Свой сценарий»: показ текста, кнопки «📎 Свои картинки и видео», «✅ Собрать шортс»
    await state.update_data(own_scripts=[(title, f"**Заголовок:** {title}\n{body}")], own_pick=0, lore_kind="own",
                            lore_name=title, lore_hook="", lore_script=None, lore_media=[])
    await own_generate(callback.message, state)


@router.message(Command("plan"))
async def plan_command(message: Message) -> None:
    """/plan — что по контент-плану сегодня и на неделю вперёд."""
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    today = datetime.now(MSK).date()
    text, kb = plan_text(today)
    rows = []
    for day, item, long_video in shorts_plan.week(today + timedelta(days=1), 6):
        part = escape(item[1]) if item else "—"
        rows.append(f"• {day:%d.%m}: {part}" + (f"\n   🎞 {escape(long_video)}" if long_video else ""))
    await message.answer(text + "\n\n<b>Дальше:</b>\n" + "\n".join(rows), reply_markup=kb)


async def shorts_daily_loop(
    bot: Bot,
) -> None:
    await report_interrupted_short(bot)
    while True:
        try:
            await plan_reminder(bot)
        except Exception:  # noqa: BLE001 — напоминание не должно мешать шортсам
            logging.exception("Plan reminder failed")
        try:
            now = datetime.now(MSK)
            today = now.date().isoformat()
            if (
                now.hour >= SHORTS_HOUR
                and await shorts_setting("daily_last_run") != today
            ):
                await set_shorts_setting("daily_last_run", today)
                try:
                    await run_short(bot, sorted(ADMIN_IDS))
                except Exception as exc:
                    logging.exception("Daily short failed")
                    for admin_id in ADMIN_IDS:
                        try:
                            await bot.send_message(
                                admin_id,
                                "❌ Ежедневный шортс не собрался:\n"
                                f"<code>{escape(_safe_err(exc)[-700:])}</code>\n\n"
                                "Попробуй вручную: /short",
                            )
                        except Exception:
                            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Shorts loop error")
        await asyncio.sleep(60)


DIRECT_REPORT_HOUR = int(os.getenv("DIRECT_REPORT_HOUR", "10"))  # МСК


async def direct_report_text(bot: Bot, remember: bool) -> str:
    """Отчёт Директа + прирост подписчиков канала с прошлого отчёта."""
    try:
        subs = await bot.get_chat_member_count(POST_CHANNEL)
    except Exception:  # noqa: BLE001 — бот не админ канала
        subs = None
    before_raw = await shorts_setting("direct_subs")
    before = int(before_raw) if before_raw and before_raw.isdigit() else None
    text = await direct_stats.daily_text(subs, before)
    if remember and subs is not None:
        await set_shorts_setting("direct_subs", str(subs))
    return text


async def direct_report_loop(bot: Bot) -> None:
    """Каждый день в DIRECT_REPORT_HOUR по Москве — статистика рекламы в Директе админам."""
    while True:
        try:
            now = datetime.now(MSK)
            today = now.date().isoformat()
            if (direct_stats.token() and now.hour >= DIRECT_REPORT_HOUR
                    and await shorts_setting("direct_last_run") != today):
                await set_shorts_setting("direct_last_run", today)
                try:
                    text = await direct_report_text(bot, remember=True)
                except Exception as exc:  # noqa: BLE001
                    logging.exception("Direct report failed")
                    text = f"❌ Отчёт Директа не собрался: <code>{escape(_safe_err(exc)[:500])}</code>"
                for admin_id in ADMIN_IDS:
                    try:
                        await bot.send_message(admin_id, text, disable_web_page_preview=True)
                    except Exception:  # noqa: BLE001
                        pass
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logging.exception("Direct report loop error")
        await asyncio.sleep(300)


@router.message(Command("direct"))
async def direct_command(message: Message) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    if not direct_stats.token():
        await message.answer("Нет YANDEX_DIRECT_TOKEN в переменных окружения бота.")
        return
    status = await message.answer("⏳ Собираю статистику Директа…")
    try:
        text = await direct_report_text(message.bot, remember=False)
    except Exception as exc:  # noqa: BLE001
        text = f"❌ <code>{escape(_safe_err(exc)[:500])}</code>"
    await status.edit_text(text, disable_web_page_preview=True)


@router.message(Command("short"))
async def short_command(
    message: Message,
) -> None:
    if (
        not message.from_user
        or message.from_user.id not in ADMIN_IDS
    ):
        return
    await start_short(message, (message.text or "").partition(" ")[2].strip())


async def start_short(message: Message, theme: str) -> None:
    """Собрать шортс по теме (как в /short) и прислать в этот чат — для команды и кнопок."""
    if theme.lower() in ("facts_new", "facts_old"):
        theme = theme.lower()
    elif theme.lower().startswith("facts"):
        # /short facts Матрица — название фильма оставляем как есть
        query = theme[5:].strip(" :")
        theme = f"facts:{query}" if query else "facts"
    else:
        theme = theme.lower()
    if theme and theme not in SHORT_THEMES and not theme.startswith(("genre:", "facts:", "birthday:")):
        await message.answer(
            "Использование: /short [тема]\n\n"
            + "\n".join(
                f"<code>{key}</code> — {name}"
                for key, name in SHORT_THEMES.items()
            )
            + "\n<code>genre:ужасы</code> — топ жанра "
            "(фантастика, ужасы, триллер, комедия, боевик, детектив)\n"
            "<code>facts Матрица</code> — факты о конкретном фильме\n\n"
            "Без темы — тема дня по расписанию."
        )
        return
    if shorts_lock.locked():
        await message.answer("⏳ Шортс уже собирается, подожди пару минут.")
        return

    status = await message.answer(progress_bar(0, "Начинаю, обычно это 2–5 минут"))

    async def job() -> None:
        try:
            await run_short(message.bot, [message.chat.id], theme or None, status)
        except Exception as exc:
            logging.exception("Manual short failed")
            try:
                await status.edit_text("❌ Сборка остановилась — причина ниже")
            except TelegramBadRequest:
                pass
            await message.answer(
                "❌ Шортс не собрался:\n"
                f"<code>{escape(_safe_err(exc)[-700:])}</code>"
            )

    task = asyncio.create_task(job())
    shorts_jobs.add(task)
    task.add_done_callback(shorts_jobs.discard)


@router.message(Command("feeds"))
async def feeds_command(
    message: Message,
) -> None:
    if (
        not message.from_user
        or message.from_user.id not in ADMIN_IDS
    ):
        return
    await feeds_action(message, (message.text or "").partition(" ")[2].strip())


async def feeds_action(message: Message, text: str) -> None:
    action, _, arg = text.partition(" ")
    feeds = await news_feeds()
    if action == "add" and arg.startswith(("http://", "https://")):
        if arg not in feeds:
            feeds.append(arg.strip())
        await set_shorts_setting("news_feeds", json.dumps(feeds))
        await message.answer("✅ Лента добавлена.")
        return
    if action == "del" and arg.isdigit() and 1 <= int(arg) <= len(feeds):
        removed = feeds.pop(int(arg) - 1)
        await set_shorts_setting("news_feeds", json.dumps(feeds) if feeds else None)
        await message.answer(f"✅ Удалена: <code>{escape(removed[:80])}</code>")
        return
    if action == "default":
        await set_shorts_setting("news_feeds", None)
        await message.answer("✅ Вернул стандартный список лент.")
        return

    await message.answer("⏳ Проверяю ленты новостей…")
    lines = ["📰 <b>Ленты новостей</b> (свежих за 48 ч):"]
    for i, (url, res) in enumerate(await shorts_news.check_feeds(feeds), 1):
        mark = f"✅ {res}" if isinstance(res, int) and res else f"❌ {escape(_safe_err(str(res)))}"
        lines.append(f"{i}. {mark}\n<code>{escape(url[:90])}</code>")
    lines.append(
        "\n<code>/feeds add URL</code> — добавить RSS\n"
        "<code>/feeds del N</code> — удалить\n"
        "<code>/feeds default</code> — стандартный список"
    )
    await message.answer("\n".join(lines), disable_web_page_preview=True)


@router.message(Command("title"))
async def title_command(message: Message) -> None:
    """/title Clayface = Глиноликий — как называть фильм в постах (важнее базы фильмов); /title — список."""
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    arg = (message.text or "").partition(" ")[2].strip()
    mine = await _json_setting("my_titles", {})
    if arg.lower().startswith("del "):
        key = arg[4:].strip()
        found = next((k for k in mine if k.lower() == key.lower()), None)
        if found:
            mine.pop(found)
            await set_shorts_setting("my_titles", json.dumps(mine, ensure_ascii=False))
        await message.answer(f"✅ Убрал: {escape(key)}" if found else "Такого названия в словаре нет.")
        return
    if "=" in arg:
        en, _, ru = (x.strip() for x in arg.partition("="))
        if en and ru:
            mine[en] = ru
            await set_shorts_setting("my_titles", json.dumps(mine, ensure_ascii=False))
            await message.answer(f"✅ В постах «{escape(en)}» будет «{escape(ru)}».")
            return
    rows = [f"• {escape(k)} → «{escape(v)}»" for k, v in (await _my_titles()).items()]
    await message.answer("🎬 <b>Свои названия фильмов</b> (важнее базы фильмов):\n" + "\n".join(rows)
                         + "\n\n<code>/title Clayface = Глиноликий</code> — добавить\n"
                           "<code>/title del Clayface</code> — убрать")


@router.message(Command("fast"))
async def fast_command(message: Message) -> None:
    """/fast — быстрые зарубежные источники новостей: проверить, добавить, удалить."""
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    action, _, arg = (message.text or "").partition(" ")[2].strip().partition(" ")
    arg = arg.strip()
    feeds = await fast_feeds()
    if action == "add" and not arg.startswith(("http://", "https://", "x:", "@")):
        await message.answer("Напиши ссылку через пробел после команды, например:\n"
                             "<code>/fast add https://rss.app/feeds/xxxxx.xml</code>\n"
                             "или аккаунт X (если настроен RSSHub): <code>/fast add @DanielRPK</code>")
        return
    if action == "add":
        entry = f"x:{arg.lstrip('@')}" if arg.startswith("@") else arg
        if entry not in feeds:
            feeds.append(entry)
        await set_shorts_setting("fast_feeds", json.dumps(feeds))
        await message.answer(f"✅ Добавил: <code>{escape(entry[:90])}</code>")
        return
    if action == "del" and arg.isdigit() and 1 <= int(arg) <= len(feeds):
        removed = feeds.pop(int(arg) - 1)
        await set_shorts_setting("fast_feeds", json.dumps(feeds))  # пустой список — пусто, а не стандартный
        await message.answer(f"✅ Удалил: <code>{escape(removed[:90])}</code>")
        return
    if action == "default":
        await set_shorts_setting("fast_feeds", None)
        await message.answer("✅ Вернул стандартный список быстрых источников.")
        return
    wait = await message.answer("⏳ Проверяю быстрые источники…")
    lines = ["⚡ <b>Быстрые источники</b> (Reddit, Deadline, инсайдеры, X) — новостей за сутки:"]
    for i, (entry, res, name) in enumerate(await shorts_fast.check(feeds), 1):
        mark = f"✅ {res}" if isinstance(res, int) and res else ("⚪ 0" if res == 0 else f"❌ {escape(_safe_err(str(res)))}")
        lines.append(f"{i}. {escape(name)} — {mark}\n<code>{escape(entry[:90])}</code>")
    lines.append(
        "\n<code>/fast add URL</code> — добавить RSS (в том числе X через rss.app)\n"
        "<code>/fast add @Имя</code> — аккаунт X через свой RSSHub (COMPOSE_PROFILES=x в .env)\n"
        "<code>/fast del N</code> — удалить\n<code>/fast default</code> — стандартный список")
    await wait.edit_text("\n".join(lines), disable_web_page_preview=True)


STRESS_LABELS = {"names": "только имена и словарь /say", "all": "во всех словах", "off": "без знаков"}
STRESS_TEST = ("Режиссёр Майкл Манн получил травму: оружие, находившееся в кобуре, случайно выстрелило. Сейчас "
               "съёмки «Схватки 2» запланированы на январь, а Мэттью Макконахи получил «Оскар».")


async def stress_menu() -> tuple[str, InlineKeyboardMarkup]:
    await load_pronunciations()
    mode = shorts_daily.stress_mode
    text = ("🗣 <b>Ударения в озвучке</b>\n\nСейчас: <b>" + STRESS_LABELS[mode] + "</b>.\n\n"
            "• <b>Только имена</b> — знак ударения в именах, названиях и словах из /say; обычные слова голос "
            "читает сам.\n• <b>Во всех словах</b> — по словарю ударений; ElevenLabs от этого может сбиваться.\n"
            "• <b>Без знаков</b> — голос ставит ударения сам.\n\n«🎧 Сравнить» озвучит одну фразу тремя способами — "
            "выбери на слух (своя фраза: <code>/stress test текст</code>).")
    rows = [[_btn(("✅ " if mode == m else "") + STRESS_LABELS[m].capitalize(), f"ud:{m}")] for m in STRESS_LABELS]
    rows.append([_btn("🎧 Сравнить на слух", "ud:test")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def stress_test(bot: Bot, chat_id: int, phrase: str) -> None:
    """Одна фраза тремя способами — автор выбирает режим ударений на слух."""
    status = await bot.send_message(chat_id, "🎧 Озвучиваю фразу тремя способами…")
    work = SHORTS_DIR / f"stress_{time.time_ns()}"
    keep = shorts_daily.stress_mode
    try:
        async with shorts_lock:
            await load_pronunciations()
            await load_voice()
            keep = shorts_daily.stress_mode
            for mode in ("off", "names", "all"):
                shorts_daily.stress_mode = mode
                speech = await shorts_daily.spoken_lines([phrase], careful=True)
                path, _, _, _ = await shorts_daily.synthesize(speech, work / mode, prepared=True)
                mp3 = work / f"{mode}.mp3"
                await shorts_weekly._run("ffmpeg", "-v", "error", "-y", "-i", str(path), "-c:a", "libmp3lame",
                                         "-b:a", "160k", str(mp3))
                await bot.send_audio(chat_id, FSInputFile(mp3, filename=f"udarenia_{mode}.mp3"),
                                     caption=f"🗣 {STRESS_LABELS[mode].capitalize()}:\n{escape(speech[0][:900])}",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [_btn("✅ Так лучше", f"ud:{mode}")]]))
        await status.delete()
    except Exception as exc:  # noqa: BLE001
        logging.exception("Stress test failed")
        await status.edit_text(f"❌ Не получилось: <code>{escape(_safe_err(exc)[:300])}</code>")
    finally:
        shorts_daily.stress_mode = keep
        shutil.rmtree(work, ignore_errors=True)


@router.message(Command("stress"))
async def stress_command(message: Message) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    arg = (message.text or "").partition(" ")[2].strip()
    if arg.lower().startswith("test"):
        phrase = arg[4:].strip() or STRESS_TEST
        chat_id = message.chat.id
        _run_job(lambda: stress_test(message.bot, chat_id, phrase[:400]))
        return
    text, kb = await stress_menu()
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("ud:"))
async def stress_buttons(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS or not callback.message:
        await callback.answer()
        return
    kind = (callback.data or "")[3:]
    if kind == "test":
        await callback.answer()
        chat_id = callback.message.chat.id
        _run_job(lambda: stress_test(callback.bot, chat_id, STRESS_TEST))
        return
    if kind == "menu":
        await callback.answer()
        text, kb = await stress_menu()
        await callback.message.answer(text, reply_markup=kb)
        return
    if kind in STRESS_LABELS:
        await set_shorts_setting("stress_mode", kind)
        shorts_daily.stress_mode = kind
        await callback.answer("Сохранено")
        await callback.message.answer(f"✅ Ударения в озвучке: <b>{STRESS_LABELS[kind]}</b>. Следующие ролики — так.")
        return
    await callback.answer()


@router.message(Command("stress"))
async def stress_style_command(message: Message) -> None:
    """/stress caps|plus|acute — как отдавать голосу ударение из словаря /say (что голос слушает — проверить /voice)."""
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    await load_pronunciations()
    arg = ((message.text or "").split(maxsplit=1)[1:] or [""])[0].strip().lower()
    if arg in shorts_pronounce.STRESS_STYLES:
        await set_shorts_setting("stress_style", arg)
        shorts_daily.stress_style = arg
        await message.answer(f"✅ Ударение голосу: {escape(shorts_pronounce.STRESS_STYLES[arg])}.\n"
                             "Послушай: <code>/voice Алая ведьма у горы моей после титров</code>")
        return
    rows = [f"{'👉 ' if k == shorts_daily.stress_style else '• '}<code>/stress {k}</code> — {escape(v)}"
            for k, v in shorts_pronounce.STRESS_STYLES.items()]
    await message.answer("🗣 <b>Как голос получает ударение из /say</b>\n" + "\n".join(rows)
                         + "\n\nВыбери способ и послушай <code>/voice фраза</code> — какой голос слушает, тот и "
                           "оставь. Потом «🎙 Звук» под роликом.")


@router.message(Command("say"))
async def say_command(
    message: Message,
) -> None:
    if (
        not message.from_user
        or message.from_user.id not in ADMIN_IDS
    ):
        return
    # «/say» и пары с новой строки: делим по любому пробелу или переносу, а не только по пробелу
    parts = (message.text or "").split(maxsplit=1)
    await say_action(message, parts[1].strip() if len(parts) > 1 else "")


async def say_action(message: Message, arg: str) -> None:
    table = dict(await load_pronunciations())
    if arg.lower().startswith("del "):
        word = arg[4:].strip()
        if table.pop(word, None) is None:
            await message.answer("Такого слова в своём словаре нет.")
            return
        await set_shorts_setting("pronounce", json.dumps(table, ensure_ascii=False) if table else None)
        await message.answer(f"✅ Удалено: {escape(word)}")
        return
    if "=" in arg:
        pairs = shorts_pronounce.parse_pairs(arg)
        if not pairs:
            await message.answer("Формат: <code>/say IMAX = Аймакс</code> (несколько слов — каждое с новой строки)")
            return
        table.update(pairs)
        await set_shorts_setting("pronounce", json.dumps(table, ensure_ascii=False))
        rows = [f"• «{escape(w)}» → «{escape(shorts_pronounce.mark_stress(v))}»" for w, v in pairs]
        await message.answer("✅ Теперь читается так:\n" + "\n".join(rows)
                             + f"\nПроверить: <code>/voice {escape(pairs[0][0])}</code>")
        return

    lines = ["🗣 <b>Произношение в озвучке</b>"]
    if table:
        lines.append("\nСвои замены:")
        lines += [f"• {escape(w)} → {escape(shorts_pronounce.mark_stress(s))}" for w, s in table.items()]
    lines.append(f"\nВстроенных замен: {len(shorts_pronounce.DEFAULT)} (IMAX, Netflix, HBO, Marvel…)")
    lines.append(
        "\n<code>/say IMAX = Аймакс</code> — как читать слово\n"
        "<code>/say замок = замОк</code> — ударение: заглавная гласная\n"
        "<code>/say del IMAX</code> — удалить свою замену\n"
        "<code>/voice текст</code> — послушать, как прозвучит"
    )
    await message.answer("\n".join(lines))


@router.message(Command("voice"))
async def voice_command(
    message: Message,
) -> None:
    if (
        not message.from_user
        or message.from_user.id not in ADMIN_IDS
    ):
        return
    await voice_action(message, (message.text or "").partition(" ")[2].strip())


async def voice_action(message: Message, text: str) -> None:
    if not text:
        await message.answer("Использование: <code>/voice Премьера в IMAX уже завтра</code>")
        return
    await message.answer("⏳ Озвучиваю…")
    path = SHORTS_DIR / f"voice_test_{message.chat.id}.mp3"
    try:
        await load_pronunciations()
        await load_voice()
        await load_fact_channels()
        spoken = (await shorts_daily.spoken_lines([text[:500]]))[0]
        path.parent.mkdir(parents=True, exist_ok=True)
        await shorts_say_to_file(spoken, path)
        engine = shorts_daily.last_engine
        note = (f"\n\n🎙 Голос: {escape(await current_voice_name())} (Lumean)" if engine == "lumean" else "" if engine == "edge"
                else f"\n\n⚠️ Основной голос не ответил — это запасной голос: {escape(engine[:300])}")
        if engine != "lumean" and shorts_daily.lumean_error:
            note += f"\n\n⚠️ ElevenLabs (Lumean) не сработал: {escape(shorts_daily.lumean_error)}"
        await message.answer_audio(
            FSInputFile(path, filename="voice.mp3"),
            caption=f"🗣 {escape(spoken[:800])}{note}",
        )
    except Exception as exc:
        logging.exception("Voice test failed")
        reason = "сервис голоса не ответил вовремя" if isinstance(exc, asyncio.TimeoutError) else str(exc)
        await message.answer(
            f"❌ Не получилось озвучить: <code>{escape(type(exc).__name__)}: {escape(reason[:300])}</code>"
        )
    finally:
        path.unlink(missing_ok=True)


TRAILER_WORDS = ("трейлер", "тизер", "trailer", "teaser")


async def trailer_sources(item: dict[str, Any]) -> tuple[list[tuple[str, str]], list[str]]:
    """Откуда можно взять фрагмент трейлера: ([(источник, url)], заметки для отчёта)."""
    sources: list[tuple[str, str]] = []
    notes: list[str] = []
    try:
        trailer = await resolve_trailer(int(item["id"]))
        if trailer and trailer.get("youtube_id"):
            sources.append(("YouTube", f"https://www.youtube.com/watch?v={trailer['youtube_id']}"))
        elif trailer and trailer.get("direct_url"):
            sources.append(("Прямая ссылка", str(trailer["direct_url"])))
        else:
            notes.append("YouTube: официальный трейлер не найден")
    except Exception as exc:
        logging.exception("Trailer lookup failed")
        notes.append(f"YouTube: ошибка поиска ({type(exc).__name__})")

    title = kp.title(item)
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                RUTUBE_SEARCH_API,
                params={"query": f"{title} трейлер", "format": "json"},
                headers={"accept": "application/json"},
            ) as resp:
                status = resp.status
                data = await resp.json(content_type=None)
        results = [v for v in (data.get("results") or []) if isinstance(v, dict)][:15]
        # сначала ролики со словом «трейлер/тизер», потом любые с названием фильма
        want = _rutube_norm(title)
        picked = next((v for v in results if any(w in str(v.get("title") or "").lower() for w in TRAILER_WORDS)
                       and _rutube_video_id(v)), None)
        picked = picked or next((v for v in results if want and want in _rutube_norm(v.get("title"))
                                 and _rutube_video_id(v)), None)
        if picked:
            sources.append(("Rutube", f"https://rutube.ru/video/{_rutube_video_id(picked)}/"))
            notes.append(f"Rutube: нашёлся «{str(picked.get('title') or '')[:70]}»")
        else:
            top = "; ".join(str(v.get("title") or "")[:40] for v in results[:3]) or "пусто"
            notes.append(f"Rutube: HTTP {status}, результатов {len(results)}, подходящих нет ({top})")
    except Exception as exc:
        logging.exception("Rutube trailer search failed")
        notes.append(f"Rutube: поиск не удался ({type(exc).__name__}: {_safe_err(exc)[:120]})")
    return sources, notes


@router.message(Command("trailer"))
async def trailer_test_command(
    message: Message,
) -> None:
    """Проверка: качаются ли фрагменты трейлеров с этого сервера."""
    if (
        not message.from_user
        or message.from_user.id not in ADMIN_IDS
    ):
        return
    await trailer_action(message, (message.text or "").partition(" ")[2].strip())


async def trailer_action(message: Message, query: str) -> None:
    if not query:
        await message.answer(
            "Использование: <code>/trailer Интерстеллар</code>\n"
            "или ссылка на видео (VK, Rutube, YouTube…): <code>/trailer https://vkvideo.ru/video-…</code>"
        )
        return
    if query.startswith(("http://", "https://")):
        # проверка конкретной ссылки: качается ли с этого сервера
        await message.answer("⏳ Пробую скачать 4 секунды по ссылке…")
        out = SHORTS_DIR / f"clip_test_{message.chat.id}_link.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            await shorts_download_clip(query, out, start=5.0)
            await message.answer_video(FSInputFile(out), caption="✅ Скачалось с этого сервера")
        except Exception as exc:
            logging.exception("Clip link test failed for %s", query)
            await message.answer(
                f"❌ <code>{escape(type(exc).__name__)}: {escape(_safe_err(exc)[:300])}</code>",
                disable_web_page_preview=True,
            )
        finally:
            out.unlink(missing_ok=True)
        return
    await message.answer("⏳ Ищу трейлер и пробую скачать 4 секунды…")
    try:
        found = await kp.search(query, limit=1)
    except Exception as exc:
        await message.answer(f"❌ Поиск не удался: <code>{escape(_safe_err(exc)[:200])}</code>")
        return
    if not found:
        await message.answer("Фильм не найден.")
        return
    item = found[0]
    sources, notes = await trailer_sources(item)
    report = [f"🎬 <b>{escape(kp.title(item))}</b>"] + [f"ℹ️ {escape(n)}" for n in notes]
    for name, url in sources:
        out = SHORTS_DIR / f"clip_test_{message.chat.id}_{name}.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            await shorts_download_clip(url, out)
            await message.answer_video(FSInputFile(out), caption=f"✅ {name}: фрагмент скачался")
            report.append(f"✅ {name}")
        except Exception as exc:
            logging.exception("Clip test failed for %s", url)
            report.append(f"❌ {name}: <code>{escape(type(exc).__name__)}: {escape(_safe_err(exc)[:250])}</code>")
        finally:
            out.unlink(missing_ok=True)
    await message.answer("\n".join(report), disable_web_page_preview=True)


@router.message(Command("clips"))
async def clips_command(
    message: Message,
) -> None:
    if (
        not message.from_user
        or message.from_user.id not in ADMIN_IDS
    ):
        return

    arg = (message.text or "").partition(" ")[2].strip().lower()
    if arg in ("on", "off"):
        await set_shorts_setting("clips", None if arg == "on" else "off")
    state = "выключены" if await shorts_setting("clips") == "off" else "включены"
    await message.answer(
        f"🎞 Фрагменты трейлеров в подборках: <b>{state}</b>.\n"
        "Трейлеры ищутся на Rutube (работает с сервера в Европе), 5 секунд без звука.\n\n"
        "<code>/clips on</code> — включить, <code>/clips off</code> — выключить"
    )


async def expenses_rows() -> list[str]:
    """Расходы: озвучка, ИИ Lumean, музыка за сегодня / 7 / 30 дней, кошельки бюджета, журнал Lumean для сверки."""
    rows: list[str] = []
    rows.append("\n📊 <b>Расходы (сохраняются и после обновлений)</b>")
    for label, days in (("Сегодня", 1), ("7 дней", 7), ("30 дней", 30)):
        s = shorts_usage.summary(days)
        def num(v: Any) -> str:  # 12345 → «12 345»
            return f"{int(v or 0):,}".replace(",", " ")
        rows.append(f"{label}: ИИ Lumean — {num(s.get('llm_calls'))} запр. ({num(s.get('llm_tokens'))} ток.), "
                    f"озвучка — {num(s.get('tts_chars'))} симв. ({num(s.get('tts_orders'))} заказ.), "
                    f"музыка — {num(s.get('music_tracks'))} тр.")
    week = shorts_usage.summary(7)["tasks"]
    if week:
        rows.append("ИИ за 7 дней на что: " + ", ".join(
            f"{escape(t)} — {n}" for t, n in sorted(week.items(), key=lambda kv: -kv[1])[:6]))
    rows.append("Цену символа и запроса смотри в кабинете Lumean — так можно посчитать стоимость ролика.")
    rows += await shorts_budget.status_lines()
    lumean_rows = await shorts_budget.lumean_check()
    if lumean_rows:
        rows.append("Lumean за 30 дней по журналу (для сверки цен с кабинетом): " + "; ".join(lumean_rows))
    return rows


async def expenses_action(message: Message) -> None:
    """«💵 Расходы» в админке — сразу, без пробных запросов к ИИ."""
    await message.answer("\n".join(["💵 <b>Расходы</b>"] + await expenses_rows()),
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [_btn("🤖 Проверить ИИ и режим Lumean", "sh:ai")], [_btn("⬅️ Админка", "admin:home")]]))


async def ai_status_action(message: Message) -> None:
    """Какие ИИ подключены; Gemini (и с поиском Google) и Groq — пробный запрос, BeatAPI — ключ, баланс
    и бесплатные модели (без платных запросов)."""
    from shorts import websearch as shorts_websearch
    rows = [
        "🤖 <b>ИИ бота</b>",
        "Посты: свой поиск (Groq/DuckDuckGo/Tavily/Яндекс) → Gemini с поиском Google → пишет сильная модель Lumean → "
        "второй проход-фактчекер.",
        f"0. Lumean ИИ (Claude/GPT): {'включён ✅' if shorts_ai.lumean_llm_on() else 'выключен ❌ (нужен LUMEAN_API_KEY)'}"
        f"; поиск: Tavily {'✅' if shorts_websearch.TAVILY_KEY else '— нет TAVILY_API_KEY'}, Яндекс "
        f"{'✅' if shorts_websearch.YANDEX_KEY and shorts_websearch.YANDEX_FOLDER else '— нет YANDEX_SEARCH_API_KEY/YANDEX_FOLDER_ID'}"
        f", DuckDuckGo {'✅' if shorts_websearch.DDG_ON else '— выключен'}"
        f", Groq-поиск {'✅' if shorts_websearch.groq_key() else '— нет ключа Groq или GROQ_SEARCH=off'}",
        f"1. Gemini: {f'ключей: {len(shorts_ai.gemini_keys(GEMINI_API_KEY))} ✅' if GEMINI_API_KEY else 'нет GEMINI_API_KEY ❌'}"
        f" ({escape(shorts_ai.MODEL)})",
        f"2. Groq: {'ключ есть ✅' if shorts_ai.RESERVE_KEY else 'нет RESERVE_AI_KEY ❌'} ({escape(shorts_ai.RESERVE_MODEL)})",
    ]
    wait = await message.answer("⏳ Отправляю ИИ пробные запросы — до минуты…")
    try:
        for name, ok, info in await shorts_ai.probe(GEMINI_API_KEY):
            rows.append(f"   {'✅' if ok else '❌'} {escape(name)}: {escape(info)}")
    except Exception as exc:
        rows.append(f"   ❌ проверка не удалась: {escape(_safe_err(exc)[:200])}")
    await wait.delete()
    if not shorts_ai.BEAT_KEY:
        rows.append("3. BeatAPI: нет переменной BEAT_API_KEY ❌")
    else:
        wait = await message.answer("⏳ Проверяю BeatAPI…")
        try:
            st = await shorts_ai.beat_status()
            free = st["free"]
            listing = f" — {escape(', '.join(free[:8]))}" + ("…" if len(free) > 8 else "") if free else ""
            rows.append(f"3. BeatAPI: ключ работает ✅, баланс ${float(st['balance'] or 0):.2f}\n"
                        f"   модель: <code>{escape(st['model'])}</code>\n"
                        f"   бесплатных моделей: {len(free)}{listing}")
        except Exception as exc:
            rows.append(f"3. BeatAPI: ❌ {escape(_safe_err(exc)[:300])}")
        await wait.delete()
    await load_ai_mode()
    u = shorts_ai.lumean_usage
    modes = {"important": "только важное (сценарий выпуска, пост по ссылке) + запасной", "all": "всё подряд",
             "off": "выключен для текстов"}
    rows.append(f"\n💳 <b>Lumean для текстов</b>: {modes.get(shorts_ai.LUMEAN_MODE, shorts_ai.LUMEAN_MODE)}")
    if u["date"] == date.today().isoformat() and u["calls"]:
        top = ", ".join(f"{escape(t)} — {n}" for t, n in sorted(u["tasks"].items(), key=lambda kv: -kv[1])[:6])
        tin, tout = f"{u['tokens_in']:,}".replace(",", " "), f"{u['tokens_out']:,}".replace(",", " ")
        rows.append(f"Сегодня запросов к Lumean: {u['calls']} (токенов: {tin} на вход, {tout} на выход).\n"
                    f"На что: {top}")
    else:
        rows.append("Сегодня запросов к Lumean не было (считается с последнего запуска бота).")
    rows.append("Озвучка клоном голоса тоже идёт с баланса Lumean: выпуск на 6–8 минут — самое дорогое.")
    paused = [w for w in (shorts_ai.cooling(n) for n in shorts_ai.NAMES) if w]
    stats = [f"{shorts_ai.NAMES.get(n, n)}: ✅ {st['ok']} / ❌ {st['fail']} (последний: {st['last']})"
             for n, st in shorts_ai.provider_stats.items()]
    if paused or stats:
        rows.append("\n🔁 <b>ИИ с запуска бота</b> (переключений на запасной: " + str(shorts_ai.switches) + ")\n"
                    + "\n".join(escape(x) for x in stats + [f"⏸ {w}" for w in paused]))
    rows += await expenses_rows()
    await message.answer("\n".join(rows), reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [_btn(("✅ " if shorts_ai.LUMEAN_MODE == "important" else "") + "💳 Lumean: только важное", "aim:important")],
        [_btn(("✅ " if shorts_ai.LUMEAN_MODE == "all" else "") + "💎 Lumean: всё (дороже)", "aim:all"),
         _btn(("✅ " if shorts_ai.LUMEAN_MODE == "off" else "") + "🆓 Lumean: выкл", "aim:off")]]))


async def load_ai_mode() -> None:
    mode = await shorts_setting("lumean_mode") or "important"
    shorts_ai.LUMEAN_MODE = mode if mode in ("important", "all", "off") else "important"


@router.callback_query(F.data.startswith("aim:"))
async def ai_mode_button(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS or not callback.message:
        await callback.answer()
        return
    mode = (callback.data or "")[4:]
    if mode not in ("important", "all", "off"):
        await callback.answer()
        return
    await set_shorts_setting("lumean_mode", None if mode == "important" else mode)
    shorts_ai.LUMEAN_MODE = mode
    await callback.answer({"important": "Lumean — только для важного", "all": "Lumean — для всех текстов",
                           "off": "Lumean для текстов выключен"}[mode], show_alert=True)


@router.message(Command("ai"))
async def ai_command(message: Message) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    await ai_status_action(message)


CLONE_POLL_SECONDS = 20
CLONE_WAIT_SECONDS = 40 * 60


async def set_voice(service: str | None, voice: str | None, name: str | None) -> None:
    await set_shorts_setting("lumean_service", service)
    await set_shorts_setting("lumean_voice", voice)
    await set_shorts_setting("lumean_voice_name", name)


async def clone_off(message: Message) -> None:
    await set_voice(None, None, None)
    await message.answer("✅ Вернул стандартный голос ElevenLabs.")


@router.message(Command("clone"))
async def clone_command(message: Message) -> None:
    """Клон своего голоса для озвучки (Lumean, Fish Audio): ответить /clone на голосовое сообщение."""
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    arg = (message.text or "").partition(" ")[2].strip().lower()
    if arg in ("off", "выкл", "default"):
        await clone_off(message)
        return
    reply = message.reply_to_message
    await clone_action(message, (reply.voice or reply.audio) if reply else None)


async def clone_action(message: Message, media: Any) -> None:
    if not shorts_lumean.enabled():
        await message.answer("Нет ключа Lumean: добавьте переменную LUMEAN_API_KEY на Railway.")
        return
    if media is None:
        current = await shorts_setting("lumean_voice")
        await message.answer(
            "🎙 <b>Свой голос для роликов</b>\n\n"
            "1. Запишите голосовое сообщение 30–60 секунд: обычная речь, как в роликах, "
            "в тишине, без музыки и чужих голосов.\n"
            "2. Ответьте на него командой <code>/clone</code> (свайп по сообщению влево → написать /clone).\n\n"
            "Проще — кнопками: /shorts → ⚙️ Настройки → 🎙 Свой голос.\n"
            "Клонирование занимает несколько минут, бот напишет, когда голос будет готов.\n"
            "Вернуть стандартный голос: <code>/clone off</code>\n\n"
            + ("Сейчас: <b>ваш голос</b> (клон)." if current else "Сейчас: стандартный голос ElevenLabs.")
        )
        return
    if (media.duration or 0) < 10:
        await message.answer("Запись слишком короткая — нужно хотя бы 10 секунд, лучше 30–60.")
        return
    buf = io.BytesIO()
    await message.bot.download(media.file_id, destination=buf)
    try:
        clone = await shorts_lumean.create_clone(buf.getvalue(), f"KinoMir360 {datetime.now(MSK):%d.%m %H:%M}")
    except Exception as exc:
        logging.exception("Voice clone failed")
        await message.answer(f"❌ Не получилось отправить голос на клонирование: <code>{escape(_safe_err(exc)[:400])}</code>")
        return
    await message.answer("⏳ Голос отправлен на клонирование. Обычно это несколько минут — напишу, когда будет готов.")

    async def wait_ready() -> None:
        waited = 0
        status, info = clone.get("status"), clone
        while status not in ("ready", "failed", "archived") and waited < CLONE_WAIT_SECONDS:
            await asyncio.sleep(CLONE_POLL_SECONDS)
            waited += CLONE_POLL_SECONDS
            try:
                info = await shorts_lumean.clone_status(str(clone["id"]))
                status = info.get("status")
            except Exception:
                logging.exception("Voice clone status failed")
        if status == "ready" and info.get("voice_id"):
            await set_shorts_setting("clone_voice", str(info["voice_id"]))  # чтобы вернуться к нему кнопкой
            await set_voice("fishaudio", str(info["voice_id"]), "ваш голос (клон)")
            await message.answer(
                "✅ Ваш голос готов! Теперь ролики озвучиваются им.\n"
                "Проверить и вернуть стандартный — кнопками ниже.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [_btn("🔊 Проверить голос", "sh:ask:voice")],
                    [_btn("↩️ Стандартный голос", "sh:clone_off")]]),
            )
        else:
            reason = (info.get("error") or {}).get("reason") if isinstance(info.get("error"), dict) else info.get("error")
            await message.answer(
                f"❌ Клон не получился ({escape(str(reason or status))}). "
                "Попробуйте запись длиннее и чище — без музыки и шума."
            )

    task = asyncio.create_task(wait_ready())
    shorts_jobs.add(task)
    task.add_done_callback(shorts_jobs.discard)


@router.message(Command("music"))
async def music_command(
    message: Message,
) -> None:
    if (
        not message.from_user
        or message.from_user.id not in ADMIN_IDS
    ):
        return

    arg = (message.text or message.caption or "").partition(" ")[2].strip().lower()
    if arg in ("default", "сброс"):
        await set_shorts_setting("music_file_id", None)
        await message.answer("✅ Вернул стандартную музыку для шортсов.")
        return

    source = message if (message.audio or message.document) else message.reply_to_message
    media = None
    if source:
        media = source.audio or source.document or source.voice
    if not media:
        await message.answer(
            "Пришли аудиофайл с подписью /music "
            "или ответь /music на сообщение с музыкой.\n"
            "Музыка должна быть без авторских прав.\n"
            "<code>/music default</code> — вернуть стандартную."
        )
        return

    await set_shorts_setting("music_file_id", media.file_id)
    await message.answer("✅ Музыка для шортсов обновлена — со следующего ролика.")


# ---------------------------------------------------------------- панель шортсов (кнопки)

SHORTS_GENRES = ["фантастика", "ужасы", "триллер", "комедия", "боевик", "детектив"]


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


SHORTS_SECTIONS: dict[str, tuple[str, str]] = {  # раздел меню YouTube → (кнопка, заголовок)
    "shorts": ("🎞 Шортсы: готовые темы", "🎞 <b>Шортсы</b> — бот сам находит фильмы и новости, ролик за 2–5 минут."),
    "long": ("📺 Длинные видео 16:9", "📺 <b>Длинные видео для YouTube</b> — выпуск «Новости кино», обзоры, перевод "
                                    "трейлеров."),
    "own": ("✍️ По моему тексту и сценарию", "✍️ <b>По твоему материалу</b> — свой текст, сценарий, подборка, "
                                          "история персонажа."),
    "facts": ("💡 Факты о фильме", "💡 <b>Факты о фильме</b> — шортс «5 фактов»: новое, классика или выбери фильм."),
    "tools": ("🛠 Инструменты", "🛠 <b>Инструменты</b> — диктор, скачать видео, остановить сборку."),
}


def shorts_menu() -> InlineKeyboardMarkup:
    """YouTube и видео — по разделам, как посты: не один длинный список кнопок."""
    rows = [[_btn(f"{HOLIDAYS[k][0]}: шортс к празднику", f"sh:hol:{k}")]
            for k in holidays_now(datetime.now(MSK).date())]
    rows += [[_btn("🎬 Шортс: тема дня", "sh:go:"), _btn("🎞 Выпуск «Новости кино»", "pt:wv")]]
    rows += [[_btn(SHORTS_SECTIONS[k][0] + " ▸", f"sh:sec:{k}")] for k in ("shorts", "long", "own", "facts")]
    rows += [[_btn(SHORTS_SECTIONS["tools"][0] + " ▸", "sh:sec:tools"), _btn("⚙️ Настройки ▸", "sh:settings")],
             [_btn("⬅️ В админку", "admin:home")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def shorts_section(name: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if name == "shorts":
        rows = [[_btn("🎬 Тема дня", "sh:go:"), _btn("📰 Новости кино", "sh:go:news")],
                [_btn("🆕 Онлайн-премьеры", "sh:go:online"), _btn("🍿 Кинопрокат", "sh:go:cinema")],
                [_btn("📺 Сериалы", "sh:go:series"), _btn("⏳ Скоро в кино", "sh:go:upcoming")],
                [_btn("🎭 Топ жанра ▸", "sh:genres"), _btn("🎂 День рождения", "sh:bdl")],
                [_btn("🎉 Шортс к празднику ▸", "sh:hol")]]
    elif name == "long":
        rows = [[_btn("🎞 Выпуск «Новости кино» (8 мин)", "pt:wv")],
                [_btn("🔁 Перемонтировать последний выпуск", "pt:wvre")],
                [_btn("🎞 Обзор фильма 16:9", "rv:start"), _btn("🎙 Перевод трейлера", "dub:start")]]
    elif name == "own":
        rows = [[_btn("🔮 Что мы знаем о фильме (слухи до премьеры)", "lo:wwk")],
                [_btn("🎬 Свой сценарий: озвучу как есть", "lo:own")],
                [_btn("📰 Своя новость: вставлю текст", "lo:news")],
                [_btn("🎯 Своя подборка", "cs:text"), _btn("🔁 По образцу (видео)", "cs:video")],
                [_btn("📖 История персонажа", "lo:start")]]
    elif name == "facts":
        rows = [[_btn("💡 Новое (с 2015)", "sh:go:facts_new"), _btn("📼 Классика", "sh:go:facts_old")],
                [_btn("🎲 Случайный фильм", "sh:go:facts")],
                # поиск с подсказками прямо при наборе (inline-режим бота)
                [InlineKeyboardButton(text="🔎 Найти фильм/сериал", switch_inline_query_current_chat="")],
                [_btn("✍️ Ввести название", "sh:ask:facts"), _btn("🔎 Интернет или свой текст", "fx:start")]]
    elif name == "tools":
        rows = [[_btn("🎙 Диктор: текст → проверка → озвучка", "pt:nar")],
                [_btn("⬇️ Скачать видео по ссылке (Rutube, VK, YouTube)", "sh:ask:dl")],
                [_btn("⏹ Остановить сборку", "pt:stop")]]
    rows.append([_btn("⬅️ YouTube и видео", "sh:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def shorts_genres_menu() -> InlineKeyboardMarkup:
    rows = [[_btn(g.capitalize(), f"sh:go:genre:{g}") for g in SHORTS_GENRES[k:k + 2]]
            for k in range(0, len(SHORTS_GENRES), 2)]
    rows.append([_btn("🎲 Случайный жанр", "sh:go:genre")])
    rows.append([_btn("⬅️ Назад", "sh:sec:shorts")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def shorts_settings_menu() -> InlineKeyboardMarkup:
    clips_on = await shorts_setting("clips") != "off"
    mode = await shorts_setting("sub_mode") or "soft"
    hidden = await shorts_setting("yt_privacy") == "unlisted"
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn(f"🔒 Подписка на канал: {SUB_MODES.get(mode, mode)}", "sh:sub")],
        [_btn("▶️ На YouTube: " + ("сначала скрыто (проверю права) 🔐" if hidden else "сразу открыто 🌍"), "sh:ytpriv")],
        [_btn(f"🎞 Трейлеры: {'вкл ✅' if clips_on else 'выкл ❌'}", "sh:clips")],
        [_btn("🎵 Сменить музыку", "sh:ask:music"), _btn("↩️ Стандартная", "sh:music_default")],
        [_btn("🎼 Музыка Lumean под настроение ▸", "sh:mlib")],
        [_btn("🗣 Произношение", "sh:say"), _btn("➕ Добавить слово", "sh:ask:say")],
        [_btn("🎚 Ударения в озвучке", "ud:menu")],
        [_btn("🎙 Голос озвучки ▸", "sh:clone"), _btn("🔊 Проверить голос", "sh:ask:voice")],
        [_btn("📡 Проверить ленты", "sh:feeds"), _btn("➕ Добавить ленту", "sh:ask:feed")],
        [_btn("📡 Каналы для фактов и новостей", "sh:tgch")],
        [_btn("🎬 Проверить трейлер", "sh:ask:trailer"), _btn("🤖 Проверить ИИ", "sh:ai")],
        [_btn("⬅️ Назад", "sh:menu")],
    ])


async def current_voice_name() -> str:
    if not await shorts_setting("lumean_voice"):
        return "стандартный голос ElevenLabs"
    return await shorts_setting("lumean_voice_name") or "выбранный голос"


async def find_saved_clone() -> str | None:
    """Клон из настроек; нет (новая база после переезда) — уже готовый клон из аккаунта Lumean."""
    clone = await shorts_setting("clone_voice")
    if clone or not shorts_lumean.enabled():
        return clone
    try:
        ready = await shorts_lumean.ready_clones()
    except Exception:
        logging.exception("Lumean clones lookup failed")
        return None
    if ready:
        clone = str(ready[0]["voice_id"])
        await set_shorts_setting("clone_voice", clone)
    return clone


async def shorts_clone_menu() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "🎙 <b>Голос озвучки роликов</b>\n\n"
        f"Сейчас: <b>{escape(await current_voice_name())}</b>.\n\n"
        "• <b>Мужские / Женские голоса</b> — послушать образцы и выбрать любой (прослушивание бесплатно).\n"
        "• <b>Записать свой голос</b> — пришлите голосовое 30–60 секунд: обычная речь, "
        "как в роликах, в тишине, без музыки и чужих голосов.\n"
        "• <b>Стандартный голос</b> — вернуть голос по умолчанию."
    )
    rows = [[_btn("👨 Мужские голоса", "sh:vl:male"), _btn("👩 Женские голоса", "sh:vl:female")]]
    if await find_saved_clone():
        rows.append([_btn("👤 Мой голос (клон)", "sh:vmine")])
    rows += [
        [_btn("🎤 Записать свой голос", "sh:ask:clone")],
        [_btn("🔊 Проверить голос", "sh:ask:voice"), _btn("↩️ Стандартный голос", "sh:clone_off")],
        [_btn("⬅️ Назад", "sh:settings")],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _voice_label(voice: dict[str, Any]) -> str:
    name = str(voice.get("name") or voice["voice_id"]).split(" - ")[0]
    return name[:40]


async def find_library_voice(voice_id: str) -> tuple[dict[str, Any] | None, str]:
    for gender in ("male", "female"):
        for voice in await shorts_lumean.library(gender):
            if voice["voice_id"] == voice_id:
                return voice, gender
    return None, "male"


async def voice_list_action(message: Message, gender: str) -> tuple[str, InlineKeyboardMarkup] | None:
    try:
        voices = await shorts_lumean.library(gender)
    except Exception as exc:
        logging.exception("Voice library failed")
        await message.answer(f"❌ Не получилось загрузить голоса: <code>{escape(_safe_err(exc)[:300])}</code>")
        return None
    rows = [[_btn(f"▶️ {_voice_label(v)}", f"sh:vp:{v['voice_id']}") for v in voices[k:k + 2]]
            for k in range(0, len(voices), 2)]
    rows.append([_btn("⬅️ Назад", "sh:clone")])
    title = "👨 Мужские" if gender == "male" else "👩 Женские"
    return (f"{title} <b>голоса</b>\n\nНажмите на голос — пришлю образец, под ним кнопка «Выбрать».",
            InlineKeyboardMarkup(inline_keyboard=rows))


async def voice_preview_action(message: Message, voice_id: str) -> None:
    try:
        voice, gender = await find_library_voice(voice_id)
        if not voice or not voice.get("preview_url"):
            await message.answer("У этого голоса нет образца — выберите и проверьте кнопкой «🔊 Проверить голос».")
            data = None
        else:
            data = await shorts_lumean.preview(str(voice["preview_url"]))
    except Exception as exc:
        logging.exception("Voice preview failed")
        await message.answer(f"❌ Не получилось загрузить образец: <code>{escape(_safe_err(exc)[:300])}</code>")
        return
    name = _voice_label(voice) if voice else voice_id
    about = escape(str((voice or {}).get("description") or "")[:300])
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("✅ Выбрать этот голос", f"sh:vs:{voice_id}")],
        [_btn("⬅️ К списку", f"sh:vl:{gender}")],
    ])
    caption = f"🎙 <b>{escape(name)}</b>" + (f"\n{about}" if about else "") + \
        "\n\n<i>Образец может быть на другом языке — по-русски проверьте после выбора.</i>"
    if data:
        await message.answer_audio(BufferedInputFile(data, filename=f"{name}.mp3"), caption=caption,
                                   reply_markup=markup)
    else:
        await message.answer(caption, reply_markup=markup)


SHORTS_MENU_TEXT = (
    "▶️ <b>YouTube и видео KinoMir360</b>\n\n"
    "Выбери раздел. Ролик приходит сюда вместе с SEO для YouTube и TikTok; шортс дня — сам в 8:00 по Москве, "
    "выпуск «Новости кино» — по средам и воскресеньям. Посты в Telegram — в админке, «📢 Telegram-канал»."
)

SHORTS_ASKS = {
    "facts": (ShortsState.facts_title, "💡 Напиши название фильма или сериала — соберу 5 фактов о нём."),
    "say": (ShortsState.say_entry,
            "🗣 Напиши слово и как его читать через «=».\n"
            "Например: <code>IMAX = Аймакс</code>\n"
            "Ударение — заглавной гласной: <code>замок = замОк</code>"),
    "voice": (ShortsState.voice_text, "🔊 Напиши фразу — пришлю, как она прозвучит в озвучке."),
    "trailer": (ShortsState.trailer_query,
                "🎬 Напиши название фильма или пришли ссылку на видео (VK, Rutube) — проверю, качается ли трейлер."),
    "feed": (ShortsState.feed_url, "📡 Пришли ссылку на RSS-ленту новостей кино."),
    "dl": (ShortsState.download_url, "⬇️ Пришли ссылку на видео — Rutube, VK, YouTube, Дзен и др. Скачаю со звуком "
                                     "(до 720p) и пришлю сюда; длинное — частями до 50 МБ."),
    "clone": (ShortsState.clone_audio,
              "🎤 Запишите и пришлите сюда голосовое сообщение на 30–60 секунд — сделаю из него ваш голос."),
    "tgch": (ShortsState.fact_channels,
             "📡 Пришли список каналов одним сообщением — каждый с новой строки, ссылкой или @именем "
             "(например <code>https://t.me/KinoMir360</code>). Этот список заменит текущий."),
    "tgadd": (ShortsState.fact_channels_add,
              "➕ Пришли ссылки на каналы (можно несколько, хоть слитно) — добавлю их к текущему списку."),
    "music": (ShortsState.music_file, "🎵 Пришли аудиофайл (mp3) — он станет фоновой музыкой. Только музыка без АП!"),
}


@router.message(Command("shorts"))
async def shorts_panel_command(
    message: Message,
    state: FSMContext,
) -> None:
    if (
        not message.from_user
        or message.from_user.id not in ADMIN_IDS
    ):
        return
    await state.clear()
    await message.answer(SHORTS_MENU_TEXT, reply_markup=shorts_menu())


@router.callback_query(F.data.startswith("sh:"))
async def shorts_panel(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if callback.from_user.id not in ADMIN_IDS or not callback.message:
        await callback.answer()
        return
    message = callback.message
    data = callback.data or ""
    await callback.answer()

    async def show(text: str, markup: InlineKeyboardMarkup) -> None:
        try:
            await message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await message.answer(text, reply_markup=markup)

    if data == "sh:menu":
        await state.clear()
        await show(SHORTS_MENU_TEXT, shorts_menu())
    elif data.startswith("sh:sec:") and data[7:] in SHORTS_SECTIONS:
        await show(SHORTS_SECTIONS[data[7:]][1], shorts_section(data[7:]))
    elif data == "sh:hol":
        await show("🎉 <b>Шортс к празднику</b> — выбери праздник:", holidays_menu("sh:hol:", "sh:sec:shorts"))
    elif data.startswith("sh:hol:") and data[7:] in HOLIDAYS:
        await holiday_short(message, state, data[7:])
    elif data == "sh:genres":
        await show("🎭 <b>Топ какого жанра собрать?</b>", shorts_genres_menu())
    elif data == "sh:settings":
        await show("⚙️ <b>Настройки шортсов</b>", await shorts_settings_menu())
    elif data == "sh:bdl":  # именинники дня — автор выбирает, про кого шортс
        wait = await message.answer("🎂 Ищу, у кого из кино сегодня день рождения…")
        today = datetime.now(MSK).date()
        try:
            people = await shorts_daily.wiki.birthdays(today.month, today.day, limit=20)
        except Exception as exc:  # noqa: BLE001
            await wait.edit_text(f"❌ Не получилось: <code>{escape(_safe_err(exc)[:300])}</code>")
            return
        if not people:
            await wait.edit_text("Сегодня в Википедии нет именинников из кино 🤷")
            return
        rows = [[_btn(f"🎂 {p['name']}, {today.year - int(p['born'][:4])}", f"sh:go:birthday:{p['imdb']}")]
                for p in people if p.get("imdb")][:20]
        rows.append([_btn("🎲 Самый известный (автоматически)", "sh:go:birthday")])
        await wait.edit_text("🎂 <b>Сегодня дни рождения</b> — от самых известных. Выбери, про кого шортс "
                             "(лучшие фильмы и роли):", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    elif data.startswith("sh:go:"):
        await start_short(message, data[len("sh:go:"):])
    elif data.startswith("sh:ask:"):
        target, prompt = SHORTS_ASKS[data[len("sh:ask:"):]]
        await state.set_state(target)
        await message.answer(prompt + "\n\n<i>Передумал — /cancel</i>")
    elif data == "sh:sub":  # по кругу: мягкая → строгая → выключена
        modes = list(SUB_MODES)
        cur = await shorts_setting("sub_mode") or "soft"
        new = modes[(modes.index(cur) + 1) % len(modes)] if cur in modes else "soft"
        await set_shorts_setting("sub_mode", new)
        global _sub_mode
        _sub_mode = (0.0, new)
        await show("⚙️ <b>Настройки шортсов</b>\n\n🔒 Обязательная подписка на канал: "
                   f"<b>{SUB_MODES[new]}</b>", await shorts_settings_menu())
    elif data == "sh:ytpriv":
        hidden = await shorts_setting("yt_privacy") == "unlisted"
        await set_shorts_setting("yt_privacy", None if hidden else "unlisted")
        await show("⚙️ <b>Настройки шортсов</b>", await shorts_settings_menu())
        if not hidden:
            await message.answer(
                "🔐 Теперь ролики загружаются на YouTube <b>по ссылке</b> (не видны подписчикам). Открой YouTube "
                "Studio → ролик → вкладка «Проверки»: если нет претензий по авторским правам, поставь «Открытый "
                "доступ». Так меньше риск страйка или блокировки монетизации.")
    elif data == "sh:clips":
        off = await shorts_setting("clips") == "off"
        await set_shorts_setting("clips", None if off else "off")
        await show("⚙️ <b>Настройки шортсов</b>", await shorts_settings_menu())
    elif data == "sh:music_default":
        await set_shorts_setting("music_file_id", None)
        await message.answer("✅ Вернул стандартную музыку для шортсов.")
    elif data == "sh:say":
        await say_action(message, "")
    elif data == "sh:feeds":
        await feeds_action(message, "")
    elif data == "sh:tgch":
        await channels_action(message)
    elif data == "sh:tgch_default":
        await set_shorts_setting("fact_channels", None)
        await load_fact_channels()
        await message.answer(f"✅ Вернул стандартный список: {len(shorts_tgsearch.DEFAULT_CHANNELS)} каналов.")
    elif data == "sh:ai":
        await ai_status_action(message)
    elif data == "sh:mlib":
        await show(*await music_library_menu())
    elif data.startswith("sh:mlib:"):
        await music_make_action(message, data[len("sh:mlib:"):])
    elif data == "sh:mown":
        await show("➕ <b>Свои треки</b>\n\nК какому настроению добавить? Потом пришли mp3 — можно несколько "
                   "подряд. Бесплатная музыка: mixkit.co, pixabay.com/music, Фонотека YouTube "
                   "(в YouTube Studio → «Фонотека»).", music_own_menu())
    elif data.startswith("sh:mown:") and data[8:] in shorts_music.MOODS:
        await state.set_state(ShortsState.music_own)
        await state.update_data(music_mood=data[8:])
        await message.answer(f"🎵 Пришли трек для «{shorts_music.MOODS[data[8:]][0]}» файлом mp3 (до 20 МБ). "
                             "Можно несколько подряд.\n\n<i>Передумал — /cancel</i>")
    elif data == "sh:mownclr":
        await set_shorts_setting("music_own", None)
        await message.answer("🗑 Свои треки убраны — снова играют треки Lumean.")
    elif data == "sh:clone":
        await show(*await shorts_clone_menu())
    elif data == "sh:clone_off":
        await clone_off(message)
    elif data.startswith("sh:vl:"):
        menu = await voice_list_action(message, data[len("sh:vl:"):])
        if menu:
            await show(*menu)
    elif data.startswith("sh:vp:"):
        await voice_preview_action(message, data[len("sh:vp:"):])
    elif data.startswith("sh:vs:"):
        voice_id = data[len("sh:vs:"):]
        voice, _ = await find_library_voice(voice_id)
        name = _voice_label(voice) if voice else voice_id
        await set_voice("elevenlabs", voice_id, f"{name} (ElevenLabs)")
        await message.answer(f"✅ Теперь ролики озвучивает голос <b>{escape(name)}</b>.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [_btn("🔊 Проверить по-русски", "sh:ask:voice")],
                                 [_btn("⬅️ К голосам", "sh:clone")]]))
    elif data == "sh:vmine":
        clone = await shorts_setting("clone_voice")
        if clone:
            await set_voice("fishaudio", clone, "ваш голос (клон)")
            await message.answer("✅ Снова озвучиваю вашим голосом.")


def _typed(message: Message) -> str:
    """Текст ответа на вопрос панели (команды не перехватываем)."""
    return (message.text or "").strip()


@router.message(ShortsState.facts_title, F.text, ~F.text.startswith("/"))
async def shorts_facts_title(message: Message, state: FSMContext) -> None:
    await state.clear()
    await start_short(message, f"facts {_typed(message)}")


@router.message(ShortsState.say_entry, F.text, ~F.text.startswith("/"))
async def shorts_say_entry(message: Message, state: FSMContext) -> None:
    if "=" not in _typed(message):
        await message.answer("Нужен формат «слово = как читать», например <code>IMAX = Аймакс</code>.")
        return
    await state.clear()
    await say_action(message, _typed(message))


@router.message(ShortsState.voice_text, F.text, ~F.text.startswith("/"))
async def shorts_voice_text(message: Message, state: FSMContext) -> None:
    await state.clear()
    await voice_action(message, _typed(message))


@router.message(ShortsState.trailer_query, F.text, ~F.text.startswith("/"))
async def shorts_trailer_query(message: Message, state: FSMContext) -> None:
    await state.clear()
    await trailer_action(message, _typed(message))


@router.message(ShortsState.feed_url, F.text, ~F.text.startswith("/"))
async def shorts_feed_url(message: Message, state: FSMContext) -> None:
    if not _typed(message).startswith(("http://", "https://")):
        await message.answer("Нужна ссылка, начинается с https://")
        return
    await state.clear()
    await feeds_action(message, f"add {_typed(message)}")


@router.message(ShortsState.fact_channels, F.text, ~F.text.startswith("/"))
@router.message(ShortsState.fact_channels_add, F.text, ~F.text.startswith("/"))
async def shorts_fact_channels(message: Message, state: FSMContext) -> None:
    """Список каналов: «Изменить список» заменяет его, «Добавить каналы» — дописывает к текущему."""
    adding = await state.get_state() == ShortsState.fact_channels_add.state
    channels, bad = shorts_tgsearch.split_channels(_typed(message))
    if not channels:
        await message.answer("Не вижу каналов — нужны ссылки вида https://t.me/имя или @имя.")
        return
    await state.clear()
    old = await fact_channels() if adding else []
    added = [c for c in channels if c.lower() not in {o.lower() for o in old}]
    result = (old + added)[: shorts_tgsearch.MAX_CHANNELS]
    await set_shorts_setting("fact_channels", json.dumps(result))
    await load_fact_channels()
    note = f"\n⚠️ Не понял: {escape(', '.join(bad[:10]))}" if bad else ""
    head = f"✅ Добавил {len(added)}, всего {len(result)} каналов." if adding else f"✅ Сохранил {len(result)} каналов."
    await message.answer(head + note)
    await channels_action(message)


dl_lock = asyncio.Lock()


async def download_action(message: Message, url: str) -> None:
    """⬇️ Скачать видео по ссылке: yt-dlp со звуком, до 720p; больше 50 МБ — частями."""
    if dl_lock.locked():
        await message.answer("⏳ Уже качаю другое видео — пришли ссылку через пару минут.")
        return
    wait = await message.answer("⬇️ Качаю видео… Длинное может занять несколько минут.")
    work = SHORTS_DIR / f"dl_{time.time_ns()}"
    work.mkdir(parents=True, exist_ok=True)

    async def job() -> None:
        try:
            async with dl_lock:
                video = await shorts_clips.download_video(url, work)
                parts = await shorts_clips.split_for_telegram(video)
                for k, part in enumerate(parts, 1):
                    caption = f"⬇️ Часть {k} из {len(parts)}" if len(parts) > 1 else "⬇️ Готово"
                    await message.bot.send_video(message.chat.id, FSInputFile(part, filename=f"video_{k}.mp4"),
                                                 caption=caption, supports_streaming=True, request_timeout=600)
            await wait.delete()
        except Exception as exc:  # noqa: BLE001
            logging.exception("Video download failed: %s", url)
            await wait.edit_text(f"❌ Не скачалось: <code>{escape(_safe_err(exc)[-400:])}</code>")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    _run_job(job)


@router.message(ShortsState.download_url, F.text, ~F.text.startswith("/"))
async def download_url_message(message: Message, state: FSMContext) -> None:
    await state.clear()
    urls = URL_RE.findall(message.text or "")
    if not urls:
        await message.answer("Не вижу ссылки. Нажми «⬇️ Скачать видео» и пришли ссылку ещё раз.")
        return
    await download_action(message, urls[0].rstrip(").,»\"'"))


@router.message(Command("dl"))
async def download_command(message: Message) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    urls = URL_RE.findall(message.text or "")
    if not urls:
        await message.answer("⬇️ Пришли так: <code>/dl https://rutube.ru/video/…</code>")
        return
    await download_action(message, urls[0].rstrip(").,»\"'"))


@router.message(ShortsState.music_file, F.audio | F.document | F.voice)
async def shorts_music_file(message: Message, state: FSMContext) -> None:
    media = message.audio or message.document or message.voice
    await state.clear()
    await set_shorts_setting("music_file_id", media.file_id)
    await message.answer("✅ Музыка для шортсов обновлена — со следующего ролика.")


@router.message(ShortsState.clone_audio, F.voice | F.audio)
async def shorts_clone_audio(message: Message, state: FSMContext) -> None:
    await state.clear()
    await clone_action(message, message.voice or message.audio)


# ---------------------------------------------------------------- обзор фильма 16:9

REVIEW_TIMEOUT = 30 * 60


MUSIC_VARIANTS = 2  # треков на настроение; Lumean берёт плату за каждый
music_jobs: set[asyncio.Task] = set()
music_lock = asyncio.Lock()


async def music_library_menu() -> tuple[str, InlineKeyboardMarkup]:
    lib = await music_library()
    own = await _music_json("music_own")
    rows = [f"{label}: {len(lib.get(m) or [])} тр." + (" (свои)" if own.get(m) else "")
            for m, (label, _, _) in shorts_music.MOODS.items()]
    missing = [m for m in shorts_music.MOODS if not lib.get(m)]
    custom = await shorts_setting("music_file_id")
    text = (
        "🎼 <b>Музыка Lumean под настроение</b>\n"
        "Бот один раз заказывает треки у Lumean и дальше сам подбирает их к ролику: хоррор — мрачная, "
        "комедия — весёлая, новости — бодрая. Трек 30 с плавно склеен до ~2 минут.\n\n"
        + "\n".join(rows)
        + f"\n\n💰 Каждый трек оплачивается в Lumean отдельно (по {MUSIC_VARIANTS} трека на настроение)."
        + "\n➕ Или добавь свои треки — бесплатные с mixkit.co, pixabay.com/music, Фонотеки YouTube: "
        "скачай mp3 и пришли боту. Свои играют вместо треков Lumean."
        + ("\n⚠️ Сейчас стоит своя музыка — она важнее библиотеки. Чтобы играла библиотека, нажми "
           "«↩️ Стандартная» в настройках." if custom else "")
    )
    buttons = []
    if missing:
        buttons.append([_btn(f"🎼 Заказать недостающие ({len(missing) * MUSIC_VARIANTS} тр.)", "sh:mlib:all")])
    moods = list(shorts_music.MOODS.items())
    for k in range(0, len(moods), 2):
        buttons.append([_btn(f"🔄 {label}", f"sh:mlib:{m}") for m, (label, _, _) in moods[k:k + 2]])
    buttons.append([_btn("➕ Свои треки (Mixkit и др.)", "sh:mown")])
    if own:
        buttons.append([_btn("🗑 Убрать все свои треки", "sh:mownclr")])
    buttons.append([_btn("⬅️ Назад", "sh:settings")])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


async def music_make_action(message: Message, which: str) -> None:
    """Заказать треки у Lumean: «all» — для настроений без треков, иначе — заново для одного настроения."""
    if not shorts_lumean.enabled():
        await message.answer("❌ Нет ключа Lumean (LUMEAN_API_KEY) — музыку заказать негде.")
        return
    if music_jobs:
        await message.answer("⏳ Музыка уже заказывается — дождись, пришлю треки сюда.")
        return
    lib = await music_library()
    moods = [m for m in shorts_music.MOODS if not lib.get(m)] if which == "all" else \
        [which] if which in shorts_music.MOODS else []
    if not moods:
        await message.answer("✅ Треки есть для всех настроений. Заменить — кнопка 🔄 у нужного.")
        return
    status = await message.answer(f"🎼 Заказываю у Lumean {len(moods) * MUSIC_VARIANTS} тр. "
                                  f"({len(moods)} настр.) — займёт несколько минут, треки пришлю сюда.")
    task = asyncio.create_task(music_make_job(message, status, moods))
    music_jobs.add(task)
    task.add_done_callback(music_jobs.discard)


async def music_make_job(message: Message, status: Message, moods: list[str]) -> None:
    gate = asyncio.Semaphore(3)  # по три заказа сразу
    done: list[str] = []
    failed: list[str] = []

    async def one(mood: str) -> None:
        label = shorts_music.MOODS[mood][0]
        work = SHORTS_DIR / "music_new" / mood
        try:
            async with gate:
                raw = await shorts_lumean.make_music(shorts_music.prompt(mood), work, mood, MUSIC_VARIANTS)
            ids = []
            for i, path in enumerate(raw, 1):
                long = await shorts_music.make_long(path, path.with_name(f"{path.stem}_long.mp3"))
                sent = await message.answer_audio(
                    FSInputFile(long, filename=f"kinomir360_{mood}_{i}.mp3"),
                    caption=f"🎼 {label} — вариант {i}", title=f"KinoMir360 {label} {i}", performer="Lumean")
                if sent.audio:
                    ids.append(sent.audio.file_id)
            if not ids:
                raise RuntimeError("Lumean не прислал файлов")
            async with music_lock:
                lib = await music_library()
                lib[mood] = ids
                await set_shorts_setting("music_lib", json.dumps(lib))
            done.append(label)
        except Exception as exc:
            logging.exception("Shorts: music for %s failed", mood)
            failed.append(f"{label}: {escape(_safe_err(exc)[:200])}")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    await asyncio.gather(*(one(m) for m in moods))
    report = []
    if done:
        report.append(f"✅ Готово: {', '.join(done)}. Со следующего ролика музыка подбирается под настроение.")
    if failed:
        report.append("❌ Не вышло:\n" + "\n".join(failed))
    try:
        await status.reply("\n\n".join(report) or "Ничего не заказано.")
    except Exception:
        await message.answer("\n\n".join(report) or "Ничего не заказано.")


async def music_library() -> dict[str, list[str]]:
    """Треки по настроениям: {настроение: [file_id в Telegram]}. Свои треки автора (Mixkit и т. п.) важнее
    треков Lumean: у настроения, куда автор добавил свои, играют только они."""
    lib, own = await _music_json("music_lib"), await _music_json("music_own")
    return {m: own.get(m) or lib.get(m) or [] for m in shorts_music.MOODS if own.get(m) or lib.get(m)}


async def _music_json(key: str) -> dict[str, list[str]]:
    try:
        lib = json.loads(await shorts_setting(key) or "{}")
    except ValueError:
        return {}
    return {m: [str(f) for f in ids] for m, ids in lib.items() if m in shorts_music.MOODS and isinstance(ids, list)}


OWN_TRACKS_MAX = 10  # своих треков на одно настроение


def music_own_menu() -> InlineKeyboardMarkup:
    moods = list(shorts_music.MOODS.items())
    rows = [[_btn(label, f"sh:mown:{m}") for m, (label, _, _) in moods[k:k + 2]] for k in range(0, len(moods), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows + [[_btn("⬅️ Назад", "sh:mlib")]])


@router.message(ShortsState.music_own, F.audio | F.document)
async def music_own_file(message: Message, state: FSMContext) -> None:
    """Свой трек в библиотеку настроения (скачанный с Mixkit, Pixabay, YouTube Audio Library…)."""
    media = message.audio or message.document
    mood = (await state.get_data()).get("music_mood")
    if mood not in shorts_music.MOODS:
        await state.clear()
        await message.answer("Начни заново: Настройки → «🎼 Музыка под настроение» → «➕ Свои треки».")
        return
    name = str(getattr(media, "file_name", "") or "").lower()
    if message.document and not (str(media.mime_type or "").startswith("audio/") or name.endswith(
            (".mp3", ".m4a", ".wav", ".ogg", ".aac", ".flac"))):
        await message.answer("Это не аудио — пришли трек файлом mp3 (или m4a, wav).")
        return
    if (media.file_size or 0) > TG_DOWNLOAD_LIMIT:
        await message.answer("⚠️ Файл больше 20 МБ — бот не может его скачать. Пришли mp3 поменьше.")
        return
    async with music_lock:
        own = await _music_json("music_own")
        ids = own.get(mood) or []
        if len(ids) >= OWN_TRACKS_MAX:
            await message.answer(f"Хватит: у настроения уже {OWN_TRACKS_MAX} своих треков.")
            return
        ids.append(media.file_id)
        own[mood] = ids
        await set_shorts_setting("music_own", json.dumps(own))
    label = shorts_music.MOODS[mood][0]
    await message.answer(
        f"✅ {label}: своих треков {len(ids)}. Теперь у этого настроения играют только твои треки "
        "(вперемешку). Пришли ещё или нажми «Готово».",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("✅ Готово", "sh:mlib")]]))


async def sync_music_library(bot: Bot) -> None:
    """Скачать треки библиотеки, которых ещё нет на диске (после перезапуска диск пустой), и убрать лишние."""
    for mood, ids in (await music_library()).items():
        folder = shorts_music.DIR / mood
        folder.mkdir(parents=True, exist_ok=True)
        wanted = {f"{i}_{fid[-16:]}.mp3": fid for i, fid in enumerate(ids)}
        for old in folder.glob("*.mp3"):
            if old.name not in wanted:
                old.unlink(missing_ok=True)
        for name, fid in wanted.items():
            if not (folder / name).exists():
                try:
                    await bot.download(fid, destination=folder / name)
                except Exception:
                    logging.exception("Shorts: music library download failed (%s)", mood)


async def custom_music(bot: Bot, work: Path) -> Path | None:
    """Своя фоновая музыка (кнопка «Сменить музыку»), если задана. Иначе ролик сам возьмёт трек
    библиотеки Lumean под настроение — здесь только докачиваем её на диск."""
    try:
        await sync_music_library(bot)
    except Exception:
        logging.exception("Shorts: music library sync failed")
    file_id = await shorts_setting("music_file_id")
    if not file_id:
        return None
    music = work / "music_custom"
    try:
        await bot.download(file_id, destination=music)
        return music
    except Exception:
        logging.exception("Shorts: custom music download failed")
        return None


def review_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("🤖 ИИ напишет сам", "rv:ai")],
        [_btn("✍️ Мой текст + ИИ дополнит", "rv:mix")],
        [_btn("📝 Только мой текст", "rv:own")],
        [_btn("❌ Отмена", "rv:cancel")],
    ])


async def review_ask_title(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(ReviewState.title)
    await message.answer(
        "🎞 <b>Обзор фильма 16:9</b>\n\nНапиши название фильма или сериала (можно с годом: "
        "<code>Дюна 2021</code>).\n\n<i>Передумал — /cancel</i>"
    )


async def review_pick(message: Message, state: FSMContext, query: str) -> None:
    wait = await message.answer("🔎 Ищу фильм…")
    display = ""  # название автора, если на Кинопоиске фильм называется иначе
    try:
        film = await shorts_review.find_film(kp._get, query)
    except Exception as exc:
        # на Кинопоиске так не называется — ИИ ищет настоящее название в интернете и Telegram-каналах
        await wait.edit_text("🔎 На Кинопоиске так не называется — ищу настоящее название в интернете "
                             "и Telegram-каналах…")
        await load_fact_channels()
        try:
            film, note = await shorts_daily.find_film_smart(kp._get, query, GEMINI_API_KEY)
        except Exception:
            logging.exception("Review smart film search failed")
            film, note = None, ""
        display = shorts_daily.display_name(query) if film else ""
        if not film:
            logging.warning("Review film search failed: %s", exc)
            await wait.edit_text(f"❌ Не нашёл «{escape(query)}» ни на Кинопоиске, ни в интернете — "
                                 "напиши название иначе (можно по-английски или с годом).")
            return
        await message.answer(escape(note) + f"\nВ ролике оставлю ваше название: «{escape(display)}»")
    title = display or shorts_daily._title(film)
    year = f" ({film['year']})" if film.get("year") else ""
    await state.set_state(None)
    await state.update_data(review_film=int(film["id"]), review_title=title, review_notes="", review_script=None,
                            review_display=display,
                            review_media=[])
    await wait.edit_text(
        f"🎞 Обзор: <b>«{escape(title)}»{year}</b>\n\n"
        "Как напишем текст?\n"
        "• <b>ИИ напишет сам</b> — по данным Кинопоиска: сюжет, создатели, факты.\n"
        "• <b>Мой текст + ИИ</b> — пришлёшь слух, разбор или мысли, ИИ дополнит и оформит.\n"
        "• <b>Только мой текст</b> — озвучу как есть.\n\n"
        "Перед озвучкой текст придёт на проверку — его можно поправить.",
        reply_markup=review_menu(),
    )


async def show_script(message: Message, state: FSMContext, script: dict[str, Any], can_redo: bool,
                      brief: bool = False) -> None:
    data = await state.get_data()
    if data.get("review_display"):  # фильм нашёлся под другим названием — в ролике название автора
        script["display"] = data["review_display"]
    await state.update_data(review_script=script)
    chars = shorts_review.script_chars(script)
    service = await shorts_setting("lumean_service")
    if shorts_lumean.enabled():
        tokens = chars * (2 if service in ("fishaudio", "cartesia") else 1)
        cost = f", ≈{tokens:,} токенов Lumean".replace(",", " ")
    else:
        cost = ""
    head = (f"📝 <b>Сценарий обзора «{escape(data.get('review_title') or '')}»</b>\n"
            f"{chars} символов ≈ {max(1, round(chars / 14 / 60 * 10) / 10)} мин{cost}"
            + (f"\nНазвание для YouTube: <i>{escape(script['title'])}</i>" if script.get("title") else ""))
    chunks, cur = [], head
    for block in [] if brief else shorts_review.script_text(script).split("\n\n"):
        piece = escape(block)
        if len(cur) + len(piece) + 2 > 3800:
            chunks.append(cur)
            cur = piece
        else:
            cur = f"{cur}\n\n{piece}"
    chunks.append(cur)
    media = data.get("review_media") or []
    rows = [[_btn("✅ Озвучить и собрать видео", "rv:go")], [_btn("✏️ Поправить текст", "rv:edit")],
            [_btn(f"📎 Свои фото/видео ({len(media)})" if media else "📎 Добавить свои фото/видео", "rv:media")]]
    if can_redo:
        rows.append([_btn("🔄 Написать заново", "rv:redo")])
    rows.append([_btn("❌ Отмена", "rv:cancel")])
    for k, chunk in enumerate(chunks):
        last = k == len(chunks) - 1
        await message.answer(chunk, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if last else None)


async def review_generate(message: Message, state: FSMContext, notes: str) -> None:
    data = await state.get_data()
    wait = await message.answer("✍️ ИИ пишет сценарий, обычно до минуты…")
    try:
        film = shorts_daily.renamed(await kp._get(f"/movie/{data['review_film']}"), data.get("review_display") or "")
        script = await shorts_review.write_script(film, notes, GEMINI_API_KEY)
    except Exception as exc:
        logging.exception("Review script failed")
        await wait.edit_text(f"❌ Сценарий не написался: <code>{escape(_safe_err(exc)[:400])}</code>",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [_btn("🔄 Ещё раз", "rv:redo")], [_btn("❌ Отмена", "rv:cancel")]]))
        return
    await state.update_data(review_notes=notes)
    await wait.delete()
    await show_script(message, state, script, can_redo=True)


async def send_review(bot: Bot, chat_id: int, result: Any) -> None:
    seo = result.seo
    await bot.send_photo(chat_id, FSInputFile(result.cover, filename="cover.jpg"),
                         caption="🖼 Обложка для YouTube (1280×720)")
    sent = await bot.send_video(
        chat_id,
        FSInputFile(result.video, filename="kinomir360_review.mp4"),
        caption=f"🎞 <b>{escape(seo['title'])}</b>",
        width=1280,
        height=720,
        supports_streaming=True,
        request_timeout=600,  # файл до 50 МБ — загрузка дольше обычного
    )
    has_project = await keep_project(chat_id, sent.message_id, result.video.parent,
                                     {"mode": "review", "caption": f"🎞 <b>{escape(seo['title'])}</b>", "seo": seo})
    for label, key in (("Название", "title"), ("Описание (с таймкодами)", "description"),
                       ("Теги", "tags"), ("Закреплённый комментарий", "pinned")):
        await bot.send_message(chat_id, f"<b>{label}:</b>\n<pre>{escape(seo[key])}</pre>")
    if seo.get("warning"):
        await bot.send_message(chat_id, escape(seo["warning"]))
    if has_project:
        await fix_buttons(bot, chat_id, sent.message_id)


async def run_review(bot: Bot, chat_id: int, script: dict[str, Any], status: Message,
                     media: list[dict[str, str]] | None = None) -> None:
    async with shorts_lock:
        await load_pronunciations()
        await load_voice()
        await load_fact_channels()
        work = SHORTS_DIR / datetime.now(MSK).strftime("review_%Y%m%d_%H%M%S")
        work.mkdir(parents=True, exist_ok=True)
        try:
            music = await custom_music(bot, work)
            await set_shorts_setting("running", json.dumps({"chats": [chat_id], "theme": "обзор фильма"}))
            user_files: list[Path] = []
            for k, item in enumerate(media or []):
                path = work / f"user{k}{item['ext']}"
                try:
                    await bot.download(item["file_id"], destination=path)
                    user_files.append(path)
                except Exception:
                    logging.exception("Review: user file download failed")
            progress = progress_updater(status, "обзор")
            shorts_daily.on_progress, shorts_daily.last_stage = progress, ""
            result = await asyncio.wait_for(shorts_review.make_review(
                kp._get, work, script, music, TMDB_API_KEY,
                trailer_clips=await shorts_setting("clips") != "off",
                user_media=user_files, gemini_key=GEMINI_API_KEY,
            ), timeout=REVIEW_TIMEOUT)
            await progress(100, "Готово — отправляю видео")
            await send_review(bot, chat_id, result)
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"сборка шла дольше {REVIEW_TIMEOUT // 60} минут и была прервана — "
                f"застряла на этапе: {shorts_daily.last_stage or 'начало'}."
            ) from None
        finally:
            shorts_daily.on_progress = None
            await clear_running()
            shutil.rmtree(work, ignore_errors=True)


async def start_review(message: Message, script: dict[str, Any], media: list[dict[str, str]]) -> None:
    if shorts_lock.locked():
        await message.answer("⏳ Сейчас собирается другой ролик — нажми «Озвучить» ещё раз через пару минут.")
        return
    status = await message.answer(progress_bar(0, "Начинаю, обычно это 5–15 минут", "обзор"))

    async def job() -> None:
        try:
            await run_review(message.bot, message.chat.id, script, status, media)
        except Exception as exc:
            logging.exception("Review failed")
            try:
                await status.edit_text("❌ Сборка остановилась — причина ниже")
            except TelegramBadRequest:
                pass
            await message.answer(f"❌ Обзор не собрался:\n<code>{escape(_safe_err(exc)[-700:])}</code>")

    task = asyncio.create_task(job())
    shorts_jobs.add(task)
    task.add_done_callback(shorts_jobs.discard)


@router.message(Command("review"))
async def review_command(message: Message, state: FSMContext) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    query = (message.text or "").partition(" ")[2].strip()
    if query:
        await state.clear()
        await review_pick(message, state, query)
    else:
        await review_ask_title(message, state)


@router.callback_query(F.data.startswith("rv:"))
async def review_buttons(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id not in ADMIN_IDS or not callback.message:
        await callback.answer()
        return
    await callback.answer()
    message, action = callback.message, (callback.data or "")[3:]
    data = await state.get_data()
    if action == "start":
        await review_ask_title(message, state)
        return
    if action == "cancel":
        await state.clear()
        await message.answer("Обзор отменён.")
        return
    if not data.get("review_film"):
        await message.answer("Начни заново: кнопка «🎞 Обзор фильма 16:9» или /review")
        return
    if action == "ai":
        await review_generate(message, state, "")
    elif action == "redo":
        await review_generate(message, state, data.get("review_notes") or "")
    elif action == "mix":
        await state.set_state(ReviewState.notes)
        await message.answer(
            "✍️ Пришли свой текст одним сообщением: слух, разбор, мнение, тезисы — как угодно. "
            "ИИ сохранит твои мысли, дополнит фактами о фильме и разложит по разделам.\n\n"
            "<i>Передумал — /cancel</i>")
    elif action == "own":
        await state.set_state(ReviewState.own)
        await message.answer(
            "📝 Пришли готовый текст обзора одним сообщением — озвучу как есть.\n"
            "Разделы можно отметить строкой <code>## Название</code> — они станут плашками в видео "
            "и главами на YouTube. Без них разделю по абзацам.\n\n<i>Передумал — /cancel</i>")
    elif action == "edit":
        if not data.get("review_script"):
            return
        await state.set_state(ReviewState.edit)
        await message.answer(
            "✏️ Скопируй сценарий выше, поправь и пришли одним сообщением. "
            "Строки <code>## Название</code> — начало раздела.\n\n<i>Передумал — /cancel</i>")
    elif action == "go":
        script = data.get("review_script")
        if not script:
            await message.answer("Сначала нужен сценарий — выбери, как его написать.", reply_markup=review_menu())
            return
        await state.clear()
        await start_review(message, script, data.get("review_media") or [])
    elif action == "media":
        await state.set_state(ReviewState.media)
        await message.answer(
            "📎 Присылай свои фото и видео — можно по одному или альбомом. ИИ поставит каждый файл "
            "туда, где о нём речь в тексте, остальные — равномерно по ролику. Из видео возьму 1–3 "
            "фрагмента по 5 секунд, звук не нужен.\n"
            "Бот Telegram скачивает файлы <b>до 20 МБ</b>, больше — пришли сжатую версию.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [_btn("✅ Готово", "rv:media_done")], [_btn("🗑 Убрать все мои файлы", "rv:media_clear")]]))
    elif action == "style":
        await set_shorts_setting("lore_style", "calm" if await lore_style() == "dynamic" else "dynamic")
        await message.answer("⚡ Монтаж: динамичный — видео трейлера на весь экран, новый кадр каждые ~2 с, "
                             "переходы, субтитры по одному слову, без заставки в конце." if await lore_style() ==
                             "dynamic" else "🎞 Монтаж: спокойный — как раньше (кадр по центру, субтитры фразами, "
                             "заставка «Подпишись» в конце).")
        await show_lore(message, state, brief=True)
    elif action in ("media_done", "media_clear"):
        if action == "media_clear":
            await state.update_data(review_media=[])
        await state.set_state(None)
        script = data.get("review_script")
        if script:
            await show_script(message, state, script, can_redo=bool(script.get("title")), brief=True)


REVIEW_MEDIA_MAX = 20
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # бот Telegram скачивает файлы до 20 МБ
review_media_lock = asyncio.Lock()


@router.message(ReviewState.media, F.photo | F.video | F.animation | F.document)
async def review_media(message: Message, state: FSMContext) -> None:
    if message.photo:
        file, ext = message.photo[-1], ".jpg"
    elif message.video or message.animation:
        file, ext = message.video or message.animation, ".mp4"
    else:
        doc = message.document
        mime = str(doc.mime_type or "")
        if not mime.startswith(("image/", "video/")):
            await message.answer("Это не фото и не видео — пропускаю.")
            return
        file = doc
        ext = Path(doc.file_name or "").suffix.lower() or (".jpg" if mime.startswith("image/") else ".mp4")
    if (file.file_size or 0) > TG_DOWNLOAD_LIMIT:
        await message.answer("⚠️ Файл больше 20 МБ — бот Telegram не может его скачать. Пришли сжатую версию.")
        return
    async with review_media_lock:  # альбом приходит пачкой сообщений одновременно — не теряем файлы
        media = (await state.get_data()).get("review_media") or []
        if len(media) >= REVIEW_MEDIA_MAX:
            await message.answer(f"Хватит: уже {REVIEW_MEDIA_MAX} файлов. Нажми «✅ Готово».")
            return
        media.append({"file_id": file.file_id, "ext": ext})
        await state.update_data(review_media=media)
    await message.answer(f"📎 Принято: {len(media)}", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[_btn("✅ Готово", "rv:media_done")]]))


@router.message(ReviewState.title, F.text, ~F.text.startswith("/"))
async def review_title(message: Message, state: FSMContext) -> None:
    await review_pick(message, state, _typed(message))


@router.message(ReviewState.notes, F.text, ~F.text.startswith("/"))
async def review_notes(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await review_generate(message, state, _typed(message))


@router.message(ReviewState.own, F.text, ~F.text.startswith("/"))
@router.message(ReviewState.edit, F.text, ~F.text.startswith("/"))
async def review_own_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    editing = await state.get_state() == ReviewState.edit.state
    old = data.get("review_script") or {}
    try:
        script = shorts_review.parse_script(_typed(message), int(data["review_film"]),
                                            old.get("title", "") if editing else "")
    except (ValueError, KeyError):
        await message.answer("Не вижу текста — пришли его одним сообщением.")
        return
    await state.set_state(None)
    await show_script(message, state, script, can_redo=editing and bool(old.get("title")))


# ---------------------------------------------------------------- факты из своего текста

async def show_facts(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    facts = data["fx_facts"]
    what = {"rumors": "новостей и слухов", "review": "тезисов обзора"}.get(data.get("fx_mode") or "", "фактов")
    rows = [f"💡 <b>{len(facts)} {what} о «{escape(data.get('fx_label') or data['fx_title'])}»</b> — "
            "проверь, всё ли правда:", ""]
    rows += [f"{i}. {escape(f)}" for i, f in enumerate(facts, 1)]
    if data.get("fx_sources"):
        rows += ["", f"🔎 <i>Источники: {escape(', '.join(data['fx_sources'][:8]))}</i>"]
    if data.get("fx_tg_media"):
        rows.append(f"📸 <i>Из Telegram-каналов: {len(data['fx_tg_media'])} фото/видео — ИИ поставит подходящие к фактам</i>")
    media = data.get("fx_media") or []
    await message.answer("\n".join(rows)[:4000], reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [_btn("✅ Собрать шортс", "fx:go")], [_btn("✏️ Поправить факты", "fx:edit")],
        [_btn(f"📎 Свои фото/видео ({len(media)})" if media else "📎 Добавить свои фото/видео", "fx:media")],
        [_btn("🔄 Выбрать заново", "fx:redo")], [_btn("❌ Отмена", "fx:cancel")]]))


async def facts_condense(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    wait = await message.answer("✍️ ИИ выбирает факты из текста…")
    try:
        facts = await shorts_ai.condense_facts(GEMINI_API_KEY, data["fx_title"], data["fx_text"])
    except Exception as exc:
        logging.exception("Facts condense failed")
        await wait.edit_text(f"❌ Не получилось: <code>{escape(_safe_err(exc)[:400])}</code>",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [_btn("🔄 Ещё раз", "fx:redo")], [_btn("❌ Отмена", "fx:cancel")]]))
        return
    await wait.delete()
    await state.update_data(fx_facts=facts)
    await show_facts(message, state)


def _film_label(film: dict[str, Any]) -> str:
    kind = "сериал" if film.get("isSeries") or "series" in str(film.get("type") or "") else ""
    extra = ", ".join(x for x in (str(film.get("year") or ""), kind,
                                  "ещё не вышел" if shorts_daily.is_upcoming(film) else "") if x)
    return shorts_daily._title(film) + (f" ({extra})" if extra else "")


async def facts_find(message: Message, state: FSMContext) -> None:
    """Кнопка «ИИ найдёт в интернете»: ищем фильм; есть и старый, и ещё не вышедший с таким названием —
    спрашиваем, какой. Дальше facts_web сам решает: факты или новости и слухи."""
    data = await state.get_data()
    try:
        old = await shorts_daily.find_film(kp._get, data["fx_title"])
        new = await shorts_daily.find_film(kp._get, data["fx_title"], upcoming=True)
    except Exception as exc:
        logging.exception("Facts film search failed")
        await message.answer(f"❌ Поиск на Кинопоиске не удался: <code>{escape(_safe_err(exc)[:300])}</code>")
        return
    if not old and not new:
        # на Кинопоиске так не называется — ИИ ищет настоящее название в интернете и Telegram-каналах
        await load_fact_channels()
        wait = await message.answer(f"🔎 На Кинопоиске «{escape(data['fx_title'])}» нет — ищу настоящее название "
                                    "в интернете и Telegram-каналах…")
        try:
            old, note = await shorts_daily.find_film_smart(kp._get, data["fx_title"], GEMINI_API_KEY)
        except Exception as exc:
            logging.exception("Smart film search failed")
            old, note = None, ""
            logging.warning("smart search: %s", exc)
        if not old:
            await wait.edit_text(f"Не нашёл «{escape(data['fx_title'])}» ни на Кинопоиске, ни в интернете — "
                                 "напиши название иначе (можно по-английски или с годом).",
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("❌ Отмена", "fx:cancel")]]))
            return
        await wait.edit_text(f"{escape(note)}\nНа Кинопоиске: «{escape(_film_label(old))}»\n"
                             f"В ролике оставлю ваше название: «{escape(shorts_daily.display_name(data['fx_title']))}»")
        await state.update_data(fx_display=shorts_daily.display_name(data["fx_title"]))
        await facts_web(message, state, old)
        return
    if old and new and old["id"] != new["id"] and shorts_daily.is_upcoming(new):
        await message.answer("Какой из них?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_btn(f"🆕 {_film_label(new)}"[:60], f"fx:film:{new['id']}")],
            [_btn(_film_label(old)[:60], f"fx:film:{old['id']}")],
            [_btn("❌ Отмена", "fx:cancel")]]))
        return
    await facts_web(message, state, old or new)


async def facts_web(message: Message, state: FSMContext, film: dict[str, Any] | None = None) -> None:
    """Факты из интернета: ИИ ищет в Google, Википедии и Telegram-каналах (плюс факты Кинопоиска),
    автор проверяет и правит. Фильм ещё не вышел — вместо фактов новости и слухи о нём."""
    data = await state.get_data()
    await load_fact_channels()
    try:
        if film is None:  # «Ещё раз» — тот же фильм
            film = await kp._get(f"/movie/{int(str(data['fx_film'])[3:])}")
    except Exception as exc:
        await message.answer(f"❌ Не получилось: <code>{escape(_safe_err(exc)[:300])}</code>")
        return
    rumors = shorts_daily.is_upcoming(film)
    film = shorts_daily.renamed(film, data.get("fx_display") or "")
    label = shorts_daily._title(film) + (f", {film['year']}" if film.get("year") else "")
    await state.update_data(fx_mode="rumors" if rumors else "web", fx_film=f"id:{film['id']}", fx_label=label)
    what = "новости и слухи" if rumors else "факты"
    await message.answer(f"{'📰' if rumors else '🔎'} «{escape(label)}»"
                         + (" ещё не вышел — собираю новости и слухи." if rumors else " — собираю факты."))
    wait = await message.answer(progress_bar(5, "Ищу в интернете, обычно до минуты", what))
    update = progress_updater(wait, what)
    stage = {"pct": 5, "text": "Ищу в интернете, обычно до минуты"}

    async def report(pct: int, text: str) -> None:
        stage.update(pct=pct, text=text)
        await update(pct, text)

    async def tick() -> None:  # ИИ думает до минуты — полоска понемногу ползёт, чтобы было видно, что идёт работа
        while True:
            await asyncio.sleep(4)
            if stage["pct"] < 90:
                stage["pct"] += 3 if stage["pct"] >= 40 else 1
                await update(stage["pct"], stage["text"])

    ticker = asyncio.create_task(tick())
    try:
        tg_media: list[dict[str, Any]] = []
        facts, sources = await shorts_daily.web_facts(film, shorts_daily.usable_facts(film), GEMINI_API_KEY,
                                                      n=8, rumors=rumors, media_out=tg_media, progress=report)
    except Exception as exc:
        ticker.cancel()
        logging.exception("Facts web search failed")
        await wait.edit_text(f"❌ Не получилось: <code>{escape(_safe_err(exc)[:400])}</code>",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [_btn("🔄 Ещё раз", "fx:redo")], [_btn("📝 Вставлю свой текст", "fx:own")],
                                 [_btn("❌ Отмена", "fx:cancel")]]))
        return
    ticker.cancel()
    await wait.delete()
    await state.update_data(fx_facts=facts, fx_sources=sources,
                            fx_tg_media=[{"image": m.get("image"), "video": m.get("video")} for m in tg_media][:12])
    await show_facts(message, state)


async def run_text_facts(bot: Bot, chat_id: int, title: str, facts: list[str], status: Message,
                         media: list[dict[str, str]] | None = None, rumors: bool = False,
                         tg_media: list[dict[str, Any]] | None = None, review: bool = False,
                         display_title: str = "") -> None:
    async with shorts_lock:
        await load_pronunciations()
        await load_voice()
        await load_fact_channels()
        work = SHORTS_DIR / datetime.now(MSK).strftime("facts_%Y%m%d_%H%M%S")
        work.mkdir(parents=True, exist_ok=True)
        try:
            music = await custom_music(bot, work)
            await set_shorts_setting("running", json.dumps({"chats": [chat_id], "theme": "факты: свой текст"}))
            files: list[Path] = []
            for k, item in enumerate(media or []):
                path = work / f"user{k}{item['ext']}"
                try:
                    await bot.download(item["file_id"], destination=path)
                    files.append(path)
                except Exception:
                    logging.exception("Facts: user file download failed")
            progress = progress_updater(status)
            shorts_daily.on_progress, shorts_daily.last_stage = progress, ""
            result = await asyncio.wait_for(shorts_daily.make_facts_short(
                kp._get, work, title, set(), music, TMDB_API_KEY, GEMINI_API_KEY,
                trailer_clips=await shorts_setting("clips") != "off", custom_facts=facts, user_media=files,
                rumors=rumors, tg_media=tg_media, review=review, display_title=display_title),
                timeout=SHORTS_TIMEOUT)
            shorts_daily.add_english_seo(result)
            await progress(100, "Готово — отправляю ролик")
            await send_short(bot, chat_id, result)
        except asyncio.TimeoutError:
            raise RuntimeError(f"сборка шла дольше {SHORTS_TIMEOUT // 60} минут — "
                               f"застряла на этапе: {shorts_daily.last_stage or 'начало'}.") from None
        finally:
            shorts_daily.on_progress = None
            await clear_running()
            shutil.rmtree(work, ignore_errors=True)


@router.message(SceneReviewState.replacement, F.photo)
async def scene_replacement_photo(message: Message, state: FSMContext) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        await state.clear()
        return
    data = await state.get_data()
    first, index = data.get("scene_review_first"), data.get("scene_review_index")
    if not isinstance(first, int) or not isinstance(index, int):
        await state.clear()
        await message.answer("Сессия замены истекла. Открой проверку сцен заново.")
        return
    folder = proj_dir(message.chat.id, first)
    proj = shorts_project.load(folder)
    cards = (proj or {}).get("rerun", {}).get("cards") or []
    if not proj or not 0 <= index < len(cards):
        await state.clear()
        await message.answer("Проект не найден. Открой проверку сцен заново.")
        return
    photo = message.photo[-1]
    if (photo.file_size or 0) > TG_DOWNLOAD_LIMIT:
        await message.answer("Фото слишком большое. Пришли сжатую версию.")
        return
    path = folder / f"scene_replacement_{index + 1}.jpg"
    await message.bot.download(photo.file_id, destination=path)
    cards[index]["image"] = path.name
    cards[index].pop("clip", None)
    cards[index].pop("overlay", None)
    cards[index]["asset_source"] = "user_replacement"
    cards[index]["visual_match"] = {
        "status": "user_replacement_review", "score": None,
        "reason": "Изображение заменено автором; ожидает подтверждения",
    }
    proj["rerun"]["scene_plan_enabled"] = True
    (folder / shorts_project.FILE).write_text(json.dumps(proj, ensure_ascii=False, default=str), encoding="utf-8")
    await state.clear()
    await message.answer("✅ Изображение принято. Перемонтирую ролик с заменённой сценой…")
    _run_job(lambda: project_remake_job(message.bot, message.chat.id, first))


@router.message(FactsState.title, F.text, ~F.text.startswith("/"))
async def facts_title(message: Message, state: FSMContext) -> None:
    await state.update_data(fx_title=_typed(message), fx_mode="", fx_film="", fx_label="", fx_sources=[],
                            fx_tg_media=[], fx_display="")
    await state.set_state(None)
    await message.answer("Откуда взять факты? ИИ ищет везде — Google, Википедия, Telegram-каналы, Кинопоиск. "
                         "Если фильм ещё не вышел, соберёт новости и слухи о нём.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [_btn("🔎 ИИ найдёт в интернете", "fx:web")],
        [_btn("📝 Вставлю свой текст", "fx:own")],
        [_btn("🎬 Короткий обзор: свои тезисы", "fx:review")],
        [_btn("❌ Отмена", "fx:cancel")]]))


@router.message(FactsState.text, F.text, ~F.text.startswith("/"))
async def facts_text(message: Message, state: FSMContext) -> None:
    if (await state.get_data()).get("fx_mode") == "review":
        # короткий обзор: тезисы автора как есть — строка на карточку
        points = [re.sub(r"^\s*(\d+[.)]|[-•—])\s*", "", ln).strip() for ln in _typed(message).splitlines()]
        points = [p for p in points if len(p) >= 10][:8]
        if len(points) < 3:
            await message.answer("Нужно хотя бы 3 тезиса — каждый с новой строки.")
            return
        await state.set_state(None)
        await state.update_data(fx_text=_typed(message), fx_facts=points, fx_sources=[], fx_film="", fx_label="",
                                fx_tg_media=[])
        await show_facts(message, state)
        return
    await state.set_state(None)
    await state.update_data(fx_text=_typed(message), fx_mode="text", fx_sources=[], fx_film="", fx_label="",
                            fx_tg_media=[])
    await facts_condense(message, state)


@router.message(FactsState.media, F.photo | F.video | F.animation | F.document)
async def facts_media(message: Message, state: FSMContext) -> None:
    if message.photo:
        file, ext = message.photo[-1], ".jpg"
    elif message.video or message.animation:
        file, ext = message.video or message.animation, ".mp4"
    else:
        doc = message.document
        mime = str(doc.mime_type or "")
        if not mime.startswith(("image/", "video/")):
            await message.answer("Это не фото и не видео — пропускаю.")
            return
        file = doc
        ext = Path(doc.file_name or "").suffix.lower() or (".jpg" if mime.startswith("image/") else ".mp4")
    if (file.file_size or 0) > TG_DOWNLOAD_LIMIT:
        await message.answer("⚠️ Файл больше 20 МБ — бот Telegram не может его скачать. Пришли сжатую версию.")
        return
    async with review_media_lock:
        media = (await state.get_data()).get("fx_media") or []
        if len(media) >= 10:
            await message.answer("Хватит: уже 10 файлов. Нажми «✅ Готово».")
            return
        media.append({"file_id": file.file_id, "ext": ext})
        await state.update_data(fx_media=media)
    await message.answer(f"📎 Принято: {len(media)}", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[_btn("✅ Готово", "fx:media_done")]]))


@router.message(FactsState.edit, F.text, ~F.text.startswith("/"))
async def facts_edit(message: Message, state: FSMContext) -> None:
    facts = [re.sub(r"^\s*\d+[.)]\s*", "", ln).strip() for ln in _typed(message).splitlines()]
    facts = [f for f in facts if len(f) >= 10][:8]
    if len(facts) < 3:
        await message.answer("Нужно хотя бы 3 факта — каждый с новой строки.")
        return
    await state.set_state(None)
    await state.update_data(fx_facts=facts)
    await show_facts(message, state)


@router.callback_query(F.data.startswith("fx:"))
async def facts_buttons(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id not in ADMIN_IDS or not callback.message:
        await callback.answer()
        return
    await callback.answer()
    message, action = callback.message, (callback.data or "")[3:]
    if action == "start":
        await state.clear()
        await state.set_state(FactsState.title)
        await message.answer("📝 <b>Факты о фильме</b>: ИИ найдёт их в интернете (или новости и слухи о том, что ещё не вышло) либо возьмёт из твоего текста.\n\nНапиши название фильма или сериала "
                             "(можно с годом: <code>Человек-паук: Новый день 2026</code>).\n\n<i>Передумал — /cancel</i>")
        return
    if action == "cancel":
        await state.clear()
        await message.answer("Отменено.")
        return
    data = await state.get_data()
    if not data.get("fx_title"):
        await message.answer("Начни заново: кнопка «🔎 Факты: интернет или свой текст».")
        return
    if action == "own":
        await state.update_data(fx_mode="text")
        await state.set_state(FactsState.text)
        await message.answer("📝 Вставь текст с фактами одним сообщением (ссылки на источники можно не убирать). "
                             "ИИ выберет 5 самых интересных и ничего не добавит от себя.\n\n<i>Передумал — /cancel</i>")
    elif action == "review":
        await state.update_data(fx_mode="review")
        await state.set_state(FactsState.text)
        await message.answer("🎬 <b>Короткий обзор</b> (вертикальный ролик ~45 с). Пришли 3–8 тезисов одним "
                             "сообщением — каждый с новой строки: о чём фильм, что хорошо, что плохо, итог и "
                             "оценка. Каждый тезис станет карточкой, голос прочитает их как есть.\n\n"
                             "<i>Передумал — /cancel</i>")
    elif action in ("web", "rumors"):  # «rumors» — кнопка из старых сообщений
        await facts_find(message, state)
    elif action.startswith("film:") and action[5:].isdigit():
        try:
            film = await kp._get(f"/movie/{int(action[5:])}")
        except Exception as exc:
            await message.answer(f"❌ Кинопоиск не ответил: <code>{escape(_safe_err(exc)[:300])}</code>")
            return
        await facts_web(message, state, film)
    elif action == "redo" and data.get("fx_mode") == "review":
        await state.set_state(FactsState.text)
        await message.answer("Пришли тезисы обзора заново — каждый с новой строки.")
    elif action == "redo" and data.get("fx_mode") != "text":
        if data.get("fx_film"):
            await facts_web(message, state)
        else:
            await facts_find(message, state)
    elif action == "redo":
        await facts_condense(message, state)
    elif action == "media":
        await state.set_state(FactsState.media)
        await message.answer(
            "📎 Присылай свои фото и видео — по одному или альбомом (до 10). ИИ поставит каждый файл к "
            "факту, о котором он; из видео возьму 5 секунд. Файлы до 20 МБ.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [_btn("✅ Готово", "fx:media_done")], [_btn("🗑 Убрать все мои файлы", "fx:media_clear")]]))
    elif action in ("media_done", "media_clear"):
        if action == "media_clear":
            await state.update_data(fx_media=[])
        await state.set_state(None)
        if data.get("fx_facts"):
            await show_facts(message, state)
    elif action == "edit" and data.get("fx_facts"):
        await state.set_state(FactsState.edit)
        await message.answer("✏️ Скопируй факты, поправь и пришли одним сообщением — каждый с новой строки "
                             "(3–8 фактов).\n\n<i>Передумал — /cancel</i>")
    elif action == "go" and data.get("fx_facts"):
        if shorts_lock.locked():
            await message.answer("⏳ Сейчас собирается другой ролик — нажми «Собрать» ещё раз через пару минут.")
            return
        await state.clear()
        status = await message.answer(progress_bar(0, "Начинаю, обычно это 3–6 минут"))

        async def job() -> None:
            try:
                await run_text_facts(message.bot, message.chat.id, data.get("fx_film") or data["fx_title"],
                                     data["fx_facts"], status, data.get("fx_media") or [],
                                     data.get("fx_mode") == "rumors", data.get("fx_tg_media") or [],
                                     data.get("fx_mode") == "review", data.get("fx_display") or "")
            except Exception as exc:
                logging.exception("Text facts short failed")
                try:
                    await status.edit_text("❌ Сборка остановилась — причина ниже")
                except TelegramBadRequest:
                    pass
                await message.answer(f"❌ Шортс не собрался:\n<code>{escape(_safe_err(exc)[-700:])}</code>")

        task = asyncio.create_task(job())
        shorts_jobs.add(task)
        task.add_done_callback(shorts_jobs.discard)


# ---------------------------------------------------------------- своя подборка и «по образцу»

CUSTOM_TIMEOUT = 25 * 60  # своя подборка до 10 фильмов с отрывками трейлеров — самая долгая сборка
CUSTOM_HELP = (
    "Вставь любой текст: готовый пост с описаниями (ИИ сам вытащит тему, фильмы и напишет фразы для "
    "озвучки по твоим описаниям — и сразу соберу ролик) или простой список — первой строкой тема, "
    "дальше фильм на строку:\n"
    "<code>Фильмы, после которых не уснёшь\nСияние 1980\nРеинкарнация\nОно 2017</code>\n"
    "Порядок — как в ролике: последний станет №1.")


async def custom_ask(message: Message, state: FSMContext, video: bool) -> None:
    await state.clear()
    if video:
        shutil.rmtree(SHORTS_DIR / f"copy_{message.chat.id}", ignore_errors=True)
        await state.set_state(CustomState.video)
        await message.answer(
            "🔁 <b>Сделать по образцу</b>\n\nПришли чужой ролик-подборку: видеофайл до 20 МБ "
            "(с YouTube/TikTok — сохрани на телефон и отправь) или ссылку на Rutube/VK. "
            "ИИ вытащит тему и список фильмов, ты проверишь — и соберём нашу версию: "
            "свои картинки, текст и голос.\n\n<i>Передумал — /cancel</i>")
    else:
        await state.set_state(CustomState.text)
        await message.answer("🎯 <b>Своя подборка</b>\n\n" + CUSTOM_HELP +
                             "\n\n📎 Свои фото и видео (по желанию) пришли <b>до текста</b> — по одному или альбомом, "
                             "до 10 файлов по 20 МБ. ИИ сам поставит каждый к нужному фильму."
                             "\n\n<i>Передумал — /cancel</i>")


async def show_plan(message: Message, state: FSMContext, title: str, films: list[dict[str, Any]]) -> None:
    wait = await message.answer("🔎 Ищу фильмы на Кинопоиске…")
    items = await shorts_copycat.resolve(kp._get, films)
    await _tmdb_fill(items, films)
    await wait.delete()
    found = [it for it in items if it["id"]]
    await state.update_data(cs_title=title, cs_items=items, cs_notes=None)
    rows = [f"🎯 <b>Тема:</b> {escape(title)}", ""]
    for i, it in enumerate(items, 1):
        mark = f"✅ {escape(it['label'])}" if it["id"] else "❌ не нашёл на Кинопоиске"
        rows.append(f"{i}. {escape(it['query'])} — {mark}")
    ok = shorts_daily.CUSTOM_MIN <= len(found) <= shorts_daily.CUSTOM_MAX
    rows += ["", f"В ролик пойдут найденные ({len(found)}), последний — №1." if ok else
             f"⚠️ Нужно от {shorts_daily.CUSTOM_MIN} до {shorts_daily.CUSTOM_MAX} найденных фильмов, сейчас "
             f"{len(found)}. Поправь список."]
    buttons = ([[_btn("✅ Собрать шортс", "cs:go")]] if ok else []) + [
        [_btn("✏️ Изменить тему или список", "cs:edit")], [_btn("❌ Отмена", "cs:cancel")]]
    await message.answer("\n".join(rows), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


async def run_custom(bot: Bot, chat_id: int, title: str, ids: list[int], status: Message,
                     notes: list[str] | None = None, media: list[dict[str, str]] | None = None) -> None:
    async with shorts_lock:
        await load_pronunciations()
        await load_voice()
        await load_fact_channels()
        work = SHORTS_DIR / datetime.now(MSK).strftime("custom_%Y%m%d_%H%M%S")
        work.mkdir(parents=True, exist_ok=True)
        try:
            music = await custom_music(bot, work)
            await set_shorts_setting("running", json.dumps({"chats": [chat_id], "theme": "своя подборка"}))
            files: list[Path] = []
            for k, item in enumerate(media or []):
                path = work / f"user{k}{item['ext']}"
                try:
                    await bot.download(item["file_id"], destination=path)
                    files.append(path)
                except Exception:
                    logging.exception("Custom: user file download failed")
            progress = progress_updater(status)
            shorts_daily.on_progress, shorts_daily.last_stage = progress, ""
            result = await asyncio.wait_for(shorts_daily.make_custom_short(
                kp._get, work, title, ids, music, TMDB_API_KEY,
                trailer_clips=await shorts_setting("clips") != "off", notes=notes, user_media=files,
                gemini_key=GEMINI_API_KEY), timeout=CUSTOM_TIMEOUT)
            await progress(100, "Готово — отправляю ролик")
            await send_short(bot, chat_id, result)
        except asyncio.TimeoutError:
            raise RuntimeError(f"сборка шла дольше {CUSTOM_TIMEOUT // 60} минут — "
                               f"застряла на этапе: {shorts_daily.last_stage or 'начало'}.") from None
        finally:
            shorts_daily.on_progress = None
            await clear_running()
            shutil.rmtree(work, ignore_errors=True)


@router.message(Command("mylist"))
async def custom_command(message: Message, state: FSMContext) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    await custom_ask(message, state, video=False)


def _free_text(text: str) -> bool:
    """Пост с описаниями, а не простой список «тема + фильм на строку»."""
    return any(len(ln) > 70 for ln in text.splitlines()) or bool(re.search(r"[\U0001F300-\U0001FAFF]", text))


async def custom_from_post(message: Message, state: FSMContext, text: str,
                           images: list[Image.Image] | None = None) -> None:
    """Своя подборка из любого текста или из картинок автора: ИИ вытаскивает тему, фильмы и фразы для озвучки,
    бот находит фильмы на Кинопоиске (не нашёл — ищет настоящее название в интернете) и сразу собирает ролик."""
    await load_fact_channels()
    if images is None:  # пересланный пост: постер со списком фильмов пришёл фото, а текст — подписью
        images = await _state_photos(message, state) or None
    wait = await message.answer(("🤖 ИИ узнаёт фильмы на картинках" if images else "🤖 ИИ разбирает текст")
                                + " и ищет о них всё: Кинопоиск, интернет, Telegram-каналы. Обычно 1–2 минуты…")
    try:
        plan = await shorts_ai.extract_plan(GEMINI_API_KEY, text, shorts_daily.CUSTOM_MAX, images)
        items = await shorts_copycat.resolve(kp._get, plan["films"])
        for it, f in zip(items, plan["films"]):
            if not it["id"]:
                film, _ = await shorts_daily.find_film_smart(
                    kp._get, f["ru"] or f["en"], GEMINI_API_KEY,
                    upcoming=bool(f.get("fresh")) and not f.get("year"), exact=True)
                if film:
                    it.update(id=int(film["id"]), label=shorts_daily._title(film) + (
                        f" ({film['year']})" if film.get("year") else ""))
        await _tmdb_fill(items, plan["films"])
        await _fill_notes(wait, plan["films"], items)
    except Exception as exc:
        logging.exception("Custom post parsing failed")
        await wait.edit_text(f"❌ Не получилось разобрать {'картинки' if images else 'текст'}: "
                             f"<code>{escape(_safe_err(exc)[:300])}</code>\n\n" + CUSTOM_HELP)
        await state.set_state(CustomState.text)
        return
    await wait.delete()
    # сериал с номером сезона: голос говорит, что это новый сезон, а не «сериал 2022 года»
    notes = [(f"{f['season']}-й сезон. " if f.get("season") and "сезон" not in f["note"].lower() else "") + f["note"]
             for f in plan["films"]]
    await state.update_data(cs_title=plan["title"], cs_items=items, cs_notes=notes)
    found = [(it, note) for it, note in zip(items, notes) if it["id"]]
    rows = [f"🎯 <b>{escape(plan['title'])}</b>", ""]
    rows += [f"{i}. {escape(it['query'])} — " + (f"✅ {escape(it['label'])}" if it["id"] else "❌ не нашёл")
             for i, it in enumerate(items, 1)]
    if not shorts_daily.CUSTOM_MIN <= len(found) <= shorts_daily.CUSTOM_MAX:
        rows += ["", f"⚠️ Нашёл {len(found)} — нужно от {shorts_daily.CUSTOM_MIN} до {shorts_daily.CUSTOM_MAX}. "
                     "Поправь список."]
        await message.answer("\n".join(rows), reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_btn("✏️ Изменить тему или список", "cs:edit")], [_btn("❌ Отмена", "cs:cancel")]]))
        return
    media = (await state.get_data()).get("cs_media") or []
    rows += ["", "Собираю ролик — не найденные пропущу."
             + (f" Ваших фото/видео: {len(media)} — ИИ поставит их к нужным фильмам." if media else "")]
    await message.answer("\n".join(rows))
    await start_custom(message, state, plan["title"], [it["id"] for it, _ in found], [n for _, n in found])


async def _tmdb_fill(items: list[dict[str, Any]], films: list[dict[str, Any]]) -> None:
    """Фильма нет на Кинопоиске (свежая зарубежная новинка, корейское кино) — берём его с TMDB: название, год,
    рейтинг TMDB, жанры, актёры, кадры. В подборке он остаётся, а не пропускается."""
    for it, f in zip(items, films):
        if it["id"]:
            continue
        try:
            card = await shorts_daily.tmdb.find_kp_like(
                TMDB_API_KEY, [f.get("ru") or "", f.get("en") or ""], f.get("year"), f.get("series"),
                bool(f.get("fresh")))
        except Exception:  # noqa: BLE001
            logging.exception("Custom: TMDB lookup %s failed", f.get("ru") or f.get("en"))
            continue
        if card:
            it.update(id=card["id"], label=f"{card['name']}" + (f" ({card['year']})" if card.get("year") else "")
                      + " · TMDB")


async def _fill_notes(wait: Message, films: list[dict[str, Any]], items: list[dict[str, Any]]) -> None:
    """У автора нет описания фильма (часто при подборке по фото) — ИИ ищет, о чём он, в интернете и в
    Telegram-каналах, чтобы озвучка была не просто «Жанр.»."""
    empty = [k for k, (f, it) in enumerate(zip(films, items)) if it["id"] and len(f["note"]) < 25]
    if not empty:
        return
    try:
        await wait.edit_text("🌐 Ищу о фильмах в интернете и Telegram-каналах, чтобы было что рассказать…")
    except TelegramBadRequest:
        pass
    names = [films[k]["ru"] or films[k]["en"] for k in empty]
    try:  # каналов много, а запрос на каждое название — не ждём дольше 45 с
        posts = await asyncio.wait_for(shorts_tgsearch.search(shorts_daily.fact_channels, names, per_channel=2), 45)
    except Exception as exc:  # noqa: BLE001
        logging.info("Custom: tg search for notes skipped: %s", exc)
        posts = []
    notes = await shorts_ai.film_notes(GEMINI_API_KEY, [films[k] for k in empty], shorts_tgsearch.as_text(posts, 6000))
    for k, note in zip(empty, notes):
        if note:
            films[k]["note"] = note


async def holiday_short(message: Message, state: FSMContext, key: str) -> None:
    """Шортс к празднику: ИИ выбирает 5 фильмов (Кинопоиск, интернет, подсказка о празднике) — дальше как
    «Своя подборка»."""
    if shorts_lock.locked():
        await message.answer("⏳ Сейчас собирается другой ролик — попробуй через пару минут.")
        return
    wait = await message.answer(f"{HOLIDAYS[key][0]}: ИИ подбирает фильмы к празднику…")
    try:
        title, kp_films, hint = await holiday_pick(key)
        today = datetime.now(MSK).date()
        res = await shorts_ai.write_list_post(GEMINI_API_KEY, title,
                                              HOLIDAY_SEEDS.get(key, []) + [_film_line(f) for f in kp_films], "", [],
                                              f"{today.day} {shorts_daily.MONTHS[today.month - 1]} {today.year}",
                                              n=5, hint=hint)
        ids: list[int] = []

        async def by_title(name: str, year: int | None) -> dict[str, Any] | None:
            try:
                film = await shorts_daily.find_film(kp._get, f"{name} {year}" if year else name)
            except Exception:  # noqa: BLE001
                return None
            return film if film and _same_title(shorts_daily._title(film), name) else None

        for f in (res or {}).get("films") or []:
            _, film = await _list_poster(f, kp_films, [])
            if not film:  # постер нашёлся не на Кинопоиске — фильм всё равно нужен ролику
                film = await by_title(f["ru"] or f["en"], f.get("year"))
            if film and film.get("id") and int(film["id"]) not in ids:
                ids.append(int(film["id"]))
        for seed in HOLIDAY_SEEDS.get(key, []):  # мало — добираем фильмами точно про праздник
            if len(ids) >= 5:
                break
            m = re.match(r"«([^»]+)»\s*\((?:[^)]*?)(\d{4})", seed)
            film = await by_title(m.group(1), int(m.group(2))) if m else None
            if film and film.get("id") and int(film["id"]) not in ids:
                ids.append(int(film["id"]))
        # ИИ не ответил или нашёл мало — добираем кандидатами Кинопоиска (не для «тематических» праздников:
        # там это просто фильмы жанра, не про праздник)
        for f in [] if key in HOLIDAY_SEEDS and len(ids) >= shorts_daily.CUSTOM_MIN else kp_films:
            if len(ids) >= 5:
                break
            if f.get("id") and int(f["id"]) not in ids:
                ids.append(int(f["id"]))
    except Exception as exc:  # noqa: BLE001
        logging.exception("Holiday short pick failed")
        await wait.edit_text(f"❌ Не получилось подобрать фильмы: <code>{escape(_safe_err(exc)[:300])}</code>")
        return
    await wait.delete()
    if len(ids) < shorts_daily.CUSTOM_MIN:
        await message.answer("😔 Мало фильмов для ролика — попробуй ещё раз позже.")
        return
    await start_custom(message, state, re.sub(r"\s*[\U0001F300-\U0001FAFF]\s*$", "", title), ids[:5])


async def start_custom(message: Message, state: FSMContext, title: str, ids: list[int],
                       notes: list[str] | None = None) -> None:
    media = (await state.get_data()).get("cs_media") or []
    if shorts_lock.locked():
        await message.answer("⏳ Сейчас собирается другой ролик — нажми «✅ Собрать шортс» через пару минут.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("✅ Собрать шортс", "cs:go")]]))
        return
    await state.clear()
    status = await message.answer(progress_bar(0, "Начинаю, обычно это 3–6 минут"))

    async def job() -> None:
        try:
            await run_custom(message.bot, message.chat.id, title, ids, status, notes, media)
        except Exception as exc:
            logging.exception("Custom short failed")
            try:
                await status.edit_text("❌ Сборка остановилась — причина ниже")
            except TelegramBadRequest:
                pass
            await message.answer(f"❌ Шортс не собрался:\n<code>{escape(_safe_err(exc)[-700:])}</code>")

    task = asyncio.create_task(job())
    shorts_jobs.add(task)
    task.add_done_callback(shorts_jobs.discard)


@router.message(CustomState.text, F.photo | F.video | F.animation | F.document)
async def custom_media(message: Message, state: FSMContext) -> None:
    """Свои фото и видео для подборки — до текста; ИИ потом ставит каждый файл к нужному фильму."""
    if message.photo:
        file, ext = message.photo[-1], ".jpg"
    elif message.video or message.animation:
        file, ext = message.video or message.animation, ".mp4"
    else:
        doc = message.document
        mime = str(doc.mime_type or "")
        if not mime.startswith(("image/", "video/")):
            await message.answer("Это не фото и не видео — пропускаю.")
            return
        file = doc
        ext = Path(doc.file_name or "").suffix.lower() or (".jpg" if mime.startswith("image/") else ".mp4")
    if (file.file_size or 0) > TG_DOWNLOAD_LIMIT:
        await message.answer("⚠️ Файл больше 20 МБ — бот Telegram не может его скачать. Пришли сжатую версию.")
        return
    async with review_media_lock:  # альбом приходит пачкой сообщений одновременно — не теряем файлы
        media = (await state.get_data()).get("cs_media") or []
        if len(media) >= 10:
            await message.answer("Хватит: уже 10 файлов. Теперь пришли текст подборки.")
            return
        media.append({"file_id": file.file_id, "ext": ext})
        await state.update_data(cs_media=media)
    caption = (message.caption or "").strip()
    if caption and len(media) == 1 and (_free_text(caption) or len(caption.splitlines()) >= 3):
        # текст пришёл подписью к альбому — дождёмся остальных файлов альбома и начнём
        await asyncio.sleep(3)
        await state.set_state(None)
        await custom_text_go(message, state, caption)
        return
    # альбом — это пачка сообщений: отвечаем один раз, когда файлы перестали приходить
    tick = _custom_ack[message.chat.id] = _custom_ack.get(message.chat.id, 0) + 1
    await asyncio.sleep(3)
    if _custom_ack.get(message.chat.id) != tick or await state.get_state() != CustomState.text.state:
        return
    photos = sum(m["ext"] != ".mp4" for m in media)
    if not photos:
        await message.answer(f"📎 Принято файлов: {len(media)}. Теперь вставь текст подборки.")
        return
    await message.answer(
        f"📎 Принято файлов: {len(media)}. Через {PHOTOS_WAIT} секунд ИИ сам узнает фильмы на фото (постеры, "
        "кадры, скриншоты постов) и соберёт ролик. Хочешь добавить фото или свой текст — пришли сейчас.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("🤖 Собрать сразу", "cs:photos")]]))
    await asyncio.sleep(PHOTOS_WAIT)
    if _custom_ack.get(message.chat.id) == tick and await state.get_state() == CustomState.text.state:
        await custom_from_photos(message, state)


_custom_ack: dict[int, int] = {}
PHOTOS_WAIT = 15  # сек: успеть дослать фото или текст, потом ИИ собирает по фото сам


async def custom_from_photos(message: Message, state: FSMContext) -> None:
    """Своя подборка без текста: ИИ узнаёт фильмы на присланных фото; сами фото идут в ролик."""
    await state.set_state(None)  # сразу: кнопка и автозапуск не должны собрать ролик дважды
    images = await _state_photos(message, state)
    if not images:
        await state.set_state(CustomState.text)
        await message.answer("Не вижу фото — пришли картинки или вставь текст подборки.")
        return
    await custom_from_post(message, state, "", images)


@router.message(CustomState.text, F.text, ~F.text.startswith("/"))
async def custom_text(message: Message, state: FSMContext) -> None:
    await custom_text_go(message, state, _typed(message))


async def _web_images(urls: list[str], limit: int = 10) -> list[Image.Image]:
    """Картинки поста по ссылкам (до 15 МБ каждая); битые пропускаем."""
    out: list[Image.Image] = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        for url in urls[:limit]:
            try:
                raw = await netsafe.fetch(session, url, 15 * 1024 * 1024)
                out.append(Image.open(io.BytesIO(raw)).convert("RGB"))
            except Exception as exc:  # noqa: BLE001
                logging.info("Custom: post image %s skipped: %s", url[:80], exc)
    return out


async def _state_photos(message: Message, state: FSMContext) -> list[Image.Image]:
    """Фото, присланные в «Своей подборке» (пересланный пост с постером, альбом)."""
    images: list[Image.Image] = []
    for item in (await state.get_data()).get("cs_media") or []:
        if item["ext"] == ".mp4":
            continue
        try:
            buf = await message.bot.download(item["file_id"], destination=io.BytesIO())
            images.append(Image.open(buf).convert("RGB"))
        except Exception:
            logging.exception("Custom: photo download failed")
    return images


async def custom_text_go(message: Message, state: FSMContext, text: str) -> None:
    link = shorts_linkfetch.TG_POST.search(text)
    if link and len(text.strip()) - len(link.group(0)) < 40 and shorts_ai.available(GEMINI_API_KEY):
        # ссылка на пост Telegram: берём его текст и картинки (список фильмов часто только на постере)
        await state.set_state(None)
        wait = await message.answer("🔗 Открываю пост…")
        try:
            post = await shorts_linkfetch.fetch(link.group(0))
        except Exception as exc:  # noqa: BLE001
            await state.set_state(CustomState.text)
            await wait.edit_text(f"❌ Пост не открылся: <code>{escape(_safe_err(exc)[:300])}</code>\n"
                                 "Перешли сам пост боту или пришли скриншот.")
            return
        urls = [m["image"] for m in post.get("media") or [] if m.get("image")] or (
            [post["image"]] if post.get("image") else [])
        images = await _web_images(urls)
        await wait.delete()
        await custom_from_post(message, state, post.get("text") or "", images or None)
        return
    if _free_text(text) and shorts_ai.available(GEMINI_API_KEY):
        await state.set_state(None)
        await custom_from_post(message, state, text)
        return
    try:
        title, films = shorts_copycat.parse_plan(text)
    except ValueError:
        await message.answer("Не понял список.\n\n" + CUSTOM_HELP)
        return
    await state.set_state(None)
    await show_plan(message, state, title, films)


@router.message(CustomState.video, F.video | F.document | F.text)
async def custom_video(message: Message, state: FSMContext) -> None:
    if (message.text or "").startswith("/"):
        return
    work = SHORTS_DIR / f"copy_{message.chat.id}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    wait = await message.answer("⏳ Скачиваю ролик…")
    try:
        media = message.video or message.document
        if media:
            if media is message.document and not str(media.mime_type or "").startswith("video/"):
                await wait.edit_text("Это не видео. Пришли видеофайл или ссылку.")
                return
            if (media.file_size or 0) > TG_DOWNLOAD_LIMIT:
                await wait.edit_text("⚠️ Файл больше 20 МБ — бот Telegram не может его скачать. "
                                     "Пришли ссылку на Rutube/VK или сжатую версию.")
                return
            src = work / "source.mp4"
            await message.bot.download(media.file_id, destination=src)
        else:
            m = re.search(r"https?://\S+", message.text or "")
            if not m:
                await wait.edit_text("Нужен видеофайл или ссылка (https://…).")
                return
            src = await shorts_dub.fetch_source(m.group(0), work)
        await wait.edit_text("👀 ИИ смотрит ролик и выписывает тему и фильмы, обычно до минуты…")
        found = await shorts_copycat.extract(src, work, GEMINI_API_KEY)
    except Exception as exc:
        logging.exception("Copycat extraction failed")
        await wait.edit_text(f"❌ Не получилось: <code>{escape(_safe_err(exc)[:400])}</code>")
        return
    finally:
        shutil.rmtree(work, ignore_errors=True)
    await wait.delete()
    await state.set_state(None)
    await show_plan(message, state, found["title"] or "Подборка фильмов", found["films"])


@router.callback_query(F.data.startswith("cs:"))
async def custom_buttons(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id not in ADMIN_IDS or not callback.message:
        await callback.answer()
        return
    await callback.answer()
    message, action = callback.message, (callback.data or "")[3:]
    if action in ("text", "video"):
        await custom_ask(message, state, video=action == "video")
        return
    if action == "photos":
        if await state.get_state() != CustomState.text.state:
            await message.answer("Начни заново: «🎯 Своя подборка».")
            return
        await custom_from_photos(message, state)
        return
    if action == "cancel":
        await state.clear()
        await message.answer("Отменено.")
        return
    data = await state.get_data()
    if not data.get("cs_items"):
        await message.answer("Начни заново: «🎯 Своя подборка» или «🔁 По образцу».")
        return
    if action == "edit":
        await state.set_state(CustomState.text)
        lines = [data["cs_title"]] + [it["label"] or it["query"] for it in data["cs_items"]]
        await message.answer("✏️ Скопируй, поправь и пришли обратно (можно убрать, добавить, поменять порядок):\n\n"
                             f"<code>{escape(chr(10).join(lines))}</code>")
    elif action == "go":
        items, notes = data["cs_items"], data.get("cs_notes")
        keep = [k for k, it in enumerate(items) if it["id"]]
        await start_custom(message, state, data["cs_title"], [items[k]["id"] for k in keep],
                           [notes[k] for k in keep] if notes and len(notes) == len(items) else None)


# ---------------------------------------------------------------- посты для Telegram-канала

POST_CHANNEL = os.getenv("POST_CHANNEL", "@KinoMir360")  # свой канал: из него новости не берём, в него публикуем
POSTS_EVERY = 30 * 60
POSTS_NIGHT = range(0, 8)  # часы по времени автора (POST_TZ): ночью авто-посты не присылаем
POST_TZ = timezone(timedelta(hours=int(os.getenv("POST_TZ_HOURS", "5"))))  # «⏰ Позже» — время автора (Екатеринбург)
POST_TZ_NAME = os.getenv("POST_TZ_NAME", "Екб")
# автоплан: (дни недели, 0 — пн; час по времени автора, POST_TZ; что) — бот сам готовит пост и присылает
# автору на публикацию
POSTS_PLAN = [
    (range(7), 9, "bd"),          # каждый день — день рождения звезды
    (range(7), 13, "an"),         # каждый день — «Этот день в кино» (если есть круглая дата)
    (range(7), 19, "qz"),         # каждый день — «Угадай фильм по кадру»
    (range(7), 20, "ev"),         # каждый день — фильм вечера
    ((0,), 16, "vt"),             # понедельник — голосование «Выбор подписчиков»
    ((1,), 17, "bt"),             # вторник — битва фильмов
    ((4,), 16, "vr"),             # пятница — факты о фильме, который выбрали подписчики
    ((5,), 15, "bl"),             # суббота — ляпы в кино
    ((0,), 12, "bo"),             # понедельник — сборы уикенда
    ((2,), 15, "qt"),             # среда — цитата дня
    ((3,), 11, "l:week"),         # четверг — премьеры недели в кино
    ((4,), 11, "l:online"),       # пятница — что посмотреть на выходных
    ((6,), 18, "wk"),             # воскресенье — итоги недели
]
PLAN_TITLES = {"l:week": "Премьеры недели в кино", "l:online": "Что посмотреть на выходных"}
MAX_URL = os.getenv("MAX_URL", "https://max.ru/se14155241_biz")  # канал KinoMir360 в мессенджере MAX
SIGNATURE = (f'\n\n🎬 <a href="https://t.me/{POST_CHANNEL.lstrip("@")}">KinoMir360</a> | '
             f'<a href="{shorts_daily.YOUTUBE_URL}">YouTube</a> | <a href="{MAX_URL}">KinoMir360 в MAX</a>')
SIGN_LEN = len("\n\n🎬 KinoMir360 | YouTube | KinoMir360 в MAX")  # столько подпись занимает в лимите Telegram (1024)
post_lock = asyncio.Lock()


class PostState(StatesGroup):
    media = State()
    narrate = State()
    edit = State()
    link = State()
    actor = State()
    digest = State()
    listlink = State()
    rt = State()


HISTORY_KINDS = ("urls", "topics", "films", "list_titles", "people", "facts", "quiz", "quotes", "anniv", "evening",
                 "battle", "bloopers", "stories")
HISTORY_CAP = {"urls": 500, "topics": 60, "stories": 60, "list_titles": 80, "people": 100, "facts": 150,
               "films": 150}  # остальные — 200


async def _posts_history() -> dict[str, list[Any]]:
    """Что уже было в постах (ссылки, темы, сюжеты, фильмы рубрик) — из таблицы ai_post_history (миграция 005),
    по порядку добавления; сюжеты — словари."""
    out: dict[str, list[Any]] = {k: [] for k in HISTORY_KINDS}
    for r in await db().fetch("SELECT kind, value FROM ai_post_history ORDER BY kind, pos"):
        if r["kind"] in out:
            out[r["kind"]].append(json.loads(r["value"]) if r["kind"] == "stories" else r["value"])
    return out


async def save_posts_history(hist: dict[str, list[Any]]) -> None:
    """Записать историю постов: новые элементы ДОБАВЛЯЮТСЯ (то, что параллельно записала другая задача, не
    затирается), старые сверх лимита вида удаляются. Плюс старый JSON на переходный период (откат версии)."""
    async with db().acquire() as conn, conn.transaction():
        for kind in HISTORY_KINDS:
            values = [shorts_migrations.history_value(v) for v in hist.get(kind) or []]
            if not values:
                continue
            have = {r["value"] for r in await conn.fetch("SELECT value FROM ai_post_history WHERE kind = $1", kind)}
            pos = int(await conn.fetchval("SELECT COALESCE(MAX(pos), 0) FROM ai_post_history WHERE kind = $1", kind))
            for v in dict.fromkeys(values):
                if v not in have:
                    pos += 1
                    await conn.execute("INSERT INTO ai_post_history (kind, pos, value) VALUES ($1, $2, $3) "
                                       "ON CONFLICT DO NOTHING", kind, pos, v)
            await conn.execute(
                "DELETE FROM ai_post_history WHERE kind = $1 AND pos < (SELECT MIN(pos) FROM (SELECT pos FROM "
                "ai_post_history WHERE kind = $1 ORDER BY pos DESC LIMIT $2) t)", kind, HISTORY_CAP.get(kind, 200))
    await set_shorts_setting("posts_history", json.dumps(hist, ensure_ascii=False))  # двойная запись: откат версии


# рубрики — хэштег в каждом посте; закреп-навигатор ведёт по ним (тап по хэштегу ищет посты канала)
RUBRICS = {
    "#новости": "📰 Новости и трейлеры", "#подборка": "🍿 Подборки «что посмотреть»",
    "#фильмвечера": "🎬 Фильм вечера", "#факты": "💡 Факты о фильмах", "#викторина": "🧩 Угадай фильм по кадру",
    "#битвафильмов": "⚔️ Битва фильмов", "#деньрождения": "🎂 Дни рождения звёзд", "#цитата": "💬 Цитата дня",
    "#этотденьвкино": "📅 Этот день в кино", "#ляпы": "🎭 Ляпы в кино", "#сборы": "💰 Сборы уикенда",
    "#итогинедели": "🗞 Итоги недели", "#выборподписчиков": "🗳 Выбор подписчиков",
    "#сактёром": "🎭 Фильмы и сериалы с актёром",
}


BOT_URL = os.getenv("BOT_URL", "https://t.me/VKino_360bot")  # бот в строке «Где смотреть» постов


def post_caption(text: str, rubric: str = "", links: list[tuple[str, str]] | None = None) -> str:
    """Текст поста → подпись в HTML: рубрика-хэштег, «Где смотреть» (ссылки на онлайн-кинотеатры) и подпись
    канала внизу — всё в пределах 1024 символов Telegram."""
    text = shorts_ai.strip_citations(text)  # сноски поиска «([theguardian.com](https://…))» — не для канала
    text = re.sub(r"\s*🎬 KinoMir360 \| YouTube( \| KinoMir360 в MAX)?\s*$", "", text.strip())  # подпись добавим сами
    tail, tail_len = "", SIGN_LEN
    if rubric and rubric.lower() not in text.lower():
        lines = text.rstrip().split("\n")
        if lines and lines[-1].lstrip().startswith("#"):  # дописываем в строку хэштегов, а не отдельной строкой
            lines[-1] = f"{lines[-1].rstrip()} {rubric}"
            text = "\n".join(lines)
        else:
            tail += f"\n{rubric}"
            tail_len += len(rubric) + 1
    if links:
        shown = links[:3] + [("🤖 VKino бот", BOT_URL)]
        tail += "\n\n📺 Где смотреть: " + " · ".join(f'<a href="{escape(url)}">{escape(name)}</a>' for name, url in shown)
        tail_len += len("\n\n📺 Где смотреть: ") + sum(len(name) + 3 for name, _ in shown)
    return escape(_fit_text(text, 1020 - tail_len)) + tail + SIGNATURE


def _fit_text(text: str, limit: int) -> str:
    """Укоротить пост до limit символов, не обрывая хэштеги и слова: строка хэштегов остаётся целой,
    а текст над ней режется по последней целой строке (или слову)."""
    text = text.strip()
    if len(text) <= limit:
        return text
    lines = text.split("\n")
    tags = lines.pop() if lines and lines[-1].lstrip().startswith("#") else ""
    words = tags.split()
    words = words[:5] + words[-1:] if len(words) > 6 else words  # последний — хэштег рубрики, его оставляем
    tags = " ".join(words)[: max(0, limit // 4)]
    room = limit - (len(tags) + 2 if tags else 0)
    body = "\n".join(lines).rstrip()[:room]
    cut = body.rfind("\n")
    body = body[:cut] if cut > room * 0.6 else body[: body.rfind(" ")] + "…" if " " in body else body
    return body.rstrip() + (f"\n\n{tags}" if tags else "")


async def _json_setting(key: str, default: Any) -> Any:
    try:
        return json.loads(await shorts_setting(key) or "null") or default
    except ValueError:
        return default


POLL_QUESTION = "Что посмотрите первым? 🍿"


VOTE_QUESTION = "О каком фильме рассказать факты в пятницу? 🗳"


async def save_post(chat_id: int, ids: list[int], kind: str, poll: list[str] | None = None,
                    question: str = POLL_QUESTION, quiz: dict[str, Any] | None = None,
                    extra: dict[str, Any] | None = None) -> None:
    """Запомнить готовый пост в чате бота: сообщения (альбом), где текст (caption | text), опрос, викторина;
    extra — {"button": {"text", "url"}} или {"vote": {"ids", "titles"}}. Таблица ai_drafts (миграция 003)."""
    meta = {"ids": ids, "kind": kind, "poll": poll or [], "question": question, "quiz": quiz, **(extra or {})}
    await db().execute(
        "INSERT INTO ai_drafts (chat_id, first_id, meta) VALUES ($1, $2, $3::jsonb) "
        "ON CONFLICT (chat_id, first_id) DO UPDATE SET meta = EXCLUDED.meta, updated_at = NOW()",
        chat_id, ids[0], json.dumps(meta, ensure_ascii=False))
    await audit(chat_id, ids[0], None, "draft", {"kind": kind})
    # двойная запись в старый JSON на переходный период: откат на прошлую версию бота не потеряет черновики
    old = await _json_setting("posts_meta", {})
    old[f"{chat_id}:{ids[0]}"] = meta
    await set_shorts_setting("posts_meta", json.dumps(dict(list(old.items())[-80:]), ensure_ascii=False))


async def audit(chat_id: int, first: int, actor: int | None, action: str, details: dict[str, Any] | None = None) -> None:
    """Журнал действий с постом (ai_audit_log); ошибка журнала не мешает работе."""
    try:
        await db().execute("INSERT INTO ai_audit_log (chat_id, first_id, actor, action, details) "
                           "VALUES ($1, $2, $3, $4, $5::jsonb)", chat_id, first, actor, action,
                           json.dumps(details or {}, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        logging.exception("Audit log write failed")


async def _post_meta(chat_id: int, first: int) -> dict[str, Any]:
    row = await db().fetchrow("SELECT meta, published_at FROM ai_drafts WHERE chat_id = $1 AND first_id = $2",
                              chat_id, first)
    if row:
        meta = json.loads(row["meta"]) if isinstance(row["meta"], str) else dict(row["meta"])
        if row["published_at"]:
            meta["published"] = int(row["published_at"].timestamp())
        return meta
    return (await _json_setting("posts_meta", {})).get(f"{chat_id}:{first}") or {"ids": [first], "kind": "caption",
                                                                                   "poll": []}


def post_keyboard(first: int, more: str = "", yt: bool = False) -> InlineKeyboardMarkup:
    rows = [[_btn(f"📢 Опубликовать в {POST_CHANNEL}", f"pt:p:{first}")],
            [_btn("⏰ Позже", f"pt:t:{first}"), _btn("✏️ Поправить текст", f"pt:e:{first}")],
            [_btn("🖼 Заменить или добавить фото/видео", f"pt:m:{first}")]]
    if yt:  # шортс: YouTube — первой кнопкой, а публикация в канал подписана явно, чтобы не перепутать
        rows[0] = [_btn(f"✈️ Только в Telegram-канал {POST_CHANNEL}", f"pt:p:{first}")]
        rows.insert(0, [_btn("▶️ Загрузить на YouTube", f"yt:{first}")])
    if more:
        rows.append([_btn("🔄 Ещё такой же", more), _btn("📝 Другие посты", "pt:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def offer_post(bot: Bot, chat_id: int, ids: list[int], kind: str, more: str = "",
                     poll: list[str] | None = None, what: str = "Пост готов", question: str = POLL_QUESTION,
                     quiz: dict[str, Any] | None = None, extra: dict[str, Any] | None = None,
                     note: str = "") -> None:
    """note — строки для автора под сообщением (например, источники новости)."""
    await save_post(chat_id, ids, kind, poll, question, quiz, extra)
    await bot.send_message(chat_id, f"👆 {what}"
                           + (f". После публикации в канале добавлю опрос «{escape(question)}»." if poll else ".")
                           + note, reply_markup=post_keyboard(ids[0], more, bool((extra or {}).get("yt"))),
                           disable_web_page_preview=True)


async def _queue_json_remove(pairs: set[tuple[int, int]]) -> None:
    """Двойная запись очереди в старый JSON (переходный период)."""
    queue = await _json_setting("posts_queue", [])
    rest = [q for q in queue if (q["chat"], q["first"]) not in pairs]
    if len(rest) != len(queue):
        await set_shorts_setting("posts_queue", json.dumps(rest))


async def queue_add(chat_id: int, first: int, at: int) -> None:
    """Поставить пост в отложенную публикацию (ai_queue, миграция 004); тот же пост — новое время."""
    await db().execute("INSERT INTO ai_queue (chat_id, first_id, at) VALUES ($1, $2, to_timestamp($3)) "
                       "ON CONFLICT (chat_id, first_id) DO UPDATE SET at = EXCLUDED.at", chat_id, first, at)
    queue = [q for q in await _json_setting("posts_queue", []) if not (q["chat"] == chat_id and q["first"] == first)]
    await set_shorts_setting("posts_queue", json.dumps(queue + [{"chat": chat_id, "first": first, "at": at}]))


async def _unqueue(chat_id: int, first: int) -> bool:
    """Снять пост с отложенной публикации; True — он там был."""
    done = await db().execute("DELETE FROM ai_queue WHERE chat_id = $1 AND first_id = $2", chat_id, first)
    await _queue_json_remove({(chat_id, first)})
    return done.endswith(" 1")


class AlreadyPublishing(RuntimeError):
    pass


PUBLISH_CLAIM = timedelta(minutes=5)  # «публикуется» дольше — публикация оборвалась (перезапуск), можно снова


async def _claim_publish(chat_id: int, first: int) -> bool:
    """Атомарно занять пост для публикации: True — можно публиковать. Второе нажатие, кнопка + расписание или
    повтор после перезапуска получат False, пока идёт первая публикация."""
    meta = await _post_meta(chat_id, first)
    meta.pop("published", None)
    await db().execute(  # черновик из старого JSON — заводим строку, чтобы было что занимать
        "INSERT INTO ai_drafts (chat_id, first_id, meta) VALUES ($1, $2, $3::jsonb) ON CONFLICT DO NOTHING",
        chat_id, first, json.dumps(meta, ensure_ascii=False))
    got = await db().fetchval(
        "UPDATE ai_drafts SET status = 'publishing', claimed_at = NOW(), updated_at = NOW() "
        "WHERE chat_id = $1 AND first_id = $2 AND (status <> 'publishing' OR claimed_at < NOW() - $3::interval) "
        "RETURNING 1", chat_id, first, PUBLISH_CLAIM)
    return bool(got)


async def publish_post(bot: Bot, chat_id: int, first: int, actor: int | None = None) -> None:
    """Публикация в канал ровно один раз: занимаем пост в базе (status='publishing'), публикуем, отмечаем
    'published' (или 'failed'). Повторно тот же пост — только после подтверждения автора («📢 Да, ещё раз»)."""
    if not await _claim_publish(chat_id, first):
        raise AlreadyPublishing("этот пост уже публикуется")
    try:
        await _publish_post(bot, chat_id, first)
    except Exception as exc:
        await db().execute("UPDATE ai_drafts SET status = 'failed', updated_at = NOW() "
                           "WHERE chat_id = $1 AND first_id = $2", chat_id, first)
        await audit(chat_id, first, actor, "publish_failed", {"error": _safe_err(exc)[:300]})
        raise
    await db().execute("UPDATE ai_drafts SET status = 'published', published_at = NOW(), published_by = $3, "
                       "updated_at = NOW() WHERE chat_id = $1 AND first_id = $2", chat_id, first, actor)
    shorts_usage.record("tg_posts")  # для недельного отчёта
    await audit(chat_id, first, actor, "published", {"channel": POST_CHANNEL, "scheduled": actor is None})
    old = await _json_setting("posts_meta", {})  # двойная запись: отметка в старом JSON
    if f"{chat_id}:{first}" in old:
        old[f"{chat_id}:{first}"]["published"] = int(time.time())
        await set_shorts_setting("posts_meta", json.dumps(old, ensure_ascii=False))


async def _publish_post(bot: Bot, chat_id: int, first: int) -> None:
    """Копия поста (альбом целиком) в канал автора; у подборки — опрос «Что посмотрите первым?».
    Пост снимается с отложенной публикации (вышел сейчас — по расписанию повторно не выйдет) и помечается
    опубликованным (повторное нажатие спросит подтверждение)."""
    await _unqueue(chat_id, first)
    meta = await _post_meta(chat_id, first)
    button = meta.get("button")
    if button and len(meta["ids"]) == 1:  # пост со ссылкой-кнопкой (бот, буст): копируем вместе с кнопкой
        await bot.copy_message(POST_CHANNEL, chat_id, meta["ids"][0], reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=button["text"], url=button["url"])]]))
    else:
        await bot.copy_messages(POST_CHANNEL, chat_id, meta["ids"])
    vote = meta.get("vote")
    if vote:  # «Выбор подписчиков»: опрос запоминаем — в пятницу бот подведёт итог и сделает пост о победителе
        sent = await bot.send_poll(POST_CHANNEL, VOTE_QUESTION, vote["titles"], is_anonymous=True)
        await set_shorts_setting("vote_poll", json.dumps({"message_id": sent.message_id, "ids": vote["ids"],
                                                           "titles": vote["titles"]}, ensure_ascii=False))
        return
    quiz = meta.get("quiz")
    if quiz:  # «Угадай фильм»: викторина Telegram — правильный ответ виден сразу после голоса
        await bot.send_poll(POST_CHANNEL, "Что это за фильм? 🎬", quiz["options"], type="quiz",
                            correct_option_id=quiz["correct"], is_anonymous=True, explanation=quiz["explanation"])
        return
    options = list(dict.fromkeys(t[:100] for t in meta["poll"] if t))[:10]
    if len(options) >= 2:
        try:
            await bot.send_poll(POST_CHANNEL, meta.get("question") or POLL_QUESTION, options, is_anonymous=True)
        except Exception:  # noqa: BLE001 — пост уже вышел, опрос не главное
            logging.exception("Poll failed")


PUBLISH_HELP = ("Добавь бота в администраторы канала с правом «Публикация сообщений» "
                "(канал → Изменить → Администраторы → Добавить администратора → найди бота).")


def _season_of(text: str) -> int | None:
    """Номер сезона из текста новости: «3-го сезона», «третий сезон», «сезон 2», «season 4». Если сезонов
    несколько («после финала 2 сезона „Игры“ вышел трейлер 3-го сезона…»), берём ближайший к слову «трейлер»;
    «продлили на 2 сезона», «после 5 сезонов» — число сезонов, а не номер."""
    words = {"втор": 2, "трет": 3, "четв": 4, "пят": 5, "шест": 6, "седьм": 7, "восьм": 8, "девят": 9, "десят": 10}
    t = str(text or "").lower()
    found: list[tuple[int, int]] = []  # (позиция, номер)
    for m in re.finditer(r"(\d{1,2})\s*-?\s*(го|й|ий|ого)?\s+сезон(\w*)|сезон\w*\s+(\d{1,2})\b|"
                         r"season\s+(\d{1,2})", t):
        if m.group(1):
            before = t[max(0, m.start() - 12):m.start()]
            if m.group(3) == "ов" or (not m.group(2) and re.search(
                    r"\b(на|после|из|все|всех|уже|ещё|еще|спустя|за)\s*$", before)):
                continue
        found.append((m.start(), int(next(g for g in (m.group(1), m.group(4), m.group(5)) if g))))
    for m in re.finditer(r"\b(втор|трет|четв|пят|шест|седьм|восьм|девят|десят)\w*\s+сезон", t):
        found.append((m.start(), words[m.group(1)]))
    if not found:
        return None
    trailer = [m.start() for m in re.finditer(r"трейлер|тизер|trailer|teaser", t)]
    if trailer:
        return min(found, key=lambda f: min(abs(f[0] - p) for p in trailer))[1]
    return min(found)[1]


async def _media_frame(path: Path, work: Path) -> Image.Image | None:
    """Картинка для проверки: само фото или кадр видео на 1-й секунде."""
    try:
        if path.suffix.lower() in (".mp4", ".mov", ".webm", ".mkv"):
            frame = work / f"{path.stem}_check.jpg"
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-v", "error", "-y", "-ss", "1", "-i", str(path), "-frames:v", "1", str(frame),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(proc.wait(), 60)
            path = frame
        return await asyncio.to_thread(lambda: Image.open(path).convert("RGB"))
    except Exception:  # noqa: BLE001
        return None


async def _check_media(files: list[Path], res: dict[str, Any], work: Path,
                       notes: list[str] | None = None) -> list[Path]:
    """Фото и видео поста — только о той же новости: ИИ смотрит на каждое (до 4). Не подошло ничего — постер
    фильма или портрет человека из базы (TMDB), иначе пост без картинки (её можно добавить кнопкой)."""
    if not files:
        return files
    news = f"{res['post'][:900]}\nФильм/сериал: {res.get('film') or '—'} ({res.get('film_en') or '—'}). " \
           f"Человек: {res.get('person') or '—'} ({res.get('person_en') or '—'})."
    keep: list[Path] = []
    unsure: list[Path] = []  # ИИ не ответил (лимит, сбой) — такой картинке не верим, если есть замена из базы
    for k, path in enumerate(files):
        if k >= 2:  # остальные кадры альбома — из того же поста; лимит Gemini на день не тратим
            (keep if keep else unsure).append(path)
            continue
        img = await _media_frame(path, work)
        fits, seen = await shorts_ai.media_fits(GEMINI_API_KEY, img, news) if img else (None, "")
        if notes is not None:  # автору под постом: что ИИ увидел на картинке и решил
            notes.append(f"{'✅' if fits else '❌ убрал' if fits is False else '❔ не проверено'}"
                         + (f": {seen}" if seen else ""))
        if fits is False:
            logging.info("Tg post: media %s rejected for %s", path.name, res.get("topic"))
            continue
        (keep if fits else unsure).append(path)
    if keep:
        return keep
    # непроверенная картинка часто из сборного поста канала (к новости о «I Play Rocky» пришёл постер
    # «Мстителей» соседней новости) — надёжнее постер или портрет из базы по названию
    urls = await _tmdb_media_urls(res)
    if not urls and unsure:
        if notes is not None:
            notes.append("⚠️ ИИ не проверил картинку, замены в базе нет — оставил как есть, проверь глазами")
        return unsure
    if notes is not None:
        notes.append({2: "поставил портрет человека и постер фильма из базы TMDB",
                      1: "поставил портрет или постер из базы TMDB"}.get(len(urls), "подходящей картинки нет — пост "
                     "без фото, добавь кнопкой «🖼 Заменить или добавить фото/видео»"))
    if not urls:
        return []
    return await shorts_tgsearch.download_media([{"image": u} for u in urls], work, limit=2, prefix="repl")


async def _illustration(res: dict[str, Any], work: Path, checks: list[str]) -> list[Path]:
    """Подходящего реального фото нет — оригинальная иллюстрация от ИИ (не фото, не постер, без лиц реальных
    людей) с пометкой «Иллюстрация · ИИ» на самой картинке. Только в пределах месячного лимита; выключается
    в «🖼 Фото к новостям»."""
    if await shorts_setting("illustrations") == "off":
        return []
    try:
        # платная картинка — через общий бюджет: резерв до запроса, не влезает — не рисуем
        raw = await shorts_ai.generate_illustration(GEMINI_API_KEY, res.get("topic") or res["post"][:300],
                                                    res.get("film_en") or res.get("film") or "")
    except shorts_budget.BudgetExceeded as exc:
        checks.append(f"🎨 картинку ИИ не рисую: {exc}")
        return []
    except Exception as exc:  # noqa: BLE001
        logging.info("Tg post: illustration failed: %s", exc)
        raw = None
    if not raw:
        checks.append("🎨 нарисовать иллюстрацию не вышло (у ключа Gemini нет модели картинок — она только на "
                      "платном тарифе Google) — пост без фото, добавь кнопкой")
        return []

    def mark() -> Path:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        d = ImageDraw.Draw(img)
        size = max(18, img.width // 40)
        try:
            font = ImageFont.truetype(str(shorts_daily.ASSETS / "Montserrat.ttf"), size)
        except OSError:
            font = ImageFont.load_default()
        text = "Иллюстрация · ИИ"
        w = d.textlength(text, font=font)
        pad = size // 2
        box = (pad, img.height - size * 2 - pad, pad + w + size, img.height - pad)
        d.rounded_rectangle(box, radius=size // 2, fill=(0, 0, 0))
        d.text((box[0] + size // 2, box[1] + size // 2), text, font=font, fill=(255, 255, 255))
        out = work / "illustration.jpg"
        img.save(out, quality=92)
        return out

    path = await asyncio.to_thread(mark)
    checks.append(f"🎨 реального фото нет — ИИ нарисовал иллюстрацию (≈{shorts_budget.IMAGE_USD:.3f}$, "
                  "помечена на картинке). Замени, если найдёшь фото")
    return [path]


async def _tmdb_media_urls(res: dict[str, Any]) -> list[str]:
    """Картинки из базы фильмов по названию — точно «те»: портрет человека из новости (смерть, интервью,
    роль) и рядом постер фильма («кто озвучит Дума» — актёр и «Мстители»)."""
    urls: list[str] = []
    try:
        if res.get("person_en") or res.get("person"):
            info = await shorts_daily.tmdb.person_by_name(TMDB_API_KEY, res.get("person_en") or res["person"])
            urls += (shorts_daily.tmdb.profile_urls(info) or [])[:1] if info else []
    except Exception as exc:  # noqa: BLE001
        logging.info("Tg post: tmdb portrait failed: %s", exc)
    try:
        if res.get("film_en") or res.get("film"):
            poster = await shorts_daily.tmdb.poster_by_title(
                TMDB_API_KEY, [n for n in (res.get("film_en"), res.get("film")) if n])
            urls += [poster] if poster else []
    except Exception as exc:  # noqa: BLE001
        logging.info("Tg post: tmdb poster failed: %s", exc)
    return urls


async def _rutube_post_trailer(titles: list[str], work: Path, season: int | None = None,
                               fresh: bool = False) -> Path | None:
    """Трейлер фильма со звуком с Rutube для поста (до 3 минут, до 50 МБ); None — не нашёлся или не скачался.
    season — трейлер именно этого сезона; fresh — новость о только что вышедшем трейлере (ролик не старше
    двух недель): иначе к «трейлеру 3-го сезона» попадал старый трейлер первого."""
    try:
        found = await asyncio.wait_for(shorts_clips.rutube_trailer(
            [t for t in titles if t], season=season, fresh_days=14 if fresh else None), 20)
        if not found:
            return None
        return await asyncio.wait_for(shorts_clips.download_trailer(found["url"], work / "trailer.mp4"), 300)
    except Exception as exc:  # noqa: BLE001
        logging.info("Tg post: rutube trailer failed: %s", exc)
        return None


DEFAULT_TITLES = {"Clayface": "Глиноликий"}  # свои названия: важнее базы фильмов (/title)


async def _my_titles() -> dict[str, str]:
    titles = dict(DEFAULT_TITLES)
    titles.update(await _json_setting("my_titles", {}))
    return titles


async def _official_title(name_en: str) -> str:
    """Русское название по оригинальному: свой словарь автора (/title), TMDB (фильм или сериал), иначе
    Кинопоиск. Пусто — не нашли."""
    if not name_en or not re.search(r"[A-Za-z]", name_en):
        return ""
    mine = {k.lower(): v for k, v in (await _my_titles()).items()}
    if name_en.strip().lower() in mine:
        return mine[name_en.strip().lower()]
    try:
        films, shows = await asyncio.gather(shorts_daily.tmdb.search_movie(TMDB_API_KEY, name_en),
                                            shorts_daily.tmdb.search_tv(TMDB_API_KEY, name_en), return_exceptions=True)
        for cand in (films if isinstance(films, list) else [])[:5] + (shows if isinstance(shows, list) else [])[:5]:
            if shorts_daily.tmdb.similar(cand.get("original") or "", name_en) >= 0.93 \
                    and re.search(r"[А-Яа-яЁё]", cand.get("title") or ""):
                return str(cand["title"])
    except Exception as exc:  # noqa: BLE001
        logging.info("Official title %s via TMDB failed: %s", name_en, exc)
    try:
        film = await asyncio.wait_for(shorts_daily.find_film(kp._get, name_en), 20)
        if film and shorts_daily.tmdb.similar(str(film.get("enName") or film.get("alternativeName") or ""),
                                              name_en) >= 0.93 and re.search(r"[А-Яа-яЁё]", str(film.get("name") or "")):
            return str(film["name"])
    except Exception as exc:  # noqa: BLE001
        logging.info("Official title %s via Kinopoisk failed: %s", name_en, exc)
    return ""


async def _fix_titles(res: dict[str, Any]) -> dict[str, Any]:
    """ИИ мог перевести название сам («Clayface» → «Клейкое лицо»): находим прокатное и меняем в тексте поста
    (и в «ёлочках», и в заголовке КАПСОМ)."""
    official = await _official_title(res.get("film_en") or "")
    wrong = (res.get("film") or "").strip()
    if not official or not wrong or official.lower() == wrong.lower():
        return res
    post = res["post"]
    for old, new in ((wrong, official), (wrong.upper(), official.upper())):
        post = post.replace(f"«{old}»", f"«{new}»").replace(old, new)
    if post != res["post"]:
        logging.info("Tg post: title fixed %r -> %r", wrong, official)
    return {**res, "post": post, "film": official}


def _sources_note(posts: list[dict[str, Any]]) -> str:
    """Строки «откуда новость» под постом (видит только автор в боте, в канал не уходят)."""
    rows = []
    for p in posts[:4]:
        name = p.get("label") or f"t.me/{p.get('channel', '')}"
        rows.append(f'• <a href="{escape(p["url"], quote=True)}">{escape(name)}</a>')
    return ("\n\n📎 Источник (только для тебя):\n" + "\n".join(rows)) if rows else ""


_STOP_STEMS = {"фильм", "сериа", "котор", "также", "перво", "будет", "сезон", "студи", "режис", "трейл", "премь",
               "прока", "релиз", "покаж", "снима", "получ", "стане", "сообщ", "расск", "объяв", "верну", "собра",
               "новог", "новый", "новая", "новые", "тольк", "после", "сейча", "больш", "долла", "милли",
               "зрите", "истор", "росси", "недел", "сегод", "главн", "вышел", "выйде", "выход", "канал", "подпи",
               "перес", "друго", "другу", "этого", "своей", "своих", "всего", "можно", "очень", "когда", "через"}
_STOP_EN = {"this", "that", "with", "from", "have", "will", "their", "they", "film", "movie", "series", "first",
            "after", "about", "into", "more", "season", "trailer", "official", "release", "today", "news", "just",
            "been", "were", "what", "when", "which", "while", "would", "could", "also", "than", "then", "over"}


def _key_tokens(text: str) -> set[str]:
    """Опознавательные слова текста для сверки «о том же ли новость»: латинские слова, числа (кроме годов) и
    основы длинных русских слов без общих («фильм», «сезон», «премьера»…)."""
    t = str(text or "").lower().replace("ё", "е")
    latin = {w for w in re.findall(r"[a-z][a-z0-9]{3,}", t)} - _STOP_EN
    nums = {n for n in re.findall(r"\d{2,}", t) if not re.fullmatch(r"(19|20)\d\d", n)}
    # основа русского слова от 4 букв: без окончания («дюна/дюны» → «дюн», «ривз/ривза» → «рив»), до 6 букв
    cyr = {w[:min(6, max(3, len(w) - 2))] for w in re.findall(r"[а-я]{4,}", t)}
    cyr = {c for c in cyr if c[:5] not in _STOP_STEMS and c not in _STOP_STEMS}
    return latin | nums | cyr


def _proper_names(text: str) -> str:
    """Слова с заглавной буквы не в начале предложения (имена, названия) и всё в «ёлочках»."""
    out = re.findall(r"«([^»]{2,60})»", text)
    for m in re.finditer(r"(?<![.!?…\n]\s)(?<!^)(?<=\s)([A-ZА-ЯЁ][a-zа-яё]{2,}(?:[\s-][A-ZА-ЯЁ][a-zа-яё]{2,})*)", text):
        out.append(m.group(1))
    return " ".join(out)


def _shared(about: set[str], words: set[str]) -> set[str]:
    """Общие опознавательные слова; русские основы совпадают и по началу («мстите» ~ «мстител»)."""
    out = about & words
    cyr = [a for a in about if a.isalpha() and not a.isascii()]
    for w in words - out:
        if w.isalpha() and not w.isascii() and any(w.startswith(a) or a.startswith(w) for a in cyr):
            out.add(w)
    return out


STORY_HOURS = 72  # о том же событии за это время второй пост не делаем


def _story_tokens(res: dict[str, Any]) -> list[str]:
    return sorted(_key_tokens(" ".join(str(res.get(k) or "") for k in
                                       ("post", "film", "film_en", "person", "person_en"))))


def _repeats(words: set[str], stories: list[dict[str, Any]], english: bool = False) -> bool:
    """Это та же новость, что уже была за последние STORY_HOURS часов? (Джонни Депп и «Пираты» из четырёх
    каналов — одна история.) Русский текст: много общих опознавательных слов; английский: общие латинские
    слова (имена и названия — в посте они записаны и латиницей)."""
    now = time.time()
    for st in stories:
        if now - st.get("at", 0) > STORY_HOURS * 3600:
            continue
        old = set(st.get("tokens") or [])
        if not old or not words:
            continue
        if english:
            if len({w for w in words & old if w.isascii() and not w.isdigit()}) >= 3:
                return True
            continue
        hits = _shared(old, words)
        if len(hits) >= 4 and len(hits) / min(len(old), len(words)) >= 0.35:
            return True
    return False


async def make_tg_post(bot: Bot, chat_ids: list[int], mode: str = "tg",
                       pool: list[dict[str, Any]] | None = None) -> str:
    """Пост в стиле канала автора по самой интересной свежей новости, с фото или видео из источника.
    mode: "tg" — Telegram-каналы за сутки (кроме своего); "fast" — быстрые зарубежные источники (Reddit,
    Deadline, инсайдеры, X) за 12 часов — там новости раньше; "all" — и то и другое.
    Присылает в чаты; возвращает тему или "" (новых новостей нет)."""
    async with post_lock:
        own = POST_CHANNEL.lstrip("@").lower()
        channels = [c for c in await fact_channels() if c.lower() != own]
        if pool is not None:  # инфоповод: пост только по этим постам-источникам
            posts = list(pool)
        else:
            posts = await shorts_tgsearch.latest(channels, hours=24, per_channel=5) if mode != "fast" else []
        hist = await _posts_history()
        used = set(hist["urls"])
        fresh = [p for p in posts if p["url"] not in used][:30 if mode == "tg" else 20]
        if mode != "tg" and pool is None:
            try:
                fast = await shorts_fast.latest(await fast_feeds(), timeout=50)
            except Exception as exc:  # noqa: BLE001
                logging.info("Tg post: fast sources failed: %s", exc)
                fast = []
            fresh += [p for p in fast if p["url"] not in used][:30 if mode == "fast" else 15]
        if not fresh:
            return ""
        try:  # что уже вышло в своём канале — ИИ эти новости не повторит
            mine = await shorts_tgsearch.latest([POST_CHANNEL.lstrip("@")], hours=96, per_channel=25)
        except Exception as exc:  # noqa: BLE001
            logging.info("Tg post: own channel read failed: %s", exc)
            mine = []
        own_text = "\n".join("— " + re.sub(r"\s+", " ", p["text"])[:200] for p in mine)
        stories = [st for st in hist["stories"] if time.time() - st.get("at", 0) < STORY_HOURS * 3600]
        # посты о событии, про которое уже был пост (за 3 дня), — сразу убираем: другой канал, та же новость
        fresh = [p for p in fresh if not _repeats(_key_tokens(p["text"]), stories, p.get("lang") == "en")]
        if not fresh:
            return ""
        res = None
        for _ in range(3):  # ИИ всё же выбрал повтор — отбрасываем эти посты и просим другую новость
            res = await shorts_ai.write_post(GEMINI_API_KEY, fresh, hist["topics"], own_text)
            if not res:
                raise RuntimeError(shorts_ai.last_error or "ИИ не написал пост")
            if not _repeats(set(_story_tokens(res)), stories):
                break
            logging.info("Tg post: repeat of a recent story skipped: %s", res["topic"])
            dup = {fresh[i]["url"] for i in res["media"]}
            hist["urls"] = (hist["urls"] + list(dup))[-500:]
            fresh = [p for p in fresh if p["url"] not in dup and not _repeats(
                _key_tokens(p["text"]), stories + [{"at": time.time(), "tokens": _story_tokens(res)}],
                p.get("lang") == "en")]
            res = None
            if not fresh:
                break
        if not res:
            await save_posts_history(hist)
            return ""
        res = await _fix_titles(res)  # «Клейкое лицо» → «Глиноликий»: прокатное название из базы фильмов
        same = [fresh[i] for i in res["media"]]
        hist["stories"] = (stories + [{"at": int(time.time()), "tokens": _story_tokens(res)}])[-60:]
        sources = list(same)  # для автора под постом: откуда взята новость
        hist["urls"] = (hist["urls"] + [p["url"] for p in same])[-500:]
        hist["topics"] = (hist["topics"] + [res["topic"] or res["post"][:60]])[-60:]
        await save_posts_history(hist)
        # фото и видео — только из постов, где упомянут сам фильм: ИИ иногда даёт номер соседнего поста
        # (в посте о «Leviathan» оказался баннер Game Pass из той же ленты X)
        names = [_title_words(n) for n in (res["film"], res["film_en"]) if n and len(_title_words(n)) >= 3]
        if names:
            same = [p for p in same if any(_mentions_title(n, _title_words(p["text"])) for n in names)]
        else:  # фильма нет — только главный пост новости, без соседних
            same = same[:1]
        # и только если в источнике есть те же имена, названия или цифры, что в готовом посте: ИИ бывает
        # называет номер чужого поста (в посте о Warner Bros. оказалось видео про Zelda)
        about = _key_tokens(" ".join([res["post"], res["film"], res["film_en"], res["person"]]))
        # имена и названия новости: общие слова («экранизация», «хоррор», «роман») совпадают у разных новостей —
        # без общего имени фото чужого поста не берём (к посту про Андерсона пришёл портрет Деппа)
        names_ru = _key_tokens(" ".join([res["film"], res["person"], _proper_names(res["post"])]))
        names_en = _key_tokens(" ".join([res["film_en"], res["person_en"]]))
        about_en = names_en | {t for t in about if t.isdigit() or t.isascii()}

        def related(p: dict[str, Any]) -> bool:
            words = _key_tokens(p["text"])
            if p.get("lang") == "en":  # пост уже по-русски, имена в нём — кириллицей: сверяем с латиницей и числами
                hits = about_en & words
                # одного общего слова мало («marvel», «star») — нужно название/имя из новости или два совпадения
                return bool(hits & names_en) or len(hits) >= 2
            hits = _shared(about, words)
            return len(hits) >= 2 and bool(_shared(names_ru | names_en, words))

        same = [p for p in same if related(p)]
        # фото и видео сборных постов (дайджестов) не берём: их картинка обычно от другой новости
        # и только из постов целиком об этой новости: название в начале, других фильмов не больше одного
        digests = [p for p in same if not _focused(p.get("text") or "", names)]
        if digests:
            logging.info("Tg post: %d digest/off-topic source(s) skipped for media", len(digests))
            same = [p for p in same if p not in digests]
        work = SHORTS_DIR / f"post_{time.time_ns()}"
        work.mkdir(parents=True, exist_ok=True)
        try:
            # 0) альбом поста-источника целиком (несколько фото/видео) — все медиа, а не одно
            album = next((p["media"] for p in same if len(p.get("media") or []) >= 2), None)
            # 1) видео из постов об этой же новости
            with_media = [p for p in same if p.get("video")]
            # 2) новость о трейлере — сам трейлер со звуком с Rutube
            if not with_media and res["trailer"] and res["film"]:
                trailer = await _rutube_post_trailer([res["film"], res["film_en"]], work,
                                                     _season_of(res["post"]), fresh=True)
                with_media = [{"file": trailer}] if trailer else []
            # 3) фото из постов об этой новости
            with_media = with_media or [p for p in same if p.get("image")]
            if not with_media and res["film"]:  # 4) посты каналов о том же фильме (о трейлере — со словом «трейлер»)
                try:
                    found = await asyncio.wait_for(shorts_tgsearch.search(
                        channels, [n for n in (res["film"], res["film_en"]) if n], per_channel=2), 40)
                    if res["trailer"]:
                        found = [p for p in found if re.search(r"трейлер|тизер|trailer|teaser", p["text"], re.I)]
                    # пост именно об этой новости: фильм назван в начале поста и есть общее имя с новостью —
                    # иначе брали видео из подборки, где фильм лишь упомянут (к посту об «Обсессии» Карри
                    # Баркера пришёл ролик «From director Zack Snyder»)
                    found = [p for p in found if related(p) and _focused(p["text"], names)] if names else []
                    with_media = [p for p in found if p.get("video") or p.get("image")]
                except Exception as exc:  # noqa: BLE001
                    logging.info("Tg post: media search skipped: %s", exc)
            if not with_media and res["film"]:  # кадр фильма с Кинопоиска
                try:
                    film = await shorts_daily.find_film(kp._get, res["film"])
                    url = shorts_daily._image_url(film) if film else None
                    with_media = [{"image": url}] if url else []
                except Exception as exc:  # noqa: BLE001
                    logging.info("Tg post: kinopoisk image skipped: %s", exc)
            if not with_media and (res["film_en"] or res["film"]):  # постер с TMDB (новинок нет на Кинопоиске)
                try:
                    url = await shorts_daily.tmdb.poster_by_title(
                        TMDB_API_KEY, [n for n in (res["film_en"], res["film"]) if n])
                    with_media = [{"image": url}] if url else []
                except Exception as exc:  # noqa: BLE001
                    logging.info("Tg post: tmdb poster skipped: %s", exc)
            if not with_media and (res["person_en"] or res["person"]):  # смерть, юбилей — портрет человека с TMDB
                try:
                    info = await shorts_daily.tmdb.person_by_name(TMDB_API_KEY, res["person_en"] or res["person"])
                    url = (shorts_daily.tmdb.profile_urls(info) or [None])[0] if info else None
                    with_media = [{"image": url}] if url else []
                except Exception as exc:  # noqa: BLE001
                    logging.info("Tg post: tmdb person skipped: %s", exc)
            # последнее — маленькая обложка видео из самой новости
            with_media = with_media or [{"image": p["thumb"]} for p in same if p.get("thumb")]
            files = await shorts_tgsearch.download_media(album, work, limit=10, prefix="alb") if album else []
            if len(files) < 2:
                files = ([with_media[0]["file"]] if with_media and with_media[0].get("file") else
                         await shorts_tgsearch.download_media(with_media[:3], work, limit=1, prefix="post"))
            checks: list[str] = []
            db_files: list[Path] = []
            # режим «фото из базы фильмов»: постер/портрет TMDB вместо фото из постов (кроме новостей о трейлере
            # с самим трейлером); в базе ничего — как обычно, фото из новости с проверкой ИИ
            if await shorts_setting("post_media") == "db" and not (res["trailer"] and files
                                                                   and files[0].suffix == ".mp4"):
                db_urls = await _tmdb_media_urls(res)
                db_files = await shorts_tgsearch.download_media(
                    [{"image": u} for u in db_urls], work, limit=2, prefix="db") if db_urls else []
            if db_files:
                files = db_files
                checks.append("режим «фото из базы фильмов»: постер/портрет TMDB")
            else:
                files = await _check_media(files, res, work, checks)  # ИИ смотрит, о том ли картинка
            illustrated = False
            if not files:  # ни фото из новости, ни постера в базе — картинку рисует ИИ (помечена как иллюстрация)
                files = await _illustration(res, work, checks)
                illustrated = bool(files)
            # у нарисованной картинки — видимая подпись в самом посте, а не только плашка на картинке
            text = res["post"]
            if illustrated:  # сразу под заголовком: подгонка под 1024 символа режет конец текста, а не начало
                head, _, rest = text.partition("\n")
                text = f"{head}\n🎨 иллюстрация\n{rest}" if rest else f"{head}\n🎨 иллюстрация"
            caption = post_caption(text, "#новости")
            seen = ("\n\n🖼 Проверка картинки ИИ:\n" + "\n".join(f"• {escape(c)}" for c in checks)) if checks else ""
            # достоверность — автору под постом: слух в канале должен звучать как слух
            status = {"official": "✅ официально (студия, сервис или сам человек)",
                      "reported": "📰 сообщают издания (Deadline, Variety и др.)",
                      "rumor": "🕵️ СЛУХ — не подтверждено; в посте должно быть видно, что это слух"}.get(
                res.get("status") or "")
            if status:
                seen += f"\n\n🏷 Достоверность: {status}"
            for chat_id in chat_ids:
                ids, kind = await _send_media(bot, chat_id, files, caption)
                await offer_post(bot, chat_id, ids, kind, "pt:fast" if mode == "fast" else "pt:make",
                                 note=_sources_note(sources) + seen)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return res["topic"] or "пост"


LIST_POSTS = {  # подборки для поста: ключ темы шортсов → кнопка
    "week": "🎟 Премьеры недели в кино", "fresh": "🔥 Только вышло онлайн", "online": "🆕 Новинки онлайн",
    "cinema": "🍿 Сейчас в кино", "films": "🎬 Новые фильмы", "series": "📺 Новые сериалы",
    "weekfilms": "🗓 Вышло онлайн за неделю: фильмы", "weekseries": "🗓 Вышло онлайн за неделю: сериалы",
    "upcoming": "⏳ Скоро в кино", "topfilms": "🏆 Топ фильмов", "topseries": "🏆 Топ сериалов",
}


POST_SECTIONS: dict[str, tuple[str, str]] = {  # раздел меню постов → (кнопка, заголовок)
    "news": ("📰 Новости", "📰 <b>Новости</b> — свежее из Telegram-каналов и зарубежных источников, по ссылке или сводкой."),
    "lists": ("🎬 Подборки", "🎬 <b>Подборки</b> — новинки, топы, жанры и темы; постеры фирменными карточками."),
    "dates": ("🎂 Даты и люди", "🎂 <b>Даты и люди</b> — дни рождения, круглые даты, актёры, оценки критиков."),
    "fun": ("💬 Рубрики и вовлечение", "💬 <b>Рубрики и вовлечение</b> — факты, викторины, голосования, фильм вечера."),
    "promo": ("📣 Продвижение канала", "📣 <b>Продвижение</b> — реклама, бот, буст, закреп, розыгрыш, статьи."),
}


def posts_menu() -> InlineKeyboardMarkup:
    """Посты в Telegram-канал — по разделам, чтобы не листать длинный список."""
    rows = [[_btn(f"{HOLIDAYS[k][0]}: подборка к празднику", f"pt:l:h:{k}")]
            for k in holidays_now(datetime.now(MSK).date())]
    rows.append([_btn("📰 Свежая новость", "pt:make"), _btn("⚡ Инсайды раньше всех", "pt:fast")])
    rows += [[_btn(POST_SECTIONS[a][0] + " ▸", f"pt:sec:{a}"), _btn(POST_SECTIONS[b][0] + " ▸", f"pt:sec:{b}")]
             for a, b in (("news", "lists"), ("dates", "fun"))]
    rows.append([_btn(POST_SECTIONS["promo"][0] + " ▸", "pt:sec:promo")])
    rows.append([_btn("🗓 Автоплан постов", "pt:plan"), _btn("🔔 Авто-новости 30 мин", "pt:auto")])
    rows.append([_btn("🎬 Трейлеры студий с YouTube (уведомления)", "ytr:list")])
    rows.append([_btn("⬅️ В админку", "admin:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def posts_section(name: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if name == "news":
        rows = [[_btn("📰 Свежая новость из каналов", "pt:make")],
                [_btn("⚡ Инсайды и слухи раньше всех (Reddit, X, Deadline)", "pt:fast")],
                [_btn("🔗 Пост по ссылке или тексту", "pt:lk")],
                [_btn("🧩 Сводка по теме из всех каналов", "pt:dg")],
                [_btn("💰 Сборы уикенда", "pt:bo"), _btn("🗞 Итоги недели", "pt:wk")],
                [_btn("🖼 Откуда брать фото к новостям", "pt:pmode")]]
    elif name == "lists":
        keys = list(LIST_POSTS)
        rows = [[_btn(LIST_POSTS[k], f"pt:l:{k}") for k in keys[i:i + 2]] for i in range(0, len(keys), 2)]
        rows += [[_btn(f"🎭 {g.capitalize()}", f"pt:l:genre:{g}") for g in SHORTS_GENRES[k:k + 3]]
                 for k in range(0, len(SHORTS_GENRES), 3)]
        rows += [[_btn("🎨 Ещё подборки: малобюджетные хорроры, концовки… ▸", "pt:styles")],
                 [_btn("🎉 Подборки к праздникам ▸", "pt:hol")],
                 [_btn("🔗 Подборка по ссылке или тексту", "pt:ll")]]
    elif name == "dates":
        rows = [[_btn("🎂 День рождения звезды", "bd:list"), _btn("📅 Этот день в кино", "pt:an")],
                [_btn("🎭 Фильмы и сериалы с актёром", "pt:ac")],
                [_btn("🍅 Что пишут критики и зрители (Rotten Tomatoes)", "pt:rt")]]
    elif name == "fun":
        rows = [[_btn("💡 Факты: новое", "pt:fx:new"), _btn("📼 Факты: классика", "pt:fx:old")],
                [_btn("🧩 Угадай фильм по кадру", "pt:qz"), _btn("💬 Цитата дня", "pt:qt")],
                [_btn("🎬 Фильм вечера", "pt:ev"), _btn("⚔️ Битва фильмов", "pt:bt")],
                [_btn("🗳 Выбор подписчиков", "pt:vt"), _btn("🗳 Итог голосования → факты", "pt:vr")],
                [_btn("🎭 Ляпы в кино", "pt:bl")],
                [_btn("🎯 Рекомендация: что посмотреть (опрос в боте)", "pt:rq")],
                [_btn("🏆 Знатоки месяца (викторина)", "pt:qw"), _btn("⭐ Топ подписчиков", "pt:us")]]
    elif name == "promo":
        rows = [[_btn("📣 Реклама канала (подпишись)", "pt:pr"), _btn("🤖 Пост про бота", "pt:ab")],
                [_btn("📌 Закреп-навигатор", "pt:nv"), _btn("⚡ Буст канала", "pt:bs")],
                [_btn("🎁 Розыгрыш", "pt:gv"), _btn("📰 Статья для Дзен/VK", "pt:ar")],
                [_btn("🤝 Обращение к подписчикам (активность, поделиться)", "pt:sp")]]
    rows.append([_btn("⬅️ Все разделы", "pt:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


RATING_MIN_VOTES = 1000  # меньше оценок — рейтинг случайный (у новинок в первые дни бывает 8+), не показываем


def _trusted_rating(f: dict[str, Any]) -> tuple[float, str]:
    """Рейтинг, если за ним достаточно оценок; иначе (0, "")."""
    votes = f.get("votes") or {}
    try:
        enough = max(int(votes.get("kp") or 0), int(votes.get("imdb") or 0)) >= RATING_MIN_VOTES or (
            shorts_daily._best_rating(f)[1] == "TMDB")  # TMDB: _best_rating сам проверил число голосов
    except (TypeError, ValueError):
        enough = False
    return shorts_daily._best_rating(f) if enough else (0.0, "")


def _film_line(f: dict[str, Any]) -> str:
    rating, label = _trusted_rating(f)
    about = re.sub(r"\s+", " ", str(f.get("shortDescription") or f.get("description") or "")).strip()[:260]
    parts = [f"«{shorts_daily._title(f)}» ({f.get('year') or '?'})", *shorts_daily._genres(f)[:3]]
    if rating:
        parts.append(f"{label} {rating:.1f}")
    premiere = f.get("premiere") or {}
    for key, where in (("russia", "в кино в России с"), ("digital", "онлайн с"), ("world", "мировая премьера")):
        day = str(premiere.get(key) or "")[:10]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):  # дата выхода — ИИ не возьмёт старое в «премьеры недели»
            y, m, d = map(int, day.split("-"))
            parts.append(f"{where} {d} {shorts_daily.MONTHS[m - 1]} {y}")
            break
    return ", ".join(parts) + (f". {about}" if about else "")


BRAND = "🎬 Подборка от KinoMir360"


def _brand(post: str) -> str:
    """Под заголовком подборки — «Подборка от KinoMir360» (если ИИ сам не написал)."""
    if "kinomir360" in post[:250].lower():
        return post
    head, _, rest = post.partition("\n")
    return f"{head}\n{BRAND}\n{rest.lstrip(chr(10))}" if rest else f"{head}\n{BRAND}"


_ITEM_RE = re.compile(r"^\s*(?:\d️⃣|🔟|\d{1,2}[.)])")


def _compact_list(post: str, limit: int) -> str:
    """Подборка не влезает в подпись Telegram (1024 символа) — сокращаем, но так, чтобы ВСЕ фильмы остались:
    сначала убираем вводную фразу, потом укорачиваем описания фильмов (по целым словам), потом убираем их совсем,
    потом строку реакций. Хэштеги и строки названий не трогаем."""
    if len(post) <= limit:
        return post
    lines = post.split("\n")
    items = [k for k, ln in enumerate(lines) if _ITEM_RE.match(ln)]
    if not items:
        return post
    first, last = items[0], items[-1]
    # описание фильма — непустые строки после строки названия до следующего фильма (у последнего — до пустой)
    about: list[int] = []
    for n, i in enumerate(items):
        end = items[n + 1] if n + 1 < len(items) else len(lines)
        for k in range(i + 1, end):
            if not lines[k].strip():
                if n + 1 == len(items):
                    break
                continue
            if not lines[k].lstrip().startswith("#") and "❤️" not in lines[k]:
                about.append(k)

    def size() -> int:
        return len("\n".join(ln for ln in lines if ln is not None))

    # 1) вводная фраза между заголовком (с подписью) и первым фильмом
    for k in range(2, first):
        if size() > limit and lines[k] and lines[k].strip() and "kinomir360" not in lines[k].lower():
            lines[k] = None
    # 2) описания — короче, по целым словам
    for cap in (110, 90, 70, 55, 40):
        if size() <= limit:
            break
        for k in about:
            if lines[k] and len(lines[k]) > cap:
                cut = lines[k][:cap].rsplit(" ", 1)[0].rstrip(",;:—– ")
                lines[k] = cut + "…"
    # 3) без описаний, 4) без строки реакций
    for k in about:
        if size() > limit:
            lines[k] = None
    for k in range(last + 1, len(lines)):
        if size() > limit and lines[k] and ("❤️" in lines[k] or "🔥" in lines[k]):
            lines[k] = None
    text = "\n".join(ln for ln in lines if ln is not None)
    return re.sub(r"\n{3,}", "\n\n", text)


def _list_fallback(title: str, films: list[dict[str, Any]]) -> str:
    nums = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣"]
    rows = [f"🍿 {title.upper()}!", BRAND, ""]
    for n, f in zip(nums, films):
        rating, _ = _trusted_rating(f)
        about = str(f.get("shortDescription") or "").strip()
        rows.append(f"{n} «{shorts_daily._title(f)}» ({f.get('year') or '?'})" + (f" ⭐ {rating:.1f}" if rating else ""))
        if about:
            rows.append(about[:120])
        rows.append("")
    rows += ["❤️ — беру на вечер 🔥 — уже смотрел 📤 Перешли другу, с кем будешь смотреть", "",
             "#ЧтоПосмотреть #KinoMir360 #Кино"]
    return "\n".join(rows)


def _same_title(a: str, b: str) -> bool:
    norm = lambda x: re.sub(r"[^\w]+", "", x.lower().replace("ё", "е"))  # noqa: E731
    return bool(norm(a)) and norm(a) == norm(b)


async def _list_poster(f: dict[str, Any], kp_films: list[dict[str, Any]],
                       posts: list[dict[str, Any]]) -> tuple[str | None, dict[str, Any] | None]:
    """Постер фильма из подборки и его карточка Кинопоиска (для рейтинга и актёров): кандидат Кинопоиска,
    поиск на Кинопоиске, иначе — картинка из поста канала о нём (без карточки)."""
    names = [x for x in (f["ru"], f["en"]) if x]
    for d in kp_films:
        if any(_same_title(n, t) for n in names for t in (shorts_daily._title(d), str(d.get("enName") or ""))):
            return shorts_daily._poster_url(d), d
    # поиск на Кинопоиске — только точное совпадение названия: первый результат поиска бывает чужим
    # («Американская история ужасов» → «Американская история X», «Тёмная материя» → «Тёмные начала»)
    def titles(d: dict[str, Any]) -> list[str]:
        out = [shorts_daily._title(d), str(d.get("alternativeName") or ""), str(d.get("enName") or "")]
        return out + [str(x.get("name") or "") for x in d.get("names") or [] if isinstance(x, dict)]

    for q in names:
        try:
            data = await kp._get("/movie/search", query=q, page=1, limit=10)
        except Exception as exc:  # noqa: BLE001
            logging.info("List post: kinopoisk lookup %r failed: %s", q, exc)
            continue
        docs = [d for d in data.get("docs") or [] if isinstance(d, dict) and d.get("id")]
        same = [d for d in docs if any(_same_title(n, t) for n in names for t in titles(d))]
        if not same and f["year"]:  # чуть другое написание («Шериф поневоле» / «Шериф-поневоле») — того же года
            same = [d for d in docs if abs(int(d.get("year") or 0) - f["year"]) <= 1 and max(
                (shorts_daily.tmdb.similar(n, t) for n in names for t in titles(d) if t), default=0) >= 0.85]
        if f.get("series") is not None and same:
            # «Величайший (1 сезон)» — сериал, а не одноимённый фильм 2024 года: тип должен совпасть
            kind = [d for d in same if bool(d.get("isSeries")) == bool(f["series"])]
            same = kind or ([] if f["series"] else same)
        if f.get("fresh") and not f["year"] and same:
            # новинка из поста-афиши: «Обитель зла» на Apple TV — это фильм 2026 года, а не классика 2002-го
            this_year = datetime.now(MSK).year
            recent = [d for d in same if int(d.get("year") or 0) >= this_year - 1]
            same = sorted(recent, key=lambda d: -int(d.get("year") or 0))  # нет новой — ищем на TMDB, не классику
        same.sort(key=lambda d: (abs(int(d.get("year") or 0) - f["year"]) > 1 if f["year"] else False,
                                 -int((d.get("votes") or {}).get("kp") or 0)))
        for d in same[:2]:
            try:
                full = await kp._get(f"/movie/{d['id']}")
            except Exception:  # noqa: BLE001
                full = d
            if shorts_daily._poster_url(full):
                return shorts_daily._poster_url(full), full
    try:  # нет на Кинопоиске (бывает у свежих сериалов) — постер с TMDB, тоже по точному названию
        url = await shorts_daily.tmdb.poster_by_title(
            TMDB_API_KEY, names, f["year"] or (datetime.now(MSK).year if f.get("fresh") else None))
        if url:
            return url, None
    except Exception as exc:  # noqa: BLE001
        logging.info("List post: TMDB poster %r failed: %s", names, exc)
    for p in posts:
        if p.get("image") and shorts_tgsearch._mentions(p["text"], names):
            return p["image"], None
    return None, None


async def _list_cards(work: Path, items: list[tuple[str, str | None, dict[str, Any] | None]],
                      extras: list[str] | None = None,
                      episodes: list[tuple[str, str] | None] | None = None) -> list[Path]:
    """Фирменные карточки KinoMir360 для альбома подборки: (название, картинка, карточка Кинопоиска);
    extras — своя нижняя строка у каждой карточки (например «Сборы: $45 млн»)."""
    out = []
    for k, (name, url, film) in enumerate(items, 1):
        extra = (extras or [])[k - 1] if extras and k <= len(extras) else None
        if not url:
            continue
        got = await shorts_tgsearch.download_media([{"image": url}], work, limit=1, prefix=f"src{k}_")
        if not got:
            continue
        try:
            rating, label = _trusted_rating(film) if film else (0.0, "")
            ep = (episodes or [])[k - 1] if episodes and k <= len(episodes) else None
            card = await asyncio.to_thread(
                shorts_postcard.make_post_card, Image.open(got[0]), name, k, len(items), rating or None, label,
                ep[1] if ep else (film or {}).get("year"), shorts_daily._genres(film)[:2] if film else None,
                shorts_daily._actors(film, 3) if film else None, extra=extra, episode=ep[0] if ep else None)
            path = work / f"card{k}.jpg"
            card.save(path, quality=92)
            out.append(path)
        except Exception:  # noqa: BLE001 — битая картинка — без этой карточки
            logging.exception("List post card failed")
    return out


async def _send_album(bot: Bot, chat_id: int, files: list[Path], post: str, rubric: str = "",
                      links: list[tuple[str, str]] | None = None) -> tuple[list[int], str]:
    """Альбом фото с текстом поста подписью к первому (одно фото или только текст — как получится)."""
    caption = post_caption(post, rubric, links)
    if len(files) >= 2:
        sent = await bot.send_media_group(chat_id, [
            InputMediaPhoto(media=FSInputFile(path), caption=caption if k == 0 else None)
            for k, path in enumerate(files[:10])])
        return [m.message_id for m in sent], "caption"
    if files:
        return [(await bot.send_photo(chat_id, FSInputFile(files[0]), caption=caption)).message_id], "caption"
    return [(await bot.send_message(chat_id, caption, disable_web_page_preview=True)).message_id], "text"


async def _send_media(bot: Bot, chat_id: int, files: list[Path], caption: str) -> tuple[list[int], str]:
    """Фото и видео поста (весь альбом источника, до 10) с подписью к первому; одно — фото или видео; нет —
    просто текст."""
    files = files[:10]
    if len(files) >= 2:
        # подпись — сразу в первом элементе: объекты aiogram неизменяемые, потом её не добавить
        media = [(InputMediaVideo(media=FSInputFile(f), supports_streaming=True, caption=caption if k == 0 else None)
                  if f.suffix == ".mp4" else InputMediaPhoto(media=FSInputFile(f), caption=caption if k == 0 else None))
                 for k, f in enumerate(files)]
        sent = await bot.send_media_group(chat_id, media)
        return [m.message_id for m in sent], "caption"
    if files and files[0].suffix == ".mp4":
        return [(await bot.send_video(chat_id, FSInputFile(files[0]), caption=caption,
                                      supports_streaming=True)).message_id], "caption"
    if files:
        return [(await bot.send_photo(chat_id, FSInputFile(files[0]), caption=caption)).message_id], "caption"
    return [(await bot.send_message(chat_id, caption, disable_web_page_preview=True)).message_id], "text"


async def _image(url: str | None, work: Path, prefix: str) -> Image.Image | None:
    if not url:
        return None
    got = await shorts_tgsearch.download_media([{"image": url}], work, limit=1, prefix=prefix)
    try:
        return await asyncio.to_thread(lambda: Image.open(got[0]).convert("RGB")) if got else None
    except Exception:  # noqa: BLE001
        return None


async def make_birthday_post(bot: Bot, chat_id: int, chosen: dict[str, Any] | None = None) -> None:
    """День рождения звезды кино: пара фраз о человеке, его лучшие фильмы с ролями и лёгкое поздравление.
    Альбом: портрет «С днём рождения» и карточки фильмов; в канале — опрос «Какой фильм любимый?».
    chosen — именинник, которого выбрал автор из списка (иначе — самый известный, кого ещё не было)."""
    today = datetime.now(MSK).date()
    hist = await _posts_history()
    if chosen:
        who = chosen
        info = await shorts_daily.tmdb.person(TMDB_API_KEY, who["imdb"])
        top = shorts_daily.tmdb.best_movies(info, shorts_daily.BIRTHDAY_FILMS) if info else []
        if not top:
            raise RuntimeError(f"у «{who['name']}» на TMDB не нашлось фильмов — выбери другого")
    else:
        who, info, top = await shorts_daily.pick_birthday(today, {int(x) for x in hist["people"]}, TMDB_API_KEY)
    name, female = who["name"], who["female"]
    age = today.year - date.fromisoformat(who["born"]).year
    years = shorts_daily._years(age)
    director = info.get("known_for_department") == "Directing"
    films = []
    for f in top:
        role = await shorts_daily._kp_role(kp._get, f, who) if f.get("job") != "Director" else ""
        if not role and not shorts_daily._latin(str(f.get("character") or "")):
            role = str(f.get("character") or "").strip()
        films.append({"title": str(f.get("title") or f.get("original_title") or ""),
                      "year": str(f.get("release_date") or "")[:4], "rating": float(f.get("vote_average") or 0),
                      "role": role, "poster": f.get("poster_path") or f.get("backdrop_path")})
    who_is = "режиссёр" if director else "актриса" if female else "актёр"
    fame = shorts_daily.tmdb.known_for(info, 4)
    material = (f"{name}" + (f" ({who['en']})" if who["en"] else "") + f", {who_is}, сегодня {age} {years}.\n"
                + (f"Чем известен зрителям прежде всего (фильмы и сериалы): {', '.join(fame)}\n" if fame else "")
                + "Лучшие фильмы:\n" + "\n".join(
                    f"— «{f['title']}» ({f['year']})" + (f", роль: {f['role']}" if f["role"] else "")
                    for f in films)
                + "\n\nБиография:\n" + str(info.get("biography") or "—")[:2500])
    task = (f"Сегодня день рождения: {name}, {age} {years}. Напиши пост-поздравление: заголовок «🎂 С ДНЁМ "
            f"РОЖДЕНИЯ, …!» с возрастом; 2–3 предложения о том, чем {'она известна' if female else 'он известен'} "
            "и какой ролью или фильмом запомнились зрителям; затем лучшие фильмы из материала строками «1️⃣ "
            "«Название» (год) — роль»; лёгкое тёплое поздравление с эмодзи и строка реакций, чтобы подписчики "
            "поздравили (например «🎉 — поздравляю! ❤️ — обожаю эти фильмы»).")
    post = await shorts_ai.write_styled_post(GEMINI_API_KEY, task, material) or "\n".join(
        [f"🎂 С ДНЁМ РОЖДЕНИЯ, {name.upper()}!", "", f"Сегодня {'ей' if female else 'ему'} {age} {years} 🎉", "",
         "Лучшие фильмы:"]
        + [f"{n} «{f['title']}» ({f['year']})" + (f" — {f['role']}" if f["role"] else "")
           for n, f in zip(["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"], films)]
        + ["", "🎉 — поздравляю! ❤️ — обожаю эти фильмы", "", "#ДеньРождения #Кино #KinoMir360"])
    hist["people"] = (hist["people"] + [str(shorts_daily.person_uid(who["imdb"]))])[-100:]
    await save_posts_history(hist)
    work = SHORTS_DIR / f"bday_{time.time_ns()}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        cards: list[Path] = []
        known = ", ".join(f"«{t}»" for t in (shorts_daily.tmdb.known_for(info, 2) or [f["title"] for f in films[:2]]))
        for url in shorts_daily.tmdb.profile_urls(info)[:2]:
            photo = await _image(url, work, "face")
            if photo:
                card = await asyncio.to_thread(
                    shorts_postcard.make_post_card, photo, name, pill=("С ДНЁМ РОЖДЕНИЯ", ""), portrait=True,
                    meta=f"Сегодня {age} {years}", extra=f"{'Известна' if female else 'Известен'} по: {known}")
                cards.append(work / "face.jpg")
                card.save(cards[-1], quality=92)
                break
        for k, f in enumerate(films, 1):
            img = await _image(f"{shorts_daily.tmdb.IMG}w780{f['poster']}" if f["poster"] else None, work, f"f{k}_")
            if not img:
                continue
            rating = f["rating"] if f["rating"] >= shorts_daily.MIN_RATING else None
            card = await asyncio.to_thread(shorts_postcard.make_post_card, img, f["title"], k, len(films), rating,
                                           "TMDB", f["year"], None, None,
                                           extra=f"Роль: {f['role']}" if f["role"] else None)
            cards.append(work / f"film{k}.jpg")
            card.save(cards[-1], quality=92)
        ids, kind = await _send_album(bot, chat_id, cards, post, "#деньрождения")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    await offer_post(bot, chat_id, ids, kind, "pt:bd", [f["title"] for f in films], "Пост ко дню рождения готов",
                     f"Какой фильм с {'ней' if female else 'ним'} ваш любимый? 🎬")


ACTOR_SKIP_GENRES = {99, 10763, 10764, 10767}  # документальные, новости, реалити, ток-шоу


async def find_actor(query: str) -> dict[str, Any] | None:
    """Человек на TMDB (профиль на русском, фильмы, сериалы, фото): по имени как написано, иначе по
    английскому имени с Кинопоиска («Патрик Стюарт» → «Patrick Stewart»)."""
    info = await shorts_daily.tmdb.person_by_name(TMDB_API_KEY, query)
    if info:
        return info
    try:
        data = await kp._get("/person/search", query=query, page=1, limit=5)
    except Exception:  # noqa: BLE001
        logging.exception("KP person search failed")
        return None
    for doc in data.get("docs") or []:
        if doc.get("enName"):
            return await shorts_daily.tmdb.person_by_name(TMDB_API_KEY, str(doc["enName"]))
    return None


def actor_works(info: dict[str, Any], films: int = 5, series: int = 3) -> list[dict[str, Any]]:
    """Самые известные фильмы и сериалы с человеком: без камео «в роли себя», документалок и ток-шоу;
    у сериалов — только заметные роли (от 3 эпизодов)."""
    self_role = re.compile(r"\b(self|himself|herself|себя|камео)\b", re.I)

    def ok(c: dict[str, Any]) -> bool:
        return (bool(c.get("id")) and not self_role.search(str(c.get("character") or ""))
                and not ACTOR_SKIP_GENRES & set(c.get("genre_ids") or []))

    movies: dict[int, dict[str, Any]] = {}
    for c in (info.get("movie_credits") or {}).get("cast") or []:
        if ok(c) and c.get("release_date") and (c.get("vote_count") or 0) >= 50:
            movies.setdefault(int(c["id"]), {**c, "tv": False})
    shows: dict[int, dict[str, Any]] = {}
    for c in (info.get("tv_credits") or {}).get("cast") or []:
        if ok(c) and c.get("first_air_date") and (c.get("vote_count") or 0) >= 30 and (c.get("episode_count") or 0) >= 3:
            shows.setdefault(int(c["id"]), {**c, "tv": True})
    top_m = sorted(movies.values(), key=lambda c: c.get("vote_count") or 0, reverse=True)[:films]
    top_s = sorted(shows.values(), key=lambda c: c.get("vote_count") or 0, reverse=True)[:series]
    if len(top_s) < series:  # мало сериалов — добираем фильмами
        top_m = sorted(movies.values(), key=lambda c: c.get("vote_count") or 0, reverse=True)[:films + series - len(top_s)]
    return top_m + top_s


async def make_actor_post(bot: Bot, chat_id: int, query: str) -> None:
    """«🎭 Фильмы и сериалы с актёром»: портрет и лучшие фильмы и сериалы с ролями, опрос «какой любимый».
    Если человека нет в живых (или автор написал «память») — пост «Светлая память» с чёрно-белым портретом."""
    memorial = bool(re.search(r"\bпамят", query, re.I))
    name_q = re.sub(r"\b(светлая\s+)?памят\w*\b", "", query, flags=re.I).strip(" ,.-—")
    info = await find_actor(name_q)
    if not info:
        raise RuntimeError(f"не нашёл «{name_q}» — проверь имя (можно по-английски)")
    name = str(info.get("name") or name_q)
    if shorts_daily._latin(name):  # на TMDB нет русского имени — берём как написал автор
        name = name_q
    female = info.get("gender") == 1
    born, died = str(info.get("birthday") or "")[:4], str(info.get("deathday") or "")[:4]
    memorial = memorial or bool(died)
    works = actor_works(info)
    if not works:
        raise RuntimeError(f"у «{name}» на TMDB не нашлось известных фильмов и сериалов")
    who = {"name": name, "en": str(info.get("original_name") or "") if not shorts_daily._latin(name_q) else name_q}
    items = []
    for w in works:
        title = str(w.get("name") or w.get("original_name") if w["tv"] else w.get("title") or w.get("original_title") or "")
        when = str((w.get("first_air_date") if w["tv"] else w.get("release_date")) or "")[:4]
        role = await shorts_daily._kp_role(kp._get, {"title": title, "release_date": when}, who)
        if not role and not shorts_daily._latin(str(w.get("character") or "")):
            role = str(w.get("character") or "").strip()
        items.append({"title": title, "year": when, "tv": w["tv"], "rating": float(w.get("vote_average") or 0),
                      "role": role, "poster": w.get("poster_path") or w.get("backdrop_path"),
                      "episodes": int(w.get("episode_count") or 0)})
    life = f"{born}–{died}" if born and died else (born and f"род. {born}") or ""
    material = (f"{name} ({who['en'] or '—'}), {'актриса' if female else 'актёр'}" + (f", {life}" if life else "")
                + ".\nФильмы и сериалы:\n" + "\n".join(
                    f"— {'сериал' if i['tv'] else 'фильм'} «{i['title']}» ({i['year']})"
                    + (f", роль: {i['role']}" if i["role"] else "") for i in items)
                + "\n\nБиография:\n" + str(info.get("biography") or "—")[:2500])
    if memorial:
        task = (f"Напиши пост памяти: {name} ({life}). Заголовок «🕯 СВЕТЛАЯ ПАМЯТЬ: {name.upper()}»; 2–3 сдержанных "
                f"предложения о том, чем {'она запомнилась' if female else 'он запомнился'} зрителям; затем лучшие роли "
                "из материала строками «🎬 «Название» (год) — роль» (у сериалов пометь «сериал»); в конце — "
                "«Какая роль запомнилась вам больше всего?» и хэштеги. Без шуток и восклицаний, факты — только "
                "из материала.")
    else:
        task = (f"Напиши пост-подборку «Фильмы и сериалы с {name}»: заголовок с эмодзи 🎭; 1–2 предложения, чем "
                f"{'она интересна' if female else 'он интересен'}; затем фильмы и сериалы из материала строками "
                "«1️⃣ «Название» (год) — роль» (у сериалов пометь «сериал»); в конце вопрос подписчикам и хэштеги. "
                "Факты — только из материала.")
    num = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣"]
    post = await shorts_ai.write_styled_post(GEMINI_API_KEY, task, material) or "\n".join(
        [f"🕯 СВЕТЛАЯ ПАМЯТЬ: {name.upper()}" if memorial else f"🎭 ФИЛЬМЫ И СЕРИАЛЫ С {name.upper()}", ""]
        + [f"{'🎬' if memorial else num[k]} «{i['title']}» ({i['year']})" + (" — сериал" if i["tv"] else "")
           + (f" — {i['role']}" if i["role"] else "") for k, i in enumerate(items)]
        + ["", "Какая роль запомнилась вам больше всего?" if memorial else "Что из этого вы смотрели? 👇", "",
           "#Кино #KinoMir360"])
    work = SHORTS_DIR / f"actor_{time.time_ns()}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        cards: list[Path] = []
        known = ", ".join(f"«{i['title']}»" for i in items[:2])
        for url in shorts_daily.tmdb.profile_urls(info)[:2]:
            photo = await _image(url, work, "face")
            if photo:
                if memorial:
                    photo = ImageOps.grayscale(photo).convert("RGB")
                card = await asyncio.to_thread(
                    shorts_postcard.make_post_card, photo, name,
                    pill=("СВЕТЛАЯ ПАМЯТЬ", "") if memorial else ("ФИЛЬМЫ И СЕРИАЛЫ", ""),
                    meta=life or None, extra=f"{'Известна' if female else 'Известен'} по: {known}")
                cards.append(work / "face.jpg")
                card.save(cards[-1], quality=92)
                break
        for k, i in enumerate(items, 1):
            img = await _image(f"{shorts_daily.tmdb.IMG}w780{i['poster']}" if i["poster"] else None, work, f"w{k}_")
            if not img:
                continue
            rating = i["rating"] if i["rating"] >= shorts_daily.MIN_RATING else None
            role = f"Роль: {i['role']}" if i["role"] else None
            if i["tv"]:
                role = "Сериал" + (f" · {i['role']}" if i["role"] else "")
            card = await asyncio.to_thread(shorts_postcard.make_post_card, img, i["title"], k, len(items), rating,
                                           "TMDB", i["year"], None, None, extra=role)
            cards.append(work / f"work{k}.jpg")
            card.save(cards[-1], quality=92)
        ids, kind = await _send_album(bot, chat_id, cards[:10], post, "#сактёром")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    him = "ней" if female else "ним"
    await offer_post(bot, chat_id, ids, kind, "pt:ac", [i["title"] for i in items][:10],
                     "Пост памяти готов" if memorial else "Подборка с актёром готова",
                     "Какая роль запомнилась вам больше всего? 🕯" if memorial
                     else f"Какой фильм или сериал с {him} ваш любимый? 🎬")


bd_lists: dict[int, dict[str, Any]] = {}  # чат → {"people": именинники дня, "sel": выбранные номера}


def birthday_menu(chat_id: int) -> InlineKeyboardMarkup:
    data = bd_lists.get(chat_id) or {"people": [], "sel": set()}
    today = datetime.now(MSK).date()
    rows = []
    for i, p in enumerate(data["people"]):
        age = today.year - int(p["born"][:4])
        rows.append([_btn(f"{'✅' if i in data['sel'] else '▫️'} {p['name']}, {age}", f"bd:t:{i}")])
    n = len(data["sel"])
    rows.append([_btn(f"🎂 Сделать пост ({n})" if n else "🎂 Выбери хотя бы одного", "bd:go")])
    rows.append([_btn("🎲 Самый известный (автоматически)", "pt:bd")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("bd:"))
async def birthday_buttons(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS or not callback.message:
        await callback.answer()
        return
    message, action = callback.message, (callback.data or "")[3:]
    chat_id = message.chat.id
    if action == "list":
        await callback.answer()
        wait = await message.answer("🎂 Ищу, у кого из кино сегодня день рождения…")
        today = datetime.now(MSK).date()
        try:
            people = await shorts_daily.wiki.birthdays(today.month, today.day, limit=20)
        except Exception as exc:  # noqa: BLE001
            await wait.edit_text(f"❌ Не получилось: <code>{escape(_safe_err(exc)[:300])}</code>")
            return
        if not people:
            await wait.edit_text("Сегодня в Википедии нет именинников из кино 🤷")
            return
        bd_lists[chat_id] = {"people": people, "sel": set()}
        await wait.edit_text("🎂 <b>Сегодня дни рождения</b> — от самых известных. Отметь одного или нескольких "
                             "(актёры, режиссёры, продюсеры) и нажми «Сделать пост». Несколько — один общий пост.",
                             reply_markup=birthday_menu(chat_id))
        return
    data = bd_lists.get(chat_id)
    if not data:
        await callback.answer("Список устарел — нажми «🎂 День рождения» ещё раз", show_alert=True)
        return
    if action.startswith("t:"):
        i = int(action[2:])
        data["sel"].symmetric_difference_update({i})
        await callback.answer()
        try:
            await message.edit_reply_markup(reply_markup=birthday_menu(chat_id))
        except TelegramBadRequest:
            pass
        return
    if action == "go":
        chosen = [data["people"][i] for i in sorted(data["sel"])]
        if not chosen:
            await callback.answer("Отметь хотя бы одного человека", show_alert=True)
            return
        await callback.answer()
        if post_lock.locked():
            await message.answer("⏳ Пост уже пишется — подожди минуту.")
            return
        wait = await message.answer(f"🎂 Готовлю пост: {escape(', '.join(p['name'] for p in chosen))}…")

        async def job() -> None:
            try:
                async with post_lock:
                    if len(chosen) == 1:
                        await make_birthday_post(message.bot, chat_id, chosen[0])
                    else:
                        await make_birthdays_post(message.bot, chat_id, chosen)
                await wait.delete()
            except Exception as exc:  # noqa: BLE001
                logging.exception("Birthday post failed")
                await wait.edit_text(f"❌ Пост не получился: <code>{escape(_safe_err(exc)[:300])}</code>")

        _run_job(job)


async def make_birthdays_post(bot: Bot, chat_id: int, people: list[dict[str, Any]]) -> None:
    """Общий пост «Сегодня дни рождения»: несколько звёзд, у каждого возраст и 2–3 главных фильма, портреты."""
    today = datetime.now(MSK).date()
    rows, cards_data = [], []
    for who in people[:6]:
        try:
            info = await shorts_daily.tmdb.person(TMDB_API_KEY, who["imdb"])
        except Exception:  # noqa: BLE001
            info = None
        age = today.year - int(who["born"][:4])
        titles = (shorts_daily.tmdb.known_for(info, 3) or [str(f.get("title") or f.get("original_title") or "")
                  for f in shorts_daily.tmdb.best_movies(info, 3)]) if info else []
        rows.append(f"{who['name']} ({who['en'] or '—'}), {age} {shorts_daily._years(age)}"
                    + (f"; известен по: {', '.join(titles)}" if titles else ""))
        cards_data.append((who, info, age, titles))
    task = ("Сегодня дни рождения у нескольких звёзд кино. Напиши пост: заголовок «🎂 СЕГОДНЯ ДНИ РОЖДЕНИЯ "
            "ОТМЕЧАЮТ»; для каждого — строка с эмодзи: имя, сколько лет и 2–3 главных фильма в «ёлочках»; тёплое "
            "поздравление одной фразой и строка реакций. Только факты из материала.")
    post = await shorts_ai.write_styled_post(GEMINI_API_KEY, task, "\n".join(rows)) or "\n".join(
        ["🎂 СЕГОДНЯ ДНИ РОЖДЕНИЯ ОТМЕЧАЮТ", ""] + [f"🎉 {r}" for r in rows]
        + ["", "Поздравляем! 🥳 Кого из них любите больше всего?", "", "#ДеньРождения #Кино #KinoMir360"])
    work = SHORTS_DIR / f"bdays_{time.time_ns()}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        cards: list[Path] = []
        for k, (who, info, age, titles) in enumerate(cards_data):
            url = (shorts_daily.tmdb.profile_urls(info) or [None])[0] if info else None
            photo = await _image(url, work, f"face{k}_")
            if not photo:
                continue
            card = await asyncio.to_thread(
                shorts_postcard.make_post_card, photo, who["name"], pill=("С ДНЁМ РОЖДЕНИЯ", ""), portrait=True,
                meta=f"{age} {shorts_daily._years(age)}",
                extra=("Известен по: " if not who.get("female") else "Известна по: ") + ", ".join(f"«{t}»" for t in titles[:2])
                if titles else None)
            cards.append(work / f"face{k}.jpg")
            card.save(cards[-1], quality=92)
        ids, kind = await _send_album(bot, chat_id, cards, post, "#деньрождения")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    await offer_post(bot, chat_id, ids, kind, "bd:list", [p["name"] for p in people[:6]] if len(people) >= 2 else None,
                     "Пост с днями рождения готов", "Кого из именинников любите больше всего? 🎉")


async def make_facts_post(bot: Bot, chat_id: int, era: str = "", film_id: int = 0, voted: bool = False) -> None:
    """5 фактов о фильме (Кинопоиск, интернет, Telegram-каналы): карточка фильма и кадры со знаком канала.
    film_id — этот фильм (voted — его выбрали подписчики в опросе), иначе — случайный."""
    hist = await _posts_history()
    film, kp_facts = await shorts_daily.pick_fact_film(kp._get, f"id:{film_id}" if film_id else "",
                                                       {int(x) for x in hist["facts"]},
                                                       web=shorts_ai.available(GEMINI_API_KEY), era=era)
    facts, _ = await shorts_daily.gather_facts(film, kp_facts, GEMINI_API_KEY)
    title, year = shorts_daily._title(film), film.get("year") or ""
    kind_word = shorts_daily._kind(film)
    material = (_film_line(film) + "\n\nФакты:\n" + "\n".join(f"— {f}" for f in facts[:7]))
    task = (f"Напиши пост «Факты о {kind_word} «{title}» ({year}), которые знают не все»: заголовок КАПСОМ с "
            "эмодзи, одна вводная фраза, затем 5 фактов из материала строками «1️⃣ …» — каждый 1–2 коротких "
            "предложения своими словами, без спойлеров; в конце вопрос «Какой факт удивил больше всего?» и строка "
            "реакций." + (" Начни с того, что этот фильм выбрали подписчики в опросе «Выбор подписчиков» — "
                          "спасибо, что голосовали!" if voted else ""))
    post = await shorts_ai.write_styled_post(GEMINI_API_KEY, task, material, search=False) or "\n".join(
        [f"💡 ФАКТЫ О {kind_word.upper()} «{title.upper()}»!", ""]
        + [f"{n} {f}" for n, f in zip(["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"], facts)]
        + ["", "Какой факт удивил больше всего? 👇", "", "❤️ — не знал! 🔥 — знал всё", "",
           "#ФактыОКино #Кино #KinoMir360"])
    hist["facts"] = (hist["facts"] + [str(film["id"])])[-150:]
    await save_posts_history(hist)
    work = SHORTS_DIR / f"facts_post_{time.time_ns()}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        cards: list[Path] = []
        poster = await _image(shorts_daily._poster_url(film), work, "poster")
        if poster:
            rating, label = shorts_daily._best_rating(film)
            card = await asyncio.to_thread(shorts_postcard.make_post_card, poster, title, 0, 0, rating or None, label,
                                           year, shorts_daily._genres(film)[:2], shorts_daily._actors(film, 3))
            cards.append(work / "card.jpg")
            card.save(cards[-1], quality=92)
        backdrops, _ = await shorts_daily.tmdb.images(TMDB_API_KEY, film, limit=6)
        for k, url in enumerate(shorts_daily.tmdb.pick_varied(backdrops, 3)):
            frame = await _image(url, work, f"frame{k}_")
            if frame:
                cards.append(work / f"frame{k}.jpg")
                (await asyncio.to_thread(shorts_postcard.watermark, frame)).save(cards[-1], quality=92)
        ids, kind = await _send_album(bot, chat_id, cards, post, "#факты", kp.watch_links(film))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    await offer_post(bot, chat_id, ids, kind, f"pt:fx:{era or 'any'}", what="Пост с фактами готов")


async def _popular_films(pages: int = 10, series: bool = False) -> list[dict[str, Any]]:
    """Известные фильмы Кинопоиска (много оценок, рейтинг 7+), случайная страница — для викторины и цитат."""
    docs = await shorts_daily._query(kp._get, page=random.randint(1, pages), sortField="votes.kp", sortType="-1",
                                     notNullFields="name", isSeries=str(series).lower(),
                                     **{"rating.kp": "7-10", "votes.kp": "150000-100000000"})
    random.shuffle(docs)
    return [d for d in docs if d.get("id") and shorts_daily._title(d)]


def _label(f: dict[str, Any]) -> str:
    return f"«{shorts_daily._title(f)}»" + (f" ({f['year']})" if f.get("year") else "")


async def _frames(film: dict[str, Any], work: Path, n: int, prefix: str) -> list[Image.Image]:
    """Кадры фильма с TMDB (без надписей), разные."""
    backdrops, _ = await shorts_daily.tmdb.images(TMDB_API_KEY, film, limit=8)
    out = []
    for k, url in enumerate(shorts_daily.tmdb.pick_varied(backdrops, n)):
        img = await _image(url, work, f"{prefix}{k}_")
        if img:
            out.append(img)
    return out


async def _quiz_from_kp(used: set[str], work: Path) -> tuple[str, str, Image.Image, list[str]] | None:
    """Викторина из известных фильмов Кинопоиска: (id для истории, название, кадр, 3 других названия)."""
    try:
        films = await _popular_films()
    except Exception:  # noqa: BLE001
        logging.exception("Quiz: kinopoisk list failed")
        return None
    for pick in [f for f in films if str(f["id"]) not in used][:6]:
        try:
            film = await kp._get(f"/movie/{pick['id']}")  # полная карточка — с id TMDB/IMDb, кадр точно из него
            frames = await _frames(film, work, 1, "q")
        except Exception:  # noqa: BLE001
            logging.exception("Quiz: film %s failed", pick.get("id"))
            continue
        if not frames:
            continue
        genres = set(shorts_daily._genres(film))
        others = sorted((f for f in films if f["id"] != film["id"]),
                        key=lambda f: -len(genres & set(shorts_daily._genres(f))))[:3]
        if len(others) == 3:
            return str(film["id"]), shorts_daily._title(film), frames[0], [shorts_daily._title(f) for f in others]
    logging.warning("Quiz: kinopoisk gave %d films, none with a TMDB frame", len(films))
    return None


async def _quiz_from_tmdb(used: set[str], work: Path) -> tuple[str, str, Image.Image, list[str]] | None:
    """Запас: известные фильмы TMDB (рейтинг 7+, тысячи голосов), кадр без надписей — с TMDB."""
    try:
        pool = await shorts_daily.tmdb.popular_pool(TMDB_API_KEY, random.randint(1, 15))
    except Exception:  # noqa: BLE001
        logging.exception("Quiz: tmdb pool failed")
        return None
    random.shuffle(pool)
    for row in [r for r in pool if f"t{r['id']}" not in used][:6]:
        try:
            urls = await shorts_daily.tmdb.backdrops(TMDB_API_KEY, "movie", row["id"], 4)
            img = await _image(random.choice(urls), work, "qt") if urls else None
        except Exception:  # noqa: BLE001
            logging.exception("Quiz: tmdb film %s failed", row.get("id"))
            continue
        if not img:
            continue
        genres = set(row["genres"])
        others = sorted((r for r in pool if r["id"] != row["id"] and r["title"] != row["title"]),
                        key=lambda r: -len(genres & set(r["genres"])))[:3]
        if len(others) == 3:
            return f"t{row['id']}", row["title"], img, [r["title"] for r in others]
    return None


async def make_quiz_post(bot: Bot, chat_id: int) -> None:
    """«Угадай фильм по кадру»: кадр без надписей и викторина Telegram из 4 вариантов."""
    hist = await _posts_history()
    used = {str(x) for x in hist["quiz"]}
    work = SHORTS_DIR / f"quiz_{time.time_ns()}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        picked = await _quiz_from_kp(used, work)
        if not picked:  # Кинопоиск не ответил (лимит запросов) или у фильмов нет кадров — берём фильмы TMDB
            picked = await _quiz_from_tmdb(used, work)
        if not picked:
            raise RuntimeError("не нашлось кадра для викторины: Кинопоиск и TMDB не дали фильмов с кадрами "
                               "(лимит запросов или нет связи) — попробуй через пару минут")
        film_id, title, frame, others = picked
        names = [title[:100], *[o[:100] for o in others]]
        random.shuffle(names)
        correct = names.index(title[:100])
        frames = [frame]
        card = work / "quiz.jpg"
        (await asyncio.to_thread(shorts_postcard.make_quiz_card, frames[0])).save(card, quality=92)
        post = ("🎬 УГАДАЙ ФИЛЬМ ПО КАДРУ!\n\nУзнаёшь, из какого фильма этот кадр? 🤔 Голосуй в викторине ниже — "
                "правильный ответ откроется сразу после голоса 👇\n\n🔥 — угадал с первого взгляда ❤️ — сложно, но "
                "интересно\n📤 Перешли другу — проверь, угадает ли он\n\n#УгадайФильм #КиноВикторина #KinoMir360")
        ids, kind = await _send_album(bot, chat_id, [card], post, "#викторина")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    hist["quiz"] = (hist["quiz"] + [film_id])[-200:]
    await save_posts_history(hist)
    await offer_post(bot, chat_id, ids, kind, "pt:qz",
                     what=f"Викторина готова (правильный ответ — {escape(f"«{title}»")}). В канале под ней будет "
                          "викторина с 4 вариантами",
                     quiz={"options": names, "correct": correct, "explanation": f"Это «{title}» 🎬"[:200]})


async def make_quote_post(bot: Bot, chat_id: int) -> None:
    """«Цитата дня»: известная цитата из популярного фильма на фирменной карточке."""
    hist = await _posts_history()
    used = {int(x) for x in hist["quotes"]}
    film = next((f for f in await _popular_films(15) if f["id"] not in used), None)
    if not film:
        raise RuntimeError("не нашлось фильма — попробуй ещё раз")
    res = await shorts_ai.quote_post(GEMINI_API_KEY, _label(film) + (f", {film.get('enName')}" if film.get("enName") else ""))
    if not res:
        raise RuntimeError(shorts_ai.last_error or "ИИ не нашёл цитату")
    work = SHORTS_DIR / f"quote_{time.time_ns()}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        frames = await _frames(film, work, 1, "f") or [img for img in [await _image(
            shorts_daily._image_url(film), work, "p")] if img]
        cards = []
        if frames:
            cards.append(work / "quote.jpg")
            (await asyncio.to_thread(shorts_postcard.make_quote_card, frames[0], res["quote"], res["character"],
                                     _label(film))).save(cards[0], quality=92)
        ids, kind = await _send_album(bot, chat_id, cards, res["post"], "#цитата")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    hist["quotes"] = (hist["quotes"] + [str(film["id"])])[-200:]
    await save_posts_history(hist)
    await offer_post(bot, chat_id, ids, kind, "pt:qt", what="Цитата дня готова")


ANNIVERSARIES = (50, 40, 30, 25, 20, 15, 10)


class NothingToday(RuntimeError):
    """Сегодня для этого поста нет повода (например, круглых дат) — в автоплане молча пропускаем."""


async def make_anniversary_post(bot: Bot, chat_id: int) -> None:
    """«Этот день в кино»: самый известный фильм, вышедший в этот день круглое число лет назад."""
    today = datetime.now(MSK).date()
    hist = await _posts_history()
    best, best_years, best_votes, best_field = None, 0, 0, ""
    for years in ANNIVERSARIES:
        try:
            day = today.replace(year=today.year - years)
        except ValueError:  # 29 февраля
            continue
        for field in ("premiere.world", "premiere.russia"):
            for d in await shorts_daily._query(kp._get, sortField="votes.kp", sortType="-1", notNullFields="name",
                                               **{field: shorts_daily._range(day, day), "votes.kp": "50000-100000000"}):
                votes = int((d.get("votes") or {}).get("kp") or 0)
                if d.get("id") and f"{d['id']}:{today.year}" not in hist["anniv"] and votes > best_votes:
                    best, best_years, best_votes, best_field = d, years, votes, field
    if not best:
        raise NothingToday("сегодня нет круглых дат у известных фильмов")
    film = await kp._get(f"/movie/{best['id']}")
    material = _film_line(film) + "\n\nФакты с Кинопоиска:\n" + "\n".join(
        f"— {f}" for f in shorts_daily.usable_facts(film)[:8])
    years_word = shorts_daily._years(best_years)
    when = f"{today.day} {shorts_daily.MONTHS[today.month - 1].upper()} {today.year - best_years} ГОДА"
    where = "В РОССИЙСКИЙ ПРОКАТ" if best_field == "premiere.russia" else "НА ЭКРАНЫ"
    post = await shorts_ai.anniversary_post(
        GEMINI_API_KEY, material, best_years, f"{best_years} {years_word.upper()} НАЗАД, {when}, {where}") or "\n".join(
        [f"📅 {best_years} {years_word.upper()} НАЗАД ВЫШЕЛ {_label(film).upper()}!", "",
         str(film.get("shortDescription") or film.get("description") or "")[:300], "",
         "Пересматривали? 👇", "", "❤️ — классика 🔥 — пересмотрю сегодня", "", "#ЭтотДеньВКино #Кино #KinoMir360"])
    work = SHORTS_DIR / f"anniv_{time.time_ns()}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        cards: list[Path] = []
        poster = await _image(shorts_daily._poster_url(film), work, "poster")
        if poster:
            rating, label = shorts_daily._best_rating(film)
            cards.append(work / "card.jpg")
            (await asyncio.to_thread(
                shorts_postcard.make_post_card, poster, shorts_daily._title(film), pill=(f"{best_years} {years_word}", ""),
                meta=f"Премьера: {today.day} {shorts_daily.MONTHS[today.month - 1]} {today.year - best_years}",
                extra=(f"{label} {rating:.1f}" if rating else None))).save(cards[-1], quality=92)
        for k, frame in enumerate(await _frames(film, work, 2, "fr")):
            cards.append(work / f"frame{k}.jpg")
            (await asyncio.to_thread(shorts_postcard.watermark, frame)).save(cards[-1], quality=92)
        ids, kind = await _send_album(bot, chat_id, cards, post, "#этотденьвкино")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    hist["anniv"] = (hist["anniv"] + [f"{film['id']}:{today.year}"])[-200:]
    await save_posts_history(hist)
    await offer_post(bot, chat_id, ids, kind, what="Пост «Этот день в кино» готов")


async def make_box_office_post(bot: Bot, chat_id: int) -> None:
    """Сборы прошедшего уикенда (мир и Россия) — ИИ ищет свежие цифры в Google; постеры — карточками."""
    today = datetime.now(MSK).date()
    res = await shorts_ai.box_office_post(GEMINI_API_KEY, f"{today.day} {shorts_daily.MONTHS[today.month - 1]} {today.year}")
    if not res:
        raise RuntimeError("сборам нужен поиск Google через Gemini, а он сейчас не ответил"
                           + (f": {shorts_ai.last_error}" if shorts_ai.last_error else ""))
    found = [await _list_poster(f, [], []) for f in res["films"]]
    names = [f["ru"] or f["en"] for f in res["films"]]
    work = SHORTS_DIR / f"box_{time.time_ns()}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        cards = await _list_cards(work, [(n, url, film) for n, (url, film) in zip(names, found)],
                                  [f"Сборы уикенда: {f['gross']}" if f.get("gross") else "" for f in res["films"]])
        ids, kind = await _send_album(bot, chat_id, cards, res["post"], "#сборы")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    await offer_post(bot, chat_id, ids, kind, what="Пост «Сборы уикенда» готов")


async def make_week_post(bot: Bot, chat_id: int) -> None:
    """Итоги недели в кино: главные новости по постам Telegram-каналов за 7 дней, постеры — карточками."""
    today = datetime.now(MSK).date()
    own = POST_CHANNEL.lstrip("@").lower()
    channels = [c for c in await fact_channels() if c.lower() != own]
    posts = await shorts_tgsearch.latest(channels, hours=168, per_channel=12)
    try:
        mine = await shorts_tgsearch.latest([own], hours=168, per_channel=30)
    except Exception:  # noqa: BLE001
        mine = []
    res = await shorts_ai.week_digest_post(
        GEMINI_API_KEY, shorts_tgsearch.as_text(posts, 9000),
        "\n".join("— " + re.sub(r"\s+", " ", p["text"])[:160] for p in mine),
        f"{today.day} {shorts_daily.MONTHS[today.month - 1]} {today.year}")
    if not res:
        raise RuntimeError(shorts_ai.last_error or "ИИ не собрал итоги недели")
    found = [await _list_poster(f, [], posts) for f in res["films"]]
    names = [f["ru"] or f["en"] for f in res["films"]]
    work = SHORTS_DIR / f"week_{time.time_ns()}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        cards = await _list_cards(work, [(n, url, film) for n, (url, film) in zip(names, found)])
        ids, kind = await _send_album(bot, chat_id, cards, res["post"], "#итогинедели")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    await offer_post(bot, chat_id, ids, kind, what="Итоги недели готовы")


def _subs_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "подписчик"
    return "подписчика" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "подписчиков"


async def make_evening_post(bot: Bot, chat_id: int) -> None:
    """«🎬 Фильм вечера»: один отличный фильм — почему стоит посмотреть, трейлер со звуком с Rutube
    (или карточка), «Где смотреть» — ссылки на онлайн-кинотеатры."""
    hist = await _posts_history()
    used = {int(x) for x in hist["evening"]}
    pick = next((f for f in await _popular_films(20) if f["id"] not in used
                 and shorts_daily._best_rating(f)[0] >= 7.5), None)
    if not pick:
        raise RuntimeError("не нашлось фильма — попробуй ещё раз")
    film = await kp._get(f"/movie/{pick['id']}")
    title = shorts_daily._title(film)
    task = (f"Напиши пост «🎬 ФИЛЬМ ВЕЧЕРА» про «{title}» ({film.get('year') or ''}): заголовок КАПСОМ с "
            "эмодзи, о чём фильм в 2 предложениях без спойлеров, почему стоит посмотреть именно сегодня (2–3 "
            "пункта с эмодзи), кто играет, для какого настроения; в конце строка реакций (например «🍿 — смотрю "
            "сегодня ❤️ — уже видел»).")
    post = await shorts_ai.write_styled_post(GEMINI_API_KEY, task, _film_line(film), search=False) or "\n".join([
        f"🎬 ФИЛЬМ ВЕЧЕРА: {_label(film).upper()}", "",
        str(film.get("shortDescription") or film.get("description") or "")[:400], "",
        "🍿 — смотрю сегодня ❤️ — уже видел"])
    links = kp.watch_links(film)
    hist["evening"] = (hist["evening"] + [str(film["id"])])[-200:]
    await save_posts_history(hist)
    work = SHORTS_DIR / f"evening_{time.time_ns()}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        trailer = await _rutube_post_trailer([title, str(film.get("enName") or film.get("alternativeName") or "")], work)
        if trailer:
            sent = await bot.send_video(chat_id, FSInputFile(trailer), caption=post_caption(post, "#фильмвечера", links),
                                        supports_streaming=True)
            ids, kind = [sent.message_id], "caption"
        else:
            cards: list[Path] = []
            poster = await _image(shorts_daily._poster_url(film), work, "poster")
            if poster:
                rating, label = shorts_daily._best_rating(film)
                cards.append(work / "card.jpg")
                (await asyncio.to_thread(
                    shorts_postcard.make_post_card, poster, title, 0, 0, rating or None, label, film.get("year"),
                    shorts_daily._genres(film)[:2], shorts_daily._actors(film, 3))).save(cards[-1], quality=92)
            ids, kind = await _send_album(bot, chat_id, cards, post, "#фильмвечера", links)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    await offer_post(bot, chat_id, ids, kind, "pt:ev", what="Фильм вечера готов")


async def make_battle_post(bot: Bot, chat_id: int) -> None:
    """«⚔️ Битва фильмов»: два известных фильма одного жанра, карточка с VS и опрос «Какой лучше?»."""
    hist = await _posts_history()
    films = [f for f in await _popular_films(15) if shorts_daily._poster_url(f)]
    pairs = [(a, b) for i, a in enumerate(films) for b in films[i + 1:]
             if set(shorts_daily._genres(a)) & set(shorts_daily._genres(b))
             and f"{min(a['id'], b['id'])}:{max(a['id'], b['id'])}" not in hist["battle"]]
    if not pairs:
        raise RuntimeError("не нашлось пары фильмов — попробуй ещё раз")
    a, b = random.choice(pairs[:10])
    work = SHORTS_DIR / f"battle_{time.time_ns()}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        pa = await _image(shorts_daily._poster_url(a), work, "a")
        pb = await _image(shorts_daily._poster_url(b), work, "b")
        if not pa or not pb:
            raise RuntimeError("не скачались постеры — попробуй ещё раз")
        card = work / "battle.jpg"
        (await asyncio.to_thread(shorts_postcard.make_battle_card, pa, pb, shorts_daily._title(a),
                                 shorts_daily._title(b))).save(card, quality=92)
        post = "\n".join([
            f"⚔️ БИТВА ФИЛЬМОВ: {_label(a)} или {_label(b)}?", "",
            f"🅰️ {_label(a)} — " + (str(a.get("shortDescription") or "").strip() or "классика жанра"),
            f"🅱️ {_label(b)} — " + (str(b.get("shortDescription") or "").strip() or "классика жанра"), "",
            "Голосуй в опросе ниже и защищай свой выбор в комментариях 👇", "",
            "📤 Перешли другу — узнай, на чьей он стороне"])
        ids, kind = await _send_album(bot, chat_id, [card], post, "#битвафильмов")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    hist["battle"] = (hist["battle"] + [f"{min(a['id'], b['id'])}:{max(a['id'], b['id'])}"])[-200:]
    await save_posts_history(hist)
    await offer_post(bot, chat_id, ids, kind, "pt:bt", [shorts_daily._title(a), shorts_daily._title(b)],
                     "Битва фильмов готова", "Какой фильм лучше? ⚔️")


async def make_vote_post(bot: Bot, chat_id: int) -> None:
    """«🗳 Выбор подписчиков»: 4 фильма и опрос «О каком фильме рассказать факты?»; в пятницу — итог."""
    films = [f for f in await _popular_films(20) if shorts_daily._poster_url(f)][:4]
    if len(films) < 4:
        raise RuntimeError("мало фильмов — попробуй ещё раз")
    titles = [shorts_daily._title(f)[:100] for f in films]
    post = "\n".join([
        "🗳 ВЫБОР ПОДПИСЧИКОВ!", "", "О каком фильме рассказать интересные факты в пятницу? Решаете вы 👇", "",
        *[f"{n} {_label(f)}" for n, f in zip(["1️⃣", "2️⃣", "3️⃣", "4️⃣"], films)], "",
        "Голосуй в опросе — в пятницу пост о победителе 🔥"])
    work = SHORTS_DIR / f"vote_{time.time_ns()}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        cards = await _list_cards(work, [(shorts_daily._title(f), shorts_daily._poster_url(f), f) for f in films])
        ids, kind = await _send_album(bot, chat_id, cards, post, "#выборподписчиков")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    await offer_post(bot, chat_id, ids, kind, "pt:vt", what="Голосование готово. После публикации под ним будет "
                     f"опрос «{VOTE_QUESTION}», в пятницу бот подведёт итог",
                     extra={"vote": {"ids": [int(f["id"]) for f in films], "titles": titles}})


async def make_vote_result_post(bot: Bot, chat_id: int) -> None:
    """Итог «Выбора подписчиков»: закрываем опрос в канале и делаем факты о победителе."""
    vote = await _json_setting("vote_poll", {})
    if not vote:
        raise NothingToday("в канале нет опроса «Выбор подписчиков» — сначала опубликуй голосование")
    await set_shorts_setting("vote_poll", None)
    try:
        poll = await bot.stop_poll(POST_CHANNEL, vote["message_id"])
        counts = [o.voter_count for o in poll.options]
    except Exception as exc:  # noqa: BLE001 — опрос уже закрыт или удалён — берём первый вариант
        logging.info("Vote poll stop failed: %s", exc)
        counts = [0] * len(vote["ids"])
    winner = vote["ids"][max(range(len(counts)), key=lambda i: counts[i])] if counts else vote["ids"][0]
    await make_facts_post(bot, chat_id, film_id=winner, voted=True)


async def make_bloopers_post(bot: Bot, chat_id: int) -> None:
    """«🎭 Ляпы в кино»: известный фильм с 3+ ляпами Кинопоиска, пересказ своими словами, кадры со знаком."""
    hist = await _posts_history()
    used = {int(x) for x in hist["bloopers"]}
    film, bloopers = None, []
    for pick in [f for f in await _popular_films(15) if f["id"] not in used][:8]:
        full = await kp._get(f"/movie/{pick['id']}")
        bloopers = [shorts_daily._clean_fact(f.get("value")) for f in full.get("facts") or []
                    if isinstance(f, dict) and str(f.get("type") or "").upper() == "BLOOPER" and not f.get("spoiler")]
        bloopers = [b for b in bloopers if 30 <= len(b) <= 400]
        if len(bloopers) >= 3:
            film = full
            break
    if not film:
        raise RuntimeError("не нашлось фильма с ляпами — попробуй ещё раз")
    title = shorts_daily._title(film)
    task = (f"Напиши пост «🎭 Ляпы в «{title}» ({film.get('year') or ''}), которые вы не замечали»: заголовок "
            "КАПСОМ с эмодзи, 3–5 ляпов из материала строками «1️⃣ …» — своими словами, коротко и с юмором; в конце "
            "вопрос «А вы замечали?» и строка реакций.")
    post = await shorts_ai.write_styled_post(GEMINI_API_KEY, task, _film_line(film) + "\n\nЛяпы:\n" + "\n".join(
        f"— {b}" for b in bloopers[:8]), search=False) or "\n".join(
        [f"🎭 ЛЯПЫ В {_label(film).upper()}", ""] + [f"{n} {b}" for n, b in zip(["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"],
                                                                               bloopers)]
        + ["", "А вы замечали? 👇", "", "🔥 — замечал ❤️ — теперь пересмотрю"])
    hist["bloopers"] = (hist["bloopers"] + [str(film["id"])])[-200:]
    await save_posts_history(hist)
    work = SHORTS_DIR / f"bloopers_{time.time_ns()}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        cards: list[Path] = []
        poster = await _image(shorts_daily._poster_url(film), work, "poster")
        if poster:
            rating, label = shorts_daily._best_rating(film)
            cards.append(work / "card.jpg")
            (await asyncio.to_thread(shorts_postcard.make_post_card, poster, title, pill=("ЛЯПЫ", ""),
                                     meta=" · ".join(x for x in [str(film.get("year") or ""),
                                                                 ", ".join(shorts_daily._genres(film)[:2])] if x),
                                     extra=(f"{label} {rating:.1f}" if rating else None))).save(cards[-1], quality=92)
        for k, frame in enumerate(await _frames(film, work, 2, "fr")):
            cards.append(work / f"frame{k}.jpg")
            (await asyncio.to_thread(shorts_postcard.watermark, frame)).save(cards[-1], quality=92)
        ids, kind = await _send_album(bot, chat_id, cards, post, "#ляпы")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    await offer_post(bot, chat_id, ids, kind, "pt:bl", what="Пост с ляпами готов")


BOT_USERNAME = ""  # имя бота: для ссылок «Поделиться» (запоминаем при первом запросе)


async def _bot_link(bot: Bot) -> str:
    global BOT_USERNAME
    if not BOT_USERNAME:
        BOT_USERNAME = (await bot.get_me()).username or ""
    return f"https://t.me/{BOT_USERNAME}"


async def _card_post(bot: Bot, chat_id: int, tagline: str, link: str, post: str, rubric: str = "") -> list[int]:
    """Пост с рекламной картинкой канала (логотип, свежие постеры) и своей строкой-призывом."""
    today = datetime.now(MSK).date()
    films = await shorts_daily.pick_films(kp._get, shorts_daily.theme_for("online", today), today, set())
    if len(films) < 4:
        films += await _popular_films(3)
    work = SHORTS_DIR / f"card_{time.time_ns()}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        posters = [img for img in [await _image(shorts_daily._poster_url(f), work, f"p{k}_")
                                   for k, f in enumerate(films[:4])] if img]
        card = work / "promo.jpg"
        (await asyncio.to_thread(shorts_postcard.make_promo_card, posters, tagline, link)).save(card, quality=92)
        sent = await bot.send_photo(chat_id, FSInputFile(card), caption=post_caption(post, rubric))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return [sent.message_id]


async def make_bot_post(bot: Bot, chat_id: int) -> None:
    """«🤖 Подбери фильм в нашем боте»: пост с кнопкой-ссылкой на бота VKino."""
    url = await _bot_link(bot)
    post = "\n".join([
        "🤖 НЕ ЗНАЕШЬ, ЧТО ПОСМОТРЕТЬ? СПРОСИ НАШЕГО БОТА!", "",
        "В боте KinoMir360 за пару секунд:", "🔎 найдёшь любой фильм или сериал", "🆕 посмотришь новинки и "
        "что скоро выйдет", "🎲 получишь случайный фильм на вечер", "⭐ сохранишь фильмы в избранное", "",
        "Жми кнопку под постом 👇", "", "📤 Перешли другу, который вечно не знает, что посмотреть"])
    ids = await _card_post(bot, chat_id, "Бот подберёт фильм на вечер", url.replace("https://", ""), post)
    await offer_post(bot, chat_id, ids, "caption", "pt:ab", what="Пост про бота готов (в канале под ним будет "
                     "кнопка «🎬 Подобрать фильм»)", extra={"button": {"text": "🎬 Подобрать фильм", "url": url}})


async def make_pick_post(bot: Bot, chat_id: int) -> None:
    """«🎯 Рекомендация: что посмотреть»: пост с кнопкой — бот задаёт 4 вопроса и советует фильм или сериал."""
    url = f"{await _bot_link(bot)}?start=pick"
    post = "\n".join([
        "🎯 РЕКОМЕНДАЦИЯ: ЧТО ПОСМОТРЕТЬ?", "",
        "Не знаешь, что включить вечером? Ответь боту на 4 вопроса:", "",
        "🎬 Фильм, сериал, мультфильм или аниме?", "🎭 Какой жанр любишь?",
        "🌍 Какая страна: Россия, США, Корея, Англия, Франция…?", "🆕 Новинка или классика?", "",
        "…и получи персональную рекомендацию с рейтингом и описанием. Не зашло — жми «Ещё вариант» 🔄", "",
        "Жми кнопку под постом 👇", "",
        "📤 Перешли другу, который вечно выбирает фильм дольше, чем смотрит",
        "", "#чтопосмотреть #рекомендация #кино"])
    ids = await _card_post(bot, chat_id, "4 вопроса — и бот подберёт фильм", url.replace("https://", "").split("?")[0], post)
    await offer_post(bot, chat_id, ids, "caption", "pt:rq", what="Пост «Рекомендация: что посмотреть» готов (в "
                     "канале под ним будет кнопка «🎯 Подобрать мне фильм»)",
                     extra={"button": {"text": "🎯 Подобрать мне фильм", "url": url}})


async def make_boost_post(bot: Bot, chat_id: int) -> None:
    """«⚡ Буст канала»: просьба подписчиков забустить канал — откроются истории и новые реакции."""
    own = POST_CHANNEL.lstrip("@")
    url = f"https://t.me/boost/{own}"
    post = "\n".join([
        "⚡ ПОДДЕРЖИ KINOMIR360 БУСТОМ!", "",
        "Если у тебя Telegram Premium — у тебя есть бесплатные бусты. Отдай один нашему каналу: так мы откроем "
        "истории с трейлерами и новые реакции 🔥", "", "Это бесплатно и займёт 2 секунды — кнопка под постом 👇", "",
        "❤️ Спасибо, что вы с нами!"])
    ids = await _card_post(bot, chat_id, "Поддержи канал бустом", f"t.me/boost/{own}", post)
    await offer_post(bot, chat_id, ids, "caption", what="Пост про буст готов (в канале под ним будет кнопка "
                     "«⚡ Забустить»)", extra={"button": {"text": "⚡ Забустить канал", "url": url}})


async def make_support_post(bot: Bot, chat_id: int) -> None:
    """«🤝 Обращение к подписчикам»: тёплая просьба быть активнее (реакции, комментарии, опросы) и делиться
    каналом с друзьями, родными и знакомыми; под постом в канале — кнопка «Поделиться каналом». Текст каждый
    раз новый (ИИ), чтобы повторное обращение не выглядело копией."""
    own = POST_CHANNEL.lstrip("@")
    share = ("https://t.me/share/url?url=" + quote(f"https://t.me/{own}")
             + "&text=" + quote("Канал про кино: новинки, трейлеры и что посмотреть вечером 🍿"))
    try:
        members = await bot.get_chat_member_count(POST_CHANNEL)
    except Exception:  # noqa: BLE001 — бот не админ канала: без числа подписчиков
        members = 0
    material = (f"Канал KinoMir360 (t.me/{own}) ведёт один автор — сам ищет новости, делает подборки, ролики и "
                "выпуски «Новости кино» на YouTube." + (f" Подписчиков сейчас: {members}." if members else "")
                + " Активность подписчиков (реакции, комментарии, ответы в опросах, репосты) помогает каналу: "
                "Telegram чаще показывает активные каналы, а автору важно видеть, что его работа нужна, — это "
                "мотивирует стараться ещё больше.")
    task = ("Напиши от лица автора искреннее, тёплое обращение к подписчикам (не реклама, без пафоса и без "
            "давления). Заголовок КАПСОМ с эмодзи. Поблагодари, что они с каналом. Коротко объясни, почему "
            "активность важна. Попроси: ставить реакции, писать в комментариях, что думают о фильмах, отвечать в "
            "опросах, предлагать темы — и, если канал нравится, поделиться им с друзьями, родными и знакомыми, "
            "которые любят кино (кнопка под постом). Закончи благодарностью. 600–900 знаков. Без строки реакций "
            "и без хэштегов.")
    post = await shorts_ai.write_styled_post(GEMINI_API_KEY, task, material, search=False) or "\n".join([
        "❤️ СПАСИБО, ЧТО ВЫ С НАМИ!", "",
        "KinoMir360 — это каждый день новости, трейлеры и подборки, которые я собираю для вас. И мне очень важно "
        "знать, что это кому-то нужно 🙏", "",
        "Как поддержать канал — бесплатно и за пару секунд:",
        "🔥 ставьте реакции на посты, которые понравились;", "💬 пишите в комментариях, что думаете о фильмах;",
        "📊 отвечайте в опросах и предлагайте темы;",
        "📤 поделитесь каналом с друзьями, родными и знакомыми, которые любят кино — кнопка под постом.", "",
        "Telegram чаще показывает активные каналы — так о нас узнает больше людей, а у меня будет ещё больше "
        "сил стараться для вас 🎬", "", "Спасибо, что вы здесь ❤️"])
    ids = await _card_post(bot, chat_id, "Спасибо, что вы с нами!", f"t.me/{own}", post)
    await offer_post(bot, chat_id, ids, "caption", "pt:sp",
                     what="Обращение готово (в канале под ним будет кнопка «📤 Поделиться каналом»)",
                     extra={"button": {"text": "📤 Поделиться каналом с друзьями", "url": share}})


async def make_navigator_post(bot: Bot, chat_id: int) -> None:
    """«📌 Закреп-навигатор»: что есть в канале — рубрики-хэштеги, бот, YouTube и MAX; закрепить в канале."""
    url = await _bot_link(bot)
    post = "\n".join([
        "📌 ДОБРО ПОЖАЛОВАТЬ В KINOMIR360!", "", "Всё о кино каждый день. Жми на хэштег — увидишь все посты рубрики:",
        "", *[f"{name} — {tag}" for tag, name in RUBRICS.items()], "",
        f"🤖 Подобрать фильм: {url.replace('https://', '')}", f"📺 YouTube: {shorts_daily.YOUTUBE_URL.replace('https://', '')}",
        f"💬 Мы в MAX: {MAX_URL.replace('https://', '')}",
        f"✉️ Связь с админом: {url.replace('https://', '')}?start=contact"])
    ids = await _card_post(bot, chat_id, "Всё о кино каждый день", f"t.me/{POST_CHANNEL.lstrip('@')}", post)
    await offer_post(bot, chat_id, ids, "caption", what="Навигатор готов. После публикации закрепи его в канале "
                     "(долгий тап по посту → Закрепить)", extra={"button": {"text": "🎬 Подобрать фильм", "url": url}})


async def make_giveaway_post(bot: Bot, chat_id: int) -> None:
    """«🎁 Розыгрыш»: шаблон совместного конкурса с каналами-партнёрами; приз и партнёров автор вписывает
    кнопкой «✏️ Поправить текст»."""
    own = POST_CHANNEL.lstrip("@")
    post = "\n".join([
        "🎁 РОЗЫГРЫШ! [ПРИЗ — например, 3 подписки на онлайн-кинотеатр на месяц]", "",
        "Условия простые:", f"1️⃣ Подпишись на KinoMir360 — t.me/{own}", "2️⃣ Подпишись на каналы-партнёры:",
        "[@канал1, @канал2, @канал3]", "3️⃣ Нажми «Участвую» под постом (или поставь ❤️)", "",
        "📅 Итоги — [ДАТА] в [ВРЕМЯ] по МСК. Победителей выберем случайно 🍀", "",
        "📤 Перешли другу — вдруг повезёт именно ему!"])
    ids = await _card_post(bot, chat_id, "РОЗЫГРЫШ для подписчиков", f"t.me/{own}", post)
    await offer_post(bot, chat_id, ids, "caption", what=(
        "Шаблон розыгрыша готов. Нажми «✏️ Поправить текст» и впиши приз, каналы-партнёры и дату. Победителей "
        "удобно выбрать встроенным розыгрышем Telegram или ботом @GiveShareBot — он проверит подписку на все "
        "каналы"))


async def make_article(bot: Bot, chat_id: int) -> None:
    """«📰 Статья для Дзен/VK»: длинная подборка (3–4,5 тыс. знаков) файлом и текстом + карточки-постеры."""
    today = datetime.now(MSK).date()
    key = random.choice(["fresh", "online", "topfilms", "topseries", *[f"genre:{g}" for g in SHORTS_GENRES]])
    theme = shorts_daily.theme_for(key, today)
    kp_films = await shorts_daily.pick_films(kp._get, theme, today, set())
    res = await shorts_ai.long_article(GEMINI_API_KEY, theme.hook, [_film_line(f) for f in kp_films],
                                       f"{today.day} {shorts_daily.MONTHS[today.month - 1]} {today.year}")
    if not res:
        raise RuntimeError(shorts_ai.last_error or "ИИ не написал статью")
    text = f"{res['title']}\n\n{res['text']}\n\nПодписывайтесь на KinoMir360 в Telegram: t.me/{POST_CHANNEL.lstrip('@')}"
    await bot.send_document(chat_id, BufferedInputFile(text.encode("utf-8"), filename="statya_kinomir360.txt"),
                            caption="📰 Статья для Дзена и VK — файл целиком. Ниже текст частями и картинки к ней.")
    for k in range(0, len(text), 3800):
        await bot.send_message(chat_id, escape(text[k:k + 3800]), disable_web_page_preview=True)
    work = SHORTS_DIR / f"article_{time.time_ns()}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        found = [await _list_poster(f, kp_films, []) for f in res["films"]]
        cards = await _list_cards(work, [(f["ru"] or f["en"], url, film) for f, (url, film) in zip(res["films"], found)])
        if len(cards) >= 2:
            await bot.send_media_group(chat_id, [InputMediaPhoto(media=FSInputFile(p)) for p in cards[:10]])
    finally:
        shutil.rmtree(work, ignore_errors=True)
    await bot.send_message(chat_id, "👆 Статья готова: скопируй текст в Дзен (dzen.ru → Создать → Статья) или "
                           "VK (Статьи), картинки — к разделам о фильмах. Яндекс приводит из поиска читателей месяцами.",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("🔄 Другая статья", "pt:ar")]]))


async def make_promo_post(bot: Bot, chat_id: int) -> None:
    """«📣 Взаимный пиар»: рекламный пост канала (картинка с логотипом и свежими постерами) — переслать
    партнёрам, и готовое сообщение админам других каналов с предложением ВП."""
    today = datetime.now(MSK).date()
    films = await shorts_daily.pick_films(kp._get, shorts_daily.theme_for("online", today), today, set())
    if len(films) < 4:
        films += await _popular_films(3)
    own = POST_CHANNEL.lstrip("@")
    try:
        members = await bot.get_chat_member_count(POST_CHANNEL)
    except Exception:  # noqa: BLE001 — бот не админ канала: без числа подписчиков
        members = 0
    names = ", ".join(_label(f) for f in films[:4])
    material = (f"Канал: KinoMir360 (t.me/{own}), YouTube: {shorts_daily.YOUTUBE_URL}, MAX: {MAX_URL}.\n"
                "Что в канале каждый день: свежие новости кино и трейлеры, подборки «что посмотреть», новинки "
                "онлайн, факты о фильмах, дни рождения звёзд, викторины «Угадай фильм по кадру», цитаты, сборы, "
                f"итоги недели. Сейчас в канале, например: {names}.")
    task = ("Напиши рекламный пост этого канала для взаимного пиара в чужом кино-канале: цепляющий заголовок "
            "КАПСОМ, 2–3 коротких абзаца, почему стоит подписаться (польза для зрителя, не хвастовство), список "
            "рубрик с эмодзи, призыв подписаться и ссылка t.me/" + own + ". Без строки реакций.")
    post = await shorts_ai.write_styled_post(GEMINI_API_KEY, task, material, search=False) or "\n".join([
        "🎬 НЕ ЗНАЕШЬ, ЧТО ПОСМОТРЕТЬ ВЕЧЕРОМ?", "",
        "В KinoMir360 каждый день — свежие новинки кино и сериалов, трейлеры и подборки на любое настроение 🍿", "",
        "🔥 Новости и трейлеры", "🆕 Что вышло онлайн", "💡 Факты, которых ты не знал", "🧩 Викторины «Угадай фильм»",
        "🎂 Дни рождения звёзд", "", f"Подписывайся 👉 t.me/{own}"])
    work = SHORTS_DIR / f"promo_{time.time_ns()}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        posters = [img for img in [await _image(shorts_daily._poster_url(f), work, f"p{k}_")
                                   for k, f in enumerate(films[:4])] if img]
        card = work / "promo.jpg"
        (await asyncio.to_thread(shorts_postcard.make_promo_card, posters, link=f"t.me/{own}")).save(card, quality=92)
        await bot.send_photo(chat_id, FSInputFile(card), caption=post_caption(post), reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="✅ Подписаться на KinoMir360", url=f"https://t.me/{own}")]]))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    offer = (f"Здравствуйте! Я админ кино-канала KinoMir360 — t.me/{own}"
             + (f" ({members} {_subs_word(members)})" if members else "") + ". Каждый день новинки, трейлеры, подборки и "
             "викторины о кино.\n\nПредлагаю взаимный пиар: ваш пост у нас и наш у вас, формат 1/24 (час в топе, "
             "сутки в ленте), время согласуем. Рекламный пост пришлю. Интересно?")
    await bot.send_message(
        chat_id, "👆 Рекламный пост с кнопкой «✅ Подписаться» — перешли его админу канала-партнёра (кнопка сохранится при пересылке).\n\n✉️ Сообщение админу (нажми, чтобы "
        f"скопировать):\n<code>{escape(offer)}</code>\n\n💡 Партнёров ищи в поиске Telegram и на tgstat.ru → "
        "«Кино»: каналы примерно твоего размера (±50% подписчиков), с живыми просмотрами.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("🔄 Другой вариант", "pt:pr")],
                                                           [_btn("📝 Другие посты", "pt:menu")]]))


# подборки по стилю: код → (кнопка, тема поста, кандидаты Кинопоиска). Фильмы выбирает ИИ по теме — из
# кандидатов, своих знаний и поиска Google; Кинопоиск даёт постеры и рейтинги.
STYLE_LISTS: dict[str, tuple[str, str, dict[str, str]]] = {
    "lbh": ("💀 Малобюджетные хорроры", "Малобюджетные хорроры, которые напугали весь мир",
            {"genres.name": "ужасы", "rating.kp": "6.3-10", "votes.kp": "15000-100000000"}),
    "hor": ("🩸 Хорроры", "Хорроры, после которых не уснёшь",
            {"genres.name": "ужасы", "rating.kp": "6.8-10", "votes.kp": "30000-100000000"}),
    "lbf": ("💎 Малобюджетные хиты", "Малобюджетные фильмы, от которых зрители без ума",
            {"rating.kp": "7.3-10", "votes.kp": "20000-100000000"}),
    "twi": ("🤯 Неожиданная концовка", "Фильмы с концовкой, которую никто не ожидал",
            {"genres.name": "триллер", "rating.kp": "7.3-10", "votes.kp": "50000-100000000"}),
    "und": ("🕵️ Недооценённые", "Недооценённые фильмы, которые вы могли пропустить",
            {"rating.kp": "7.2-10", "votes.kp": "5000-60000"}),
    "tru": ("📜 По реальным событиям", "Фильмы, основанные на реальных событиях",
            {"genres.name": "биография", "rating.kp": "7.2-10", "votes.kp": "20000-100000000"}),
    "brn": ("🧠 Взрыв мозга", "Фильмы, которые взрывают мозг",
            {"genres.name": "фантастика", "rating.kp": "7.5-10", "votes.kp": "50000-100000000"}),
    "psy": ("🔪 Психологические триллеры", "Психологические триллеры, которые держат до последней минуты",
            {"genres.name": "триллер", "rating.kp": "7-10", "votes.kp": "30000-100000000"}),
    "com": ("😂 Комедии на вечер", "Комедии, которые точно поднимут настроение",
            {"genres.name": "комедия", "rating.kp": "7-10", "votes.kp": "50000-100000000"}),
    "asi": ("🏯 Азиатские хорроры", "Азиатские хорроры, от которых стынет кровь",
            {"genres.name": "ужасы", "countries.name": "Южная Корея", "rating.kp": "6.3-10"}),
    "kor": ("🇰🇷 Корейское кино", "Корейские фильмы, которые стоит посмотреть каждому",
            {"countries.name": "Южная Корея", "rating.kp": "7.2-10", "votes.kp": "10000-100000000"}),
    "min": ("📺 Мини-сериалы на выходные", "Мини-сериалы, которые можно посмотреть за выходные",
            {"isSeries": "true", "rating.kp": "7.5-10", "votes.kp": "20000-100000000"}),
    "fam": ("👨‍👩‍👧 Для всей семьи", "Фильмы для всей семьи",
            {"genres.name": "семейный", "rating.kp": "7-10", "votes.kp": "30000-100000000"}),
    "ani": ("🎌 Аниме", "Аниме, которое стоит посмотреть даже тем, кто не смотрит аниме",
            {"type": "anime", "rating.kp": "7.5-10", "votes.kp": "10000-100000000"}),
    "s90": ("📼 Культовое кино 90-х", "Культовые фильмы 90-х, которые пора пересмотреть",
            {"year": "1990-1999", "rating.kp": "7.5-10", "votes.kp": "100000-100000000"}),
    "spc": ("🚀 Про космос", "Лучшие фильмы про космос",
            {"genres.name": "фантастика", "rating.kp": "7-10", "votes.kp": "50000-100000000"}),
    "sur": ("🏝 Про выживание", "Фильмы про выживание, от которых не оторваться",
            {"genres.name": "приключения", "rating.kp": "7-10", "votes.kp": "50000-100000000"}),
    "one": ("🏠 Одна локация", "Фильмы, снятые почти в одной локации, но держащие в напряжении",
            {"rating.kp": "7-10", "votes.kp": "20000-100000000"}),
}


def style_menu() -> InlineKeyboardMarkup:
    keys = list(STYLE_LISTS)
    rows = [[_btn(STYLE_LISTS[k][0], f"pt:l:x:{k}") for k in keys[i:i + 2]] for i in range(0, len(keys), 2)]
    rows.append([_btn("⬅️ Назад", "pt:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# праздники: ключ → (кнопка, месяц, день, дней до, дней после, заголовок, фильтр Кинопоиска, подсказка ИИ)
HOLIDAYS: dict[str, tuple[str, int, int, int, int, str, dict[str, str], str]] = {
    "halloween": ("🎃 Хэллоуин", 10, 31, 10, 1, "Фильмы на Хэллоуин 🎃",
                  {"genres.name": "ужасы", "rating.kp": "6.8-10", "votes.kp": "50000-100000000"},
                  "ТОЛЬКО фильмы, где Хэллоуин — место действия или сама тема: события 31 октября, тыквы-фонари, "
                  "костюмы, «сладость или гадость», ведьмы и праздник мёртвых — или признанная хэллоуинская классика "
                  "(«Хэллоуин», «Кошелёк или жизнь», «Битлджус», «Кошмар перед Рождеством», «Фокус-покус», "
                  "«Сонная лощина», «Семейка Аддамс»). Просто страшные фильмы без Хэллоуина НЕ бери"),
    "newyear": ("🎄 Новый год", 12, 31, 21, 8, "Новогодние фильмы, которые создают настроение 🎄",
                {"genres.name": "комедия", "rating.kp": "7-10", "votes.kp": "50000-100000000"},
                "ТОЛЬКО фильмы, где действие происходит на Новый год или Рождество и праздник — часть сюжета: и "
                "зарубежная классика («Один дома», «Реальная любовь», «Гринч»), и наша («Ирония судьбы», «Чародеи», "
                "«Ёлки»). Просто комедии без праздника НЕ бери"),
    "valentine": ("💘 День святого Валентина", 2, 14, 7, 0, "Фильмы о любви ко Дню святого Валентина 💘",
                  {"genres.name": "мелодрама", "rating.kp": "7-10", "votes.kp": "50000-100000000"},
                  "романтичные фильмы о любви для вечера вдвоём"),
    "feb23": ("🎖 23 февраля", 2, 23, 5, 0, "Фильмы к 23 февраля 🎖",
              {"genres.name": "военный", "rating.kp": "7.3-10", "votes.kp": "50000-100000000"},
              "мужское кино ко Дню защитника Отечества: военные фильмы и боевики о героях"),
    "march8": ("🌷 8 Марта", 3, 8, 5, 0, "Фильмы к 8 Марта 🌷",
               {"genres.name": "мелодрама", "rating.kp": "7-10", "votes.kp": "40000-100000000"},
               "фильмы к Международному женскому дню: о сильных женщинах, романтические комедии, кино для девичника"),
    "april1": ("🤡 1 апреля", 4, 1, 3, 0, "Самые смешные комедии ко Дню смеха 🤡",
               {"genres.name": "комедия", "rating.kp": "7.2-10", "votes.kp": "80000-100000000"},
               "самые смешные комедии ко Дню смеха"),
    "space": ("🚀 День космонавтики", 4, 12, 3, 0, "Фильмы о космосе ко Дню космонавтики 🚀",
              {"genres.name": "фантастика", "rating.kp": "7.2-10", "votes.kp": "50000-100000000"},
              "фильмы о космосе и космонавтах: «Время первых», «Салют-7», «Интерстеллар», «Марсианин», «Аполлон-13»"),
    "may9": ("🎗 9 Мая", 5, 9, 7, 0, "Фильмы о Великой Отечественной войне ко Дню Победы 🎗",
             {"genres.name": "военный", "countries.name": "СССР", "rating.kp": "7.5-10", "votes.kp": "10000-100000000"},
             "лучшие фильмы о Великой Отечественной войне ко Дню Победы — советская классика и новые фильмы"),
    "kids": ("🧸 День защиты детей", 6, 1, 3, 0, "Фильмы и мультфильмы для всей семьи ко Дню защиты детей 🧸",
             {"genres.name": "семейный", "rating.kp": "7.2-10", "votes.kp": "50000-100000000"},
             "добрые семейные фильмы и мультфильмы для детей и родителей"),
    "school": ("🎒 1 сентября", 9, 1, 5, 0, "Фильмы про школу к 1 сентября 🎒",
               {"genres.name": "драма", "rating.kp": "7.2-10", "votes.kp": "30000-100000000"},
               "фильмы про школу, учителей и школьную жизнь: «Общество мёртвых поэтов», «Чучело», «Школа рока»"),
}


# фильмы, которые точно про праздник, — первыми кандидатами (по жанру Кинопоиск отдаёт просто «ужасы» и т. п.)
HOLIDAY_SEEDS: dict[str, list[str]] = {
    "halloween": ["«Хэллоуин» (1978), слэшер Джона Карпентера: маньяк Майкл Майерс в ночь Хэллоуина",
                  "«Кошелёк или жизнь» (Trick 'r Treat, 2007): четыре истории одной ночи Хэллоуина",
                  "«Битлджус» (1988), Тим Бёртон: призраки и биоэкзорцист",
                  "«Кошмар перед Рождеством» (1993): Джек Скеллингтон, король Хэллоуина",
                  "«Фокус-покус» (1993): три ведьмы-сестры Сандерсон воскресают в ночь Хэллоуина",
                  "«Сонная лощина» (1999), Тим Бёртон: всадник без головы",
                  "«Семейка Аддамс» (1991)",
                  "«Ужастики» (Goosebumps, 2015): монстры из книг вырываются на свободу перед Хэллоуином",
                  "«Хэллоуин» (2018): возвращение Майкла Майерса",
                  "«Коралина в Стране Кошмаров» (2009)",
                  "«Каспер» (1995): дружелюбное привидение",
                  "«Тыквоголовый» (1988)"],
    "newyear": ["«Один дома» (1990)", "«Ирония судьбы, или С лёгким паром!» (1975)", "«Реальная любовь» (2003)",
                "«Гринч — похититель Рождества» (2000)", "«Чародеи» (1982)", "«Ёлки» (2010)",
                "«Эльф» (2003)", "«Отпуск по обмену» (2006)", "«Карнавальная ночь» (1956)",
                "«Полярный экспресс» (2004)", "«Санта Клаус» (1994)", "«Крепкий орешек» (1988), действие в сочельник"],
}


def holidays_now(today: date) -> list[str]:
    """Праздники, к которым сейчас уместна подборка (за несколько дней до и в сам день)."""
    out = []
    for key, (_, month, day, before, after, *_rest) in HOLIDAYS.items():
        for year in (today.year - 1, today.year, today.year + 1):
            try:
                d = date(year, month, day)
            except ValueError:
                continue
            if d - timedelta(days=before) <= today <= d + timedelta(days=after):
                out.append(key)
                break
    return out


def holidays_menu(prefix: str, back: str) -> InlineKeyboardMarkup:
    keys = list(HOLIDAYS)
    rows = [[_btn(HOLIDAYS[k][0], f"{prefix}{k}") for k in keys[i:i + 2]] for i in range(0, len(keys), 2)]
    rows.append([_btn("⬅️ Назад", back)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def holiday_pick(key: str) -> tuple[str, list[dict[str, Any]], str]:
    """Заголовок, кандидаты Кинопоиска и подсказка для подборки к празднику."""
    _, _, _, _, _, title, params, hint = HOLIDAYS[key]
    kp_films = await _style_candidates(params, set())
    return title, kp_films, hint


async def _style_candidates(params: dict[str, str], exclude: set[int]) -> list[dict[str, Any]]:
    docs = await shorts_daily._query(kp._get, page=random.randint(1, 3), sortField="votes.kp", sortType="-1",
                                     notNullFields="name", **params)
    random.shuffle(docs)
    return [d for d in docs if d.get("id") and d["id"] not in exclude and shorts_daily._poster_url(d)][:10]


FRESH_LISTS = {"week", "fresh", "online", "cinema", "films", "series", "weekfilms", "weekseries", "upcoming"}


async def _tmdb_week_lines(series: bool, today: date) -> tuple[list[str], dict[str, tuple[str, str]]]:
    """Кандидаты «за неделю» с TMDB, от самых популярных: у сериалов — какая серия какого сезона и когда вышла
    (на Кинопоиске есть только дата первого сезона, свежих серий идущих сериалов там не видно)."""
    since, until = (today - timedelta(days=10)).isoformat(), today.isoformat()
    try:
        found = await (shorts_daily.tmdb.tv_aired if series else shorts_daily.tmdb.movies_digital)(
            TMDB_API_KEY, since, until)
    except Exception as exc:  # noqa: BLE001
        logging.info("TMDB week list failed: %s", exc)
        return [], {}
    out: list[str] = []
    seasons: dict[str, tuple[str, str]] = {}  # название → («38 сезон · 4 серия», год сезона) для карточек
    for t in found:
        if series and t.get("season") and t.get("episode"):
            label = f"{t['season']} сезон · {t['episode']} серия"
            for n in (t["name"], t["original"]):
                if n:
                    seasons[_norm_title(n)] = (label, t["aired"][:4])
            parts = [f"«{t['name']}» — {t['season']}-й сезон ({t['aired'][:4]}), сериал идёт с {t['year'] or '?'}"]
        else:
            parts = [f"«{t['name']}» ({t['year'] or '?'})"]
        if t["original"] and t["original"] != t["name"]:
            parts.append(f"ориг. {t['original']}")
        parts += t.get("genres", [])[:2]
        if t["votes"] >= 50 and t["rating"]:
            parts.append(f"TMDB {t['rating']:.1f}")
        if series and t.get("episode"):
            d = date.fromisoformat(t["aired"])
            parts.append(f"вышла {t['episode']}-я серия {t['season']}-го сезона {d.day} "
                         f"{shorts_daily.MONTHS[d.month - 1]}")
        out.append(", ".join(parts) + (f". {t['overview'][:220]}" if t["overview"] else ""))
    return out, seasons


def _norm_title(text: str) -> str:
    return re.sub(r"[^\w]+", "", str(text or "").lower().replace("ё", "е"))


async def make_list_post(bot: Bot, chat_id: int, key: str, title: str = "") -> None:
    """Пост-подборка («Новинки онлайн», «Топ сериалов», жанр) в стиле канала: ИИ выбирает фильмы везде —
    кандидаты Кинопоиска, свежие посты Telegram-каналов и поиск Google; альбом постеров, текст — подписью к
    первому; ниже — кнопки публикации. Фильмы не повторяются подряд."""
    today = datetime.now(MSK).date()
    hist = await _posts_history()
    hint = ""
    if key.startswith("h:") and key[2:] in HOLIDAYS:  # подборка к празднику
        hol_title, kp_films, hint = await holiday_pick(key[2:])
        title = title or hol_title
    elif key.startswith("x:") and key[2:] in STYLE_LISTS:  # подборка по стилю
        _, style_title, params = STYLE_LISTS[key[2:]]
        title = title or style_title
        kp_films = await _style_candidates(params, {int(x) for x in hist["films"]}) or \
            await _style_candidates(params, set())
    else:
        theme = shorts_daily.theme_for(key, today)
        title = title or theme.hook
        # подборки «что вышло сейчас» историю не учитывают: неудачная или неопубликованная попытка не должна
        # отнимать фильмы у следующей; повторы отсекаем только у «вечных» подборок (жанры, топы)
        exclude = set() if key in FRESH_LISTS else {int(x) for x in hist["films"]}
        kp_films = await shorts_daily.pick_films(kp._get, theme, today, exclude)
        if len(kp_films) < 3 and exclude:
            kp_films = await shorts_daily.pick_films(kp._get, theme, today, set())
    own = POST_CHANNEL.lstrip("@").lower()
    try:  # свежие посты каналов — что вышло и что обсуждают
        posts = await asyncio.wait_for(shorts_tgsearch.latest(
            [c for c in await fact_channels() if c.lower() != own], hours=72, per_channel=6), 40)
    except Exception as exc:  # noqa: BLE001
        logging.info("List post: tg channels skipped: %s", exc)
        posts = []
    lines = [_film_line(f) for f in kp_films]
    if key.startswith("h:"):  # фильмы точно про праздник — первыми
        lines = HOLIDAY_SEEDS.get(key[2:], []) + lines
    seasons: dict[str, tuple[str, str]] = {}
    if key in ("weekseries", "weekfilms"):  # первыми — популярное с TMDB: свежие серии идущих сериалов, релизы онлайн
        tm_lines, seasons = await _tmdb_week_lines(key == "weekseries", today)
        lines = tm_lines + lines
    res = await shorts_ai.write_list_post(
        GEMINI_API_KEY, title, lines, shorts_tgsearch.as_text(posts, 7000),
        [] if key in FRESH_LISTS or key.startswith("h:") else hist["list_titles"],
        f"{today.day} {shorts_daily.MONTHS[today.month - 1]} {today.year}",
        n=6 if key in ("weekseries", "weekfilms") else 5, hint=hint)
    if res:
        # в альбом — только то, что есть в тексте поста: ИИ иногда перечисляет в films больше, чем написал
        flat = re.sub(r"[^\w]+", "", res["post"].lower().replace("ё", "е"))
        in_post = [f for f in res["films"] if any(
            n and re.sub(r"[^\w]+", "", n.lower().replace("ё", "е")) in flat for n in (f["ru"], f["en"]))]
        res["films"] = in_post or res["films"]
        post, names = res["post"], [f["ru"] or f["en"] for f in res["films"]]
        found = [await _list_poster(f, kp_films, posts) for f in res["films"]]
        items = [(name, url, film) for name, (url, film) in zip(names, found)]
        episodes = [next((seasons[_norm_title(n)] for n in (f["ru"], f["en"]) if n and _norm_title(n) in seasons), None)
                    for f in res["films"]]
    elif len(kp_films) >= 3:  # ИИ не ответил — подборка только с Кинопоиска по шаблону
        post, names = _list_fallback(title, kp_films), [shorts_daily._title(f) for f in kp_films]
        items = [(shorts_daily._title(f), shorts_daily._poster_url(f), f) for f in kp_films]
        episodes = []
    else:
        raise RuntimeError("ни ИИ, ни Кинопоиск не дали фильмов для этой подборки — попробуй другую")
    post = _brand(post)
    hist["films"] = (hist["films"] + [str(f["id"]) for f in kp_films if f.get("id")])[-150:]
    hist["list_titles"] = (hist["list_titles"] + names)[-80:]
    await save_posts_history(hist)
    work = SHORTS_DIR / f"list_{time.time_ns()}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        ids, kind = await _send_album(bot, chat_id, await _list_cards(work, items, episodes=episodes), post,
                                      "#подборка")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    await offer_post(bot, chat_id, ids, kind, f"pt:l:{key}", names, "Подборка готова")


# ---------------------------------------------------------------- загрузка шортсов на YouTube

YT_CLIENT_ID = os.getenv("YT_CLIENT_ID", "").strip()
YT_CLIENT_SECRET = os.getenv("YT_CLIENT_SECRET", "").strip()
# готовые ролики и их проекты («🎙 Только звук», «🔁 Полностью») — в томе /data, а не во /tmp контейнера: иначе
# каждое обновление бота стирало их, и кнопки под вчерашним роликом отвечали «проекта нет»
READY_DIR = shorts_daily.DATA_DIR / "ready"
READY_DAYS = 3
yt_lock = asyncio.Lock()

YT_SETUP = (
    "▶️ <b>Загрузка на YouTube ещё не настроена.</b> Один раз, ~10 минут, с телефона:\n\n"
    "1. Открой console.cloud.google.com → проект <b>vkino2</b> → «APIs &amp; Services» → «Library» → найди "
    "<b>YouTube Data API v3</b> → «Enable».\n"
    "2. «OAuth consent screen» → тип <b>External</b> → название «KinoMir360 bot», твоя почта → сохрани. В «Test "
    "users» добавь свою Gmail (ту, где YouTube-канал). Потом нажми «Publish app» — иначе вход слетает каждые 7 дней.\n"
    "3. «Credentials» → «Create credentials» → «OAuth client ID» → тип <b>TVs and Limited Input devices</b> → "
    "«Create». Скопируй Client ID и Client secret.\n"
    "4. В Termius: <code>nano /opt/vkino/deploy/.env</code>, добавь две строки\n"
    "<code>YT_CLIENT_ID=…</code>\n<code>YT_CLIENT_SECRET=…</code>\nсохрани и перезапусти бота: "
    "<code>cd /opt/vkino/deploy &amp;&amp; docker compose up -d</code>\n"
    "5. Нажми «▶️ Загрузить на YouTube» ещё раз — бот покажет код для входа.\n\n"
    "⚠️ Пока Google не одобрит проект, ролики загружаются <b>приватными</b>. Чтобы загружались публичными, "
    "подай бесплатную заявку «YouTube API Services — Audit and Quota Extension Form» "
    "(support.google.com/youtube/contact/yt_api_form), рассмотрение — несколько недель.")


def keep_for_upload(chat_id: int, message_id: int, result: Any) -> None:
    """Сохранить готовый шортс и его SEO для кнопки «▶️ Загрузить на YouTube» (старше 3 дней — удаляем)."""
    try:
        READY_DIR.mkdir(parents=True, exist_ok=True)
        for old in READY_DIR.iterdir():
            if time.time() - old.stat().st_mtime > READY_DAYS * 86400:
                if old.is_dir():  # проект ролика (для «🎙 Только звук»)
                    shutil.rmtree(old, ignore_errors=True)
                else:
                    old.unlink(missing_ok=True)
        shutil.copy(result.video, READY_DIR / f"{chat_id}_{message_id}.mp4")
        (READY_DIR / f"{chat_id}_{message_id}.json").write_text(json.dumps(result.seo, ensure_ascii=False))
    except Exception:  # noqa: BLE001 — без копии просто не будет загрузки на YouTube
        logging.exception("Keep short for upload failed")


async def _yt_login(message: Message) -> str:
    """Вход по коду: показываем код, ждём подтверждения на google.com/device; refresh-токен — в базу."""
    device = await shorts_yt.device_start(YT_CLIENT_ID)
    await message.answer(
        "🔐 <b>Вход в YouTube</b>\n\n1. Открой <b>google.com/device</b> (или "
        f"{escape(str(device.get('verification_url') or 'https://www.google.com/device'))})\n"
        f"2. Введи код: <code>{escape(str(device['user_code']))}</code>\n"
        "3. Выбери аккаунт и YouTube-канал KinoMir360, нажми «Разрешить».\n\n"
        "Google покажет «приложение не проверено» — это твоё же приложение: «Дополнительно» → «Перейти». "
        "Жду подтверждения до 30 минут, потом загружу ролик сам.")
    refresh = await shorts_yt.device_wait(YT_CLIENT_ID, YT_CLIENT_SECRET, device)
    await set_shorts_setting("yt_refresh", refresh)
    await message.answer("✅ YouTube подключён — дальше загрузка одной кнопкой, без кода.")
    return refresh


YT_MANAGE_HELP = (
    "▶️ <b>Управление видео на YouTube</b>\n\n"
    "<code>/yt</code> — последние 15 видео: доступ, просмотры, ID\n"
    "<code>/yt hide ID [ID…]</code> — скрыть (ограниченный доступ)\n"
    "<code>/yt unlist ID</code> — доступ по ссылке\n"
    "<code>/yt show ID</code> — открыть всем\n"
    "<code>/yt title ID Новое название</code> — переименовать\n\n"
    "Вместо ID можно ссылку. Пачка правок — одним сообщением, каждая команда с новой строки "
    "(первая — с /yt):\n<code>/yt hide abc123def45\ntitle xyz789ghi01 Что посмотреть: …</code>")


async def _yt_token(message: Message) -> str:
    refresh = await shorts_setting("yt_refresh") or await _yt_login(message)
    try:
        return await shorts_yt.access_token(YT_CLIENT_ID, YT_CLIENT_SECRET, refresh)
    except shorts_yt.LoginExpired:
        await set_shorts_setting("yt_refresh", None)
        await message.answer("Вход в YouTube устарел — войдём заново.")
        return await shorts_yt.access_token(YT_CLIENT_ID, YT_CLIENT_SECRET, await _yt_login(message))


@router.message(Command("yt"))
async def youtube_manage(message: Message) -> None:
    """Скрыть, открыть или переименовать уже загруженные видео — без YouTube Studio."""
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    if not YT_CLIENT_ID or not YT_CLIENT_SECRET:
        await message.answer(YT_SETUP, disable_web_page_preview=True)
        return
    text = re.sub(r"^/yt(?:@\w+)?", "", message.text or "", count=1)
    if text.strip().lower() in ("help", "помощь", "?"):
        await message.answer(YT_MANAGE_HELP)
        return
    actions, errors = shorts_yt.parse_commands(text)
    if errors and not actions:
        await message.answer("⚠️ " + "\n".join(escape(e) for e in errors) + "\n\n" + YT_MANAGE_HELP)
        return

    async def job() -> None:
        try:
            token = await _yt_token(message)
            if not actions:
                rows = await shorts_yt.recent(token)
                icon = {"public": "🟢", "unlisted": "🔗", "private": "🔒"}
                lines = [f"{icon.get(r['privacy'], '❔')} <code>{r['id']}</code> · {r['views']} 👁 · "
                         f"{escape(r['title'][:70])}" for r in rows]
                await message.answer("▶️ <b>Последние видео</b>\n\n" + ("\n".join(lines) or "Видео нет.") +
                                     "\n\n<i>/yt help — команды</i>")
                return
            report: list[str] = []
            for verb, vid, value in actions:
                try:
                    if verb == "title":
                        old = await shorts_yt.set_title(token, vid, value)
                        report.append(f"✏️ <code>{vid}</code>: {escape(old[:50])} → {escape(value)}")
                    else:
                        title = await shorts_yt.set_privacy(token, vid, value)
                        mark = {"private": "🔒 скрыто", "unlisted": "🔗 по ссылке", "public": "🟢 открыто"}[value]
                        report.append(f"{mark}: {escape(title[:70])}")
                except shorts_yt.YouTubeError as exc:
                    report.append(f"❌ <code>{vid}</code>: {escape(str(exc)[:200])}")
            report += [f"⚠️ {escape(e)}" for e in errors]
            await message.answer("\n".join(report))
        except shorts_yt.YouTubeError as exc:
            await message.answer(f"❌ {escape(str(exc)[:300])}")
        except Exception:  # noqa: BLE001 — сообщить, а не молча упасть в фоне
            logging.exception("YouTube manage failed")
            await message.answer("❌ Не получилось связаться с YouTube — подробности в логах.")

    _run_job(job)


@router.callback_query(F.data.startswith("yt:"))
async def youtube_button(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS or not callback.message:
        await callback.answer()
        return
    await callback.answer()
    message = callback.message
    first, again = (callback.data or "")[3:], False
    if first.startswith("again:"):  # «загрузить ещё раз» после предупреждения о повторе
        first, again = first[6:], True
    if not YT_CLIENT_ID or not YT_CLIENT_SECRET:
        await message.answer(YT_SETUP, disable_web_page_preview=True)
        return
    video = READY_DIR / f"{message.chat.id}_{first}.mp4"
    seo_file = video.with_suffix(".json")
    done_file = video.with_suffix(".yt")  # ссылка на уже загруженный ролик — от двойной загрузки
    if not video.exists() or not seo_file.exists():
        await message.answer(f"Файла этого шортса уже нет на сервере (храню {READY_DAYS} дня). Собери ролик заново.")
        return
    if done_file.exists() and not again:
        await message.answer(
            f"⚠️ Этот ролик уже загружен на YouTube: {done_file.read_text().strip()}\nЗагрузить ещё раз (будет дубль)?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("▶️ Да, ещё раз", f"yt:again:{first}")]]),
            disable_web_page_preview=True)
        return
    if yt_lock.locked():
        await message.answer("⏳ Уже идёт загрузка на YouTube — подожди минуту.")
        return

    async def job() -> None:
        async with yt_lock:
            try:
                refresh = await shorts_setting("yt_refresh") or await _yt_login(message)
                try:
                    token = await shorts_yt.access_token(YT_CLIENT_ID, YT_CLIENT_SECRET, refresh)
                except shorts_yt.LoginExpired:
                    await set_shorts_setting("yt_refresh", None)
                    await message.answer("Вход в YouTube устарел — войдём заново.")
                    token = await shorts_yt.access_token(YT_CLIENT_ID, YT_CLIENT_SECRET, await _yt_login(message))
                seo = json.loads(seo_file.read_text())
                long = bool(seo.get("long"))
                wait = await message.answer("▶️ Загружаю выпуск на YouTube (большой файл — до 10–15 минут)…"
                                            if long else "▶️ Загружаю шортс на YouTube…")
                hidden = await shorts_setting("yt_privacy") == "unlisted"
                res = await shorts_yt.upload(token, video, seo, "unlisted" if hidden else "public")
                link = f"https://youtu.be/{res['id']}" if long else f"https://youtube.com/shorts/{res['id']}"
                shorts_usage.record("yt_long" if long else "yt_shorts")  # для недельного отчёта
                try:
                    done_file.write_text(link)
                except OSError:
                    logging.info("YouTube: upload mark not saved")
                thumb_note = ""
                cover = video.with_suffix(".jpg")
                if long and cover.exists():
                    try:
                        await shorts_yt.set_thumbnail(token, res["id"], cover)
                    except Exception as exc:  # noqa: BLE001
                        logging.info("YouTube thumbnail failed: %s", exc)
                        thumb_note = ("\n\n🖼 Обложку YouTube не принял (своя обложка доступна только подтверждённым "
                                      "каналам: YouTube Studio → Настройки → Канал → Доступ к функциям). Поставь её "
                                      "вручную — она выше в чате.")
                if hidden and res["privacy"] == "unlisted":
                    await wait.edit_text(
                        f"🔐 {'Выпуск' if long else 'Шортс'} загружен <b>по ссылке</b>: {link}{thumb_note}\n\n"
                        "Проверь в YouTube Studio → ролик → «Проверки» (авторские права). Всё чисто — «Доступ» → "
                        "«Открытый».")
                elif res["privacy"] == "public":
                    await wait.edit_text(f"✅ {'Выпуск' if long else 'Шортс'} на YouTube: {link}{thumb_note}")
                else:
                    await wait.edit_text(
                        f"✅ {'Выпуск' if long else 'Шортс'} загружен, но YouTube сделал его <b>приватным</b>: {link}"
                        f"{thumb_note}\n\nТак Google поступает "
                        "с роликами из непроверенных проектов. Открой YouTube Studio → ролик → «Доступ» → "
                        "«Открытый доступ», а чтобы дальше загружались сразу открытыми — подай заявку на аудит "
                        "(support.google.com/youtube/contact/yt_api_form).")
            except Exception as exc:  # noqa: BLE001
                logging.exception("YouTube upload failed")
                await message.answer(f"❌ YouTube: <code>{escape(_safe_err(exc)[:300])}</code>")

    _run_job(job)


def short_caption(result: Any) -> str:
    """Подпись шортса для Telegram-канала: название, вопрос зрителям, хэштеги, подпись канала."""
    seo = result.seo
    title = re.sub(r"\s*#shorts\s*", " ", seo.get("title", "")).strip()
    question = next((ln.strip() for ln in seo.get("pinned", "").splitlines() if ln.strip().endswith("👇")),
                    "Пиши в комментариях 👇")
    return f"🎬 {title}\n\n{question}\n\n" + " ".join(shorts_daily._hashtags(result)[:8])


def _hour_slots(now: datetime) -> list[datetime]:
    """Ближайшие 24 часа для «⏰ Позже»: начала часов, в которых ещё осталось куда поставить пост."""
    start = now.replace(minute=0, second=0, microsecond=0)
    hours = [start + timedelta(hours=k) for k in range(25)]
    return [h for h in hours if h + timedelta(minutes=45) > now + timedelta(minutes=2)][:24]


def _when(at: datetime, now: datetime) -> str:
    days = (at.date() - now.date()).days
    day = {0: "сегодня", 1: "завтра", -1: "вчера"}.get(days, at.strftime("%d.%m"))
    return day + at.strftime(" в %H:%M")


async def _scheduled() -> list[dict[str, Any]]:
    """Отложенные посты в канал (у всех админов — канал общий), по времени."""
    rows = await db().fetch("SELECT chat_id, first_id, EXTRACT(EPOCH FROM at)::BIGINT AS at FROM ai_queue "
                            "WHERE at > NOW() - INTERVAL '60 seconds' ORDER BY at")
    return [{"chat": r["chat_id"], "first": r["first_id"], "at": int(r["at"])} for r in rows]


def _busy_note(queue: list[dict[str, Any]], now: datetime) -> str:
    if not queue:
        return "\n\nЗапланированных постов нет."
    rows = [f"📌 {_when(datetime.fromtimestamp(q['at'], POST_TZ), now)}" for q in queue[:10]]
    return "\n\n<b>Уже запланировано</b> (📌 на кнопках — занято):\n" + "\n".join(rows)


async def publish_due(bot: Bot) -> None:
    """Очередь «⏰ Опубликовать позже»: подошло время — публикуем и сообщаем автору."""
    # подошедшие посты забираем из очереди одним запросом: вторая копия цикла (или повтор) их уже не увидит
    rows = await db().fetch("DELETE FROM ai_queue WHERE at <= NOW() RETURNING chat_id, first_id")
    due = [{"chat": r["chat_id"], "first": r["first_id"]} for r in rows]
    if not due:
        return
    await _queue_json_remove({(q["chat"], q["first"]) for q in due})
    for q in due:
        try:
            await publish_post(bot, q["chat"], q["first"])
            text = "✅ Пост вышел в канале по расписанию."
        except Exception as exc:  # noqa: BLE001
            logging.exception("Scheduled post failed")
            text = (f"❌ Пост по расписанию не вышел: <code>{escape(_safe_err(exc)[:200])}</code>\n\n" + PUBLISH_HELP)
        try:
            await bot.send_message(q["chat"], text)
        except Exception:  # noqa: BLE001
            logging.exception("Scheduled post report failed")


POST_MAKERS: dict[str, tuple[str, Any]] = {}  # заполняется ниже: код → (что пишем автору, функция)


async def run_post_maker(bot: Bot, chat_id: int, code: str) -> None:
    if code.startswith("l:"):
        await make_list_post(bot, chat_id, code[2:], PLAN_TITLES.get(code, ""))
    elif code.startswith("fx:"):
        await make_facts_post(bot, chat_id, code[3:] if code[3:] in ("new", "old") else "")
    else:
        await POST_MAKERS[code][1](bot, chat_id)


async def run_posts_plan(bot: Bot, now: datetime) -> None:
    """Автоплан (кнопка «🗓 Автоплан постов»): подошло время — готовим пост и присылаем админам."""
    if await shorts_setting("posts_plan") == "off":
        return
    done = await _json_setting("posts_plan_done", {})
    today = now.date().isoformat()
    for days, hour, code in POSTS_PLAN:
        if now.weekday() not in days or now.hour < hour or done.get(code) == today:
            continue
        done[code] = today
        await set_shorts_setting("posts_plan_done", json.dumps(done))
        if now.hour > hour + 2:  # бот был выключен или обновлялся — устаревший пост не шлём пачкой
            continue
        for admin_id in sorted(ADMIN_IDS):
            try:
                async with post_lock:
                    await run_post_maker(bot, admin_id, code)
            except NothingToday:
                pass
            except Exception:
                logging.exception("Planned post %s failed", code)


async def posts_auto_loop(bot: Bot) -> None:
    """Каждую минуту: очередь отложенных постов; автоплан постов; «🔔 Авто-посты» — раз в 30 минут
    (кроме ночи) новый пост по свежей новости."""
    last = 0.0
    while True:
        await asyncio.sleep(60)
        try:
            await publish_due(bot)
            now = datetime.now(POST_TZ)  # автоплан и «ночь без авто-постов» — по времени автора (Екатеринбург)
            await run_posts_plan(bot, now)
            if (await shorts_setting("posts_auto") == "on" and time.monotonic() - last >= POSTS_EVERY
                    and now.hour not in POSTS_NIGHT):
                last = time.monotonic()
                await make_tg_post(bot, sorted(ADMIN_IDS), "all")
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Posts loop failed")


def _run_job(coro_fn: Any) -> None:
    task = asyncio.create_task(coro_fn())
    shorts_jobs.add(task)
    task.add_done_callback(shorts_jobs.discard)


# ---------------------------------------------------------------- 🔥 инфоповоды

HYPE_EVERY = 20 * 60
HYPE_MAX_DAY = int(os.getenv("HYPE_MAX_DAY", "4"))  # не больше уведомлений в день, чтобы не заспамить
hype_cache: dict[str, dict[str, Any]] = {}


async def hype_scan() -> list[dict[str, Any]]:
    """Громкие истории за 8 часов, о которых ещё не было уведомления (за 3 дня)."""
    own = POST_CHANNEL.lstrip("@").lower()
    channels = [c for c in await fact_channels() if c.lower() != own]
    posts = await shorts_tgsearch.latest(channels, hours=8, per_channel=6)
    try:
        posts += await shorts_fast.latest(await fast_feeds(), timeout=50)
    except Exception as exc:  # noqa: BLE001
        logging.info("Hype: fast sources failed: %s", exc)
    seen = json.loads(await shorts_setting("hype_seen") or "[]")
    out = []
    for g in shorts_hype.hot(shorts_hype.clusters(posts, _key_tokens, _shared)):
        toks = set(g["tokens"])
        if _repeats(toks, seen) or _repeats(toks, seen, english=True):
            continue
        out.append(g)
    return out


async def hype_alert(bot: Bot, g: dict[str, Any], chat_ids: list[int]) -> None:
    """«🔥 Инфоповод» автору: о чём, сколько источников, кнопки «Пост», «Тизер», «Текст для рекламы»."""
    hid = secrets.token_hex(4)
    hype_cache[hid] = g
    for old in list(hype_cache)[:-40]:
        hype_cache.pop(old, None)
    hist = await _posts_history()
    stories = [st for st in hist["stories"] if time.time() - st.get("at", 0) < STORY_HOURS * 3600]
    posted = _repeats(set(g["tokens"]), stories)
    head = f"🔥 <b>Инфоповод</b> — пишут {g['sources']} источника(ов)"
    if g["franchise"]:
        head += f" · громкое имя: <b>{escape(g['franchise'])}</b>"
    lines = [head, ""]
    for p in g["posts"][:3]:
        lines.append("— " + escape(re.sub(r"\s+", " ", p.get("text") or "")[:180]) + "…")
    links = " · ".join(
        f'<a href="{escape(p["url"])}">{escape(str(p.get("channel") or p.get("label") or "источник"))[:24]}</a>'
        for p in g["posts"][:6] if p.get("url"))
    lines.append("\nИсточники: " + links)
    lines.append("\n✅ Пост об этом в канале уже был — сделай тизер и рекламу." if posted else
                 "\n⚡ Хайп держится 3–5 дней — лучше выйти с постом сегодня.")
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("📝 Пост в канал сейчас", f"hy:p:{hid}")],
        [_btn("🎬 Тизер-ролик (Telegram и MAX)", f"hy:t:{hid}")],
        [_btn("📣 Текст для посева и Директа", f"hy:a:{hid}")]])
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id, "\n".join(lines), reply_markup=markup, disable_web_page_preview=True)
        except Exception:  # noqa: BLE001
            logging.exception("Hype alert failed")


async def _hype_remember(g: dict[str, Any]) -> None:
    seen = [st for st in json.loads(await shorts_setting("hype_seen") or "[]")
            if time.time() - st.get("at", 0) < STORY_HOURS * 3600]
    seen.append({"at": int(time.time()), "tokens": g["tokens"][:120]})
    await set_shorts_setting("hype_seen", json.dumps(seen[-80:], ensure_ascii=False))


async def hype_loop(bot: Bot) -> None:
    """Раз в 20 минут (кроме ночи): громкие инфоповоды — уведомление автору, не больше HYPE_MAX_DAY в день."""
    while True:
        await asyncio.sleep(HYPE_EVERY)
        try:
            if await shorts_setting("hype") == "off":
                continue
            now = datetime.now(POST_TZ)
            if now.hour in POSTS_NIGHT:
                continue
            day = json.loads(await shorts_setting("hype_day") or "{}")
            if day.get("d") != now.date().isoformat():
                day = {"d": now.date().isoformat(), "n": 0}
            if day["n"] >= HYPE_MAX_DAY:
                continue
            for g in (await hype_scan())[:HYPE_MAX_DAY - day["n"]]:
                await hype_alert(bot, g, sorted(ADMIN_IDS))
                await _hype_remember(g)
                day["n"] += 1
            await set_shorts_setting("hype_day", json.dumps(day))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logging.exception("Hype loop failed")


@router.message(Command("hype"))
async def hype_command(message: Message) -> None:
    """/hype — проверить инфоповоды сейчас; /hype on|off — включить/выключить автоуведомления."""
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    arg = (message.text or "").partition(" ")[2].strip().lower()
    if arg in ("on", "off"):
        await set_shorts_setting("hype", arg)
        await message.answer("🔥 Уведомления об инфоповодах " + (
            f"включены: проверка каждые 20 минут, не больше {HYPE_MAX_DAY} в день." if arg == "on" else "выключены."))
        return
    wait = await message.answer("⏳ Ищу громкие новости: Telegram-каналы и зарубежные источники за 8 часов…")
    try:
        groups = await hype_scan()
    except Exception as exc:  # noqa: BLE001
        await wait.edit_text(f"❌ Не получилось: <code>{escape(_safe_err(exc)[:300])}</code>")
        return
    if not groups:
        auto = "выключена — /hype on" if await shorts_setting("hype") == "off" else "каждые 20 минут"
        await wait.edit_text("Сейчас громких инфоповодов нет (нужно от 3 источников, для больших франшиз — от 2). "
                             f"Автопроверка: {auto}.")
        return
    await wait.delete()
    for g in groups[:3]:
        await hype_alert(message.bot, g, [message.chat.id])
        await _hype_remember(g)


async def _hype_teaser_job(bot: Bot, chat_id: int, g: dict[str, Any]) -> None:
    lines = await shorts_hype.teaser_lines(GEMINI_API_KEY, shorts_hype.material(g))
    work = SHORTS_DIR / f"hype_{time.time_ns()}"
    try:
        for key, label in (("tg", "Telegram"), ("max", "MAX")):
            path = await asyncio.to_thread(shorts_teaser.render, lines, work / f"teaser_{key}.mp4", key)
            await bot.send_video(chat_id, FSInputFile(path, filename=f"kinomir360_teaser_{key}.mp4"), width=1080,
                                 height=1080, caption=f"🎬 Тизер для {label}: в канал, в посев или в объявление Директа.")
        await bot.send_message(chat_id, "Текст ролика: " + escape(" · ".join(v for v in lines.values() if v)))
    finally:
        shutil.rmtree(work, ignore_errors=True)


@router.callback_query(F.data.startswith("hy:"))
async def hype_buttons(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS or not callback.message:
        await callback.answer()
        return
    _, kind, hid = ((callback.data or "") + "::").split(":", 2)
    g = hype_cache.get(hid.rstrip(":"))
    if not g:
        await callback.answer("Инфоповод устарел (бот перезапускался) — /hype найдёт заново", show_alert=True)
        return
    await callback.answer()
    bot, chat_id = callback.bot, callback.message.chat.id
    if kind == "p":
        await bot.send_message(chat_id, "📝 Пишу пост по этому инфоповоду…")

        async def post_job() -> None:
            try:
                if not await make_tg_post(bot, [chat_id], "all", pool=g["posts"]):
                    await bot.send_message(chat_id, "Пост не собрался: похоже, об этом уже писали в канале.")
            except Exception as exc:  # noqa: BLE001
                await bot.send_message(chat_id, f"❌ <code>{escape(_safe_err(exc)[:400])}</code>")
        _run_job(post_job)
    elif kind == "t":
        await bot.send_message(chat_id, "🎬 Собираю тизер-ролики (Telegram и MAX), пара минут…")

        async def teaser_job() -> None:
            try:
                await _hype_teaser_job(bot, chat_id, g)
            except Exception as exc:  # noqa: BLE001
                logging.exception("Hype teaser failed")
                await bot.send_message(chat_id, f"❌ Тизер не собрался: <code>{escape(_safe_err(exc)[:400])}</code>")
        _run_job(teaser_job)
    elif kind == "a":
        texts = await shorts_hype.promo_texts(GEMINI_API_KEY, shorts_hype.material(g), POST_CHANNEL)
        if not texts:
            await bot.send_message(chat_id, "❌ ИИ сейчас не ответил — попробуй через пару минут.")
            return
        await bot.send_message(chat_id, "📣 <b>Пост для посева</b> (в чужой кино-канал):\n\n" + escape(texts["seed"]))
        await bot.send_message(
            chat_id, "🟡 <b>Для Яндекс Директа</b> (нативный пост, без эмодзи)\n\n<b>Заголовок:</b> "
            + escape(texts["direct_title"]) + "\n\n<b>Текст:</b>\n" + escape(texts["direct_text"])
            + "\n\nНазвания чужих брендов модерация может не пропустить — тогда замени их описанием "
            "(«культовый сериал про 80-е»).")


POST_MAKERS.update({
    "bd": ("🎂 Ищу именинника дня, его лучшие фильмы и роли, ИИ пишет поздравление…", make_birthday_post),
    "an": ("📅 Ищу фильмы, у которых сегодня круглая дата премьеры…", make_anniversary_post),
    "qz": ("🧩 Выбираю фильм и кадр без подсказок для викторины…", make_quiz_post),
    "qt": ("💬 ИИ ищет известную цитату из фильма…", make_quote_post),
    "bo": ("💰 ИИ ищет в Google сборы прошедшего уикенда…", make_box_office_post),
    "wk": ("🗞 Читаю каналы за неделю, ИИ собирает главное…", make_week_post),
    "pr": ("📣 Готовлю рекламный пост канала для взаимного пиара…", make_promo_post),
    "sp": ("🤝 ИИ пишет обращение к подписчикам…", make_support_post),
    "rq": ("🎯 Готовлю пост «Рекомендация: что посмотреть» с кнопкой опроса…", make_pick_post),
    "ev": ("🎬 Выбираю фильм вечера, ищу трейлер со звуком и где посмотреть…", make_evening_post),
    "bt": ("⚔️ Подбираю пару фильмов для битвы…", make_battle_post),
    "vt": ("🗳 Готовлю голосование «Выбор подписчиков»…", make_vote_post),
    "vr": ("🗳 Подвожу итог голосования и готовлю факты о победителе…", make_vote_result_post),
    "bl": ("🎭 Ищу фильм с ляпами…", make_bloopers_post),
    "ab": ("🤖 Готовлю пост про бота…", make_bot_post),
    "bs": ("⚡ Готовлю пост про буст канала…", make_boost_post),
    "nv": ("📌 Готовлю закреп-навигатор…", make_navigator_post),
    "gv": ("🎁 Готовлю шаблон розыгрыша…", make_giveaway_post),
    "ar": ("📰 ИИ пишет статью для Дзена и VK, обычно 1–2 минуты…", make_article),
})
PLAN_HELP = (
    "🗓 <b>Автоплан постов включён.</b> Бот сам готовит посты и присылает тебе — публикуешь кнопкой "
    "(можно «⏰ Позже»):\n"
    "• каждый день 9:00 — 🎂 день рождения звезды\n• каждый день 13:00 — 📅 этот день в кино (если есть "
    "круглая дата)\n• каждый день 19:00 — 🧩 угадай фильм по кадру\n• каждый день 20:00 — 🎬 фильм вечера\n"
    "• пн 12:00 — 💰 сборы уикенда\n• пн 16:00 — 🗳 выбор подписчиков (голосование)\n• вт 17:00 — ⚔️ битва "
    "фильмов\n• ср 15:00 — 💬 цитата дня\n• чт 11:00 — 🍿 премьеры недели в кино\n• пт 11:00 — 🆕 что "
    "посмотреть на выходных\n• пт 16:00 — 🗳 факты о фильме, который выбрали подписчики\n• сб 15:00 — 🎭 ляпы "
    "в кино\n• вс 18:00 — 🗞 итоги недели\n\nВремя екатеринбургское. Выключить — та же кнопка.")


@router.callback_query(F.data.startswith("pt:"))
async def post_buttons(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id not in ADMIN_IDS or not callback.message:
        await callback.answer()
        return
    message, action = callback.message, (callback.data or "")[3:]
    chat_id = message.chat.id
    kind, _, arg = action.partition(":")
    if kind == "vis" and arg.isdigit():
        await callback.answer()
        await show_scene_review(message.bot, chat_id, int(arg))
        return
    if kind == "va" and arg.isdigit():
        await callback.answer()
        await approve_scene_review(message.bot, chat_id, int(arg))
        return
    if kind == "vr" and re.fullmatch(r"\d+:\d+", arg):
        await callback.answer()
        first, index = map(int, arg.split(":"))
        folder = proj_dir(chat_id, first)
        proj = shorts_project.load(folder) or {}
        cards = (proj.get("rerun") or {}).get("cards") or []
        if not 0 <= index < len(cards):
            await message.answer("Эта сцена уже недоступна. Открой проверку ещё раз.")
            return
        await state.set_state(SceneReviewState.replacement)
        await state.update_data(scene_review_first=first, scene_review_index=index)
        await message.answer(f"📷 Пришли новое изображение для сцены №{index + 1}. /cancel — отмена.")
        return
    if kind == "wvcut" and arg.isdigit():  # выпуск → вертикальные шортсы по новостям
        await callback.answer()
        _run_job(lambda: episode_cuts_job(message.bot, chat_id, int(arg)))
        return
    if kind in ("rva", "rvf") and arg.split(":")[0].isdigit():  # шортс/лор/обзор: только звук или полностью
        await callback.answer()
        first, sure = int(arg.split(":")[0]), arg.endswith(":go")
        if kind == "rva":
            _run_job(lambda: project_audio_job(message.bot, chat_id, first, sure))
        else:
            _run_job(lambda: project_remake_job(message.bot, chat_id, first))
        return
    if kind == "wvvoice" and arg.split(":")[0].isdigit():  # выпуск → только исправленные фразы голоса
        await callback.answer()
        first, sure = int(arg.split(":")[0]), arg.endswith(":go")
        _run_job(lambda: episode_revoice_job(message.bot, chat_id, first, sure))
        return
    if kind == "p" and arg.isdigit():  # опубликовать сейчас
        done = (await _post_meta(chat_id, int(arg))).get("published")
        if done:
            await callback.answer()
            await message.edit_text(
                f"⚠️ Этот пост уже вышел в канале {_when(datetime.fromtimestamp(done, POST_TZ), datetime.now(POST_TZ))}"
                f" ({POST_TZ_NAME}). Опубликовать ещё раз?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [_btn("📢 Да, ещё раз", f"pt:pf:{arg}")], [_btn("⬅️ Нет", f"pt:b:{arg}")]]))
            return
    if (kind == "p" or kind == "pf") and arg.isdigit():
        try:
            await publish_post(message.bot, chat_id, int(arg), callback.from_user.id)
        except AlreadyPublishing:
            await callback.answer("⏳ Уже публикую — секунду")
            return
        except Exception as exc:  # noqa: BLE001
            await callback.answer()
            await message.answer(f"❌ Не получилось опубликовать в {escape(POST_CHANNEL)}: "
                                 f"<code>{escape(_safe_err(exc)[:200])}</code>\n\n" + PUBLISH_HELP)
            return
        await callback.answer("✅ Опубликовано")
        try:
            await message.edit_text("✅ Опубликовано в канале.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [_btn("📝 Ещё посты", "pt:menu")]]))
        except TelegramBadRequest:
            pass
        return
    await callback.answer()
    if kind == "t" and arg.isdigit():  # «⏰ Позже»: сначала час (ближайшие 24 часа, время автора)
        now = datetime.now(POST_TZ)
        hours = _hour_slots(now)
        queue = [q for q in await _scheduled() if not (q["chat"] == chat_id and q["first"] == int(arg))]
        taken = {int(q["at"]) // 3600 for q in queue}
        buttons = [_btn(("📌" if int(h.timestamp()) // 3600 in taken else "")
                        + ("" if h.date() == now.date() else "завтра ") + h.strftime("%H:00"),
                        f"pt:h:{arg}:{int(h.timestamp()) // 3600}") for h in hours]
        rows = [buttons[k:k + 4] for k in range(0, len(buttons), 4)]
        if queue:
            rows.append([_btn(f"📋 Что запланировано ({len(queue)})", f"pt:q:{arg}")])
        rows.append([_btn("⬅️ Назад", f"pt:b:{arg}")])
        await message.edit_text(f"⏰ Выбери час публикации (время {POST_TZ_NAME}), потом — минуты."
                                + _busy_note(queue, now), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        return
    if kind == "q" and arg.isdigit():  # список отложенных: показать пост или снять с публикации
        now = datetime.now(POST_TZ)
        queue = await _scheduled()
        rows = []
        for q in queue[:10]:
            label = _when(datetime.fromtimestamp(q["at"], POST_TZ), now)
            mine = q["chat"] == chat_id
            rows.append([_btn(f"👁 {label}", f"pt:v:{q['chat']}:{q['first']}") if mine else
                         _btn(f"📌 {label} (у другого админа)", f"pt:q:{arg}")]
                        + ([_btn("❌ Снять", f"pt:x:{arg}:{q['first']}")] if mine else []))
        rows.append([_btn("⬅️ К выбору времени", f"pt:t:{arg}")])
        await message.edit_text("📋 <b>Запланированные посты</b>. «👁» — показать пост, «❌ Снять» — отменить "
                                "отложенную публикацию." if queue else "📋 Запланированных постов нет.",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        return
    if kind == "v":  # показать запланированный пост ещё раз
        owner, _, first = arg.partition(":")
        if owner.isdigit() and first.isdigit() and int(owner) == chat_id:
            meta = await _post_meta(chat_id, int(first))
            try:
                await message.bot.copy_messages(chat_id, chat_id, meta["ids"])
            except Exception as exc:  # noqa: BLE001
                await message.answer(f"Пост уже не показать (удалён из чата?): <code>{escape(_safe_err(exc)[:150])}</code>")
        return
    if kind == "x":  # снять с публикации из списка
        back, _, first = arg.partition(":")
        if back.isdigit() and first.isdigit():
            await _unqueue(chat_id, int(first))
            await message.answer("❌ Отложенная публикация снята. Пост остался в чате — его можно опубликовать "
                                 "или поставить на другое время кнопками под ним.")
            await message.edit_text("📋 Обновляю список…", reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[_btn("📋 Показать список", f"pt:q:{back}")]]))
        return
    if kind == "h":  # потом минуты: :00, :15, :30, :45
        first, _, hour = arg.partition(":")
        if not first.isdigit() or not hour.isdigit():
            return
        start = int(hour) * 3600
        now = time.time()
        mins = [start + m * 60 for m in (0, 15, 30, 45) if start + m * 60 > now + 60]
        label = datetime.fromtimestamp(start, POST_TZ).strftime("%H")
        queue = [q for q in await _scheduled() if not (q["chat"] == chat_id and q["first"] == int(first))]
        # занято — если другой пост выходит ближе 15 минут к этому времени
        busy = [t for t in mins if any(abs(q["at"] - t) < 15 * 60 for q in queue)]
        rows = [[_btn(("📌 " if t in busy else "") + f"{label}:{datetime.fromtimestamp(t, POST_TZ):%M}",
                      f"pt:s:{first}:{t // 60}") for t in mins]]
        rows.append([_btn("⬅️ Другой час", f"pt:t:{first}")])
        here = [q for q in queue if start - 15 * 60 < q["at"] < start + 75 * 60]
        note = ("\n\n📌 В это время уже есть: " + ", ".join(
            datetime.fromtimestamp(q["at"], POST_TZ).strftime("%H:%M") for q in here)
            + ". Лучше выбрать минуты без 📌 — два поста подряд хуже смотрятся." if here else "")
        await message.edit_text(f"⏰ Час выбран: {label}:00. Теперь минуты (время {POST_TZ_NAME})." + note,
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r]))
        return
    if kind == "b" and arg.isdigit():
        meta = await _post_meta(chat_id, int(arg))
        await message.edit_text("👆 Пост готов.", reply_markup=post_keyboard(int(arg), yt=bool(meta.get("yt"))))
        return
    if kind == "s":  # поставить в очередь
        first, _, minute = arg.partition(":")
        if not first.isdigit() or not minute.isdigit():
            return
        at = int(minute) * 60
        await queue_add(chat_id, int(first), at)
        when = _when(datetime.fromtimestamp(at, POST_TZ), datetime.now(POST_TZ))
        await message.edit_text(f"⏰ Пост выйдет в канале {when} ({POST_TZ_NAME}).", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[_btn("📢 Нет, опубликовать сейчас", f"pt:p:{first}")],
                             [_btn("❌ Отменить", f"pt:u:{first}")]]))
        return
    if kind == "u" and arg.isdigit():
        await _unqueue(chat_id, int(arg))
        meta = await _post_meta(chat_id, int(arg))
        await message.edit_text("Отложенная публикация отменена.",
                                reply_markup=post_keyboard(int(arg), yt=bool(meta.get("yt"))))
        return
    if kind == "m" and arg.isdigit():  # своё фото или видео к посту
        await state.set_state(PostState.media)
        await state.update_data(post_first=int(arg), post_kb=message.message_id)
        _media_buf[chat_id] = {"first": int(arg), "items": [], "groups": set()}
        await message.answer("🖼 Пришли фото или видео для поста — одно или несколько (до 10, можно альбомом). "
                             "Потом выберешь: заменить медиа в посте или добавить к тем, что уже есть.\n\n"
                             "<i>Передумал — /cancel</i>")
        return
    if kind in ("mr", "ma") and arg.isdigit():  # заменить / добавить присланные медиа
        data = await state.get_data()
        await state.clear()
        buf = _media_buf.pop(chat_id, None)
        if not buf or buf["first"] != int(arg) or not buf["items"]:
            await message.answer("Сначала нажми «🖼 Заменить или добавить фото/видео» под постом и пришли медиа.")
            return
        await post_set_media(message.bot, chat_id, int(arg), buf["items"][:10], add=kind == "ma",
                             kb_message=data.get("post_kb"))
        return
    if kind == "e" and arg.isdigit():  # поправить текст
        await state.set_state(PostState.edit)
        await state.update_data(post_first=int(arg))
        await message.answer("✏️ Пришли новый текст поста целиком (можно скопировать старый и поправить). "
                             "Подпись канала внизу добавлю сам.\n\n<i>Передумал — /cancel</i>")
        return
    if action == "hol":
        await message.answer("🎉 <b>Подборка к празднику</b> — выбери праздник:", reply_markup=holidays_menu("pt:l:h:", "pt:menu"))
        return
    if action == "styles":
        await message.answer("🎨 <b>Подборки по стилю</b> — ИИ подберёт фильмы по теме (Кинопоиск, интернет), "
                             "постеры — фирменными карточками.", reply_markup=style_menu())
        return
    if action in ("menu", "home"):
        text = ("📢 <b>Telegram-канал: посты</b>\n\nВыбери раздел. Видео для YouTube — в админке, «▶️ YouTube и "
                "видео».")
        if action == "home":  # «⬅️ Все разделы» из раздела — меняем то же сообщение
            try:
                await message.edit_text(text, reply_markup=posts_menu())
                return
            except TelegramBadRequest:
                pass
        await message.answer(text, reply_markup=posts_menu())
        return
    if kind == "sec" and arg in POST_SECTIONS:
        try:
            await message.edit_text(POST_SECTIONS[arg][1], reply_markup=posts_section(arg))
        except TelegramBadRequest:
            await message.answer(POST_SECTIONS[arg][1], reply_markup=posts_section(arg))
        return
    if action == "ll":
        await state.set_state(PostState.listlink)
        await message.answer(
            "🔗 Пришли ссылку на подборку (статья, пост Telegram-канала) или вставь сам список фильмов — можно с "
            "описаниями. Бот возьмёт фильмы ровно оттуда, в том же порядке, перескажет в стиле канала и сделает "
            "фирменные карточки с постерами.\n\n<i>Передумал — /cancel</i>")
        return
    if action == "dg":
        await state.set_state(PostState.digest)
        await message.answer(
            "🧩 Напиши тему — имя, фильм или событие: <code>Патрик Стюарт</code>, <code>Мстители: Судный день</code>. "
            "Можно с английским вариантом через «/»: <code>Патрик Стюарт / Patrick Stewart</code>.\n"
            "Бот найдёт посты об этом во всех каналах из списка за последние 3 дня, ИИ соберёт их в один пост "
            "без повторов (главное, подробности, кто что сказал), фото возьму из постов.\n\n<i>Передумал — /cancel</i>")
        return
    if action in ("wvon", "wvoff"):
        await set_shorts_setting("weekly_video", None if action == "wvon" else "off")
        await message.answer("🔔 По средам и воскресеньям в 10:00 (Екатеринбург) соберу выпуск «Новости кино» и пришлю "
                             "сюда." if action == "wvon" else "🔕 По средам и воскресеньям выпуск больше не собираю. Вручную — "
                             "▶️ YouTube и видео → «🎞 Выпуск «Новости кино»».")
        return
    if action == "stop":
        await message.answer(await stop_jobs())
        return
    if action == "nar":
        await state.set_state(PostState.narrate)
        await message.answer(
            "🎙 <b>Диктор</b>: пришли текст для озвучки сообщением или файлом .txt / .docx (до 20 000 знаков).\n\n"
            "Бот проверит русский язык (имена, падежи, грамматика, речевые ошибки), подготовит версию для голоса "
            "(числа словами, ударения) и пришлёт оба текста и список правок. Озвучка твоим голосом — отдельной "
            "кнопкой, после того как посмотришь текст.\n\n<i>Передумал — /cancel</i>")
        return
    if action == "narv":
        await narrator_voice(message)
        return
    if action == "wvre":
        if weekly_busy:
            await message.answer("⏳ Выпуск уже собирается — пришлю, как будет готов.")
            return
        status = await message.answer("🔁 Перемонтирую последний выпуск: тот же текст, озвучка заново с исправленными "
                                      "ударениями (/say), графики сборов. 15–40 минут — пришлю, когда будет готово.")
        chat_id = message.chat.id

        async def remake_job() -> None:
            try:
                await make_weekly_video(message.bot, [chat_id], status, remake=True)
                await status.delete()
            except Exception as exc:  # noqa: BLE001
                logging.exception("Weekly remake failed")
                await status.edit_text(f"❌ Выпуск не перемонтировался: <code>{escape(_safe_err(exc)[:300])}</code>")

        _run_job(remake_job)
        return
    if action == "wv":
        if weekly_busy:
            await message.answer("⏳ Выпуск уже собирается — пришлю, как будет готов.")
            return
        status = await message.answer("🎞 Собираю выпуск «Новости кино» (новости с прошлого выпуска): 6–8 новостей, голос, картинки, "
                                      "заставки. Это 15–40 минут — пришлю, когда будет готово."
                                      + ("\n⏳ Сейчас собирается другой ролик — начну сразу после него."
                                         if shorts_lock.locked() else ""))
        chat_id = message.chat.id

        async def weekly_job() -> None:
            try:
                await make_weekly_video(message.bot, [chat_id], status)
                await status.delete()
            except Exception as exc:  # noqa: BLE001
                logging.exception("Weekly video failed")
                await status.edit_text(f"❌ Выпуск не собрался: <code>{escape(_safe_err(exc)[:300])}</code>")

        _run_job(weekly_job)
        return
    if action == "rt":
        await message.answer("🍅 Ищу свежие и ближайшие премьеры…")
        text, kb = await rt_menu()  # второй позиционный аргумент answer — не клавиатура
        await message.answer(text, reply_markup=kb)
        return
    if action == "ac":
        await state.set_state(PostState.actor)
        await message.answer(
            "🎭 Напиши имя актёра или актрисы — например <code>Патрик Стюарт</code> или <code>Cillian Murphy</code>. "
            "Бот возьмёт лучшие фильмы и сериалы с ролями, портрет и сделает пост с опросом.\n"
            "Если человека нет в живых — выйдет пост «Светлая память» с чёрно-белым портретом "
            "(или допиши слово «память»).\n\n<i>Передумал — /cancel</i>")
        return
    if action == "lk":
        await state.set_state(PostState.link)
        await message.answer(
            "🔗 Пришли ссылку на новость — или несколько ссылок в одном сообщении (сайты, посты Telegram-каналов). "
            "Можно просто переслать пост из канала или вставить текст. ИИ перескажет своими словами в стиле "
            "канала, добавит эмодзи и хэштеги; фото или видео возьму из источника.\n\n"
            "После ссылок можно дописать пожелание, например: «с юмором», «коротко».\n\n<i>Передумал — /cancel</i>")
        return
    if action == "plan":
        on = await shorts_setting("posts_plan") == "off"
        await set_shorts_setting("posts_plan", None if on else "off")
        await message.answer(PLAN_HELP if on else "🗓 Автоплан постов выключен. Включить — та же кнопка.")
        return
    if action in POST_MAKERS or kind == "fx":
        if post_lock.locked():
            await message.answer("⏳ Пост уже пишется — подожди минуту.")
            return
        wait = await message.answer(POST_MAKERS[action][0] if action in POST_MAKERS else
                                    "💡 Выбираю фильм, ищу факты везде: Кинопоиск, интернет, Telegram-каналы…")

        async def other_job() -> None:
            try:
                async with post_lock:
                    await run_post_maker(message.bot, chat_id, action)
                await wait.delete()
            except Exception as exc:  # noqa: BLE001
                logging.exception("Post failed")
                await wait.edit_text(f"❌ Пост не получился: <code>{escape(_safe_err(exc)[:300])}</code>")

        _run_job(other_job)
        return
    if kind == "l":
        key = arg
        if key not in LIST_POSTS and not key.startswith("genre:") and not (key.startswith("x:") and key[2:] in STYLE_LISTS) \
                and not (key.startswith("h:") and key[2:] in HOLIDAYS):
            return
        if post_lock.locked():
            await message.answer("⏳ Пост уже пишется — подожди минуту.")
            return
        wait = await message.answer("🍿 Ищу фильмы везде: Кинопоиск, интернет, Telegram-каналы — ИИ собирает подборку…")

        async def list_job() -> None:
            try:
                async with post_lock:
                    await make_list_post(message.bot, chat_id, key)
                await wait.delete()
            except Exception as exc:  # noqa: BLE001
                logging.exception("List post failed")
                await wait.edit_text(f"❌ Подборка не получилась: <code>{escape(_safe_err(exc)[:300])}</code>")

        _run_job(list_job)
        return
    if action in ("pmode", "pmode:live", "pmode:db", "pmode:art"):
        if action == "pmode:art":
            await set_shorts_setting("illustrations",
                                     None if await shorts_setting("illustrations") == "off" else "off")
        elif action != "pmode":
            await set_shorts_setting("post_media", "db" if action == "pmode:db" else None)
        db = await shorts_setting("post_media") == "db"
        art = await shorts_setting("illustrations") != "off"
        await message.answer(
            "🖼 <b>Фото к новостям</b>\n\n"
            f"{'⚪️' if db else '🟢'} <b>Из новости</b> — фото и видео из поста-источника (кадры со съёмок, ролики, "
            "альбомы), если ИИ подтвердил, что они о той же новости; иначе постер из базы фильмов.\n"
            f"{'🟢' if db else '⚪️'} <b>Из базы фильмов</b> — всегда постер фильма и портрет человека из базы TMDB: "
            "чужих фото почти не бывает, но меньше живых кадров и видео. Видео трейлера к новости о трейлере "
            "остаётся.\n\n"
            f"🎨 <b>Картинка ИИ</b> ({'вкл' if art else 'выкл'}) — если ни фото, ни постера не нашлось, ИИ рисует "
            "иллюстрацию (без лиц реальных людей, не постер) с пометкой «Иллюстрация · ИИ». "
            f"≈{shorts_budget.IMAGE_USD:.3f}$ за картинку, из общего лимита {shorts_budget.LIMITS['ai']:.0f}$ "
            "в месяц.\n" + "\n".join(await shorts_budget.status_lines()),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [_btn(("✅ " if not db else "") + "Из новости", "pt:pmode:live"),
                 _btn(("✅ " if db else "") + "Из базы фильмов", "pt:pmode:db")],
                [_btn(("✅ " if art else "") + "🎨 Картинка ИИ, если фото нет", "pt:pmode:art")],
                [_btn("⬅️ Назад", "pt:sec:news")]]))
        return
    if action == "auto":
        on = await shorts_setting("posts_auto") != "on"
        await set_shorts_setting("posts_auto", "on" if on else None)
        await message.answer(
            "🔔 Авто-посты включены: раз в 30 минут (кроме ночи, 0–8 по Екатеринбургу) пришлю новый пост по свежей "
            "новости из каналов. Нажми «📢 Опубликовать» под тем, что нравится. Выключить — та же кнопка."
            if on else "🔕 Авто-посты выключены.")
        return
    if action in ("make", "fast"):
        if post_lock.locked():
            await message.answer("⏳ Пост уже пишется — подожди минуту.")
            return
        wait = await message.answer(
            "📝 Читаю свежие посты каналов за сутки, ИИ выбирает новость и пишет пост…" if action == "make" else
            "⚡ Читаю Reddit, Deadline, Variety, инсайдеров и X за 12 часов — ИИ выбирает новость, которой ещё нет "
            "в русских каналах, и пишет пост…")

        async def job() -> None:
            try:
                topic = await make_tg_post(message.bot, [chat_id], "tg" if action == "make" else "fast")
                if topic:
                    await wait.delete()
                else:
                    await wait.edit_text("🤷 Новых новостей нет — всё уже было в прошлых постах.")
            except Exception as exc:  # noqa: BLE001
                logging.exception("Tg post failed")
                await wait.edit_text(f"❌ Пост не получился: <code>{escape(_safe_err(exc)[:300])}</code>")

        _run_job(job)


URL_RE = re.compile(r"https?://\S+|(?<!\w)t\.me/\S+")


DIGEST_HOURS = 72
DIGEST_NOTE = ("Это СВОДКА: материалов несколько, все об одной теме из разных каналов. Собери их в ОДИН пост: "
               "главное событие, затем важные подробности и реакции (кто что сказал — коротко, своими словами или "
               "одной фразой цитаты), без повторов одного и того же. Если в материалах есть противоречия в цифрах "
               "или датах — бери то, что подтверждает поиск, или не указывай. До 1000 символов.")


async def make_digest_post(bot: Bot, chat_id: int, topic: str) -> None:
    """«🧩 Сводка по теме»: посты всех каналов из списка о теме за 3 дня → один пост в стиле канала + фото."""
    names = [n.strip() for n in re.split(r"[/|]", topic) if n.strip()][:3]
    own = POST_CHANNEL.lstrip("@").lower()
    channels = [c for c in await fact_channels() if c.lower() != own]
    since = time.time() - DIGEST_HOURS * 3600
    posts = [p for p in await shorts_tgsearch.search(channels, names, per_channel=3)
             if not p.get("time") or p["time"] >= since]
    if not posts:
        raise RuntimeError(f"в каналах ({len(channels)}) за {DIGEST_HOURS // 24} дня нет постов про «{topic}». "
                           "Проверь написание или добавь английский вариант через «/»")
    posts.sort(key=lambda p: -(p.get("time") or 0))
    material = shorts_tgsearch.as_text(posts, 9000)
    res = await shorts_ai.rewrite_post(GEMINI_API_KEY, material, DIGEST_NOTE)
    if not res:
        raise RuntimeError(shorts_ai.last_error or "ИИ не написал пост")
    work = SHORTS_DIR / f"digest_{time.time_ns()}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        photos = await shorts_tgsearch.download_media(
            [{"image": p["image"]} for p in posts if p.get("image")], work, limit=4, prefix="dg")
        ids, kind = await _send_album(bot, chat_id, photos, res["post"], "#новости")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    used = sorted({p["channel"] for p in posts})
    await offer_post(bot, chat_id, ids, kind, "pt:dg",
                     what=f"Сводка готова: {len(posts)} постов из {len(used)} каналов")


async def make_link_list_post(bot: Bot, chat_id: int, raw: str, urls: list[str],
                              photos: list[Image.Image] | None = None) -> None:
    """«🔗 Подборка по ссылке или тексту»: фильмы ровно из источника автора → пост-подборка с карточками.
    Список часто только на постере — картинки поста (и присланные фото) тоже показываем ИИ."""
    texts = []
    image_urls: list[str] = []
    for url in urls:
        try:
            src = await shorts_linkfetch.fetch(url)
            texts.append(f"{src['title']}\n{src['text']}")
            image_urls += [m["image"] for m in src.get("media") or [] if m.get("image")] or (
                [src["image"]] if src.get("image") else [])
        except Exception as exc:  # noqa: BLE001
            logging.info("List link %s failed: %s", url, exc)
    rest = URL_RE.sub("", raw).strip()
    if rest:
        texts.append(rest)
    material = "\n\n".join(t for t in texts if t.strip())
    images = list(photos or []) + await _web_images(list(dict.fromkeys(image_urls)), 8)
    if len(material) < 20 and not images:
        raise RuntimeError("не получилось прочитать ссылку — вставь список фильмов текстом")
    try:
        plan = await shorts_ai.extract_plan(GEMINI_API_KEY, material, max_films=10)
    except shorts_ai.AIError:
        if not images:
            raise
        plan = None  # в тексте фильмов нет — читаем постеры
    if images and (plan is None or len(plan.get("films") or []) < 3):
        plan = await shorts_ai.extract_plan(GEMINI_API_KEY, material, 10, images[:8])
    films = plan.get("films") or []
    if len(films) < 2:
        raise RuntimeError("в источнике не нашлось списка фильмов")
    # сначала находим каждый фильм (постер, год, рейтинг) — год в посте берём из карточки, а не догадку ИИ
    found = [await _list_poster(f, [], []) for f in films]

    def line(f: dict[str, Any], card: dict[str, Any] | None) -> str:
        year = (card or {}).get("year") or f.get("year")
        kind = (f"сериал, {f['season']}-й сезон" if f.get("season") else "сериал") if f.get("series") else "фильм"
        rating, _ = _trusted_rating(card) if card else (0, "")
        return (f"«{f['ru'] or f['en']}» — {kind}" + (f", год {'старта ' if f.get('series') else ''}{year}" if year
                                                       else ", год неизвестен — НЕ пиши год")
                + (f", рейтинг {rating:.1f}" if rating else "") + (f", ориг. {f['en']}" if f.get("en") else "")
                + (f". {f['note']}" if f.get("note") else ""))

    lines = [line(f, card) for f, (_, card) in zip(films, found)]
    today = datetime.now(MSK).date()
    res = await shorts_ai.write_list_post(GEMINI_API_KEY, plan.get("title") or "Подборка", lines, "", [],
                                          f"{today.day} {shorts_daily.MONTHS[today.month - 1]} {today.year}",
                                          n=len(films), only=True)
    post = _brand(res["post"] if res else "\n".join(
        [f"🍿 {str(plan.get('title') or 'ПОДБОРКА').upper()}", ""] + [f"{k}. {ln}" for k, ln in enumerate(lines, 1)]))
    post = _compact_list(post, 1020 - SIGN_LEN - len(" #подборка"))  # все фильмы источника — в подписи
    names = [f["ru"] or f["en"] for f in films]
    work = SHORTS_DIR / f"llist_{time.time_ns()}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        ids, kind = await _send_album(
            bot, chat_id, await _list_cards(work, [(n, u, fl) for n, (u, fl) in zip(names, found)]), post, "#подборка")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    await offer_post(bot, chat_id, ids, kind, "pt:ll", names[:10], "Подборка по ссылке готова")


@router.message(PostState.listlink, ~F.text.startswith("/"))
async def post_list_link(message: Message, state: FSMContext) -> None:
    await state.clear()
    raw = message.text or message.caption or ""
    photos: list[Image.Image] = []
    if message.photo:  # пересланный пост с постером или скриншот списка
        try:
            buf = await message.bot.download(message.photo[-1].file_id, destination=io.BytesIO())
            photos.append(Image.open(buf).convert("RGB"))
        except Exception:
            logging.exception("List link: photo download failed")
    urls = list(dict.fromkeys(
        [u.rstrip(").,»\"'") for u in URL_RE.findall(raw)]
        + [e.url for e in (message.entities or message.caption_entities or []) if e.type == "text_link" and e.url]))[:3]
    if not urls and len(raw) < 20 and not photos:
        await message.answer("Не вижу ни ссылки, ни списка. Нажми «🔗 Подборка по ссылке» и пришли ещё раз.")
        return
    if post_lock.locked():
        await message.answer("⏳ Пост уже пишется — подожди минуту и пришли ещё раз.")
        return
    wait = await message.answer("🔗 Читаю источник, ИИ собирает подборку и ищет постеры…")
    chat_id = message.chat.id

    async def job() -> None:
        try:
            async with post_lock:
                await make_link_list_post(message.bot, chat_id, raw, urls, photos)
            await wait.delete()
        except Exception as exc:  # noqa: BLE001
            logging.exception("List-by-link post failed")
            await wait.edit_text(f"❌ Подборка не получилась: <code>{escape(_safe_err(exc)[:300])}</code>")

    _run_job(job)


async def stop_jobs() -> str:
    """Остановить всё, что бот сейчас собирает по кнопкам (ролики, выпуск, озвучка, посты): задачи отменяются,
    ffmpeg внутри них завершается, очередь сборки освобождается. Клонирование голоса не трогаем — оно идёт
    в Lumean и просто ждёт ответа."""
    jobs = [t for t in shorts_jobs if not t.done() and "wait_ready" not in t.get_coro().__qualname__]
    for task in jobs:
        task.cancel()
    if jobs:
        await asyncio.wait(jobs, timeout=20)
    await clear_running()
    if not jobs:
        return "Сейчас ничего не собирается."
    return (f"⏹ Остановлено: {len(jobs)}. Уже озвученные фразы не пропадут — при следующей сборке "
            "возьмутся из кэша (заново оплатится только недоозвученное).")


@router.message(Command("stop"))
async def stop_command(message: Message) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    await message.answer(await stop_jobs())


@router.message(Command("narrate"))
async def narrate_command(message: Message, state: FSMContext) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(PostState.narrate)
    await message.answer("🎙 Пришли текст для озвучки сообщением или файлом .txt / .docx (до 20 000 знаков).\n"
                         "<i>Передумал — /cancel</i>")


@router.message(PostState.narrate, F.text & ~F.text.startswith("/") | F.document)
async def narrate_text(message: Message, state: FSMContext) -> None:
    """«Диктор»: текст → проверка русского языка → версия для голоса; озвучка — кнопкой."""
    text = message.text or ""
    if message.document:
        name = (message.document.file_name or "").lower()
        if not name.endswith((".txt", ".docx")) or (message.document.file_size or 0) > 5 * 1024 * 1024:
            await message.answer("Нужен файл .txt или .docx до 5 МБ — или просто вставь текст сообщением.")
            return
        buf = io.BytesIO()
        await message.bot.download(message.document.file_id, destination=buf)
        data = buf.getvalue()
        try:
            text = shorts_narrator.read_docx(data) if name.endswith(".docx") else data.decode("utf-8", "ignore")
        except Exception as exc:  # noqa: BLE001
            await message.answer(f"Не прочитал файл: <code>{escape(str(exc)[:200])}</code>")
            return
    await state.clear()
    if len(text.strip()) < 20:
        await message.answer("Текст слишком короткий.")
        return
    wait = await message.answer(f"✍️ Проверяю текст ({len(text)} знаков): имена, падежи, грамматика, ударения… "
                                "Обычно 1–3 минуты.")
    chat_id = message.chat.id

    async def narrate_job() -> None:
        try:
            await load_pronunciations()
            res = await shorts_narrator.prepare(GEMINI_API_KEY, text)
            await set_shorts_setting("narrator_last", json.dumps({"clean": res["clean"], "speech": res["speech"]},
                                                                 ensure_ascii=False))
            bot = message.bot
            rows, size = [], 0
            for fix in res["fixes"]:
                row = f"• {escape(fix)}"
                if size + len(row) > 3500:
                    break
                rows.append(row)
                size += len(row) + 1
            await bot.send_message(chat_id, "✍️ <b>Исправлено:</b>\n" + "\n".join(rows) if rows else
                                   "✍️ Ошибок в тексте не нашёл.")
            await bot.send_document(chat_id, BufferedInputFile(
                shorts_narrator.as_text(res["clean"]).encode("utf-8"), filename="text_ispravlen.txt"),
                caption="📄 Исправленный текст — для субтитров и публикации.")
            chars = sum(len(x) for x in res["speech"])
            await bot.send_document(chat_id, BufferedInputFile(
                shorts_narrator.as_text(res["speech"]).encode("utf-8"), filename="text_dlya_golosa.txt"),
                caption=f"🗣 Текст для голоса: числа словами, ударения (знак над гласной). {chars} знаков.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [_btn("🎙 Озвучить моим голосом", "pt:narv")]]))
            if res["stressed"] and shorts_daily.stress_mode != "off":
                await bot.send_message(chat_id, "Ударения в именах и редких словах — проверь: "
                                       + escape(", ".join(res["stressed"])[:3000])
                                       + "\nНеверное — <code>/say слово = словО</code>, потом пришли текст снова.")
            await wait.delete()
        except Exception as exc:  # noqa: BLE001
            logging.exception("Narrator failed")
            await wait.edit_text(f"❌ Не получилось: <code>{escape(_safe_err(exc)[:300])}</code>")

    _run_job(narrate_job)


async def narrator_voice(message: Message) -> None:
    try:
        saved = json.loads(await shorts_setting("narrator_last") or "{}")
    except ValueError:
        saved = {}
    speech = saved.get("speech") or []
    if not speech:
        await message.answer("Сначала пришли текст: ▶️ YouTube и видео → 🛠 Инструменты → «🎙 Диктор».")
        return
    status = await message.answer("🎙 Озвучиваю твоим голосом" + (" (жду, пока соберётся другой ролик)…"
                                                                   if shorts_lock.locked() else "…"))
    chat_id = message.chat.id

    async def voice_job() -> None:
        work = SHORTS_DIR / f"narr_{time.time_ns()}"
        try:
            async with shorts_lock:
                await load_voice()
                res = await shorts_narrator.voice(speech, work)
                mp3 = work / "narration.mp3"
                await shorts_weekly._run("ffmpeg", "-v", "error", "-y", "-i", str(res["file"]), "-c:a", "libmp3lame",
                                         "-b:a", "192k", str(mp3))
                bot = message.bot
                note = f"\n💾 Из уже оплаченных: {res['hits']} из {res['total']} фраз." if res["hits"] else ""
                await bot.send_audio(chat_id, FSInputFile(mp3, filename="ozvuchka.mp3"),
                                     caption="🎙 Озвучка готова." + note + (f"\n{escape(res['note'][:300])}"
                                                                          if res["note"] else ""),
                                     request_timeout=600)
                if res["issues"]:
                    await bot.send_message(chat_id, "🎧 <b>Послушай эти места</b> — распознавание услышало не то, "
                                           "что в тексте:\n" + escape("\n".join(
                                               f"«{x['want']}» → «{x['got']}»" for x in res["issues"][:20]))
                                           + "\n\nЕсли и правда неверно: <code>/say слово = как читать</code>, пришли "
                                             "текст снова и озвучь — оплатятся только изменённые фразы.")
            await status.delete()
        except Exception as exc:  # noqa: BLE001
            logging.exception("Narrator voice failed")
            await status.edit_text(f"❌ Озвучка не получилась: <code>{escape(_safe_err(exc)[:300])}</code>")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    _run_job(voice_job)


@router.message(PostState.digest, F.text, ~F.text.startswith("/"))
async def post_digest(message: Message, state: FSMContext) -> None:
    await state.clear()
    topic = (message.text or "").strip()[:120]
    if post_lock.locked():
        await message.answer("⏳ Пост уже пишется — подожди минуту и нажми кнопку ещё раз.")
        return
    wait = await message.answer(f"🧩 Ищу «{escape(topic)}» во всех каналах, ИИ собирает сводку… (1–2 минуты)")
    chat_id = message.chat.id

    async def digest_job() -> None:
        try:
            async with post_lock:
                await make_digest_post(message.bot, chat_id, topic)
            await wait.delete()
        except Exception as exc:  # noqa: BLE001
            logging.exception("Digest post failed")
            await wait.edit_text(f"❌ Сводка не получилась: <code>{escape(_safe_err(exc)[:300])}</code>")

    _run_job(digest_job)


@router.message(PostState.actor, F.text, ~F.text.startswith("/"))
async def post_actor(message: Message, state: FSMContext) -> None:
    await state.clear()
    query = (message.text or "").strip()[:100]
    if post_lock.locked():
        await message.answer("⏳ Пост уже пишется — подожди минуту и нажми кнопку ещё раз.")
        return
    wait = await message.answer(f"🎭 Ищу «{escape(query)}»: фильмы, сериалы, роли и портрет…")
    chat_id = message.chat.id

    async def actor_job() -> None:
        try:
            async with post_lock:
                await make_actor_post(message.bot, chat_id, query)
            await wait.delete()
        except Exception as exc:  # noqa: BLE001
            logging.exception("Actor post failed")
            await wait.edit_text(f"❌ Пост не получился: <code>{escape(_safe_err(exc)[:300])}</code>")

    _run_job(actor_job)


@router.message(PostState.link, ~F.text.startswith("/"))
async def post_from_link(message: Message, state: FSMContext) -> None:
    """«🔗 Пост по ссылке»: ссылки (сайты, посты каналов), пересланный пост или текст → пост в стиле канала."""
    await state.clear()
    raw = message.text or message.caption or ""
    photos: list[Image.Image] = []
    if message.photo:  # пересланный пост с постером или скриншот списка
        try:
            buf = await message.bot.download(message.photo[-1].file_id, destination=io.BytesIO())
            photos.append(Image.open(buf).convert("RGB"))
        except Exception:
            logging.exception("List link: photo download failed")
    urls = list(dict.fromkeys(
        [u.rstrip(").,»\"'") for u in URL_RE.findall(raw)]
        + [e.url for e in (message.entities or message.caption_entities or []) if e.type == "text_link" and e.url]))[:5]
    note = URL_RE.sub("", raw).strip() if urls else ""
    own_media = None  # пересланный пост с фото/видео — берём его же файл
    if message.photo:
        own_media = ("photo", message.photo[-1].file_id)
    elif message.video:
        own_media = ("video", message.video.file_id)
    if not urls and len(raw) < 60:
        await message.answer("Не вижу ссылки или текста новости. Нажми «🔗 Пост по ссылке» и пришли ещё раз.")
        return
    wait = await message.answer("🔗 Читаю " + (f"ссылки ({len(urls)})" if len(urls) > 1 else "ссылку" if urls
                                              else "текст") + ", ИИ пишет пост…")

    async def job() -> None:
        try:
            async with post_lock:
                await _make_link_post(message, urls, note, raw if not urls else "", own_media)
            await wait.delete()
        except Exception as exc:  # noqa: BLE001
            logging.exception("Link post failed")
            await wait.edit_text(f"❌ Пост не получился: <code>{escape(_safe_err(exc)[:300])}</code>")

    _run_job(job)


async def _make_link_post(message: Message, urls: list[str], note: str, text: str,
                          own_media: tuple[str, str] | None) -> None:
    bot, chat_id = message.bot, message.chat.id
    sources, failed = [], []
    for url in urls:
        try:
            sources.append(await shorts_linkfetch.fetch(url))
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{url[:60]} — {_safe_err(exc)[:80]}")
    if text:
        sources.append({"url": "", "title": "", "text": text, "image": None, "video": None, "thumb": None})
    if not sources:
        raise RuntimeError("ни одна ссылка не открылась:\n" + "\n".join(failed))
    material = "\n\n".join(f"[{s['url'] or 'текст автора'}]\n{s['title']}\n{s['text']}" for s in sources)
    pics: list[Image.Image] = []  # картинки источника — ИИ видит, что на них (часто там сам список фильмов)
    pic_dir = SHORTS_DIR / f"linkpic_{time.time_ns()}"
    pic_dir.mkdir(parents=True, exist_ok=True)
    try:
        imgs = [m for s in sources for m in (s.get("media") or ([{"image": s["image"]}] if s.get("image") else []))
                if m.get("image")]
        for path in await shorts_tgsearch.download_media(imgs[:3], pic_dir, limit=3, prefix="v"):
            try:
                pics.append(await asyncio.to_thread(lambda p=path: Image.open(p).convert("RGB")))
            except Exception:  # noqa: BLE001
                pass
    finally:
        shutil.rmtree(pic_dir, ignore_errors=True)
    res = await shorts_ai.rewrite_post(GEMINI_API_KEY, material, note, images=pics)
    if not res:
        raise RuntimeError(shorts_ai.last_error or "ИИ не написал пост")
    caption = post_caption(res["post"], "#новости")
    work = SHORTS_DIR / f"link_{time.time_ns()}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        if own_media:
            send = bot.send_photo if own_media[0] == "photo" else bot.send_video
            ids, kind = [(await send(chat_id, own_media[1], caption=caption)).message_id], "caption"
        else:
            # все фото и видео источника (альбом поста) → видео источника → трейлер с Rutube (новость о трейлере)
            # → фото источника → кадр Кинопоиска → превью видео
            files = await shorts_tgsearch.download_media(
                [m for s in sources for m in s.get("media") or []], work, limit=10, prefix="alb")
            if len(files) < 2:
                files = await shorts_tgsearch.download_media(
                    [{"video": s["video"]} for s in sources if s.get("video")], work, limit=1, prefix="link")
            yt = next((u for u in urls if re.search(r"youtube\.com/watch|youtu\.be/|youtube\.com/shorts", u)), "")
            if not files and yt:  # ссылка на YouTube (новый трейлер студии) — именно этот ролик
                try:
                    files = [await asyncio.wait_for(shorts_clips.download_trailer(yt, work / "youtube.mp4"), 300)]
                except Exception as exc:  # noqa: BLE001 — YouTube бывает не отдаёт видео серверу: тогда обложка
                    logging.info("Link post: YouTube video %s failed: %s", yt, exc)
            # Rutube — только если ссылка не на YouTube: новый трейлер выходит сначала там, а на Rutube за те же
            # две недели может найтись предыдущий трейлер того же фильма
            if not files and not yt and res["trailer"] and res["film"]:
                trailer = await _rutube_post_trailer([res["film"], res["film_en"]], work,
                                                     _season_of(res["post"]), fresh=True)
                files = [trailer] if trailer else []
            if not files:
                files = await shorts_tgsearch.download_media(
                    [{"image": s["image"]} for s in sources if s.get("image")], work, limit=1, prefix="img")
            if not files and res["film"]:
                film = await shorts_daily.find_film(kp._get, res["film"])
                url = shorts_daily._image_url(film) if film else None
                files = await shorts_tgsearch.download_media([{"image": url}], work, limit=1, prefix="kp") if url else []
            if not files:
                files = await shorts_tgsearch.download_media(
                    [{"image": s["thumb"]} for s in sources if s.get("thumb")], work, limit=1, prefix="thumb")
            ids, kind = await _send_media(bot, chat_id, files, caption)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if failed:
        await bot.send_message(chat_id, "⚠️ Не открылись:\n" + escape("\n".join(failed)))
    await offer_post(bot, chat_id, ids, kind, "pt:lk", what="Пост по ссылке готов")


@router.message(PostState.media, F.photo | F.video | F.animation)
async def post_media_collect(message: Message, state: FSMContext) -> None:
    """Фото/видео автора для готового поста. Альбом Telegram присылает отдельными сообщениями почти разом —
    копим в памяти (без await между чтением и записью, иначе второе фото альбома затирало первое) и отвечаем
    один раз на альбом, когда пришли все его части."""
    chat_id = message.chat.id
    buf = _media_buf.get(chat_id)
    if not buf:
        first = (await state.get_data()).get("post_first")
        if not first:
            await state.clear()
            return
        buf = _media_buf.setdefault(chat_id, {"first": first, "items": [], "groups": set()})
    if message.photo:
        buf["items"].append({"type": "photo", "id": message.photo[-1].file_id})
    elif message.video:
        buf["items"].append({"type": "video", "id": message.video.file_id})
    elif message.animation:
        buf["items"].append({"type": "animation", "id": message.animation.file_id})
    group = message.media_group_id or ""
    if group:
        if group in buf["groups"]:
            return  # остальные части того же альбома — без лишних сообщений
        buf["groups"].add(group)
        await asyncio.sleep(1.5)  # ждём остальные части альбома, чтобы назвать верное число
    first = buf["first"]
    await message.answer(
        f"Получено: {min(len(buf['items']), 10)}. Можно прислать ещё — или выбери:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_btn("🔁 Заменить медиа в посте", f"pt:mr:{first}")],
            [_btn("➕ Добавить к текущим", f"pt:ma:{first}")]]))


_media_buf: dict[int, dict[str, Any]] = {}  # чат → медиа, присланные для замены в посте


async def _post_parts(bot: Bot, chat_id: int, ids: list[int]) -> tuple[str, list[dict[str, str]]]:
    """Текст (HTML) и медиа готового поста: Bot API не отдаёт сообщения по номеру — пересылаем их себе,
    читаем и удаляем пересланное."""
    caption, media = "", []
    for mid in ids:
        try:
            fwd = await bot.forward_message(chat_id, chat_id, mid, disable_notification=True)
        except TelegramBadRequest:
            continue
        try:
            if not caption and (fwd.caption or fwd.text):
                caption = fwd.html_text
            if fwd.photo:
                media.append({"type": "photo", "id": fwd.photo[-1].file_id})
            elif fwd.video:
                media.append({"type": "video", "id": fwd.video.file_id})
            elif fwd.animation:
                media.append({"type": "animation", "id": fwd.animation.file_id})
        finally:
            try:
                await bot.delete_message(chat_id, fwd.message_id)
            except TelegramBadRequest:
                pass
    return caption, media


async def post_set_media(bot: Bot, chat_id: int, first: int, new: list[dict[str, str]], add: bool,
                         kb_message: int | None = None) -> None:
    """Пост с новыми фото/видео: текст тот же, медиа — присланные (замена) или старые + присланные; старый
    вариант поста удаляется, новый — с теми же кнопками (опубликовать, позже, поправить…)."""
    meta = await _post_meta(chat_id, first)
    caption, old = await _post_parts(bot, chat_id, meta["ids"])
    media = ((old if add else []) + new)[:10]
    if len(media) >= 2:
        media = [m for m in media if m["type"] != "animation"] or media[:1]  # GIF в альбом Telegram не берёт
    long_text = len(caption) > 1024  # подпись к фото — до 1024 знаков: длинный текст идёт сообщением под медиа
    body, caption = (caption, None) if long_text else ("", caption)
    try:
        if len(media) >= 2:
            items = [(InputMediaVideo(media=m["id"], supports_streaming=True, caption=caption if k == 0 else None)
                      if m["type"] == "video" else InputMediaPhoto(media=m["id"], caption=caption if k == 0 else None))
                     for k, m in enumerate(media)]
            ids = [x.message_id for x in await bot.send_media_group(chat_id, items)]
        elif media and media[0]["type"] == "video":
            ids = [(await bot.send_video(chat_id, media[0]["id"], caption=caption, supports_streaming=True)).message_id]
        elif media and media[0]["type"] == "animation":
            ids = [(await bot.send_animation(chat_id, media[0]["id"], caption=caption)).message_id]
        elif media:
            ids = [(await bot.send_photo(chat_id, media[0]["id"], caption=caption)).message_id]
        else:
            return
        if long_text:
            ids.append((await bot.send_message(chat_id, body, disable_web_page_preview=True)).message_id)
    except TelegramBadRequest as exc:
        await bot.send_message(chat_id, f"❌ Не получилось: <code>{escape(_safe_err(exc)[:200])}</code>")
        return
    was_queued = await _unqueue(chat_id, first)
    extra = {k: v for k, v in meta.items() if k not in ("ids", "kind", "poll", "question", "quiz", "text_id")}
    if long_text:
        extra["text_id"] = ids[-1]  # «Поправить текст» правит это сообщение, а не подпись к фото
    await save_post(chat_id, ids, "text" if long_text else "caption", meta.get("poll") or [],
                    meta.get("question") or POLL_QUESTION, meta.get("quiz"), extra)
    for mid in meta["ids"] + ([kb_message] if kb_message else []):  # старый вариант поста и его кнопки
        try:
            await bot.delete_message(chat_id, mid)
        except TelegramBadRequest:
            pass
    await bot.send_message(chat_id, "👆 Пост с новыми медиа." + (
        " Он стоял в расписании — выбери время заново («⏰ Позже»)." if was_queued else ""),
        reply_markup=post_keyboard(ids[0], yt=bool(meta.get("yt"))))


@router.message(PostState.edit, F.text, ~F.text.startswith("/"))
async def post_edit_text(message: Message, state: FSMContext) -> None:
    first = (await state.get_data()).get("post_first")
    await state.clear()
    if not first:
        return
    meta = await _post_meta(message.chat.id, first)
    caption = post_caption(message.text or "")
    try:
        if meta["kind"] == "text":
            await message.bot.edit_message_text(caption, chat_id=message.chat.id,
                                                message_id=meta.get("text_id") or first,
                                                disable_web_page_preview=True)
        else:
            await message.bot.edit_message_caption(chat_id=message.chat.id, message_id=first, caption=caption)
    except TelegramBadRequest as exc:
        await message.answer(f"❌ Не получилось поправить: <code>{escape(_safe_err(exc)[:200])}</code>")
        return
    await message.answer("✅ Текст поста обновлён.", reply_markup=post_keyboard(first, yt=bool(meta.get("yt"))))


# ---------------------------------------------------------------- история персонажа (лор)

LORE_TIMEOUT = 20 * 60
LORE_MEDIA_MAX = 24


async def lore_ask_name(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(LoreState.name)
    await message.answer(
        "📖 <b>История персонажа</b>\n\nНапиши, про кого ролик: <code>Доктор Дум</code>, "
        "<code>Дэдпул</code>, <code>Джокер</code>…\n\n<i>Передумал — /cancel</i>")


async def lore_ask_hook(message: Message, state: FSMContext, name: str) -> None:
    await state.update_data(lore_name=name, lore_hook="", lore_script=None, lore_media=[])
    await state.set_state(LoreState.hook)
    await message.answer(
        f"📖 <b>{escape(name)}</b>\n\nЕсть новость-зацепка для начала ролика? Например: "
        "<code>Роберт Дауни-младший сыграет Доктора Дума</code>. Напиши её — или жми «Без новости».",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("➡️ Без новости", "lo:nohook")],
                                                           [_btn("❌ Отмена", "lo:cancel")]]))


async def news_ask_text(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(LoreState.news)
    await message.answer(
        "📰 <b>Своя новость</b>\n\nВставь текст новости целиком — как есть, хоть с цитатами и хэштегами. "
        "ИИ сократит его до ролика на минуту (ничего не добавляя от себя), сам подберёт фото человека "
        "и кадры фильмов из новости. Дальше можно поправить текст и добавить свои фото.\n\n"
        "<i>Передумал — /cancel</i>")


async def news_generate(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    wait = await message.answer("✍️ Сокращаю текст и подбираю фото, обычно до минуты…")
    try:
        script = await shorts_lore.write_news(data.get("news_text") or "", GEMINI_API_KEY, TMDB_API_KEY)
    except Exception as exc:
        logging.exception("News script failed")
        await wait.edit_text(f"❌ Не получилось: <code>{escape(_safe_err(exc)[:400])}</code>",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [_btn("🔄 Ещё раз", "lo:redo")], [_btn("❌ Отмена", "lo:cancel")]]))
        return
    await wait.delete()
    await state.update_data(lore_name=script["name"], lore_script=script)
    await show_lore(message, state)
    if script.get("missing"):
        await message.answer("🔎 Не нашёл на TMDB кадров к: " + ", ".join(f"«{escape(m)}»" for m in script["missing"])
                             + ". К этим фразам встанут общие кадры — свои добавь кнопкой «📎 Добавить свои картинки».")
    if len(script.get("images") or []) < 6:
        await message.answer(f"🖼 Нашёл только {len(script.get('images') or [])} фото — добавь свои кнопкой "
                             "«📎 Добавить свои картинки», иначе картинки будут повторяться.")


async def lore_generate(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if data.get("lore_kind") == "news":
        await news_generate(message, state)
        return
    if data.get("lore_kind") == "own":
        await own_generate(message, state)
        return
    wait = await message.answer("✍️ Читаю Википедию и пишу сценарий, обычно до минуты…")
    try:
        script = await shorts_lore.write_script(data["lore_name"], data.get("lore_hook") or "", GEMINI_API_KEY)
    except Exception as exc:
        logging.exception("Lore script failed")
        await wait.edit_text(f"❌ Сценарий не написался: <code>{escape(_safe_err(exc)[:400])}</code>",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [_btn("🔄 Ещё раз", "lo:redo")], [_btn("❌ Отмена", "lo:cancel")]]))
        return
    await wait.delete()
    await state.update_data(lore_script=script)
    await show_lore(message, state)


async def lore_style() -> str:
    """Монтаж шортса «свой сценарий / история / новость»: dynamic — видео на весь экран, кадр каждые ~1,8 с,
    переходы (рывок, наезд, рваная бумага, вспышка), субтитры по одному слову, без заставки; calm — как раньше."""
    return await shorts_setting("lore_style") or "dynamic"


# ссылка кончается на пробеле, «»», «)» или «]» — markdown «[текст](https://…)» больше не склеивается в одну ссылку
IMG_URL = re.compile(r"(?:(?<!\d)(\d{1,2})\s*[.:)\-—]?\s*)?(https?://[^\s\]\)>»\"]+)")


def search_query(url: str) -> str:
    """Ссылка на поиск картинок (Google, Яндекс, Bing) → сам запрос: «Doctor Doom Latveria throne»."""
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    if not any(h in host for h in ("google.", "yandex.", "bing.com", "duckduckgo.com")):
        return ""
    params = dict(parse_qsl(parts.query))
    return (params.get("q") or params.get("text") or "").strip()


STOCK = ("shutterstock", "alamy", "gettyimages", "istockphoto", "dreamstime", "depositphotos", "123rf", "adobe.com",
         "pinterest", "vecteezy")  # фотостоки с водяными знаками и Pinterest (уменьшенные копии)


async def ddg_images(session: aiohttp.ClientSession, query: str, limit: int = 12) -> list[dict[str, str]]:
    """Картинки DuckDuckGo по запросу (без ключа): [{"image", "thumb", "title"}], без фотостоков с водяными
    знаками. DuckDuckGo иногда отказывает частым запросам — тогда пусто."""
    page, _ = await netsafe._get(session, "https://duckduckgo.com/?ia=images&iax=images&q=" + quote_plus(query),
                                 2 * 1024 * 1024, "")
    m = re.search(rb"vqd=[\"']?([\d-]+)", page)
    if not m:
        return []
    api = ("https://duckduckgo.com/i.js?l=wt-wt&o=json&f=,,,,,&p=1&q=" + quote_plus(query)
           + "&vqd=" + m.group(1).decode())
    raw, _ = await netsafe._get(session, api, 3 * 1024 * 1024, "")
    out = []
    for r in json.loads(raw.decode("utf-8", "ignore")).get("results") or []:
        image, thumb = str(r.get("image") or ""), str(r.get("thumbnail") or "")
        if not image.startswith("http") or any(x in (image + str(r.get("url") or "")).lower() for x in STOCK):
            continue
        if int(r.get("width") or 0) and int(r.get("width") or 0) < 500:
            continue  # маленькая картинка в ролике будет мылом
        out.append({"image": image, "thumb": thumb or image, "title": str(r.get("title") or "")})
        if len(out) >= limit:
            break
    return out


async def pick_searched_image(session: aiohttp.ClientSession, query: str, phrase: str) -> bytes | None:
    """Как человек в поиске картинок: смотрим выдачу и берём ту, что подходит к фразе (выбирает Gemini по
    миниатюрам). Ничего не подошло — None."""
    from PIL import Image as PILImage
    try:
        found = await ddg_images(session, query)
    except Exception as exc:  # noqa: BLE001
        logging.info("DDG images %s failed: %s", query, exc)
        return None
    thumbs, cands = [], []
    for item in found:
        try:
            raw, ctype = await netsafe._get(session, item["thumb"], 2 * 1024 * 1024, "")
            thumbs.append(PILImage.open(io.BytesIO(raw)).convert("RGB"))
            cands.append(item)
        except Exception:  # noqa: BLE001
            continue
    if not cands:
        return None
    order = list(range(len(cands)))
    choice = await shorts_ai.match_images(GEMINI_API_KEY, [phrase or query], thumbs)
    if choice and 0 <= choice[0] < len(cands):
        order.remove(choice[0])
        order.insert(0, choice[0])
    elif choice is not None:
        return None  # ИИ посмотрел и сказал, что ни одна не подходит
    for k in order[:3]:  # лучшая; не скачалась — следующая
        try:
            raw, ctype = await netsafe._get(session, cands[k]["image"], 15 * 1024 * 1024, "")
            if ctype.startswith("image/"):
                return raw
        except Exception:  # noqa: BLE001
            continue
    return None


async def commons_image(session: aiohttp.ClientSession, query: str) -> str:
    """Картинка по запросу из Wikimedia Commons (бесплатные лицензии): адрес первой подходящей jpg/png."""
    api = ("https://commons.wikimedia.org/w/api.php?action=query&format=json&generator=search&gsrnamespace=6"
           "&gsrlimit=8&prop=imageinfo&iiprop=url|mime&iiurlwidth=1600&gsrsearch=" + quote_plus(query))
    raw, _ = await netsafe._get(session, api, 2 * 1024 * 1024, "")
    pages = (json.loads(raw.decode("utf-8", "ignore")).get("query") or {}).get("pages") or {}
    # главные слова запроса (без «Marvel», «comics», «art»…) должны быть в названии файла — иначе Commons отдаёт
    # что-то общее, а случайная картинка хуже, чем кадр из видео автора
    generic = {"marvel", "comics", "comic", "art", "official", "still", "photo", "movie", "film", "poster", "the", "and"}
    words = [w for w in re.findall(r"[a-zа-яё0-9]+", query.lower()) if len(w) > 2 and w not in generic][:3]
    for page in sorted(pages.values(), key=lambda p: p.get("index", 99)):
        info = (page.get("imageinfo") or [{}])[0]
        title = str(page.get("title") or "").lower()
        if words and not all(w in title for w in words[:2]):
            continue
        if str(info.get("mime") or "").startswith(("image/jpeg", "image/png", "image/webp")):
            return info.get("thumburl") or info.get("url") or ""
    return ""


async def fetch_image_url(url: str, path: Path, phrase: str = "") -> None:
    """Картинка по ссылке: прямая ссылка на файл — как есть; страница — её главная картинка (og:image)."""
    # Wikimedia требует понятный User-Agent с контактом — иначе отвечает 403
    async with netsafe.safe_session(timeout=aiohttp.ClientTimeout(total=40),
                                    headers={"User-Agent": "KinoMir360-bot/1.0 (+https://t.me/KinoMir360) Mozilla/5.0"}) \
            as session:
        query = search_query(url)
        raw = b""
        if query:  # ссылка на поиск Google — Google ботов не пускает: ищем в DuckDuckGo и выбираем по фразе
            raw = await pick_searched_image(session, query, phrase) or b""
            if raw:
                ctype = "image/"
            else:  # запасной — Wikimedia Commons (только если в названии файла слова запроса)
                found = await commons_image(session, query)
                if not found:
                    raise RuntimeError(f"по запросу «{query}» подходящей картинки не нашлось")
                url = found
        if not raw:
            raw, ctype = await netsafe._get(session, url, 15 * 1024 * 1024, "")
        if not ctype.startswith("image/"):
            html = raw[:2_000_000].decode("utf-8", errors="ignore")
            m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', html) or \
                re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image', html)
            if not m:
                raise RuntimeError("на странице нет картинки")
            raw, ctype = await netsafe._get(session, urljoin(url, m.group(1).replace("&amp;", "&")),
                                            15 * 1024 * 1024, "")
            if not ctype.startswith("image/"):
                raise RuntimeError("это не картинка")
    from PIL import Image as PILImage
    img = PILImage.open(io.BytesIO(raw))
    img.convert("RGB").save(path.with_suffix(".jpg") if path.suffix != ".jpg" else path, quality=92)


async def lore_media_mode() -> str:
    """Свои картинки и видео автора: own — ролик только из них; mix — вместе с найденными ботом (ИИ выбирает
    лучшее под каждую фразу и в своих, и в найденных)."""
    return await shorts_setting("lore_media_mode") or "own"


async def show_lore(message: Message, state: FSMContext, brief: bool = False) -> None:
    data = await state.get_data()
    script = data["lore_script"]
    media = data.get("lore_media") or []
    chars = sum(len(x) for x in script["lines"])
    source = (f"<a href=\"{escape(script['url'])}\">{escape(script.get('article') or 'Википедия')}</a>"
              if script.get("url") else escape(script.get("article") or "—"))
    photos = f" • фото найдено: {len(script.get('images') or [])}" if script.get("kind") == "news" else ""
    pinned = shorts_lore.fixed_lines(script, script["lines"])
    if pinned:
        photos += "\n📌 " + ", ".join(f"{i + 1} — {escape(str(item.get('who') or ''))}"
                                     for i, item in sorted(pinned.items()))
    head = (f"📝 <b>{escape(script.get('title') or data['lore_name'])}</b>\n"
            f"{chars} символов ≈ {round(chars / 15)} с • источник: {source}{photos}\n"
            "Каждая строка — отдельная картинка в ролике.")
    body = "" if brief else "\n\n" + escape(shorts_lore.script_text(script))
    dynamic = await lore_style() == "dynamic"
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("✅ Собрать шортс", "lo:go")], [_btn("✏️ Поправить текст", "lo:edit")],
        [_btn("⚡ Монтаж: динамичный (сменить)" if dynamic else "🎞 Монтаж: спокойный (сменить)", "lo:style")],
        [_btn(f"📎 Свои картинки и видео ({len(media)})" if media else "📎 Добавить свои картинки и видео", "lo:media")],
        [_btn("🖼 Кадры: только мои (сменить)" if await lore_media_mode() == "own" else
              "🤝 Кадры: мои + найденные ИИ (сменить)", "lo:mix")],
        [_btn("🔄 Написать заново", "lo:redo")], [_btn("❌ Отмена", "lo:cancel")]])
    await message.answer((head + body)[:4000], reply_markup=markup, disable_web_page_preview=True)


async def run_lore(bot: Bot, chat_id: int, data: dict[str, Any], status: Message) -> None:
    async with shorts_lock:
        await load_pronunciations()
        await load_voice()
        await load_fact_channels()
        work = SHORTS_DIR / datetime.now(MSK).strftime("lore_%Y%m%d_%H%M%S")
        work.mkdir(parents=True, exist_ok=True)
        try:
            music = await custom_music(bot, work)
            await set_shorts_setting("running", json.dumps({"chats": [chat_id], "theme": "история персонажа"}))
            files: list[Path] = []
            pins: dict[int, int] = {}  # своя картинка -> номер фразы из подписи
            lost: list[str] = []
            for k, item in enumerate(data.get("lore_media") or []):
                path = work / f"user{k}{item['ext']}"
                try:
                    if item.get("url"):  # картинка по ссылке автора — скачиваем сами
                        lines = (data.get("lore_script") or {}).get("lines") or []
                        line = int(item.get("line") or 0)
                        await fetch_image_url(item["url"], path, lines[line - 1] if 0 < line <= len(lines) else "")
                    else:
                        await bot.download(item["file_id"], destination=path)
                    if item.get("line"):
                        pins[len(files)] = int(item["line"])
                    files.append(path)
                except Exception as exc:  # noqa: BLE001
                    logging.info("Lore: user file %s failed: %s", item.get("url") or k, exc)
                    lost.append((f"к фразе {item['line']}: " if item.get("line") else "") + str(item.get("url") or k)[:80])
            if lost:
                await bot.send_message(chat_id, "⚠️ Не скачались картинки (ссылка не открылась или это не картинка):\n"
                                       + "\n".join(f"• {escape(x)}" for x in lost[:10])
                                       + "\nНа этих фразах будут другие кадры.")
            progress = progress_updater(status)
            shorts_daily.on_progress, shorts_daily.last_stage = progress, ""
            script = dict(data["lore_script"], style=await lore_style(), media_mode=await lore_media_mode())
            result = await asyncio.wait_for(shorts_lore.make_lore(
                work, script, music, files, GEMINI_API_KEY, pins), timeout=LORE_TIMEOUT)
            shorts_daily.add_english_seo(result)
            await progress(100, "Готово — отправляю ролик")
            await send_short(bot, chat_id, result)
        except asyncio.TimeoutError:
            raise RuntimeError(f"сборка шла дольше {LORE_TIMEOUT // 60} минут — "
                               f"застряла на этапе: {shorts_daily.last_stage or 'начало'}.") from None
        finally:
            shorts_daily.on_progress = None
            await clear_running()
            shutil.rmtree(work, ignore_errors=True)


@router.message(Command("lore"))
async def lore_command(message: Message, state: FSMContext) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    name = (message.text or "").partition(" ")[2].strip()
    if name:
        await state.clear()
        await lore_ask_hook(message, state, name)
    else:
        await lore_ask_name(message, state)


@router.message(LoreState.wwk, F.text, ~F.text.startswith("/"))
async def wwk_film(message: Message, state: FSMContext) -> None:
    """«🔮 Что мы знаем о фильме»: ИИ с поиском пишет сценарий по схеме, дальше — как «свой сценарий»."""
    film = _typed(message).strip(" «»\"")[:80]
    if len(film) < 2:
        await message.answer("Напиши название фильма или сериала.")
        return
    await state.set_state(None)
    wait = await message.answer(f"🔮 Ищу новости и слухи о «{escape(film)}» и пишу текст, обычно 1–2 минуты…")
    # главный материал — свежие посты кино-каналов (за месяц), с датами: так в ролик не попадут старые новости
    material = ""
    try:
        await load_fact_channels()
        posts = await asyncio.wait_for(shorts_tgsearch.search(shorts_daily.fact_channels, [film], per_channel=4), 60)
        cutoff = time.time() - 31 * 86400
        posts = sorted((p for p in posts if (p.get("time") or 0) >= cutoff), key=lambda p: -(p.get("time") or 0))
        material = "\n\n".join(f"{datetime.fromtimestamp(p['time'], MSK):%Y-%m-%d} [t.me/{p['channel']}]\n{p['text']}"
                                 for p in posts[:20])[:9000]
    except Exception:  # noqa: BLE001 — без постов ИИ ищет сам
        logging.exception("wwk: tg search failed")
    script = await shorts_ai.whatweknow_script(GEMINI_API_KEY, film, datetime.now(MSK).strftime("%d.%m.%Y"),
                                               material)
    if script and not script["lines"]:
        await wait.edit_text(f"🔮 Свежих новостей о «{escape(film)}» за последний месяц почти нет"
                             + (f" (старых отброшено: {script['stale']})" if script.get("stale") else "")
                             + " — ролик вышел бы из общеизвестного. Попробуй ближе к премьере или другой фильм.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("🔮 Другой фильм", "lo:wwk")]]))
        return
    if not script:
        await wait.edit_text("❌ Не получилось собрать текст — ИИ не ответил или нашёл слишком мало. "
                             "Попробуй ещё раз или уточни название.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("🔮 Ещё раз", "lo:wwk")]]))
        return
    await wait.delete()
    body = (f"**Заголовок:** {script['title']}\n" if script.get("title") else "") + "\n".join(script["lines"])
    await state.update_data(own_scripts=[(f"Что мы знаем — {film}", body)], own_pick=0, lore_kind="own",
                            lore_name="Что мы знаем", lore_hook="", lore_script=None, lore_media=[])
    await own_generate(message, state)
    if script.get("stale"):
        await message.answer(f"🗑 Старых новостей (больше {shorts_ai.WWK_FRESH_DAYS} дней) отброшено: {script['stale']}. "
                             "Если что-то всё равно давно известно — «✏️ Поправить текст».")


@router.message(LoreState.own, F.text & ~F.text.startswith("/") | F.document)
async def own_script_text(message: Message, state: FSMContext) -> None:
    """«🎬 Свой сценарий»: текст или файл .md/.txt; несколько сценариев в файле — кнопки выбора."""
    text = message.text or ""
    if message.document:
        name = (message.document.file_name or "").lower()
        if not name.endswith((".md", ".txt", ".docx")) or (message.document.file_size or 0) > 5 * 1024 * 1024:
            await message.answer("Нужен файл .md, .txt или .docx до 5 МБ — или вставь сценарий сообщением.")
            return
        buf = io.BytesIO()
        await message.bot.download(message.document.file_id, destination=buf)
        text = docx_text(buf.getvalue()) if name.endswith(".docx") else buf.getvalue().decode("utf-8", "ignore")
    if looks_like_task(text) or (await state.get_data()).get("task_buf"):
        await task_collect(message, state, text)  # задание целиком (озвучка + кадры + надписи) — разбирает ИИ
        return
    scripts = shorts_lore.split_scripts(text)
    if not any(len(shorts_lore.parse_script(body)["lines"]) >= 2 for _, body in scripts):
        await message.answer("Не нашёл фраз. Пришли таблицей «Время | Голос | На экране» или фразу на строку.")
        return
    await state.set_state(None)
    await state.update_data(own_scripts=scripts[:10], own_pick=0, lore_kind="own", lore_name="Свой сценарий",
                            lore_hook="", lore_script=None, lore_media=[])
    if len(scripts) == 1:
        await own_generate(message, state)
        return
    await message.answer("В файле несколько сценариев — какой собрать?", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[_btn(f"🎬 {head[:50]}", f"lo:ownpick:{k}")] for k, (head, _) in enumerate(scripts[:10])]))


def docx_text(raw: bytes) -> str:
    """Текст из .docx без лишних библиотек: абзацы и ячейки таблиц, ссылки-гиперссылки — адресом."""
    import zipfile
    from xml.etree import ElementTree
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
          "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        doc = ElementTree.fromstring(z.read("word/document.xml"))
        links = {}
        if "word/_rels/document.xml.rels" in z.namelist():
            for rel in ElementTree.fromstring(z.read("word/_rels/document.xml.rels")):
                links[rel.get("Id")] = rel.get("Target") or ""
    out = []
    for p in doc.iter(f"{{{ns['w']}}}p"):
        parts = []
        for node in p:
            if node.tag == f"{{{ns['w']}}}hyperlink":
                label = "".join(t.text or "" for t in node.iter(f"{{{ns['w']}}}t"))
                url = links.get(node.get(f"{{{ns['r']}}}id") or "", "")
                parts.append(f"{label} {url}".strip() if url and url not in label else label)
            else:
                parts.append("".join(t.text or "" for t in node.iter(f"{{{ns['w']}}}t")))
        out.append("".join(parts))
    return "\n".join(out)


def looks_like_task(text: str) -> bool:
    """Задание на ролик, а не голый текст: длинное, со сценарием озвучки и указаниями к кадрам или ссылками."""
    low = text.lower()
    return len(text) > 700 and any(w in low for w in ("озвучк", "сценари")) and \
        any(w in low for w in ("http", "кадр", "монтаж", "надпис"))


task_tokens: dict[int, int] = {}  # чат → номер последней части задания (длинное сообщение Telegram режет на части)


async def task_collect(message: Message, state: FSMContext, text: str) -> None:
    """Части задания копим 6 с: длинный текст приходит несколькими сообщениями; потом разбираем целиком."""
    buf = ((await state.get_data()).get("task_buf") or "") + "\n" + text
    await state.update_data(task_buf=buf[-60000:])
    chat = message.chat.id
    token = task_tokens.get(chat, 0) + 1
    task_tokens[chat] = token
    await asyncio.sleep(6)
    if task_tokens.get(chat) != token:
        return  # пришла ещё часть — разберёт она
    task = (await state.get_data()).get("task_buf") or ""
    await state.update_data(task_buf="")
    await state.set_state(None)
    await task_build(message, state, task)


async def task_build(message: Message, state: FSMContext, task: str) -> None:
    wait = await message.answer("📋 Читаю задание: озвучка, кадры по ссылкам, надписи, оформление…")
    plan = await shorts_ai.parse_task(GEMINI_API_KEY, task)
    if not plan:
        await wait.edit_text("❌ Не смог разобрать задание (ИИ не ответил или не нашёл текст озвучки). "
                             "Попробуй ещё раз или пришли только текст озвучки — фраза на строку.")
        return
    lines = plan["lines"]
    title = plan["title"] or "Свой сценарий"
    body = f"**Заголовок:** {title}\n" + "\n".join(x["text"] for x in lines)
    media = [{"url": x["url"], "ext": ".jpg", "line": k} for k, x in enumerate(lines, 1) if x["url"]]
    await state.update_data(own_scripts=[(title, body)], own_pick=0, lore_kind="own", lore_name=title,
                            lore_hook="", lore_script=None, lore_media=media,
                            task_labels=[x["label"] for x in lines])
    rows = [f"{k}. {escape(x['text'][:70])}" + (" 🔗" if x["url"] else "") + (f" 🏷 {escape(x['label'])}" if x["label"] else "")
            for k, x in enumerate(lines, 1)]
    await wait.edit_text(
        f"📋 <b>Задание разобрано:</b> {len(lines)} фраз, кадров по ссылкам — {len(media)}, "
        f"надписей — {sum(1 for x in lines if x['label'])}.\n\n" + "\n".join(rows)[:3000]
        + (("\n\n<b>Указания:</b>\n" + "\n".join(f"• {escape(r)}" for r in plan["rules"])) if plan["rules"] else ""))
    pack = [f"<b>Название:</b> {escape(plan['title'])}" if plan["title"] else "",
            f"<b>Обложка:</b> {escape(plan['cover'])}" if plan["cover"] else "",
            f"<b>Описание:</b>\n{escape(plan['description'])}" if plan["description"] else "",
            f"<b>Хэштеги:</b> {escape(plan['hashtags'])}" if plan["hashtags"] else ""]
    if any(pack):
        await message.answer("📦 Для YouTube (скопируй при загрузке):\n" + "\n".join(x for x in pack if x))
    await own_generate(message, state)
    data = await state.get_data()
    if data.get("lore_script") and data.get("task_labels"):
        await state.update_data(lore_script={**data["lore_script"], "screen_labels": data["task_labels"]})
    await message.answer("🔗 Кадры по ссылкам уже прикреплены к своим фразам (📎 в списке). Можно добавить видео "
                         "с материалами — «📎 Свои картинки и видео», бот найдёт в нём моменты для остальных фраз.")


async def own_generate(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    scripts = data.get("own_scripts") or []
    k = int(data.get("own_pick") or 0)
    if not 0 <= k < len(scripts):
        await message.answer("Сценарий потерялся — пришли заново: «🎬 Свой сценарий».")
        return
    head, body = scripts[k]
    wait = await message.answer(f"🎬 «{escape(head)}»: подбираю кадры к фильмам из сценария…")
    try:
        script = await shorts_lore.write_own(body, TMDB_API_KEY, head)
    except Exception as exc:  # noqa: BLE001
        logging.exception("Own script failed")
        await wait.edit_text(f"❌ Не получилось: <code>{escape(_safe_err(exc)[:400])}</code>")
        return
    await wait.delete()
    await state.update_data(lore_name=script["name"], lore_script=script)
    await show_lore(message, state)
    film = script.get("trailer_film")
    if film:
        many = script.get("trailer_films") or [film]
        names = ", ".join(f"«{escape(str(f.get('title') or ''))}»" + (f" ({f['year']})" if f.get("year") else "")
                          for f in many)
        await message.answer(
            f"🎞 При сборке бот сам возьмёт официальны{'е трейлеры' if len(many) > 1 else 'й трейлер'} {names}"
            + ", подберёт к каждой фразе отрезок 2–3 с (без "
            "звука трейлера), добавит крупные надписи на экран"
            + (f" и финальную карточку «МОЯ ОЦЕНКА {script['score']['value']}/10»" if script.get("score") else "")
            + ". Свои картинки — только если хочешь заменить что-то (подпиши номер фразы).")
    elif len(script.get("images") or []) + len(script.get("fixed") or []) < len(script["lines"]):
        await message.answer(f"🖼 Кадров нашлось меньше, чем фраз ({len(script.get('images') or [])}) — добавь свои "
                             "кнопкой «📎 Добавить свои картинки», иначе картинки будут повторяться.")


@router.message(LoreState.news, F.text, ~F.text.startswith("/"))
async def news_text(message: Message, state: FSMContext) -> None:
    text = message.text or ""
    if len(text.strip()) < 40:
        await message.answer("Маловато — пришли текст новости целиком (хотя бы пару предложений).")
        return
    await state.set_state(None)
    await state.update_data(news_text=text, lore_kind="news", lore_name="Новость", lore_hook="",
                            lore_script=None, lore_media=[])
    await news_generate(message, state)


@router.message(LoreState.name, F.text, ~F.text.startswith("/"))
async def lore_name(message: Message, state: FSMContext) -> None:
    await lore_ask_hook(message, state, _typed(message))


@router.message(LoreState.hook, F.text, ~F.text.startswith("/"))
async def lore_hook(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await state.update_data(lore_hook=_typed(message))
    await lore_generate(message, state)


@router.message(LoreState.edit, F.text, ~F.text.startswith("/"))
async def lore_edit(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    lines = shorts_lore.parse_lines(_typed(message))
    if len(lines) < 2 or not data.get("lore_script"):
        await message.answer("Пришли текст: каждая фраза — с новой строки (хотя бы 2 строки).")
        return
    script = dict(data["lore_script"], lines=lines[:20])
    await state.set_state(None)
    await state.update_data(lore_script=script)
    await show_lore(message, state)


@router.message(LoreState.media, F.text, ~F.text.startswith("/"))
async def lore_media_links(message: Message, state: FSMContext) -> None:
    """Ссылки на картинки вместо файлов: «3 https://…» — картинка к фразе 3 (номер можно не писать)."""
    found = IMG_URL.findall(message.text or "")
    if not found:
        await message.answer("Пришли картинку, видео или ссылку на картинку (можно с номером фразы: «3 https://…»).")
        return
    async with review_media_lock:
        media = (await state.get_data()).get("lore_media") or []
        for num, url in found:
            if len(media) >= LORE_MEDIA_MAX:
                break
            item = {"url": url.rstrip(").,»"), "ext": ".jpg"}
            if num:
                item["line"] = int(num)
            media.append(item)
        await state.update_data(lore_media=media)
    pinned = [f"{n} → фраза {n}" for n, _ in found if n]
    await message.answer(f"🔗 Принято ссылок: {len(found)}" + (f" ({', '.join(pinned[:12])})" if pinned else "")
                         + f". Всего материалов: {len(media)}. Картинки скачаю при сборке.",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("✅ Готово", "lo:media_done")]]))


@router.message(LoreState.media, F.photo | F.document | F.video | F.animation)
async def lore_media(message: Message, state: FSMContext) -> None:
    if message.photo:
        file, ext = message.photo[-1], ".jpg"
    elif message.video or message.animation:  # видео сцены — ИИ найдёт в нём моменты под фразы
        file, ext = message.video or message.animation, ".mp4"
    else:
        doc = message.document
        mime = str(doc.mime_type or "")
        if not mime.startswith(("image/", "video/")):
            await message.answer("Нужны картинки или видео (кадры, сцена из фильма, страницы комиксов).")
            return
        file, ext = doc, Path(doc.file_name or "").suffix.lower() or (".mp4" if mime.startswith("video/") else ".jpg")
    if (file.file_size or 0) > TG_DOWNLOAD_LIMIT:
        await message.answer("⚠️ Файл больше 20 МБ — бот Telegram не может его скачать.")
        return
    async with review_media_lock:
        media = (await state.get_data()).get("lore_media") or []
        if len(media) >= LORE_MEDIA_MAX:
            await message.answer(f"Хватит: уже {LORE_MEDIA_MAX} картинок. Нажми «✅ Готово».")
            return
        # подпись-число под картинкой — номер фразы: «4» — картинка встанет именно к фразе 4
        num = re.fullmatch(r"\s*№?\s*(\d{1,2})\s*", message.caption or "")
        item = {"file_id": file.file_id, "ext": ext}
        if num:
            item["line"] = int(num.group(1))
        media.append(item)
        await state.update_data(lore_media=media)
    note = f" — к фразе {item['line']}" if item.get("line") else ""
    await message.answer(f"📎 Принято: {len(media)}{note}", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[_btn("✅ Готово", "lo:media_done")]]))


@router.callback_query(F.data.startswith("lo:"))
async def lore_buttons(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id not in ADMIN_IDS or not callback.message:
        await callback.answer()
        return
    await callback.answer()
    message, action = callback.message, (callback.data or "")[3:]
    data = await state.get_data()
    if action == "start":
        await lore_ask_name(message, state)
        return
    if action == "news":
        await news_ask_text(message, state)
        return
    if action == "own":
        await state.clear()
        await state.set_state(LoreState.own)
        await message.answer(
            "🎬 <b>Свой сценарий</b>\n\nПришли готовый сценарий шортса сообщением или файлом .md / .txt — "
            "таблицей «Время | Голос | На экране» или просто фраза на строку. Голосом прочитаю фразы как есть, "
            "ничего не переписывая; кадры подберу к фильмам и сериалам, названным в «ёлочках». Если в файле "
            "несколько сценариев — дам выбрать. Дальше можно поправить текст и добавить свои картинки.\n\n"
            "📋 Можно прислать <b>задание целиком</b>, как ИИ-помощнику (текстом, .md, .txt или .docx): озвучка, "
            "список кадров со ссылками, план с надписями, название и хэштеги. Бот возьмёт озвучку дословно, "
            "скачает кадры по ссылкам к своим фразам и поставит надписи.\n\n"
            "<i>Передумал — /cancel</i>")
        return
    if action == "wwk":
        await state.clear()
        await state.set_state(LoreState.wwk)
        await message.answer(
            "🔮 <b>Что мы знаем о фильме</b>\n\nНапиши название фильма или сериала, который скоро выходит "
            "(например: <i>Мстители: Судный день</i>). Бот найдёт свежие новости, реакции и слухи и напишет "
            "текст по схеме вирусных шортсов: вопрос → «в конце самое шокирующее» → хорошее → «но радоваться "
            "рано» → тревожные слухи → призыв в ТГ в середине → самая сильная деталь в конце. Кадры — из трейлера, "
            "а названных героев (Дум, Циклоп…) — из их фильмов.\n\n<i>Передумал — /cancel</i>")
        return
    if action.startswith("ownpick:") and action[8:].isdigit():
        scripts = data.get("own_scripts") or []
        k = int(action[8:])
        if not 0 <= k < len(scripts):
            await message.answer("Список устарел — пришли сценарий заново: «🎬 Свой сценарий».")
            return
        await state.update_data(own_pick=k)
        await own_generate(message, state)
        return
    if action == "cancel":
        await state.clear()
        await message.answer("Отменено.")
        return
    if not data.get("lore_name"):
        await message.answer("Начни заново: кнопка «📖 История персонажа» или /lore")
        return
    if action in ("nohook", "redo"):
        await state.set_state(None)
        await lore_generate(message, state)
    elif not data.get("lore_script"):
        await message.answer("Сначала нужен сценарий.")
    elif action == "edit":
        await state.set_state(LoreState.edit)
        await message.answer("✏️ Скопируй текст выше, поправь и пришли одним сообщением — "
                             "каждая фраза с новой строки (номера можно оставить).\n\n<i>Передумал — /cancel</i>")
    elif action == "media":
        await state.set_state(LoreState.media)
        await message.answer(
            "📎 Присылай картинки и видео — кадры, арты, страницы комиксов и <b>видео сцены</b>, о которой ролик "
            "(например сцена после титров). По одному или альбомом (до 24).\n"
            "• Из видео ИИ вырежет моменты под каждую фразу — как в роликах, где на экране ровно то, о чём речь.\n"
            "• Картинки ИИ поставит к подходящим фразам.\n"
            "• «🖼 Кадры: только мои» — ролик только из твоего; «🤝 мои + найденные» — ИИ выбирает лучшее и из твоего, "
            "и из найденного сам (трейлер, фото).\n\n"
            "📌 Чтобы картинка встала к определённой фразе, подпиши под ней номер фразы цифрой "
            "(например <b>4</b>). Файлы до 20 МБ.\n"
            "🔗 Нет файлов — пришли ссылки на картинки, можно списком: «3 https://…» по строке на каждую.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [_btn("✅ Готово", "lo:media_done")], [_btn("🗑 Убрать все мои картинки", "lo:media_clear")]]))
    elif action == "mix":
        mode = "mix" if await lore_media_mode() == "own" else "own"
        await set_shorts_setting("lore_media_mode", mode)
        await message.answer("🤝 Кадры: твои + найденные — ИИ ищет нужный момент и в твоих видео и картинках, и в "
                             "трейлере и фото, которые нашёл сам." if mode == "mix" else
                             "🖼 Кадры: только твои — найденные ботом фото и трейлер не используются.")
        await show_lore(message, state, brief=True)
    elif action in ("media_done", "media_clear"):
        if action == "media_clear":
            await state.update_data(lore_media=[])
        await state.set_state(None)
        await show_lore(message, state, brief=True)
    elif action == "go":
        if shorts_lock.locked():
            await message.answer("⏳ Сейчас собирается другой ролик — нажми «Собрать» ещё раз через пару минут.")
            return
        await state.clear()
        status = await message.answer(progress_bar(0, "Начинаю, обычно это 3–6 минут"))

        async def job() -> None:
            try:
                await run_lore(message.bot, message.chat.id, data, status)
            except Exception as exc:
                logging.exception("Lore failed")
                try:
                    await status.edit_text("❌ Сборка остановилась — причина ниже")
                except TelegramBadRequest:
                    pass
                await message.answer(f"❌ Шортс не собрался:\n<code>{escape(_safe_err(exc)[-700:])}</code>")

        task = asyncio.create_task(job())
        shorts_jobs.add(task)
        task.add_done_callback(shorts_jobs.discard)


# ---------------------------------------------------------------- закадровый перевод трейлера

DUB_TIMEOUT = 20 * 60


def dub_dir(chat_id: int) -> Path:
    return SHORTS_DIR / f"dub_{chat_id}"


async def dub_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    shutil.rmtree(dub_dir(message.chat.id), ignore_errors=True)
    await state.set_state(DubState.source)
    await message.answer(
        "🎙 <b>Русский закадровый перевод трейлера</b>\n\n"
        "Пришли ссылку на трейлер (Rutube, VK Видео…) или сам видеофайл до 20 МБ. "
        "В подписи можно написать название фильма — пойдёт в SEO.\n\n"
        "ИИ выпишет реплики и переведёт, ты проверишь перевод, потом каждого героя озвучит свой голос "
        "из библиотеки Lumean (мужчин — мужские, женщин — женские), а оригинал станет тише под голосом.\n\n"
        "<i>Передумал — /cancel</i>")


async def show_dub(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    lines = data["dub_lines"]
    voices = await shorts_dub.pick_voices([ln for ln in lines if ln["ru"]])
    chars = sum(len(ln["ru"]) for ln in lines)
    head = (f"📝 <b>Перевод: {len([ln for ln in lines if ln['ru']])} реплик, {chars} символов</b>\n"
            f"Голоса: {escape(shorts_dub.voices_text(voices))}\n"
            "Чтобы поправить — пришли строки с номером: <code>3. новый перевод</code>, "
            "<code>3. -</code> — не озвучивать.")
    chunks, cur = [], head
    for row in shorts_dub.lines_text(lines).splitlines():
        piece = escape(row)
        if len(cur) + len(piece) + 1 > 3800:
            chunks.append(cur)
            cur = piece
        else:
            cur = f"{cur}\n{piece}" if cur != head else f"{cur}\n\n{piece}"
    chunks.append(cur)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("✅ Озвучить и собрать", "dub:go")], [_btn("✏️ Поправить перевод", "dub:edit")],
        [_btn("❌ Отмена", "dub:cancel")]])
    for k, chunk in enumerate(chunks):
        await message.answer(chunk, reply_markup=markup if k == len(chunks) - 1 else None)


@router.message(Command("dub"))
async def dub_command(message: Message, state: FSMContext) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    await dub_start(message, state)


@router.message(DubState.source, F.video | F.document | F.text)
async def dub_source(message: Message, state: FSMContext) -> None:
    if (message.text or "").startswith("/"):
        return
    work = dub_dir(message.chat.id)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    url = ""
    title = (message.caption or "").strip()
    wait = await message.answer("⏳ Скачиваю видео…")
    try:
        media = message.video or message.document
        if media:
            if media is message.document and not str(media.mime_type or "").startswith("video/"):
                await wait.edit_text("Это не видео. Пришли видеофайл или ссылку.")
                return
            if (media.file_size or 0) > TG_DOWNLOAD_LIMIT:
                await wait.edit_text("⚠️ Файл больше 20 МБ — бот Telegram не может его скачать. "
                                     "Пришли ссылку на Rutube/VK или сжатую версию.")
                return
            src = work / "source.mp4"
            await message.bot.download(media.file_id, destination=src)
        else:
            m = re.search(r"https?://\S+", message.text or "")
            if not m:
                await wait.edit_text("Нужна ссылка на видео (https://…) или сам видеофайл.")
                return
            url = m.group(0)
            title = (message.text or "").replace(url, "").strip()
            src = await shorts_dub.fetch_source(url, work)
        await wait.edit_text("🎧 ИИ слушает трейлер и переводит, обычно до минуты…")
        lines, _ = await shorts_dub.translate(src, work, GEMINI_API_KEY)
    except Exception as exc:
        logging.exception("Dub source failed")
        await wait.edit_text(f"❌ Не получилось: <code>{escape(_safe_err(exc)[:400])}</code>")
        return
    await wait.delete()
    await state.set_state(None)
    await state.update_data(dub_src=str(src), dub_lines=lines, dub_title=title, dub_url=url)
    await show_dub(message, state)


@router.message(DubState.edit, F.text, ~F.text.startswith("/"))
async def dub_edit(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    lines = data.get("dub_lines") or []
    if not shorts_dub.apply_edits(lines, _typed(message)):
        await message.answer("Не нашёл строк с номером. Формат: <code>3. новый перевод</code>")
        return
    await state.set_state(None)
    await state.update_data(dub_lines=lines)
    await show_dub(message, state)


async def run_dub(bot: Bot, chat_id: int, data: dict[str, Any], status: Message) -> None:
    async with shorts_lock:
        await load_pronunciations()
        work = dub_dir(chat_id)
        try:
            progress = progress_updater(status, "перевод")
            shorts_daily.on_progress, shorts_daily.last_stage = progress, ""
            result = await asyncio.wait_for(shorts_dub.make_dub(
                Path(data["dub_src"]), data["dub_lines"], work, data.get("dub_title") or "",
                data.get("dub_url") or ""), timeout=DUB_TIMEOUT)
            await progress(100, "Готово — отправляю видео")
            seo = result.seo
            await bot.send_video(chat_id, FSInputFile(result.video, filename="kinomir360_trailer_ru.mp4"),
                                 caption=f"🎙 <b>{escape(seo['title'])}</b>", supports_streaming=True,
                                 request_timeout=600)
            for label, key in (("Название", "title"), ("Описание", "description"), ("Теги", "tags"),
                               ("Закреплённый комментарий", "pinned")):
                await bot.send_message(chat_id, f"<b>{label}:</b>\n<pre>{escape(seo[key])}</pre>")
            if seo.get("warning"):
                await bot.send_message(chat_id, escape(seo["warning"]))
        except asyncio.TimeoutError:
            raise RuntimeError(f"перевод собирался дольше {DUB_TIMEOUT // 60} минут — "
                               f"застрял на этапе: {shorts_daily.last_stage or 'начало'}.") from None
        finally:
            shorts_daily.on_progress = None
            shutil.rmtree(work, ignore_errors=True)


@router.callback_query(F.data.startswith("dub:"))
async def dub_buttons(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id not in ADMIN_IDS or not callback.message:
        await callback.answer()
        return
    await callback.answer()
    message, action = callback.message, (callback.data or "")[4:]
    data = await state.get_data()
    if action == "start":
        await dub_start(message, state)
        return
    if action == "cancel":
        await state.clear()
        shutil.rmtree(dub_dir(message.chat.id), ignore_errors=True)
        await message.answer("Перевод отменён.")
        return
    if not data.get("dub_lines") or not Path(str(data.get("dub_src"))).exists():
        await message.answer("Начни заново: кнопка «🎙 Перевод трейлера» или /dub")
        return
    if action == "edit":
        await state.set_state(DubState.edit)
        await message.answer("✏️ Пришли строки с номером и новым переводом, можно сразу несколько:\n"
                             "<code>3. Мы опоздали.\n7. -</code>  (минус — не озвучивать)\n\n"
                             "<i>Передумал — /cancel</i>")
    elif action == "go":
        if shorts_lock.locked():
            await message.answer("⏳ Сейчас собирается другой ролик — нажми «Озвучить» ещё раз через пару минут.")
            return
        await state.clear()
        status = await message.answer(progress_bar(0, "Начинаю, обычно это 2–5 минут", "перевод"))

        async def job() -> None:
            try:
                await run_dub(message.bot, message.chat.id, data, status)
            except Exception as exc:
                logging.exception("Dub failed")
                try:
                    await status.edit_text("❌ Сборка остановилась — причина ниже")
                except TelegramBadRequest:
                    pass
                await message.answer(f"❌ Перевод не собрался:\n<code>{escape(_safe_err(exc)[-700:])}</code>")

        task = asyncio.create_task(job())
        shorts_jobs.add(task)
        task.add_done_callback(shorts_jobs.discard)


KP_TYPES = {
    "movie": "Фильм",
    "tv-series": "Сериал",
    "cartoon": "Мультфильм",
    "animated-series": "Мультсериал",
    "anime": "Аниме",
}


@router.inline_query()
async def inline_title_search(query: InlineQuery) -> None:
    """Подсказки при наборе «@бот название»: выбор → «/short facts id:…» — факты точно о нём."""
    if query.from_user.id not in ADMIN_IDS:
        await query.answer([], cache_time=300, is_personal=True)
        return
    text = query.query.strip()
    if len(text) < 2:
        await query.answer([], cache_time=5, is_personal=True)
        return
    try:
        items = await kp.search(text, limit=15)
    except Exception:
        logging.exception("Inline search failed")
        items = []
    results = []
    for item in items:
        if not item.get("id"):
            continue
        title = kp.title(item)
        year = item.get("year")
        rating = (item.get("rating") or {}).get("kp") or 0
        details = [KP_TYPES.get(str(item.get("type") or ""), "Фильм")]
        if rating:
            details.append(f"КП {float(rating):.1f}")
        genres = [g.get("name") for g in item.get("genres") or [] if isinstance(g, dict) and g.get("name")]
        if genres:
            details.append(", ".join(genres[:2]))
        poster = item.get("poster") or {}
        thumb = poster.get("previewUrl") or poster.get("url") if isinstance(poster, dict) else None
        results.append(InlineQueryResultArticle(
            id=str(item["id"]),
            title=f"{title} ({year})" if year else title,
            description=" • ".join(details),
            thumbnail_url=thumb or None,
            input_message_content=InputTextMessageContent(message_text=f"/short facts id:{item['id']}"),
        ))
    await query.answer(results, cache_time=30, is_personal=True)


# ---------------------------------------------------------------- бот + канал: статистика, уведомления, рассылка,
# викторина дня, оценки, приглашения, идеи

class IdeaState(StatesGroup):
    text = State()


class ContactState(StatesGroup):
    text = State()


# --- «✉️ Написать админу»: сообщение человека приходит админам, ответ админа (Reply) — человеку

CONTACT_BUTTON = "✉️ Написать админу"


def _user_menu(message: Message) -> ReplyKeyboardMarkup:
    """Главное меню внизу экрана: у пришедших по ссылке (?start=…) его иначе нет."""
    return main_menu(bool(message.chat and message.chat.id in ADMIN_IDS))


async def contact_ask(message: Message, state: FSMContext) -> None:
    await state.set_state(ContactState.text)
    await message.answer("✉️ Напиши сообщение одним сообщением — текст, фото, видео или голосовое. "
                         "Я передам его администратору KinoMir360, ответ придёт сюда же 🙌\n\n"
                         "<i>Передумал — /cancel</i>", reply_markup=_user_menu(message))


@router.message(Command("contact"))
async def contact_command(message: Message, state: FSMContext) -> None:
    """/contact — для админа ссылка «связь с админом», для остальных — сразу написать."""
    if message.from_user and message.from_user.id in ADMIN_IDS:
        link = f"{await _bot_link(message.bot)}?start=contact"
        await message.answer(f"✉️ Ссылка «связь с админом» — для закрепа, описания канала, постов:\n<code>{link}</code>\n\n"
                             "По ней человек сразу попадает в «Написать админу», его сообщение придёт тебе сюда.")
        return
    await contact_ask(message, state)


@router.message(F.text == CONTACT_BUTTON)
async def contact_button(message: Message, state: FSMContext) -> None:
    await contact_ask(message, state)


async def _contact_link(chat_id: int, message_id: int, user_id: int) -> None:
    await db().execute("INSERT INTO contact_msgs(chat_id, message_id, user_id) VALUES($1, $2, $3) "
                       "ON CONFLICT DO NOTHING", chat_id, message_id, user_id)


async def contact_to_admins(message: Message) -> int:
    """Сообщение человека — каждому админу: шапка «от кого» и копия сообщения (любой тип). Сколько дошло."""
    user = message.from_user
    who = (f"@{user.username}" if user.username else escape(user.first_name or "без имени")) + f" (id {user.id})"
    sent = 0
    for admin_id in ADMIN_IDS:
        try:
            head = await message.bot.send_message(
                admin_id, f"✉️ <b>Сообщение от {who}</b>\nОтветь на это или следующее сообщение (свайп → «Ответить») — "
                          "ответ уйдёт человеку.")
            copy = await message.bot.copy_message(admin_id, message.chat.id, message.message_id)
            await _contact_link(admin_id, head.message_id, user.id)
            await _contact_link(admin_id, copy.message_id, user.id)
            sent += 1
        except Exception:  # noqa: BLE001
            logging.exception("Contact: forward to admin %s failed", admin_id)
    await track(user.id, "contact")
    return sent


@router.message(ContactState.text, ~F.text.startswith("/"))
async def contact_message(message: Message, state: FSMContext) -> None:
    await state.clear()
    if message.text == CONTACT_BUTTON:
        await contact_ask(message, state)
        return
    if await contact_to_admins(message):
        await message.answer("✅ Отправил администратору. Ответ придёт сюда — обычно в течение дня.\n\n"
                             "А пока — кнопки внизу: найти фильм, подобрать на вечер, викторина 🎬",
                             reply_markup=_user_menu(message))
    else:
        await message.answer("😔 Не получилось отправить — попробуй чуть позже.")


async def _contact_thread(message: Message) -> int | None:
    """Ответ (Reply) на сообщение переписки «написать админу» — id человека в этой переписке."""
    reply = message.reply_to_message
    if not reply or not message.from_user:
        return None
    try:
        user_id = await db().fetchval("SELECT user_id FROM contact_msgs WHERE chat_id=$1 AND message_id=$2",
                                      message.chat.id, reply.message_id)
    except Exception:  # noqa: BLE001
        logging.exception("Contact: thread lookup failed")
        user_id = None
    if not user_id and reply.from_user and reply.from_user.is_bot:  # запасной путь: id человека в шапке
        m = re.search(r"Сообщение от .*\(id (\d+)\)", reply.text or reply.caption or "")
        user_id = int(m.group(1)) if m else None
    return int(user_id) if user_id else None


async def is_contact_reply(message: Message) -> bool:
    return await _contact_thread(message) is not None


@contact_router.message(is_contact_reply)
async def contact_reply(message: Message, state: FSMContext) -> None:
    user_id = await _contact_thread(message)
    if message.from_user.id in ADMIN_IDS:  # админ отвечает человеку (и себе, если проверял сам)
        await state.clear()  # ответ мог прийти посреди другого режима (ввод текста поста и т. п.)
        try:
            head = await message.bot.send_message(user_id, "💬 <b>Ответ от KinoMir360:</b>")
            copy = await message.bot.copy_message(user_id, message.chat.id, message.message_id)
            await _contact_link(user_id, head.message_id, user_id)
            await _contact_link(user_id, copy.message_id, user_id)
            await message.reply("✅ Ответ отправлен.")
        except TelegramForbiddenError:
            await message.reply("❌ Человек заблокировал бота — ответ не дойдёт.")
        except Exception as exc:  # noqa: BLE001
            await message.reply(f"❌ Не отправилось: <code>{escape(_safe_err(exc)[:200])}</code>")
    else:  # человек отвечает на ответ админа — снова админам
        await contact_to_admins(message)
        await message.answer("✅ Передал администратору.")


_seen_users: dict[int, float] = {}  # кому уже обновили last_seen (раз в 5 минут, а не на каждое нажатие)


async def activity_tracker(handler: Any, event: Any, data: dict[str, Any]) -> Any:
    """Каждое сообщение и кнопка в личке: пользователь в базе и «был в боте» — для статистики и рассылки."""
    user = getattr(event, "from_user", None)
    chat = event.chat if isinstance(event, Message) else (event.message.chat if getattr(event, "message", None) else None)
    if user and chat and chat.type == "private" and time.time() - _seen_users.get(user.id, 0) > 300:
        _seen_users[user.id] = time.time()
        try:
            await db().execute(
                """INSERT INTO users(user_id, username, first_name) VALUES($1, $2, $3)
                   ON CONFLICT(user_id) DO UPDATE SET username=EXCLUDED.username, first_name=EXCLUDED.first_name,
                   last_seen_at=NOW()""", user.id, user.username, user.first_name)
            await db().execute("UPDATE user_prefs SET blocked=FALSE WHERE user_id=$1 AND blocked", user.id)
        except Exception:  # noqa: BLE001 — статистика не должна мешать боту
            logging.exception("Activity tracking failed")
    return await handler(event, data)


async def track(user_id: int | None, kind: str) -> None:
    """Событие для статистики: подбор фильма, замок подписки, викторина, рассылка…"""
    try:
        await db().execute("INSERT INTO bot_events(user_id, kind) VALUES($1, $2)", user_id, kind)
    except Exception:  # noqa: BLE001
        logging.exception("Event tracking failed")


async def _count(sql: str, *args: Any) -> int:
    try:
        return int(await db().fetchval(sql, *args) or 0)
    except Exception:  # noqa: BLE001
        logging.exception("Stats query failed: %s", sql[:80])
        return 0


async def bot_stats_text() -> str:
    q = _count
    ev = "SELECT COUNT(*) FROM bot_events WHERE kind=$1 AND created_at > NOW() - $2::interval"
    week = timedelta(days=7)  # интервалы asyncpg передаёт как timedelta
    periods = (timedelta(days=1), week, timedelta(days=30))
    users = await q("SELECT COUNT(*) FROM users")
    new = [await q("SELECT COUNT(*) FROM users WHERE created_at > NOW() - $1::interval", i) for i in periods]
    active = [await q("SELECT COUNT(*) FROM users WHERE last_seen_at > NOW() - $1::interval", i) for i in periods]
    searches = await q("SELECT COUNT(*) FROM search_history WHERE searched_at > NOW() - INTERVAL '7 days'")
    opens = await q("SELECT COUNT(*) FROM user_activity WHERE action='open' AND created_at > NOW() - INTERVAL '7 days'")
    try:
        top_q = await db().fetch("""SELECT lower(query) q, COUNT(*) n FROM search_history
                                    WHERE searched_at > NOW() - INTERVAL '7 days' GROUP BY 1 ORDER BY 2 DESC LIMIT 5""")
        top_f = await db().fetch("""SELECT title, COUNT(*) n FROM user_activity WHERE action='open'
                                    AND created_at > NOW() - INTERVAL '7 days' GROUP BY 1 ORDER BY 2 DESC LIMIT 5""")
    except Exception:  # noqa: BLE001
        top_q, top_f = [], []
    favs = await q("SELECT COUNT(*) FROM favorites")
    fav_users = await q("SELECT COUNT(DISTINCT user_id) FROM favorites")
    reminders = await q("SELECT COUNT(*) FROM premiere_reminders WHERE sent_at IS NULL")
    rows = [
        "📈 <b>Статистика бота</b>", "",
        f"👥 Пользователей всего: <b>{users}</b>",
        f"🆕 Новых: сегодня <b>{new[0]}</b> · 7 дн <b>{new[1]}</b> · 30 дн <b>{new[2]}</b>",
        f"🟢 Заходили: сегодня <b>{active[0]}</b> · 7 дн <b>{active[1]}</b> · 30 дн <b>{active[2]}</b>",
        escape(kp.status()), "",
        f"🔎 Поисков за 7 дн: <b>{searches}</b> · 🎬 открыто карточек: <b>{opens}</b>",
        f"🎯 Подборов «что посмотреть» за 7 дн: <b>{await q(ev, 'rq', week)}</b>",
        f"❤️ В избранном: <b>{favs}</b> (у {fav_users} чел.) · 🔔 ждут премьеры: <b>{reminders}</b>",
        f"📣 Уведомлений о сериях и релизах за 7 дн: <b>{await q(ev, 'alert', week)}</b>",
        f"📬 Подборка недели: получают <b>{await q('SELECT COUNT(*) FROM users u LEFT JOIN user_prefs p USING(user_id) WHERE COALESCE(p.digest, TRUE) AND NOT COALESCE(p.blocked, FALSE)')}</b>"
        f" · отписались {await q('SELECT COUNT(*) FROM user_prefs WHERE NOT digest')}"
        f" · заблокировали бота {await q('SELECT COUNT(*) FROM user_prefs WHERE blocked')}",
        f"🧩 Викторина: сегодня играли <b>{await q('SELECT COUNT(*) FROM quiz_answers WHERE day=CURRENT_DATE')}</b>"
        f" · за месяц <b>{await q(QUIZ_MONTH_PLAYERS)}</b>",
        f"👍 Оценок фильмов: <b>{await q('SELECT COUNT(*) FROM ratings')}</b>",
        f"🎁 Пришли по приглашениям: <b>{await q('SELECT COUNT(*) FROM referrals')}</b>"
        f" (подписались {await q('SELECT COUNT(*) FROM referrals WHERE subscribed')})",
        f"🔒 Подписка: замок показан за 7 дн <b>{await q(ev, 'sub_lock', week)}</b>"
        f" · подписались после замка <b>{await q(ev, 'sub_ok', week)}</b>",
        f"✉️ Написали админу за 7 дн: <b>{await q(ev, 'contact', week)}</b>",
        f"💡 Идей от подписчиков: <b>{await q('SELECT COUNT(*) FROM ideas')}</b>"
        f" (за 7 дн {await q(IDEAS_WEEK)})",
    ]
    if top_q:
        rows += ["", "🔥 <b>Чаще искали (7 дн):</b>"] + [f"• {escape(str(r['q'])[:40])} — {r['n']}" for r in top_q]
    if top_f:
        rows += ["", "🎬 <b>Чаще открывали (7 дн):</b>"] + [f"• {escape(str(r['title'])[:40])} — {r['n']}" for r in top_f]
    return "\n".join(rows)


IDEAS_WEEK = "SELECT COUNT(*) FROM ideas WHERE created_at > NOW() - INTERVAL '7 days'"
QUIZ_MONTH_PLAYERS = ("SELECT COUNT(DISTINCT user_id) FROM quiz_answers "
                      "WHERE day >= date_trunc('month', CURRENT_DATE)")


async def growth_menu() -> InlineKeyboardMarkup:
    digest_on = await shorts_setting("digest") != "off"
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("🔄 Обновить", "st:home")],
        [_btn("💡 Идеи подписчиков", "st:ideas"), _btn("🎁 Кто приглашает", "st:refs")],
        [_btn(f"📬 Подборка недели в бот: {'вкл ✅' if digest_on else 'выкл ❌'}", "st:digest")],
        [_btn("📬 Прислать подборку мне (проверка)", "st:digest_test")],
        [_btn("🏆 Пост: знатоки месяца", "pt:qw"), _btn("⭐ Пост: топ подписчиков", "pt:us")],
        [_btn("⬅️ В админку", "admin:home")],
    ])


@router.message(Command("stats"))
async def stats_command(message: Message) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    await message.answer(await bot_stats_text(), reply_markup=await growth_menu())


@router.callback_query(F.data.startswith("st:"))
async def stats_buttons(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS or not callback.message:
        return
    message, action = callback.message, (callback.data or "")[3:]
    if action == "home":
        text = await bot_stats_text()
        try:
            await message.edit_text(text, reply_markup=await growth_menu())
        except TelegramBadRequest:
            await message.answer(text, reply_markup=await growth_menu())
    elif action == "ideas":
        rows = await db().fetch("SELECT COALESCE(i.username, u.username) username, u.first_name, i.text, i.created_at "
                                "FROM ideas i LEFT JOIN users u USING(user_id) ORDER BY i.created_at DESC LIMIT 15")
        await message.answer("💡 <b>Последние идеи подписчиков</b>\n\n" + ("\n\n".join(
            f"• {r['created_at']:%d.%m} {escape('@' + r['username'] if r['username'] else (r['first_name'] or 'аноним'))}: "
            f"{escape(str(r['text'])[:300])}" for r in rows) or "Пока нет."))
    elif action == "refs":
        rows = await db().fetch("""SELECT r.referrer_id, u.username, u.first_name, COUNT(*) n,
                                   COUNT(*) FILTER (WHERE r.subscribed) s FROM referrals r
                                   LEFT JOIN users u ON u.user_id = r.referrer_id
                                   GROUP BY 1, 2, 3 ORDER BY s DESC, n DESC LIMIT 15""")
        await message.answer("🎁 <b>Кто приглашает друзей</b> (пришло / подписались)\n\n" + ("\n".join(
            f"• {escape('@' + r['username'] if r['username'] else (r['first_name'] or str(r['referrer_id'])))}: "
            f"{r['n']} / {r['s']}" for r in rows) or "Пока никто."))
    elif action == "digest":
        on = await shorts_setting("digest") != "off"
        await set_shorts_setting("digest", "off" if on else None)
        try:
            await message.edit_reply_markup(reply_markup=await growth_menu())
        except TelegramBadRequest:
            pass
    elif action == "digest_test":
        text, markup = await digest_message()
        await message.answer(text, reply_markup=markup, disable_web_page_preview=True)


# --- уведомления: новая серия / вышло онлайн (из избранного)

def _day(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


async def favorite_news(movie_id: int, media_type: str) -> list[tuple[str, str]]:
    """Что нового у фильма из избранного: [(ключ, текст)] — свежая серия или выход онлайн."""
    out: list[tuple[str, str]] = []
    today = date.today()
    if media_type == "series":
        for season in await kp.seasons(movie_id):
            for ep in season.get("episodes") or []:
                aired = _day(ep.get("date") or ep.get("airDate"))
                if aired and today - timedelta(days=3) <= aired <= today:
                    n, e = season.get("number"), ep.get("number")
                    out.append((f"s{n}e{e}", f"вышла {e}-я серия {n}-го сезона"))
    else:
        item = await kp.details(movie_id)
        digital = _day((item.get("premiere") or {}).get("digital"))
        if digital and today - timedelta(days=14) <= digital <= today:
            out.append(("digital", "вышел онлайн"))
    return out


async def favorites_alerts(bot: Bot) -> None:
    rows = await db().fetch("SELECT DISTINCT ON (movie_id) movie_id, media_type, title FROM favorites LIMIT 400")
    for row in rows:
        movie_id, title = int(row["movie_id"]), str(row["title"])
        try:
            news = await favorite_news(movie_id, str(row["media_type"]))
        except Exception:  # noqa: BLE001
            logging.info("Favorite news for %s failed", movie_id)
            continue
        known = {r["key"] for r in await db().fetch("SELECT key FROM fav_alerts WHERE movie_id=$1", movie_id)}
        fresh = [(k, t) for k, t in news if k not in known]
        if not fresh:
            continue
        await db().executemany("INSERT INTO fav_alerts(movie_id, key) VALUES($1, $2) ON CONFLICT DO NOTHING",
                               [(movie_id, k) for k, _ in fresh])
        what = fresh[-1][1]
        users = await db().fetch(
            """SELECT f.user_id FROM favorites f LEFT JOIN user_prefs p USING(user_id)
               WHERE f.movie_id=$1 AND COALESCE(p.alerts, TRUE) AND NOT COALESCE(p.blocked, FALSE)""", movie_id)
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎬 Открыть карточку", callback_data=f"open:{movie_id}")],
            [InlineKeyboardButton(text="🔕 Не присылать такие уведомления", callback_data="pref:alerts:off")]])
        for u in users:
            await _send_to_user(bot, int(u["user_id"]), f"🔔 <b>{escape(title)}</b> — {what}! 🍿\n\n"
                                "Ты добавил это в избранное, поэтому сообщаю первым 😉", markup, "alert")


async def _send_to_user(bot: Bot, user_id: int, text: str, markup: InlineKeyboardMarkup, kind: str) -> bool:
    """Личное сообщение пользователю; заблокировал бота — помечаем, чтобы больше не слать."""
    try:
        await bot.send_message(user_id, text, reply_markup=markup, disable_web_page_preview=True)
        await track(user_id, kind)
        await asyncio.sleep(0.05)  # не больше ~20 сообщений в секунду — лимит Telegram
        return True
    except TelegramForbiddenError:
        await db().execute("INSERT INTO user_prefs(user_id, blocked) VALUES($1, TRUE) "
                           "ON CONFLICT(user_id) DO UPDATE SET blocked=TRUE", user_id)
    except Exception as exc:  # noqa: BLE001
        logging.info("Send to %s failed: %s", user_id, exc)
        await asyncio.sleep(1)
    return False


@router.callback_query(F.data.startswith("pref:"))
async def prefs_buttons(callback: CallbackQuery) -> None:
    _, what, value = ((callback.data or "") + "::").split(":")[:3]
    if what not in ("alerts", "digest"):
        await callback.answer()
        return
    on = value == "on"
    await db().execute(f"INSERT INTO user_prefs(user_id, {what}) VALUES($1, $2) "
                       f"ON CONFLICT(user_id) DO UPDATE SET {what}=EXCLUDED.{what}", callback.from_user.id, on)
    name = "уведомления о новых сериях и релизах" if what == "alerts" else "подборку недели"
    await callback.answer("✅ Включено" if on else "🔕 Больше не пришлю")
    if callback.message:
        await callback.message.answer(
            f"{'🔔 Снова присылаю' if on else '🔕 Больше не присылаю'} {name}.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text="🔕 Выключить" if on else "🔔 Включить обратно", callback_data=f"pref:{what}:{'off' if on else 'on'}")]]))


# --- подборка недели в личку пользователям бота

async def digest_message() -> tuple[str, InlineKeyboardMarkup]:
    today = datetime.now(MSK).date()
    films: list[dict[str, Any]] = []
    for key in ("weekfilms", "weekseries", "online"):
        try:
            films += await shorts_daily.pick_films(kp._get, shorts_daily.theme_for(key, today), today, set())
        except Exception:  # noqa: BLE001
            logging.exception("Digest pick %s failed", key)
        if len(films) >= 4:
            break
    seen, lines = set(), []
    for f in films:
        if f.get("id") in seen:
            continue
        seen.add(f.get("id"))
        rating, label = _trusted_rating(f)
        kind = {"сериале": "сериал", "мультфильме": "мультфильм"}.get(shorts_daily._kind(f), "фильм")
        lines.append(f"• <b>{escape(shorts_daily._title(f))}</b> ({f.get('year') or '?'}, {kind})"
                     + (f" ⭐ {rating:.1f}" if rating else ""))
        if len(lines) >= 4:
            break
    text = ("🍿 <b>Что посмотреть на выходных — подборка недели</b>\n\n" + "\n".join(lines or ["—"])
            + f"\n\nОписания, трейлеры и ещё больше новинок — в канале {escape(POST_CHANNEL)} 👇")
    return text, InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📺 Вся подборка в канале", url=f"https://t.me/{POST_CHANNEL.lstrip('@')}")],
        [InlineKeyboardButton(text="🎯 Подобрать фильм под себя", callback_data="rq:start")],
        [InlineKeyboardButton(text="🔕 Не присылать подборки", callback_data="pref:digest:off")]])


async def send_weekly_digest(bot: Bot) -> int:
    text, markup = await digest_message()
    users = await db().fetch(
        """SELECT u.user_id FROM users u LEFT JOIN user_prefs p USING(user_id)
           WHERE COALESCE(p.digest, TRUE) AND NOT COALESCE(p.blocked, FALSE)
             AND u.last_seen_at > NOW() - INTERVAL '90 days'""")
    sent = 0
    for u in users:
        sent += await _send_to_user(bot, int(u["user_id"]), text, markup, "digest")
    return sent


# --- Rotten Tomatoes: что пишут критики и зрители о фильмах и сезонах сериалов; оповещение о снятии эмбарго

RT_EVERY = 3 * 3600  # как часто проверять оценки премьер
RT_CRITICS_MIN = 10  # рецензий — оценка критиков уже что-то значит (у сезонов сериалов рецензий меньше)
RT_CRITICS_MIN_TV = 5
RT_KEY = re.compile(r"(t?)(\d+)(?:s(\d+))?")


def _rt_key(item: dict[str, Any]) -> str:
    """Ключ для кнопок и памяти: «123» — фильм, «t456s2» — 2 сезон сериала."""
    return f"t{item['id']}s{item['season']}" if item.get("kind") == "tv" else str(item["id"])


def _rt_name(item: dict[str, Any]) -> str:
    return f"{item['title']} — {item['season']} сезон" if item.get("kind") == "tv" else item["title"]


async def _rt_item(key: str) -> dict[str, Any] | None:
    m = RT_KEY.fullmatch(key)
    if not m:
        return None
    if m.group(1):
        return await shorts_daily.tmdb.tv_season(TMDB_API_KEY, int(m.group(2)), int(m.group(3) or 0),
                                                 datetime.now(MSK).date().isoformat())
    film = await shorts_daily.tmdb.movie(TMDB_API_KEY, int(m.group(2)))
    return {**film, "kind": "movie"} if film else None


_rt_premieres_cache: dict[tuple[int, ...], tuple[float, list[dict[str, Any]]]] = {}


async def _rt_premieres(back: int = 21, ahead: int = 45, n_films: int = 20, n_tv: int = 12) -> list[dict[str, Any]]:
    """Фильмы (премьера в кино США) и сезоны сериалов с премьерой между «сегодня − back» и «+ ahead» дней.
    Список на час общий для меню и автопроверки — TMDB не дёргаем заново."""
    args = (back, ahead, n_films, n_tv)
    cached = _rt_premieres_cache.get(args)
    if cached and time.time() - cached[0] < 3600 and cached[1]:
        return cached[1]
    items = await _rt_premieres_load(*args)
    if items:
        _rt_premieres_cache[args] = (time.time(), items)
    return items


async def _rt_premieres_load(back: int, ahead: int, n_films: int, n_tv: int) -> list[dict[str, Any]]:
    today = datetime.now(MSK).date()
    since, until = (today - timedelta(days=back)).isoformat(), (today + timedelta(days=ahead)).isoformat()
    films, seasons = await asyncio.gather(shorts_daily.tmdb.releases(TMDB_API_KEY, since, until, n_films),
                                          shorts_daily.tmdb.tv_seasons(TMDB_API_KEY, since, until, n_tv),
                                          return_exceptions=True)
    out = [{**f, "kind": "movie"} for f in films] if isinstance(films, list) else []
    return out + (seasons if isinstance(seasons, list) else [])


async def rt_menu() -> tuple[str, InlineKeyboardMarkup]:
    today = datetime.now(MSK).date().isoformat()
    try:  # тот же список, что у автопроверки (кэш на час); в меню — 10 фильмов и 8 сезонов
        found = await _rt_premieres()
    except Exception as exc:  # noqa: BLE001
        logging.info("RT menu: tmdb failed: %s", exc)
        found = []
    items = [i for i in found if i.get("kind") != "tv"][:10] + [i for i in found if i.get("kind") == "tv"][:8]
    rows = [[_btn(f"{'📺' if i.get('kind') == 'tv' else '🎬'}{'🆕' if i['date'] <= today else '⏳'} "
                  f"{_rt_name(i)[:42]} ({i['date'][8:10]}.{i['date'][5:7]})", f"rt:f:{_rt_key(i)}")] for i in items]
    alerts = await shorts_setting("rt_alerts") != "off"
    rows.append([_btn("✏️ Другой фильм или сериал — напишу название", "rt:q")])
    rows.append([_btn("🔕 Не сообщать о снятии эмбарго" if alerts else "🔔 Сообщать о снятии эмбарго",
                      "rt:off" if alerts else "rt:on")])
    return ("🍅 <b>Что пишут критики и зрители</b> — оценки и рецензии с Rotten Tomatoes.\n\n"
            "🎬 — фильм, 📺 — сезон сериала; 🆕 — уже вышел, ⏳ — премьера скоро (рецензии появляются, когда "
            "снимут эмбарго, обычно за несколько дней до премьеры).\n"
            + ("🔔 Как только критики увидят фильм или сезон, или появятся оценки зрителей, пришлю уведомление с "
               "кнопкой поста." if alerts else "🔕 Уведомления о снятии эмбарго выключены."),
            InlineKeyboardMarkup(inline_keyboard=rows))


def _rt_film_buttons(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("🍅 Что пишут критики", f"rt:c:{key}")],
        [_btn("🍿 Что пишут зрители", f"rt:a:{key}")]])


async def _rt_state() -> dict[str, Any]:
    try:
        data = json.loads(await shorts_setting("rt_watch") or "{}")
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


async def _rt_find(item: dict[str, Any], web: bool) -> str | None:
    """Страница на RT. Адрес RT строится из английского названия: у корейских, японских, французских фильмов
    оригинальное название не подходит («/m/_2025») — берём английское с TMDB."""
    from shorts import websearch as shorts_websearch
    search = shorts_websearch.search if web else None
    name = item["original"]
    if not re.fullmatch(r"[A-Za-z0-9 :;,.!?'&\-–—()]+", name or ""):
        try:
            name = await shorts_daily.tmdb.english_title(TMDB_API_KEY, item.get("kind") or "movie", item["id"]) or name
        except Exception as exc:  # noqa: BLE001
            logging.info("RT: english title of %s failed: %s", item["id"], exc)
    if item.get("kind") == "tv":
        return await shorts_rt.find_tv(name, item["season"], item["year"], item.get("first_year", ""), search)
    return await shorts_rt.find(name, item["year"], search)


async def _rt_url(item: dict[str, Any], web: bool = True) -> str | None:
    """Страница фильма или сезона на RT (запоминаем, чтобы не искать каждый раз)."""
    key = _rt_key(item)
    st = (await _rt_state()).get(key) or {}
    if st.get("url"):
        return st["url"]
    url = await _rt_find(item, web)
    if url:
        state = await _rt_state()
        state[key] = {**(state.get(key) or {}), "url": url}
        await set_shorts_setting("rt_watch", json.dumps(state))
    return url


async def make_rt_post(bot: Bot, chat_id: int, key: str, audience: bool = False) -> None:
    """Пост «Что пишут критики» (Tomatometer, консенсус, мнения изданий) или «Что говорят зрители»
    (Popcornmeter, что нравится и нет) о фильме или сезоне сериала по Rotten Tomatoes; без страницы — по поиску."""
    item = await _rt_item(key)
    if not item:
        raise RuntimeError("не нашёл фильм или сериал на TMDB")
    url = await _rt_url(item)
    data: dict[str, Any] = {}
    if url:
        try:
            data = await shorts_rt.info(url)
        except Exception as exc:  # noqa: BLE001
            logging.info("RT post: %s failed: %s", url, exc)
    part = "audience" if audience else "critics"
    have = data.get(part) is not None or bool(data.get(f"{part}_reviews") or data.get(f"{part}_reviews_text"))
    tv = item.get("kind") == "tv"
    title = item["title"]
    about = f"{item['season']} сезоне сериала «{title}»" if tv else f"«{title}»"
    material = ((f"Сериал: «{title}» ({item['original']}), {item['season']} сезон, премьера сезона: {item['date']}.\n"
                 if tv else f"Фильм: «{title}» ({item['original']}, {item['year']}), премьера в США: {item['date']}.\n")
                + (shorts_rt.as_material(data, audience) if data else
                   "Страница Rotten Tomatoes не прочиталась — оценки и мнения найди в Google (Rotten Tomatoes, "
                   "Metacritic, рецензии изданий)."))
    if audience:
        task = (f"Напиши пост «Что говорят зрители о {about}»: заголовок КАПСОМ с эмодзи 🍿 и оценкой зрителей "
                "(Popcornmeter на Rotten Tomatoes) и числом оценок; 2–4 строки «✅ …» и «❌ …» — что зрителям "
                "понравилось и что нет (по отзывам, переведи по смыслу, без спойлеров); сравни с оценкой критиков, "
                "если она есть (совпали или разошлись); короткий вывод — стоит ли смотреть. Если оценок зрителей "
                "ещё нет — так и скажи, цифры не придумывай.")
    else:
        task = (f"Напиши пост «Что пишут критики о {about}»: заголовок КАПСОМ с эмодзи 🍅 и рейтингом "
                f"Tomatometer (например «🍅 КРИТИКИ ПОСМОТРЕЛИ {'2 СЕЗОН «…»' if tv else '«…»'}: 92% НА ROTTEN "
                "TOMATOES»), сколько рецензий; консенсус критиков своими словами; 3–4 мнения строками «🗣 Издание: "
                "суть в одну фразу» — переведи, имена и издания только из материала; что хвалят и что ругают"
                + ("; лучше или хуже прошлых сезонов, если критики об этом пишут" if tv else "")
                + "; короткий вывод — стоит ли ждать. Без спойлеров. Если рецензий ещё нет (эмбарго) — так и скажи, "
                "цифры не придумывай.")
    post = await shorts_ai.write_styled_post(GEMINI_API_KEY, task, material, search=not have)
    if not post:
        raise RuntimeError(shorts_ai.last_error or "ИИ не написал пост")
    work = SHORTS_DIR / f"rt_{time.time_ns()}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        files: list[Path] = []
        img = await _image(item["poster"] or item["backdrop"], work, "rt")
        if img:
            score = data.get(part)
            parts = [f"Критики {data['critics']}%" if data.get("critics") is not None else "",
                     f"Зрители {data['audience']}%" if data.get("audience") is not None else ""]
            card = await asyncio.to_thread(
                shorts_postcard.make_post_card, img, title,
                pill=(("ЗРИТЕЛИ" if audience else "КРИТИКИ"), f"{score}%") if score is not None else None,
                meta=" · ".join(p for p in parts if p) or item["year"], extra="Rotten Tomatoes",
                episode=f"{item['season']} сезон" if tv else None)
            files = [work / "rt.jpg"]
            card.save(files[0], quality=92)
        ids, kind = await _send_media(bot, chat_id, files, post_caption(post, "#рецензии"))
        await offer_post(bot, chat_id, ids, kind, f"rt:{'a' if audience else 'c'}:{key}",
                         what="Пост «что говорят зрители» готов" if audience else "Пост «что пишут критики» готов")
    finally:
        shutil.rmtree(work, ignore_errors=True)


YT_EVERY = 30 * 60  # проверка YouTube-каналов студий — раз в полчаса


async def yt_trailers_watch(bot: Bot) -> None:
    """Новые трейлеры и тизеры на YouTube-каналах студий — админам с кнопкой «Сделать пост». Первый проход
    только запоминает, что уже вышло (иначе пришла бы сразу пачка старых трейлеров)."""
    if await shorts_setting("yt_trailers") == "off":
        return
    found = await shorts_yttrailers.latest(hours=36, extra=await _json_setting("yt_channels", {}))
    raw = await shorts_setting("yt_seen")
    seen = list(json.loads(raw)) if raw else None
    if seen is None:
        await set_shorts_setting("yt_seen", json.dumps(
            [v["id"] for v in found] + ["t:" + shorts_yttrailers.film_key(v["title"]) for v in found]))
        return
    fresh = []
    # тот же трейлер у студии и у сборщика трейлеров — одно сообщение, со ссылкой на ролик студии
    found.sort(key=lambda v: (v["studio"] in shorts_yttrailers.AGGREGATORS, -v["time"]))
    for v in found:
        key = "t:" + shorts_yttrailers.film_key(v["title"])
        if v["id"] in seen or key in seen:
            continue
        seen.append(key)
        fresh.append(v)
    fresh = fresh[:6]
    if not fresh:
        return
    info = await _json_setting("yt_info", {})
    for v in reversed(fresh):  # по порядку выхода
        info[v["id"]] = {k: v[k] for k in ("title", "studio", "url", "desc")}
        caption = (f"🎬 <b>Новый трейлер</b> — {escape(v['studio'])}\n{escape(v['title'])}\n{v['url']}")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [_btn("📝 Сделать пост", f"ytr:p:{v['id']}")],
            [_btn("🔕 Не присылать трейлеры", "ytr:off")]])
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_photo(admin_id, v["thumb"], caption=caption, reply_markup=kb)
            except Exception:  # noqa: BLE001 — у старых видео нет maxres-обложки: без картинки
                try:
                    await bot.send_message(admin_id, caption, reply_markup=kb)
                except Exception:  # noqa: BLE001
                    logging.exception("YouTube trailer alert failed")
        seen.append(v["id"])
    await set_shorts_setting("yt_seen", json.dumps(seen[-400:]))
    await set_shorts_setting("yt_info", json.dumps(dict(list(info.items())[-100:]), ensure_ascii=False))


@router.callback_query(F.data.startswith("ytr:"))
async def yt_trailer_buttons(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS or not callback.message:
        await callback.answer()
        return
    _, kind, arg = ((callback.data or "") + "::").split(":")[:3]
    message = callback.message
    if kind == "list":
        await callback.answer()
        wait = await message.answer("🎬 Смотрю свежие трейлеры на каналах студий…")
        await wait.edit_text(await trailers_overview(), disable_web_page_preview=True)
        return
    if kind in ("off", "on"):
        await set_shorts_setting("yt_trailers", None if kind == "on" else "off")
        await callback.answer("Готово")
        await message.answer("🔔 Буду присылать новые трейлеры с YouTube-каналов студий." if kind == "on" else
                             "🔕 Трейлеры с YouTube больше не присылаю. Включить: /trailers on")
        return
    if kind == "p":
        v = (await _json_setting("yt_info", {})).get(arg)
        if not v:
            await callback.answer("Трейлер устарел — пришли ссылку через «🔗 Пост по ссылке»", show_alert=True)
            return
        await callback.answer()
        if post_lock.locked():
            await message.answer("⏳ Пост уже пишется — подожди минуту и нажми ещё раз.")
            return
        wait = await message.answer(f"📝 Пишу пост о трейлере «{escape(v['title'][:80])}»…")
        text = (f"Новый официальный трейлер на YouTube-канале {v['studio']}: {v['title']}. "
                f"Ссылка на трейлер: {v['url']}\n{v.get('desc') or ''}")

        async def job() -> None:
            try:
                async with post_lock:
                    await _make_link_post(message, [v["url"]], "это новость о вышедшем трейлере", text, None)
                await wait.delete()
            except Exception as exc:  # noqa: BLE001
                logging.exception("Trailer post failed")
                await wait.edit_text(f"❌ Пост не получился: <code>{escape(_safe_err(exc)[:300])}</code>")

        _run_job(job)
        return
    await callback.answer()


@router.message(Command("trailers"))
async def trailers_command(message: Message) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    arg = (message.text or "").partition(" ")[2].strip().lower()
    if arg in ("on", "off"):
        await set_shorts_setting("yt_trailers", None if arg == "on" else "off")
        await message.answer("🔔 Трейлеры с YouTube включены." if arg == "on" else "🔕 Трейлеры с YouTube выключены.")
        return
    mine = await _json_setting("yt_channels", {})
    raw_arg = (message.text or "").partition(" ")[2].strip()
    if arg.startswith("add "):
        try:
            got = await shorts_yttrailers.channel_id(raw_arg[4:])
        except Exception as exc:  # noqa: BLE001
            await message.answer(f"❌ Не открылся канал: <code>{escape(_safe_err(exc)[:200])}</code>")
            return
        if not got:
            await message.answer("Не нашёл канал. Пришли ссылку вида <code>/trailers add https://www.youtube.com/@Vertical_Official</code>")
            return
        mine[got[1]] = got[0]
        await set_shorts_setting("yt_channels", json.dumps(mine, ensure_ascii=False))
        await message.answer(f"✅ Канал «{escape(got[1])}» добавлен — новые трейлеры с него тоже буду присылать.")
        return
    if arg.startswith("del "):
        name = raw_arg[4:].strip()
        if mine.pop(name, None) is None:
            await message.answer("Такого своего канала нет. Список — /trailers")
            return
        await set_shorts_setting("yt_channels", json.dumps(mine, ensure_ascii=False))
        await message.answer(f"✅ Канал «{escape(name)}» убран.")
        return
    wait = await message.answer("🎬 Смотрю свежие трейлеры на каналах студий…")
    await wait.edit_text(await trailers_overview(), disable_web_page_preview=True)


async def trailers_overview() -> str:
    """Свежие трейлеры с YouTube-каналов студий и как управлять уведомлениями (/trailers и кнопка в меню)."""
    mine = await _json_setting("yt_channels", {})
    on = await shorts_setting("yt_trailers") != "off"
    found = await shorts_yttrailers.latest(hours=72, extra=mine)
    rows = [f"• {escape(v['studio'])}: <a href=\"{v['url']}\">{escape(v['title'][:80])}</a>" for v in found[:15]]
    return (
        f"🎬 <b>Трейлеры с YouTube-каналов студий</b> — {'включены ✅' if on else 'выключены ❌'} (проверка раз в "
        f"30 минут, с 8 до 23).\nКаналы: {escape(', '.join(shorts_yttrailers.STUDIOS))}."
        + (f"\nТвои каналы: {escape(', '.join(mine))}." if mine else "") + "\n\nЗа 3 дня:\n"
        + ("\n".join(rows) if rows else "новых трейлеров нет")
        + "\n\n<code>/trailers add ссылка на канал</code> — добавить свой канал (трейлеры любой студии)\n"
          "<code>/trailers del Название</code> — убрать свой канал\n"
          "<code>/trailers off</code> — не присылать, <code>/trailers on</code> — присылать")


async def rt_watch(bot: Bot) -> None:
    """Проверка премьер фильмов и сезонов: критики увидели (сняли эмбарго) или появились оценки зрителей —
    пишем админам. Отметку «уже сообщили» ставим только после отправки: что не влезло в лимит прохода,
    придёт в следующий раз."""
    if await shorts_setting("rt_alerts") == "off" or not TMDB_API_KEY:
        return
    today = datetime.now(MSK).date()
    items = await _rt_premieres()
    state, sent = await _rt_state(), 0
    changed: dict[str, dict[str, Any]] = {}
    for item in items:
        key, tv = _rt_key(item), item.get("kind") == "tv"
        st = dict(state.get(key) or {})
        if "c" in st and "a" in st:
            changed[key] = st  # обо всём уже сообщили — страницу не качаем, но помним (не вытеснит лимит 300)
            continue
        if not st.get("url"):
            if st.get("tries", 0) >= 3:
                if time.time() - st.get("ts", 0) < 86400:
                    continue
                st["tries"] = 0  # через сутки — новая попытка (RT мог быть недоступен)
            st["tries"] = st.get("tries", 0) + 1
            try:
                st["url"] = await _rt_find(item, web=False)
            except Exception as exc:  # noqa: BLE001
                logging.info("RT watch: find %s failed: %s", item["original"], exc)
            changed[key] = st
            if not st.get("url"):
                continue
        try:
            sc = await shorts_rt.page_scores(st["url"])
        except Exception as exc:  # noqa: BLE001
            logging.info("RT watch: %s failed: %s", st["url"], exc)
            continue
        first, old = not st.get("seen"), item["date"] < (today - timedelta(days=3)).isoformat()
        st["seen"] = 1
        name = escape(_rt_name(item))
        when = f"премьера{' сезона' if tv else ' в США'} {item['date'][8:10]}.{item['date'][5:7]}"
        enough = RT_CRITICS_MIN_TV if tv else RT_CRITICS_MIN
        notes: list[tuple[str, str, str, int]] = []  # (текст, кнопка, отметка, оценка)
        count = sc.get("critics_count")  # нет числа рецензий — верим оценке; 0 или мало — рано
        if sc.get("critics") is not None and (count is None or count >= enough) and "c" not in st:
            if first and old:  # давно вышедший фильм при первой проверке — молча запоминаем
                st["c"] = sc["critics"]
            else:
                notes.append((f"🍅 <b>Критики посмотрели «{name}»</b> ({escape(item['original'])}, {when}): "
                              f"<b>{sc['critics']}%</b> на Rotten Tomatoes"
                              + (f" ({sc['critics_count']} рецензий)" if sc.get("critics_count") else "")
                              + ". Эмбарго снято — можно сделать пост первыми.", f"rt:c:{key}", "c", sc["critics"]))
        if sc.get("audience") is not None and "a" not in st:
            if first and old:
                st["a"] = sc["audience"]
            else:
                notes.append((f"🍿 <b>Появились оценки зрителей «{name}»</b>: <b>{sc['audience']}%</b> на Rotten "
                              "Tomatoes" + (f" (критики — {sc['critics']}%)" if sc.get("critics") is not None
                                            else "") + ".", f"rt:a:{key}", "a", sc["audience"]))
        for text, data, mark, score in notes:
            if sent >= 4:
                break  # остальное — в следующую проверку (отметку не ставим)
            sent += 1
            markup = InlineKeyboardMarkup(inline_keyboard=[
                [_btn("🍅 Сделать пост" if data.startswith("rt:c") else "🍿 Сделать пост", data)],
                [_btn("🔕 Не присылать такие уведомления", "rt:off")]])
            delivered = False
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_message(admin_id, text, reply_markup=markup)
                    delivered = True
                except Exception:  # noqa: BLE001
                    pass
            if delivered:
                st[mark] = score
        changed[key] = st
    if not changed:
        return
    # пока мы ходили в сеть, _rt_url мог запомнить новые страницы — сливаем, а не затираем
    fresh = await _rt_state()
    now = time.time()
    for key, st in changed.items():
        fresh[key] = {**(fresh.get(key) or {}), **st, "ts": now}
    if len(fresh) > 300:  # забываем то, что дольше всех не трогали
        fresh = dict(sorted(fresh.items(), key=lambda kv: (kv[1] or {}).get("ts", 0))[-300:])
    await set_shorts_setting("rt_watch", json.dumps(fresh))


@router.callback_query(F.data.startswith("rt:"))
async def rt_buttons(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id not in ADMIN_IDS or not callback.message:
        await callback.answer()
        return
    await callback.answer()
    message = callback.message
    _, kind, arg = ((callback.data or "") + "::").split(":")[:3]
    if kind in ("on", "off"):
        await set_shorts_setting("rt_alerts", None if kind == "on" else "off")
        await message.answer("🔔 Буду сообщать, когда критики посмотрят премьеру фильма или сезона сериала и когда "
                             "появятся оценки зрителей." if kind == "on" else
                             "🔕 Уведомления о снятии эмбарго выключены. Включить — 📝 Посты → 🎂 Даты и люди → 🍅 Что пишут критики.")
        return
    if kind == "q":
        await state.set_state(PostState.rt)
        await message.answer("✏️ Напиши название фильма или сериала — можно по-русски или в оригинале. Для фильма "
                             "лучше с годом, для сериала — с номером сезона:\n<code>Аватар: Огонь и пепел</code>\n"
                             "<code>Avatar: Fire and Ash 2025</code>\n<code>Пацаны 5 сезон</code>\n"
                             "Без номера сезона возьму новый (если скоро выходит) или последний вышедший."
                             "\n\n<i>Передумал — /cancel</i>")
        return
    if not RT_KEY.fullmatch(arg):
        return
    if kind == "f":
        if arg.startswith("t") and "s" in arg and not await _rt_item(arg):  # сезона с таким номером нет
            await message.answer("🤷 У этого сериала на TMDB нет такого сезона. Напиши название с другим номером.")
            return
        if arg.startswith("t") and "s" not in arg:  # сериал без сезона — текущий сезон
            item = await _rt_item(arg)
            if not item:
                await message.answer("🤷 У этого сериала на TMDB нет сезонов с датами.")
                return
            arg = _rt_key(item)
            await message.answer(f"📺 «{escape(_rt_name(item))}» (премьера сезона {item['date'] or '—'}). "
                                 "Другой сезон — напиши название с номером через «✏️ Другой фильм или сериал».")
        await message.answer("Что сделать?", reply_markup=_rt_film_buttons(arg))
        return
    if kind in ("c", "a"):
        if post_lock.locked():
            await message.answer("⏳ Пост уже пишется — подожди минуту и нажми кнопку ещё раз.")
            return
        wait = await message.answer("🍿 Читаю отзывы зрителей на Rotten Tomatoes, ИИ пишет пост…" if kind == "a" else
                                    "🍅 Читаю рецензии критиков на Rotten Tomatoes, ИИ пишет пост…")
        chat_id = message.chat.id

        async def rt_job() -> None:
            try:
                async with post_lock:
                    await make_rt_post(message.bot, chat_id, arg, kind == "a")
                await wait.delete()
            except Exception as exc:  # noqa: BLE001
                logging.exception("RT post failed")
                await wait.edit_text(f"❌ Пост не получился: <code>{escape(_safe_err(exc)[:300])}</code>")

        _run_job(rt_job)


@router.message(PostState.rt, F.text, ~F.text.startswith("/"))
async def rt_title(message: Message, state: FSMContext) -> None:
    """Название фильма или сериала (с годом или номером сезона) → кнопки «критики» и «зрители»."""
    await state.clear()
    query = (message.text or "").strip()[:100]
    season = re.search(r"(\d{1,2})\s*(?:-?й\s*)?сезон|сезон\s*(\d{1,2})|season\s*(\d{1,2})|\bs(\d{1,2})\b", query, re.I)
    year = re.search(r"\b(19|20)\d{2}\b", query)
    name = re.sub(r"(\d{1,2})\s*(?:-?й\s*)?сезон\w*|сезон\w*\s*\d{1,2}|season\s*\d{1,2}|\bs\d{1,2}\b|\b(19|20)\d{2}\b",
                  "", query, flags=re.I).strip(" ,()-—")
    if not name:  # «1917», «2012» — это название, а не год
        name, year = query.strip(), None
    try:
        films, shows = await asyncio.gather(shorts_daily.tmdb.search_movie(TMDB_API_KEY, name),
                                            shorts_daily.tmdb.search_tv(TMDB_API_KEY, name), return_exceptions=True)
    except Exception as exc:  # noqa: BLE001
        await message.answer(f"❌ Поиск не удался: <code>{escape(_safe_err(exc)[:200])}</code>")
        return
    if isinstance(films, BaseException) and isinstance(shows, BaseException):
        await message.answer(f"❌ Поиск не удался: <code>{escape(_safe_err(films)[:200])}</code>")
        return
    films = films if isinstance(films, list) else []
    shows = shows if isinstance(shows, list) else []
    n = int(next(g for g in season.groups() if g)) if season else 0
    if season:  # просили сезон — значит, сериал
        films = []
    if year:
        films = sorted(films, key=lambda f: f["year"] != year.group(0))
    options = ([(f"📺 {s['title'][:38]} ({s['year'] or '—'})" + (f" — {n} сезон" if n else ""),
                 f"t{s['id']}" + (f"s{n}" if n else "")) for s in shows[:4]]
               + [(f"🎬 {f['title'][:40]} ({f['year'] or '—'})", str(f["id"])) for f in films[:4]])
    if not films and shows and n:  # сериал с номером сезона — проверяем, что такой сезон есть
        item = await _rt_item(f"t{shows[0]['id']}s{n}")
        if not item and len(shows) == 1:
            await message.answer(f"🤷 У сериала «{escape(shows[0]['title'])}» на TMDB нет {n} сезона.")
            return
        if item and len(shows) == 1:
            await message.answer(f"📺 «{escape(_rt_name(item))}» (премьера сезона {item['date'] or '—'}). Что сделать?",
                                 reply_markup=_rt_film_buttons(_rt_key(item)))
            return
    if not options:
        await message.answer("🤷 Не нашёл такой фильм или сериал. Попробуй оригинальное название.")
        return
    if len(options) == 1 and not options[0][1].startswith("t"):
        await message.answer(f"{options[0][0]}. Что сделать?", reply_markup=_rt_film_buttons(options[0][1]))
        return
    await message.answer("Что именно?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [_btn(text, f"rt:f:{key}")] for text, key in options]))


# --- еженедельный выпуск «Новости кино» для YouTube (воскресенье, 10:00 по Екатеринбургу)

WEEKLY_TIMEOUT = 80 * 60  # выпуск на 8 минут собирается дольше шортса
WEEKLY_DAYS, WEEKLY_HOUR = (2, 6), 10  # среда и воскресенье, 10:00 (время автора)


def _spread_by_day(posts: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Посты понемногу за каждый день (по очереди: самый свежий каждого дня, потом второй…) — иначе при
    многих каналах все места заняли бы новости последних суток."""
    days: dict[str, list[dict[str, Any]]] = {}
    for p in sorted(posts, key=lambda p: -p.get("time", 0)):
        day = datetime.fromtimestamp(p.get("time", 0), MSK).date().isoformat()
        days.setdefault(day, []).append(p)
    out: list[dict[str, Any]] = []
    while len(out) < limit and any(days.values()):
        for day in sorted(days, reverse=True):
            if days[day] and len(out) < limit:
                out.append(days[day].pop(0))
    return out


weekly_busy = False  # выпуск собирается или ждёт очереди — второй не запускаем


async def make_weekly_video(bot: Bot, chat_ids: list[int], status: Message | None = None,
                            remake: bool = False) -> None:
    """Новости недели (Telegram-каналы и быстрые зарубежные источники) → сценарий ведущего → выпуск 16:9 с
    голосом, картинками, заставками и таймкодами → превью в Telegram и кнопка загрузки на YouTube."""
    global weekly_busy
    if weekly_busy:
        raise RuntimeError("выпуск уже собирается — дождись его, он придёт сюда")
    weekly_busy = True
    try:
        # новый выпуск дальше идёт по шагам с утверждением автора — время выпуска запишет итоговая склейка
        await _make_weekly_video(bot, chat_ids, status, remake)
    finally:
        weekly_busy = False


async def _edit_quiet(message: Message | None, text: str) -> None:
    """Обновить строку статуса; «message is not modified» (тот же текст) и прочие сбои правки — не ошибка сборки."""
    if message is None:
        return
    try:
        await message.edit_text(text)
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc):
            logging.info("Status edit failed: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logging.info("Status edit failed: %s", exc)


def _saved_pool(pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Посты-источники выпуска для перемонтажа: текст и картинки (номера постов в сценарии — по этому списку)."""
    keep = ("text", "channel", "label", "image", "time")
    out = []
    for p in pool:
        row = {k: p[k] for k in keep if p.get(k)}
        row["text"] = str(row.get("text") or "")[:600]
        media = [{"image": m["image"]} for m in p.get("media") or [] if isinstance(m, dict) and m.get("image")]
        if media:
            row["media"] = media[:4]
        out.append(row)
    return out


async def _make_weekly_video(bot: Bot, chat_ids: list[int], status: Message | None = None,
                             remake: bool = False) -> None:
    """remake — перемонтировать последний выпуск: тот же сценарий заново озвучить (с новыми ударениями из /say)
    и смонтировать; если сценарий не сохранился (выпуск собран до этой версии) — те же темы, текст заново."""
    saved: dict[str, Any] = {}
    if remake:
        try:
            saved = json.loads(await shorts_setting("weekly_saved") or "{}")
        except ValueError:
            saved = {}
        if saved.get("script") and saved.get("pool") is not None:
            await _render_weekly(bot, chat_ids, status, saved["script"], saved["pool"], saved.get("label") or "")
            return
        if not await shorts_setting("weekly_last"):
            raise RuntimeError("не нашёл прошлый выпуск — собери новый")
    # новости и сценарий — до очереди сборки: сеть и ИИ не держат шортсы, очередь только на монтаж
    today = datetime.now(MSK).date()
    # выпуски по средам и воскресеньям: новости с прошлого выпуска (3–4 дня), но не меньше 3 и не больше 7 дней
    last = float(await shorts_setting("weekly_video_at") or 0)
    hours = min(168, max(72, (time.time() - last) / 3600 + 12)) if last else 168
    if remake:  # выпуск уже вышел (время записано после него) — берём новости последних 4 дней
        hours = 96
    start = (datetime.now(MSK) - timedelta(hours=hours)).date()
    months = shorts_daily.MONTHS
    label = (f"{start.day}–{today.day} {months[today.month - 1]}" if start.month == today.month else
             f"{start.day} {months[start.month - 1]} — {today.day} {months[today.month - 1]}")
    period = "за неделю" if hours >= 120 else f"за последние {round(hours / 24)} дня"
    next_day = "в воскресенье" if today.weekday() in (0, 1, 2, 3) else "в среду"
    own = POST_CHANNEL.lstrip("@").lower()
    channels = [c for c in await fact_channels() if c.lower() != own]
    pages = 2 if hours <= 96 else 4  # листаем каналы назад: страница — ~20 постов (у активного канала это сутки)
    posts = await shorts_tgsearch.latest(channels, hours=hours, per_channel=80, pages=pages)
    try:  # свой канал — то, что автор уже отобрал и опубликовал (там смерть Патрика Стюарта и т. п.)
        mine = await shorts_tgsearch.latest([own], hours=hours, per_channel=40, pages=pages)
    except Exception as exc:  # noqa: BLE001
        logging.info("Weekly video: own channel read failed: %s", exc)
        mine = []
    try:
        fast = await shorts_fast.latest(await fast_feeds(), hours=hours, per_feed=25, timeout=100)
    except Exception as exc:  # noqa: BLE001
        logging.info("Weekly video: fast sources failed: %s", exc)
        fast = []
    for p in mine:
        p["label"] = "НАШ КАНАЛ (уже опубликовано — важные новости)"
    pool = mine[:30] + _spread_by_day(posts, 80) + _spread_by_day(fast, 50)  # все дни, а не последние сутки
    if len(pool) < 10:
        raise RuntimeError("за эти дни почти нет новостей в источниках — проверь каналы (/channels) и /fast")
    listing, size = [], 0
    for i, p in enumerate(pool, 1):
        text = re.sub(r"\s+", " ", p["text"])[:450]
        row = f"{i}. [{p.get('label') or 't.me/' + p['channel']}] {text}"
        if size + len(row) > 30000:
            break
        listing.append(row)
        size += len(row)
    if status:
        await _edit_quiet(status, "🎞 ИИ выбирает главные новости недели и пишет сценарий выпуска…")
    script = None
    for attempt in range(3):  # Gemini упёрся в минутный лимит — ждём и пробуем снова, а не бросаем выпуск
        script = await shorts_ai.write_weekly_episode(
            GEMINI_API_KEY, "\n\n".join(listing), f"{today.day} {months[today.month - 1]} {today.year}",
            await shorts_setting("weekly_last") or "", period=period, next_day=next_day,
            keep=(await shorts_setting("weekly_last") or "") if remake else "")
        if script:
            break
        wait = re.search(r"снова через (\d+) мин", shorts_ai.last_error or "")
        if not wait or attempt == 2 or int(wait.group(1)) > 20:
            break
        mins = int(wait.group(1)) + 1
        if status:
            await _edit_quiet(status, f"⏳ ИИ сейчас на минутном лимите — подожду {mins} мин и продолжу сценарий выпуска…")
        await asyncio.sleep(mins * 60)
    if not script:
        raise RuntimeError(shorts_ai.last_error or "ИИ не написал сценарий выпуска")
    if remake:
        await _render_weekly(bot, chat_ids, status, script, pool[:len(listing)], label)
        return
    # новый выпуск — по шагам: тексты новостей на утверждение, потом каждая новость роликом, потом склейка
    await _edit_script(script, status)
    await start_weekly_draft(bot, chat_ids, script, pool[:len(listing)], label, "\n\n".join(listing),
                             f"{today.day} {months[today.month - 1]} {today.year}", period, next_day)


async def _edit_script(script: dict[str, Any], status: Message | None) -> None:
    # номера фраз («2. …»), которые редактор раньше оставлял в тексте, — голос читал их вслух
    for lines in [script.get("intro") or [], script.get("outro") or []] + [st["lines"] for st in script["stories"]]:
        lines[:] = [shorts_ai._unnumber(x) for x in lines]
    # редактор-корректор: имена, склонение, грамматика, речевые ошибки — и заново, если его правила обновились
    if script.get("edited") != shorts_ai.EDITOR_VERSION:
        await _edit_quiet(status, "✍️ Редактор вычитывает текст выпуска: имена, падежи, грамматика, речевые ошибки…")
        fixes = await shorts_ai.edit_episode(GEMINI_API_KEY, script)
        if fixes is not None:
            script["fixes"] = fixes


async def _render_weekly(bot: Bot, chat_ids: list[int], status: Message | None, script: dict[str, Any],
                         pool: list[dict[str, Any]], label: str) -> None:
    await _edit_script(script, status)
    if status and shorts_lock.locked():
        await _edit_quiet(status, "⏳ Сценарий готов. Жду, пока соберётся другой ролик, потом монтирую выпуск…")
    async with shorts_lock:
        await load_pronunciations()
        await load_voice()
        work = SHORTS_DIR / f"weekly_{time.time_ns()}"
        work.mkdir(parents=True, exist_ok=True)
        try:
            await set_shorts_setting("running", json.dumps({"chats": chat_ids, "theme": "выпуск «Новости кино»"}))
            shorts_daily.on_progress, shorts_daily.last_stage = progress_updater(status, "выпуск"), ""
            try:
                ep = await asyncio.wait_for(shorts_weekly.render(work, script, pool, TMDB_API_KEY, label),
                                            timeout=WEEKLY_TIMEOUT)
            except asyncio.TimeoutError:
                raise RuntimeError(f"сборка шла дольше {WEEKLY_TIMEOUT // 60} минут и была прервана — застряла на "
                                   f"этапе: {shorts_daily.last_stage or 'начало'}") from None
            await set_shorts_setting("weekly_last", shorts_weekly.headlines(script))
            try:  # для «🔁 Переозвучить и перемонтировать»: тот же текст и те же картинки
                await set_shorts_setting("weekly_saved", json.dumps(
                    {"script": script, "pool": _saved_pool(pool), "label": label, "at": time.time()},
                    ensure_ascii=False))
            except Exception:  # noqa: BLE001
                logging.exception("Weekly script save failed")
            for chat_id in chat_ids:
                await send_episode(bot, chat_id, ep, script)
        finally:
            shorts_daily.on_progress = None
            await clear_running()
            shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- выпуск по шагам: тексты → ролики → склейка
# 1) каждая новость текстом: «✅ Принять», «✍️ Переписать», «🔄 Другая новость»; вступление и финал — так же;
# 2) все тексты приняты — каждая новость монтируется отдельным роликом: «✅ Принять», «🎙 Звук», «🎞 Видео»;
#    «Звук» — только исправленные фразы (картинка та же), «Видео» — другие картинки (голос тот же, из кэша);
# 3) все ролики приняты — склейка в выпуск. Черновик — в базе (weekly_draft), куски — в томе /data: обновление
#    или перезапуск бота ничего не теряет, монтаж продолжается с того куска, где остановился.

WEEKLY_DRAFT_DIR = shorts_daily.DATA_DIR / "weekly_draft"
weekly_draft_lock = asyncio.Lock()
weekly_draft_job: asyncio.Task | None = None


async def wd_load() -> dict[str, Any] | None:
    raw = await shorts_setting("weekly_draft")
    try:
        draft = json.loads(raw) if raw else None
    except ValueError:
        return None
    return draft if isinstance(draft, dict) and draft.get("script") else None


async def wd_update(change: Any) -> dict[str, Any] | None:
    """Прочитать черновик, поменять (change(draft)) и записать — под замком: кнопки и монтаж не затрут друг друга."""
    async with weekly_draft_lock:
        draft = await wd_load()
        if draft is None:
            return None
        change(draft)
        await set_shorts_setting("weekly_draft", json.dumps(draft, ensure_ascii=False, default=str))
        return draft


def wd_keys(draft: dict[str, Any]) -> list[str]:
    return ["i"] + [str(n) for n in range(1, len(draft["script"]["stories"]) + 1)] + ["o"]


def _wd_sources(draft: dict[str, Any], story: dict[str, Any]) -> str:
    out = []
    for i in story.get("media") or []:
        if 1 <= i <= len(draft["pool"]):
            post = draft["pool"][i - 1]
            out.append(post.get("label") if post.get("label") and "НАШ" not in post["label"] else
                       f"t.me/{post.get('channel')}" if post.get("channel") else "")
    out = [x for x in dict.fromkeys(out) if x]
    return ("📎 Источники: " + ", ".join(escape(x) for x in out[:4])) if out else "📎 Источник не указан — проверь факт"


def _wd_text(draft: dict[str, Any], key: str) -> str:
    script, ok = draft["script"], draft["text_ok"].get(key)
    mark = "✅ Принято" if ok else "⏳ Ждёт решения"
    if key == "i":
        return (f"🎙 <b>Вступление и финал</b> — {mark}\n\n<b>Вступление:</b> {escape(' '.join(script['intro']))}\n\n"
                f"<b>Финал:</b> {escape(' '.join(script['outro']))}")
    n = int(key)
    story = script["stories"][n - 1]
    body = escape(" ".join(story["lines"]))
    return (f"📰 <b>Новость {n} из {len(script['stories'])}: {escape(story['headline'])}</b> — {mark}\n\n{body}\n\n"
            + _wd_sources(draft, story))[:4000]


def _wd_text_kb(key: str) -> InlineKeyboardMarkup:
    row = [_btn("✅ Принять", f"wd:ta:{key}"), _btn("✍️ Переписать", f"wd:tr:{key}")]
    rows = [row, [_btn("📝 Свой текст", f"wd:te:{key}")]
            + ([_btn("🔄 Другая новость", f"wd:tn:{key}")] if key not in ("i", "o") else [])]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _wd_phrases(text: str) -> list[str]:
    """Текст автора → фразы для голоса (по предложениям), без пустых и служебных строк."""
    text = re.sub(r"📎 Источник.*", "", text, flags=re.S)
    return [x.strip() for x in re.split(r"(?<=[.!?…])\s+|\n+", text) if len(x.strip()) >= 2]


@router.message(WeeklyEditState.text, F.text, ~F.text.startswith("/"))
async def weekly_own_text(message: Message, state: FSMContext) -> None:
    """Свой текст новости (или вступления и финала): заменяет текст ИИ как есть, без переписывания."""
    data = await state.get_data()
    await state.clear()
    key = str(data.get("wd_key") or "")
    text = message.text or ""
    head = re.match(r"\s*📰[^\n]*\n+", text)  # скопировали сообщение целиком — шапку отрезаем
    if head:
        text = text[head.end():]
    if key == "i":
        intro_part, _, outro_part = re.sub(r"^\s*🎙[^\n]*\n+", "", text).partition("Финал:")
        intro = _wd_phrases(intro_part.replace("Вступление:", ""))
        outro = _wd_phrases(outro_part)
        if not intro:
            await message.answer("Не вижу текста вступления — нажми «📝 Свой текст» ещё раз.")
            return
    else:
        lines = _wd_phrases(text)
        if len(lines) < 2:
            await message.answer("Слишком коротко для новости — нажми «📝 Свой текст» ещё раз и пришли текст целиком.")
            return

    def put(d: dict[str, Any]) -> None:
        d["text_ok"][key] = False
        if key == "i":
            d["script"]["intro"] = intro
            if outro:
                d["script"]["outro"] = outro
        else:
            d["script"]["stories"][int(key) - 1]["lines"] = lines

    draft = await wd_update(put)
    if not draft or draft.get("stage") != "text":
        await message.answer("Черновик выпуска уже не на шаге текстов — свой текст можно прислать до монтажа.")
        return
    await message.answer("✅ Текст заменён на твой. Проверь и нажми «✅ Принять»:\n\n" + _wd_text(draft, key),
                         reply_markup=_wd_text_kb(key), disable_web_page_preview=True)


def _wd_video_kb(key: str) -> InlineKeyboardMarkup:
    row = [_btn("✅ Принять", f"wd:va:{key}"), _btn("🎙 Звук", f"wd:vs:{key}")]
    if key not in ("i", "o"):
        row.append(_btn("🎞 Видео", f"wd:vv:{key}"))
    return InlineKeyboardMarkup(inline_keyboard=[row, [_btn("🔁 Заново: голос и видео", f"wd:vf:{key}")]])


async def start_weekly_draft(bot: Bot, chat_ids: list[int], script: dict[str, Any], pool: list[dict[str, Any]],
                             label: str, listing: str, today: str, period: str, next_day: str) -> None:
    """Шаг 1: тексты новостей — по одной, с кнопками. Монтаж начнётся, когда всё принято."""
    old = await wd_load()
    if old:
        shutil.rmtree(WEEKLY_DRAFT_DIR / str(old.get("id")), ignore_errors=True)
    draft = {"id": str(time.time_ns()), "chats": chat_ids, "chat": chat_ids[0], "script": script,
             "pool": _saved_pool(pool), "label": label, "listing": listing[:30000], "today": today,
             "period": period, "next_day": next_day, "stage": "text", "text_ok": {}, "rejected": [],
             "blocks": {}, "video_ok": {}, "urls": {}}
    await set_shorts_setting("weekly_draft", json.dumps(draft, ensure_ascii=False, default=str))
    chat = draft["chat"]
    await bot.send_message(chat, f"🗞 <b>Выпуск «Новости кино» ({escape(label)}) — шаг 1 из 3: тексты.</b>\n"
                                 "Прочитай каждую новость: «✅ Принять», «✍️ Переписать» (та же новость, новый текст) "
                                 "или «🔄 Другая новость» (устарела или неинтересна). Когда всё принято — смонтирую "
                                 "каждую новость отдельным роликом.")
    for key in ["i"] + [str(n) for n in range(1, len(script["stories"]) + 1)]:
        await bot.send_message(chat, _wd_text(draft, key), reply_markup=_wd_text_kb(key),
                               disable_web_page_preview=True)


async def _wd_rewrite(draft: dict[str, Any], key: str, other: bool) -> dict[str, Any]:
    """Новый текст той же новости или другая новость из тех же источников недели (с вычиткой редактора)."""
    script = draft["script"]
    if key == "i":
        got = await shorts_ai.write_weekly_bookends(GEMINI_API_KEY, [s["headline"] for s in script["stories"]],
                                                    draft["next_day"])
        if not got:
            raise RuntimeError(shorts_ai.last_error or "ИИ не написал вступление")
        return got
    story = script["stories"][int(key) - 1]
    if other:
        busy = [s["headline"] for s in script["stories"]] + draft["rejected"]
        new = await shorts_ai.write_weekly_episode(GEMINI_API_KEY, draft["listing"], draft["today"],
                                                   "\n".join(busy), n=3, period=draft["period"],
                                                   next_day=draft["next_day"])
        cands = [s for s in (new or {}).get("stories") or [] if s["headline"] not in busy]
    else:
        keep = f"{story['headline']}: {' '.join(story['lines'][:2])}"
        new = await shorts_ai.write_weekly_episode(GEMINI_API_KEY, draft["listing"], draft["today"], n=1,
                                                   period=draft["period"], next_day=draft["next_day"], keep=keep)
        cands = (new or {}).get("stories") or []
    if not cands:
        raise RuntimeError(shorts_ai.last_error or ("ИИ не нашёл другой новости" if other else "ИИ не переписал"))
    fresh = cands[0]
    fresh["lines"] = [shorts_ai._unnumber(x) for x in fresh["lines"]]
    edited = await shorts_ai.edit_lines(GEMINI_API_KEY, fresh["lines"])
    if edited:
        fresh["lines"] = edited[0]
    return fresh


async def wd_text_action(callback: CallbackQuery, action: str, key: str) -> None:
    message = callback.message
    if action == "ta":
        def accept(d: dict[str, Any]) -> None:
            d["text_ok"][key] = True
        draft = await wd_update(accept)
        if not draft:
            await callback.answer("Черновик выпуска не найден — собери выпуск заново", show_alert=True)
            return
        await callback.answer("✅ Принято")
        await _edit_quiet_markup(message, _wd_text(draft, key), _wd_text_kb(key))
        if draft["stage"] == "text" and all(draft["text_ok"].get(k) for k in wd_keys(draft) if k != "o"):
            await wd_update(lambda d: d.update(stage="video"))
            await message.answer("✅ Все тексты приняты. <b>Шаг 2 из 3:</b> монтирую каждую новость отдельным роликом "
                                 "— присылаю по мере готовности (обычно 2–4 минуты на новость).")
            wd_start_video(message.bot)
        return
    await callback.answer("✍️ Пишу заново…" if action == "tr" else "🔄 Ищу другую новость…")
    draft = await wd_load()
    if not draft or draft["stage"] != "text":
        await message.answer("Тексты уже утверждены — менять новость можно до начала монтажа.")
        return
    try:
        fresh = await _wd_rewrite(draft, key, action == "tn")
    except Exception as exc:  # noqa: BLE001
        wait = getattr(exc, "retry_in", None)
        await message.answer(f"❌ Не получилось: <code>{escape(_safe_err(exc)[:300])}</code>"
                             + (f"\n⏸ Все ИИ на лимитах — ближайший освободится через ≈{max(1, round(wait / 60))} мин. "
                                "Тексты и принятые новости сохранены, нажми кнопку снова позже." if wait else ""))
        return

    def put(d: dict[str, Any]) -> None:
        d["text_ok"][key] = False
        if key == "i":
            d["script"]["intro"], d["script"]["outro"] = fresh["intro"], fresh["outro"]
            return
        old = d["script"]["stories"][int(key) - 1]
        if action == "tn":
            d["rejected"].append(old["headline"])
            d["text_ok"]["i"] = False  # анонс во вступлении говорил о старой новости
        d["script"]["stories"][int(key) - 1] = fresh

    draft = await wd_update(put)
    await _edit_quiet_markup(message, _wd_text(draft, key), _wd_text_kb(key))
    if action == "tn":  # вступление — заново под новый набор новостей
        try:
            ends = await _wd_rewrite(draft, "i", False)
            draft = await wd_update(lambda d: d["script"].update(intro=ends["intro"], outro=ends["outro"]))
            await message.answer("🔄 Новость заменена — вступление переписано под новый набор, проверь его:\n\n"
                                 + _wd_text(draft, "i"), reply_markup=_wd_text_kb("i"))
        except Exception as exc:  # noqa: BLE001
            logging.info("Weekly bookends rewrite failed: %s", exc)


async def _edit_quiet_markup(message: Message | None, text: str, kb: InlineKeyboardMarkup) -> None:
    if message is None:
        return
    try:
        if message.text is not None:
            await message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
        else:
            await message.edit_caption(caption=text[:1000], reply_markup=kb)
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc):
            logging.info("Draft edit failed: %s", exc)


def wd_start_video(bot: Bot) -> None:
    """Монтаж недостающих кусков — одной задачей (повторное нажатие не запустит вторую)."""
    global weekly_draft_job
    if weekly_draft_job and not weekly_draft_job.done():
        return
    weekly_draft_job = asyncio.create_task(wd_video_job(bot))
    shorts_jobs.add(weekly_draft_job)
    weekly_draft_job.add_done_callback(shorts_jobs.discard)


def _wd_caption(draft: dict[str, Any], key: str, block: dict[str, Any], note: str = "") -> str:
    total = len(draft["script"]["stories"])
    name = {"i": "Вступление", "o": "Финал"}.get(key) or f"Новость {key} из {total}: {block['title']}"
    mins = f"{int(block['length']) // 60}:{int(block['length']) % 60:02d}"
    mark = "✅ Принято" if draft["video_ok"].get(key) else "⏳ Посмотри и реши"
    text = f"🎞 <b>{escape(name)}</b> ({mins}) — {mark}"
    if note:
        text += "\n" + note
    if block.get("listen"):
        text += "\n🎧 Послушай: " + escape("; ".join(block["listen"][:3]))
    return text[:1000]


async def _wd_send_block(bot: Bot, draft: dict[str, Any], key: str, note: str = "") -> None:
    work = WEEKLY_DRAFT_DIR / draft["id"]
    block = draft["blocks"][key]
    preview = await shorts_weekly.block_preview(work, block, work / f"preview_{key}.mp4")
    if preview is None:
        await bot.send_message(draft["chat"], _wd_caption(draft, key, block, note) + "\n(превью не влезло в Telegram)",
                               reply_markup=_wd_video_kb(key))
        return
    try:
        await bot.send_video(draft["chat"], FSInputFile(preview, filename=f"novost_{key}.mp4"),
                             caption=_wd_caption(draft, key, block, note), reply_markup=_wd_video_kb(key),
                             width=854, height=480, supports_streaming=True, request_timeout=600)
    finally:
        preview.unlink(missing_ok=True)


async def _wd_pictures(draft: dict[str, Any], work: Path) -> tuple[list[list[Any]], Any]:
    """Картинки всех новостей (с прошлого раза — из папки черновика) и главная картинка выпуска."""
    from PIL import Image as PILImage
    script = draft["script"]
    pictures, urls = await shorts_weekly.gather_pictures(script, draft["pool"], TMDB_API_KEY)
    draft_urls = {str(k + 1): u for k, u in enumerate(urls)}
    await wd_update(lambda d: d["urls"].update({k: v for k, v in draft_urls.items() if k not in d["urls"]}))
    first = next((imgs[0] for imgs in pictures if imgs), None)
    if first is not None:
        first.convert("RGB").save(work / "first.jpg", quality=92)
    for k, imgs in enumerate(pictures[:4]):
        if imgs:
            imgs[0].convert("RGB").save(work / f"teaser_src_{k}.jpg", quality=90)
    if first is None and (work / "first.jpg").exists():
        first = PILImage.open(work / "first.jpg").convert("RGB")
    return pictures, first


async def wd_video_job(bot: Bot) -> None:
    """Шаг 2: каждый кусок выпуска (вступление, новости, финал) — отдельным роликом; готовые не пересобираются."""
    global weekly_busy
    draft = await wd_load()
    if not draft or draft["stage"] != "video":
        return
    chat = draft["chat"]
    work = WEEKLY_DRAFT_DIR / draft["id"]
    work.mkdir(parents=True, exist_ok=True)
    todo = [k for k in wd_keys(draft) if k not in draft["blocks"]]
    if not todo:
        return
    status = await bot.send_message(chat, f"⏳ Монтирую {len(todo)} кусков выпуска…")
    weekly_busy = True
    try:
        async with shorts_lock:
            await load_pronunciations()
            await load_voice()
            await set_shorts_setting("running", json.dumps({"chats": [chat], "theme": "новости выпуска по одной"}))
            shorts_daily.on_progress, shorts_daily.last_stage = progress_updater(status, "новости выпуска"), ""
            pictures, first = await _wd_pictures(draft, work)
            script, music = draft["script"], shorts_weekly.main_music()
            total = len(script["stories"])
            for key in todo:
                if key == "i":
                    teaser = [imgs[0] for imgs in pictures[:4] if imgs]
                    job = shorts_weekly.build_intro(work, script, teaser, first, draft["label"], music)
                elif key == "o":
                    job = shorts_weekly.build_outro(work, script, first, music)
                else:
                    n = int(key)
                    job = shorts_weekly.build_story(work, script["stories"][n - 1], n, total, pictures[n - 1], first,
                                                    TMDB_API_KEY, music)
                block = await asyncio.wait_for(job, timeout=20 * 60)
                draft = await wd_update(lambda d, k=key, b=block: d["blocks"].update({k: b}))
                await _wd_send_block(bot, draft, key)
        await status.delete()
        await bot.send_message(chat, "👆 Все куски выпуска готовы. Прими каждый — после этого склею выпуск.")
    except Exception as exc:  # noqa: BLE001
        logging.exception("Weekly draft video failed")
        await _edit_quiet(status, f"❌ Монтаж остановился: <code>{escape(_safe_err(exc)[:300])}</code>\n"
                                  "Готовые куски сохранены — продолжу с этого места.")
        await bot.send_message(chat, "Продолжить монтаж выпуска?", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[_btn("▶️ Продолжить монтаж", "wd:go:0")]]))
    finally:
        shorts_daily.on_progress = None
        await clear_running()
        weekly_busy = False


async def wd_block_job(bot: Bot, key: str, mode: str, sure: bool = False) -> None:
    """«🎙 Звук» (только исправленные фразы) или «🎞 Видео» (другие картинки, голос из кэша) у одного куска."""
    draft = await wd_load()
    if not draft or key not in draft.get("blocks", {}):
        await bot.send_message(draft["chat"] if draft else ADMIN_IDS and sorted(ADMIN_IDS)[0],
                               "Этот кусок выпуска не найден — собери выпуск заново.")
        return
    chat = draft["chat"]
    if shorts_lock.locked():
        await bot.send_message(chat, "⏳ Сейчас идёт другой монтаж — нажми через пару минут.")
        return
    work = WEEKLY_DRAFT_DIR / draft["id"]
    status = await bot.send_message(chat, {"vs": "🎙 Переозвучиваю исправленные фразы…",
                                           "vv": "🎞 Подбираю другие картинки и монтирую заново (голос тот же)…"}.get(
        mode, "🔁 Записываю голос заново и монтирую кусок с другими картинками…"))
    try:
        async with shorts_lock:
            await load_pronunciations()
            await load_voice()
            block = draft["blocks"][key]
            from PIL import Image as PILImage
            first = PILImage.open(work / "first.jpg").convert("RGB") if (work / "first.jpg").exists() else None
            script = draft["script"]
            if mode == "vf":  # голос заново: озвученные фразы куска — из кэша вон, Lumean запишет их снова
                lines = script["intro"] if key == "i" else script["outro"] if key == "o" else \
                    script["stories"][int(key) - 1]["lines"]
                for text in await shorts_daily.spoken_lines(lines, careful=True):
                    shorts_daily._voice_key(shorts_daily._for_lumean(text), shorts_lumean.PAUSE).unlink(missing_ok=True)
            if mode == "vs":
                fixed = await shorts_weekly.revoice_block(work, block, sure)
                note = f"🎙 Переозвучено фраз: {len(fixed)}, картинка та же."
            elif key in ("i", "o"):
                music = shorts_weekly.main_music()
                if key == "i":
                    teaser = [PILImage.open(f).convert("RGB") for f in sorted(work.glob("teaser_src_*.jpg"))]
                    block = await shorts_weekly.build_intro(work, script, teaser, first, draft["label"], music)
                else:
                    block = await shorts_weekly.build_outro(work, script, first, music)
                note = "🔁 Голос записан заново."
            else:
                n = int(key)
                story = draft["script"]["stories"][n - 1]
                used: list[str] = []
                avoid = set(draft["urls"].get(key) or [])
                imgs = await shorts_weekly.story_images(story, draft["pool"], TMDB_API_KEY, avoid=avoid, used=used)
                block = await asyncio.wait_for(shorts_weekly.build_story(
                    work, story, n, len(draft["script"]["stories"]), imgs, first, TMDB_API_KEY,
                    shorts_weekly.main_music()), timeout=20 * 60)
                note = "🔁 Голос записан заново, другие картинки." if mode == "vf" else "🎞 Другие картинки, голос тот же."
                await wd_update(lambda d: d["urls"].update({key: list(avoid | set(used))}))

            def put(d: dict[str, Any]) -> None:
                d["blocks"][key] = block
                d["video_ok"][key] = False
            draft = await wd_update(put)
            await status.delete()
            await _wd_send_block(bot, draft, key, note)
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, shorts_project.TooLong):
            await _edit_quiet(status, f"⚠️ {escape(str(exc))}.\nНажми «🔁 Заново: голос и видео» — кусок соберётся "
                                      "под новую озвучку без наложений.")
        elif not await _revoice_guard(bot, chat, status, exc, f"wd:vs:{key}:go"):
            logging.exception("Weekly draft block fix failed")
            await _edit_quiet(status, f"❌ Не получилось: <code>{escape(_safe_err(exc)[:300])}</code>")


async def wd_assemble_job(bot: Bot) -> None:
    """Шаг 3: все куски приняты — склейка выпуска, обложка, SEO, отправка как обычно."""
    global weekly_busy
    draft = await wd_load()
    if not draft:
        return
    chat, work = draft["chat"], WEEKLY_DRAFT_DIR / draft["id"]
    status = await bot.send_message(chat, "✅ Все куски приняты. <b>Шаг 3 из 3:</b> склеиваю выпуск…")
    weekly_busy = True
    try:
        async with shorts_lock:
            from PIL import Image as PILImage
            first = PILImage.open(work / "first.jpg").convert("RGB") if (work / "first.jpg").exists() else None
            shorts_daily.on_progress, shorts_daily.last_stage = progress_updater(status, "выпуск"), ""
            blocks = [draft["blocks"][k] for k in wd_keys(draft)]
            ep = await shorts_weekly.assemble(work, draft["script"], blocks, draft["label"], first)
            script = draft["script"]
            await set_shorts_setting("weekly_last", shorts_weekly.headlines(script))
            await set_shorts_setting("weekly_video_at", str(time.time()))
            await set_shorts_setting("weekly_saved", json.dumps(
                {"script": script, "pool": draft["pool"], "label": draft["label"], "at": time.time()},
                ensure_ascii=False))
            for chat_id in draft["chats"]:
                await send_episode(bot, chat_id, ep, script)
        await status.delete()
        await set_shorts_setting("weekly_draft", None)
        shutil.rmtree(work, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        logging.exception("Weekly draft assemble failed")
        await _edit_quiet(status, f"❌ Склейка не получилась: <code>{escape(_safe_err(exc)[:300])}</code>")
        await bot.send_message(chat, "Куски сохранены. Склеить ещё раз?", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[_btn("🔗 Склеить выпуск", "wd:as:0")]]))
    finally:
        shorts_daily.on_progress = None
        weekly_busy = False


@router.callback_query(F.data.startswith("wd:"))
async def weekly_draft_buttons(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id not in ADMIN_IDS or not callback.message:
        await callback.answer()
        return
    parts = (callback.data or "").split(":")
    action, key = parts[1], parts[2] if len(parts) > 2 else ""
    sure = parts[-1] == "go" and len(parts) > 3
    if action in ("ta", "tr", "tn"):
        await wd_text_action(callback, action, key)
        return
    if action == "te":
        await callback.answer()
        await state.set_state(WeeklyEditState.text)
        await state.update_data(wd_key=key)
        await callback.message.answer(
            "📝 Пришли свой текст " + ("вступления (а после строки «Финал:» — текст финала)" if key == "i"
                                       else f"новости {key}") + " одним сообщением. Удобно: скопируй текст выше, "
            "поправь и отправь — заменю как есть, без переписывания ИИ. Отмена — /cancel.")
        return
    draft = await wd_load()
    if not draft:
        await callback.answer("Черновик выпуска не найден — собери выпуск заново", show_alert=True)
        return
    bot = callback.message.bot
    if action == "go":
        await callback.answer("▶️ Продолжаю")
        wd_start_video(bot)
    elif action == "as":
        await callback.answer("🔗 Склеиваю")
        _run_job(lambda: wd_assemble_job(bot))
    elif action in ("vs", "vv", "vf"):
        await callback.answer("Принято в работу")
        _run_job(lambda: wd_block_job(bot, key, action, sure))
    elif action == "va":
        if key not in draft["blocks"]:
            await callback.answer("Этот кусок ещё монтируется")
            return
        draft = await wd_update(lambda d: d["video_ok"].update({key: True}))
        await callback.answer("✅ Принято")
        await _edit_quiet_markup(callback.message, _wd_caption(draft, key, draft["blocks"][key]), _wd_video_kb(key))
        if all(draft["video_ok"].get(k) for k in wd_keys(draft)) and draft.get("stage") != "assemble":
            await wd_update(lambda d: d.update(stage="assemble"))
            _run_job(lambda: wd_assemble_job(bot))
    else:
        await callback.answer()


async def send_episode(bot: Bot, chat_id: int, ep: Any, script: dict[str, Any], brief: str = "") -> None:
    """brief — выпуск после «🎙 Только звук»: без повтора обложки и SEO, с этим отчётом о правке."""
    seo = ep.seo
    mins = f"{int(ep.duration) // 60}:{int(ep.duration) % 60:02d}"
    if not brief:
        await bot.send_photo(chat_id, FSInputFile(ep.cover),
                             caption=f"🖼 Обложка для YouTube\n<b>{escape(seo['title'])}</b>")
    if ep.preview:
        msg = await bot.send_video(chat_id, FSInputFile(ep.preview, filename="kinomir360_novosti_preview.mp4"),
                                   caption=f"🎞 Выпуск «Новости кино» ({mins})" + (
                                       "." if ep.preview == ep.video else " — превью в низком качестве. На YouTube "
                                       "уйдёт полный файл 1080p."), width=854, height=480,
                                   supports_streaming=True, request_timeout=600)
    else:
        msg = await bot.send_message(chat_id, f"🎞 Выпуск «Новости кино» собран ({mins}). Превью для Telegram вышло "
                                              "больше 50 МБ — посмотри ролик после загрузки на YouTube.")
    keep_for_upload(chat_id, msg.message_id, SimpleNamespace(video=ep.video, seo=seo))
    try:
        shutil.copy(ep.cover, READY_DIR / f"{chat_id}_{msg.message_id}.jpg")
    except Exception:  # noqa: BLE001
        logging.exception("Weekly cover copy failed")
    try:  # звук по кускам — для «🎙 Только звук»
        await asyncio.to_thread(shorts_weekly.keep_project, ep.video.parent, proj_dir(chat_id, msg.message_id))
    except Exception:  # noqa: BLE001
        logging.exception("Weekly project keep failed")
    topics = "\n".join(f"{n}. {escape(s['headline'])}" for n, s in enumerate(script["stories"], 1))
    on = await shorts_setting("weekly_video") != "off"
    head = (brief + "\n\n" if brief else "") + f"👆 Выпуск готов. Новости в нём:\n{topics}\n\n"
    await bot.send_message(
        chat_id, head + "Посмотри превью и жми «▶️ Загрузить на YouTube» — загружу полное качество, название, "
                        "описание с таймкодами, теги и обложку.\n\n" + SAY_HINT,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_btn("▶️ Загрузить на YouTube", f"yt:{msg.message_id}")],
            [_btn("✂️ Нарезать на шортсы (по новости)", f"pt:wvcut:{msg.message_id}")],
            *_fix_rows(msg.message_id, weekly=True),
            [_btn("🔕 Не собирать по ср и вс" if on else "🔔 Собирать по ср и вс в 10:00",
                  "pt:wvoff" if on else "pt:wvon")]]))
    if brief:
        return
    for name, key in (("Название", "title"), ("Описание", "description"), ("Теги", "tags"),
                      ("Закреплённый комментарий", "pinned")):
        await bot.send_message(chat_id, f"<b>{name}:</b>\n<pre>{escape(seo[key])}</pre>")
    if seo.get("warning"):
        await bot.send_message(chat_id, escape(seo["warning"]))
    if seo.get("listen"):
        await bot.send_message(chat_id, "🎧 <b>Послушай эти места</b> — распознавание услышало не то, что в тексте "
                                        "(часто это искажённое имя или окончание):\n" + escape(seo["listen"][:3500])
                               + "\n\nЕсли слово и правда звучит неверно — <code>/say слово = как читать</code> и "
                                 "«🎙 Только звук»: переозвучатся только исправленные фразы, картинка та же.")
    if seo.get("cache"):
        await bot.send_message(chat_id, f"💾 Озвучка: {seo['cache']} фраз взято из уже оплаченных — заново "
                                        "озвучено только новое и исправленное.")
    if script.get("fixes"):
        rows, size = [], 0
        for fix in script["fixes"][:30]:
            row = f"• {escape(fix)}"
            if size + len(row) > 3700:
                break
            rows.append(row)
            size += len(row) + 1
        await bot.send_message(chat_id, "✍️ <b>Редактор исправил в тексте:</b>\n" + "\n".join(rows))
    if seo.get("stress"):
        await bot.send_message(
            chat_id, "🗣 <b>Ударения, которые поставил ИИ</b> (знак после ударной гласной):\n"
                     f"{escape(seo['stress'][:3500])}\n\nУслышал неверное ударение — исправь: ударную гласную пиши "
                     "заглавной, например <code>/say обитель = обИтель</code>. Потом жми «🎙 Только звук» — "
                     "переозвучатся только исправленные фразы, картинка та же.")


async def episode_cuts_job(bot: Bot, chat_id: int, first: int) -> None:
    """«✂️ Нарезать на шортсы»: каждая новость выпуска — вертикальный шортс 9:16 с заголовком (озвучка, графика и
    музыка уже есть — новых трат нет). Каждый шортс — с кнопками «Загрузить на YouTube» и «В канал»."""
    video = READY_DIR / f"{chat_id}_{first}.mp4"
    seo_file = video.with_suffix(".json")
    if not video.exists() or not seo_file.exists():
        await bot.send_message(chat_id, f"Файла выпуска уже нет на сервере (храню {READY_DAYS} дня).")
        return
    seo = json.loads(seo_file.read_text())
    cuts = json.loads(seo.get("cuts") or "[]")
    if not cuts:
        await bot.send_message(chat_id, "В этом выпуске нет разметки по новостям (собран до обновления) — "
                                        "нарезка будет у следующих выпусков.")
        return
    if shorts_lock.locked():
        await bot.send_message(chat_id, "⏳ Сейчас собирается другой ролик — нажми «✂️ Нарезать» через пару минут.")
        return
    status = await bot.send_message(chat_id, f"✂️ Режу выпуск на {len(cuts)} шортсов…")
    work = SHORTS_DIR / f"cuts_{time.time_ns()}"
    work.mkdir(parents=True, exist_ok=True)
    done = 0
    try:
        async with shorts_lock:
            for k, (start, end, headline) in enumerate(cuts, 1):
                try:
                    await status.edit_text(f"✂️ Шортс {k} из {len(cuts)}: {escape(headline[:60])}")
                except Exception:  # noqa: BLE001
                    pass
                try:
                    out = await shorts_weekly.vertical_cut(video, float(start), float(end), headline,
                                                           work / f"cut{k}.mp4")
                except Exception as exc:  # noqa: BLE001
                    logging.exception("Episode cut %s failed", k)
                    await bot.send_message(chat_id, f"❌ Шортс {k} не собрался: <code>{escape(_safe_err(exc)[:200])}</code>")
                    continue
                cut_seo = shorts_weekly.cut_seo(headline, seo)
                sent = await bot.send_video(chat_id, FSInputFile(out, filename=f"kinomir360_news_{k}.mp4"),
                                            caption=post_caption(f"🎬 {headline}"), width=1080, height=1920,
                                            supports_streaming=True, request_timeout=300)
                keep_for_upload(chat_id, sent.message_id, SimpleNamespace(video=out, seo=cut_seo))
                await offer_post(bot, chat_id, [sent.message_id], "caption", what=f"Шортс {k}: {cut_seo['title']}",
                                 extra={"yt": True})
                done += 1
    finally:
        shutil.rmtree(work, ignore_errors=True)
    await status.edit_text(f"✅ Готово: {done} шортсов из выпуска. Под каждым — «▶️ Загрузить на YouTube». "
                           "Совет: выкладывай по 1–2 в день, а не все сразу.")


def _tc(sec: float) -> str:
    return f"{int(sec) // 60}:{sec % 60:04.1f}"


def proj_dir(chat_id: int, message_id: int) -> Path:
    """Проект ролика (для «🎙 Только звук» и «🔁 Полностью») — рядом с роликом для YouTube, столько же дней."""
    return READY_DIR / f"{chat_id}_{message_id}.proj"


SAY_HINT = ("Неверно произнесено слово — исправь: <code>/say слово = как читать</code> (ударную гласную — "
            "заглавной: <code>/say среду = срЕду</code>), потом жми «🎙 Только звук».")


def _fix_rows(first: int, weekly: bool = False) -> list[list[InlineKeyboardButton]]:
    if weekly:
        return [[_btn("🎙 Только звук (исправить произношение)", f"pt:wvvoice:{first}")],
                [_btn("🔁 Полностью перемонтировать (тот же текст)", "pt:wvre")]]
    return [[_btn("🎙 Только звук (исправить произношение)", f"pt:rva:{first}")],
            [_btn("🔁 Полностью перемонтировать (тот же текст)", f"pt:rvf:{first}")]]


async def keep_project(chat_id: int, message_id: int, work: Path, send: dict[str, Any]) -> bool:
    """Сохранить проект ролика (папка сборки) под номером сообщения с роликом и показать кнопки правки."""
    dest = proj_dir(chat_id, message_id)
    try:
        if not await asyncio.to_thread(shorts_project.keep, work, dest):
            return False
        proj = shorts_project.load(dest) or {}
        proj["send"] = send
        (dest / shorts_project.FILE).write_text(json.dumps(proj, ensure_ascii=False, default=str), encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001 — без проекта просто не будет кнопок правки
        logging.exception("Keep project failed")
        shutil.rmtree(dest, ignore_errors=True)
        return False


async def show_scene_review(bot: Bot, chat_id: int, first: int) -> None:
    folder = proj_dir(chat_id, first)
    proj = shorts_project.load(folder) or {}
    plan_path = folder / "scene_plan.json"
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        await bot.send_message(chat_id, "План сцен не найден. Открой проверку ещё раз.")
        return
    scenes = plan.get("scenes") or []
    if not scenes:
        await bot.send_message(chat_id, "В плане пока нет сцен для проверки.")
        return
    await bot.send_message(chat_id, f"🖼 <b>Проверка визуального ряда</b> — {len(scenes)} сцен. "
                         "Посмотри изображения. Любой кадр можно заменить своим фото.")
    cards = (proj.get("rerun") or {}).get("cards") or []
    for index, scene in enumerate(scenes):
        asset = scene.get("asset") or {}
        card_path = str(cards[index].get("image") or "") if index < len(cards) else ""
        path = Path(card_path or str(asset.get("path") or ""))
        if not path.is_absolute():
            path = folder / path
        caption = (f"Сцена {index + 1} · {float(scene.get('start') or 0):.1f}–"
                   f"{float(scene.get('end') or 0):.1f} сек.\n{str(scene.get('text') or '')[:650]}\n"
                   f"Источник: {asset.get('source') or 'неизвестен'} · "
                   f"статус: {(scene.get('match') or {}).get('status') or 'не проверено'}")
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [_btn("🔄 Заменить изображение", f"pt:vr:{first}:{index}")]])
        if path.is_file():
            await bot.send_photo(chat_id, FSInputFile(path), caption=caption[:1000], reply_markup=markup)
        else:
            await bot.send_message(chat_id, caption[:1000] + "\n⚠️ Файл изображения не найден.", reply_markup=markup)
    await bot.send_message(chat_id, "Когда все сцены устраивают, подтверди визуальный ряд:",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                               [_btn("✅ Подтвердить все сцены", f"pt:va:{first}")]]))


async def approve_scene_review(bot: Bot, chat_id: int, first: int) -> None:
    folder = proj_dir(chat_id, first)
    plan_path = folder / "scene_plan.json"
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        await bot.send_message(chat_id, "План сцен не найден. Открой проверку ещё раз.")
        return
    if plan.get("review_mode") == "approved":
        await bot.send_message(chat_id, "Этот визуальный ряд уже подтверждён.")
        return
    for scene in plan.get("scenes") or []:
        scene.setdefault("review", {})["approved"] = True
        scene["review"]["approved_at"] = datetime.now(MSK).isoformat()
    plan["review_mode"] = "approved"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    await bot.send_message(chat_id, "✅ Визуальный ряд подтверждён. Теперь можно публиковать ролик.")
    await offer_post(bot, chat_id, [first], "caption", what="Проверенный шортс готов для Telegram-канала",
                     extra={"yt": True})


async def fix_buttons(bot: Bot, chat_id: int, first: int, weekly: bool = False, note: str = "") -> None:
    rows = _fix_rows(first, weekly)
    if (proj_dir(chat_id, first) / "scene_plan.json").exists():
        rows.insert(0, [_btn("🖼 Проверить сцены и заменить кадры", f"pt:vis:{first}")])
    await bot.send_message(
        chat_id, (note + "\n\n" if note else "") + "🛠 <b>Поправить ролик:</b>\n• 🎙 <b>Только звук</b> — переозвучатся "
        "только исправленные фразы и встанут на свои места, картинка та же. Платно только исправленное.\n"
        "• 🔁 <b>Полностью</b> — тот же текст, озвучка и монтаж заново (неизменные фразы — бесплатно из кэша).\n\n"
        + SAY_HINT, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def send_fixed(bot: Bot, chat_id: int, folder: Path, video: Path, note: str) -> None:
    """Исправленный шортс/обзор: тот же текст подписи и SEO, кнопки публикации и правки — как у первого."""
    send = (shorts_project.load(folder) or {}).get("send") or {}
    review = send.get("mode") == "review"
    msg = await bot.send_video(
        chat_id, FSInputFile(video, filename="kinomir360_review.mp4" if review else "kinomir360_short.mp4"),
        caption=send.get("caption") or None, width=1280 if review else 1080, height=720 if review else 1920,
        supports_streaming=True, request_timeout=600)
    if send.get("seo"):
        keep_for_upload(chat_id, msg.message_id, SimpleNamespace(video=video, seo=send["seo"]))
    await keep_project(chat_id, msg.message_id, folder, send)
    plan_path = folder / "scene_plan.json"
    review_pending = False
    if plan_path.exists():
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            review_pending = any(not (scene.get("review") or {}).get("approved")
                                 for scene in plan.get("scenes") or [])
        except (OSError, ValueError):
            review_pending = True
    if not review and not review_pending:
        await offer_post(bot, chat_id, [msg.message_id], "caption", what="Исправленный шортс", extra={"yt": True})
    elif review_pending:
        await bot.send_message(chat_id, "🖼 Перед публикацией проверь визуальный ряд кнопкой ниже.")
    await fix_buttons(bot, chat_id, msg.message_id, note=note)


def _fixed_note(fixed: list[tuple[float, str]], notes: list[str]) -> str:
    rows = [f"⏱ {_tc(t)} — «{escape(text[:90])}»" for t, text in fixed[:15]]
    return (f"✅ Переозвучено фраз: {len(fixed)}, картинка та же.\n" + "\n".join(rows)
            + ("\n\n⚠️ " + "\n⚠️ ".join(escape(n) for n in notes) if notes else ""))


async def _revoice_guard(bot: Bot, chat_id: int, status: Message, exc: Exception, retry: str) -> bool:
    """Ответ на «нечего переозвучивать» и «слишком много фраз»; True — обработано."""
    if isinstance(exc, shorts_project.NothingToFix):
        await status.edit_text("✅ Все фразы прозвучат так же, как в ролике, — переозвучивать нечего.\n" + SAY_HINT)
        return True
    if isinstance(exc, shorts_project.TooMany):
        await status.edit_text(
            f"⚠️ Заново озвучить придётся {exc.n} фраз из {exc.total} (≈{exc.chars} символов). Обычно так бывает, "
            "если ролик собран до обновления или сменились настройки голоса. Озвучить?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn(f"🎙 Да, озвучить {exc.n} фраз", retry)]]))
        return True
    return False


async def project_audio_job(bot: Bot, chat_id: int, first: int, sure: bool = False) -> None:
    """«🎙 Только звук» у шортса, лора, обзора: исправленные фразы — в ту же дорожку, картинка без пересчёта."""
    folder = proj_dir(chat_id, first)
    if not (folder / shorts_project.FILE).exists():
        await bot.send_message(chat_id, f"Проекта этого ролика нет: он собран до обновления или старше {READY_DAYS} "
                                        "дней. Новые ролики можно поправить по частям.")
        return
    if shorts_lock.locked():
        await bot.send_message(chat_id, "⏳ Сейчас собирается другой ролик — нажми «🎙 Только звук» через пару минут.")
        return
    status = await bot.send_message(chat_id, "🎙 Сверяю фразы ролика со словарём произношения…")
    try:
        async with shorts_lock:
            await load_pronunciations()
            await load_voice()
            await _edit_quiet(status, "🎙 Озвучиваю исправленные фразы и сшиваю звук…")
            video, fixed, notes = await shorts_project.revoice(folder, sure)
            await status.delete()
            await send_fixed(bot, chat_id, folder, video, _fixed_note([(t, x) for _, t, x in fixed], notes))
    except Exception as exc:  # noqa: BLE001
        if not await _revoice_guard(bot, chat_id, status, exc, f"pt:rva:{first}:go"):
            logging.exception("Project revoice failed")
            await _edit_quiet(status, f"❌ Переозвучка не получилась: <code>{escape(_safe_err(exc)[:300])}</code>")


async def project_remake_job(bot: Bot, chat_id: int, first: int) -> None:
    """«🔁 Полностью (тот же текст)»: озвучка с исправлениями (/say) и монтаж заново по сохранённому проекту."""
    folder = proj_dir(chat_id, first)
    proj = shorts_project.load(folder)
    if not proj or not proj.get("rerun"):
        await bot.send_message(chat_id, f"Проекта этого ролика нет: он собран до обновления или старше {READY_DAYS} "
                                        "дней.")
        return
    if shorts_lock.locked():
        await bot.send_message(chat_id, "⏳ Сейчас собирается другой ролик — нажми «🔁 Полностью» через пару минут.")
        return
    status = await bot.send_message(chat_id, "🔁 Собираю ролик заново: тот же текст, озвучка с исправлениями…")
    work = SHORTS_DIR / f"remake_{time.time_ns()}"
    rr = proj["rerun"]

    def local(x: str | None) -> Path | None:
        if not x:
            return None
        path = Path(x)
        return path if path.is_absolute() else work / path

    try:
        async with shorts_lock:
            await asyncio.to_thread(shutil.copytree, folder, work)
            await load_pronunciations()
            await load_voice()
            await load_fact_channels()
            await set_shorts_setting("running", json.dumps({"chats": [chat_id], "theme": "перемонтаж ролика"}))
            shorts_daily.on_progress, shorts_daily.last_stage = progress_updater(status), ""
            if rr["fn"] == "render":
                job = shorts_daily._render(work, work / rr["cover"], rr["cards"], rr["lines"], rr["tag"], rr["header"],
                                           rr["question"], local(rr.get("music")), rr.get("boom_slide"),
                                           scene_plan=bool(rr.get("scene_plan_enabled")))
            elif rr["fn"] == "lore":
                job = shorts_lore.make_lore(work, rr["script"], local(rr.get("music")),
                                            [work / m for m in rr.get("media") or []], GEMINI_API_KEY,
                                            {int(k): v for k, v in (rr.get("pins") or {}).items()})
            else:
                job = shorts_review.make_review(kp._get, work, rr["script"], local(rr.get("music")), TMDB_API_KEY,
                                                trailer_clips=await shorts_setting("clips") != "off",
                                                user_media=[work / m for m in rr.get("media") or []],
                                                gemini_key=GEMINI_API_KEY)
            await asyncio.wait_for(job, timeout=REVIEW_TIMEOUT)  # самая долгая из сборок (обзор, своя подборка)
            new = shorts_project.load(work) or {}
            new["send"] = proj.get("send") or {}
            (work / shorts_project.FILE).write_text(json.dumps(new, ensure_ascii=False, default=str), encoding="utf-8")
            await status.delete()
            await send_fixed(bot, chat_id, work, work / new.get("video", "short.mp4"),
                             f"✅ Собрано заново: тот же текст, озвучено фраз заново — {len(shorts_daily.voice_fresh)}.")
    except asyncio.TimeoutError:
        await _edit_quiet(status, f"❌ Сборка зависла на этапе: {escape(shorts_daily.last_stage or 'начало')}")
    except Exception as exc:  # noqa: BLE001
        logging.exception("Project remake failed")
        await _edit_quiet(status, f"❌ Не собралось: <code>{escape(_safe_err(exc)[:300])}</code>")
    finally:
        shorts_daily.on_progress = None
        await clear_running()
        shutil.rmtree(work, ignore_errors=True)


async def episode_revoice_job(bot: Bot, chat_id: int, first: int, sure: bool = False) -> None:
    """«🎙 Только звук» у выпуска: исправленные фразы встают в свои куски, звук склеивается как раньше и ставится на
    ту же картинку — видео не пересобирается. Платно только исправленное."""
    video = READY_DIR / f"{chat_id}_{first}.mp4"
    seo_file = video.with_suffix(".json")
    folder = proj_dir(chat_id, first)
    if not video.exists() or not seo_file.exists():
        await bot.send_message(chat_id, f"Файла выпуска уже нет на сервере (храню {READY_DAYS} дня).")
        return
    if not (folder / "project.json").exists():
        await bot.send_message(chat_id, "Этот выпуск собран до обновления — у него нет звука по кускам. Для него — "
                                        "«🔁 Полностью перемонтировать»; новые выпуски можно править только по звуку.")
        return
    if shorts_lock.locked():
        await bot.send_message(chat_id, "⏳ Сейчас собирается другой ролик — нажми «🎙 Только звук» через пару минут.")
        return
    status = await bot.send_message(chat_id, "🎙 Сверяю фразы выпуска со словарём произношения…")
    try:
        async with shorts_lock:
            await load_pronunciations()
            await load_voice()
            await _edit_quiet(status, "🎙 Озвучиваю исправленные фразы и сшиваю звук выпуска…")
            out, fixed, notes = await shorts_weekly.revoice(folder, video, sure)
            length = await shorts_weekly._duration(out)
            preview = await shorts_weekly.make_preview(out, folder / "preview.mp4", length)
            script = json.loads((folder / "project.json").read_text(encoding="utf-8"))["script"]
            ep = SimpleNamespace(video=out, preview=preview, cover=folder / "cover.jpg",
                                 seo=json.loads(seo_file.read_text()), duration=length)
            await status.delete()
            await send_episode(bot, chat_id, ep, script, brief=_fixed_note(fixed, notes))
    except Exception as exc:  # noqa: BLE001
        if not await _revoice_guard(bot, chat_id, status, exc, f"pt:wvvoice:{first}:go"):
            logging.exception("Episode revoice failed")
            await _edit_quiet(status, f"❌ Переозвучка не получилась: <code>{escape(_safe_err(exc)[:300])}</code>")
    finally:
        for name in ("preview.mp4", "episode.mp4"):
            (folder / name).unlink(missing_ok=True)


rt_watch_busy = asyncio.Lock()


async def rt_watch_job(bot: Bot) -> None:
    async with rt_watch_busy:
        try:
            await asyncio.wait_for(rt_watch(bot), 20 * 60)
        except Exception:  # noqa: BLE001
            logging.exception("RT watch failed")


async def weekly_video_job(bot: Bot) -> None:
    """По расписанию: полоска сборки — первому админу, готовый выпуск — всем админам."""
    admins = sorted(ADMIN_IDS)
    status = None
    try:
        status = await bot.send_message(admins[0], "🎞 Собираю выпуск «Новости кино» для YouTube по расписанию "
                                                   "(15–40 минут)…")
        await make_weekly_video(bot, admins, status)
        await status.delete()
    except Exception as exc:  # noqa: BLE001
        logging.exception("Weekly video failed")
        # упал (сбой ИИ, сети, перезапуск) — ещё одна попытка через полчаса, но не больше двух за воскресенье
        now = datetime.now(POST_TZ)
        week = now.date().isoformat()  # попытки — на этот день выпуска
        tries = await shorts_setting("weekly_video_tries") or ""
        n = int(tries.split(":")[1]) if tries.startswith(week + ":") else 0
        # все ИИ на лимитах — повтор, когда освободится ближайший (а не через 30 минут в тот же отказ)
        wait = getattr(exc, "retry_in", None)
        retry = (n < 1 or wait is not None and n < 3) and "уже собирается" not in str(exc)
        delay = min(max((wait or 0) + 120, 1800), 6 * 3600)
        await set_shorts_setting("weekly_video_tries", f"{week}:{n + 1}")
        if retry:
            await set_shorts_setting("weekly_video_retry_at", str(time.time() + delay))
            await set_shorts_setting("weekly_video_done", None)
        when = (datetime.now(POST_TZ) + timedelta(seconds=delay)).strftime("%H:%M")
        for admin_id in admins:
            try:
                await bot.send_message(admin_id, f"❌ Выпуск «Новости кино» не собрался: <code>{escape(_safe_err(exc)[:300])}"
                                                 "</code>\n" + (f"Все ИИ сейчас на лимитах — сам повторю в {when} "
                                                                 f"({POST_TZ_NAME}). " if retry and wait else
                                                                 "Попробую ещё раз через 30 минут. " if retry else "")
                                       + "Собрать вручную — ▶️ YouTube и видео → «🎞 Выпуск «Новости кино»».")
            except Exception:  # noqa: BLE001
                pass


async def weekly_admin_report() -> str:
    """Отчёт админу по понедельникам: подписчики бота, что делали, что вышло, сколько потрачено, план недели."""
    async def q(sql: str) -> int:
        try:
            return int(await db().fetchval(sql) or 0)
        except Exception:  # noqa: BLE001 — нет таблицы и т. п.
            return 0

    new_users = await q("SELECT COUNT(*) FROM users WHERE created_at > NOW() - INTERVAL '7 days'")
    active = await q("SELECT COUNT(*) FROM users WHERE last_seen_at > NOW() - INTERVAL '7 days'")
    total = await q("SELECT COUNT(*) FROM users")
    searches = await q("SELECT COUNT(*) FROM search_history WHERE searched_at > NOW() - INTERVAL '7 days'")
    diary_n = await q("SELECT COUNT(*) FROM diary WHERE watched_at > NOW() - INTERVAL '7 days'")
    quiz_n = await q("SELECT COUNT(*) FROM quiz_answers WHERE created_at > NOW() - INTERVAL '7 days'")
    favs = await q("SELECT COUNT(*) FROM favorites WHERE created_at > NOW() - INTERVAL '7 days'")
    try:
        top = await db().fetch("""SELECT lower(query) q, COUNT(*) n FROM search_history
            WHERE searched_at > NOW() - INTERVAL '7 days' GROUP BY 1 ORDER BY 2 DESC LIMIT 5""")
    except Exception:  # noqa: BLE001
        top = []
    u = shorts_usage.summary(7)
    lines = [
        "📊 <b>Неделя KinoMir360</b>", "",
        f"👥 Бот: новых <b>{new_users}</b>, заходили <b>{active}</b>, всего <b>{total}</b>",
        f"🔎 Поисков {searches} · ❤️ в избранное {favs} · 📔 в кинодневник {diary_n} · 🧩 ответов в викторине {quiz_n}",
        f"📢 Постов в канал: <b>{int(u.get('tg_posts', 0))}</b> · ▶️ на YouTube: шортсов "
        f"<b>{int(u.get('yt_shorts', 0))}</b>, выпусков <b>{int(u.get('yt_long', 0))}</b>",
        f"💳 Расходы: ИИ Lumean {int(u.get('llm_calls', 0))} запр., озвучка {int(u.get('tts_chars', 0)):,} симв., "
        f"музыка {int(u.get('music_tracks', 0))} тр.".replace(",", " "),
    ]
    if top:
        lines.append("🔥 Чаще искали: " + ", ".join(f"{escape(r['q'][:30])} ({r['n']})" for r in top))
    try:
        soon = (await kp.upcoming_releases(False, limit=20, days_ahead=8))[:6]
    except Exception:  # noqa: BLE001
        soon = []
    lines += ["", "🗓 <b>План недели</b>",
              "• среда и воскресенье — выпуск «Новости кино» (соберётся сам)",
              "• пятница 18:00 — подборка недели подписчикам бота"]
    if soon:
        lines.append("• премьеры недели — повод для постов и шортсов: "
                     + ", ".join(f"«{escape(kp.title(f))}»" for f in soon))
    if top:
        lines.append(f"• подписчики ищут «{escape(top[0]['q'][:40])}» — можно сделать пост или шортс")
    return "\n".join(lines)


@router.message(Command("report"))
async def report_command(message: Message) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    await message.answer(await weekly_admin_report(), disable_web_page_preview=True)


ALIVE_FILE = Path("/tmp/vkino_alive")


async def heartbeat() -> None:
    """Раз в 30 с отмечает, что цикл бота работает (healthcheck Docker; не обновляется — бот завис или упал);
    раз в сутки удаляет брошенные диалоги (fsm_states старше недели)."""
    last_clean = 0.0
    while True:
        try:
            ALIVE_FILE.touch()
        except OSError:
            pass
        if time.monotonic() - last_clean > 86400:
            last_clean = time.monotonic()
            try:
                await shorts_fsm.PgStorage(db).cleanup()
            except Exception:  # noqa: BLE001
                logging.exception("FSM cleanup failed")
        await asyncio.sleep(30)


async def growth_loop(bot: Bot) -> None:
    """Раз в 10 минут: уведомления из избранного (раз в 3 часа), подборка недели (пятница, 18:00 Екб)."""
    last_alerts = last_rt = last_clean = last_yt = 0.0
    while True:
        await asyncio.sleep(600)
        try:
            if time.time() - last_clean > 86400:  # раз в сутки: старые события статистики не копим вечно
                last_clean = time.time()
                try:
                    await db().execute("DELETE FROM bot_events WHERE created_at < NOW() - INTERVAL '400 days'")
                except Exception:  # noqa: BLE001
                    logging.exception("bot_events cleanup failed")
            if (time.time() - last_rt > RT_EVERY and 8 <= datetime.now(POST_TZ).hour < 23
                    and not rt_watch_busy.locked()):
                last_rt = time.time()
                _run_job(lambda: rt_watch_job(bot))  # отдельно: медленный RT не задерживает остальное
            if time.time() - last_yt > YT_EVERY and 8 <= datetime.now(POST_TZ).hour < 23:
                last_yt = time.time()
                try:
                    await asyncio.wait_for(yt_trailers_watch(bot), 120)
                except Exception:  # noqa: BLE001
                    logging.exception("YouTube trailers watch failed")
            if time.time() - last_alerts > 3 * 3600:
                last_alerts = time.time()
                await favorites_alerts(bot)
            now = datetime.now(POST_TZ)
            week = f"{now.isocalendar()[0]}-{now.isocalendar()[1]}"
            today_key = now.date().isoformat()  # выпуск в каждый из дней (среда, воскресенье) — по одному
            if (now.weekday() in WEEKLY_DAYS and now.hour >= WEEKLY_HOUR and ADMIN_IDS
                    and await shorts_setting("weekly_video") != "off"
                    and await shorts_setting("weekly_video_done") != today_key
                    and time.time() - float(await shorts_setting("weekly_video_at") or 0) > 20 * 3600
                    and time.time() > float(await shorts_setting("weekly_video_retry_at") or 0)
                    and not weekly_busy):
                await set_shorts_setting("weekly_video_done", today_key)
                _run_job(lambda: weekly_video_job(bot))
            # прогноз уикенда: пятница–суббота — открыть (ИИ ищет кандидатов), понедельник после 12:00 — итоги
            if (now.weekday() in (4, 5) and now.hour >= 11 and await shorts_setting("predict_open_try") != week
                    and not await db().fetchval("SELECT 1 FROM predict_round WHERE week=$1", week)):
                if time.time() > float(await shorts_setting("predict_open_retry") or 0):
                    # ИИ не нашёл кандидатов — следующая попытка через 2 часа; в субботу вечером — сдаёмся
                    await set_shorts_setting("predict_open_retry", str(time.time() + 2 * 3600))
                    if now.weekday() == 5 and now.hour >= 18:
                        await set_shorts_setting("predict_open_try", week)
                    await predict_open(week)
            if now.weekday() == 0 and now.hour >= 12:
                prev = (now - timedelta(days=7)).isocalendar()
                await predict_settle(bot, f"{prev[0]}-{prev[1]}")
            if (now.weekday() == 0 and now.hour >= 10 and ADMIN_IDS
                    and await shorts_setting("admin_report_done") != week):
                await set_shorts_setting("admin_report_done", week)
                try:
                    report = await weekly_admin_report()
                    for admin_id in ADMIN_IDS:
                        await bot.send_message(admin_id, report, disable_web_page_preview=True)
                except Exception:  # noqa: BLE001
                    logging.exception("Weekly admin report failed")
            if (now.weekday() == 4 and now.hour >= 18 and await shorts_setting("digest") != "off"
                    and await shorts_setting("digest_done") != week):
                await set_shorts_setting("digest_done", week)
                sent = await send_weekly_digest(bot)
                for admin_id in ADMIN_IDS:
                    try:
                        await bot.send_message(admin_id, f"📬 Подборка недели разослана: {sent} пользователям бота.")
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            logging.exception("Growth loop failed")


# --- викторина дня с таблицей лидеров

quiz_lock = asyncio.Lock()


async def quiz_today(bot: Bot, chat_id: int) -> dict[str, Any] | None:
    """Вопрос дня: создаём при первом запросе за день (кадр + 4 варианта), картинку храним как file_id."""
    today = datetime.now(POST_TZ).date()
    async with quiz_lock:
        row = await db().fetchrow("SELECT * FROM quiz_daily WHERE day=$1", today)
        if row and row["image_file"]:
            return dict(row)
        used = {int(r["film_id"]) for r in await db().fetch(
            "SELECT film_id FROM quiz_daily WHERE day > CURRENT_DATE - 120")}
        films = await _popular_films()
        work = SHORTS_DIR / f"dquiz_{time.time_ns()}"
        work.mkdir(parents=True, exist_ok=True)
        try:
            film, frames = None, []
            for pick in [f for f in films if f["id"] not in used][:6]:
                film = await kp._get(f"/movie/{pick['id']}")
                frames = await _frames(film, work, 1, "q")
                if frames:
                    break
            if not film or not frames:
                return None
            genres = set(shorts_daily._genres(film))
            others = sorted((f for f in films if f["id"] != film["id"]),
                            key=lambda f: -len(genres & set(shorts_daily._genres(f))))[:3]
            options = [film, *others]
            random.shuffle(options)
            names = [shorts_daily._title(f)[:60] for f in options]
            card = work / "quiz.jpg"
            (await asyncio.to_thread(shorts_postcard.make_quiz_card, frames[0])).save(card, quality=90)
            sent = await bot.send_photo(chat_id, FSInputFile(card), caption=QUIZ_CAPTION,
                                        reply_markup=quiz_markup(today, names))
        finally:
            shutil.rmtree(work, ignore_errors=True)
        await db().execute(
            """INSERT INTO quiz_daily(day, film_id, title, image_file, options, answer) VALUES($1,$2,$3,$4,$5,$6)
               ON CONFLICT(day) DO UPDATE SET film_id=EXCLUDED.film_id, title=EXCLUDED.title,
               image_file=EXCLUDED.image_file, options=EXCLUDED.options, answer=EXCLUDED.answer""",
            today, int(film["id"]), _label(film), sent.photo[-1].file_id, json.dumps(names, ensure_ascii=False),
            options.index(film))
        return {"day": today, "sent": True}


QUIZ_CAPTION = "🧩 <b>Викторина дня:</b> из какого фильма этот кадр?\nЗа верный ответ — очко в таблицу лидеров месяца 🏆"


def quiz_markup(day: date, names: list[str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=n, callback_data=f"qd:{day:%Y%m%d}:{i}")] for i, n in enumerate(names)])


async def quiz_leaders(limit: int = 10) -> list[Any]:
    return await db().fetch(
        """SELECT a.user_id, u.username, u.first_name, COUNT(*) FILTER (WHERE a.correct) pts, COUNT(*) games
           FROM quiz_answers a LEFT JOIN users u USING(user_id)
           WHERE a.day >= date_trunc('month', CURRENT_DATE) GROUP BY 1, 2, 3
           ORDER BY pts DESC, games DESC LIMIT $1""", limit)


def _who(r: Any) -> str:
    return escape("@" + r["username"] if r["username"] else (r["first_name"] or "зритель"))


# --- «🔮 Прогноз уикенда»: кто возглавит прокат; итоги в понедельник по реальным сборам

def _week_key(when: datetime | None = None) -> str:
    iso = (when or datetime.now(POST_TZ)).isocalendar()
    return f"{iso[0]}-{iso[1]}"


async def predict_open(week: str) -> bool:
    today = datetime.now(MSK).date()
    try:
        films = await shorts_ai.weekend_candidates(
            GEMINI_API_KEY, f"{today.day} {shorts_daily.MONTHS[today.month - 1]} {today.year}")
    except Exception:  # noqa: BLE001
        logging.exception("Predict: candidates failed")
        films = []
    if len(films) < 3:
        return False
    await db().execute("INSERT INTO predict_round(week, options) VALUES($1, $2) ON CONFLICT DO NOTHING",
                       week, json.dumps(films, ensure_ascii=False))
    return True


def _pname(f: dict[str, Any]) -> str:
    return f.get("ru") or f.get("en") or "?"


async def predict_settle(bot: Bot, week: str) -> None:
    row = await db().fetchrow("SELECT * FROM predict_round WHERE week=$1 AND NOT settled", week)
    if not row:
        return
    if time.time() < float(await shorts_setting("predict_settle_retry") or 0):
        return
    await set_shorts_setting("predict_settle_retry", str(time.time() + 3 * 3600))  # ИИ не ответил — через 3 ч
    options = row["options"] if isinstance(row["options"], list) else json.loads(row["options"])
    today = datetime.now(MSK).date()
    res = await shorts_ai.box_office_post(GEMINI_API_KEY,
                                          f"{today.day} {shorts_daily.MONTHS[today.month - 1]} {today.year}")
    if not res or not res["films"]:
        return
    top = res["films"][0]
    winner = next((k for k, f in enumerate(options) if any(
        shorts_daily.tmdb.similar(a, b) >= 0.7 for a in (f.get("ru"), f.get("en")) if a
        for b in (top.get("ru"), top.get("en")) if b)), -1)
    await db().execute("UPDATE predict_round SET settled=TRUE, winner=$2, winner_title=$3 WHERE week=$1",
                       week, winner, _pname(top))
    votes = await db().fetch("SELECT user_id, choice FROM predict_votes WHERE week=$1", week)
    for v in votes:
        right = int(v["choice"]) == winner
        text = (f"🔮 Итоги прогноза уикенда: первое место — «{escape(_pname(top))}»"
                + (f" ({escape(top['gross'])})" if top.get("gross") else "") + ".\n"
                + ("🎯 Ты угадал! +1 очко в таблице прогнозистов." if right else "Не угадал — в эту пятницу новый шанс!"))
        try:
            await bot.send_message(int(v["user_id"]), text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏆 Таблица прогнозистов", callback_data="pv:top")]]))
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.05)


@router.message(F.text == "🔮 Прогноз уикенда")
async def predict_menu(message: Message) -> None:
    await remember(message)
    week = _week_key()
    row = await db().fetchrow("SELECT * FROM predict_round WHERE week=$1", week)
    top_btn = [InlineKeyboardButton(text="🏆 Таблица прогнозистов", callback_data="pv:top")]
    if not row:
        await message.answer("🔮 <b>Прогноз уикенда</b>\n\nКаждую пятницу угадываем, какой фильм заработает "
                             "больше всех в мировом прокате за выходные. Итоги — в понедельник, за верный прогноз "
                             "очко в таблице. Новый прогноз откроется в пятницу! 🍿",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[top_btn]))
        return
    options = row["options"] if isinstance(row["options"], list) else json.loads(row["options"])
    mine = await db().fetchval("SELECT choice FROM predict_votes WHERE week=$1 AND user_id=$2", week,
                               message.from_user.id)
    if mine is not None or row["settled"] or datetime.now(POST_TZ).weekday() not in (4, 5, 6):
        text = "🔮 <b>Прогноз уикенда</b>\n\n"
        if mine is not None:
            text += f"Твой выбор: «{escape(_pname(options[int(mine)]))}». "
        text += (f"Победил «{escape(row['winner_title'] or '')}»." if row["settled"]
                 else "Итоги — в понедельник днём, пришлю сюда.")
        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[top_btn]))
        return
    await message.answer(
        "🔮 <b>Прогноз уикенда</b>\n\nКакой фильм заработает больше всех в мировом прокате за эти выходные? "
        "Итоги в понедельник — за верный ответ очко в таблице 🏆",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🎬 {_pname(f)}"[:64], callback_data=f"pv:v:{k}")]
            for k, f in enumerate(options)] + [top_btn]))


@router.callback_query(F.data.startswith("pv:"))
async def predict_buttons(callback: CallbackQuery) -> None:
    action = (callback.data or "")[3:]
    if action == "top":
        await callback.answer()
        rows = await db().fetch("""SELECT v.user_id, COUNT(*) pts, MAX(u.username) username, MAX(u.first_name) first_name
            FROM predict_votes v JOIN predict_round r ON r.week = v.week AND r.settled AND r.winner = v.choice
            LEFT JOIN users u ON u.user_id = v.user_id GROUP BY v.user_id ORDER BY pts DESC LIMIT 10""")
        medals = ["🥇", "🥈", "🥉"] + [f"{k}." for k in range(4, 11)]
        text = ("🏆 <b>Лучшие прогнозисты</b>\n\n" + "\n".join(f"{medals[k]} {_who(r)} — {r['pts']}"
                                                              for k, r in enumerate(rows))) if rows else \
            "🏆 Таблица пока пустая — первые итоги в понедельник."
        if callback.message:
            await callback.message.answer(text)
        return
    if action.startswith("v:") and action[2:].isdigit():
        week = _week_key()
        if datetime.now(POST_TZ).weekday() not in (4, 5, 6):
            await callback.answer("Прогноз уже закрыт — итоги в понедельник", show_alert=True)
            return
        await db().execute("""INSERT INTO predict_votes(week, user_id, choice) VALUES($1, $2, $3)
                              ON CONFLICT(week, user_id) DO UPDATE SET choice=EXCLUDED.choice""",
                           week, callback.from_user.id, int(action[2:]))
        await callback.answer("Принято! Итоги в понедельник 🔮")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer("✅ Прогноз принят. В понедельник пришлю, кто победил в прокате.")
        return
    await callback.answer()


# --- «Кинодл»: фильм дня за 6 подсказок, серия побед

kinodl_lock = asyncio.Lock()


async def kinodl_today() -> dict[str, Any] | None:
    today = datetime.now(POST_TZ).date()
    async with kinodl_lock:
        row = await db().fetchrow("SELECT * FROM kinodl_day WHERE day=$1", today)
        if row:
            film = row["film"] if isinstance(row["film"], dict) else json.loads(row["film"])
            return {"day": today, "film": film, "frame": row["frame"]}
        used = {int(r["film_id"]) for r in await db().fetch(
            "SELECT film_id FROM kinodl_day WHERE day > CURRENT_DATE - 365")}
        for pick in [f for f in await _popular_films(pages=20) if int(f["id"]) not in used][:5]:
            try:
                film = await kp._get(f"/movie/{pick['id']}")
            except Exception:  # noqa: BLE001
                continue
            if not film.get("persons") or not shorts_kinodl.titles(film):
                continue
            keep = {k: film.get(k) for k in ("id", "name", "alternativeName", "enName", "names", "year", "genres",
                                             "countries", "persons", "rating", "movieLength", "seriesLength",
                                             "shortDescription", "description", "slogan", "isSeries", "poster")}
            keep["persons"] = [p for p in film.get("persons") or [] if p.get("enProfession") in ("director", "actor")][:8]
            frame = None
            try:
                backdrops, _ = await shorts_daily.tmdb.images(TMDB_API_KEY, film, limit=3)
                frame = backdrops[0] if backdrops else None
            except Exception:  # noqa: BLE001
                pass
            await db().execute("INSERT INTO kinodl_day(day, film_id, film, frame) VALUES($1, $2, $3, $4)",
                               today, int(film["id"]), json.dumps(keep, ensure_ascii=False), frame)
            return {"day": today, "film": keep, "frame": frame}
    return None


async def _kinodl_streak(user_id: int) -> int:
    days = [r["day"] for r in await db().fetch(
        "SELECT day FROM kinodl_plays WHERE user_id=$1 AND solved ORDER BY day DESC LIMIT 400", user_id)]
    streak, expect = 0, datetime.now(POST_TZ).date()
    for d in days:
        if d == expect or (streak == 0 and d == expect - timedelta(days=1)):
            streak += 1
            expect = d - timedelta(days=1)
        else:
            break
    return streak


@router.message(Command("kinodl"))
@router.message(F.text == "🎬 Кинодл — угадай фильм")
async def kinodl_menu(message: Message, state: FSMContext | None = None) -> None:
    await remember(message)
    game = await kinodl_today()
    if not game:
        await message.answer("Фильм дня ещё не готов — загляни через минуту 🙂")
        return
    uid = message.from_user.id
    play = await db().fetchrow("SELECT * FROM kinodl_plays WHERE day=$1 AND user_id=$2", game["day"], uid)
    if play and play["done"]:
        await _kinodl_finish(message, game, play["tries"], play["solved"], again=True)
        return
    tries = play["tries"] if play else 0
    if state is not None:
        await state.set_state(SearchState.kinodl)
    await _kinodl_clues(message, game, tries, intro=not play)


async def _kinodl_clues(message: Message, game: dict[str, Any], tries: int, intro: bool = False) -> None:
    clue_list = shorts_kinodl.clues(game["film"])
    shown = clue_list[:min(tries + 1, len(clue_list))]
    head = ("🎬 <b>Кинодл</b> — угадай фильм дня за 6 подсказок. Пиши название сообщением; каждая ошибка "
            "открывает новую подсказку.\n\n" if intro else "")
    text = head + "\n".join(escape(c) for c in shown) + f"\n\nПопытка {tries + 1} из {shorts_kinodl.MAX_TRIES} ✍️"
    if tries + 1 >= shorts_kinodl.MAX_TRIES and game.get("frame"):
        try:
            await message.answer_photo(game["frame"], caption=text[:1024])
            return
        except Exception:  # noqa: BLE001
            pass
    await message.answer(text)


async def _kinodl_finish(message: Message, game: dict[str, Any], tries: int, solved: bool,
                         again: bool = False) -> None:
    film = game["film"]
    title = shorts_kinodl.titles(film)[0]
    streak = await _kinodl_streak(message.from_user.id if message.from_user else 0)
    day = game["day"].strftime("%d.%m")
    share = shorts_kinodl.share_line(day, tries, solved)
    link = f"{await _bot_link(message.bot)}?start=kinodl"
    text = ((f"🎉 Угадал с {tries}-й попытки! Это «{escape(title)}» ({film.get('year') or ''})."
             if solved else f"😔 Это был «{escape(title)}» ({film.get('year') or ''})."))
    if again:
        text = "✅ Сегодня ты уже сыграл. " + text
    text += (f"\n🔥 Серия побед: <b>{streak}</b>" if streak else "") + "\nНовый фильм — завтра!\n\n" \
        f"Поделись без спойлера:\n<code>{escape(share)}</code>\n{link}"
    await message.answer(text, disable_web_page_preview=True, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Открыть карточку фильма", callback_data=f"open:{film['id']}")],
        [InlineKeyboardButton(text="📤 Позвать друга играть", url="https://t.me/share/url?url=" + quote(link)
                              + "&text=" + quote(share))]]))


@router.message(SearchState.kinodl, F.text, ~F.text.startswith("/"))
async def kinodl_guess(message: Message, state: FSMContext) -> None:
    guess = (message.text or "").strip()
    if guess in MAIN_MENU_LABELS:
        await state.clear()
        await do_search(message, state)  # переход в другой раздел меню
        return
    game = await kinodl_today()
    if not game:
        await state.clear()
        return
    uid = message.from_user.id
    play = await db().fetchrow("SELECT * FROM kinodl_plays WHERE day=$1 AND user_id=$2", game["day"], uid)
    if play and play["done"]:
        await state.clear()
        await _kinodl_finish(message, game, play["tries"], play["solved"], again=True)
        return
    tries = (play["tries"] if play else 0) + 1
    solved = shorts_kinodl.is_correct(guess, game["film"])
    done = solved or tries >= shorts_kinodl.MAX_TRIES
    await db().execute("""INSERT INTO kinodl_plays(day, user_id, tries, solved, done) VALUES($1, $2, $3, $4, $5)
                          ON CONFLICT(day, user_id) DO UPDATE SET tries=$3, solved=$4, done=$5""",
                       game["day"], uid, tries, solved, done)
    if done:
        await state.clear()
        await _kinodl_finish(message, game, tries, solved)
        return
    await message.answer("❌ Не то. Новая подсказка 👇")
    await _kinodl_clues(message, game, tries)


# --- «Выбрать вдвоём»: двое отмечают одни и те же фильмы, бот показывает совпадения

DUO_FILMS = 12


@router.message(F.text == "👥 Выбрать вдвоём")
async def duo_new(message: Message) -> None:
    await remember(message)
    wait = await message.answer("🎬 Собираю 12 хороших фильмов…")
    films: list[dict[str, Any]] = []
    try:
        for _ in range(2):
            for f in await _popular_films(pages=15):
                if len(films) < DUO_FILMS and all(int(f["id"]) != x["id"] for x in films):
                    films.append({"id": int(f["id"]), "title": shorts_daily._title(f), "year": f.get("year"),
                                  "poster": shorts_daily._poster_url(f), "rating": shorts_daily._best_rating(f)[0],
                                  "genres": ", ".join(str(g.get("name")) for g in (f.get("genres") or [])[:2]
                                                      if isinstance(g, dict))})
    except Exception:  # noqa: BLE001
        logging.exception("Duo: films failed")
    if len(films) < 6:
        await wait.edit_text("Не получилось собрать фильмы — попробуй через минуту.")
        return
    duo_id = secrets.token_urlsafe(6).replace("-", "x").replace("_", "y")
    await db().execute("INSERT INTO duo(id, creator, films) VALUES($1, $2, $3)", duo_id, message.from_user.id,
                       json.dumps(films, ensure_ascii=False))
    link = f"{await _bot_link(message.bot)}?start=duo_{duo_id}"
    await wait.edit_text(
        f"👥 <b>Выбираем вдвоём</b>\n\n1. Отправь другу ссылку: {link}\n2. Каждый отмечает 👍 или 👎 одни и те "
        f"же {len(films)} фильмов.\n3. Бот покажет фильмы, которые понравились обоим. 🍿\n\nНачинай 👇",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="📤 Отправить другу", url=f"https://t.me/share/url?url={quote(link)}&text="
            + quote("Давай выберем фильм на вечер вместе 🍿"))]]))
    await duo_show(message.bot, message.chat.id, message.from_user.id, duo_id, None)


async def duo_show(bot: Bot, chat_id: int, user_id: int, duo_id: str, msg: Message | None) -> None:
    """Следующий фильм, который этот участник ещё не отметил; все отмечены — итог."""
    row = await db().fetchrow("SELECT films FROM duo WHERE id=$1", duo_id)
    if not row:
        await bot.send_message(chat_id, "Эта ссылка устарела — начни заново: «👥 Выбрать вдвоём».")
        return
    films = row["films"] if isinstance(row["films"], list) else json.loads(row["films"])
    done = {int(r["idx"]) for r in await db().fetch("SELECT idx FROM duo_votes WHERE id=$1 AND user_id=$2",
                                                    duo_id, user_id)}
    left = [k for k in range(len(films)) if k not in done]
    if not left:
        if msg:
            try:
                await msg.delete()
            except Exception:  # noqa: BLE001
                pass
        await duo_result(bot, duo_id, user_id)
        return
    k = left[0]
    f = films[k]
    caption = (f"<b>{escape(f['title'])}</b>" + (f" ({f['year']})" if f.get("year") else "")
               + (f"\n⭐ {f['rating']:.1f}" if f.get("rating") else "") + (f" · {escape(f['genres'])}" if f.get("genres")
               else "") + f"\n\n{len(films) - len(left) + 1} из {len(films)}")
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👎 Не хочу", callback_data=f"duo:v:{duo_id}:{k}:0"),
        InlineKeyboardButton(text="👍 Буду смотреть", callback_data=f"duo:v:{duo_id}:{k}:1")]])
    if msg and f.get("poster"):
        try:
            await msg.edit_media(InputMediaPhoto(media=f["poster"], caption=caption), reply_markup=markup)
            return
        except Exception:  # noqa: BLE001 — не вышло заменить: пришлём новым сообщением
            pass
    if f.get("poster"):
        try:
            await bot.send_photo(chat_id, f["poster"], caption=caption, reply_markup=markup)
            return
        except Exception:  # noqa: BLE001
            pass
    await bot.send_message(chat_id, caption, reply_markup=markup)


async def duo_result(bot: Bot, duo_id: str, user_id: int) -> None:
    row = await db().fetchrow("SELECT films, creator FROM duo WHERE id=$1", duo_id)
    films = row["films"] if isinstance(row["films"], list) else json.loads(row["films"])
    votes = await db().fetch("SELECT user_id, idx, liked FROM duo_votes WHERE id=$1", duo_id)
    users: dict[int, dict[int, bool]] = {}
    for v in votes:
        users.setdefault(int(v["user_id"]), {})[int(v["idx"])] = bool(v["liked"])
    finished = [u for u, d in users.items() if len(d) >= len(films)]
    if len(finished) < 2:
        await bot.send_message(user_id, "✅ Готово! Ждём, пока друг отметит фильмы, — пришлю совпадения сюда. 👥")
        return
    common = [k for k in range(len(films)) if all(users[u].get(k) for u in finished)]
    if common:
        text = "🎉 <b>Совпадения!</b> Эти фильмы понравились вам обоим:\n\n" + "\n".join(
            f"• {escape(films[k]['title'])}" + (f" ({films[k]['year']})" if films[k].get("year") else "") for k in common)
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text=f"🎬 {films[k]['title']}"[:64], callback_data=f"open:{films[k]['id']}")] for k in common[:8]])
    else:
        text = "😅 Совпадений нет — вкусы разные. Попробуйте ещё раз: «👥 Выбрать вдвоём» даст новые фильмы."
        markup = None
    for u in finished:  # итог — всем, кто уже закончил (второму — сразу, первому — когда закончил второй)
        if u == user_id or len(finished) == 2:
            try:
                await bot.send_message(u, text, reply_markup=markup)
            except Exception:  # noqa: BLE001
                pass


@router.callback_query(F.data.startswith("duo:"))
async def duo_buttons(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 5 or parts[1] != "v" or not parts[3].isdigit():
        await callback.answer()
        return
    await callback.answer()
    await db().execute("""INSERT INTO duo_votes(id, user_id, idx, liked) VALUES($1, $2, $3, $4)
                          ON CONFLICT(id, user_id, idx) DO UPDATE SET liked=EXCLUDED.liked""",
                       parts[2], callback.from_user.id, int(parts[3]), parts[4] == "1")
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    await duo_show(callback.bot, chat_id, callback.from_user.id, parts[2], callback.message)


# --- кинодневник подписчика и «Мой год в кино»

def _diary_score_markup(movie_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=str(n), callback_data=f"dia:s:{movie_id}:{n}") for n in range(1, 6)],
        [InlineKeyboardButton(text=str(n), callback_data=f"dia:s:{movie_id}:{n}") for n in range(6, 11)],
        [InlineKeyboardButton(text="Без оценки", callback_data=f"dia:s:{movie_id}:0")]])


@router.message(F.text == "📔 Кинодневник")
async def diary_menu(message: Message) -> None:
    await remember(message)
    uid, year = message.from_user.id, datetime.now(MSK).year
    rows = await db().fetch("SELECT title, score, watched_at FROM diary WHERE user_id=$1 ORDER BY watched_at DESC",
                            uid)
    this_year = [r for r in rows if r["watched_at"].astimezone(MSK).year == year]
    if not rows:
        await message.answer(
            "📔 <b>Кинодневник</b>\n\nОтмечай, что посмотрел, и ставь оценку — кнопка «✅ Посмотрел» в карточке "
            "любого фильма. В конце года бот соберёт твою карточку «Мой год в кино»: сколько фильмов, любимый "
            "жанр и лучшее за год. 🍿\n\nНайди фильм: «🔎 Найти фильм или сериал».")
        return
    lines = [f"{'⭐ ' + str(r['score']) + '/10' if r['score'] else '✅'} {escape(r['title'])}" for r in rows[:15]]
    await message.answer(
        f"📔 <b>Кинодневник</b>\n\nЗа {year} год: <b>{len(this_year)}</b> · всего: <b>{len(rows)}</b>\n\n"
        + "\n".join(lines) + ("\n…" if len(rows) > 15 else ""),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🎉 Мой {year} в кино", callback_data="dia:y")],
            [InlineKeyboardButton(text="🗑 Убрать последнюю запись", callback_data="dia:undo")]]))


@router.callback_query(F.data.startswith("dia:"))
async def diary_buttons(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    uid = callback.from_user.id
    if parts[1] == "w" and len(parts) == 3 and parts[2].isdigit():
        await callback.answer()
        if callback.message:
            await callback.message.answer("⭐ Как тебе? Оцени от 1 до 10:", reply_markup=_diary_score_markup(int(parts[2])))
        return
    if parts[1] == "s" and len(parts) == 4 and parts[2].isdigit() and parts[3].isdigit():
        movie_id, score = int(parts[2]), max(0, min(10, int(parts[3])))
        try:
            item = await kp.details(movie_id)
        except Exception:  # noqa: BLE001
            item = {}
        genres = ", ".join(str(g.get("name")) for g in (item.get("genres") or []) if isinstance(g, dict))[:120]
        year = item.get("year") if str(item.get("year") or "").isdigit() else None
        title = kp.title(item) if item else (await db().fetchval(
            "SELECT title FROM history WHERE user_id=$1 AND movie_id=$2 LIMIT 1", uid, movie_id) or "Фильм")
        await db().execute(
            """INSERT INTO diary(user_id, movie_id, title, score, genres, year_released, poster)
               VALUES($1, $2, $3, $4, $5, $6, $7)
               ON CONFLICT(user_id, movie_id) DO UPDATE SET score=EXCLUDED.score, watched_at=NOW()""",
            uid, movie_id, title, score, genres, int(year) if year else None, kp.poster(item) if item else None)
        if score:  # оценка дневника — и в «Топ по версии подписчиков»
            await db().execute(
                """INSERT INTO ratings(user_id, movie_id, title, value) VALUES($1, $2, $3, $4)
                   ON CONFLICT(user_id, movie_id) DO UPDATE SET value=EXCLUDED.value, created_at=NOW()""",
                uid, movie_id, title, 1 if score >= 6 else -1)
        n = await db().fetchval("SELECT COUNT(*) FROM diary WHERE user_id=$1 AND watched_at >= $2", uid,
                                datetime(datetime.now(MSK).year, 1, 1, tzinfo=MSK))
        await callback.answer("Записал в кинодневник ✅")
        if callback.message:
            await callback.message.edit_text(
                f"✅ «{escape(title)}» в кинодневнике" + (f" — {score}/10" if score else "")
                + f". В этом году: <b>{n}</b>. Все записи — кнопка «📔 Кинодневник».")
        return
    if parts[1] == "undo":
        await db().execute("""DELETE FROM diary WHERE (user_id, movie_id) IN (
            SELECT user_id, movie_id FROM diary WHERE user_id=$1 ORDER BY watched_at DESC LIMIT 1)""", uid)
        await callback.answer("Убрал последнюю запись")
        return
    if parts[1] == "y":
        await callback.answer()
        year = datetime.now(MSK).year
        rows = [dict(r) for r in await db().fetch(
            "SELECT title, score, genres, year_released, poster FROM diary WHERE user_id=$1 AND watched_at >= $2",
            uid, datetime(year, 1, 1, tzinfo=MSK))]
        if not callback.message:
            return
        if not rows:
            await callback.message.answer("В этом году в дневнике пока пусто — отмечай фильмы кнопкой «✅ Посмотрел».")
            return
        st = shorts_diary.stats(rows)
        posters: list[bytes | None] = []
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            for r in st["best"]:
                try:
                    posters.append(await netsafe.fetch(session, r["poster"], 8 * 1024 * 1024) if r.get("poster")
                                   else None)
                except Exception:  # noqa: BLE001
                    posters.append(None)
        link = (await _bot_link(callback.bot)).replace("https://", "")
        name = callback.from_user.first_name or ""
        png = await asyncio.to_thread(shorts_diary.year_card, name, year, st, posters, link)
        await callback.message.answer_photo(
            BufferedInputFile(png, filename=f"my_{year}_in_cinema.png"),
            caption=f"🎉 Твой {year} в кино! Перешли друзьям — пусть тоже посчитают свой 🍿\n"
                    f"Кинодневник ведётся здесь: {link}")
        return
    await callback.answer()


@router.message(F.text == "🧩 Викторина дня")
async def quiz_day_menu(message: Message) -> None:
    today = datetime.now(POST_TZ).date()
    answered = await db().fetchrow("SELECT correct FROM quiz_answers WHERE day=$1 AND user_id=$2",
                                   today, message.from_user.id)
    if answered:
        await message.answer("✅ Сегодня ты уже отвечал — новый кадр завтра! А пока — таблица лидеров 👇",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [InlineKeyboardButton(text="🏆 Таблица лидеров", callback_data="qd:top")]]))
        return
    row = await db().fetchrow("SELECT * FROM quiz_daily WHERE day=$1", today)
    if row and row["image_file"]:
        await message.answer_photo(row["image_file"], caption=QUIZ_CAPTION,
                                   reply_markup=quiz_markup(today, json.loads(row["options"])))
        return
    wait = await message.answer("⏳ Готовлю кадр дня…")
    try:
        made = await quiz_today(message.bot, message.chat.id)
    except Exception:  # noqa: BLE001
        logging.exception("Daily quiz failed")
        made = None
    await wait.delete()
    if not made:
        await message.answer("😔 Не получилось подготовить кадр — попробуй через пару минут.")


@router.callback_query(F.data.startswith("qd:"))
async def quiz_day_answer(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    if parts[1] == "top":
        await callback.answer()
        leaders = await quiz_leaders()
        mine = next((k for k, r in enumerate(await quiz_leaders(1000), 1) if r["user_id"] == callback.from_user.id), 0)
        text = ("🏆 <b>Знатоки кино этого месяца</b>\n\n" + ("\n".join(
            f"{k}. {_who(r)} — {r['pts']} очк." for k, r in enumerate(leaders, 1)) or "Пока никто не играл.")
            + (f"\n\nТы на {mine}-м месте" if mine else "") + "\n\nКаждый день — новый кадр в «🧩 Викторина дня»")
        if callback.message:
            await callback.message.answer(text)
        return
    day = datetime.strptime(parts[1], "%Y%m%d").date()
    row = await db().fetchrow("SELECT title, answer FROM quiz_daily WHERE day=$1", day)
    if not row:
        await callback.answer("Эта викторина устарела — открой «🧩 Викторина дня»", show_alert=True)
        return
    correct = int(parts[2]) == int(row["answer"])
    inserted = await db().fetchval(
        """INSERT INTO quiz_answers(day, user_id, correct) VALUES($1, $2, $3) ON CONFLICT DO NOTHING
           RETURNING 1""", day, callback.from_user.id, correct)
    if not inserted:
        await callback.answer("Ты уже ответил на этот кадр 😉", show_alert=True)
        return
    await track(callback.from_user.id, "quiz")
    pts = await _count("""SELECT COUNT(*) FROM quiz_answers WHERE user_id=$1 AND correct
                          AND day >= date_trunc('month', CURRENT_DATE)""", callback.from_user.id)
    await callback.answer("✅ Верно!" if correct else "❌ Мимо")
    if callback.message:
        await callback.message.answer(
            (f"✅ <b>Верно!</b> Это {escape(row['title'])} 🎬" if correct
             else f"❌ Не угадал — это {escape(row['title'])} 🎬")
            + f"\n\nОчков в этом месяце: <b>{pts}</b>. Новый кадр — завтра!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏆 Таблица лидеров", callback_data="qd:top")],
                [InlineKeyboardButton(text="🎁 Позвать друга посоревноваться", callback_data="ref:me")]]))


async def make_quiz_winners_post(bot: Bot, chat_id: int) -> None:
    """«🏆 Знатоки месяца»: таблица лидеров викторины дня — пост в канал."""
    leaders = await quiz_leaders()
    if not leaders:
        raise RuntimeError("в этом месяце в викторину ещё никто не играл")
    month = shorts_daily.MONTHS[datetime.now(MSK).month - 1]
    medals = ["🥇", "🥈", "🥉"] + [f"{k}." for k in range(4, 11)]
    post = "\n".join(
        [f"🏆 ЗНАТОКИ КИНО: ИТОГИ ({month.upper()})", "", "Лучшие в «Викторине дня» нашего бота:", ""]
        + [f"{medals[k]} {_who(r)} — {r['pts']} верных ответов" for k, r in enumerate(leaders)]
        + ["", "Хочешь в таблицу? Каждый день новый кадр — жми кнопку под постом и угадывай 👇", "",
           "#викторина #знатоки #KinoMir360"])
    url = f"{await _bot_link(bot)}?start=quiz"
    sent = await bot.send_message(chat_id, post_caption(post), disable_web_page_preview=True)
    await offer_post(bot, chat_id, [sent.message_id], "text", "pt:qw", what="Пост с итогами викторины готов",
                     extra={"button": {"text": "🧩 Играть в викторину дня", "url": url}})


# --- оценки фильмов и «топ по версии подписчиков»

@router.callback_query(F.data.startswith("rate:"))
async def rate_movie(callback: CallbackQuery) -> None:
    _, movie_id, value = (callback.data or "").split(":")
    title = await db().fetchval("SELECT title FROM history WHERE user_id=$1 AND movie_id=$2 LIMIT 1",
                                callback.from_user.id, int(movie_id)) or ""
    await db().execute(
        """INSERT INTO ratings(user_id, movie_id, title, value) VALUES($1, $2, $3, $4)
           ON CONFLICT(user_id, movie_id) DO UPDATE SET value=EXCLUDED.value, created_at=NOW()""",
        callback.from_user.id, int(movie_id), title, int(value))
    await callback.answer("👍 Спасибо за оценку!" if int(value) > 0 else "👎 Учтём — спасибо за оценку!")


async def make_users_top_post(bot: Bot, chat_id: int) -> None:
    """«⭐ Топ по версии подписчиков»: фильмы с лучшими оценками 👍/👎 в боте за 60 дней."""
    rows = await db().fetch(
        """SELECT movie_id, MAX(title) title, SUM(value) score, COUNT(*) votes FROM ratings
           WHERE created_at > NOW() - INTERVAL '60 days' GROUP BY movie_id HAVING COUNT(*) >= 2 AND SUM(value) > 0
           ORDER BY score DESC, votes DESC LIMIT 7""")
    if len(rows) < 3:
        raise RuntimeError(f"мало оценок: фильмов хотя бы с 2 оценками — {len(rows)}, нужно от 3")
    items, lines = [], []
    for k, r in enumerate(rows, 1):
        try:
            film = await kp._get(f"/movie/{int(r['movie_id'])}")
        except Exception:  # noqa: BLE001
            film = None
        name = shorts_daily._title(film) if film else str(r["title"])
        likes = (int(r["votes"]) + int(r["score"])) // 2
        lines.append(f"{k}. «{name}» — 👍 {likes} из {r['votes']}")
        items.append((name, shorts_daily._poster_url(film) if film else None, film))
    post = "\n".join(["⭐ ТОП ПО ВЕРСИИ ПОДПИСЧИКОВ KINOMIR360", "🎬 Подборка от KinoMir360", "",
                      "Эти фильмы и сериалы вы оценили выше всего в нашем боте:", "", *lines, "",
                      "Оценивай фильмы в боте 👍/👎 — и попади в следующий топ!", "", "#топподписчиков #KinoMir360"])
    work = SHORTS_DIR / f"utop_{time.time_ns()}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        ids, kind = await _send_album(bot, chat_id, await _list_cards(work, items), post, "#подборка")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    await offer_post(bot, chat_id, ids, kind, "pt:us", [n for n, _, _ in items], "Топ подписчиков готов")


POST_MAKERS.update({
    "qw": ("🏆 Собираю таблицу лидеров викторины дня…", make_quiz_winners_post),
    "us": ("⭐ Собираю топ по оценкам подписчиков в боте…", make_users_top_post),
})


# --- приглашения друзей

async def referral_text(bot: Bot, user_id: int) -> str:
    link = f"{await _bot_link(bot)}?start=ref_{user_id}"
    n = await _count("SELECT COUNT(*) FROM referrals WHERE referrer_id=$1", user_id)
    s = await _count("SELECT COUNT(*) FROM referrals WHERE referrer_id=$1 AND subscribed", user_id)
    return ("🎁 <b>Пригласи друзей</b>\n\nОтправь другу свою ссылку — пусть тоже подбирает фильмы в боте:\n"
            f"<code>{escape(link)}</code>\n\n"
            f"Пришло по твоей ссылке: <b>{n}</b>, подписались на {escape(POST_CHANNEL)}: <b>{s}</b>.\n"
            "Кто приведёт больше всех подписавшихся друзей — получит дополнительные шансы в наших розыгрышах 🎉")


@router.message(F.text == "🎁 Пригласить друга")
async def referral_menu(message: Message) -> None:
    await message.answer(await referral_text(message.bot, message.from_user.id), disable_web_page_preview=True)


@router.callback_query(F.data == "ref:me")
async def referral_button(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.answer(await referral_text(callback.bot, callback.from_user.id),
                                      disable_web_page_preview=True)


async def save_referral(user_id: int, arg: str) -> None:
    """/start ref_<id>: новый пользователь (пришёл только что) по чужой ссылке."""
    try:
        referrer = int(arg[4:])
    except ValueError:
        return
    if referrer == user_id:
        return
    fresh = await db().fetchval("SELECT created_at > NOW() - INTERVAL '10 minutes' FROM users WHERE user_id=$1", user_id)
    if fresh is False:
        return
    await db().execute("INSERT INTO referrals(user_id, referrer_id) VALUES($1, $2) ON CONFLICT DO NOTHING",
                       user_id, referrer)


# --- идеи подписчиков

@router.message(F.text == "💡 Предложить идею")
async def idea_ask(message: Message, state: FSMContext) -> None:
    await state.set_state(IdeaState.text)
    await message.answer("💡 Про какой фильм или сериал сделать ролик, подборку или разбор? Или что добавить в бот? "
                         "Напиши одним сообщением — лучшие идеи возьмём в работу и отметим автора 🙌\n\n"
                         "<i>Передумал — /cancel</i>")


@router.message(IdeaState.text, F.text, ~F.text.startswith("/"))
async def idea_save(message: Message, state: FSMContext) -> None:
    await state.clear()
    text = (message.text or "").strip()[:1000]
    if len(text) < 3:
        await message.answer("Маловато 🙂 Нажми «💡 Предложить идею» и напиши подробнее.")
        return
    user = message.from_user
    await db().execute("INSERT INTO ideas(user_id, username, text) VALUES($1, $2, $3)",
                       user.id, user.username, text)
    await message.answer("🙌 Спасибо! Идея у нас — следи за каналом, возможно, следующий ролик будет по ней 🎬")
    who = f"@{user.username}" if user.username else (user.first_name or str(user.id))
    for admin_id in ADMIN_IDS:
        try:
            await message.bot.send_message(admin_id, f"💡 <b>Идея от {escape(who)}:</b>\n{escape(text)}")
        except Exception:  # noqa: BLE001
            pass


@router.message(Command("pick"))
async def pick_command(message: Message) -> None:
    text, markup = rq_screen("-", "-", "-", "-")
    await message.answer(text, reply_markup=markup)


@router.message(Command("quiz"))
async def quiz_command(message: Message) -> None:
    await quiz_day_menu(message)


@router.message(F.text == "🎯 Подобрать фильм")
async def rq_menu(message: Message) -> None:
    text, markup = rq_screen("-", "-", "-", "-")
    await message.answer(text, reply_markup=markup)


# ---------------------------------------------------------------- обязательная подписка на канал

SUB_MODES = {"soft": "мягкая (3 запроса бесплатно)", "strict": "строгая (сразу)", "off": "выключена"}
SUB_FREE = 3  # мягкий режим: столько запросов без подписки
SUB_OK_TTL = 1800  # подписку проверяем раз в 30 минут
sub_used: dict[int, int] = {}  # запросы без подписки (мягкий режим; после перезапуска счёт с нуля)
sub_ok: dict[int, float] = {}  # кто подписан → когда проверяли
_sub_mode: tuple[float, str] = (0.0, "soft")


async def sub_mode() -> str:
    global _sub_mode
    if time.time() - _sub_mode[0] > 60:
        try:
            _sub_mode = (time.time(), await shorts_setting("sub_mode") or "soft")
        except Exception:  # noqa: BLE001 — нет базы — не мешаем пользоваться
            _sub_mode = (time.time(), "off")
    return _sub_mode[1]


async def is_subscribed(bot: Bot, user_id: int) -> bool:
    if time.time() - sub_ok.get(user_id, 0) < SUB_OK_TTL:
        return True
    try:
        member = await bot.get_chat_member(POST_CHANNEL, user_id)
    except Exception as exc:  # noqa: BLE001 — бот не админ канала или Telegram не ответил: пускаем
        logging.warning("Subscription check failed: %s", exc)
        return True
    status = str(getattr(member.status, "value", member.status))
    ok = status in ("creator", "administrator", "member") or (status == "restricted" and
                                                              bool(getattr(member, "is_member", False)))
    if ok:
        sub_ok[user_id] = time.time()
        try:  # пришёл по приглашению и подписался — засчитываем пригласившему
            await db().execute("UPDATE referrals SET subscribed=TRUE WHERE user_id=$1 AND NOT subscribed", user_id)
        except Exception:  # noqa: BLE001
            pass
    return ok


def sub_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📢 Подписаться на {POST_CHANNEL}",
                              url=f"https://t.me/{POST_CHANNEL.lstrip('@')}")],
        [InlineKeyboardButton(text="✅ Я подписался", callback_data="sub:check")]])


sub_pending: dict[int, str] = {}  # /start-ссылка, с которой пришёл неподписанный: откроем после подписки


async def subscription_gate(handler: Any, event: Any, data: dict[str, Any]) -> Any:
    """Перед каждым сообщением и кнопкой в личке: подписан ли человек на канал (кроме админов)."""
    user = getattr(event, "from_user", None)
    chat = event.chat if isinstance(event, Message) else (event.message.chat if getattr(event, "message", None) else None)
    if (not user or user.id in ADMIN_IDS or not chat or chat.type != "private"
            or (isinstance(event, CallbackQuery) and event.data == "sub:check")
            # написать админу можно всегда, даже без подписки
            or (isinstance(event, Message) and (event.text in ("✉️ Написать админу", "/contact", "/start contact")
                                                or data.get("raw_state") == ContactState.text.state
                                                or await _contact_thread(event) is not None))):
        return await handler(event, data)
    mode = await sub_mode()
    if mode == "off":
        return await handler(event, data)
    bot = data["bot"]
    if await is_subscribed(bot, user.id):
        return await handler(event, data)
    if mode == "soft" and sub_used.get(user.id, 0) < SUB_FREE:
        sub_used[user.id] = sub_used.get(user.id, 0) + 1
        return await handler(event, data)
    text = (f"🔒 Чтобы пользоваться ботом дальше, подпишись на наш канал {escape(POST_CHANNEL)} — там новинки, "
            "подборки и трейлеры каждый день 🎬\n\nПодписался — жми «✅ Я подписался».")
    await track(user.id, "sub_lock")
    if isinstance(event, Message) and (event.text or "").startswith("/start f_"):
        sub_pending[user.id] = (event.text or "").partition(" ")[2].strip()  # откроем после подписки
    if isinstance(event, CallbackQuery):
        await event.answer()
        if event.message:
            await event.message.answer(text, reply_markup=sub_markup())
    else:
        await event.answer(text, reply_markup=sub_markup())
    return None


@router.callback_query(F.data == "sub:check")
async def sub_check(callback: CallbackQuery) -> None:
    sub_ok.pop(callback.from_user.id, None)
    if await is_subscribed(callback.bot, callback.from_user.id):
        await callback.answer("✅ Спасибо за подписку!")
        await track(callback.from_user.id, "sub_ok")
        if callback.message:
            try:
                await callback.message.edit_text("✅ Спасибо за подписку! Пользуйся ботом — повтори свой запрос 🎬")
            except TelegramBadRequest:
                pass
            arg = sub_pending.pop(callback.from_user.id, "")
            if arg.startswith("f_") and arg[2:].isdigit():  # пришёл по ссылке «Поделиться» — сразу фильм
                try:
                    await send_card(callback.message, await kp.details(int(arg[2:])), callback.from_user.id)
                except Exception:  # noqa: BLE001
                    logging.exception("Shared film after subscribe failed")
    else:
        await callback.answer("Подписка пока не видна — подпишись и нажми ещё раз.", show_alert=True)


async def set_admin_commands(bot: Bot) -> None:
    """Подсказки команд в меню «/» — только у админов."""
    commands = [
        BotCommand(command="admin", description="🛠 Админка: Telegram-канал и YouTube"),
        BotCommand(command="report", description="📊 Отчёт за неделю и план"),
        BotCommand(command="direct", description="📊 Статистика рекламы в Директе"),
        BotCommand(command="hype", description="🔥 Инфоповоды: проверить сейчас, /hype on|off"),
        BotCommand(command="shorts", description="▶️ YouTube и видео (кнопки)"),
        BotCommand(command="yt", description="🔒 YouTube: скрыть, открыть, переименовать видео"),
        BotCommand(command="short", description="Собрать шортс: /short [тема]"),
        BotCommand(command="review", description="Обзор фильма 16:9: /review [название]"),
        BotCommand(command="dub", description="Русский закадровый перевод трейлера"),
        BotCommand(command="lore", description="Шортс «история персонажа»: /lore Доктор Дум"),
        BotCommand(command="mylist", description="Своя подборка: тема и список фильмов"),
        BotCommand(command="ai", description="Проверить ИИ: Gemini, Groq, BeatAPI"),
        BotCommand(command="voice", description="Проверить озвучку фразы"),
        BotCommand(command="clone", description="Свой голос (или кнопками в /shorts → Настройки)"),
        BotCommand(command="say", description="Произношение слов в озвучке"),
        BotCommand(command="stress", description="Ударения в озвучке: режим и сравнение на слух"),
        BotCommand(command="stop", description="Остановить сборку ролика или выпуска"),
        BotCommand(command="trailers", description="Новые трейлеры с YouTube-каналов студий"),
        BotCommand(command="narrate", description="Диктор: текст → проверка → озвучка"),
        BotCommand(command="feeds", description="Ленты новостей кино"),
        BotCommand(command="fast", description="Быстрые источники: Reddit, X, Deadline"),
        BotCommand(command="title", description="Свои названия фильмов: /title Clayface = Глиноликий"),
        BotCommand(command="trailer", description="Проверить скачивание трейлера"),
        BotCommand(command="clips", description="Трейлеры в роликах: on/off"),
        BotCommand(command="music", description="Сменить фоновую музыку"),
        BotCommand(command="cancel", description="Отменить ввод"),
    ]
    try:  # кнопка «Меню» у всех пользователей
        await bot.set_my_commands([
            BotCommand(command="start", description="🎬 Главное меню"),
            BotCommand(command="pick", description="🎯 Подобрать фильм на вечер"),
            BotCommand(command="quiz", description="🧩 Викторина дня"),
            BotCommand(command="contact", description="✉️ Написать админу"),
        ])
    except Exception:
        logging.exception("Failed to set public commands")
    for admin_id in ADMIN_IDS:
        try:
            await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception:
            logging.exception("Failed to set admin commands for %s", admin_id)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s | "
            "%(levelname)s | "
            "%(message)s"
        ),
    )
    for handler in logging.getLogger().handlers:  # ключи из адресов запросов — не в журнал
        handler.addFilter(_RedactSecrets())

    await init_db()
    try:
        await load_ai_mode()
    except Exception:  # noqa: BLE001
        logging.exception("AI mode load failed")
    web_runner = await start_miniapp_server()
    provider_task = asyncio.create_task(
        video_provider_sync_loop()
    )
    blender_task = asyncio.create_task(
        blender_open_movies_sync_loop()
    )

    bot = Bot(
        BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML
        ),
    )

    async def budget_warning(text: str) -> None:  # 80 % месячного лимита — сообщение админам
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, text)
            except Exception:  # noqa: BLE001
                logging.exception("Budget warning to %s failed", admin_id)

    shorts_budget.on_warning = budget_warning

    reminder_task = asyncio.create_task(
        premiere_reminder_loop(bot)
    )
    shorts_task = asyncio.create_task(
        shorts_daily_loop(bot)
    )
    posts_task = asyncio.create_task(posts_auto_loop(bot))
    direct_task = asyncio.create_task(direct_report_loop(bot))  # статистика рекламы в Директе
    hype_task = asyncio.create_task(hype_loop(bot))  # 🔥 громкие инфоповоды → уведомление автору
    growth_task = asyncio.create_task(growth_loop(bot))  # уведомления из избранного, подборка недели, выпуск

    # состояния диалогов — в PostgreSQL (таблица fsm_states, миграция 002): переживают перезапуск бота
    fsm_storage = shorts_fsm.PgStorage(db)
    dp = Dispatcher(storage=fsm_storage)
    dp.message.outer_middleware(activity_tracker)
    dp.callback_query.outer_middleware(activity_tracker)
    dp.message.outer_middleware(subscription_gate)
    dp.callback_query.outer_middleware(subscription_gate)
    dp.include_router(contact_router)
    dp.include_router(router)
    commands_task = asyncio.create_task(set_admin_commands(bot))  # не задерживает запуск

    # Telegram иногда не отвечает вовремя — ждём и повторяем, а не падаем и не перезапускаемся
    for attempt in range(1, 31):
        try:
            await bot.delete_webhook(
                drop_pending_updates=True
            )
            break
        except TelegramNetworkError as exc:
            logging.warning("Telegram unreachable on start (attempt %s): %s", attempt, exc)
            await asyncio.sleep(min(5 * attempt, 60))

    logging.info("VKino started")
    heartbeat_task = asyncio.create_task(heartbeat())  # «бот жив» — по нему автообновление решает, откатывать ли

    try:
        await dp.start_polling(bot)
    finally:
        global stopping
        stopping = True
        heartbeat_task.cancel()
        provider_task.cancel()
        blender_task.cancel()
        reminder_task.cancel()
        shorts_task.cancel()
        posts_task.cancel()
        growth_task.cancel()
        direct_task.cancel()
        hype_task.cancel()
        commands_task.cancel()

        try:
            await provider_task
        except asyncio.CancelledError:
            pass

        try:
            await blender_task
        except asyncio.CancelledError:
            pass

        try:
            await reminder_task
        except asyncio.CancelledError:
            pass

        try:
            await shorts_task
        except asyncio.CancelledError:
            pass

        try:
            await posts_task
        except asyncio.CancelledError:
            pass

        await web_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())

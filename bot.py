from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import re
import time
from datetime import date, datetime, timedelta
from html import escape
from typing import Any
from urllib.parse import parse_qsl, urlencode

import aiohttp
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from dotenv import load_dotenv

from hdrezka_provider import HDRezkaProvider, HDRezkaProviderError

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
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "").strip()

HDREZKA_ENABLED = os.getenv(
    "HDREZKA_ENABLED",
    "false",
).strip().lower() in {"1", "true", "yes", "on"}
HDREZKA_MIRROR = os.getenv("HDREZKA_MIRROR", "").strip().rstrip("/")
HDREZKA_DEFAULT_VOICE = os.getenv(
    "HDREZKA_DEFAULT_VOICE",
    "",
).strip()
try:
    HDREZKA_MAX_VOICES = max(
        1,
        min(24, int(os.getenv("HDREZKA_MAX_VOICES", "8"))),
    )
except ValueError:
    HDREZKA_MAX_VOICES = 8
try:
    HDREZKA_REFRESH_MINUTES = max(
        5,
        int(os.getenv("HDREZKA_REFRESH_MINUTES", "30")),
    )
except ValueError:
    HDREZKA_REFRESH_MINUTES = 30

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

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "803444545").split(",")
    if x.strip().isdigit()
}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not configured")
if not KINOPOISK_TOKEN:
    raise RuntimeError("KINOPOISK_TOKEN is not configured")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not configured")

pool: asyncpg.Pool | None = None
router = Router()

hdrezka_provider = HDRezkaProvider(
    enabled=HDREZKA_ENABLED,
    mirror=HDREZKA_MIRROR,
    default_voice=HDREZKA_DEFAULT_VOICE,
    max_voices=HDREZKA_MAX_VOICES,
)
hdrezka_lock = asyncio.Lock()

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
}


class SearchState(StatesGroup):
    query = State()


class AdminSourceState(StatesGroup):
    search_media = State()
    voice = State()
    url = State()


class PoiskKino:
    BASE_URL = "https://api.poiskkino.dev/v1.5"

    def __init__(self, token: str) -> None:
        self.token = token

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        headers = {"X-API-KEY": self.token, "accept": "application/json"}
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            async with session.get(f"{self.BASE_URL}{path}", params=params) as resp:
                resp.raise_for_status()
                return await resp.json()

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


kp = PoiskKino(KINOPOISK_TOKEN)


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

            CREATE TABLE IF NOT EXISTS hdrezka_cache (
                movie_id BIGINT PRIMARY KEY,
                source_url TEXT NOT NULL,
                source_title TEXT,
                source_year INTEGER,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

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
            """
        )
    logging.info("PostgreSQL connected")


def _kp_titles(item: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for key in ("name", "alternativeName", "enName"):
        value = str(item.get(key) or "").strip()
        if value and value not in result:
            result.append(value)
    return result or [kp.title(item)]


async def hdrezka_watch_button(
    item: dict[str, Any],
) -> InlineKeyboardButton | None:
    """Build a direct HDRezka page button for a matched title."""
    if not HDREZKA_ENABLED or is_upcoming(item):
        return None

    movie_id = int(item["id"])

    cached = await db().fetchrow(
        """
        SELECT source_url
        FROM hdrezka_cache
        WHERE movie_id=$1
        """,
        movie_id,
    )
    if cached:
        cached_url = str(cached["source_url"] or "").strip()
        if cached_url.startswith(("https://", "http://")):
            return InlineKeyboardButton(
                text="▶️ Смотреть HDRezka",
                url=cached_url,
            )

    year_value = item.get("year")
    try:
        year = int(year_value) if year_value else None
    except (TypeError, ValueError):
        year = None

    try:
        async with hdrezka_lock:
            page_url, matched_title, matched_year = await asyncio.wait_for(
                asyncio.to_thread(
                    hdrezka_provider.find_page,
                    titles=_kp_titles(item),
                    year=year,
                    media_type=str(item.get("media_type") or "movie"),
                ),
                timeout=12,
            )
    except Exception as exc:
        logging.info(
            "HDRezka page not found for movie %s: %s",
            movie_id,
            type(exc).__name__,
        )
        return None

    page_url = str(page_url or "").strip()
    if not page_url.startswith(("https://", "http://")):
        return None

    await db().execute(
        """
        INSERT INTO hdrezka_cache(
            movie_id, source_url, source_title, source_year
        )
        VALUES($1,$2,$3,$4)
        ON CONFLICT(movie_id) DO UPDATE SET
            source_url=EXCLUDED.source_url,
            source_title=EXCLUDED.source_title,
            source_year=EXCLUDED.source_year,
            updated_at=NOW()
        """,
        movie_id,
        page_url,
        str(matched_title or kp.title(item))[:300],
        matched_year,
    )

    return InlineKeyboardButton(
        text="▶️ Смотреть HDRezka",
        url=page_url,
    )


async def refresh_hdrezka_sources(
    movie_id: int,
    season_number: int | None = None,
    episode_number: int | None = None,
) -> int:
    if not HDREZKA_ENABLED:
        return 0

    item = await kp.details(movie_id)
    if is_upcoming(item):
        return 0

    cached = await db().fetchrow(
        """
        SELECT source_url
        FROM hdrezka_cache
        WHERE movie_id=$1
        """,
        movie_id,
    )
    cached_url = str(cached["source_url"]) if cached else ""

    year_value = item.get("year")
    try:
        year = int(year_value) if year_value else None
    except (TypeError, ValueError):
        year = None

    try:
        async with hdrezka_lock:
            resolved = await asyncio.to_thread(
                hdrezka_provider.resolve,
                titles=_kp_titles(item),
                year=year,
                media_type=str(item.get("media_type") or "movie"),
                season=season_number,
                episode=episode_number,
                cached_url=cached_url,
            )
    except HDRezkaProviderError as exc:
        logging.info(
            "HDRezka unavailable for movie %s: %s",
            movie_id,
            type(exc).__name__,
        )
        return 0
    except Exception as exc:
        logging.warning(
            "HDRezka refresh failed for movie %s: %s",
            movie_id,
            type(exc).__name__,
        )
        return 0

    if not resolved.sources:
        return 0

    async with db().acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO hdrezka_cache(
                    movie_id, source_url, source_title, source_year
                )
                VALUES($1,$2,$3,$4)
                ON CONFLICT(movie_id) DO UPDATE SET
                    source_url=EXCLUDED.source_url,
                    source_title=EXCLUDED.source_title,
                    source_year=EXCLUDED.source_year,
                    updated_at=NOW()
                """,
                movie_id,
                resolved.source_url,
                resolved.matched_title,
                resolved.matched_year,
            )

            await conn.execute(
                """
                UPDATE playback_sources
                SET is_active=FALSE
                WHERE movie_id=$1
                  AND season_number IS NOT DISTINCT FROM $2
                  AND episode_number IS NOT DISTINCT FROM $3
                  AND provider='hdrezka'
                  AND is_active=TRUE
                """,
                movie_id,
                season_number,
                episode_number,
            )

            for source in resolved.sources:
                await conn.execute(
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
                    VALUES($1,$2,$3,$4,'ru',$5,$6,$7,'hdrezka',TRUE)
                    """,
                    movie_id,
                    season_number,
                    episode_number,
                    str(source["voice_name"])[:80],
                    int(source["quality"]),
                    str(source["playback_url"]),
                    str(source.get("source_type") or "mp4")[:20],
                )

    return len(resolved.sources)


async def ensure_hdrezka_sources(
    movie_id: int,
    season_number: int | None = None,
    episode_number: int | None = None,
) -> int:
    if not HDREZKA_ENABLED:
        return 0

    fresh = await db().fetchval(
        """
        SELECT 1
        FROM playback_sources
        WHERE movie_id=$1
          AND season_number IS NOT DISTINCT FROM $2
          AND episode_number IS NOT DISTINCT FROM $3
          AND provider='hdrezka'
          AND is_active=TRUE
          AND created_at > NOW() - ($4::int * INTERVAL '1 minute')
        LIMIT 1
        """,
        movie_id,
        season_number,
        episode_number,
        HDREZKA_REFRESH_MINUTES,
    )
    if fresh:
        return 0

    return await refresh_hdrezka_sources(
        movie_id,
        season_number,
        episode_number,
    )


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
  const frame = document.getElementById('frame');
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
      const data = await api('/api/trailer?movie_id=' + movieId);
      if (data.title) title.textContent = data.title;
      if (data.youtube_id) {
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
      status.textContent = 'Трейлер пока не найден.';
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
        if auth_date <= 0 or abs(int(time.time()) - auth_date) > max_age:
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

    if HDREZKA_ENABLED:
        await ensure_hdrezka_sources(
            movie_id,
            season_db,
            episode_db,
        )

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
                WHEN provider='hdrezka' THEN 2
                ELSE 3
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
        elif HDREZKA_ENABLED:
            try:
                seasons = await kp.seasons(movie_id)
                episode_pairs: list[tuple[int, int]] = []
                for season_item in seasons:
                    try:
                        sn = int(season_item.get("number"))
                    except (TypeError, ValueError):
                        continue
                    for episode_item in season_item.get("episodes") or []:
                        if not isinstance(episode_item, dict):
                            continue
                        try:
                            en = int(episode_item.get("number"))
                        except (TypeError, ValueError):
                            continue
                        episode_pairs.append((sn, en))
                episode_pairs.sort()
                current = (season, episode)
                candidate = next(
                    (pair for pair in episode_pairs if pair > current),
                    None,
                )
                if candidate:
                    next_item = {
                        "season": candidate[0],
                        "episode": candidate[1],
                    }
            except Exception:
                pass
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


async def api_progress(request: web.Request) -> web.Response:
    user = request_telegram_user(request)
    if not user:
        return web.json_response({"error": "unauthorized"}, status=401)
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
            if quality not in {360, 480, 720, 1080} or not playback_url.startswith("https://"):
                continue
            season = item.get("season")
            episode = item.get("episode")
            season_number = int(season) if season not in (None, "", 0, "0") else None
            episode_number = int(episode) if episode not in (None, "", 0, "0") else None
            source_type = str(item.get("type") or item.get("source_type") or "link")[:20]
            active = bool(item.get("active", True))
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
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Добавить фильм",
                    callback_data="admin:add:movie",
                )
            ],
            [
                InlineKeyboardButton(
                    text="➕ Добавить серию",
                    callback_data="admin:add:episode",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📋 Источники",
                    callback_data="admin:list",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Закрыть",
                    callback_data="admin:cancel",
                )
            ],
        ]
    )


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


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔎 Найти фильм или сериал")],
            [
                KeyboardButton(text="🆕 Новинки"),
                KeyboardButton(text="🔥 Популярное"),
            ],
            [KeyboardButton(text="🔜 Скоро")],
            [KeyboardButton(text="🆓 Смотреть бесплатно")],
            [KeyboardButton(text="🎲 Что посмотреть?")],
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
    if not has_source and not HDREZKA_ENABLED:
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
            lines.append(f"🔜 <b>Ещё не вышел</b>")
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

    keyboard = card_keyboard(
        item,
        favorite,
    )

    hdrezka_button = await hdrezka_watch_button(item)
    if hdrezka_button:
        keyboard.inline_keyboard.insert(
            0,
            [hdrezka_button],
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
        reply_markup=main_menu(),
    )


@router.message(F.text == "🔎 Найти фильм или сериал")
async def ask_search(
    message: Message,
    state: FSMContext,
) -> None:
    await remember(message)
    await state.set_state(SearchState.query)
    await message.answer(
        "Напиши название фильма или сериала 👇"
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
        }
        handler_name = target_handlers.get(query)
        handler = globals().get(handler_name or "")
        if callable(handler):
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
        await message.answer(
            "Ничего не нашёл 😕 "
            "Попробуй другое название."
        )
        return

    await state.clear()

    await message.answer(
        "Вот что нашёл:",
        reply_markup=results_keyboard(items),
    )


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

    if (
        await playback_voices(
            movie_id,
            season_number,
            episode_number,
        )
        or HDREZKA_ENABLED
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

    if not links:
        await callback.message.answer(
            "Для этого фильма площадки "
            "просмотра пока не указаны."
        )
        return

    text = (
        "▶️ <b>Где посмотреть:</b>\n"
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

    if not rows:
        await message.answer(
            "В избранном пока пусто ❤️"
        )
        return

    await message.answer(
        "❤️ Твоё избранное:",
        reply_markup=stored_keyboard(
            rows,
            "❤️",
        ),
    )


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

    ok = 0
    failed = 0

    for user_id in await all_user_ids():
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


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s | "
            "%(levelname)s | "
            "%(message)s"
        ),
    )

    await init_db()
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

    reminder_task = asyncio.create_task(
        premiere_reminder_loop(bot)
    )

    dp = Dispatcher()
    dp.include_router(router)

    await bot.delete_webhook(
        drop_pending_updates=True
    )

    logging.info("VKino started")
    logging.info(
        "HDRezka provider: %s",
        "enabled" if HDREZKA_ENABLED else "disabled",
    )

    try:
        await dp.start_polling(bot)
    finally:
        provider_task.cancel()
        blender_task.cancel()
        reminder_task.cancel()

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

        await web_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())

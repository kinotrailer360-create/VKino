from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import logging
import os
import random
import re
import shutil
import time
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode

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
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from dotenv import load_dotenv

from shorts import ai as shorts_ai
from shorts import tgsearch as shorts_tgsearch
from shorts import news as shorts_news
from shorts import lumean as shorts_lumean
from shorts import pronounce as shorts_pronounce
from shorts.clips import download_clip as shorts_download_clip
from shorts import daily as shorts_daily
from shorts import copycat as shorts_copycat
from shorts import dub as shorts_dub
from shorts import lore as shorts_lore
from shorts import review as shorts_review
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
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
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


class ShortsState(StatesGroup):
    facts_title = State()
    say_entry = State()
    voice_text = State()
    trailer_query = State()
    feed_url = State()
    music_file = State()
    clone_audio = State()
    fact_channels = State()


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


class CustomState(StatesGroup):
    text = State()
    video = State()


class LoreState(StatesGroup):
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
            """
        )
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
                    text="🎬 Шортсы",
                    callback_data="sh:menu",
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
            f"{escape(str(exc)[:300])}</code>"
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
            f"<code>{escape(str(exc))}</code>"
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
SHORT_THEMES = {
    "online": "онлайн-премьеры",
    "cinema": "сейчас в кино",
    "series": "новые сериалы",
    "upcoming": "скоро в кино",
    "genre": "топ случайного жанра",
    "facts": "5 фактов о фильме",
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
    shorts_pronounce.CUSTOM = table if isinstance(table, dict) else {}
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
                             [_btn("✏️ Изменить список", "sh:ask:tgch")],
                             [_btn("↩️ Стандартный список", "sh:tgch_default")],
                             [_btn("⬅️ Настройки", "sh:settings")]]))


async def news_feeds() -> list[str]:
    raw = await shorts_setting("news_feeds")
    try:
        feeds = json.loads(raw) if raw else None
    except ValueError:
        feeds = None
    return feeds if isinstance(feeds, list) and feeds else list(shorts_news.DEFAULT_FEEDS)


async def send_short(bot: Bot, chat_id: int, result: Any) -> None:
    seo = result.seo
    await bot.send_video(
        chat_id,
        FSInputFile(result.video, filename="kinomir360_short.mp4"),
        caption=f"🎬 <b>{escape(seo['title'])}</b>",
        width=1080,
        height=1920,
        supports_streaming=True,
    )
    for label, key in (
        ("Название", "title"),
        ("Описание", "description"),
        ("Теги", "tags"),
        ("Закреплённый комментарий", "pinned"),
    ):
        await bot.send_message(
            chat_id,
            f"<b>{label}:</b>\n<pre>{escape(seo[key])}</pre>",
        )
    await bot.send_message(
        chat_id,
        f"<b>TikTok — подпись с хэштегами:</b>\n<pre>{escape(shorts_daily.tiktok_caption(result))}</pre>",
    )
    if seo.get("warning"):
        await bot.send_message(chat_id, escape(seo["warning"]))


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
            music = None
            file_id = await shorts_setting("music_file_id")
            if file_id:
                music = work / "music_custom"
                try:
                    await bot.download(file_id, destination=music)
                except Exception:
                    logging.exception("Shorts: custom music download failed")
                    music = None

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


async def shorts_daily_loop(
    bot: Bot,
) -> None:
    await report_interrupted_short(bot)
    while True:
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
                                f"<code>{escape(str(exc)[-700:])}</code>\n\n"
                                "Попробуй вручную: /short",
                            )
                        except Exception:
                            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Shorts loop error")
        await asyncio.sleep(60)


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
    if theme.lower().startswith("facts"):
        # /short facts Матрица — название фильма оставляем как есть
        query = theme[5:].strip(" :")
        theme = f"facts:{query}" if query else "facts"
    else:
        theme = theme.lower()
    if theme and theme not in SHORT_THEMES and not theme.startswith(("genre:", "facts:")):
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
                f"<code>{escape(str(exc)[-700:])}</code>"
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
        mark = f"✅ {res}" if isinstance(res, int) and res else f"❌ {escape(str(res))}"
        lines.append(f"{i}. {mark}\n<code>{escape(url[:90])}</code>")
    lines.append(
        "\n<code>/feeds add URL</code> — добавить RSS\n"
        "<code>/feeds del N</code> — удалить\n"
        "<code>/feeds default</code> — стандартный список"
    )
    await message.answer("\n".join(lines), disable_web_page_preview=True)


@router.message(Command("say"))
async def say_command(
    message: Message,
) -> None:
    if (
        not message.from_user
        or message.from_user.id not in ADMIN_IDS
    ):
        return
    await say_action(message, (message.text or "").partition(" ")[2].strip())


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
        word, _, spoken = (x.strip() for x in arg.partition("="))
        if not word or not spoken:
            await message.answer("Формат: <code>/say IMAX = Аймакс</code>")
            return
        table[word] = spoken
        await set_shorts_setting("pronounce", json.dumps(table, ensure_ascii=False))
        await message.answer(
            f"✅ «{escape(word)}» теперь читается как «{escape(shorts_pronounce.mark_stress(spoken))}».\n"
            f"Проверить: <code>/voice {escape(word)}</code>"
        )
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
        notes.append(f"Rutube: поиск не удался ({type(exc).__name__}: {str(exc)[:120]})")
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
                f"❌ <code>{escape(type(exc).__name__)}: {escape(str(exc)[:300])}</code>",
                disable_web_page_preview=True,
            )
        finally:
            out.unlink(missing_ok=True)
        return
    await message.answer("⏳ Ищу трейлер и пробую скачать 4 секунды…")
    try:
        found = await kp.search(query, limit=1)
    except Exception as exc:
        await message.answer(f"❌ Поиск не удался: <code>{escape(str(exc)[:200])}</code>")
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
            report.append(f"❌ {name}: <code>{escape(type(exc).__name__)}: {escape(str(exc)[:250])}</code>")
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


async def ai_status_action(message: Message) -> None:
    """Какие ИИ подключены; BeatAPI — проверка ключа, баланс и бесплатные модели (без платных запросов)."""
    rows = [
        "🤖 <b>ИИ бота</b> (порядок: основной → запасные)",
        f"1. Gemini: {'ключ есть ✅' if GEMINI_API_KEY else 'нет GEMINI_API_KEY ❌'} ({escape(shorts_ai.MODEL)})",
        f"2. Groq: {'ключ есть ✅' if shorts_ai.RESERVE_KEY else 'нет RESERVE_AI_KEY ❌'} ({escape(shorts_ai.RESERVE_MODEL)})",
    ]
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
            rows.append(f"3. BeatAPI: ❌ {escape(str(exc)[:300])}")
        await wait.delete()
    await message.answer("\n".join(rows))


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
        await message.answer(f"❌ Не получилось отправить голос на клонирование: <code>{escape(str(exc)[:400])}</code>")
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


def shorts_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("🎬 Тема дня", "sh:go:"), _btn("📰 Новости кино", "sh:go:news")],
        [_btn("🆕 Онлайн-премьеры", "sh:go:online"), _btn("🍿 Кинопрокат", "sh:go:cinema")],
        [_btn("📺 Сериалы", "sh:go:series"), _btn("⏳ Скоро в кино", "sh:go:upcoming")],
        [_btn("🎭 Топ жанра ▸", "sh:genres"), _btn("🎂 День рождения", "sh:go:birthday")],
        [_btn("🎯 Своя подборка", "cs:text"), _btn("🔁 По образцу (видео)", "cs:video")],
        [_btn("💡 Факты: случайный", "sh:go:facts"), _btn("📖 История персонажа", "lo:start")],
        [_btn("🎞 Обзор фильма 16:9", "rv:start"), _btn("🎙 Перевод трейлера", "dub:start")],
        # поиск с подсказками прямо при наборе (inline-режим бота)
        [InlineKeyboardButton(text="🔎 Факты: найти фильм/сериал", switch_inline_query_current_chat="")],
        [_btn("✍️ Факты: ввести название", "sh:ask:facts"), _btn("🔎 Факты: интернет или свой текст", "fx:start")],
        [_btn("⚙️ Настройки ▸", "sh:settings")],
        [_btn("⬅️ В админку", "admin:home")],
    ])


def shorts_genres_menu() -> InlineKeyboardMarkup:
    rows = [[_btn(g.capitalize(), f"sh:go:genre:{g}") for g in SHORTS_GENRES[k:k + 2]]
            for k in range(0, len(SHORTS_GENRES), 2)]
    rows.append([_btn("🎲 Случайный жанр", "sh:go:genre")])
    rows.append([_btn("⬅️ Назад", "sh:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def shorts_settings_menu() -> InlineKeyboardMarkup:
    clips_on = await shorts_setting("clips") != "off"
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn(f"🎞 Трейлеры: {'вкл ✅' if clips_on else 'выкл ❌'}", "sh:clips")],
        [_btn("🎵 Сменить музыку", "sh:ask:music"), _btn("↩️ Стандартная", "sh:music_default")],
        [_btn("🗣 Произношение", "sh:say"), _btn("➕ Добавить слово", "sh:ask:say")],
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
        await message.answer(f"❌ Не получилось загрузить голоса: <code>{escape(str(exc)[:300])}</code>")
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
        await message.answer(f"❌ Не получилось загрузить образец: <code>{escape(str(exc)[:300])}</code>")
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
    "🎬 <b>Шортсы KinoMir360</b>\n\n"
    "Нажми тему — ролик соберётся за 2–5 минут и придёт сюда вместе с SEO для YouTube и TikTok.\n"
    "Каждый день в 8:00 по Москве ролик приходит сам."
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
    "clone": (ShortsState.clone_audio,
              "🎤 Запишите и пришлите сюда голосовое сообщение на 30–60 секунд — сделаю из него ваш голос."),
    "tgch": (ShortsState.fact_channels,
             "📡 Пришли список каналов одним сообщением — каждый с новой строки, ссылкой или @именем "
             "(например <code>https://t.me/KinoMir360</code>). Этот список заменит текущий."),
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
    elif data == "sh:genres":
        await show("🎭 <b>Топ какого жанра собрать?</b>", shorts_genres_menu())
    elif data == "sh:settings":
        await show("⚙️ <b>Настройки шортсов</b>", await shorts_settings_menu())
    elif data.startswith("sh:go:"):
        await start_short(message, data[len("sh:go:"):])
    elif data.startswith("sh:ask:"):
        target, prompt = SHORTS_ASKS[data[len("sh:ask:"):]]
        await state.set_state(target)
        await message.answer(prompt + "\n\n<i>Передумал — /cancel</i>")
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
async def shorts_fact_channels(message: Message, state: FSMContext) -> None:
    raw = [ln for ln in re.split(r"[\s,]+", _typed(message)) if ln]
    channels = list(dict.fromkeys(c for c in (shorts_tgsearch.channel_name(r) for r in raw) if c))
    bad = [r for r in raw if not shorts_tgsearch.channel_name(r)]
    if not channels:
        await message.answer("Не вижу каналов — нужны ссылки вида https://t.me/имя или @имя.")
        return
    await state.clear()
    await set_shorts_setting("fact_channels", json.dumps(channels[:30]))
    await load_fact_channels()
    note = f"\n⚠️ Не понял: {escape(', '.join(bad[:10]))}" if bad else ""
    await message.answer(f"✅ Сохранил {len(channels[:30])} каналов.{note}")
    await channels_action(message)


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


async def custom_music(bot: Bot, work: Path) -> Path | None:
    """Своя фоновая музыка (кнопка «Сменить музыку»), если задана."""
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
    try:
        film = await shorts_review.find_film(kp._get, query)
    except Exception as exc:
        logging.exception("Review film search failed")
        await wait.edit_text(f"❌ Не нашёл: <code>{escape(str(exc)[:300])}</code>\nНапиши название иначе.")
        return
    title = shorts_daily._title(film)
    year = f" ({film['year']})" if film.get("year") else ""
    await state.set_state(None)
    await state.update_data(review_film=int(film["id"]), review_title=title, review_notes="", review_script=None,
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
    await state.update_data(review_script=script)
    data = await state.get_data()
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
        film = await kp._get(f"/movie/{data['review_film']}")
        script = await shorts_review.write_script(film, notes, GEMINI_API_KEY)
    except Exception as exc:
        logging.exception("Review script failed")
        await wait.edit_text(f"❌ Сценарий не написался: <code>{escape(str(exc)[:400])}</code>",
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
    await bot.send_video(
        chat_id,
        FSInputFile(result.video, filename="kinomir360_review.mp4"),
        caption=f"🎞 <b>{escape(seo['title'])}</b>",
        width=1280,
        height=720,
        supports_streaming=True,
        request_timeout=600,  # файл до 50 МБ — загрузка дольше обычного
    )
    for label, key in (("Название", "title"), ("Описание (с таймкодами)", "description"),
                       ("Теги", "tags"), ("Закреплённый комментарий", "pinned")):
        await bot.send_message(chat_id, f"<b>{label}:</b>\n<pre>{escape(seo[key])}</pre>")
    if seo.get("warning"):
        await bot.send_message(chat_id, escape(seo["warning"]))


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
            await message.answer(f"❌ Обзор не собрался:\n<code>{escape(str(exc)[-700:])}</code>")

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
    what = "новостей и слухов" if data.get("fx_mode") == "rumors" else "фактов"
    rows = [f"💡 <b>{len(facts)} {what} о «{escape(data.get('fx_label') or data['fx_title'])}»</b> — "
            "проверь, всё ли правда:", ""]
    rows += [f"{i}. {escape(f)}" for i, f in enumerate(facts, 1)]
    if data.get("fx_sources"):
        rows += ["", f"🔎 <i>Источники: {escape(', '.join(data['fx_sources'][:8]))}</i>"]
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
        await wait.edit_text(f"❌ Не получилось: <code>{escape(str(exc)[:400])}</code>",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [_btn("🔄 Ещё раз", "fx:redo")], [_btn("❌ Отмена", "fx:cancel")]]))
        return
    await wait.delete()
    await state.update_data(fx_facts=facts)
    await show_facts(message, state)


async def facts_web(message: Message, state: FSMContext) -> None:
    """Факты из интернета: ИИ ищет в Google, Википедии и Telegram-каналах (плюс факты Кинопоиска),
    автор проверяет и правит. Режим «rumors» — новости и слухи о фильме, который ещё не вышел."""
    data = await state.get_data()
    rumors = data.get("fx_mode") == "rumors"
    await load_fact_channels()
    wait = await message.answer(("📰 ИИ собирает новости и слухи" if rumors else "🔎 ИИ ищет факты")
                                + " в интернете и Telegram-каналах — до минуты…")
    try:
        film = await shorts_daily.find_film(kp._get, data["fx_title"], upcoming=rumors)
        if not film:
            raise RuntimeError(f"Кинопоиск не нашёл «{data['fx_title']}» — уточни название или добавь год")
        label = shorts_daily._title(film) + (f", {film['year']}" if film.get("year") else "")
        await wait.edit_text(f"{wait.text}\nНашёл: «{escape(label)}»")
        facts, sources = await shorts_daily.web_facts(film, shorts_daily.usable_facts(film), GEMINI_API_KEY,
                                                      n=8, rumors=rumors)
    except Exception as exc:
        logging.exception("Facts web search failed")
        await wait.edit_text(f"❌ Не получилось: <code>{escape(str(exc)[:400])}</code>",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [_btn("🔄 Ещё раз", "fx:redo")], [_btn("📝 Вставлю свой текст", "fx:own")],
                                 [_btn("❌ Отмена", "fx:cancel")]]))
        return
    await wait.delete()
    await state.update_data(fx_facts=facts, fx_sources=sources, fx_film=f"id:{film['id']}", fx_label=label)
    await show_facts(message, state)


async def run_text_facts(bot: Bot, chat_id: int, title: str, facts: list[str], status: Message,
                         media: list[dict[str, str]] | None = None, rumors: bool = False) -> None:
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
                rumors=rumors),
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


@router.message(FactsState.title, F.text, ~F.text.startswith("/"))
async def facts_title(message: Message, state: FSMContext) -> None:
    await state.update_data(fx_title=_typed(message), fx_mode="", fx_film="", fx_label="", fx_sources=[])
    await state.set_state(None)
    await message.answer("Откуда взять факты?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [_btn("🔎 ИИ найдёт в интернете", "fx:web")],
        [_btn("📰 Новости и слухи (ещё не вышел)", "fx:rumors")],
        [_btn("📝 Вставлю свой текст", "fx:own")],
        [_btn("❌ Отмена", "fx:cancel")]]))


@router.message(FactsState.text, F.text, ~F.text.startswith("/"))
async def facts_text(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await state.update_data(fx_text=_typed(message), fx_mode="text", fx_sources=[], fx_film="", fx_label="")
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
        await state.set_state(FactsState.text)
        await message.answer("📝 Вставь текст с фактами одним сообщением (ссылки на источники можно не убирать). "
                             "ИИ выберет 5 самых интересных и ничего не добавит от себя.\n\n<i>Передумал — /cancel</i>")
    elif action in ("web", "rumors"):
        await state.update_data(fx_mode=action)
        await facts_web(message, state)
    elif action == "redo" and data.get("fx_mode") != "text":
        await facts_web(message, state)
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
                                     data.get("fx_mode") == "rumors")
            except Exception as exc:
                logging.exception("Text facts short failed")
                try:
                    await status.edit_text("❌ Сборка остановилась — причина ниже")
                except TelegramBadRequest:
                    pass
                await message.answer(f"❌ Шортс не собрался:\n<code>{escape(str(exc)[-700:])}</code>")

        task = asyncio.create_task(job())
        shorts_jobs.add(task)
        task.add_done_callback(shorts_jobs.discard)


# ---------------------------------------------------------------- своя подборка и «по образцу»

CUSTOM_TIMEOUT = 15 * 60
CUSTOM_HELP = (
    "Первой строкой — тема, дальше фильмы, по одному на строку (год — по желанию):\n"
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
        await message.answer("🎯 <b>Своя подборка</b>\n\n" + CUSTOM_HELP + "\n\n<i>Передумал — /cancel</i>")


async def show_plan(message: Message, state: FSMContext, title: str, films: list[dict[str, Any]]) -> None:
    wait = await message.answer("🔎 Ищу фильмы на Кинопоиске…")
    items = await shorts_copycat.resolve(kp._get, films)
    await wait.delete()
    found = [it for it in items if it["id"]]
    await state.update_data(cs_title=title, cs_items=items)
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


async def run_custom(bot: Bot, chat_id: int, title: str, ids: list[int], status: Message) -> None:
    async with shorts_lock:
        await load_pronunciations()
        await load_voice()
        await load_fact_channels()
        work = SHORTS_DIR / datetime.now(MSK).strftime("custom_%Y%m%d_%H%M%S")
        work.mkdir(parents=True, exist_ok=True)
        try:
            music = await custom_music(bot, work)
            await set_shorts_setting("running", json.dumps({"chats": [chat_id], "theme": "своя подборка"}))
            progress = progress_updater(status)
            shorts_daily.on_progress, shorts_daily.last_stage = progress, ""
            result = await asyncio.wait_for(shorts_daily.make_custom_short(
                kp._get, work, title, ids, music, TMDB_API_KEY,
                trailer_clips=await shorts_setting("clips") != "off"), timeout=CUSTOM_TIMEOUT)
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


@router.message(CustomState.text, F.text, ~F.text.startswith("/"))
async def custom_text(message: Message, state: FSMContext) -> None:
    try:
        title, films = shorts_copycat.parse_plan(_typed(message))
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
        await wait.edit_text(f"❌ Не получилось: <code>{escape(str(exc)[:400])}</code>")
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
        if shorts_lock.locked():
            await message.answer("⏳ Сейчас собирается другой ролик — нажми «Собрать» ещё раз через пару минут.")
            return
        ids = [it["id"] for it in data["cs_items"] if it["id"]]
        await state.clear()
        status = await message.answer(progress_bar(0, "Начинаю, обычно это 3–6 минут"))

        async def job() -> None:
            try:
                await run_custom(message.bot, message.chat.id, data["cs_title"], ids, status)
            except Exception as exc:
                logging.exception("Custom short failed")
                try:
                    await status.edit_text("❌ Сборка остановилась — причина ниже")
                except TelegramBadRequest:
                    pass
                await message.answer(f"❌ Шортс не собрался:\n<code>{escape(str(exc)[-700:])}</code>")

        task = asyncio.create_task(job())
        shorts_jobs.add(task)
        task.add_done_callback(shorts_jobs.discard)


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


async def lore_generate(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    wait = await message.answer("✍️ Читаю Википедию и пишу сценарий, обычно до минуты…")
    try:
        script = await shorts_lore.write_script(data["lore_name"], data.get("lore_hook") or "", GEMINI_API_KEY)
    except Exception as exc:
        logging.exception("Lore script failed")
        await wait.edit_text(f"❌ Сценарий не написался: <code>{escape(str(exc)[:400])}</code>",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [_btn("🔄 Ещё раз", "lo:redo")], [_btn("❌ Отмена", "lo:cancel")]]))
        return
    await wait.delete()
    await state.update_data(lore_script=script)
    await show_lore(message, state)


async def show_lore(message: Message, state: FSMContext, brief: bool = False) -> None:
    data = await state.get_data()
    script = data["lore_script"]
    media = data.get("lore_media") or []
    chars = sum(len(x) for x in script["lines"])
    head = (f"📝 <b>{escape(script.get('title') or data['lore_name'])}</b>\n"
            f"{chars} символов ≈ {round(chars / 15)} с • источник: "
            f"<a href=\"{escape(script.get('url') or '')}\">{escape(script.get('article') or 'Википедия')}</a>\n"
            "Каждая строка — отдельная картинка в ролике.")
    body = "" if brief else "\n\n" + escape(shorts_lore.script_text(script))
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("✅ Собрать шортс", "lo:go")], [_btn("✏️ Поправить текст", "lo:edit")],
        [_btn(f"📎 Свои картинки ({len(media)})" if media else "📎 Добавить свои картинки", "lo:media")],
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
            for k, item in enumerate(data.get("lore_media") or []):
                path = work / f"user{k}{item['ext']}"
                try:
                    await bot.download(item["file_id"], destination=path)
                    files.append(path)
                except Exception:
                    logging.exception("Lore: user file download failed")
            progress = progress_updater(status)
            shorts_daily.on_progress, shorts_daily.last_stage = progress, ""
            result = await asyncio.wait_for(shorts_lore.make_lore(
                work, data["lore_script"], music, files, GEMINI_API_KEY), timeout=LORE_TIMEOUT)
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


@router.message(LoreState.media, F.photo | F.document)
async def lore_media(message: Message, state: FSMContext) -> None:
    if message.photo:
        file, ext = message.photo[-1], ".jpg"
    else:
        doc = message.document
        if not str(doc.mime_type or "").startswith("image/"):
            await message.answer("Нужны картинки (страницы комиксов, кадры, арты) — это не картинка.")
            return
        file, ext = doc, Path(doc.file_name or "").suffix.lower() or ".jpg"
    if (file.file_size or 0) > TG_DOWNLOAD_LIMIT:
        await message.answer("⚠️ Файл больше 20 МБ — бот Telegram не может его скачать.")
        return
    async with review_media_lock:
        media = (await state.get_data()).get("lore_media") or []
        if len(media) >= LORE_MEDIA_MAX:
            await message.answer(f"Хватит: уже {LORE_MEDIA_MAX} картинок. Нажми «✅ Готово».")
            return
        media.append({"file_id": file.file_id, "ext": ext})
        await state.update_data(lore_media=media)
    await message.answer(f"📎 Принято: {len(media)}", reply_markup=InlineKeyboardMarkup(
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
            "📎 Присылай картинки — страницы комиксов, кадры, арты, по одной или альбомом (до 24). "
            "ИИ поставит каждую к подходящей фразе, камера проедет по странице сверху вниз. "
            "Файлы до 20 МБ.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [_btn("✅ Готово", "lo:media_done")], [_btn("🗑 Убрать все мои картинки", "lo:media_clear")]]))
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
                await message.answer(f"❌ Шортс не собрался:\n<code>{escape(str(exc)[-700:])}</code>")

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
        await wait.edit_text(f"❌ Не получилось: <code>{escape(str(exc)[:400])}</code>")
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
                await message.answer(f"❌ Перевод не собрался:\n<code>{escape(str(exc)[-700:])}</code>")

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


async def set_admin_commands(bot: Bot) -> None:
    """Подсказки команд в меню «/» — только у админов."""
    commands = [
        BotCommand(command="shorts", description="🎬 Панель шортсов (кнопки)"),
        BotCommand(command="short", description="Собрать шортс: /short [тема]"),
        BotCommand(command="review", description="Обзор фильма 16:9: /review [название]"),
        BotCommand(command="dub", description="Русский закадровый перевод трейлера"),
        BotCommand(command="lore", description="Шортс «история персонажа»: /lore Доктор Дум"),
        BotCommand(command="mylist", description="Своя подборка: тема и список фильмов"),
        BotCommand(command="ai", description="Проверить ИИ: Gemini, Groq, BeatAPI"),
        BotCommand(command="admin", description="Админка"),
        BotCommand(command="voice", description="Проверить озвучку фразы"),
        BotCommand(command="clone", description="Свой голос (или кнопками в /shorts → Настройки)"),
        BotCommand(command="say", description="Произношение слов в озвучке"),
        BotCommand(command="feeds", description="Ленты новостей кино"),
        BotCommand(command="trailer", description="Проверить скачивание трейлера"),
        BotCommand(command="clips", description="Трейлеры в роликах: on/off"),
        BotCommand(command="music", description="Сменить фоновую музыку"),
        BotCommand(command="cancel", description="Отменить ввод"),
    ]
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
    shorts_task = asyncio.create_task(
        shorts_daily_loop(bot)
    )

    dp = Dispatcher()
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

    try:
        await dp.start_polling(bot)
    finally:
        global stopping
        stopping = True
        provider_task.cancel()
        blender_task.cancel()
        reminder_task.cancel()
        shorts_task.cancel()
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

        await web_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())

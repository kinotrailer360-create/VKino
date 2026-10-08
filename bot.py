from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
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
try:
    VIDEO_PROVIDER_SYNC_SECONDS = max(
        60,
        int(os.getenv("VIDEO_PROVIDER_SYNC_SECONDS", "900")),
    )
except ValueError:
    VIDEO_PROVIDER_SYNC_SECONDS = 900

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

    async def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        data = await self._get("/movie/search", query=query, page=1, limit=limit)
        return [self.normalize(x) for x in (data.get("docs") or [])[:limit]]

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
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_playback_sources_media
            ON playback_sources(
                movie_id,
                season_number,
                episode_number,
                is_active
            );

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
        SELECT voice_name, quality, playback_url, source_type
        FROM playback_sources
        WHERE movie_id=$1
          AND season_number IS NOT DISTINCT FROM $2
          AND episode_number IS NOT DISTINCT FROM $3
          AND is_active=TRUE
        ORDER BY voice_name, quality
        """,
        movie_id,
        season_db,
        episode_db,
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["voice_name"]), []).append(
            {
                "quality": int(row["quality"]),
                "url": str(row["playback_url"]),
                "type": str(row["source_type"] or "link"),
            }
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
                SET playback_url=$2, source_type=$3, is_active=$4
                WHERE id=$1
                """,
                int(existing_id), playback_url, source_type, active,
            )
        else:
            await db().execute(
                """
                INSERT INTO playback_sources(
                    movie_id, season_number, episode_number,
                    voice_name, quality, playback_url, source_type, is_active
                ) VALUES($1,$2,$3,$4,$5,$6,$7,$8)
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


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔎 Найти фильм или сериал")],
            [
                KeyboardButton(text="🆕 Новинки"),
                KeyboardButton(text="🔥 Популярное"),
            ],
            [KeyboardButton(text="🎲 Что посмотреть?")],
            [
                KeyboardButton(text="🎬 Подборки"),
                KeyboardButton(text="⭐ Избранное"),
            ],
            [
                KeyboardButton(text="🕘 История"),
                KeyboardButton(text="👤 Профиль"),
            ],
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
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{icon} {title} ({year})",
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
            text="▶️ Смотреть",
            web_app=WebAppInfo(url=app_url),
        )

    return InlineKeyboardButton(
        text="▶️ Смотреть",
        callback_data=f"playvoices:{movie_id}:0:0",
    )


def card_keyboard(
    item: dict[str, Any],
    favorite: bool,
) -> InlineKeyboardMarkup:
    media_type = item.get("media_type", "movie")
    movie_id = int(item["id"])
    rows: list[list[InlineKeyboardButton]] = []

    if media_type == "series":
        rows.append(
            [
                InlineKeyboardButton(
                    text="📺 Сезоны и серии",
                    callback_data=f"seasons:{movie_id}",
                )
            ]
        )

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
                    else "⭐ В избранное"
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

    if media_type == "movie":
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
        "🔎 Поиск фильмов и сериалов\n"
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

    if len(query) < 2:
        await message.answer(
            "Напиши хотя бы 2 символа."
        )
        return

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


@router.message(F.text == "⭐ Избранное")
async def favorites_menu(
    message: Message,
) -> None:
    await remember(message)

    rows = await list_favorites(
        message.from_user.id
    )

    if not rows:
        await message.answer(
            "В избранном пока пусто ⭐"
        )
        return

    await message.answer(
        "⭐ Твоё избранное:",
        reply_markup=stored_keyboard(
            rows,
            "⭐",
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

    if not rows:
        await message.answer(
            "История пока пустая. "
            "Открой карточку фильма "
            "или сериала 🍿"
        )
        return

    await message.answer(
        "🕘 Недавно смотрел:",
        reply_markup=stored_keyboard(
            rows,
            "🕘",
        ),
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
        "подборки, история и избранное.\n"
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

    bot = Bot(
        BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML
        ),
    )

    dp = Dispatcher()
    dp.include_router(router)

    await bot.delete_webhook(
        drop_pending_updates=True
    )

    logging.info("VKino started")

    try:
        await dp.start_polling(bot)
    finally:
        provider_task.cancel()
        try:
            await provider_task
        except asyncio.CancelledError:
            pass
        await web_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())

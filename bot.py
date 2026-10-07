from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import date, datetime, timedelta
from html import escape
from typing import Any

import aiohttp
import asyncpg
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
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
KINOPOISK_TOKEN = os.getenv("KINOPOISK_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
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

    if poster:
        try:
            await message.answer_photo(
                poster,
                caption=format_card(item),
                reply_markup=card_keyboard(
                    item,
                    favorite,
                ),
            )
            return
        except Exception:
            pass

    await message.answer(
        format_card(item),
        reply_markup=card_keyboard(
            item,
            favorite,
        ),
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

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
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

    await message.answer(
        "👤 <b>Профиль VKino</b>\n\n"
        f"🆔 ID: <code>"
        f"{message.from_user.id}</code>\n"
        f"🔗 Username: {username}\n"
        f"📅 С нами с: <b>{created}</b>\n"
        f"⭐ В избранном: <b>{fav_count}</b>\n"
        f"🕘 В истории: "
        f"<b>{history_count}</b>"
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
) -> None:
    if (
        not message.from_user
        or message.from_user.id not in ADMIN_IDS
    ):
        return

    users, favs, history = (
        await global_stats()
    )

    await message.answer(
        "🛠 <b>VKino — админка</b>\n\n"
        f"👥 Пользователей: {users}\n"
        f"⭐ Избранное: {favs}\n"
        f"🕘 История: {history}\n\n"
        "<code>/broadcast текст</code> — "
        "рассылка"
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

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

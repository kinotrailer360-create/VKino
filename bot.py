from __future__ import annotations

import asyncio
import logging
import os
import random
from html import escape
from typing import Any

import aiohttp
import aiosqlite
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
DB_PATH = os.getenv("DB_PATH", "vkino.db").strip() or "vkino.db"
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "803444545").split(",")
    if x.strip().isdigit()
}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not configured")
if not KINOPOISK_TOKEN:
    raise RuntimeError("KINOPOISK_TOKEN is not configured")


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

    async def popular(self, limit: int = 8, series: bool | None = None) -> list[dict[str, Any]]:
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
        return item.get("name") or item.get("alternativeName") or item.get("enName") or "Без названия"

    @staticmethod
    def poster(item: dict[str, Any]) -> str | None:
        poster = item.get("poster") or {}
        return (poster.get("url") or poster.get("previewUrl")) if isinstance(poster, dict) else None

    @staticmethod
    def trailer(item: dict[str, Any]) -> str | None:
        videos = item.get("videos") or {}
        for trailer in videos.get("trailers", []) if isinstance(videos, dict) else []:
            if isinstance(trailer, dict) and trailer.get("url"):
                return str(trailer["url"])
        return None

    @staticmethod
    def watch_links(item: dict[str, Any]) -> list[tuple[str, str]]:
        watchability = item.get("watchability") or {}
        providers = watchability.get("items", []) if isinstance(watchability, dict) else []
        result: list[tuple[str, str]] = []
        for p in providers:
            if not isinstance(p, dict) or not p.get("url"):
                continue
            result.append((str(p.get("name") or "Площадка"), str(p["url"])))
        return result


kp = PoiskKino(KINOPOISK_TOKEN)
router = Router()


class SearchState(StatesGroup):
    query = State()


async def db_init() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS favorites (
                user_id INTEGER NOT NULL,
                media_type TEXT NOT NULL,
                movie_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, media_type, movie_id)
            );
            """
        )
        await db.commit()


async def remember(message: Message) -> None:
    if not message.from_user:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users(user_id, username, first_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
              username=excluded.username,
              first_name=excluded.first_name,
              last_seen_at=CURRENT_TIMESTAMP
            """,
            (message.from_user.id, message.from_user.username, message.from_user.first_name),
        )
        await db.commit()


async def is_favorite(user_id: int, media_type: str, movie_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM favorites WHERE user_id=? AND media_type=? AND movie_id=?",
            (user_id, media_type, movie_id),
        )
        return await cur.fetchone() is not None


async def add_favorite(user_id: int, media_type: str, movie_id: int, title: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO favorites(user_id,media_type,movie_id,title) VALUES (?,?,?,?)",
            (user_id, media_type, movie_id, title),
        )
        await db.commit()


async def delete_favorite(user_id: int, media_type: str, movie_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM favorites WHERE user_id=? AND media_type=? AND movie_id=?",
            (user_id, media_type, movie_id),
        )
        await db.commit()


async def favorites(user_id: int) -> list[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT media_type,movie_id,title FROM favorites WHERE user_id=? ORDER BY created_at DESC LIMIT 30",
            (user_id,),
        )
        return [dict(x) for x in await cur.fetchall()]


async def stats() -> tuple[int, int]:
    async with aiosqlite.connect(DB_PATH) as db:
        users = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
        favs = (await (await db.execute("SELECT COUNT(*) FROM favorites")).fetchone())[0]
        return int(users), int(favs)


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔎 Найти фильм или сериал")],
            [KeyboardButton(text="🔥 Популярное"), KeyboardButton(text="🎲 Что посмотреть?")],
            [KeyboardButton(text="🎬 Подборки"), KeyboardButton(text="⭐ Избранное")],
            [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="ℹ️ О VKino")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Что будем смотреть? 🍿",
    )


def results_keyboard(items: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows = []
    for item in items:
        title = kp.title(item)
        year = item.get("year") or "—"
        icon = "📺" if item.get("media_type") == "series" else "🎬"
        rows.append([InlineKeyboardButton(text=f"{icon} {title} ({year})", callback_data=f"open:{item['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def card_keyboard(item: dict[str, Any], favorite: bool) -> InlineKeyboardMarkup:
    media_type = item.get("media_type", "movie")
    movie_id = int(item["id"])
    rows: list[list[InlineKeyboardButton]] = []
    trailer = kp.trailer(item)
    if trailer:
        rows.append([InlineKeyboardButton(text="🎞 Трейлер", url=trailer)])
    rows.append([InlineKeyboardButton(text="▶️ Где смотреть", callback_data=f"watch:{movie_id}")])
    rows.append([
        InlineKeyboardButton(
            text="💔 Убрать из избранного" if favorite else "⭐ В избранное",
            callback_data=f"{'favdel' if favorite else 'favadd'}:{media_type}:{movie_id}",
        )
    ])
    rows.append([InlineKeyboardButton(text="🔎 Найти ещё", callback_data="search_again")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_card(item: dict[str, Any]) -> str:
    rating = item.get("rating") or {}
    genres = ", ".join(x.get("name", "") for x in (item.get("genres") or [])[:4] if x.get("name"))
    countries = ", ".join(x.get("name", "") for x in (item.get("countries") or [])[:3] if x.get("name"))
    description = (item.get("description") or item.get("shortDescription") or "Описание пока отсутствует.").strip()
    if len(description) > 850:
        description = description[:847].rstrip() + "…"
    kind = "Сериал" if item.get("media_type") == "series" else "Фильм"
    lines = [
        f"<b>{escape(kp.title(item))}</b>",
        f"{kind} • {item.get('year') or '—'}",
        f"⭐ Кинопоиск: <b>{float(rating.get('kp')):.1f}</b>/10" if rating.get("kp") else "⭐ Кинопоиск: —",
    ]
    if rating.get("imdb"):
        lines.append(f"⭐ IMDb: <b>{float(rating['imdb']):.1f}</b>/10")
    if genres:
        lines.append(f"🎭 {escape(genres)}")
    if countries:
        lines.append(f"🌍 {escape(countries)}")
    if item.get("ageRating"):
        lines.append(f"🔞 {item['ageRating']}+")
    lines += ["", escape(description)]
    return "\n".join(lines)


async def send_card(message: Message, item: dict[str, Any], user_id: int) -> None:
    favorite = await is_favorite(user_id, item.get("media_type", "movie"), int(item["id"]))
    poster = kp.poster(item)
    if poster:
        try:
            await message.answer_photo(poster, caption=format_card(item), reply_markup=card_keyboard(item, favorite))
            return
        except Exception:
            pass
    await message.answer(format_card(item), reply_markup=card_keyboard(item, favorite))


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await remember(message)
    name = escape(message.from_user.first_name if message.from_user else "друг")
    await message.answer(
        f"🎬 <b>Привет, {name}! Это VKino.</b>\n\n"
        "🔎 Поиск фильмов и сериалов\n"
        "⭐ Рейтинги Кинопоиска и IMDb\n"
        "🎞 Трейлеры и описания\n"
        "🍿 Подборки и рекомендации",
        reply_markup=main_menu(),
    )


@router.message(F.text == "🔎 Найти фильм или сериал")
async def ask_search(message: Message, state: FSMContext) -> None:
    await remember(message)
    await state.set_state(SearchState.query)
    await message.answer("Напиши название фильма или сериала 👇")


@router.callback_query(F.data == "search_again")
async def search_again(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SearchState.query)
    await callback.answer()
    if callback.message:
        await callback.message.answer("Напиши название 👇")


@router.message(SearchState.query, F.text)
async def do_search(message: Message, state: FSMContext) -> None:
    query = (message.text or "").strip()
    if len(query) < 2:
        await message.answer("Напиши хотя бы 2 символа.")
        return
    try:
        items = await kp.search(query)
    except Exception as exc:
        logging.exception("Search failed: %s", exc)
        await message.answer("Не удалось связаться с каталогом. Попробуй позже.")
        return
    if not items:
        await message.answer("Ничего не нашёл 😕 Попробуй другое название.")
        return
    await state.clear()
    await message.answer("Вот что нашёл:", reply_markup=results_keyboard(items))


@router.callback_query(F.data.startswith("open:"))
async def open_movie(callback: CallbackQuery) -> None:
    await callback.answer()
    try:
        item = await kp.details(int(callback.data.split(":", 1)[1]))
        if callback.message:
            await send_card(callback.message, item, callback.from_user.id)
    except Exception as exc:
        logging.exception("Open movie failed: %s", exc)
        if callback.message:
            await callback.message.answer("Не удалось открыть карточку.")


@router.callback_query(F.data.startswith("favadd:"))
async def fav_add(callback: CallbackQuery) -> None:
    _, media_type, raw_id = callback.data.split(":", 2)
    item = await kp.details(int(raw_id))
    await add_favorite(callback.from_user.id, media_type, int(raw_id), kp.title(item))
    await callback.answer("Добавлено ⭐")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=card_keyboard(item, True))


@router.callback_query(F.data.startswith("favdel:"))
async def fav_del(callback: CallbackQuery) -> None:
    _, media_type, raw_id = callback.data.split(":", 2)
    await delete_favorite(callback.from_user.id, media_type, int(raw_id))
    await callback.answer("Удалено")
    if callback.message:
        item = await kp.details(int(raw_id))
        await callback.message.edit_reply_markup(reply_markup=card_keyboard(item, False))


@router.callback_query(F.data.startswith("watch:"))
async def watch(callback: CallbackQuery) -> None:
    await callback.answer()
    if not callback.message:
        return
    try:
        item = await kp.details(int(callback.data.split(":", 1)[1]))
        links = kp.watch_links(item)
    except Exception:
        links = []
    if not links:
        await callback.message.answer("Для этого фильма площадки просмотра пока не указаны.")
        return
    text = "▶️ <b>Где посмотреть:</b>\n" + "\n".join(
        f'• <a href="{escape(url)}">{escape(name)}</a>' for name, url in links[:8]
    )
    await callback.message.answer(text, disable_web_page_preview=True)


@router.message(F.text == "🔥 Популярное")
async def popular(message: Message) -> None:
    await remember(message)
    try:
        items = await kp.popular()
        await message.answer("🔥 Популярное сейчас:", reply_markup=results_keyboard(items))
    except Exception:
        await message.answer("Не удалось загрузить популярное.")


@router.message(F.text == "🎲 Что посмотреть?")
async def random_movie(message: Message) -> None:
    await remember(message)
    try:
        item = await kp.random_pick()
        if item:
            await send_card(message, item, message.from_user.id)
        else:
            await message.answer("Не получилось выбрать фильм. Попробуй ещё раз.")
    except Exception:
        await message.answer("Не получилось выбрать фильм. Попробуй ещё раз.")


@router.message(F.text == "🎬 Подборки")
async def collections(message: Message) -> None:
    await remember(message)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Популярные фильмы", callback_data="collection:movies")],
        [InlineKeyboardButton(text="📺 Популярные сериалы", callback_data="collection:series")],
    ])
    await message.answer("Что показать?", reply_markup=kb)


@router.callback_query(F.data.startswith("collection:"))
async def collection(callback: CallbackQuery) -> None:
    await callback.answer()
    if not callback.message:
        return
    kind = callback.data.split(":", 1)[1]
    try:
        items = await kp.popular(series=(kind == "series"))
        await callback.message.answer(
            "📺 Популярные сериалы" if kind == "series" else "🎬 Популярные фильмы",
            reply_markup=results_keyboard(items),
        )
    except Exception:
        await callback.message.answer("Не удалось загрузить подборку.")


@router.message(F.text == "⭐ Избранное")
async def favorites_menu(message: Message) -> None:
    await remember(message)
    rows = await favorites(message.from_user.id)
    if not rows:
        await message.answer("В избранном пока пусто ⭐")
        return
    items = [
        {"id": x["movie_id"], "name": x["title"], "year": "—", "media_type": x["media_type"]}
        for x in rows
    ]
    await message.answer("⭐ Твоё избранное:", reply_markup=results_keyboard(items))


@router.message(F.text == "👤 Профиль")
async def profile(message: Message) -> None:
    await remember(message)
    count = len(await favorites(message.from_user.id))
    await message.answer(f"👤 <b>Профиль VKino</b>\n\n🆔 ID: <code>{message.from_user.id}</code>\n⭐ В избранном: {count}")


@router.message(F.text == "ℹ️ О VKino")
async def about(message: Message) -> None:
    await remember(message)
    await message.answer(
        "🎬 <b>VKino</b> — кино-гид в Telegram.\n\n"
        "Поиск, рейтинги, русские описания, трейлеры, подборки и избранное.\n"
        "Метаданные: ПоискКино API."
    )


@router.message(Command("admin"))
async def admin(message: Message) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    users, favs = await stats()
    await message.answer(
        f"🛠 <b>VKino — админка</b>\n\n👥 Пользователей: {users}\n⭐ Избранное: {favs}\n\n"
        "<code>/broadcast текст</code> — рассылка"
    )


@router.message(Command("broadcast"))
async def broadcast(message: Message) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    text = (message.text or "").partition(" ")[2].strip()
    if not text:
        await message.answer("Использование: /broadcast текст")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute("SELECT user_id FROM users")).fetchall()
    ok = failed = 0
    for (user_id,) in rows:
        try:
            await message.bot.send_message(int(user_id), text)
            ok += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await message.answer(f"Рассылка завершена. ✅ {ok}  ❌ {failed}")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    await db_init()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("VKino started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

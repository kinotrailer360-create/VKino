"""Поиск по открытым Telegram-каналам — материал для фактов о фильме.

У открытого канала есть веб-версия t.me/s/<канал>, и в ней работает поиск: t.me/s/<канал>?q=<слова>.
Бот читает найденные посты (без входа в Telegram); ИИ берёт из них факты и пересказывает своими словами.
Закрытые каналы (без @имени) так не читаются.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import aiohttp

log = logging.getLogger("shorts")

URL = "https://t.me/s/{channel}"
DEFAULT_CHANNELS = [
    "KinoMir360", "harrypotter_hbo", "kino_filmach", "MarvelGeeksOffical2028", "QewbsNews", "littler0ckone",
    "Cronenberg1664", "Qewbite", "PokvmOfficial", "pluggedin", "FRESHTrailersNEWS", "otherworlds_tg",
    "themovieblogteam", "trailerparkcinema", "POKDC", "scarylandteam", "rhsnews", "cherevatstreams",
    "DetectiveComics_Stuff", "marvelgolik", "games_sportsru", "cutterpool",
]
MAX_CHANNELS = 40
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; KinoMir360-bot/1.0)", "Accept-Language": "ru,en;q=0.8"}
POSTS_PER_CHANNEL = 4
POST_MAX = 700


def channel_name(raw: str) -> str | None:
    """@name, t.me/name, https://t.me/s/name/123 → name; иначе None."""
    m = re.fullmatch(r"\s*(?:https?://)?(?:t\.me/(?:s/)?|@)?([A-Za-z][A-Za-z0-9_]{3,31})(?:/\d+)?/?\s*", raw)
    return m.group(1) if m and m.group(1).lower() not in ("joinchat", "addlist", "s") else None


def split_channels(text: str) -> tuple[list[str], list[str]]:
    """Каналы из сообщения автора: ссылки через пробел, с новой строки или слитно
    («https://t.me/ahttps://t.me/b»). Возвращает (каналы без повторов, непонятые куски)."""
    pieces = [x for x in re.split(r"[\s,;]+|(?=https?://)|(?<=\w)(?=@)", text) if x.strip()]
    good = [c for c in (channel_name(x) for x in pieces) if c]
    return list(dict.fromkeys(good)), [x for x in pieces if not channel_name(x)]


class _Posts(HTMLParser):
    """Посты из веб-версии канала: [{"url", "text", "time" (unix, 0 — нет), "image", "video"}] по порядку."""

    def __init__(self) -> None:
        super().__init__()
        self.all: list[dict[str, Any]] = []
        self.depth = 0  # >0 — внутри текста поста (считаем вложенные div)
        self.buf: list[str] = []

    @property
    def posts(self) -> list[dict[str, Any]]:
        return [p for p in self.all if p["text"]]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        cls = a.get("class") or ""
        if a.get("data-post") and "tgme_widget_message" in cls:
            self.all.append({"url": f"https://t.me/{a['data-post']}", "text": "", "time": 0.0, "image": None,
                             "video": None})
        cur = self.all[-1] if self.all else None
        if tag == "div":
            if self.depth:
                self.depth += 1
            elif "tgme_widget_message_text" in cls and cur is not None:
                self.depth, self.buf = 1, []
        elif tag == "br" and self.depth:
            self.buf.append("\n")
        elif tag == "time" and cur is not None and a.get("datetime") and not cur["time"]:
            try:
                cur["time"] = datetime.fromisoformat(str(a["datetime"])).timestamp()
            except ValueError:
                pass
        elif tag == "a" and "tgme_widget_message_photo_wrap" in cls and cur is not None and not cur["image"]:
            m = re.search(r"background-image:url\(['\"]?([^'\")]+)", a.get("style") or "")
            cur["image"] = m.group(1) if m else None
        elif tag == "video" and cur is not None and not cur["video"] and str(a.get("src") or "").startswith("http"):
            cur["video"] = a["src"]

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self.depth:
            self.depth -= 1
            if not self.depth and self.all:
                self.all[-1]["text"] = re.sub(r"[ \t]+", " ", unescape("".join(self.buf))).strip()

    def handle_data(self, data: str) -> None:
        if self.depth:
            self.buf.append(data)


async def _fetch(session: aiohttp.ClientSession, channel: str, query: str = "") -> list[dict[str, Any]]:
    async with session.get(URL.format(channel=channel), params={"q": query} if query else None) as resp:
        resp.raise_for_status()
        if "/s/" not in str(resp.url):  # закрытый канал или нет такого — t.me уводит на обычную страницу
            raise RuntimeError("канал закрытый или не найден")
        parser = _Posts()
        parser.feed(await resp.text())
    return parser.posts


async def latest(channels: list[str], hours: float = 48, per_channel: int = 8) -> list[dict[str, Any]]:
    """Свежие посты каналов (для новостей): [{"channel", "url", "text", "time", "image"}], новые первыми."""
    since = time.time() - hours * 3600
    sem = asyncio.Semaphore(4)

    async def one(session: aiohttp.ClientSession, channel: str) -> list[dict[str, Any]]:
        async with sem:
            try:
                posts = await _fetch(session, channel)
            except Exception as exc:  # noqa: BLE001
                log.info("shorts: tg channel %s read failed: %s", channel, exc)
                return []
        fresh = [p for p in reversed(posts) if p["time"] >= since and len(p["text"]) >= 60]
        return [{**p, "channel": channel, "text": p["text"][:POST_MAX]} for p in fresh[:per_channel]]

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), headers=HEADERS) as session:
        results = await asyncio.gather(*(one(session, c) for c in channels))
    return sorted((p for chunk in results for p in chunk), key=lambda p: -p["time"])


def _mentions(text: str, names: list[str]) -> bool:
    low = text.lower().replace("ё", "е")
    return any(re.search(rf"(?<!\w){re.escape(n.lower().replace('ё', 'е'))}(?!\w)", low) for n in names if len(n) >= 2)


async def search(channels: list[str], names: list[str], per_channel: int = POSTS_PER_CHANNEL) -> list[dict[str, Any]]:
    """Посты, где упомянут фильм (по русскому или английскому названию): [{"channel", "url", "text"}]."""
    names = [n.strip() for n in names if n and n.strip()]
    if not channels or not names:
        return []
    sem = asyncio.Semaphore(4)
    timeout = aiohttp.ClientTimeout(total=20)

    async def one(session: aiohttp.ClientSession, channel: str) -> list[dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for name in names:
            async with sem:
                try:
                    posts = await _fetch(session, channel, name)
                except Exception as exc:  # noqa: BLE001 — один канал не мешает остальным
                    log.info("shorts: tg channel %s search %r failed: %s", channel, name, exc)
                    return list(found.values())
            for post in reversed(posts):  # свежие — первыми
                url, text = post["url"], post["text"]
                if len(text) >= 60 and _mentions(text, names) and url not in found:
                    found[url] = {"channel": channel, "url": url, "text": text[:POST_MAX],
                                  "image": post["image"], "video": post["video"]}
            if len(found) >= per_channel:
                break
        return list(found.values())[:per_channel]

    async with aiohttp.ClientSession(timeout=timeout, headers=HEADERS) as session:
        results = await asyncio.gather(*(one(session, c) for c in channels))
    return [p for chunk in results for p in chunk]


def as_text(posts: list[dict[str, Any]], limit: int = 9000) -> str:
    out, size = [], 0
    for p in posts:
        block = f"[t.me/{p['channel']}]\n{p['text']}"
        if size + len(block) > limit:
            break
        out.append(block)
        size += len(block)
    return "\n\n".join(out)


async def check(channels: list[str]) -> list[tuple[str, int | str]]:
    """Для кнопки в боте: читается ли канал — (канал, число постов на странице | причина ошибки)."""
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout, headers=HEADERS) as session:
        async def one(channel: str) -> tuple[str, int | str]:
            try:
                return channel, len(await _fetch(session, channel))
            except Exception as exc:  # noqa: BLE001
                return channel, str(exc)[:80] or type(exc).__name__
        return list(await asyncio.gather(*(one(c) for c in channels)))


MEDIA_MAX = 25 * 1024 * 1024  # видео больше — не качаем (долго, а нужно 5 секунд)


async def download_media(posts: list[dict[str, Any]], work: Path, limit: int = 8,
                         prefix: str = "tg") -> list[Path]:
    """Фото и видео из постов в папку ролика (видео поста важнее его обложки). Битые и огромные пропускаем."""
    urls: list[tuple[str, str]] = []
    for p in posts:
        if p.get("video"):
            urls.append((p["video"], ".mp4"))
        elif p.get("image"):
            urls.append((p["image"], ".jpg"))
    out: list[Path] = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60), headers=HEADERS) as session:
        for url, ext in urls:
            if len(out) >= limit:
                break
            try:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    if int(resp.headers.get("Content-Length") or 0) > MEDIA_MAX:
                        continue
                    chunks, size = [], 0
                    async for chunk in resp.content.iter_chunked(1 << 16):  # read(n) отдаёт лишь доступный кусок
                        chunks.append(chunk)
                        size += len(chunk)
                        if size > MEDIA_MAX:
                            break
                    data = b"".join(chunks)
                if len(data) > MEDIA_MAX or len(data) < 2000:
                    continue
                path = work / f"{prefix}{len(out)}{ext}"
                path.write_bytes(data)
                out.append(path)
            except Exception as exc:  # noqa: BLE001
                log.info("shorts: tg media %s skipped: %s", url[:80], exc)
    return out

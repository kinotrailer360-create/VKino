"""Новости кино из RSS-лент: свежие заголовки, короткий лид, картинка и источник."""

from __future__ import annotations

import asyncio
import calendar
import hashlib
import html
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import aiohttp
import feedparser

log = logging.getLogger("shorts")

# Google Новости работают стабильно; ленты киносайтов — если доступны с сервера.
# Список можно поменять в боте: /feeds add|del|default
DEFAULT_FEEDS = [
    "https://news.google.com/rss/search?q=%D0%BD%D0%BE%D0%B2%D0%BE%D1%81%D1%82%D0%B8+%D0%BA%D0%B8%D0%BD%D0%BE"
    "+when:2d&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=%D1%81%D0%B5%D1%80%D0%B8%D0%B0%D0%BB+%D0%BF%D1%80%D0%B5%D0%BC%D1%8C%D0%B5%D1%80%D0%B0"
    "+when:2d&hl=ru&gl=RU&ceid=RU:ru",
    "https://kg-portal.ru/rss/news.rss",
    "https://www.film.ru/rss.xml",
    "https://www.kinonews.ru/rss/",
]
MAX_AGE_HOURS = 48
LEAD_MAX = 150  # длиннее — обрезаем по предложению
USER_AGENT = "Mozilla/5.0 (compatible; KinoMir360-bot/1.0; +https://t.me/KinoMir360)"


@dataclass
class NewsItem:
    title: str
    lead: str
    source: str
    link: str
    published: float
    image: str | None = None
    film_query: str | None = None  # «Название» из заголовка — ищем фильм
    film_name: str | None = None  # название найденного фильма (для тегов)

    @property
    def uid(self) -> int:
        """Стабильный отрицательный id для таблицы истории (фильмы там положительные)."""
        return -int(hashlib.sha1(self.link.encode()).hexdigest()[:12], 16)


def _text(raw: Any) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(raw or "")))
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _lead(summary: str, title: str) -> str:
    """Первые предложения описания до LEAD_MAX; пусто, если это просто повтор заголовка."""
    text = _text(summary)
    if not text or text.lower().startswith(title.lower()[:40]):
        return ""
    out = ""
    for sentence in re.split(r"(?<=[.!?…])\s+", text):
        if len(out) + len(sentence) + 1 > LEAD_MAX:
            break
        out = f"{out} {sentence}".strip()
    return out if len(out) >= 25 else ""  # огрызки вроде «x» или «Читать далее» не озвучиваем


def _image(entry: Any) -> str | None:
    for media in (entry.get("media_content") or []) + (entry.get("media_thumbnail") or []):
        if media.get("url") and "image" in str(media.get("type") or "image"):
            return str(media["url"])
    for link in entry.get("links") or []:
        if link.get("rel") == "enclosure" and str(link.get("type") or "").startswith("image") and link.get("href"):
            return str(link["href"])
    found = re.search(r'<img[^>]+src="([^"]+)"', str(entry.get("summary") or ""))
    return found.group(1) if found else None


def parse_feed(raw: bytes, now: float | None = None) -> list[NewsItem]:
    now = now or time.time()
    feed = feedparser.parse(raw)
    default_source = _text((feed.get("feed") or {}).get("title")) or "RSS"
    items = []
    for entry in feed.get("entries") or []:
        title = _text(entry.get("title"))
        link = str(entry.get("link") or "")
        stamp = entry.get("published_parsed") or entry.get("updated_parsed")
        published = calendar.timegm(stamp) if stamp else now
        if not title or not link or now - published > MAX_AGE_HOURS * 3600:
            continue
        source = _text((entry.get("source") or {}).get("title")) or default_source
        # Google Новости: «Заголовок - Источник»
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].strip()
        quoted = re.search(r"«([^»]{2,60})»", title)
        items.append(NewsItem(title=title, lead=_lead(entry.get("summary", ""), title), source=source,
                              link=link, published=published, image=_image(entry),
                              film_query=quoted.group(1) if quoted else None))
    return items


async def fetch_feed(session: aiohttp.ClientSession, url: str) -> list[NewsItem]:
    async with session.get(url, headers={"User-Agent": USER_AGENT}) as resp:
        resp.raise_for_status()
        raw = await resp.read()
    return await asyncio.to_thread(parse_feed, raw)


async def check_feeds(feeds: list[str]) -> list[tuple[str, int | str]]:
    """Для /feeds: сколько свежих новостей даёт каждая лента (или текст ошибки)."""
    out: list[tuple[str, int | str]] = []
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for url in feeds:
            try:
                out.append((url, len(await fetch_feed(session, url))))
            except Exception as exc:  # noqa: BLE001
                out.append((url, f"{type(exc).__name__}: {str(exc)[:80]}"))
    return out


def _norm(title: str) -> str:
    return re.sub(r"[^\wа-яё]+", " ", title.lower()).strip()


async def latest_news(feeds: list[str], exclude: set[int]) -> list[NewsItem]:
    """Свежие новости со всех лент без повторов (по ссылке и по похожим заголовкам)."""
    timeout = aiohttp.ClientTimeout(total=20)
    items: list[NewsItem] = []
    async with aiohttp.ClientSession(timeout=timeout) as session:
        results = await asyncio.gather(*(fetch_feed(session, u) for u in feeds), return_exceptions=True)
    for url, res in zip(feeds, results):
        if isinstance(res, Exception):
            log.info("shorts: feed %s failed: %s", url, res)
            continue
        items += res
    items.sort(key=lambda n: n.published, reverse=True)
    unique, seen = [], []
    for item in items:
        words = set(_norm(item.title).split())
        if item.uid in exclude or not words:
            continue
        # один и тот же сюжет у разных изданий — пропускаем
        if any(len(words & other) / len(words | other) > 0.5 for other in seen):
            continue
        seen.append(words)
        unique.append(item)
    return unique

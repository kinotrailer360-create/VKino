"""Быстрые зарубежные источники новостей кино: Reddit, Deadline, Variety, THR, инсайдеры и X (через RSS).

Здесь новости и слухи появляются на часы раньше, чем в русскоязычных Telegram-каналах. Посты отдаются в
том же виде, что и посты Telegram-каналов (shorts.tgsearch.latest), — их дальше пишет тот же ИИ.

X (Twitter) бесплатного API не даёт. Аккаунты X читаются через RSS: либо свой сервер Nitter/RSSHub
(переменная NITTER_URL, тогда в списке можно писать x:ИмяАккаунта), либо готовая RSS-ссылка (например,
rss.app) — её можно просто добавить в список источников.
"""

from __future__ import annotations

import asyncio
import calendar
import html
import logging
import os
import re
import time
from typing import Any
from urllib.parse import urlparse

import aiohttp
import feedparser

log = logging.getLogger("shorts")

DEFAULT_FEEDS = [
    "https://www.reddit.com/r/movies/search.rss?q=flair%3ANews&restrict_sr=1&sort=new",
    "https://www.reddit.com/r/boxoffice/new/.rss?limit=25",
    "https://deadline.com/feed/",
    "https://variety.com/v/film/feed/",
    "https://www.hollywoodreporter.com/c/movies/movie-news/feed/",
    "https://www.theinsneider.com/feed",
    "https://www.worldofreel.com/blog?format=rss",
    "https://bloody-disgusting.com/feed/",
    "x:DiscussingFilm",
    "x:DanielRPK",
    "x:MyTimeToShineH",
]
HOURS = 12
PER_FEED = 6
TEXT_MAX = 900
REDDIT_SKIP = re.compile(r"\b(?:thread|discussion|megathread|predictions?|projections?|update)\b|\?\s*$", re.I)
REDDIT_EVERY = 600  # Reddit без входа режет частые запросы (429): одну ленту — не чаще раза в 10 минут
REDDIT_STALE = 6 * 3600  # при отказе берём последнюю сохранённую ленту, если ей меньше 6 часов
_reddit_cache: dict[str, tuple[float, bytes]] = {}
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; KinoMir360-bot/1.0; +https://t.me/KinoMir360)"}


def nitter() -> str:
    return os.getenv("NITTER_URL", "").strip().rstrip("/")


def rsshub() -> str:
    """Свой RSSHub (в docker-compose — сервис rsshub, включается COMPOSE_PROFILES=x и TWITTER_AUTH_TOKEN)."""
    return os.getenv("RSSHUB_URL", "").strip().rstrip("/")


def feed_url(entry: str) -> str | None:
    """Адрес ленты: x:Имя → RSS аккаунта через Nitter/RSSHub (если задан NITTER_URL), иначе как есть."""
    entry = entry.strip()
    m = re.fullmatch(r"(?:x:|@|https?://(?:www\.)?(?:x|twitter)\.com/)@?(\w{1,30})/?", entry, re.I)
    if m:
        if rsshub():
            return f"{rsshub()}/twitter/user/{m.group(1)}"
        return f"{nitter()}/{m.group(1)}/rss" if nitter() else None
    if "/r/boxoffice/search.rss" in entry:  # в RSS Reddit нет флеров — поиск по флеру пуст, берём свежие посты
        return "https://www.reddit.com/r/boxoffice/new/.rss?limit=25"
    return entry if entry.startswith("http") else None


def label(entry: str) -> str:
    """Короткое имя источника для ИИ и автора: r/movies, Deadline, X @DanielRPK."""
    m = re.fullmatch(r"(?:x:|@|https?://(?:www\.)?(?:x|twitter)\.com/)@?(\w{1,30})/?", entry.strip(), re.I)
    if m:
        return f"X @{m.group(1)}"
    url = urlparse(entry)
    sub = re.search(r"/r/(\w+)", url.path)
    if "reddit.com" in url.netloc and sub:
        return f"Reddit r/{sub.group(1)}"
    host = url.netloc.removeprefix("www.")
    if host == "rss.app":  # имя ленты rss.app — из самой ленты (аккаунт X и т. п.)
        return ""
    if "/twitter/user/" in url.path:
        return f"X @{url.path.rstrip('/').rsplit('/', 1)[1]}"
    if "nitter" in host or (nitter() and entry.startswith(nitter())):  # готовая ссылка Nitter: /Имя/rss
        return f"X @{url.path.strip('/').split('/')[0]}"
    return host.split(".")[0].capitalize() if host else entry[:30]


def _text(raw: Any) -> str:
    text = str(raw or "")
    text = re.sub(r"submitted by.*", " ", text, flags=re.S | re.I)  # хвост Reddit «submitted by /u/… [link]»
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _image(entry: Any) -> str | None:
    for media in (entry.get("media_content") or []) + (entry.get("media_thumbnail") or []):
        url = str(media.get("url") or "")
        if url and "image" in str(media.get("type") or "image"):
            return html.unescape(url)
    for link in entry.get("links") or []:
        if link.get("rel") == "enclosure" and str(link.get("type") or "").startswith("image") and link.get("href"):
            return str(link["href"])
    found = re.search(r'<img[^>]+src="([^"]+)"', str(entry.get("summary") or entry.get("content") or ""))
    return html.unescape(found.group(1)) if found else None


def _article(entry: Any) -> str:
    """Ссылка на саму статью у поста Reddit ([link]) — ИИ видит, откуда новость."""
    found = re.search(r'<a href="([^"]+)">\[link\]</a>', str(entry.get("summary") or ""))
    return html.unescape(found.group(1)) if found and "reddit.com" not in found.group(1) else ""


def feed_name(feed: Any) -> str:
    """Имя ленты по её заголовку: «X @DiscussingFilm» для аккаунтов X, иначе сам заголовок."""
    title = _text((feed.get("feed") or {}).get("title"))
    handle = re.search(r"@(\w{1,30})", title)
    if handle or re.search(r"\b(?:twitter|x\.com)\b|/ X$", title, re.I):
        return f"X @{handle.group(1)}" if handle else f"X {title[:40]}"
    return title[:40] or "RSS"


def parse(raw: bytes, source: str, since: float, per_feed: int = PER_FEED) -> list[dict[str, Any]]:
    feed = feedparser.parse(raw)
    source = source or feed_name(feed)
    out = []
    for entry in feed.get("entries") or []:
        title = _text(entry.get("title"))
        link = str(entry.get("link") or "")
        stamp = entry.get("published_parsed") or entry.get("updated_parsed")
        published = calendar.timegm(stamp) if stamp else 0.0
        if not title or not link or published < since:
            continue
        if source.startswith("Reddit") and REDDIT_SKIP.search(title):
            continue
        body = ""
        for part in entry.get("content") or []:
            body = _text(part.get("value"))
            if body:
                break
        body = body or _text(entry.get("summary"))
        if body.lower().startswith(title.lower()[:40]):
            body = body[len(title):].strip()
        article = _article(entry)
        text = (f"{title}\n{body}".strip() + (f"\nИсточник статьи: {urlparse(article).netloc}" if article else ""))
        image = _image(entry)
        out.append({"channel": source, "label": source, "url": link, "text": text[:TEXT_MAX], "time": published,
                    "image": image, "video": None, "media": [{"image": image}] if image else [], "lang": "en"})
        if len(out) >= per_feed:
            break
    return out


_reddit: asyncio.Lock | None = None


async def _download(session: aiohttp.ClientSession, url: str) -> bytes:
    async with session.get(url) as resp:
        resp.raise_for_status()
        return await resp.read()


async def _fetch(session: aiohttp.ClientSession, entry: str, since: float, per_feed: int) -> list[dict[str, Any]]:
    global _reddit
    url = feed_url(entry)
    if not url:
        return []
    if "reddit.com" in url:  # Reddit режет частые запросы (429): по одному, с паузой, не чаще раза в 10 минут
        _reddit = _reddit or asyncio.Lock()
        async with _reddit:
            cached = _reddit_cache.get(url)
            if cached and time.time() - cached[0] < REDDIT_EVERY:
                raw = cached[1]
            else:
                await asyncio.sleep(3)
                try:
                    raw = await _download(session, url)
                    _reddit_cache[url] = (time.time(), raw)
                except aiohttp.ClientResponseError as exc:
                    if exc.status != 429 or not cached or time.time() - cached[0] > REDDIT_STALE:
                        raise
                    log.info("shorts: reddit 429, using cached %s", url)
                    raw = cached[1]
    else:
        raw = await _download(session, url)
    return await asyncio.to_thread(parse, raw, label(entry), since, per_feed)


async def latest(feeds: list[str], hours: float = HOURS, per_feed: int = PER_FEED) -> list[dict[str, Any]]:
    """Свежие новости всех источников в виде постов каналов, новые первыми."""
    since = time.time() - hours * 3600
    sem = asyncio.Semaphore(4)

    async def one(session: aiohttp.ClientSession, entry: str) -> list[dict[str, Any]]:
        async with sem:
            try:
                return await _fetch(session, entry, since, per_feed)
            except Exception as exc:  # noqa: BLE001
                log.info("shorts: fast feed %s failed: %s", entry, exc)
                return []

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), headers=HEADERS) as session:
        results = await asyncio.gather(*(one(session, f) for f in feeds))
    return sorted((p for chunk in results for p in chunk), key=lambda p: -p["time"])


async def check(feeds: list[str]) -> list[tuple[str, int | str, str]]:
    """Для /fast: (источник, сколько новостей за сутки или почему не читается, имя). Все сразу; Reddit —
    по очереди (частые запросы он режет)."""
    since = time.time() - 24 * 3600

    async def one(session: aiohttp.ClientSession, entry: str) -> tuple[str, int | str, str]:
        name = label(entry)
        if not feed_url(entry):
            return entry, ("X не подключён: нужен RSSHub (COMPOSE_PROFILES=x и TWITTER_AUTH_TOKEN в .env) "
                           "или RSS-ссылка rss.app"), name
        try:
            posts = await _fetch(session, entry, 0, 200)
            fresh = [p for p in posts if p["time"] >= since]
            return entry, len(fresh), name or (posts[0]["label"] if posts else "RSS")
        except Exception as exc:  # noqa: BLE001
            hint = (" — RSSHub не запущен или нет TWITTER_AUTH_TOKEN" if rsshub() and
                    str(feed_url(entry)).startswith(rsshub()) else "")
            return entry, f"{type(exc).__name__}: {str(exc)[:80]}{hint}", name or "RSS"

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40), headers=HEADERS) as session:
        return list(await asyncio.gather(*(one(session, e) for e in feeds)))

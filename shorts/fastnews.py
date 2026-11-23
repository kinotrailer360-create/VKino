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
from urllib.parse import quote_plus, urljoin, urlparse

import aiohttp
import feedparser

from . import netsafe

log = logging.getLogger("shorts")

LEGACY_REDDIT_FEEDS = (
    "https://www.reddit.com/r/movies/search.rss?q=flair%3ANews&restrict_sr=1&sort=new",
    "https://www.reddit.com/r/boxoffice/new/.rss?limit=25",
    "https://www.reddit.com/r/boxoffice/search.rss?q=flair%3A%22Industry+News%22&restrict_sr=1&",
)
REDDIT_COMMUNITIES = (
    "movies", "television", "boxoffice", "horror", "netflix", "hbo", "DisneyPlus", "Oscars", "Documentaries",
    "marvelstudios", "MarvelStudiosSpoilers", "DCULeaks", "StarWars", "StarWarsLeaks", "gameofthrones",
    "HouseOfTheDragon", "StrangerThings", "TheBoys", "breakingbad", "thewalkingdead", "dune", "GODZILLA",
    "MovieDetails", "MovieSuggestions", "NetflixBestOf", "televisionsuggestions",
)
REDDIT_PACK = "reddit:cinema"
REDDIT_PACK_LABEL = f"Reddit — кино и сериалы ({len(REDDIT_COMMUNITIES)})"
REDDIT_COMMUNITY_NAMES = frozenset(name.casefold() for name in REDDIT_COMMUNITIES)
REDDIT_RSS_URL = "https://api.redlisten.com/r/" + "+".join(REDDIT_COMMUNITIES) + "/new.rss?limit=50"
DEFAULT_FEEDS = [
    REDDIT_PACK,
    "https://deadline.com/feed/",
    "https://variety.com/v/film/feed/",
    "https://www.hollywoodreporter.com/c/movies/movie-news/feed/",
    "https://www.theinsneider.com/feed",
    "https://www.worldofreel.com/blog?format=rss",
    "https://bloody-disgusting.com/feed/",
    "x:DiscussingFilm",
    "x:DanielRPK",
    "x:MyTimeToShineH",
    "https://www.empireonline.com/",
]
HOURS = 12
PER_FEED = 6
TEXT_MAX = 900
REDDIT_SKIP = re.compile(r"\b(?:thread|discussion|megathread|predictions?|projections?|update)\b|\?\s*$", re.I)
REDDIT_EVERY = 3600  # Пауза после успешной загрузки общей ленты.
REDDIT_STALE = 6 * 3600  # при отказе берём последнюю сохранённую ленту, если ей меньше 6 часов
_reddit_cache: dict[str, tuple[float, bytes]] = {}
_reddit_retry: dict[str, float] = {}
REDDIT_BACKOFF = 6 * 3600


class RedditRateLimited(RuntimeError):
    """Temporary upstream throttle, distinct from a broken source."""
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; KinoMir360-bot/1.0; +https://t.me/KinoMir360)"}
FX_FEED_PATH = re.compile(r"/(\w{1,30})/(?:feed|media)(?:\.atom)?\.xml")
FX_FEED_MIRRORS = {"fxtwitter.com": "fixupx.com", "fixupx.com": "fxtwitter.com"}


def with_reddit_pack(feeds: list[str]) -> list[str]:
    """Add the pack once and replace retired URLs while preserving other sources."""
    return list(dict.fromkeys(REDDIT_PACK if is_retired_reddit_feed(entry) else entry
                              for entry in [*feeds, REDDIT_PACK]))


def _reddit_name(url: str) -> str:
    try:
        parts = urlparse(url)
    except ValueError:
        return ""
    host = parts.hostname or ""
    match = re.match(r"/r/([A-Za-z0-9_]+)(?:/|$)", parts.path, re.I)
    if match and parts.scheme in {"https", "http"} and (host == "reddit.com" or host.endswith(".reddit.com")):
        return match[1]
    return ""


def is_retired_reddit_feed(entry: str) -> bool:
    url = entry.strip()
    if url in LEGACY_REDDIT_FEEDS:
        return True
    # The curated pack already includes r/boxoffice. Reddit's old RSS no longer works with any query parameters.
    return _reddit_name(url).casefold() == "boxoffice" and ".rss" in urlparse(url).path.casefold()


def nitter() -> str:
    return os.getenv("NITTER_URL", "").strip().rstrip("/")


def rsshub() -> str:
    """Свой RSSHub: RSSHUB_URL или сервис rsshub из docker-compose — только если он включён (COMPOSE_PROFILES=x
    в .env). Без него аккаунты X не читаются, а /fast подсказывает, как подключить."""
    url = os.getenv("RSSHUB_URL", "").strip().rstrip("/")
    if url:
        return url
    profiles = {p.strip() for p in os.getenv("COMPOSE_PROFILES", "").split(",")}
    return "http://rsshub:1200" if "x" in profiles else ""


def feed_url(entry: str) -> str | None:
    """Адрес ленты: x:Имя → RSS аккаунта через Nitter/RSSHub (если задан NITTER_URL), иначе как есть."""
    entry = entry.strip()
    m = re.fullmatch(r"(?:x:|@|https?://(?:www\.)?(?:x|twitter)\.com/)@?(\w{1,30})/?", entry, re.I)
    if m:
        if rsshub():
            return f"{rsshub()}/twitter/user/{m.group(1)}"
        return f"{nitter()}/{m.group(1)}/rss" if nitter() else None
    if entry == REDDIT_PACK:
        return REDDIT_RSS_URL
    return entry if entry.startswith("http") else None


def parse_add_sources(args: str) -> list[str]:
    """Parse the first /fast add argument and any additional /fast add lines."""
    sources = []
    for index, line in enumerate(args.strip().splitlines()):
        entry = line.strip()
        if not entry:
            continue
        if index:
            command = re.fullmatch(r"/fast(?:@\w+)?\s+add\s+(\S+)", entry)
            if not command:
                return []
            entry = command[1]
        if re.fullmatch(r"(?:x:|@)\w{1,30}", entry):
            sources.append(f"x:{entry[1:]}" if entry.startswith("@") else entry)
            continue
        try:
            url = urlparse(entry)
            if url.scheme in {"https", "http"} and url.hostname and not re.search(r"\s", entry):
                sources.append(entry)
                continue
        except ValueError:
            pass
        return []
    return sources


def label(entry: str) -> str:
    """Короткое имя источника для ИИ и автора: r/movies, Deadline, X @DanielRPK."""
    if entry == REDDIT_PACK:
        return REDDIT_PACK_LABEL
    m = re.fullmatch(r"(?:x:|@|https?://(?:www\.)?(?:x|twitter)\.com/)@?(\w{1,30})/?", entry.strip(), re.I)
    if m:
        return f"X @{m.group(1)}"
    url = urlparse(entry)
    sub = re.search(r"/r/(\w+)", url.path)
    if "reddit.com" in url.netloc and sub:
        return f"Reddit r/{sub.group(1)}"
    host = url.netloc.removeprefix("www.")
    if host in FX_FEED_MIRRORS:
        profile = FX_FEED_PATH.fullmatch(url.path)
        if profile:
            return f"X @{profile[1]}"
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
    """Картинка новости: сначала полноразмерная (media_content), потом превью — но не крошечные значки."""
    for media in (entry.get("media_content") or []) + (entry.get("media_thumbnail") or []):
        url = str(media.get("url") or "")
        width = int(str(media.get("width") or 0) or 0) if str(media.get("width") or "0").isdigit() else 0
        if not url or "image" not in str(media.get("type") or "image"):
            continue
        if (0 < width < 300) or "thumbs.redditmedia.com" in url:  # значок 70–140 px — в посте будет мылом
            continue
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
        entry_source = source
        if source == REDDIT_PACK_LABEL:
            subreddit = _reddit_name(link)
            if subreddit.casefold() not in REDDIT_COMMUNITY_NAMES:
                continue
            entry_source = f"Reddit r/{subreddit}"
        body = ""
        for part in entry.get("content") or []:
            body = _text(part.get("value"))
            if body:
                break
        body = body or _text(entry.get("summary"))
        if body.lower().startswith(title.lower()):  # тело повторяет заголовок — убираем повтор
            body = body[len(title):].strip()
        article = _article(entry)
        text = (f"{title}\n{body}".strip() + (f"\nИсточник статьи: {urlparse(article).netloc}" if article else ""))
        image = _image(entry)
        out.append({"channel": entry_source, "label": entry_source, "url": link, "article_url": article, "title": title,
                    "text": text[:TEXT_MAX], "time": published,
                    "image": image, "video": None, "media": [{"image": image}] if image else [], "lang": "en"})
        if len(out) >= per_feed:
            break
    return out


_reddit: asyncio.Lock | None = None


async def _download(session: aiohttp.ClientSession, url: str) -> bytes:
    trusted = next((t for t in (rsshub(), nitter()) if t and netsafe._same_origin(url, t)), "")
    if trusted:  # свой RSSHub/Nitter во внутренней сети — его задал владелец бота
        return await netsafe.fetch(session, url, 5 * 1024 * 1024, trusted)
    async with netsafe.safe_session(timeout=aiohttp.ClientTimeout(total=20)) as safe:  # чужой адрес — только публичные IP
        return await netsafe.fetch(safe, url, 5 * 1024 * 1024)


async def _download_reddit(session: aiohttp.ClientSession) -> bytes:
    key = os.getenv("REDLISTEN_API_KEY", "").strip()
    if not key:
        raise RuntimeError("Reddit не подключён: нужен REDLISTEN_API_KEY на сервере")
    async with session.get(REDDIT_RSS_URL, headers={"Authorization": f"Bearer {key}"},
                           allow_redirects=False, timeout=aiohttp.ClientTimeout(total=20)) as resp:
        resp.raise_for_status()
        if resp.status != 200:
            raise RuntimeError(f"Неожиданный ответ Redlisten: HTTP {resp.status}")
        raw = bytearray()
        async for chunk in resp.content.iter_chunked(64 * 1024):
            raw.extend(chunk)
            if len(raw) > 5 * 1024 * 1024:
                raise ValueError("Лента Redlisten больше 5 МБ")
        if not is_feed(raw):
            raise ValueError("Redlisten не вернул RSS или Atom")
        return bytes(raw)


_found_feed: dict[str, str] = {}  # адрес сайта → его RSS, найденный на главной (чтобы не искать каждый раз)
EMPIRE_RSS = "https://rss.onebauer.media/api/feed-aggregator?hostname=https://www.empireonline.com"
GOOGLE_NEWS_ON_FORBIDDEN = frozenset({"empireonline.com", "worldofreel.com"})
ALT = re.compile(r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*>', re.I)
OG_IMAGE = re.compile(r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]+content=["\']([^"\']+)'
                      r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:image|twitter:image)',
                      re.I)
OG_EVERY = 4  # у скольких новостей сайта без картинки в ленте искать её на странице статьи


def google_news(host: str) -> str:
    """Запасная лента любого сайта: свежие статьи сайта в Google Новостях (если своей RSS у сайта нет)."""
    return (f"https://news.google.com/rss/search?q={quote_plus('site:' + host + ' when:1d')}"
            "&hl=en-US&gl=US&ceid=US:en")


def is_feed(raw: bytes) -> bool:
    head = raw[:600].lstrip().lower()
    return head.startswith(b"<?xml") or b"<rss" in head or b"<feed" in head


async def _site_feed(session: aiohttp.ClientSession, url: str, raw: bytes) -> tuple[str, bytes]:
    """Добавили адрес сайта, а не ленты: ищем его RSS на странице (<link rel="alternate">), нет — Google Новости
    по этому сайту."""
    page = raw.decode("utf-8", "ignore")
    for tag in ALT.findall(page):
        href = re.search(r'href=["\']([^"\']+)', tag)
        if not href:
            continue
        feed = urljoin(url, html.unescape(href.group(1)))
        try:
            data = await _download(session, feed)
        except Exception as exc:  # noqa: BLE001
            log.info("shorts: site feed %s failed: %s", feed, exc)
            continue
        if is_feed(data):
            return feed, data
    feed = google_news(urlparse(url).netloc.removeprefix("www."))
    return feed, await _download(session, feed)


async def _add_images(posts: list[dict[str, Any]]) -> None:
    """Картинка новости со страницы статьи (og:image), если в ленте её нет: у сайтов вроде Empire там постер
    или эксклюзивный кадр."""
    todo = [p for p in posts if not p.get("image") and "news.google.com" not in p["url"]][:OG_EVERY]
    if not todo:
        return
    async with netsafe.safe_session(timeout=aiohttp.ClientTimeout(total=15), headers=HEADERS) as safe:
        async def one(post: dict[str, Any]) -> None:
            try:
                page, _ = await netsafe.fetch_page(safe, post["url"], 1024 * 1024)
            except Exception:  # noqa: BLE001
                return
            m = OG_IMAGE.search(page)
            if m:
                image = urljoin(post["url"], html.unescape(m.group(1) or m.group(2)))
                post["image"], post["media"] = image, [{"image": image}]
        await asyncio.gather(*(one(p) for p in todo))


async def _fetch(session: aiohttp.ClientSession, entry: str, since: float, per_feed: int) -> list[dict[str, Any]]:
    global _reddit
    url = feed_url(entry)
    if not url:
        return []
    if entry == REDDIT_PACK:
        _reddit = _reddit or asyncio.Lock()
        async with _reddit:
            cached = _reddit_cache.get(url)
            if time.time() < _reddit_retry.get(url, 0):
                if not cached or time.time() - cached[0] > REDDIT_STALE:
                    raise RedditRateLimited("Лимит Reddit: ждём следующей попытки")
                raw = cached[1]
            elif cached and time.time() - cached[0] < REDDIT_EVERY:
                raw = cached[1]
            else:
                try:
                    raw = await _download_reddit(session)
                    _reddit_cache[url] = (time.time(), raw)
                    _reddit_retry.pop(url, None)
                except aiohttp.ClientResponseError as exc:
                    if exc.status != 429:
                        raise
                    retry = exc.headers.get("Retry-After", "") if exc.headers else ""
                    try:
                        seconds = float(retry)
                    except ValueError:
                        seconds = REDDIT_BACKOFF
                    _reddit_retry[url] = time.time() + max(REDDIT_EVERY, min(seconds, 31 * 86400))
                    if not cached or time.time() - cached[0] > REDDIT_STALE:
                        raise RedditRateLimited("Лимит Reddit: ждём следующей попытки") from None
                    log.info("shorts: reddit 429, using cached %s", url)
                    raw = cached[1]
    elif _reddit_name(url) and ".rss" in urlparse(url).path:
        raise RuntimeError("Reddit отключил RSS. Добавь новую подборку командой /fast reddit")
    else:
        parts = urlparse(url)
        host = (parts.hostname or "").removeprefix("www.")
        empire_home = host == "empireonline.com" and parts.path in ("", "/") and not parts.query
        known = EMPIRE_RSS if empire_home else _found_feed.get(url)
        try:
            raw = await _download(session, known or url)
            if empire_home and not is_feed(raw):
                raise ValueError("RSS Empire вернул ответ без ленты")
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            mirror = FX_FEED_MIRRORS.get(host)
            forbidden = isinstance(exc, aiohttp.ClientResponseError) and exc.status == 403
            if (mirror and FX_FEED_PATH.fullmatch(parts.path)
                    and isinstance(exc, aiohttp.ClientResponseError) and exc.status == 404):
                fallback = parts._replace(netloc=mirror).geturl()
            elif empire_home or (forbidden and host in GOOGLE_NEWS_ON_FORBIDDEN):
                fallback = google_news(host)
            else:
                raise
            raw = await _download(session, fallback)
            if not is_feed(raw):
                raise ValueError(f"{urlparse(fallback).hostname} не вернул RSS или Atom")
        else:
            if not known and not is_feed(raw):  # страница сайта — ищем его ленту
                feed, raw = await _site_feed(session, url, raw)
                _found_feed[url] = feed
    limit = min(50, 4 * per_feed) if entry == REDDIT_PACK else per_feed
    posts = await asyncio.to_thread(parse, raw, label(entry), since, limit)
    if per_feed <= PER_FEED and not entry.startswith("x:") and entry != REDDIT_PACK and not _reddit_name(url):
        await _add_images(posts)
    return posts


async def latest(feeds: list[str], hours: float = HOURS, per_feed: int = PER_FEED,
                 timeout: float = 50) -> list[dict[str, Any]]:
    """Свежие новости всех источников в виде постов каналов, новые первыми. Что не успело за timeout секунд,
    отбрасываем, но успевшее сохраняем (медленный Reddit не должен утянуть за собой Deadline и Variety)."""
    since = time.time() - hours * 3600
    sem = asyncio.Semaphore(4)

    async def one(session: aiohttp.ClientSession, entry: str) -> list[dict[str, Any]]:
        try:
            if entry == REDDIT_PACK:
                return await _fetch(session, entry, since, per_feed)
            async with sem:
                return await _fetch(session, entry, since, per_feed)
        except Exception as exc:  # noqa: BLE001
            log.info("shorts: fast feed %s failed: %s", entry, exc)
            return []

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), headers=HEADERS) as session:
        tasks = [asyncio.create_task(one(session, f)) for f in feeds]
        if not tasks:
            return []
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            log.info("shorts: %d fast feeds too slow, skipped", len(pending))
            await asyncio.gather(*pending, return_exceptions=True)
    results = [t.result() for t in done if not t.cancelled() and not t.exception()]
    return sorted((p for chunk in results for p in chunk), key=lambda p: -p["time"])


async def check(feeds: list[str]) -> list[tuple[str, int | str, str]]:
    """Для /fast: (источник, сколько новостей за сутки или почему не читается, имя). Все сразу; Reddit —
    по очереди (частые запросы он режет)."""
    since = time.time() - 24 * 3600
    sem = asyncio.Semaphore(6)

    async def one(session: aiohttp.ClientSession, entry: str) -> tuple[str, int | str, str]:
        async with sem:
            return await _one(session, entry)

    async def _one(session: aiohttp.ClientSession, entry: str) -> tuple[str, int | str, str]:
        name = label(entry)
        if not feed_url(entry):
            return entry, ("X не подключён: нужен RSSHub (COMPOSE_PROFILES=x и TWITTER_AUTH_TOKEN в .env) "
                           "или RSS-ссылка rss.app"), name
        try:
            posts = await _fetch(session, entry, 0, 200)
            url = feed_url(entry) or ""
            if entry == REDDIT_PACK and time.time() < _reddit_retry.get(url, 0):
                return entry, "Лимит Reddit: ждём следующей попытки", name
            fresh = [p for p in posts if p["time"] >= since]
            return entry, len(fresh), name or (posts[0]["label"] if posts else "RSS")
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, RedditRateLimited):
                return entry, str(exc), name
            if (isinstance(exc, aiohttp.ClientResponseError) and exc.status == 402
                    and urlparse(entry).hostname == "rss.app"):
                return entry, "RSS.app требует оплату (HTTP 402). Проверь подписку или замени ссылку.", "RSS.app"
            hint = (" — RSSHub не запущен или нет TWITTER_AUTH_TOKEN" if rsshub() and
                    str(feed_url(entry)).startswith(rsshub()) else "")
            return entry, f"{type(exc).__name__}: {str(exc)[:80]}{hint}", name or "RSS"

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40), headers=HEADERS) as session:
        return list(await asyncio.gather(*(one(session, e) for e in feeds)))


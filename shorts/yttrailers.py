"""Новые трейлеры и тизеры на YouTube-каналах киностудий.

У каждого YouTube-канала есть открытая лента новых видео (RSS, без ключа API):
https://www.youtube.com/feeds/videos.xml?channel_id=… — бот читает ленты студий и отбирает видео, в названии
которых «Trailer», «Teaser» или «Трейлер». Полные фильмы, закулисье, клипы и шортсы — мимо.
"""

from __future__ import annotations

import asyncio
import calendar
import html
import logging
import re
import time
from typing import Any

import aiohttp
import feedparser

from . import netsafe

log = logging.getLogger("shorts")

FEED = "https://www.youtube.com/feeds/videos.xml?channel_id="
# официальные каналы студий и стримингов (ID проверены: лента отдаёт заголовок канала)
STUDIOS: dict[str, str] = {
    "Warner Bros.": "UCjmJDM5pRKbUlVIzDYYWb6g",
    "Universal Pictures": "UCq0OueAsdxH6b8nyAspwViw",
    "Sony Pictures": "UCz97F7dMxBNOfGYu3rx8aCw",
    "Marvel": "UCvC4D8onUfXzvjTOM-dBfEA",
    "Paramount Pictures": "UCF9imwPMSGz4Vq1NiTWCC7g",
    "20th Century Studios": "UC2-BeLxzUBSs0uSrmzWhJuQ",
    "Disney Animation": "UC_976xMxPgzIa290Hqtk-9g",
    "Pixar": "UC_IRYSp4auq7hKLvziWVH6w",
    "Lionsgate": "UCJ6nMHaJPZvsJ-HmUmj1SeA",
    "A24": "UCuPivVjnfNo4mb3Oog_frZg",
    "DC": "UCiifkYAs_bq1pt_zbNAzYGg",
    "Netflix": "UCWOA1ZGywLbqmigxE4Qlvuw",
    "Prime Video": "UCQJWtTnAHhEG5w4uN0udnUQ",
    "HBO": "UCVTQuK2CaWaTgSsoNkn5AiQ",
    "Apple TV": "UC1Myj674wRVXB9I4c6Hm5zA",
}
TRAILER = re.compile(r"\b(?:trailers?|teasers?)\b|трейлер|тизер", re.I)
SKIP = re.compile(r"full movie|behind the scenes|featurette|#shorts|reaction|recap|clip\b|scene\b|"
                  r"audio description|sign language|\bASL\b", re.I)
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; KinoMir360-bot/1.0; +https://t.me/KinoMir360)"}


def _parse(raw: bytes, studio: str, since: float) -> list[dict[str, Any]]:
    feed = feedparser.parse(raw)
    out = []
    for entry in feed.get("entries") or []:
        title = html.unescape(str(entry.get("title") or "")).strip()
        stamp = entry.get("published_parsed") or entry.get("updated_parsed")
        published = calendar.timegm(stamp) if stamp else 0.0
        vid = str(entry.get("yt_videoid") or "")
        if not vid or published < since or not TRAILER.search(title) or SKIP.search(title):
            continue
        desc = html.unescape(str(entry.get("summary") or (entry.get("media_description") or ""))).strip()
        out.append({"id": vid, "title": title, "studio": studio, "time": published,
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "thumb": f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg", "desc": desc[:600]})
    return out


async def latest(hours: float = 24) -> list[dict[str, Any]]:
    """Трейлеры и тизеры студий за последние часы, новые первыми."""
    since = time.time() - hours * 3600

    async def one(session: aiohttp.ClientSession, studio: str, channel: str) -> list[dict[str, Any]]:
        try:
            raw = await netsafe.fetch(session, FEED + channel, 3 * 1024 * 1024)
            return await asyncio.to_thread(_parse, raw, studio, since)
        except Exception as exc:  # noqa: BLE001 — одна студия не мешает остальным
            log.info("shorts: youtube feed %s failed: %s", studio, exc)
            return []

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), headers=HEADERS) as session:
        found = await asyncio.gather(*(one(session, s, c) for s, c in STUDIOS.items()))
    return sorted((v for chunk in found for v in chunk), key=lambda v: -v["time"])

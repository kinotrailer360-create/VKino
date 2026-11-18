"""Public trailer discovery through official provider APIs.

Search results are *unverified candidates*, not permission to republish.
Optional credentials are read from the running server's environment, never
from project payloads and never returned to the user.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import aiohttp

from . import clips

log = logging.getLogger("shorts.social_sources")
TIMEOUT = aiohttp.ClientTimeout(total=12)


def useful_title(title: str, names: list[str]) -> bool:
    value = title.casefold().replace("ё", "е")
    return bool(any(name.casefold().replace("ё", "е") in value for name in names)
                and any(word in value for word in ("трейлер", "тизер", "trailer", "teaser")))


async def youtube(names: list[str]) -> list[dict[str, Any]]:
    api_key = os.getenv("YOUTUBE_API_KEY", "").strip()
    if not api_key or not names:
        return []
    query = names[0] + " official trailer"
    params = {"part": "snippet", "q": query, "type": "video",
              "maxResults": "8", "key": api_key, "safeSearch": "moderate"}
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        async with session.get("https://www.googleapis.com/youtube/v3/search",
                               params=params) as response:
            if response.status != 200:
                log.warning("YouTube search refused: HTTP %d", response.status)
                return []
            data = await response.json()
    found = []
    for item in data.get("items", []):
        video = (item.get("id") or {}).get("videoId")
        info = item.get("snippet") or {}
        title = str(info.get("title") or "")
        if video and useful_title(title, names):
            found.append({"url": "https://www.youtube.com/watch?v=" + video,
                          "title": title[:160], "publisher": str(info.get("channelTitle") or "")[:100],
                          "platform": "YouTube", "rights": "unconfirmed"})
    return found[:3]


async def vk(names: list[str]) -> list[dict[str, Any]]:
    token = os.getenv("VK_API_TOKEN", "").strip()
    if not token or not names:
        return []
    params = {"access_token": token, "v": "5.199", "q": names[0] + " трейлер",
              "count": 8, "sort": 2, "adult": 0}
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        async with session.get("https://api.vk.com/method/video.search",
                               params=params) as response:
            if response.status != 200:
                return []
            data = await response.json()
    found = []
    for item in (data.get("response") or {}).get("items", []):
        title = str(item.get("title") or "")
        owner_id, video_id = item.get("owner_id"), item.get("id")
        if isinstance(owner_id, int) and isinstance(video_id, int) and useful_title(title, names):
            found.append({"url": f"https://vkvideo.ru/video{owner_id}_{video_id}",
                          "title": title[:160], "publisher": "",
                          "platform": "VK Video", "rights": "unconfirmed"})
    return found[:3]


async def rutube(names: list[str]) -> list[dict[str, Any]]:
    match = await clips.rutube_trailer(names)
    if not match:
        return []
    return [{"url": match["url"], "title": match.get("title", "")[:160],
             "publisher": "", "platform": "Rutube",
             "rights": "unconfirmed", "duration": match.get("duration")}]


async def discover(names: list[str]) -> list[dict[str, Any]]:
    """Use the configured providers with individual failures and bounded results."""
    if not names:
        return []
    out = await asyncio.gather(youtube(names), vk(names), rutube(names),
                               return_exceptions=True)
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for block in out:
        if isinstance(block, BaseException):
            log.info("A video search provider failed: %s", type(block).__name__)
            continue
        for item in block:
            if item["url"] not in seen:
                seen.add(item["url"])
                results.append(item)
    return results[:6]

"""Свой поиск в интернете для ИИ — не зависит от лимитов Gemini.

Tavily (TAVILY_API_KEY, бесплатно до 1000 запросов в месяц, tavily.com) — тексты статей со ссылками.
Поиск Яндекса (YANDEX_SEARCH_API_KEY и YANDEX_FOLDER_ID, Yandex Cloud → Search API) — лучше всех находит
российские источники: Кинопоиск, СМИ, даты проката в России. Нет ключей — пустой результат, бот работает как раньше.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import xml.etree.ElementTree as ET
from typing import Any

import aiohttp

log = logging.getLogger("shorts")

TAVILY_KEY = os.getenv("TAVILY_API_KEY", "").strip()
YANDEX_KEY = os.getenv("YANDEX_SEARCH_API_KEY", "").strip()
YANDEX_FOLDER = os.getenv("YANDEX_FOLDER_ID", "").strip()
TAVILY_URL = "https://api.tavily.com/search"
YANDEX_URL = "https://searchapi.api.cloud.yandex.net/v2/web/search"


def available() -> bool:
    return bool(TAVILY_KEY or (YANDEX_KEY and YANDEX_FOLDER))


async def tavily(query: str, n: int = 6) -> list[dict[str, str]]:
    if not TAVILY_KEY:
        return []
    body = {"query": query[:400], "max_results": n, "search_depth": "advanced", "include_answer": False}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        async with session.post(TAVILY_URL, json=body, headers={"Authorization": f"Bearer {TAVILY_KEY}"}) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                raise RuntimeError(f"Tavily HTTP {resp.status}: {str(data)[:200]}")
    return [{"title": str(r.get("title") or ""), "url": str(r.get("url") or ""),
             "text": re.sub(r"\s+", " ", str(r.get("content") or ""))[:900]} for r in data.get("results") or []]


async def yandex(query: str, n: int = 6) -> list[dict[str, str]]:
    if not (YANDEX_KEY and YANDEX_FOLDER):
        return []
    body = {"query": {"searchType": "SEARCH_TYPE_RU", "queryText": query[:400]}, "folderId": YANDEX_FOLDER,
            "responseFormat": "FORMAT_XML", "groupSpec": {"groupsOnPage": str(n)}}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        async with session.post(YANDEX_URL, json=body, headers={"Authorization": f"Api-Key {YANDEX_KEY}"}) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                raise RuntimeError(f"Яндекс HTTP {resp.status}: {str(data)[:200]}")
    root = ET.fromstring(base64.b64decode(data.get("rawData") or ""))
    out = []
    for doc in root.iter("doc"):
        title = "".join(doc.find("title").itertext()) if doc.find("title") is not None else ""
        passages = " … ".join("".join(p.itertext()) for p in doc.iter("passage"))
        out.append({"title": title.strip(), "url": (doc.findtext("url") or "").strip(),
                    "text": re.sub(r"\s+", " ", passages)[:900]})
    return out[:n]


async def search(query: str, n: int = 6) -> list[dict[str, str]]:
    """Результаты обоих поисковиков (что подключено), без повторов ссылок; ошибки — в лог, не наверх."""
    if not query.strip() or not available():
        return []
    found: list[dict[str, str]] = []
    for name, res in zip(("Tavily", "Яндекс"), await asyncio.gather(tavily(query, n), yandex(query, n),
                                                                    return_exceptions=True)):
        if isinstance(res, Exception):
            log.warning("shorts: web search %s failed: %s", name, res)
            continue
        found += res
    seen, out = set(), []
    for r in found:
        if r["url"] not in seen and (r["title"] or r["text"]):
            seen.add(r["url"])
            out.append(r)
    return out


def as_text(results: list[dict[str, Any]], limit: int = 6000) -> str:
    blocks, size = [], 0
    for r in results:
        block = f"[{r['url']}] {r['title']}\n{r['text']}"
        if size + len(block) > limit:
            break
        blocks.append(block)
        size += len(block)
    return "\n\n".join(blocks)


async def probe() -> list[tuple[str, bool, str]]:
    """Для «Проверить ИИ»: работает ли каждый подключённый поисковик."""
    out = []
    for name, fn, on in (("Поиск Tavily", tavily, bool(TAVILY_KEY)),
                         ("Поиск Яндекса", yandex, bool(YANDEX_KEY and YANDEX_FOLDER))):
        if not on:
            continue
        try:
            res = await asyncio.wait_for(fn("Дюна 3 дата выхода", 3), 40)
            out.append((name, bool(res), f"найдено {len(res)}" if res else "пустой ответ"))
        except Exception as exc:  # noqa: BLE001
            out.append((name, False, str(exc)[:200] or type(exc).__name__))
    return out

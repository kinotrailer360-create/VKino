"""Свой поиск в интернете для ИИ — не зависит от лимитов Gemini.

DuckDuckGo — бесплатно и без ключа (выключить: DDG_SEARCH=off).
Groq Compound — ИИ Groq сам ищет в интернете (ключ тот же, что у Groq; выключить: GROQ_SEARCH=off).
Tavily (TAVILY_API_KEY, бесплатно до 1000 запросов в месяц, tavily.com) — тексты статей со ссылками.
Поиск Яндекса (YANDEX_SEARCH_API_KEY и YANDEX_FOLDER_ID, Yandex Cloud → Search API) — лучше всех находит
российские источники: Кинопоиск, СМИ, даты проката в России. Нет ключей — пустой результат, бот работает как раньше.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import aiohttp

log = logging.getLogger("shorts")

TAVILY_KEY = os.getenv("TAVILY_API_KEY", "").strip()
YANDEX_KEY = os.getenv("YANDEX_SEARCH_API_KEY", "").strip()
YANDEX_FOLDER = os.getenv("YANDEX_FOLDER_ID", "").strip()
TAVILY_URL = "https://api.tavily.com/search"
YANDEX_URL = "https://searchapi.api.cloud.yandex.net/v2/web/search"
# DuckDuckGo — бесплатно и без ключа (html-версия поиска); DDG_SEARCH=off — выключить
DDG_ON = os.getenv("DDG_SEARCH", "on").strip().lower() not in ("off", "0", "no", "false")
DDG_URL = "https://html.duckduckgo.com/html/"
# Groq Compound: модель со встроенным поиском в интернете, ищет на серверах Groq — с российского сервера работает
GROQ_SEARCH_ON = os.getenv("GROQ_SEARCH", "on").strip().lower() not in ("off", "0", "no", "false")
GROQ_SEARCH_MODEL = os.getenv("GROQ_SEARCH_MODEL", "").strip()  # пусто — бот сам находит модель Compound у Groq
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"
_groq_model = ""
BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/124.0 Safari/537.36", "Accept-Language": "ru,en;q=0.8"}


def groq_key() -> str:
    """Ключ Groq — тот же, что у резервного ИИ (если он Groq), или GROQ_API_KEY."""
    if not GROQ_SEARCH_ON:
        return ""
    from . import ai  # здесь, а не наверху: ai сам импортирует этот модуль
    if ai.RESERVE_KEY and "groq.com" in ai.RESERVE_URL:
        return ai.RESERVE_KEY
    return os.getenv("GROQ_API_KEY", "").strip().strip("\"'")


def available() -> bool:
    return bool(TAVILY_KEY or (YANDEX_KEY and YANDEX_FOLDER) or DDG_ON or groq_key())


HINTS = {401: "ключ не подходит — проверь, что скопирован целиком", 403: "доступ запрещён — сервис, похоже, "
         "не пускает запросы с этого сервера (например, из России)", 429: "кончился лимит запросов",
         432: "кончился бесплатный лимит месяца"}


async def _json_or_error(name: str, resp: aiohttp.ClientResponse) -> dict[str, Any]:
    """JSON ответа или понятная ошибка: код, подсказка и начало ответа (а не «Expecting value…»)."""
    text = await resp.text(errors="ignore")
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if resp.status != 200 or not isinstance(data, dict):
        snippet = re.sub(r"<[^>]+>|\s+", " ", text).strip()[:150]
        hint = HINTS.get(resp.status, "")
        raise RuntimeError(f"{name} HTTP {resp.status}" + (f" — {hint}" if hint else "") + (f": {snippet}" if snippet else ""))
    return data


async def tavily(query: str, n: int = 6) -> list[dict[str, str]]:
    if not TAVILY_KEY:
        return []
    body = {"query": query[:400], "max_results": n, "search_depth": "advanced", "include_answer": False}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        async with session.post(TAVILY_URL, json=body, headers={"Authorization": f"Bearer {TAVILY_KEY}"}) as resp:
            data = await _json_or_error("Tavily", resp)
    return [{"title": str(r.get("title") or ""), "url": str(r.get("url") or ""),
             "text": re.sub(r"\s+", " ", str(r.get("content") or ""))[:900]} for r in data.get("results") or []]


async def yandex(query: str, n: int = 6) -> list[dict[str, str]]:
    if not (YANDEX_KEY and YANDEX_FOLDER):
        return []
    body = {"query": {"searchType": "SEARCH_TYPE_RU", "queryText": query[:400]}, "folderId": YANDEX_FOLDER,
            "responseFormat": "FORMAT_XML", "groupSpec": {"groupsOnPage": str(n)}}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        async with session.post(YANDEX_URL, json=body, headers={"Authorization": f"Api-Key {YANDEX_KEY}"}) as resp:
            data = await _json_or_error("Яндекс", resp)
    root = ET.fromstring(base64.b64decode(data.get("rawData") or ""))
    out = []
    for doc in root.iter("doc"):
        title = "".join(doc.find("title").itertext()) if doc.find("title") is not None else ""
        passages = " … ".join("".join(p.itertext()) for p in doc.iter("passage"))
        out.append({"title": title.strip(), "url": (doc.findtext("url") or "").strip(),
                    "text": re.sub(r"\s+", " ", passages)[:900]})
    return out[:n]


class _DDG(HTMLParser):
    """Результаты html-версии DuckDuckGo: ссылка, заголовок, сниппет."""

    def __init__(self) -> None:
        super().__init__()
        self.items: list[dict[str, str]] = []
        self.field = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "a" and "result__a" in cls:
            self.items.append({"url": _ddg_url(a.get("href") or ""), "title": "", "text": ""})
            self.field = "title"
        elif "result__snippet" in cls and self.items:
            self.field = "text"

    def handle_endtag(self, tag: str) -> None:
        if tag in ("a", "td", "div"):
            self.field = ""

    def handle_data(self, data: str) -> None:
        if self.field and self.items:
            self.items[-1][self.field] += data


def _ddg_url(href: str) -> str:
    """Ссылки DuckDuckGo идут через редирект //duckduckgo.com/l/?uddg=<настоящий адрес>."""
    q = parse_qs(urlparse(href if href.startswith("http") else "https:" + href).query)
    return unquote(q["uddg"][0]) if q.get("uddg") else href


async def duckduckgo(query: str, n: int = 6) -> list[dict[str, str]]:
    if not DDG_ON:
        return []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25), headers=BROWSER) as session:
        async with session.post(DDG_URL, data={"q": query[:400], "kl": "ru-ru"}) as resp:
            text = await resp.text(errors="ignore")
            if resp.status != 200:
                hint = HINTS.get(resp.status, "")
                raise RuntimeError(f"DuckDuckGo HTTP {resp.status}" + (f" — {hint}" if hint else ""))
    if "anomaly" in text.lower() and "result__a" not in text:
        raise RuntimeError("DuckDuckGo просит капчу — с этого сервера он запросы не принимает")
    parser = _DDG()
    parser.feed(text)
    out = []
    for r in parser.items:
        if r["url"].startswith("http") and "duckduckgo.com/y.js" not in r["url"]:  # без рекламы
            out.append({"url": r["url"], "title": re.sub(r"\s+", " ", r["title"]).strip(),
                        "text": re.sub(r"\s+", " ", r["text"]).strip()[:600]})
    for r in out[:2]:  # у двух первых — текст самой страницы: сниппета для проверки фактов мало
        try:
            page = await asyncio.wait_for(_page_text(r["url"]), 15)
            if page:
                r["text"] = (r["text"] + " … " + page)[:1500]
        except Exception as exc:  # noqa: BLE001
            log.info("shorts: page %s not read: %s", r["url"][:60], exc)
    return out[:n]


async def _page_text(url: str) -> str:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), headers=BROWSER) as session:
        async with session.get(url) as resp:
            if resp.status != 200 or "html" not in resp.headers.get("Content-Type", ""):
                return ""
            html = await resp.text(errors="ignore")
    paras = [re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", p))).strip()
             for p in re.findall(r"<p[^>]*>(.*?)</p>", html, re.S | re.I)]
    return " ".join(p for p in paras if len(p) >= 40)[:1200]


async def _pick_groq_model(session: aiohttp.ClientSession, key: str) -> str:
    """Модель Compound из списка моделей ключа: названия у Groq меняются, поэтому не зашиваем."""
    global _groq_model
    if GROQ_SEARCH_MODEL:
        return GROQ_SEARCH_MODEL
    if not _groq_model:
        async with session.get(GROQ_MODELS_URL, headers={"Authorization": f"Bearer {key}"}) as resp:
            data = await _json_or_error("Groq Compound (список моделей)", resp)
        ids = [str(m.get("id")) for m in data.get("data") or [] if isinstance(m, dict) and m.get("id")]
        found = sorted((i for i in ids if "compound" in i.lower()), key=lambda i: ("mini" in i.lower(), len(i)))
        if not found:
            raise RuntimeError("у ключа Groq нет моделей Compound с поиском; доступны: " + ", ".join(ids[:8]))
        _groq_model = found[0]
    return _groq_model


async def groq_compound(query: str, n: int = 6) -> list[dict[str, str]]:
    """Groq Compound ищет в интернете сам: сводка фактов со ссылками + найденные страницы."""
    key = groq_key()
    if not key:
        return []
    ask = (f"Найди в интернете свежие проверенные факты по запросу: «{query[:400]}». Ответь по-русски коротким "
           "списком фактов: у каждого — дата, если есть, и ссылка на источник. Ничего не додумывай: не нашёл — "
           "так и напиши.")
    global _groq_model
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=38)) as session:
        model = await _pick_groq_model(session, key)
        body = {"model": model, "temperature": 0.1, "messages": [{"role": "user", "content": ask}]}
        async with session.post(GROQ_URL, json=body, headers={"Authorization": f"Bearer {key}"}) as resp:
            if resp.status == 404:
                _groq_model = ""  # модель убрали — в следующий раз выберем заново
            data = await _json_or_error(f"Groq Compound ({model})", resp)
    msg = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
    answer = re.sub(r"<think>.*?</think>", "", str(msg.get("content") or ""), flags=re.S).strip()
    out = [{"title": "Сводка поиска Groq Compound", "url": "groq-compound", "text": answer[:2500]}] if answer else []
    for tool in msg.get("executed_tools") or []:
        results = (tool.get("search_results") or {}).get("results") if isinstance(tool, dict) else None
        for r in results or []:
            if isinstance(r, dict) and r.get("url"):
                out.append({"title": str(r.get("title") or ""), "url": str(r["url"]),
                            "text": re.sub(r"\s+", " ", str(r.get("content") or ""))[:900]})
    return out[: n + 1]


async def search(query: str, n: int = 6) -> list[dict[str, str]]:
    """Результаты всех поисковиков (что подключено), без повторов ссылок; ошибки — в лог, не наверх."""
    if not query.strip() or not available():
        return []
    found: list[dict[str, str]] = []
    for name, res in zip(("Groq Compound", "Tavily", "Яндекс", "DuckDuckGo"),
                         await asyncio.gather(groq_compound(query, n), tavily(query, n), yandex(query, n),
                                              duckduckgo(query, n), return_exceptions=True)):
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
                         ("Поиск Яндекса", yandex, bool(YANDEX_KEY and YANDEX_FOLDER)),
                         ("Поиск DuckDuckGo (бесплатный)", duckduckgo, DDG_ON),
                         ("Поиск Groq Compound (ИИ ищет сам)", groq_compound, bool(groq_key()))):
        if not on:
            continue
        try:
            res = await asyncio.wait_for(fn("Дюна 3 дата выхода", 3), 40)
            out.append((name, bool(res), f"найдено {len(res)}" if res else "пустой ответ"))
        except Exception as exc:  # noqa: BLE001
            out.append((name, False, str(exc)[:200] or type(exc).__name__))
    return out

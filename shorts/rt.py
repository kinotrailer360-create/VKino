"""Rotten Tomatoes: оценки критиков (Tomatometer) и зрителей (Popcornmeter), консенсус и цитаты рецензий.

Официального API нет — читаем страницы сайта. Разметка у сайта меняется, поэтому разбор терпимый: сначала
JSON-блоки страницы, потом регулярные выражения, в конце — просто текст страницы (из него цитаты выберет ИИ).
"""

from __future__ import annotations

import html
import json
import logging
import re
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import quote

import aiohttp

log = logging.getLogger("shorts")

BASE = "https://www.rottentomatoes.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/130.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
URL_RE = re.compile(r"https?://(?:www\.)?rottentomatoes\.com/m/[a-z0-9_\-]+", re.I)


def slug(title: str) -> str:
    text = re.sub(r"[’'`]", "", title.lower().replace("&", "and"))
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def _clean(raw: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw or ""))
    return re.sub(r"\s+", " ", text).strip()


async def _get(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url) as resp:
        if resp.status == 404:
            return ""
        resp.raise_for_status()
        return await resp.text()


def _page_title(page: str) -> str:
    m = re.search(r"<title>(.*?)</title>", page, re.S)
    return _clean(m.group(1)).split("|")[0].strip() if m else ""


def _matches(page: str, title: str, year: str) -> bool:
    """Страница того фильма: похожее название и год (у ремейков и тёзок другой год)."""
    name = re.sub(r"\(\d{4}\)", "", _page_title(page)).strip().lower()
    if SequenceMatcher(None, name, title.lower()).ratio() < 0.75 and title.lower() not in name:
        return False
    return not year or year in page[:200000]


async def find(title: str, year: str = "", search: Any = None) -> str | None:
    """Адрес страницы фильма на RT по оригинальному названию и году. search — веб-поиск (запасной путь)."""
    if not title:
        return None
    s = slug(title)
    tries = ([f"{BASE}/m/{s}_{year}"] if year else []) + [f"{BASE}/m/{s}"]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), headers=HEADERS) as session:
        for url in tries:
            try:
                page = await _get(session, url)
            except Exception as exc:  # noqa: BLE001
                log.info("shorts: rt %s failed: %s", url, exc)
                continue
            if page and _matches(page, title, year):
                return url
        try:  # поиск самого сайта
            page = await _get(session, f"{BASE}/search?search={quote(title)}")
            for path in dict.fromkeys(re.findall(r'href="((?:https://www\.rottentomatoes\.com)?/m/[a-z0-9_\-]+)"',
                                                 page)):
                url = path if path.startswith("http") else BASE + path
                if SequenceMatcher(None, url.rsplit("/", 1)[1].rstrip("_0123456789"), s).ratio() < 0.8:
                    continue
                cand = await _get(session, url)
                if cand and _matches(cand, title, year):
                    return url
        except Exception as exc:  # noqa: BLE001
            log.info("shorts: rt search failed: %s", exc)
    if search:  # веб-поиск: «Title 2026 rotten tomatoes»
        try:
            for r in await search(f"{title} {year} Rotten Tomatoes"):
                m = URL_RE.search(str(r.get("url") or r.get("link") or ""))
                if m:
                    return m.group(0).rstrip("/")
        except Exception as exc:  # noqa: BLE001
            log.info("shorts: rt web search failed: %s", exc)
    return None


def _num(value: Any) -> int | None:
    m = re.search(r"\d[\d,]*", str(value or ""))
    return int(m.group(0).replace(",", "")) if m else None


def scores(page: str) -> dict[str, Any]:
    """{"critics": %, "critics_count": рецензий, "audience": %, "audience_count": оценок, "consensus",
    "audience_consensus"} — чего нет на странице, того нет в словаре."""
    out: dict[str, Any] = {}
    m = re.search(r'<script[^>]+id="media-scorecard-json"[^>]*>(.*?)</script>', page, re.S)
    if m:
        try:
            data = json.loads(m.group(1))
            critics, audience = data.get("criticsScore") or {}, data.get("audienceScore") or {}
            if _num(critics.get("score")) is not None:
                out["critics"] = _num(critics.get("score"))
                out["critics_count"] = _num(critics.get("reviewCount") or critics.get("ratingCount"))
            if _num(audience.get("score")) is not None:
                out["audience"] = _num(audience.get("score"))
                out["audience_count"] = _num(audience.get("bandedRatingCount") or audience.get("ratingCount")
                                             or audience.get("reviewCount"))
            for key, name in (("criticsConsensus", "consensus"), ("audienceConsensus", "audience_consensus")):
                if isinstance(data.get(key), str):
                    out[name] = _clean(data[key])
            overlay = data.get("overlay") or {}
            for key, name in (("criticsConsensus", "consensus"), ("audienceConsensus", "audience_consensus")):
                if name not in out and isinstance(overlay.get(key), str):
                    out[name] = _clean(overlay[key])
        except (ValueError, AttributeError) as exc:
            log.info("shorts: rt scorecard parse failed: %s", exc)
    if "critics" not in out:  # JSON-LD: aggregateRating — это Tomatometer
        m = re.search(r'"aggregateRating"\s*:\s*\{[^}]*?"ratingValue"\s*:\s*"?(\d{1,3})"?[^}]*\}', page)
        if m:
            out["critics"] = int(m.group(1))
            c = re.search(r'"(?:reviewCount|ratingCount)"\s*:\s*"?(\d+)', m.group(0))
            out["critics_count"] = int(c.group(1)) if c else None
    if "critics" not in out:
        m = re.search(r"(\d{1,3})%\s*(?:</[^>]+>\s*)*(?:<[^>]+>\s*)*Tomatometer", page)
        if m:
            out["critics"] = int(m.group(1))
    if "audience" not in out:
        m = re.search(r"(\d{1,3})%\s*(?:</[^>]+>\s*)*(?:<[^>]+>\s*)*(?:Popcornmeter|Audience Score)", page)
        if m:
            out["audience"] = int(m.group(1))
    for name, attr in (("consensus", "critics-consensus"), ("audience_consensus", "audience-consensus")):
        if name not in out:
            m = re.search(rf'(?:id|data-qa)="{attr}"[^>]*>(.*?)</(?:p|div|span)>', page, re.S)
            text = _clean(m.group(1)) if m else ""
            if len(text) > 20:
                out[name] = text
    return out


def reviews(page: str, limit: int = 12) -> list[str]:
    """Цитаты рецензий со страницы отзывов: «Имя (Издание): текст»; пусто — разметка не узналась."""
    out = []
    rows = re.split(r'<div[^>]+class="[^"]*(?:review-row|audience-review-row)[^"]*"', page)[1:]
    for row in rows:
        text = re.search(r'<p[^>]+(?:class="[^"]*review-text[^"]*"|data-qa="review-(?:quote|text)")[^>]*>(.*?)</p>',
                         row, re.S)
        if not text:
            continue
        who = re.search(r'data-qa="review-critic-link"[^>]*>(.*?)</a>|class="[^"]*display-name[^"]*"[^>]*>(.*?)<',
                        row, re.S)
        pub = re.search(r'data-qa="review-publication"[^>]*>(.*?)</a>', row, re.S)
        mood = re.search(r'state="rotten"|sentiment="(?:NEGATIVE|negative)"', row[:4000])
        name = _clean(next((g for g in (who.groups() if who else ()) if g), ""))
        head = name + (f" ({_clean(pub.group(1))})" if pub else "")
        out.append((f"{head}: " if head else "") + _clean(text.group(1)) + (" [отрицательная]" if mood else ""))
        if len(out) >= limit:
            break
    return out


def page_text(page: str, limit: int = 5000) -> str:
    page = re.sub(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", page, flags=re.S | re.I)
    return _clean(page)[:limit]


async def info(url: str) -> dict[str, Any]:
    """Всё для поста: оценки, консенсусы, цитаты критиков и зрителей (или куски текста страниц)."""
    out: dict[str, Any] = {"url": url}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25), headers=HEADERS) as session:
        main = await _get(session, url)
        out.update(scores(main))
        out["title"] = _page_title(main)
        for key, paths in (("critics_reviews", ["/reviews?type=top_critics", "/reviews"]),
                           ("audience_reviews", ["/reviews/all-audience", "/reviews?type=user"])):
            for path in paths:
                try:
                    page = await _get(session, url + path)
                except Exception as exc:  # noqa: BLE001
                    log.info("shorts: rt %s failed: %s", path, exc)
                    continue
                found = reviews(page)
                if found:
                    out[key] = found
                    break
                text = page_text(page, 4000) if page else ""
                if key + "_text" not in out and len(text) > 500:
                    out[key + "_text"] = text
    return out


def as_material(data: dict[str, Any], audience: bool) -> str:
    rows = [f"Rotten Tomatoes: {data.get('url')}"]
    if data.get("critics") is not None:
        rows.append(f"Tomatometer (критики): {data['critics']}%"
                    + (f", рецензий: {data['critics_count']}" if data.get("critics_count") else ""))
    if data.get("audience") is not None:
        rows.append(f"Popcornmeter (зрители): {data['audience']}%"
                    + (f", оценок: {data['audience_count']}" if data.get("audience_count") else ""))
    if data.get("consensus"):
        rows.append(f"Консенсус критиков: {data['consensus']}")
    if data.get("audience_consensus"):
        rows.append(f"Что говорят зрители (сводка сайта): {data['audience_consensus']}")
    key = "audience_reviews" if audience else "critics_reviews"
    if data.get(key):
        rows.append(("Отзывы зрителей:\n" if audience else "Рецензии критиков:\n") + "\n".join(
            f"— {r}" for r in data[key]))
    elif data.get(key + "_text"):
        rows.append("Текст страницы отзывов (выбери из него цитаты):\n" + data[key + "_text"])
    if audience and data.get("critics_reviews"):  # для сравнения со зрителями
        rows.append("Критики коротко:\n" + "\n".join(f"— {r}" for r in data["critics_reviews"][:4]))
    return "\n".join(rows)


async def page_scores(url: str) -> dict[str, Any]:
    """Только оценки со страницы фильма (для автопроверки: сняли ли эмбарго, появились ли оценки зрителей)."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), headers=HEADERS) as session:
        return scores(await _get(session, url))

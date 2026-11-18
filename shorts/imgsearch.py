"""Поиск картинок без ключей, как человек в поиске: выдача DuckDuckGo или Bing → миниатюры → Gemini выбирает ту,
что подходит к фразе → скачиваем полную. Google ботов на страницу поиска не пускает, поэтому ссылки на поиск
Google бот превращает в запрос и ищет здесь. Фотостоки с водяными знаками и мелкие картинки пропускаются."""

from __future__ import annotations

import asyncio
import html
import io
import json
import logging
import re
from urllib.parse import quote_plus

import aiohttp
from PIL import Image

from . import ai, netsafe

log = logging.getLogger("shorts")

STOCK = ("shutterstock", "alamy", "gettyimages", "istockphoto", "dreamstime", "depositphotos", "123rf", "adobe.com",
         "pinterest", "vecteezy", "freepik")
# поисковики отдают выдачу браузеру, а не «боту»
BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/130.0 Safari/537.36", "Accept-Language": "ru,en;q=0.8"}
last_error = ""  # почему поиск не дал картинок (для сообщения автору)
last_pick = ""  # адрес выбранной картинки (источник — для автора)


def _ok(url: str, width: int = 0) -> bool:
    return url.startswith("http") and not any(x in url.lower() for x in STOCK) and not (0 < width < 500)


async def ddg(session: aiohttp.ClientSession, query: str, limit: int = 12) -> list[dict[str, str]]:
    page, _ = await netsafe._get(session, "https://duckduckgo.com/?ia=images&iax=images&q=" + quote_plus(query),
                                 2 * 1024 * 1024, "")
    m = re.search(rb"vqd=[\"']?([\d-]+)", page)
    if not m:
        raise RuntimeError("DuckDuckGo не отдал токен поиска")
    raw, _ = await netsafe._get(session, "https://duckduckgo.com/i.js?l=wt-wt&o=json&f=,,,,,&p=1&q="
                                + quote_plus(query) + "&vqd=" + m.group(1).decode(), 3 * 1024 * 1024, "")
    out = []
    for r in json.loads(raw.decode("utf-8", "ignore")).get("results") or []:
        image = str(r.get("image") or "")
        if _ok(image + " " + str(r.get("url") or ""), int(r.get("width") or 0)):
            out.append({"image": image, "thumb": str(r.get("thumbnail") or image), "source": str(r.get("url") or "")})
        if len(out) >= limit:
            break
    return out


async def bing(session: aiohttp.ClientSession, query: str, limit: int = 12) -> list[dict[str, str]]:
    page, _ = await netsafe._get(session, "https://www.bing.com/images/search?form=HDRSC2&first=1&q="
                                 + quote_plus(query), 3 * 1024 * 1024, "")
    out = []
    for m in re.finditer(r'm="(\{[^"]+\})"', page.decode("utf-8", "ignore")):
        try:
            meta = json.loads(html.unescape(m.group(1)))
        except ValueError:
            continue
        image, thumb = str(meta.get("murl") or ""), str(meta.get("turl") or "")
        if _ok(image + " " + str(meta.get("purl") or "")):
            out.append({"image": image, "thumb": thumb or image, "source": str(meta.get("purl") or "")})
        if len(out) >= limit:
            break
    return out


async def search(query: str, limit: int = 12) -> list[dict[str, str]]:
    """Search across DDG/Bing/Yandex and deduplicate image URLs."""
    global last_error
    last_error = ""
    errors = []
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    async with netsafe.safe_session(timeout=aiohttp.ClientTimeout(total=40), headers=BROWSER) as session:
        for name, engine in (("DuckDuckGo", ddg), ("Bing", bing), ("Yandex", yandex_images)):
            try:
                result = await asyncio.wait_for(engine(session, query, limit), timeout=12)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name}: {str(exc)[:80]}")
                log.info("shorts: image search %s %r failed: %s", name, query, exc)
                continue
            if not result:
                errors.append(f"{name}: пусто")
            for item in result:
                url = str(item.get("image") or "")
                if not _ok(url) or url in seen:
                    continue
                seen.add(url)
                found.append(item)
                if len(found) >= limit:
                    break
            if len(found) >= limit:
                break
    last_error = "; ".join(errors)
    return found


def yandex_results(page: str, limit: int) -> list[dict[str, str]]:
    for match in re.finditer(r'data-state="([^"]+)"', page):
        if "origUrl" not in match[1]:
            continue
        try:
            items = json.loads(html.unescape(match[1]))["initialState"]["serpList"]["items"]
            rows = [items["entities"][ident] for ident in items["keys"]]
        except (KeyError, TypeError, ValueError):
            continue
        hits = []
        for row in rows:
            image = str(row.get("origUrl") or "")
            source = str((row.get("snippet") or {}).get("url") or "")
            width = row.get("origWidth")
            if source.startswith(("https://", "http://")) and _ok(image + " " + source, width if isinstance(width, int) else 0):
                hits.append({"image": image, "source": source})
                if len(hits) >= limit:
                    break
        return hits
    return []


async def yandex_images(session: aiohttp.ClientSession, query: str, limit: int) -> list[dict[str, str]]:
    page, _ = await netsafe.fetch_page(session, "https://yandex.ru/images/search?text=" + quote_plus(query))
    return yandex_results(page, limit)


async def broad_search(session: aiohttp.ClientSession, queries: list[str]) -> list[dict[str, str]]:
    async def run(engine, query):
        try:
            return await asyncio.wait_for(engine(session, query, 6), 8)
        except (aiohttp.ClientError, OSError, TimeoutError, RuntimeError, ValueError) as exc:
            log.info("shorts: %s did not return images: %s", engine.__name__, type(exc).__name__)
            return []

    batches = await asyncio.gather(*(run(engine, query) for query in queries
                                      for engine in (yandex_images, bing, ddg)))
    # Round-robin so one engine's results do not fill the entire shortlist.
    return [batch[i] for i in range(6) for batch in batches if len(batch) > i]


async def pick(query: str, phrase: str, gemini_key: str) -> bytes | None:
    """Найти по запросу и выбрать картинку под фразу (Gemini по миниатюрам). Ничего не подошло — None."""
    found = await search(query)
    if not found:
        return None
    thumbs, cands = [], []
    async with netsafe.safe_session(timeout=aiohttp.ClientTimeout(total=40), headers=BROWSER) as session:
        for item in found:
            try:
                raw, _ = await netsafe._get(session, item["thumb"], 2 * 1024 * 1024, "")
                thumbs.append(Image.open(io.BytesIO(raw)).convert("RGB"))
                cands.append(item)
            except Exception:  # noqa: BLE001
                continue
        if not cands:
            return None
        order = list(range(len(cands)))
        choice = await ai.match_images(gemini_key, [phrase or query], thumbs)
        if choice and 0 <= choice[0] < len(cands):
            order.remove(choice[0])
            order.insert(0, choice[0])
        else:
            return None  # ИИ посмотрел: ни одна не подходит
        # Never fall back to a runner-up which was not verified for this scene.
        for k in order[:1]:
            try:
                raw, ctype = await netsafe._get(session, cands[k]["image"], 15 * 1024 * 1024, "")
                if ctype.startswith("image/"):
                    global last_pick
                    last_pick = cands[k]["image"]
                    return raw
            except Exception:  # noqa: BLE001
                continue
            try:  # полная не скачалась — миниатюра лучше, чем ничего, если она не крошечная
                if thumbs[k].width >= 400:
                    buf = io.BytesIO()
                    thumbs[k].save(buf, "JPEG", quality=92)
                    last_pick = cands[k]["image"]
                    return buf.getvalue()
            except Exception:  # noqa: BLE001
                continue
    return None


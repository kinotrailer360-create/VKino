"""Текст и медиа по ссылке — для «🔗 Пост по ссылке»: пост Telegram-канала или статья сайта."""

from __future__ import annotations

import asyncio
import json
import re
import sys
from urllib.parse import urlparse
from html import unescape
from html.parser import HTMLParser
from typing import Any

import aiohttp

from . import netsafe, tgsearch

HEADERS = {"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/120.0 Mobile Safari/537.36", "Accept-Language": "ru,en;q=0.8"}
TEXT_MAX = 6000
INSTAGRAM = re.compile(r"^https?://(?:www\.)?instagram\.com/(?:p|reel|reels|tv)/[A-Za-z0-9_-]+/?(?:\?[^#]*)?$", re.I)
TG_POST = re.compile(r"(?:https?://)?t\.me/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,31})/(\d+)")


class _Article(HTMLParser):
    """og:title/description/image/video и абзацы <p> статьи (без меню, скриптов и подвалов)."""

    SKIP = {"script", "style", "nav", "footer", "header", "aside", "form", "noscript"}

    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, str] = {}
        self.paras: list[str] = []
        self.images: list[str] = []
        self.skip = 0
        self.in_p = 0
        self.buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key == "og:image" and a.get("content"):
                self.images.append(str(a["content"]))
            if key in ("og:title", "og:description", "og:image", "og:video", "og:video:url", "twitter:image"):
                self.meta.setdefault(key, str(a.get("content") or ""))
        elif tag in self.SKIP:
            self.skip += 1
        elif tag == "p" and not self.skip:
            self.in_p, self.buf = 1, []

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag == "p" and self.in_p:
            self.in_p = 0
            text = re.sub(r"\s+", " ", unescape("".join(self.buf))).strip()
            if len(text) >= 40:
                self.paras.append(text)

    def handle_data(self, data: str) -> None:
        if self.in_p and not self.skip:
            self.buf.append(data)


async def _instagram_metadata(url: str) -> dict[str, Any]:
    """Try yt-dlp's Instagram extractor, including carousel entries, without downloading media."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "yt_dlp", "--flat-playlist", "--dump-single-json", "--skip-download",
        "--no-warnings", "--yes-playlist", "--socket-timeout", "12", url,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=45)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        proc.kill()
        await proc.wait()
        raise RuntimeError("Instagram: истекло время получения публикации")
    if proc.returncode:
        raise RuntimeError("Instagram: не удалось получить медиа: " +
                           err.decode(errors="replace")[-180:])
    try:
        info = json.loads(out)
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError("Instagram вернул некорректные метаданные") from exc
    entries = info.get("entries") or [info]
    images: list[str] = []
    for entry in entries[:20]:
        if not isinstance(entry, dict):
            continue
        # For carousel images, yt-dlp exposes the image URL as the entry URL.
        candidate = entry.get("url") if entry.get("ext") in ("jpg", "jpeg", "png", "webp") else None
        candidate = candidate or entry.get("thumbnail")
        if not candidate:
            thumbs = entry.get("thumbnails") or []
            candidate = next((t.get("url") for t in reversed(thumbs)
                              if isinstance(t, dict) and t.get("url")), None)
        if isinstance(candidate, str) and candidate.startswith("https://"):
            images.append(candidate)
    return {
        "title": str(info.get("title") or ""),
        "text": str(info.get("description") or ""),
        "images": list(dict.fromkeys(images))[:20],
    }


async def fetch(url: str) -> dict[str, Any]:
    """{"url", "title", "text", "image", "video", "thumb"} — пост Telegram (через веб-версию) или статья сайта;
    thumb — маленькая обложка видео, которое веб-версия Telegram не отдаёт."""
    if "instagram.com" in (urlparse(url).hostname or "").lower() and not INSTAGRAM.fullmatch(url):
        raise RuntimeError("Поддерживаются только публичные Instagram-публикации и Reels")
    timeout = aiohttp.ClientTimeout(total=25)
    async with netsafe.safe_session(timeout=timeout, headers=HEADERS) as session:
        m = TG_POST.search(url)
        if m:
            async with session.get(f"https://t.me/{m.group(1)}/{m.group(2)}", params={"embed": "1", "mode": "tme"}) as resp:
                resp.raise_for_status()
                parser = tgsearch._Posts()
                parser.feed(await resp.text())
            post = next((p for p in parser.all if p["text"] or p["image"] or p["video"]), None)
            if not post:
                raise RuntimeError("пост не открылся (канал закрытый или пост удалён)")
            return {"url": url, "title": "", "text": post["text"][:TEXT_MAX], "image": post["image"],
                    "video": post["video"], "thumb": post.get("thumb"), "media": post.get("media") or []}
        # статья по ссылке: только публичный интернет (и после перенаправлений), не больше 3 МБ
        try:
            html, _ = await netsafe.fetch_page(session, url)
        except Exception:
            if not INSTAGRAM.fullmatch(url):
                raise
            html = ""  # yt-dlp fallback can work even if Instagram blocks HTML access
    parser = _Article()
    parser.feed(html)
    if INSTAGRAM.fullmatch(url):
        description = unescape(parser.meta.get("og:description", ""))
        title = unescape(parser.meta.get("og:title", ""))
        pictures = list(dict.fromkeys(parser.images))[:8]
        # Instagram's OpenGraph usually exposes only the first carousel image.
        # Use yt-dlp metadata to discover remaining public slides.
        try:
            extra = await _instagram_metadata(url)
            description = extra["text"] or description
            title = extra["title"] or title
            pictures = list(dict.fromkeys(extra["images"] + pictures))[:20]
        except Exception:
            if not description and not title and not pictures:
                raise
        if not description and not title and not pictures:
            raise RuntimeError("Instagram не предоставил содержимое публикации")
        return {"url": url, "title": title, "text": description[:TEXT_MAX],
                "image": pictures[0] if pictures else None,
                "video": _direct_video(parser.meta.get("og:video:url") or parser.meta.get("og:video") or ""),
                "media": [{"image": img} for img in pictures], "source": "instagram"}
    text = "\n".join(parser.paras)[:TEXT_MAX] or parser.meta.get("og:description", "")
    if not text and not parser.meta.get("og:title"):
        raise RuntimeError("на странице не нашлось текста")
    return {"url": url, "title": parser.meta.get("og:title", ""), "text": text,
            "image": parser.meta.get("og:image") or parser.meta.get("twitter:image") or None,
            "video": _direct_video(parser.meta.get("og:video:url") or parser.meta.get("og:video") or "")}


def _direct_video(url: str) -> str | None:
    """og:video годится, только если это сам файл (.mp4 и т. п.): у YouTube, Vimeo и многих сайтов там адрес
    встроенного проигрывателя — страница, а не видео (в посте выходил пустой ролик 0:00)."""
    if not url or re.search(r"youtube\.com|youtu\.be|vimeo\.com|rutube\.ru|vk\.com|dailymotion", url, re.I):
        return None
    return url if re.search(r"\.(?:mp4|m4v|mov|webm)(?:\?|$)", url, re.I) else None

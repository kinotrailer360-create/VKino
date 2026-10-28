"""Текст и медиа по ссылке — для «🔗 Пост по ссылке»: пост Telegram-канала или статья сайта."""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser
from typing import Any

import aiohttp

from . import tgsearch

HEADERS = {"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/120.0 Mobile Safari/537.36", "Accept-Language": "ru,en;q=0.8"}
TEXT_MAX = 6000
TG_POST = re.compile(r"(?:https?://)?t\.me/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,31})/(\d+)")


class _Article(HTMLParser):
    """og:title/description/image/video и абзацы <p> статьи (без меню, скриптов и подвалов)."""

    SKIP = {"script", "style", "nav", "footer", "header", "aside", "form", "noscript"}

    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, str] = {}
        self.paras: list[str] = []
        self.skip = 0
        self.in_p = 0
        self.buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
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


async def fetch(url: str) -> dict[str, Any]:
    """{"url", "title", "text", "image", "video", "thumb"} — пост Telegram (через веб-версию) или статья сайта;
    thumb — маленькая обложка видео, которое веб-версия Telegram не отдаёт."""
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout, headers=HEADERS) as session:
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
                    "video": post["video"], "thumb": post.get("thumb")}
        async with session.get(url) as resp:
            resp.raise_for_status()
            html = await resp.text(errors="ignore")
    parser = _Article()
    parser.feed(html)
    text = "\n".join(parser.paras)[:TEXT_MAX] or parser.meta.get("og:description", "")
    if not text and not parser.meta.get("og:title"):
        raise RuntimeError("на странице не нашлось текста")
    return {"url": url, "title": parser.meta.get("og:title", ""), "text": text,
            "image": parser.meta.get("og:image") or parser.meta.get("twitter:image") or None,
            "video": parser.meta.get("og:video:url") or parser.meta.get("og:video") or None}

"""Безопасные загрузки из интернета: только публичные адреса и ограничение размера.

Адреса картинок и лент приходят из чужих RSS и постов: такой адрес не должен вести на сам сервер
(localhost, внутренняя сеть, служебные адреса облака), а огромный файл — забивать память.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlparse

import aiohttp
from yarl import URL

MAX_BYTES = 15 * 1024 * 1024  # картинка или лента больше — не нужна
MAX_REDIRECTS = 5


class UnsafeURL(RuntimeError):
    pass


def _bad_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
            or addr.is_multicast or addr.is_unspecified)


async def check_url(url: str) -> None:
    """Адрес — http(s) и ведёт в публичный интернет; иначе UnsafeURL."""
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UnsafeURL(f"не http(s): {url[:60]}")
    host = parts.hostname
    try:
        if _bad_ip(host):
            raise UnsafeURL(f"внутренний адрес: {host}")
        return
    except ValueError:
        pass  # это имя, а не IP — проверяем, куда оно указывает
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return  # не разрешилось — запрос просто не пройдёт
    for info in infos:
        if _bad_ip(info[4][0]):
            raise UnsafeURL(f"{host} ведёт во внутреннюю сеть")


def _same_origin(url: str, trusted: str) -> bool:
    """Тот же сервер, что доверенный (схема, хост и порт — разобранные, а не сравнение строк: иначе
    «http://rsshub:1200@чужой-сайт/» тоже начиналось бы с доверенного адреса)."""
    a, b = urlparse(url), urlparse(trusted)
    try:
        return (a.scheme, a.hostname, a.port or 0) == (b.scheme, b.hostname, b.port or 0) and not a.username
    except ValueError:
        return False


async def fetch(session: aiohttp.ClientSession, url: str, limit: int = MAX_BYTES, trusted: str = "") -> bytes:
    """GET с проверкой адреса на каждом перенаправлении и потолком размера. trusted — свой адрес из .env
    (RSSHub в docker-сети: он внутренний, но его задал владелец бота)."""
    return (await _get(session, url, limit, trusted))[0]


PAGE_BYTES = 3 * 1024 * 1024  # html-страница больше — читать не нужно


async def fetch_page(session: aiohttp.ClientSession, url: str, limit: int = PAGE_BYTES) -> tuple[str, str]:
    """Веб-страница (адрес из чужого поста или выдачи поиска): (текст, Content-Type) — только публичный
    интернет, не больше limit байт, перенаправления проверяются."""
    raw, ctype = await _get(session, url, limit, "")
    charset = "utf-8"
    m = re.search(r"charset=([\w-]+)", ctype)
    if m:
        charset = m.group(1)
    try:
        return raw.decode(charset, errors="ignore"), ctype
    except LookupError:
        return raw.decode("utf-8", errors="ignore"), ctype


async def _get(session: aiohttp.ClientSession, url: str, limit: int, trusted: str) -> tuple[bytes, str]:
    for _ in range(MAX_REDIRECTS + 1):
        if not (trusted and _same_origin(url, trusted)):
            await check_url(url)
        async with session.get(url, allow_redirects=False) as resp:
            if resp.status in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                url = str(resp.url.join(URL(resp.headers["Location"])))
                continue
            resp.raise_for_status()
            if int(resp.headers.get("Content-Length") or 0) > limit:
                raise UnsafeURL(f"файл больше {limit / 1048576:.1f} МБ")
            chunks, size = [], 0
            async for chunk in resp.content.iter_chunked(1 << 16):
                size += len(chunk)
                if size > limit:
                    raise UnsafeURL(f"файл больше {limit / 1048576:.1f} МБ")
                chunks.append(chunk)
            return b"".join(chunks), resp.headers.get("Content-Type", "")
    raise UnsafeURL("слишком много перенаправлений")

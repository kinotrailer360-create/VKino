"""Загрузка шортса на YouTube прямо из бота (YouTube Data API v3).

Вход — как на телевизоре (OAuth device flow): бот показывает код, автор вводит его на google.com/device
с телефона и разрешает доступ к каналу. Бот хранит refresh-токен и дальше загружает сам.
Нужен OAuth-клиент Google типа «TVs and Limited Input devices»: YT_CLIENT_ID и YT_CLIENT_SECRET в .env.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

import aiohttp

DEVICE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
# для входа по коду Google разрешает общий доступ к YouTube (узкий youtube.upload там недоступен)
SCOPE = "https://www.googleapis.com/auth/youtube"


class YouTubeError(RuntimeError):
    pass


class LoginExpired(YouTubeError):
    """refresh-токен отозван или истёк — нужен новый вход по коду."""


async def _post(url: str, data: dict[str, str]) -> tuple[int, dict[str, Any]]:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        async with session.post(url, data=data) as resp:
            return resp.status, await resp.json(content_type=None)


async def device_start(client_id: str) -> dict[str, Any]:
    """{"device_code", "user_code", "verification_url", "interval", "expires_in"}."""
    status, data = await _post(DEVICE_URL, {"client_id": client_id, "scope": SCOPE})
    if status != 200:
        raise YouTubeError(f"Google не выдал код входа: {data.get('error_description') or data.get('error') or status}")
    return data


async def device_wait(client_id: str, secret: str, device: dict[str, Any]) -> str:
    """Ждём, пока автор введёт код и разрешит доступ; возвращает refresh-токен."""
    interval = int(device.get("interval") or 5)
    deadline = time.monotonic() + int(device.get("expires_in") or 1800)
    while time.monotonic() < deadline:
        await asyncio.sleep(interval)
        status, data = await _post(TOKEN_URL, {
            "client_id": client_id, "client_secret": secret, "device_code": device["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        if status == 200 and data.get("refresh_token"):
            return str(data["refresh_token"])
        error = data.get("error")
        if error == "slow_down":
            interval += 5
        elif error != "authorization_pending":
            raise YouTubeError(f"вход не удался: {data.get('error_description') or error or status}")
    raise YouTubeError("код входа истёк — нажми кнопку ещё раз")


async def access_token(client_id: str, secret: str, refresh: str) -> str:
    status, data = await _post(TOKEN_URL, {"client_id": client_id, "client_secret": secret,
                                           "refresh_token": refresh, "grant_type": "refresh_token"})
    if status == 200 and data.get("access_token"):
        return str(data["access_token"])
    if data.get("error") in ("invalid_grant", "unauthorized_client"):
        raise LoginExpired("вход в YouTube устарел")
    raise YouTubeError(f"Google не выдал доступ: {data.get('error_description') or data.get('error') or status}")


def _tags(raw: str) -> list[str]:
    """Теги в лимите YouTube (500 знаков на все): тег с пробелом YouTube считает в кавычках (+2 знака),
    между тегами — запятая. Без повторов."""
    out: list[str] = []
    size = 0
    for tag in (t.strip().lstrip("#")[:100] for t in raw.split(",")):
        if not tag or tag.lower() in {t.lower() for t in out}:
            continue
        cost = len(tag) + (2 if " " in tag else 0) + (1 if out else 0)
        if size + cost > 480:
            continue
        out.append(tag)
        size += cost
    return out


def no_links(text: str) -> str:
    """В описании канала ссылки запрещены (YouTube отклоняет или прячет их): «https://t.me/x» → «t.me/x»,
    ссылки на сам YouTube убираем целиком."""
    lines = [ln for ln in text.splitlines() if not re.search(r"(?:youtube\.com|youtu\.be)/", ln)]
    return re.sub(r"https?://(?:www\.)?", "", "\n".join(lines))


async def upload(token: str, video: Path, seo: dict[str, str], privacy: str = "public") -> dict[str, Any]:
    """Загрузить ролик; {"id", "privacy"} — privacy может оказаться private, пока проект Google не прошёл аудит."""
    meta = {
        "snippet": {"title": seo.get("title", "")[:100], "description": no_links(seo.get("description", ""))[:4900],
                    "tags": _tags(seo.get("tags", "")), "categoryId": "1", "defaultLanguage": "ru"},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    size = video.stat().st_size
    headers = {"Authorization": f"Bearer {token}", "X-Upload-Content-Type": "video/mp4",
               "X-Upload-Content-Length": str(size), "Content-Type": "application/json; charset=UTF-8"}
    # большой файл (выпуск на 8 минут — сотни МБ) отдаём потоком, время — по размеру (не меньше 1 МБ/с)
    timeout = aiohttp.ClientTimeout(total=600 + size / 1_000_000)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"},
                                data=json.dumps(meta), headers=headers) as resp:
            if resp.status != 200 or not resp.headers.get("Location"):
                raise YouTubeError(f"YouTube не принял загрузку: HTTP {resp.status} {(await resp.text())[:300]}")
            location = resp.headers["Location"]
        with video.open("rb") as fh:
            async with session.put(location, data=fh, headers={"Authorization": f"Bearer {token}",
                                                               "Content-Type": "video/mp4",
                                                               "Content-Length": str(size)}) as resp:
                body = await resp.json(content_type=None)
                if resp.status not in (200, 201) or not body.get("id"):
                    raise YouTubeError(f"загрузка не удалась: HTTP {resp.status} {str(body)[:300]}")
    return {"id": body["id"], "privacy": (body.get("status") or {}).get("privacyStatus", privacy)}


async def set_thumbnail(token: str, video_id: str, image: Path) -> None:
    """Своя обложка ролика. YouTube разрешает её только подтверждённым каналам (подтверждение по телефону
    в YouTube Studio) — иначе ошибка 403, обложку тогда ставят вручную."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        async with session.post("https://www.googleapis.com/upload/youtube/v3/thumbnails/set",
                                params={"videoId": video_id, "uploadType": "media"}, data=image.read_bytes(),
                                headers={"Authorization": f"Bearer {token}", "Content-Type": "image/jpeg"}) as resp:
            if resp.status not in (200, 201):
                raise YouTubeError(f"обложка: HTTP {resp.status} {(await resp.text())[:200]}")

"""Загрузка шортса на YouTube прямо из бота (YouTube Data API v3).

Вход — как на телевизоре (OAuth device flow): бот показывает код, автор вводит его на google.com/device
с телефона и разрешает доступ к каналу. Бот хранит refresh-токен и дальше загружает сам.
Нужен OAuth-клиент Google типа «TVs and Limited Input devices»: YT_CLIENT_ID и YT_CLIENT_SECRET в .env.
"""

from __future__ import annotations

import asyncio
import json
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
    out: list[str] = []
    for tag in (t.strip().lstrip("#") for t in raw.split(",")):
        if tag and len(",".join(out + [tag])) <= 480:
            out.append(tag)
    return out


async def upload(token: str, video: Path, seo: dict[str, str], privacy: str = "public") -> dict[str, Any]:
    """Загрузить ролик; {"id", "privacy"} — privacy может оказаться private, пока проект Google не прошёл аудит."""
    meta = {
        "snippet": {"title": seo.get("title", "")[:100], "description": seo.get("description", "")[:4900],
                    "tags": _tags(seo.get("tags", "")), "categoryId": "1", "defaultLanguage": "ru"},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    data = video.read_bytes()
    headers = {"Authorization": f"Bearer {token}", "X-Upload-Content-Type": "video/mp4",
               "X-Upload-Content-Length": str(len(data)), "Content-Type": "application/json; charset=UTF-8"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600)) as session:
        async with session.post(UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"},
                                data=json.dumps(meta), headers=headers) as resp:
            if resp.status != 200 or not resp.headers.get("Location"):
                raise YouTubeError(f"YouTube не принял загрузку: HTTP {resp.status} {(await resp.text())[:300]}")
            location = resp.headers["Location"]
        async with session.put(location, data=data, headers={"Authorization": f"Bearer {token}",
                                                             "Content-Type": "video/mp4"}) as resp:
            body = await resp.json(content_type=None)
            if resp.status not in (200, 201) or not body.get("id"):
                raise YouTubeError(f"загрузка не удалась: HTTP {resp.status} {str(body)[:300]}")
    return {"id": body["id"], "privacy": (body.get("status") or {}).get("privacyStatus", privacy)}

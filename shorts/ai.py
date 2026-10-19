"""Gemini: подбор кадра под текст факта и пересказ новостей для озвучки.

Ключ — GEMINI_API_KEY (бесплатный, aistudio.google.com). Без ключа или при ошибке
функции возвращают None, и ролик собирается без ИИ.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
from typing import Any

import aiohttp
from PIL import Image

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MODELS = "https://generativelanguage.googleapis.com/v1beta/models"
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()

log = logging.getLogger("shorts")


class AIError(RuntimeError):
    pass


last_error: str | None = None  # причина последнего сбоя — бот показывает её админу


def _thumb(img: Image.Image) -> dict[str, Any]:
    """Уменьшенная копия кадра для запроса — экономим лимиты и трафик."""
    small = img.copy()
    small.thumbnail((448, 448))
    buf = io.BytesIO()
    small.convert("RGB").save(buf, "JPEG", quality=80)
    return {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(buf.getvalue()).decode()}}


def _version(name: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", name))  # 3.10 новее 3.9


async def _flash_models(session: aiohttp.ClientSession, key: str, lite: bool = False) -> list[str]:
    """Стабильные модели Flash, доступные этому ключу, новейшие первыми (lite=True — и облегчённые)."""
    async with session.get(MODELS, params={"pageSize": 200}, headers={"x-goog-api-key": key}) as resp:
        data = await resp.json(content_type=None)
    names = [
        str(m.get("name", "")).removeprefix("models/")
        for m in (data.get("models") or [] if isinstance(data, dict) else [])
        if "generateContent" in (m.get("supportedGenerationMethods") or [])
    ]
    skip = ("image", "tts", "live", "audio", "preview", "exp", "thinking", "latest") + (() if lite else ("lite",))
    flash = [n for n in names if n.startswith("gemini-") and "flash" in n and not any(w in n for w in skip)]
    # при равной версии полная модель раньше облегчённой
    return sorted(flash, key=lambda n: (_version(n), "lite" not in n), reverse=True)


BUSY = {429, 500, 503}  # перегрузка у Google — обычно проходит за секунды
RETRY_DELAYS = (4, 10)


async def _post(session: aiohttp.ClientSession, key: str, model: str, body: dict[str, Any]) -> tuple[int, Any]:
    """Запрос к модели; при перегрузке ждём и повторяем."""
    for delay in (*RETRY_DELAYS, None):
        async with session.post(API.format(model=model), json=body, headers={"x-goog-api-key": key}) as resp:
            status, data = resp.status, await resp.json(content_type=None)
        if status not in BUSY or delay is None:
            return status, data
        log.info("shorts: Gemini %s busy (HTTP %s), retry in %ss", model, status, delay)
        await asyncio.sleep(delay)
    raise AssertionError("unreachable")


async def _ask(key: str, parts: list[dict[str, Any]]) -> Any:
    global MODEL
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
    }
    timeout = aiohttp.ClientTimeout(total=180)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        model = MODEL
        status, data = await _post(session, key, model, body)
        if status == 404:
            # модель сняли с поддержки — переходим на новейшую Flash насовсем
            newer = next(iter(await _flash_models(session, key)), None)
            if newer and newer != MODEL:
                log.warning("shorts: Gemini model %s unavailable, switching to %s", MODEL, newer)
                MODEL = model = newer
                status, data = await _post(session, key, model, body)
        if status in BUSY:
            # модель перегружена — разово пробуем другую Flash (в т.ч. облегчённую)
            backup = next((m for m in await _flash_models(session, key, lite=True) if m != model), None)
            if backup:
                log.warning("shorts: Gemini %s overloaded, trying %s", model, backup)
                model = backup
                status, data = await _post(session, key, model, body)
        if status != 200:
            msg = (data.get("error") or {}).get("message") if isinstance(data, dict) else data
            raise AIError(f"Gemini HTTP {status} ({model}): {str(msg)[:200]}")
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise AIError(f"Gemini: неожиданный ответ ({exc})") from exc


async def match_images(key: str, texts: list[str], images: list[Image.Image]) -> list[int] | None:
    """Для каждого текста — номер самого подходящего кадра (или -1, если подходящего нет)."""
    if not key or not texts or not images:
        return None
    parts: list[dict[str, Any]] = [{"text": (
        "Ты монтажёр коротких роликов о кино. Ниже пронумерованные кадры из фильма, затем "
        "пронумерованные факты. Для каждого факта выбери кадр, на котором видно то, о чём факт: "
        "предмет, место, сцену, персонажа. Старайся не повторять кадры. Если ни один кадр не "
        "подходит по смыслу, поставь -1. Ответь JSON: {\"choices\": [номер кадра для факта 1, ...]}."
    )}]
    for i, img in enumerate(images):
        parts += [{"text": f"Кадр {i}:"}, _thumb(img)]
    parts.append({"text": "Факты:\n" + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))})
    try:
        data = await _ask(key, parts)
        raw = list(data["choices"])[: len(texts)]
    except Exception as exc:  # noqa: BLE001
        global last_error
        last_error = str(exc)[:300]
        log.warning("shorts: Gemini match_images failed: %s", exc)
        return None
    choices = []
    for c in raw:  # мусор в отдельном ответе — только этот факт без подбора
        try:
            choices.append(int(c))
        except (TypeError, ValueError):
            choices.append(-1)
    choices += [-1] * (len(texts) - len(choices))
    return [c if 0 <= c < len(images) else -1 for c in choices]


async def retell_news(key: str, items: list[tuple[str, str, str]]) -> list[str] | None:
    """(заголовок, лид, источник) -> текст для озвучки своими словами, 1–2 предложения."""
    if not key or not items:
        return None
    listing = "\n".join(f"{i + 1}. Заголовок: {t}\n   Подробности: {lead or '—'}\n   Источник: {src}"
                        for i, (t, lead, src) in enumerate(items))
    prompt = (
        "Ты ведущий короткого видео «Новости кино». Перескажи каждую новость своими словами "
        "по-русски: 1–2 коротких живых предложения, до 200 символов, для озвучки голосом. "
        "Используй только факты из заголовка и подробностей, ничего не выдумывай. Названия "
        "фильмов — в «ёлочках». Без эмодзи. Ответь JSON: {\"texts\": [\"...\", ...]} "
        "в том же порядке.\n\n" + listing
    )
    try:
        data = await _ask(key, [{"text": prompt}])
        texts = [str(t).strip() for t in data["texts"]]
    except Exception as exc:  # noqa: BLE001
        global last_error
        last_error = str(exc)[:300]
        log.warning("shorts: Gemini retell_news failed: %s", exc)
        return None
    if len(texts) != len(items) or not all(texts):
        return None
    return [t[:260] for t in texts]

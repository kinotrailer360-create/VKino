"""Gemini: подбор кадра под текст факта и пересказ новостей для озвучки.

Ключ — GEMINI_API_KEY (бесплатный, aistudio.google.com). Без ключа или при ошибке
функции возвращают None, и ролик собирается без ИИ.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
from typing import Any

import aiohttp
from PIL import Image

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()

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


async def _ask(key: str, parts: list[dict[str, Any]]) -> Any:
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
    }
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(API.format(model=MODEL), json=body, headers={"x-goog-api-key": key}) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                msg = (data.get("error") or {}).get("message") if isinstance(data, dict) else data
                raise AIError(f"Gemini HTTP {resp.status}: {str(msg)[:200]}")
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

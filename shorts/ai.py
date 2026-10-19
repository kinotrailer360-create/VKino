"""ИИ: подбор кадра под текст факта и пересказ новостей для озвучки.

Основной — Gemini, ключ GEMINI_API_KEY (бесплатный, aistudio.google.com). Резерв на случай,
когда Gemini перегружен или недоступен, — любой OpenAI-совместимый сервис с поддержкой картинок,
по умолчанию Groq (бесплатный ключ на console.groq.com): RESERVE_AI_KEY, RESERVE_AI_MODEL,
RESERVE_AI_URL. Без ключей или при ошибке функции возвращают None, и ролик собирается без ИИ.
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
from PIL import Image, ImageDraw, ImageFont

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MODELS = "https://generativelanguage.googleapis.com/v1beta/models"
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()

RESERVE_URL = os.getenv("RESERVE_AI_URL", "https://api.groq.com/openai/v1/chat/completions").strip()
RESERVE_MODEL = os.getenv("RESERVE_AI_MODEL", "qwen/qwen3.8-27b").strip()
RESERVE_KEY = os.getenv("RESERVE_AI_KEY", "").strip()
FONT = os.path.join(os.path.dirname(__file__), "assets", "Oswald.ttf")

log = logging.getLogger("shorts")


def available(key: str) -> bool:
    """Есть ли хоть один ИИ: ключ Gemini или резервный."""
    return bool(key or RESERVE_KEY)


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


def _sheet(images: list[Image.Image]) -> Image.Image:
    """Все кадры на одном листе с крупными номерами — резервные модели берут 1–3 картинки за раз."""
    cols = 6 if len(images) > 16 else 4
    tw, th = 320, 180
    rows = (len(images) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tw, rows * th), "black")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.truetype(FONT, 44)
    for i, img in enumerate(images):
        tile = img.convert("RGB").copy()
        tile.thumbnail((tw - 6, th - 6))
        x, y = (i % cols) * tw, (i // cols) * th
        sheet.paste(tile, (x + (tw - tile.width) // 2, y + (th - tile.height) // 2))
        draw.rectangle((x + 4, y + 4, x + 78, y + 58), fill="#ffd400")
        draw.text((x + 41, y + 31), str(i), font=font, fill="black", anchor="mm")
    return sheet


def _json_from(text: str) -> Any:
    """JSON из ответа модели: без рассуждений <think> и обёрток ```json."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise AIError("резервный ИИ: в ответе нет JSON")
    return json.loads(text[start:end + 1])


async def _ask_reserve(prompt: str, image: Image.Image | None = None) -> Any:
    """Запрос к резервному OpenAI-совместимому ИИ (по умолчанию Groq)."""
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    if image is not None:
        small = image.copy()
        small.thumbnail((1920, 1920))
        buf = io.BytesIO()
        small.save(buf, "JPEG", quality=80)
        url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        content.append({"type": "image_url", "image_url": {"url": url}})
    body = {"model": RESERVE_MODEL, "temperature": 0.2,
            "messages": [{"role": "user", "content": content}]}
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for delay in (*RETRY_DELAYS, None):
            async with session.post(RESERVE_URL, json=body,
                                    headers={"Authorization": f"Bearer {RESERVE_KEY}"}) as resp:
                status, data = resp.status, await resp.json(content_type=None)
            if status not in BUSY or delay is None:
                break
            await asyncio.sleep(delay)
    if status != 200:
        msg = (data.get("error") or {}).get("message") if isinstance(data, dict) else data
        raise AIError(f"резервный ИИ HTTP {status} ({RESERVE_MODEL}): {str(msg)[:200]}")
    try:
        return _json_from(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise AIError(f"резервный ИИ: неожиданный ответ ({exc})") from exc


async def _with_reserve(key: str, gemini_parts: list[dict[str, Any]], prompt: str,
                        image: Image.Image | None = None) -> Any:
    """Сначала Gemini; не вышло — резервный ИИ. Ошибка — с причинами обоих."""
    errors = []
    if key:
        try:
            return await _ask(key, gemini_parts)
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
            log.warning("shorts: Gemini failed: %s", exc)
    if RESERVE_KEY:
        try:
            result = await _ask_reserve(prompt, image)
            log.info("shorts: reserve AI (%s) answered", RESERVE_MODEL)
            return result
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
            log.warning("shorts: reserve AI failed: %s", exc)
    raise AIError("; ".join(errors) or "нет ключа ИИ")


async def match_images(key: str, texts: list[str], images: list[Image.Image]) -> list[int] | None:
    """Для каждого текста — номер самого подходящего кадра (или -1, если подходящего нет)."""
    if not available(key) or not texts or not images:
        return None
    task = ("Для каждого факта выбери кадр, на котором видно то, о чём факт: предмет, место, сцену, "
            "персонажа. Старайся не повторять кадры. Если ни один кадр не подходит по смыслу, поставь -1. "
            "Ответь только JSON: {\"choices\": [номер кадра для факта 1, ...]}.")
    facts = "Факты:\n" + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
    parts: list[dict[str, Any]] = [{"text": (
        "Ты монтажёр коротких роликов о кино. Ниже пронумерованные кадры из фильма, затем "
        "пронумерованные факты. " + task)}]
    for i, img in enumerate(images):
        parts += [{"text": f"Кадр {i}:"}, _thumb(img)]
    parts.append({"text": facts})
    prompt = ("Ты монтажёр коротких роликов о кино. На картинке — кадры из фильма, номер каждого "
              "в жёлтом квадрате в углу кадра. " + task + "\n\n" + facts)
    try:
        data = await _with_reserve(key, parts, prompt, _sheet(images) if RESERVE_KEY else None)
        raw = list(data["choices"])[: len(texts)]
    except Exception as exc:  # noqa: BLE001
        global last_error
        last_error = str(exc)[:300]
        log.warning("shorts: AI match_images failed: %s", exc)
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
    if not available(key) or not items:
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
        data = await _with_reserve(key, [{"text": prompt}], prompt)
        texts = [str(t).strip() for t in data["texts"]]
    except Exception as exc:  # noqa: BLE001
        global last_error
        last_error = str(exc)[:300]
        log.warning("shorts: AI retell_news failed: %s", exc)
        return None
    if len(texts) != len(items) or not all(texts):
        return None
    return [t[:260] for t in texts]

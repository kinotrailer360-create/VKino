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
import time
from datetime import date
from typing import Any

import aiohttp
from PIL import Image, ImageDraw, ImageFont

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MODELS = "https://generativelanguage.googleapis.com/v1beta/models"
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()

RESERVE_URL = os.getenv("RESERVE_AI_URL", "https://api.groq.com/openai/v1/chat/completions").strip()
RESERVE_MODEL = os.getenv("RESERVE_AI_MODEL", "qwen/qwen3.8-27b").strip()
log = logging.getLogger("shorts")

# русские буквы, похожие на латинские, — если имя переменной набрано на русской раскладке
_LOOKALIKE = str.maketrans("АВЕКМНОРСТУХ", "ABEKMHOPCTYX")


def _env_key(*names: str) -> str:
    """Ключ из переменной окружения; находит имя и с русскими «двойниками» букв или пробелами."""
    for name in names:
        if os.getenv(name, "").strip():
            return os.getenv(name, "").strip().strip("\"'")
    for env, value in os.environ.items():
        if env.strip().upper().translate(_LOOKALIKE) in names and value.strip():
            log.warning("shorts: env %r looks like %s — using it", env, names[0])
            return value.strip().strip("\"'")
    return ""


RESERVE_KEY = _env_key("RESERVE_AI_KEY", "GROQ_API_KEY")
# третий ИИ — BeatAPI (beatapi.io): OpenAI-совместимый, есть бесплатные модели (id на «-free»)
BEAT_KEY = _env_key("BEAT_API_KEY", "BEATAPI_API_KEY")
BEAT_URL = os.getenv("BEAT_API_URL", "https://api.beatapi.io/v1").rstrip("/")
BEAT_MODEL = os.getenv("BEAT_API_MODEL", "").strip()  # пусто — бесплатная модель, предпочтительно DeepSeek
BEAT_HEADERS = {"User-Agent": "KinoMir360-bot/1.0"}  # без своего User-Agent их защита отвечает 403
FONT = os.path.join(os.path.dirname(__file__), "assets", "Oswald.ttf")


def available(key: str) -> bool:
    """Есть ли хоть один ИИ: ключ Gemini или резервный."""
    return bool(key or RESERVE_KEY or BEAT_KEY)


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


def _quota_value(data: Any) -> str:
    """Размер лимита из ответа Google («20 в день»), если он там есть."""
    details = (data.get("error") or {}).get("details") or [] if isinstance(data, dict) else []
    for d in details:
        for v in (d.get("violations") or []) if isinstance(d, dict) else []:
            if isinstance(v, dict) and v.get("quotaValue") is not None:
                per = "день" if "PerDay" in str(v.get("quotaId")) else "минуту"
                return f"{v['quotaValue']} в {per}"
    return ""


def _quota(status: int, data: Any) -> tuple[str, float]:
    """Какой лимит Gemini кончился: ("day", 0) — на сегодня, ("minute", сек) — в минуту, подождать
    столько, сколько просит Google; ("", 0) — это не лимит. Google пишет это в error.details."""
    if status != 429:
        return "", 0.0
    details = (data.get("error") or {}).get("details") or [] if isinstance(data, dict) else []
    ids = " ".join(str(v.get("quotaId") or "") for d in details if isinstance(d, dict)
                   for v in d.get("violations") or [] if isinstance(v, dict))
    delay = next((d.get("retryDelay") for d in details if isinstance(d, dict) and d.get("retryDelay")), "")
    m = re.match(r"([\d.]+)s", str(delay))
    wait = float(m.group(1)) if m else 0.0
    if "PerDay" in ids:
        return "day", 0.0
    if "PerMinute" in ids or wait:
        return "minute", min(max(wait, 5.0), 60.0)
    text = json.dumps(data, ensure_ascii=False).lower()
    return ("day", 0.0) if "quota" in text else ("", 0.0)


# у бесплатного Gemini ещё и лимит запросов в минуту: на ролик их 5–7 подряд — разносим во времени
GEMINI_GAP = 4.0
_gemini_lock = asyncio.Lock()
_gemini_last = 0.0


# Gemini исчерпал лимит — до этого времени (time.monotonic) не обращаемся, сразу идём к запасным ИИ;
# отдельно для обычных запросов и для поиска Google (у поиска свой лимит)
QUOTA_PAUSE = 30 * 60
_quota_until: dict[tuple[str, bool], float] = {}  # (ключ, поиск) -> до какого времени не трогаем


class QuotaError(AIError):
    """У этого ключа Gemini кончился лимит — пробуем следующий ключ."""


# запасные ключи Gemini с других аккаунтов Google: у каждого свой бесплатный лимит
EXTRA_KEYS = [k for k in (_env_key("GEMINI_API_KEY_2"), _env_key("GEMINI_API_KEY_3")) if k]
last_key_no = 0  # каким по счёту ключом ответил Gemini в последний раз — для «Проверить ИИ»


def gemini_keys(key: str) -> list[str]:
    return list(dict.fromkeys(k for k in (key, *EXTRA_KEYS) if k))


async def _post(session: aiohttp.ClientSession, key: str, model: str, body: dict[str, Any],
                wait_minute: bool = True) -> tuple[int, Any]:
    """Запрос к модели; при перегрузке ждём и повторяем; кончился лимит в минуту — ждём, сколько просит Google
    (wait_minute=False — не ждём: есть другой ключ)."""
    global _gemini_last
    minute_waits = 2 if wait_minute else 0
    for delay in (*RETRY_DELAYS, None):
        async with _gemini_lock:  # не чаще раза в GEMINI_GAP секунд
            pause = _gemini_last + GEMINI_GAP - time.monotonic()
            if pause > 0:
                await asyncio.sleep(pause)
            _gemini_last = time.monotonic()
        async with session.post(API.format(model=model), json=body, headers={"x-goog-api-key": key}) as resp:
            status, data = resp.status, await resp.json(content_type=None)
        kind, wait = _quota(status, data)
        while kind == "minute" and minute_waits:
            minute_waits -= 1
            log.info("shorts: Gemini per-minute limit, waiting %.0fs", wait)
            await asyncio.sleep(wait)
            async with session.post(API.format(model=model), json=body, headers={"x-goog-api-key": key}) as resp:
                status, data = resp.status, await resp.json(content_type=None)
            kind, wait = _quota(status, data)
        if status not in BUSY or delay is None or kind:
            return status, data
        log.info("shorts: Gemini %s busy (HTTP %s), retry in %ss", model, status, delay)
        await asyncio.sleep(delay)
    raise AssertionError("unreachable")


async def _ask(key: str, parts: list[dict[str, Any]], search: bool = False) -> Any:
    """search=True — модель сама ищет в Google (ответ JSON-текстом, источники — в ключе "_sources").
    У ключа кончился лимит — пробуем запасные ключи (GEMINI_API_KEY_2, _3), потом ошибка."""
    global last_key_no
    keys = gemini_keys(key)
    errors: list[str] = []
    for n, k in enumerate(keys, 1):
        until = _quota_until.get((k, search), 0.0)
        if time.monotonic() < until:
            errors.append(f"ключ {n}: лимит исчерпан, снова через {round((until - time.monotonic()) / 60)} мин")
            continue
        try:
            result = await _ask_key(k, parts, search, wait_minute=n == len(keys))
            last_key_no = n
            return result
        except QuotaError as exc:
            errors.append(f"ключ {n}: {exc}")
            log.warning("shorts: Gemini key %s out of quota%s, next key", n, " (search)" if search else "")
    raise AIError(f"Gemini{' (поиск Google)' if search else ''}: " + "; ".join(errors or ["нет ключа"]))


async def _ask_key(key: str, parts: list[dict[str, Any]], search: bool, wait_minute: bool) -> Any:
    global MODEL
    body: dict[str, Any] = {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
    }
    if search:  # с поиском строгий JSON-режим недоступен — просим JSON в тексте
        body = {"contents": [{"parts": parts}], "tools": [{"google_search": {}}],
                "generationConfig": {"temperature": 0.3}}
    timeout = aiohttp.ClientTimeout(total=180)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        model = MODEL
        status, data = await _post(session, key, model, body, wait_minute)
        if status == 404:
            # модель сняли с поддержки — переходим на новейшую Flash насовсем
            newer = next(iter(await _flash_models(session, key)), None)
            if newer and newer != MODEL:
                log.warning("shorts: Gemini model %s unavailable, switching to %s", MODEL, newer)
                MODEL = model = newer
                status, data = await _post(session, key, model, body)
        if status in BUSY and not _quota(status, data)[0]:
            # модель перегружена — разово пробуем другую Flash (в т.ч. облегчённую)
            backup = next((m for m in await _flash_models(session, key, lite=True) if m != model), None)
            if backup:
                log.warning("shorts: Gemini %s overloaded, trying %s", model, backup)
                model = backup
                status, data = await _post(session, key, model, body)
        if status != 200:
            msg = (data.get("error") or {}).get("message") if isinstance(data, dict) else data
            kind, _ = _quota(status, data)
            if kind:  # лимит этого ключа: на день — пауза 30 мин, в минуту — 1 мин; пробуем другой ключ
                _quota_until[(key, search)] = time.monotonic() + (QUOTA_PAUSE if kind == "day" else 60)
                value = _quota_value(data)
                raise QuotaError(f"лимит {'на день' if kind == 'day' else 'в минуту'} ({model}"
                                 + (f", разрешено {value}" if value else "") + ")")
            raise AIError(f"Gemini HTTP {status} ({model}): {str(msg)[:200]}")
    try:
        if not search:
            return json.loads(data["candidates"][0]["content"]["parts"][0]["text"])
        cand = data["candidates"][0]
        result = _json_from("".join(str(p.get("text") or "") for p in cand["content"]["parts"]))
        chunks = (cand.get("groundingMetadata") or {}).get("groundingChunks") or []
        sources = [str((c.get("web") or {}).get("title") or "") for c in chunks]
        if isinstance(result, dict):
            result["_sources"] = list(dict.fromkeys(x for x in sources if x))
        return result
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
    text = str(text or "")
    if "</think>" in text:  # рассуждения модели — до ответа
        text = text.rsplit("</think>", 1)[1]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise AIError(f"в ответе нет JSON: «{text.strip()[:80]}»")
    try:
        return json.loads(text[start:end + 1])
    except ValueError:  # лишняя запятая в конце списка — частая ошибка моделей
        return json.loads(re.sub(r",\s*([\]}])", r"\1", text[start:end + 1]))


# у бесплатного Groq лимит ~7000 токенов в минуту (≈ 4 символа на токен по-русски): длинный материал
# (статья Википедии, посты каналов) сокращаем — начало с заданием и конец с форматом ответа остаются
RESERVE_MAX_CHARS = 16000


def _fit(prompt: str, limit: int = RESERVE_MAX_CHARS) -> str:
    if len(prompt) <= limit:
        return prompt
    return prompt[: limit - 2500] + "\n…\n" + prompt[-2500:]


async def _ask_reserve(prompt: str, image: Image.Image | None = None) -> Any:
    """Запрос к резервному OpenAI-совместимому ИИ (по умолчанию Groq)."""
    content: list[dict[str, Any]] = [{"type": "text", "text": _fit(prompt)}]
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
    if BEAT_KEY:
        try:
            result = await _ask_beat(prompt, image)
            log.info("shorts: BeatAPI (%s) answered", _beat_model or BEAT_MODEL)
            return result
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
            log.warning("shorts: BeatAPI failed: %s", exc)
    if not RESERVE_KEY and not BEAT_KEY:
        errors.append("резервный ИИ не подключён: бот не видит переменную RESERVE_AI_KEY")
    raise AIError("; ".join(errors))


_beat_model = ""


async def _beat_get(session: aiohttp.ClientSession, path: str) -> Any:
    async with session.get(f"{BEAT_URL}{path}",
                           headers={**BEAT_HEADERS, "Authorization": f"Bearer {BEAT_KEY}"}) as resp:
        data = await resp.json(content_type=None)
        if resp.status != 200:
            err = (data.get("error") or {}) if isinstance(data, dict) else {}
            raise AIError(f"BeatAPI HTTP {resp.status} {err.get('code') or ''}: {str(err.get('message') or data)[:200]}")
        return data


async def beat_free_models() -> list[str]:
    """Бесплатные модели BeatAPI прямо сейчас (список меняется, id заканчиваются на «-free»)."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        data = await _beat_get(session, "/models")
    ids = [str(m.get("id")) for m in (data.get("data") if isinstance(data, dict) else None) or []
           if isinstance(m, dict) and m.get("id")]
    return [i for i in ids if i.endswith("-free") and not i.startswith("jev")]


async def beat_status() -> dict[str, Any]:
    """Для проверки в боте: баланс и бесплатные модели (без платных запросов)."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        usage = await _beat_get(session, "/usage")
    return {"balance": usage.get("credit_balance") if isinstance(usage, dict) else None,
            "free": await beat_free_models(), "model": BEAT_MODEL or await _pick_beat_model()}


async def probe(key: str) -> list[tuple[str, bool, str]]:
    """Для кнопки «Проверить ИИ»: короткий настоящий запрос к каждому ИИ — [(название, работает, подробности)]."""
    out: list[tuple[str, bool, str]] = []

    async def run(name: str, coro: Any, show: Any) -> None:
        start = asyncio.get_running_loop().time()
        try:
            res = await asyncio.wait_for(coro, timeout=90)
            out.append((name, True, f"{show(res)} · {asyncio.get_running_loop().time() - start:.0f} с"))
        except Exception as exc:  # noqa: BLE001
            text = str(exc) or type(exc).__name__
            if "429" in text or "RESOURCE_EXHAUSTED" in text or "quota" in text.lower() or "лимит" in text:
                text = "лимит запросов исчерпан (бесплатный тариф) — " + text
            out.append((name, False, text[:300]))

    ask = 'Ответь только JSON: {"ok": true}'
    find = [{"text": "Найди в Google, какой фильм получил «Оскар» за лучший фильм в 2025 году. "
                     'Ответь только JSON: {"film": "название по-русски"}'}]
    # каждый ключ отдельно и напрямую (без паузы после лимита) — видно, какой ключ что умеет
    for n, k in enumerate(gemini_keys(key), 1):
        await run(f"Gemini ключ {n}", _ask_key(k, [{"text": ask}], False, wait_minute=False),
                  lambda r: "отвечает")
        await run(f"Gemini ключ {n} + поиск Google", _ask_key(k, find, True, wait_minute=False),
                  lambda r: f"нашёл «{r.get('film', '?')}», источников: {len(r.get('_sources') or [])}")
    if RESERVE_KEY:
        await run("Groq", _ask_reserve(ask), lambda r: "отвечает")
    return out


async def extract_plan(key: str, text: str, max_films: int = 10) -> dict[str, Any]:
    """Своя подборка из любого текста автора (пост, список с описаниями): тема и фильмы/сериалы с короткой
    фразой для озвучки по описанию автора. {"title", "films": [{"ru", "en", "year", "note"}]}."""
    if not available(key):
        raise AIError("нет ключа GEMINI_API_KEY или RESERVE_AI_KEY")
    prompt = (
        "Ниже текст автора про подборку фильмов и сериалов (пост, список, заметки). Вытащи из него:\n"
        "1) title — короткую тему ролика по-русски, 3–6 слов, без эмодзи (например «Онлайн-премьеры недели»);\n"
        f"2) films — до {max_films} фильмов и сериалов в том же порядке, что в тексте: ru — название по-русски "
        "как в тексте, en — оригинальное название, если знаешь, year — год, если указан или известен, "
        "note — одна-две короткие фразы для озвучки (до 150 символов): сезон и где выходит, если указано, "
        "и о чём — пересказом описания автора, живо, без спойлеров, только факты из текста. Названия "
        "сервисов пиши как в тексте (Netflix, Apple TV). Не выдумывай фильмы, которых нет в тексте." + GRAMMAR +
        '\n\nОтветь только JSON: {"title": "...", "films": [{"ru": "...", "en": "...", "year": 2025, '
        '"note": "..."}]}\n\nТЕКСТ:\n' + text.strip()[:9000]
    )
    data = await _with_reserve(key, [{"text": prompt}], prompt)
    films = []
    for f in (data.get("films") if isinstance(data, dict) else None) or []:
        if isinstance(f, dict) and str(f.get("ru") or f.get("en") or "").strip():
            year = f.get("year")
            films.append({"ru": str(f.get("ru") or "").strip(" «»\""), "en": str(f.get("en") or "").strip(" «»\""),
                          "year": int(year) if str(year or "").isdigit() else None,
                          "note": re.sub(r"\s+", " ", str(f.get("note") or "")).strip()[:220]})
    if len(films) < 3:
        raise AIError("ИИ не нашёл в тексте хотя бы 3 фильма или сериала")
    title = re.sub(r"\s+", " ", str(data.get("title") or "")).strip(" .!«»\"") or "Подборка фильмов"
    return {"title": title, "films": films[:max_films]}


async def identify_film(key: str, query: str, posts: str = "") -> list[dict[str, Any]]:
    """Какой фильм или сериал имеет в виду автор («Глиноликий» → «Клейфейс», Clayface, 2026): ИИ ищет в Google
    и смотрит посты Telegram-каналов. До 3 вариантов [{"ru", "en", "year"}], самый вероятный первым."""
    if not available(key):
        return []
    prompt = (
        f"Автор ищет фильм или сериал по названию «{query}». Название может быть неофициальным, переводом, "
        "сленгом, именем персонажа или с ошибкой. Определи, какой это фильм или сериал, и назови его "
        "официальное русское название (как на Кинопоиске и в российском прокате), оригинальное название "
        "и год выхода. Если вариантов несколько — до трёх, самый вероятный первым. Не выдумывай."
        + ("\n\nПосты из Telegram-каналов о кино, где встречается это название:\n" + posts[:6000] if posts else "")
        + '\n\nОтветь только JSON: {"films": [{"ru": "...", "en": "...", "year": 2026}]}'
    )
    data = None
    if key:
        try:
            data = await _ask(key, [{"text": "Поищи в интернете. " + prompt}], search=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("shorts: identify film via search failed: %s", exc)
    if data is None:
        try:
            data = await _with_reserve(key, [{"text": prompt}], prompt)
        except Exception as exc:  # noqa: BLE001
            log.warning("shorts: identify film failed: %s", exc)
            return []
    out = []
    for f in (data.get("films") if isinstance(data, dict) else None) or []:
        if not isinstance(f, dict):
            continue
        year = f.get("year")
        out.append({"ru": str(f.get("ru") or "").strip(" «»\""), "en": str(f.get("en") or "").strip(" «»\""),
                    "year": int(year) if str(year or "").isdigit() else None})
    return [f for f in out if f["ru"] or f["en"]][:3]


async def proofread(key: str, texts: list[str]) -> list[str]:
    """Корректор: исправляет грамматику (род, число, падеж, предлоги) и написание имён в готовых фразах,
    не меняя смысла. Один запрос на все фразы: Gemini, если лимит есть; иначе бесплатная Gemini через
    BeatAPI (по-русски пишет лучше), в крайнем случае Groq. Не вышло — фразы как были."""
    texts = [t for t in texts]
    if not texts or not available(key):
        return texts
    listing = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
    prompt = (
        "Ты корректор русского текста для озвучки. Исправь в каждой фразе только ошибки: род, число, падеж, "
        "согласование, предлоги («интерес к новой экранизации», «сыграют Дамблдора и Снейпа», «среди сорока "
        "тысяч участников»). Иностранные имена, издания и названия — так, как их пишут российские СМИ и "
        "русский дубляж (The Times → «Таймс», Gilderoy Lockhart → Златопуст Локонс, Leavesden → «Ливсден»). "
        "Не меняй смысл, факты, цифры, порядок и стиль, ничего не добавляй и не убирай. Безошибочную фразу "
        "оставь как есть. Ответь только JSON: {\"texts\": [\"...\", ...]} — столько же фраз, в том же порядке."
        "\n\n" + listing
    )
    attempts = []
    if key:
        attempts.append(("Gemini", lambda: _ask(key, [{"text": prompt}])))
    if BEAT_KEY:
        async def beat_gemini() -> Any:
            free = await beat_free_models()
            model = next((m for m in free if "gemini" in m.lower()), None)
            return await _ask_beat(prompt, model=model)
        attempts.append(("BeatAPI", beat_gemini))
    if RESERVE_KEY:
        attempts.append(("Groq", lambda: _ask_reserve(prompt)))
    for name, call in attempts:
        try:
            data = await asyncio.wait_for(call(), timeout=120)
            out = [str(t).strip() for t in (data.get("texts") if isinstance(data, dict) else None) or []]
        except Exception as exc:  # noqa: BLE001
            log.warning("shorts: proofread via %s failed: %s", name, exc)
            continue
        if len(out) != len(texts):
            log.warning("shorts: proofread via %s returned %s of %s", name, len(out), len(texts))
            continue
        # корректор не переписывает: заметно другая длина — оставляем исходную фразу
        fixed = [new if new and 0.75 * len(old) <= len(new) <= 1.3 * len(old) + 10 else old
                 for old, new in zip(texts, out)]
        log.info("shorts: proofread via %s: %s of %s changed", name, sum(a != b for a, b in zip(texts, fixed)),
                 len(texts))
        return fixed
    return texts


async def _pick_beat_model() -> str:
    global _beat_model
    if not _beat_model:
        free = await beat_free_models()
        if not free:
            raise AIError("BeatAPI: бесплатных моделей сейчас нет — задайте BEAT_API_MODEL")
        _beat_model = next((m for m in free if "deepseek" in m.lower()), free[0])
    return _beat_model


async def _ask_beat(prompt: str, image: Image.Image | None = None, model: str | None = None) -> Any:
    """Запрос к BeatAPI (/v1/chat/completions). Бесплатные модели — 1 запрос в минуту до пополнения."""
    model = model or BEAT_MODEL or await _pick_beat_model()
    content: list[dict[str, Any]] = [{"type": "text", "text": _fit(prompt, 30000)}]
    if image is not None:
        small = image.copy()
        small.thumbnail((1920, 1920))
        buf = io.BytesIO()
        small.save(buf, "JPEG", quality=80)
        content.append({"type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}})
    body = {"model": model, "temperature": 0.3, "messages": [{"role": "user", "content": content}]}
    headers = {**BEAT_HEADERS, "Authorization": f"Bearer {BEAT_KEY}"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as session:
        for attempt in range(2):
            async with session.post(f"{BEAT_URL}/chat/completions", json=body, headers=headers) as resp:
                status, data = resp.status, await resp.json(content_type=None)
            err = (data.get("error") or {}) if isinstance(data, dict) else {}
            if status == 429 and attempt == 0:  # лимит бесплатной модели — подождать и повторить один раз
                await asyncio.sleep(min(float(err.get("retry_after_seconds") or 30), 65))
                continue
            break
    if status != 200:
        raise AIError(f"BeatAPI HTTP {status} {err.get('code') or ''} ({model}): {str(err.get('message') or data)[:200]}")
    try:
        return _json_from(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise AIError(f"BeatAPI: неожиданный ответ ({exc})") from exc


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
        "фильмов — в «ёлочках». Без эмодзи." + GRAMMAR + "Ответь JSON: {\"texts\": [\"...\", ...]} "
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


async def pick_news(key: str, items: list[tuple[str, str, str]], n: int = 5) -> list[dict[str, Any]] | None:
    """Редактор новостей: из свежих заголовков (заголовок, лид, источник) выбирает n самых интересных —
    без местных событий и без двух новостей об одном и том же — и пересказывает понятно для озвучки
    (Gemini уточняет детали поиском Google). [{"i": индекс, "text": ..., "film": название}] или None."""
    if not available(key) or not items:
        return None
    listing = "\n".join(f"{i + 1}. {t}\n   Подробности: {lead or '—'}\n   Источник: {src}"
                        for i, (t, lead, src) in enumerate(items))
    rules = (
        f"Ты редактор короткого видео «Новости кино» для широкой аудитории. Ниже свежие заголовки. Выбери {n} "
        "самых интересных новостей о фильмах, сериалах, мультфильмах, актёрах и режиссёрах: премьеры, трейлеры, "
        "кастинг, продолжения, сборы, награды, скандалы. Пропускай местные события (показы в городском "
        "кинотеатре, региональные фестивали и конкурсы, новости городов), рекламу и политику. Не бери две "
        "новости об одном и том же фильме или человеке — только одну. Источники вида t.me/… — посты "
        "Telegram-каналов: сверяй их со СМИ; что есть и там и там — надёжнее; новость только из канала бери, "
        "если она правдоподобна, а неподтверждённое подавай со словами «по слухам». Для каждой выбранной напиши текст для "
        "озвучки: 1–2 коротких живых предложения, до 220 символов, чтобы зритель сразу понял, о каком фильме "
        "или сериале речь, кто в нём и что случилось (и когда выйдет, если известно). Названия — как в "
        "российском прокате, по-русски, в «ёлочках» (Ramayana → «Рамаяна»). Не выдумывай." + GRAMMAR + "Самые интересные — "
        "первыми. Ответь только JSON: {\"news\": [{\"n\": номер заголовка, \"text\": \"...\", "
        "\"film\": \"название фильма или сериала по-русски или пусто\"}]}"
    )
    data = None
    if key:
        try:
            data = await _ask(key, [{"text": "Если в заголовке мало деталей — уточни поиском в Google. " + rules
                                     + "\n\n" + listing}], search=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("shorts: news editor with search failed: %s", exc)
    if data is None:
        prompt = rules + " Используй только факты из заголовков и подробностей.\n\n" + listing
        try:
            data = await _with_reserve(key, [{"text": prompt}], prompt)
        except Exception as exc:  # noqa: BLE001
            global last_error
            last_error = str(exc)[:300]
            log.warning("shorts: news editor failed: %s", exc)
            return None
    out, seen = [], set()
    for row in (data.get("news") if isinstance(data, dict) else None) or []:
        try:
            i = int(row.get("n")) - 1
        except (TypeError, ValueError, AttributeError):
            continue
        text = re.sub(r"\s*\[\d+(?:,\s*\d+)*\]", "", str(row.get("text") or "")).strip()
        if 0 <= i < len(items) and i not in seen and len(text) >= 20:
            seen.add(i)
            out.append({"i": i, "text": text[:260], "film": str(row.get("film") or "").strip(" «»\"")})
    return out[:n] if len(out) >= 3 else None


async def spoken_form(texts: list[str], key: str | None = None) -> list[str] | None:
    """Фразы для озвучки: числа — словами в нужном падеже, латиница — русскими буквами по звучанию."""
    key = os.getenv("GEMINI_API_KEY", "").strip() if key is None else key
    if not available(key) or not texts:
        return None
    listing = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
    prompt = (
        "Подготовь фразы для синтеза речи по-русски. Правила:\n"
        "1. Все числа, годы, даты, проценты, рейтинги, номера сезонов — словами, в правильном падеже и роде: "
        "«в 2024 году» → «в две тысячи двадцать четвёртом году», «7.8» → «семь и восемь», "
        "«3 сезона» → «три сезона», «1990-х» → «девяностых», «15%» → «пятнадцать процентов».\n"
        "2. Все слова латиницей — русскими буквами так, как их произносят: имена, названия, бренды "
        "(«Christopher Nolan» → «Кристофер Нолан», «Warner Bros.» → «Уорнер Бразерс», "
        "аббревиатуры по буквам: «FBI» → «эф-би-ай»).\n"
        "3. Исправь явные ошибки в падежах и согласовании («роли достались Доминику Маклафлину», «музыку к "
        "проекту пишет»). Больше ничего не меняй: те же слова, порядок, смысл, знаки препинания, «ёлочки». "
        "Знак ударения (символ U+0301 после гласной) сохраняй.\n"
        "Ответь JSON: {\"texts\": [\"...\", ...]} — столько же фраз, в том же порядке.\n\n" + listing
    )
    try:
        data = await _with_reserve(key, [{"text": prompt}], prompt)
        out = [str(t).strip() for t in data["texts"]]
    except Exception as exc:  # noqa: BLE001
        log.warning("shorts: AI spoken_form failed: %s", exc)
        return None
    if len(out) != len(texts):
        return None
    # явно испорченную фразу (пустая, вдвое короче или втрое длиннее) оставляем как была
    return [new if new and len(old) / 2 <= len(new) <= len(old) * 3 + 40 else old for old, new in zip(texts, out)]


REVIEW_CHARS = (1600, 2600)  # длина сценария обзора: примерно 2–3 минуты голоса


async def write_review(key: str, brief: str, notes: str = "") -> dict[str, Any]:
    """Сценарий обзора фильма для закадра: {"title": название для YouTube, "sections": [{heading, text}]}.

    brief — данные о фильме (сюжет, создатели, рейтинги, факты); notes — текст автора (слух, разбор,
    мнение), который надо сохранить и дополнить. Факты и пасхалки — только из brief и notes.
    """
    if not available(key):
        raise AIError("нет ключа GEMINI_API_KEY или RESERVE_AI_KEY — ИИ не может написать сценарий")
    lo, hi = REVIEW_CHARS
    author = (
        "\n\nТЕКСТ АВТОРА (главная основа обзора): сохрани его мнение, оценки, шутки и формулировки, "
        "выстрой по разделам и дополни данными о фильме. Слухи из текста автора подавай как слухи "
        "(«по слухам», «говорят»), не выдавай за факт.\n«««\n" + notes.strip()[:6000] + "\n»»»"
    ) if notes.strip() else ""
    prompt = (
        "Ты автор YouTube-канала о кино KinoMir360. Напиши сценарий закадрового текста для "
        f"горизонтального видео-обзора фильма, {lo}–{hi} символов (2–3 минуты речи). "
        "Живо, разговорно, на «ты» со зрителем, короткими фразами, без канцелярита и без эмодзи.\n"
        "Разделы (заголовок — 1–3 слова):\n"
        "1. Вступление — цепляющая первая фраза, о чём обзор.\n"
        "2. О чём фильм — завязка БЕЗ спойлеров.\n"
        "3. Кто снимал и кто играет — режиссёр, главные актёры и роли.\n"
        "4. Факты и пасхалки — ТОЛЬКО из данных ниже (факты Кинопоиска, текст автора). "
        "Ничего не выдумывай; нет фактов — пропусти раздел.\n"
        "5. Что получилось, а что нет — честное мнение: сильные и слабые стороны.\n"
        "6. Итог — кому смотреть, оценка, призыв написать мнение в комментариях и подписаться на КиноМир360.\n"
        "Названия фильмов — в «ёлочках». Никаких ссылок, хэштегов, ремарок в скобках и пометок "
        "для монтажа — только текст, который прочитает диктор.\n"
        "Ответь JSON: {\"title\": \"название ролика для YouTube до 90 символов\", "
        "\"sections\": [{\"heading\": \"...\", \"text\": \"...\"}, ...]}.\n\n"
        "ДАННЫЕ О ФИЛЬМЕ:\n" + brief + author
    )
    data = await _with_reserve(key, [{"text": prompt}], prompt)
    sections = [{"heading": str(s.get("heading") or "").strip()[:40], "text": str(s.get("text") or "").strip()}
                for s in (data.get("sections") if isinstance(data, dict) else None) or [] if isinstance(s, dict)]
    sections = [s for s in sections if s["text"]]
    if len(sections) < 3:
        raise AIError("ИИ вернул сценарий без разделов — попробуй ещё раз")
    return {"title": str(data.get("title") or "").strip()[:95], "sections": sections}


DUB_CPS = 15  # символов в секунду — столько русского текста успевает прозвучать


def _dub_task(duration: float) -> str:
    return (
        f"Переводи для закадрового русского перевода трейлера (длина {duration:.0f} с). "
        "Перевод живой, разговорный, как в профессиональном дубляже, а не дословный. Каждая русская "
        f"реплика должна успеть прозвучать за время оригинала: не длиннее {DUB_CPS} символов на секунду "
        "реплики — сокращай, сохраняя смысл. Имена и названия — как в русском прокате. "
        "gender — пол говорящего (male/female), speaker — метка голоса (A, B, C… один человек — одна метка; "
        "закадровый голос диктора — тоже отдельная метка)."
    )


def _dub_lines(data: Any, duration: float) -> list[dict[str, Any]]:
    out = []
    for ln in (data.get("lines") if isinstance(data, dict) else None) or []:
        try:
            start, end = float(ln["start"]), float(ln["end"])
        except (KeyError, TypeError, ValueError):
            continue
        ru = str(ln.get("ru") or "").strip()
        if not ru or end <= start or start >= duration + 1:
            continue
        out.append({"start": round(max(start, 0.0), 2), "end": round(min(end, duration), 2),
                    "speaker": str(ln.get("speaker") or "A")[:3],
                    "gender": "female" if str(ln.get("gender") or "").lower().startswith("f") else "male",
                    "text": str(ln.get("text") or "").strip(), "ru": ru})
    out.sort(key=lambda ln: ln["start"])
    return out


async def _groq_whisper(audio: bytes) -> list[dict[str, Any]]:
    """Распознавание речи Whisper на Groq (тот же ключ резервного ИИ): сегменты со временем."""
    url = RESERVE_URL.split("/chat/completions")[0] + "/audio/transcriptions"
    form = aiohttp.FormData()
    form.add_field("file", audio, filename="audio.mp3", content_type="audio/mpeg")
    form.add_field("model", os.getenv("RESERVE_STT_MODEL", "whisper-large-v3"))
    form.add_field("response_format", "verbose_json")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as session, \
            session.post(url, data=form, headers={"Authorization": f"Bearer {RESERVE_KEY}"}) as resp:
        data = await resp.json(content_type=None)
        if resp.status != 200:
            raise AIError(f"распознавание речи HTTP {resp.status}: {str(data)[:200]}")
    return [{"start": s["start"], "end": s["end"], "text": str(s.get("text") or "").strip()}
            for s in data.get("segments") or [] if str(s.get("text") or "").strip()]


async def transcribe_dub(key: str, audio: bytes, duration: float) -> list[dict[str, Any]]:
    """Реплики трейлера для перевода: [{start, end, speaker, gender, text, ru}].

    Gemini слушает дорожку сам; не вышло — Whisper на Groq + перевод резервным ИИ.
    """
    errors = []
    if key:
        prompt = (
            "Это звуковая дорожка трейлера. Выпиши все реплики людей по порядку (диалоги и закадровый "
            "голос; песни и надписи пропусти) с временем начала и конца в секундах (точность 0.1). "
            + _dub_task(duration)
            + " Ответь JSON: {\"lines\": [{\"start\": 0.0, \"end\": 0.0, \"speaker\": \"A\", "
            "\"gender\": \"male\", \"text\": \"оригинал\", \"ru\": \"перевод\"}, ...]}."
        )
        parts = [{"text": prompt},
                 {"inline_data": {"mime_type": "audio/mp3", "data": base64.b64encode(audio).decode()}}]
        try:
            lines = _dub_lines(await _ask(key, parts), duration)
            if lines:
                return lines
            errors.append("Gemini не нашёл реплик")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Gemini: {exc}")
            log.warning("shorts: Gemini dub transcription failed: %s", exc)
    if RESERVE_KEY and "groq.com" in RESERVE_URL:
        try:
            segs = await _groq_whisper(audio)
            if not segs:
                raise AIError("в дорожке не распознано речи")
            listing = "\n".join(f"{i + 1}. [{s['start']:.1f}–{s['end']:.1f}] {s['text']}" for i, s in enumerate(segs))
            prompt = (
                "Ниже реплики трейлера со временем (распознаны автоматически). " + _dub_task(duration)
                + " Для каждой реплики угадай по смыслу пол и говорящего. Ответь JSON: {\"lines\": "
                "[{\"start\": ..., \"end\": ..., \"speaker\": \"A\", \"gender\": \"male\", \"text\": \"оригинал\", "
                "\"ru\": \"перевод\"}, ...]} — время бери из списка.\n\n" + listing
            )
            lines = _dub_lines(await _ask_reserve(prompt), duration)
            if lines:
                return lines
            errors.append("резервный ИИ вернул пустой перевод")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"резерв: {exc}")
            log.warning("shorts: reserve dub transcription failed: %s", exc)
    raise AIError("; ".join(errors) or "нет ключа GEMINI_API_KEY или RESERVE_AI_KEY (Groq)")


async def transcribe_words(audio: bytes, language: str = "ru") -> list[dict[str, Any]]:
    """Время каждого слова в готовой озвучке (Whisper на Groq): [{"w": слово, "s": начало, "e": конец}]."""
    if not RESERVE_KEY or "groq.com" not in RESERVE_URL:
        raise AIError("для субтитров по слову нужен ключ Groq (RESERVE_AI_KEY)")
    url = RESERVE_URL.split("/chat/completions")[0] + "/audio/transcriptions"
    form = aiohttp.FormData()
    form.add_field("file", audio, filename="voice.mp3", content_type="audio/mpeg")
    form.add_field("model", os.getenv("RESERVE_STT_MODEL", "whisper-large-v3"))
    form.add_field("response_format", "verbose_json")
    form.add_field("language", language)
    form.add_field("timestamp_granularities[]", "word")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session, \
            session.post(url, data=form, headers={"Authorization": f"Bearer {RESERVE_KEY}"}) as resp:
        data = await resp.json(content_type=None)
        if resp.status != 200:
            raise AIError(f"распознавание слов HTTP {resp.status}: {str(data)[:200]}")
    words = []
    for w in data.get("words") or []:
        text = str(w.get("word") or "").strip()
        if text:
            words.append({"w": text, "s": round(float(w["start"]), 2), "e": round(float(w["end"]), 2)})
    return words


async def write_lore(key: str, name: str, article: str, hook: str = "") -> dict[str, Any]:
    """Сценарий шортса «история персонажа» на ~60 секунд: {"title", "en", "lines": [фраза, ...]}.

    Факты — только из статьи (Википедия); hook — новость-зацепка автора («Дауни сыграет Дума»).
    """
    if not available(key):
        raise AIError("нет ключа GEMINI_API_KEY или RESERVE_AI_KEY — ИИ не может написать сценарий")
    zacep = (f"Начни с зацепки-новости автора: «{hook.strip()[:300]}» — и сразу вопрос, кто это такой. "
             if hook.strip() else "Начни с цепляющего вопроса или факта, чтобы зритель не пролистнул. ")
    prompt = (
        f"Ты автор вертикальных роликов о кино и комиксах KinoMir360. Напиши закадровый текст шортса "
        f"«история персонажа» про «{name}» на 55–65 секунд: 850–1050 символов. " + zacep +
        "Дальше — история происхождения по порядку: детство и семья, что сделало его таким, ключевые "
        "повороты, кем стал. Быстро, плотно, короткими фразами, разговорно, на «ты» не надо — "
        "рассказ от третьего лица. В конце — короткий вывод и вопрос зрителю для комментариев. "
        "Используй ТОЛЬКО факты из статьи ниже, ничего не выдумывай. Без эмодзи, ссылок и ремарок.\n"
        "Раздели текст на 8–12 фраз — каждая фраза станет отдельной картинкой в ролике.\n"
        "Ответь JSON: {\"title\": \"название шортса до 70 символов\", \"en\": \"имя персонажа по-английски\", "
        "\"lines\": [\"фраза\", ...]}.\n\nСТАТЬЯ:\n" + article[:12000]
    )
    data = await _with_reserve(key, [{"text": prompt}], prompt)
    lines = [str(x).strip() for x in (data.get("lines") if isinstance(data, dict) else None) or [] if str(x).strip()]
    if len(lines) < 4:
        raise AIError("ИИ вернул слишком короткий сценарий — попробуй ещё раз")
    return {"title": str(data.get("title") or "").strip()[:90], "en": str(data.get("en") or "").strip()[:60],
            "lines": lines[:14]}


LIST_TASK = (
    "Это короткий ролик-подборка фильмов или сериалов. Определи тему подборки (её название — как в ролике, "
    "по-русски, до 60 символов) и все фильмы/сериалы по порядку появления. Для каждого: название "
    "по-русски, как в российском прокате, оригинальное название и год, если можно понять. Не выдумывай "
    "фильмы, которых в ролике нет. Ответь JSON: {\"title\": \"тема\", \"films\": [{\"ru\": \"...\", "
    "\"en\": \"...\", \"year\": 2018}, ...]}."
)


def _list_from(data: Any) -> dict[str, Any]:
    films = []
    for f in (data.get("films") if isinstance(data, dict) else None) or []:
        if not isinstance(f, dict):
            continue
        ru, en = str(f.get("ru") or "").strip(), str(f.get("en") or "").strip()
        try:
            year = int(f.get("year") or 0) or None
        except (TypeError, ValueError):
            year = None
        if ru or en:
            films.append({"ru": ru, "en": en, "year": year})
    if not films:
        raise AIError("в ролике не нашлось списка фильмов")
    return {"title": str(data.get("title") or "").strip()[:80], "films": films[:12]}


async def extract_list(key: str, video: bytes, frames: list[Image.Image], audio: bytes) -> dict[str, Any]:
    """Тема и список фильмов из чужого ролика-подборки: Gemini смотрит видео со звуком; не вышло —
    кадры + распознанная речь (Whisper на Groq) резервному ИИ."""
    errors = []
    if key:
        try:
            parts = [{"text": LIST_TASK},
                     {"inline_data": {"mime_type": "video/mp4", "data": base64.b64encode(video).decode()}}]
            return _list_from(await _ask(key, parts))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Gemini: {exc}")
            log.warning("shorts: Gemini list extraction failed: %s", exc)
    if RESERVE_KEY or BEAT_KEY:
        speech = ""
        if RESERVE_KEY and "groq.com" in RESERVE_URL:
            try:
                speech = " ".join(s["text"] for s in await _groq_whisper(audio))
            except Exception as exc:  # noqa: BLE001
                log.warning("shorts: whisper for list extraction failed: %s", exc)
        prompt = (LIST_TASK + " На картинке — кадры ролика по порядку (номер в углу), надписи на них "
                  "обычно и есть названия." + (f"\n\nРечь в ролике: {speech[:4000]}" if speech else ""))
        try:
            sheet = _sheet(frames) if frames else None
            if RESERVE_KEY:
                return _list_from(await _ask_reserve(prompt, sheet))
            return _list_from(await _ask_beat(prompt, sheet))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"резерв: {exc}")
    raise AIError("; ".join(errors) or "нет ключа GEMINI_API_KEY или RESERVE_AI_KEY")


async def condense_facts(key: str, title: str, text: str, n: int = 5) -> list[str]:
    """Факты для шортса из текста автора: n коротких фактов (до 170 символов), без выдумок и ссылок."""
    if not available(key):
        raise AIError("нет ключа GEMINI_API_KEY или RESERVE_AI_KEY — ИИ не может обработать текст")
    prompt = (
        f"Ниже текст автора о фильме «{title}». Выбери {n} самых интересных фактов и перескажи каждый "
        "одной-двумя короткими фразами для озвучки в шортсе: 60–170 символов, живо, по-русски. "
        "Используй ТОЛЬКО то, что есть в тексте, ничего не добавляй и не уточняй от себя." + GRAMMAR +
        "Убери ссылки и названия сайтов-источников. Названия фильмов — в «ёлочках». "
        "Ответь JSON: {\"facts\": [\"...\", ...]}.\n\nТЕКСТ:\n" + text.strip()[:8000]
    )
    data = await _with_reserve(key, [{"text": prompt}], prompt)
    facts = [re.sub(r"\s+", " ", str(f)).strip() for f in (data.get("facts") if isinstance(data, dict) else None) or []]
    facts = [f for f in facts if len(f) >= 20][:n]
    if len(facts) < 3:
        raise AIError("ИИ не нашёл в тексте хотя бы 3 факта — пришли текст подлиннее")
    return facts


NOMINATIVE = {"фильме": "Фильм", "сериале": "Сериал", "мультфильме": "Мультфильм"}


# грамотность текстов для озвучки: ИИ часто путает падежи иностранных имён и чисел
GRAMMAR = (" Пиши грамотно: проверь падежи и согласование, особенно у имён и чисел («роли достались Доминику "
           "Маклафлину и Аластеру Стауту», «роль Гермионы», «выйдет 25 декабря 2026 года»). Иностранные имена и "
           "названия — так, как их пишут российские СМИ и русский дубляж (Дамблдор, Снейп, Флитвик, Златопуст "
           "Локонс, Ханс Циммер, «Рамаяна»). ")


def _clean_facts(data: Any, n: int) -> list[str]:
    facts = [re.sub(r"\s+", " ", re.sub(r"\s*\[\d+(?:,\s*\d+)*\]", "", str(f))).strip()
             for f in (data.get("facts") if isinstance(data, dict) else None) or []]
    return [f for f in facts if 30 <= len(f) <= 220][:n]


async def web_facts(key: str, title: str, en: str = "", year: int | None = None, kind: str = "фильме",
                    known: list[str] | None = None, article: str = "", n: int = 5,
                    posts: str = "", rumors: bool = False) -> tuple[list[str], list[str]]:
    """Интересные факты о фильме из интернета: Gemini ищет в Google; не вышло — ИИ выбирает из Википедии,
    постов Telegram-каналов и фактов Кинопоиска. Возвращает (факты, источники).
    rumors=True — фильм или сериал ещё не вышел: свежие новости и слухи о нём (слух помечен как слух)."""
    if not available(key):
        raise AIError("нет ключа GEMINI_API_KEY или RESERVE_AI_KEY")
    name = f"«{title}»" + (f" ({en}, {year})" if en and year else f" ({year})" if year else "")
    style = ("Каждый пункт — одна-две короткие фразы для озвучки, 60–170 символов, живо, по-русски, без ссылок "
             "и названий сайтов. Названия фильмов — в «ёлочках». Самое сильное — первым." + GRAMMAR)
    if rumors:
        rules = (
            f"Сегодня {date.today():%d.%m.%Y}. {NOMINATIVE.get(kind, 'Фильм')} {name} ещё не вышел. Нужно {n} самых "
            f"интересных новостей и слухов о нём для шортса «Что известно о {kind} «{title}»»: дата выхода, "
            "кто кого сыграет, что известно о сюжете, съёмки, бюджет, трейлер и тизеры, отличия от книг или "
            "оригинала, утечки со съёмок. Свежее важнее старого. Не путай с прошлыми фильмами и сериалами "
            "с похожим названием. Подтверждённое подавай как факт, а слух — обязательно со словами "
            "«по слухам», «инсайдеры говорят» или «пока не подтверждено»; не выдумывай. " + style
        )
    else:
        rules = (
            f"Нужно {n} самых интересных и малоизвестных фактов для шортса «{n} фактов о {kind} {name}»: "
            "съёмки, кастинг, кто отказался от роли, трюки, бюджет и рекорды, пасхалки, вырезанные сцены, "
            "реквизит, курьёзы на площадке. Без спойлеров сюжета и концовки. Только проверенное — никаких "
            "слухов и догадок, не выдумывай цифры. " + style
        )
    extra = ""
    if known:
        extra += "\n\nФакты с Кинопоиска (можно взять лучшие, но ищи и новые):\n" + "\n".join(f"- {f}" for f in known[:15])
    if article:
        extra += "\n\nСправка из Википедии:\n" + article[:12000]
    if posts and rumors:
        extra += ("\n\nПосты из Telegram-каналов о кино (новости и слухи; шутки и мнения пропускай, "
                  "перескажи своими словами):\n" + posts[:12000])
    elif posts:
        extra += ("\n\nПосты из Telegram-каналов о кино (бери только то, что похоже на проверенный факт, "
                  "а не слухи, шутки и мнения; перескажи своими словами):\n" + posts[:9000])
    tail = '\n\nОтветь только JSON: {"facts": ["...", ...]}'
    errors = []
    if key:
        try:
            look = ("Поищи в интернете, в том числе в открытых Telegram-каналах о кино (отдельным запросом "
                    f"site:t.me «{title}»" + (f" и site:t.me {en}" if en else "") + "). ")
            data = await _ask(key, [{"text": look + rules + extra + tail}], search=True)
            facts = _clean_facts(data, n)
            if len(facts) >= 3:
                return facts, data.get("_sources") or []
            errors.append("поиск Google: мало фактов")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"поиск Google: {exc}")
            log.warning("shorts: web facts search failed: %s", exc)
    if not extra:
        raise AIError("; ".join(errors) or "нет материала для фактов")
    prompt = rules + " Бери факты ТОЛЬКО из текстов ниже, ничего не добавляй от себя." + extra + tail
    data = await _with_reserve(key, [{"text": prompt}], prompt)
    facts = _clean_facts(data, n)
    if len(facts) < 3:
        raise AIError("ИИ не нашёл хотя бы 3 факта" + (f" ({'; '.join(errors)})" if errors else ""))
    sources = (["Википедия"] if article else []) + (["Telegram-каналы"] if posts else [])
    return facts, sources + (["Кинопоиск"] if known else [])

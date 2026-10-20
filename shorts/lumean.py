"""Озвучка через Lumean (ElevenLabs, Fish Audio, Cartesia): https://lumean.app, документация — api.lumean.app/docs/llm.md.

Ключ — LUMEAN_API_KEY. Сервис и голос — из настроек бота (свой клон голоса, команда /clone), иначе
LUMEAN_SERVICE / LUMEAN_VOICE_ID, иначе ElevenLabs и первый мужской русский голос его библиотеки.
Шаблон под сервис и голос бот находит по имени или создаёт сам и дальше переиспользует.

Озвучка в Lumean асинхронная: заказ → ждём статус → скачиваем mp3. Чтобы не платить минимум за каждую
фразу, весь ролик идёт одним заказом, фразы разделены бесплатными паузами {{pause=…}}, а потом
дорожка режется по этим паузам.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any

import aiohttp

from .ai import _env_key

API = os.getenv("LUMEAN_API_URL", "https://api.lumean.app/api/public").rstrip("/")
KEY = _env_key("LUMEAN_API_KEY")  # находит и имя, набранное с русскими буквами-двойниками
SERVICE = os.getenv("LUMEAN_SERVICE", "elevenlabs").strip() or "elevenlabs"  # elevenlabs | fishaudio | cartesia
VOICE_ID = os.getenv("LUMEAN_VOICE_ID", "").strip()
MODEL = os.getenv("LUMEAN_MODEL", "eleven_multilingual_v2").strip()
SPEED = float(os.getenv("LUMEAN_SPEED", "1.12"))  # 0.7..1.2; ~как +22% у голоса Microsoft
TEMPLATE_NAME = "KinoMir360 TTS"
CLONE_SERVICES = ("fishaudio", "cartesia")
PAUSE = 1.0  # сек тишины между фразами — по ним режем дорожку
WAIT = 240  # сек на готовность заказа
DONE = {"completed", "result_delivered"}
FAILED = {"failed", "compensated", "cancelled"}

log = logging.getLogger("shorts")
_templates: dict[tuple[str, str], str] = {}  # (сервис, голос) -> id шаблона
_library: dict[str, list[dict[str, Any]]] = {}  # пол -> голоса библиотеки
voice_name = ""  # какой голос выбран — для подписи в /voice


class LumeanError(RuntimeError):
    pass


def enabled() -> bool:
    return bool(KEY)


def use_voice(service: str | None, voice_id: str | None) -> None:
    """Голос из настроек бота (например, свой клон) — поверх переменных окружения."""
    global SERVICE, VOICE_ID
    SERVICE = service or os.getenv("LUMEAN_SERVICE", "elevenlabs").strip() or "elevenlabs"
    VOICE_ID = voice_id or os.getenv("LUMEAN_VOICE_ID", "").strip()


async def _call(session: aiohttp.ClientSession, method: str, path: str, **kw: Any) -> Any:
    async with session.request(method, f"{API}{path}", headers={"X-API-KEY": KEY, "Accept": "application/json"},
                               **kw) as resp:
        try:
            body = await resp.json(content_type=None)
        except Exception:  # noqa: BLE001
            body = {"message": (await resp.text())[:200]}
    if resp.status >= 400 or (isinstance(body, dict) and body.get("success") is False):
        msg = body.get("message") if isinstance(body, dict) else body
        reason = body.get("reason") or body.get("error_code") if isinstance(body, dict) else None
        if resp.status == 402:
            msg = f"не хватает средств на балансе Lumean ({msg})"
        if isinstance(body, dict) and isinstance(body.get("errors"), dict):  # 422: что именно не так
            msg = f"{msg} — " + "; ".join(f"{k}: {' '.join(map(str, v)) if isinstance(v, list) else v}"
                                          for k, v in body["errors"].items())
        raise LumeanError(f"Lumean HTTP {resp.status}{f' {reason}' if reason else ''}: {str(msg)[:200]}")
    return body.get("data") if isinstance(body, dict) and "data" in body else body


async def _pick_voice(session: aiohttp.ClientSession) -> str:
    """Мужской голос с русским из библиотеки ElevenLabs (если LUMEAN_VOICE_ID не задан)."""
    global voice_name
    if VOICE_ID:
        return VOICE_ID
    data = await _call(session, "GET", "/voices/elevenlabs/library",
                       params={"required_languages": "ru", "gender": "male", "page": 0, "page_size": 30})
    voices = [v for v in (data or {}).get("voices") or [] if isinstance(v, dict) and v.get("voice_id")]
    if not voices:
        raise LumeanError("в библиотеке ElevenLabs не нашлось мужского русского голоса — задайте LUMEAN_VOICE_ID")
    ru = [v for v in voices if str(v.get("language") or "").lower().startswith("ru")]
    best = (ru or voices)[0]
    voice_name = str(best.get("name") or "")
    log.info("shorts: Lumean voice %s (%s)", best["voice_id"], voice_name)
    return str(best["voice_id"])


async def library(gender: str) -> list[dict[str, Any]]:
    """Русские голоса библиотеки ElevenLabs для выбора кнопками: [{voice_id, name, preview_url, …}]."""
    if gender not in _library:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            data = await _call(session, "GET", "/voices/elevenlabs/library",
                               params={"required_languages": "ru", "gender": gender, "page": 0, "page_size": 30})
        voices = [v for v in (data or {}).get("voices") or [] if isinstance(v, dict) and v.get("voice_id")]
        voices.sort(key=lambda v: not str(v.get("language") or "").lower().startswith("ru"))
        _library[gender] = voices[:12]
    return _library[gender]


async def preview(url: str) -> bytes:
    """Демо-запись голоса из библиотеки (бесплатно, без заказа)."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session, session.get(url) as resp:
        resp.raise_for_status()
        return await resp.read()


def _config(service: str, voice: str) -> dict[str, Any]:
    if service == "cartesia":
        speed = min(max(SPEED, 0.6), 1.5)
        return {"tts_settings": {"voice_id": voice, "model_id": "sonic-3", "language_code": "ru",
                                 "voice_settings": {"speed": speed}}}
    if service == "fishaudio":
        return {"tts_settings": {"voice_id": voice}}  # язык Fish определяет сам
    return {"tts_settings": {
        "mode": "mode_v1", "model_id": MODEL, "voice_id": voice, "language_code": "ru",
        "advanced_voice_settings": True,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "use_speaker_boost": True,
                           "speed": min(max(SPEED, 0.7), 1.2)},
        "output_settings": {"audio_format": "mp3"},
    }}


def _option_value(option: Any) -> str:
    return str(option.get("value") or option.get("id") or "") if isinstance(option, dict) else str(option or "")


async def _default_model(session: aiohttp.ClientSession, service: str) -> str:
    """Модель сервиса из GET /templates/config-options: значение по умолчанию, иначе первая из списка."""
    data = await _call(session, "GET", "/templates/config-options", params={"service": service})
    field = ((data or {}).get("tts_settings") or {}).get("model_id") if isinstance(data, dict) else None
    options = (field.get("options") or []) if isinstance(field, dict) else []
    if isinstance(options, dict):  # {"значение": "название"}
        options = list(options)
    model = (_option_value(field.get("default")) if isinstance(field, dict) else "") or \
        next((v for v in map(_option_value, options) if v), "")
    if not model:
        raise LumeanError(f"не нашёл модель {service} в config-options: {str(data)[:200]}")
    return model


async def _template_id(session: aiohttp.ClientSession) -> str:
    """Шаблон под текущий сервис и голос: найти по имени или создать один раз."""
    service = SERVICE
    voice = VOICE_ID if (VOICE_ID or service != "elevenlabs") else await _pick_voice(session)
    if not voice:
        raise LumeanError(f"для сервиса {service} не задан голос (LUMEAN_VOICE_ID или /clone)")
    if (service, voice) in _templates:
        return _templates[(service, voice)]
    name = TEMPLATE_NAME if service == "elevenlabs" and not VOICE_ID else f"{TEMPLATE_NAME} {service} {voice[:8]}"
    data = await _call(session, "GET", "/templates", params={"page": 1, "search": name, "service": service})
    for t in (data or {}).get("items") or []:
        if t.get("name") == name and t.get("id"):
            _templates[(service, voice)] = str(t["id"])
            return _templates[(service, voice)]
    config = _config(service, voice)
    if service == "fishaudio":  # модели Fish в Lumean меняются — берём ту, что предлагает сам сервис
        config["tts_settings"]["model_id"] = await _default_model(session, service)
    created = await _call(session, "POST", "/templates",
                          json={"service_key": service, "name": name, "config": config})
    _templates[(service, voice)] = str(created["id"])
    log.info("shorts: Lumean template %s (%s) created", created["id"], name)
    return _templates[(service, voice)]


async def create_clone(audio: bytes, name: str, service: str = "fishaudio") -> dict[str, Any]:
    """Отправить образец голоса на клонирование; возвращает клон (status pending/cloning/…)."""
    import base64

    if service not in CLONE_SERVICES:
        raise LumeanError(f"клонирование есть только у {', '.join(CLONE_SERVICES)}")
    audio = await _to_wav(audio)  # голосовые Telegram — OGG/Opus, а OGG и MP3 Lumean не опознаёт
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        return await _call(session, "POST", f"/voices/{service}/clones", json={
            "display_name": name[:128], "reference_audio": "data:audio/wav;base64," + base64.b64encode(audio).decode(),
            "language": "ru", "consent_confirmed": True})


async def _to_wav(audio: bytes) -> bytes:
    """Образец голоса → WAV моно 44.1 кГц, до 2 минут (по WAV Lumean точно узнаёт формат)."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "voice.in", Path(tmp) / "voice.wav"
        src.write_bytes(audio)
        await _run("ffmpeg", "-v", "error", "-y", "-i", str(src), "-vn", "-t", "120", "-ac", "1", "-ar", "44100",
                   "-c:a", "pcm_s16le", str(dst))
        return dst.read_bytes()


async def clone_status(clone_id: str, service: str = "fishaudio") -> dict[str, Any]:
    """Найти свой клон по id: status (pending/cloning/ready/failed), voice_id, error."""
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        data = await _call(session, "GET", f"/voices/{service}/clones", params={"per_page": 100})
    for c in (data or {}).get("items") or []:
        if str(c.get("id")) == clone_id:
            return c
    raise LumeanError("клон не найден")


async def _order_audio(session: aiohttp.ClientSession, text: str, out: Path) -> Path:
    order = await _call(session, "POST", "/orders", json={
        "template_id": await _template_id(session), "input_text": text, "name": "KinoMir360 short"})
    oid = order["id"]
    status, waited = order.get("status"), 0.0
    while status not in DONE:
        if status in FAILED or status == "partially_completed":
            reason = (order.get("result") or {}).get("user_message") or status
            raise LumeanError(f"заказ не озвучен: {reason}")
        if waited >= WAIT:
            raise LumeanError(f"заказ не готов за {WAIT} с (статус {status})")
        await asyncio.sleep(3)
        waited += 3
        order = await _call(session, "GET", f"/orders/{oid}")
        status = order.get("status")
    files = (order.get("result") or {}).get("files") or []
    if not files:
        raise LumeanError("заказ готов, но файла нет")
    link = await _call(session, "POST", "/storage/url", json={"path": files[0]})
    async with session.get(link["url"]) as resp:
        resp.raise_for_status()
        out.write_bytes(await resp.read())
    return out


async def _run(*cmd: str) -> str:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode:
        raise LumeanError(f"{cmd[0]}: {err.decode(errors='ignore')[-200:]}")
    return out.decode(errors="ignore") + err.decode(errors="ignore")


async def _speech_spans(path: Path, parts: int) -> list[tuple[float, float]]:
    """Отрезки речи: режем по parts-1 самым длинным паузам (наши {{pause}} между фразами)."""
    log_text = await _run("ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
                          "-af", f"silencedetect=noise=-38dB:d={PAUSE * 0.6:.2f}", "-f", "null", "-")
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", log_text)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", log_text)]
    total = float((await _run("ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", str(path))).strip().splitlines()[0])
    silences = [(s, e) for s, e in zip(starts, ends) if 0.05 < s and e < total - 0.05]  # не края дорожки
    if len(silences) < parts - 1:
        raise LumeanError(f"не удалось разрезать озвучку на фразы ({len(silences)} пауз из {parts - 1})")
    cuts = sorted(sorted(silences, key=lambda se: se[1] - se[0], reverse=True)[:parts - 1])
    bounds = [0.0] + [x for s, e in cuts for x in (s, e)] + [total]
    return [(bounds[2 * i], bounds[2 * i + 1]) for i in range(parts)]


async def synthesize(lines: list[str], parts: list[Path], work: Path) -> None:
    """Озвучить все фразы одним заказом и разложить по файлам parts (mp3, по фразе на файл)."""
    text = f"\n{{{{pause={PAUSE:g}}}}}\n".join(lines)
    full = work / "lumean_full.mp3"
    timeout = aiohttp.ClientTimeout(total=WAIT + 60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        await _order_audio(session, text, full)
    spans = [(0.0, 0.0)] if len(lines) == 1 else await _speech_spans(full, len(lines))
    for (start, end), part in zip(spans, parts):
        cut = ["-ss", f"{start:.3f}", "-to", f"{end:.3f}"] if end > start else []
        await _run("ffmpeg", "-v", "error", "-y", "-i", str(full), *cut,
                   "-c:a", "libmp3lame", "-q:a", "3", str(part))
    full.unlink(missing_ok=True)

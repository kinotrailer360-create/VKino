"""Озвучка через Lumean (голоса ElevenLabs): https://lumean.app, документация — api.lumean.app/docs/llm.md.

Ключ — LUMEAN_API_KEY. Голос — LUMEAN_VOICE_ID (если не задан, берётся первый мужской русский голос
из библиотеки ElevenLabs), готовый шаблон — LUMEAN_TEMPLATE_ID (если не задан, бот создаёт свой
«KinoMir360 TTS» и дальше переиспользует).

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
VOICE_ID = os.getenv("LUMEAN_VOICE_ID", "").strip()
TEMPLATE_ID = os.getenv("LUMEAN_TEMPLATE_ID", "").strip()
MODEL = os.getenv("LUMEAN_MODEL", "eleven_multilingual_v2").strip()
SPEED = float(os.getenv("LUMEAN_SPEED", "1.12"))  # 0.7..1.2; ~как +22% у голоса Microsoft
TEMPLATE_NAME = "KinoMir360 TTS"
PAUSE = 1.0  # сек тишины между фразами — по ним режем дорожку
WAIT = 240  # сек на готовность заказа
DONE = {"completed", "result_delivered"}
FAILED = {"failed", "compensated", "cancelled"}

log = logging.getLogger("shorts")
_template: str = TEMPLATE_ID
voice_name = ""  # какой голос выбран — для подписи в /voice


class LumeanError(RuntimeError):
    pass


def enabled() -> bool:
    return bool(KEY)


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


async def _template_id(session: aiohttp.ClientSession) -> str:
    """Свой шаблон ElevenLabs: найти по имени или создать один раз."""
    global _template
    if _template:
        return _template
    data = await _call(session, "GET", "/templates", params={"page": 1, "search": TEMPLATE_NAME,
                                                              "service": "elevenlabs"})
    for t in (data or {}).get("items") or []:
        if t.get("name") == TEMPLATE_NAME and t.get("id"):
            _template = str(t["id"])
            return _template
    config = {"tts_settings": {
        "mode": "mode_v1", "model_id": MODEL, "voice_id": await _pick_voice(session), "language_code": "ru",
        "advanced_voice_settings": True,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "use_speaker_boost": True, "speed": SPEED},
        "output_settings": {"audio_format": "mp3"},
    }}
    created = await _call(session, "POST", "/templates",
                          json={"service_key": "elevenlabs", "name": TEMPLATE_NAME, "config": config})
    _template = str(created["id"])
    log.info("shorts: Lumean template %s created", _template)
    return _template


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

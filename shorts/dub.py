"""Закадровый перевод трейлера: русские голоса из библиотеки ElevenLabs (Lumean) поверх оригинала.

Шаги (бот проводит по ним кнопками):
  1. fetch_source — видео по ссылке (Rutube, VK…) или файл из Telegram;
  2. translate — ИИ слушает дорожку: реплики со временем, пол и говорящий, перевод под длину реплики;
     автор проверяет и правит перевод в боте;
  3. make_dub — у каждого говорящего свой голос (мужской/женский), реплики подгоняются под время,
     оригинал приглушается под русским голосом; видео не перекодируется, если влезает в лимит Telegram.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import ai, daily, lumean, pronounce
from .daily import TELEGRAM_URL, YOUTUBE_URL, _duration, _progress, _run

log = logging.getLogger("shorts")

MAX_SECONDS = 8 * 60  # трейлер, а не фильм целиком
DUB_PAUSE = 2.0  # тишина между репликами в заказе Lumean — по ней режем дорожку
MAX_TEMPO = 1.35  # во сколько раз можно ускорить реплику, чтобы влезла в своё время
MAX_MB = 48  # Telegram-бот отправляет файлы до 50 МБ
EDGE_VOICES = {"male": "ru-RU-DmitryNeural", "female": "ru-RU-SvetlanaNeural"}  # если Lumean недоступен


@dataclass
class DubResult:
    video: Path
    seo: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------- исходник и перевод

async def fetch_source(url: str, work: Path) -> Path:
    """Скачать видео со звуком (до 720p) по ссылке."""
    cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-warnings", "--no-progress",
           "-f", "bv*[height<=720]+ba/b[height<=720]/b", "--merge-output-format", "mp4",
           "--max-filesize", "300M", "-o", str(work / "source.%(ext)s"), "--socket-timeout", "20",
           "--print", "after_move:filepath", url]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=300)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        proc.kill()
        await proc.wait()
        raise
    lines = [ln for ln in out.decode(errors="ignore").splitlines() if ln.strip()]
    if proc.returncode or not lines:
        raise RuntimeError(f"не скачалось: {err.decode(errors='ignore').strip()[-300:] or 'нет файла'}")
    return Path(lines[-1].strip())


async def has_audio(path: Path) -> bool:
    out = await _run("ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                     "-of", "csv=p=0", str(path))
    return bool(out.strip())


async def translate(src: Path, work: Path, key: str) -> tuple[list[dict[str, Any]], float]:
    """Реплики с переводом и длительность видео."""
    dur = await _duration(src)
    if dur > MAX_SECONDS:
        raise RuntimeError(f"видео {dur / 60:.0f} мин — дубляж рассчитан на трейлеры до {MAX_SECONDS // 60} мин")
    if not await has_audio(src):
        raise RuntimeError("в видео нет звуковой дорожки")
    audio = work / "speech.mp3"
    await _run("ffmpeg", "-v", "error", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
               "-c:a", "libmp3lame", "-b:a", "48k", str(audio))
    lines = await ai.transcribe_dub(key, audio.read_bytes(), dur)
    audio.unlink(missing_ok=True)
    return lines, dur


def _clock(sec: float) -> str:
    return f"{int(sec) // 60}:{int(sec) % 60:02d}"


def lines_text(lines: list[dict[str, Any]], voices: dict[str, dict[str, str]] | None = None) -> str:
    """Перевод для проверки: «3. 👨 A 0:12 — текст». Так же его можно прислать обратно с правками."""
    rows = []
    for i, ln in enumerate(lines, 1):
        icon = "👩" if ln["gender"] == "female" else "👨"
        rows.append(f"{i}. {icon} {ln['speaker']} {_clock(ln['start'])} — {ln['ru']}")
    return "\n".join(rows)


def apply_edits(lines: list[dict[str, Any]], text: str) -> int:
    """Правки автора: строки «3. новый текст» или «3. 👨 A 0:12 — новый текст». Возвращает, сколько правок.

    Пустой текст после номера («3.» или «3. -») — реплику не озвучивать.
    """
    changed = 0
    for raw in text.splitlines():
        m = re.match(r"\s*(\d+)[.)]\s*(.*)$", raw)
        if not m or not 1 <= int(m.group(1)) <= len(lines):
            continue
        body = m.group(2)
        if "—" in body and re.match(r"^\S*\s*\w{1,3}\s+\d+:\d\d\s+—", body):
            body = body.split("—", 1)[1]
        body = body.strip()
        lines[int(m.group(1)) - 1]["ru"] = "" if body in ("", "-", "—") else body
        changed += 1
    return changed


async def pick_voices(lines: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Каждому говорящему — свой голос из библиотеки: мужчинам мужские, женщинам женские, по очереди."""
    speakers: dict[str, str] = {}
    for ln in lines:
        speakers.setdefault(ln["speaker"], ln["gender"])
    out: dict[str, dict[str, str]] = {}
    used = {"male": 0, "female": 0}
    for spk, gender in speakers.items():
        voice = {"id": "", "name": EDGE_VOICES[gender].split("-")[2].replace("Neural", ""), "gender": gender}
        if lumean.enabled():
            try:
                lib = await lumean.library(gender)
                if lib:
                    v = lib[used[gender] % len(lib)]
                    voice = {"id": str(v["voice_id"]), "name": str(v.get("name") or "").split(" - ")[0][:30],
                             "gender": gender}
            except Exception as exc:  # noqa: BLE001 — тогда голос Microsoft
                log.warning("shorts: dub voice library failed: %s", exc)
        used[gender] += 1
        out[spk] = voice
    return out


def voices_text(voices: dict[str, dict[str, str]]) -> str:
    return ", ".join(f"{spk} — {v['name']} ({'жен.' if v['gender'] == 'female' else 'муж.'})"
                     for spk, v in voices.items())


# ---------------------------------------------------------------- озвучка и сведение

async def _edge(text: str, gender: str, path: Path) -> None:
    import edge_tts
    await asyncio.wait_for(edge_tts.Communicate(text, EDGE_VOICES[gender], rate="+10%").save(str(path)), 30)


async def _silence(path: Path, sec: float = 0.5) -> None:
    await _run("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
               "-t", f"{sec:.2f}", "-c:a", "libmp3lame", str(path))


async def _edge_safe(text: str, gender: str, path: Path) -> bool:
    """Голос Microsoft с повтором: «No audio was received» обычно — временное ограничение частых запросов."""
    for delay in (0, 8, 20):
        if delay:
            await asyncio.sleep(delay)
        try:
            await _edge(text, gender, path)
            if path.exists() and path.stat().st_size > 0:
                return True
        except Exception as exc:  # noqa: BLE001
            log.warning("shorts: Edge dub line failed (%s): %r", exc, text[:60])
    return False


async def _lumean_voice(texts: list[str], parts: list[Path], work: Path, voice_id: str) -> None:
    """Все реплики героя одним заказом; если дорожку не разрезать на реплики — по заказу на реплику."""
    try:
        await lumean.synthesize(texts, parts, work, DUB_PAUSE, "elevenlabs", voice_id)
    except lumean.LumeanError as exc:
        if "разрезать" not in str(exc):
            raise
        log.info("shorts: dub split failed (%s), ordering lines one by one", exc)
        for text, part in zip(texts, parts):
            await lumean.synthesize([text], [part], work, DUB_PAUSE, "elevenlabs", voice_id)


async def _voice_lines(lines: list[dict[str, Any]], voices: dict[str, dict[str, str]], work: Path) -> list[Path]:
    """mp3 на каждую реплику: голос героя из Lumean; не вышло — голос Microsoft; и он не смог — тишина."""
    spoken = await daily.spoken_lines([ln["ru"] for ln in lines])
    spoken = [t.replace(pronounce.STRESS, "") for t in spoken]
    parts = [work / f"dub_{i}.mp3" for i in range(len(lines))]
    notes: list[str] = []
    lost: list[int] = []
    by_voice: dict[str, list[int]] = {}
    for i, ln in enumerate(lines):
        if not re.search(r"[A-Za-zА-Яа-яЁё0-9]", spoken[i]):  # одни знаки — озвучивать нечего
            await _silence(parts[i])
            continue
        by_voice.setdefault(ln["speaker"], []).append(i)
    for spk, idx in by_voice.items():
        v = voices[spk]
        if v["id"]:
            try:
                await _lumean_voice([spoken[i] for i in idx], [parts[i] for i in idx], work, v["id"])
                continue
            except Exception as exc:  # noqa: BLE001
                log.warning("shorts: Lumean dub for %s failed (%s), using Edge", spk, exc)
                notes.append(f"🗣 Голос {v['name']} (Lumean) не сработал: {str(exc)[:200]} — "
                             "его реплики озвучены голосом Microsoft.")
        for i in idx:
            if not await _edge_safe(spoken[i], v["gender"], parts[i]):
                await _silence(parts[i])
                lost.append(i + 1)
            await asyncio.sleep(1.0)
    if lost:
        notes.append(f"⚠️ Не озвучились реплики № {', '.join(map(str, lost))} — в видео они без перевода.")
    daily.tts_note = "\n".join(notes) or None
    return parts


VOICE_DB = -17.0  # средняя громкость русского голоса, дБ (как у диктора поверх трейлера)
DUCK_BELOW = 14.0  # на столько дБ оригинал тише голоса, пока звучит перевод


async def _mean_db(files: list[Path], work: Path) -> float:
    """Средняя громкость (RMS, дБ) речи/звука из файлов, тишину не считаем."""
    inputs: list[str] = []
    for f in files:
        inputs += ["-i", str(f)]
    concat = "".join(f"[{k}:a]aresample=48000,aformat=channel_layouts=mono[c{k}];" for k in range(len(files)))
    concat += "".join(f"[c{k}]" for k in range(len(files))) + f"concat=n={len(files)}:v=0:a=1,"
    concat += "silenceremove=stop_periods=-1:stop_threshold=-45dB,volumedetect[o]"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-nostats", *inputs, "-filter_complex", concat, "-map", "[o]", "-f", "null", "-",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    log_text = err.decode(errors="ignore")  # volumedetect пишет в stderr
    m = re.search(r"mean_volume: (-?[\d.]+) dB", log_text)
    return float(m.group(1)) if m else VOICE_DB


async def _fit(part: Path, slot: float, out: Path) -> float:
    """Ускорить реплику (до MAX_TEMPO), чтобы влезла в slot секунд; вернуть итоговую длину."""
    dur = await _duration(part)
    tempo = min(max(dur / max(slot, 0.3), 1.0), MAX_TEMPO)
    await _run("ffmpeg", "-v", "error", "-y", "-i", str(part), "-af", f"atempo={tempo:.3f}", "-ar", "48000",
               "-ac", "1", str(out))
    return dur / tempo


async def make_dub(src: Path, lines: list[dict[str, Any]], work: Path, title: str = "",
                   source_url: str = "") -> DubResult:
    lines = [ln for ln in lines if ln["ru"].strip()]
    if not lines:
        raise RuntimeError("нет ни одной реплики для озвучки")
    daily.tts_note = None
    total = await _duration(src)
    await _progress(10, "Подбираю голоса")
    voices = await pick_voices(lines)
    await _progress(20, f"Озвучка: {len(lines)} реплик, голоса — {voices_text(voices)}")
    parts = await _voice_lines(lines, voices, work)

    await _progress(70, "Подгоняю реплики под время")
    fitted, delays, spans = [], [], []
    prev_end = 0.0
    for i, ln in enumerate(lines):
        nxt = lines[i + 1]["start"] if i + 1 < len(lines) else total
        start = max(ln["start"], prev_end + 0.05)
        slot = max(nxt - start - 0.1, ln["end"] - ln["start"])
        out = work / f"fit_{i}.wav"
        length = await _fit(parts[i], slot, out)
        fitted.append(out)
        delays.append(start)
        spans.append((start, start + length))
        prev_end = start + length

    await _progress(85, "Свожу звук")
    # громкость меряем, а не угадываем: голос — к VOICE_DB, оригинал под голосом — на DUCK_BELOW тише
    voice_db = await _mean_db(fitted, work)
    voice_gain = VOICE_DB - voice_db
    with_audio = await has_audio(src)
    orig_db = await _mean_db([src], work) if with_audio else -90.0
    duck = min(VOICE_DB - DUCK_BELOW - orig_db, -8.0)  # дБ для оригинала, пока звучит перевод
    inputs = ["-i", str(src)]
    chains = []
    for k, (f, t) in enumerate(zip(fitted, delays), 1):
        inputs += ["-i", str(f)]
        ms = round(t * 1000)
        chains.append(f"[{k}:a]volume={voice_gain:.1f}dB,adelay={ms}|{ms}[p{k}]")
    n = len(fitted)
    # где звучит перевод (с запасом на вдох и затухание) — оригинал тише, в паузах — как был
    zones = "+".join(f"between(t,{a - 0.15:.2f},{b + 0.3:.2f})" for a, b in spans)
    level = 10 ** (duck / 20)
    orig = (f"[0:a]aresample=48000,aformat=channel_layouts=stereo,"
            f"volume='if(gt({zones},0),{level:.3f},1)':eval=frame[orig];") if with_audio else \
        f"anullsrc=r=48000:cl=stereo,atrim=0:{total:.2f}[orig];"
    filt = (";".join(chains) + ";" + "".join(f"[p{k}]" for k in range(1, n + 1))
            + f"amix=inputs={n}:normalize=0:duration=longest,aformat=channel_layouts=stereo,"
            + f"apad=whole_dur={total:.2f}[dub];"  # до конца видео, иначе оригинал обрежется
            + orig
            + "[orig][dub]amix=inputs=2:normalize=0:duration=first,alimiter=limit=0.95[a]")
    audio = work / "dub_mix.m4a"
    await _run("ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", filt, "-map", "[a]",
               "-c:a", "aac", "-b:a", "160k", str(audio))

    await _progress(93, "Собираю видео")
    out = work / "dub.mp4"
    await _run("ffmpeg", "-v", "error", "-y", "-i", str(src), "-i", str(audio), "-map", "0:v:0", "-map", "1:a",
               "-c:v", "copy", "-c:a", "copy", "-shortest", "-movflags", "+faststart", str(out))
    if out.stat().st_size > MAX_MB * 1024 * 1024:  # не влезает в Telegram — пережимаем видео в 720p
        kbps = int(max(500, MAX_MB * 8 * 1024 / total - 200))
        small = work / "dub_small.mp4"
        await _run("ffmpeg", "-v", "error", "-y", "-i", str(out), "-vf", "scale=-2:min(720\\,ih)",
                   "-c:v", "libx264", "-preset", "veryfast", "-threads", "1", "-b:v", f"{kbps}k",
                   "-maxrate", f"{int(kbps * 1.3)}k", "-bufsize", f"{kbps * 2}k", "-c:a", "copy",
                   "-movflags", "+faststart", str(small))
        small.replace(out)
    audio.unlink(missing_ok=True)
    result = DubResult(out, build_dub_seo(title, source_url, voices))
    if daily.tts_note:
        result.seo["warning"] = daily.tts_note
    return result


def build_dub_seo(title: str, source_url: str, voices: dict[str, dict[str, str]]) -> dict[str, str]:
    name = title.strip() or "Трейлер"
    head = f"{name} — трейлер на русском | перевод KinoMir360"
    if len(head) > 100:
        head = f"{name[:60]} — трейлер на русском"[:100]
    lines = [f"🎙 Русский закадровый перевод трейлера «{name}» от KinoMir360.", ""]
    if source_url:
        lines += [f"Оригинал: {source_url}", ""]
    lines += ["Как вам перевод? Пишите в комментариях 👇", "",
              f"📺 YouTube: {YOUTUBE_URL}",
              f"✈️ Telegram: {TELEGRAM_URL} — новинки, трейлеры и подборки каждый день", "",
              "#трейлер #трейлернарусском #переводтрейлера #кино #KinoMir360"]
    tags = [t for t in [f"{name.lower()} трейлер", f"{name.lower()} трейлер на русском", name.lower(),
                        "трейлер на русском", "перевод трейлера", "русский трейлер", "новый трейлер",
                        "трейлер 2026", "KinoMir360", "киномир360"] if t]
    pinned = f"Больше трейлеров на русском — в Telegram {TELEGRAM_URL} 🍿"
    return {"title": head, "description": "\n".join(lines), "tags": ", ".join(tags), "pinned": pinned}

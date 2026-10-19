"""Короткие фрагменты трейлеров (YouTube, Rutube) через yt-dlp — только нужный кусок, без звука."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

log = logging.getLogger("shorts")

CLIP_FORMAT = "bv*[height<=720][ext=mp4]/bv*[height<=720]/b[height<=720]/b"


async def download_clip(url: str, out: Path, start: float = 20.0, length: float = 4.0,
                        timeout: float = 120) -> Path:
    """Скачать отрезок [start, start+length] видео в out (mp4, 720p, без звука)."""

    def run(partial: bool) -> Path:
        import yt_dlp

        opts = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "format": CLIP_FORMAT,
            "outtmpl": str(out.with_name(out.stem + "_raw.%(ext)s")),
            "socket_timeout": 20,
            "retries": 1,
        }
        if partial:
            opts["download_ranges"] = yt_dlp.utils.download_range_func(None, [(start, start + length)])
            opts["force_keyframes_at_cuts"] = True
        else:
            opts["max_filesize"] = 80 * 1024 * 1024  # трейлер целиком — только если не огромный
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return Path(ydl.prepare_filename(info))

    seek = 0.0
    try:
        raw = await asyncio.wait_for(asyncio.to_thread(run, True), timeout=timeout)
    except Exception as exc:
        if "partially downloaded" not in str(exc):
            raise
        # источник не умеет отдавать кусок — качаем целиком и режем сами
        log.info("shorts: partial download unsupported for %s, downloading whole", url)
        raw = await asyncio.wait_for(asyncio.to_thread(run, False), timeout=timeout)
        seek = start

    # единый вид: mp4 h264, без звука, ровно length секунд
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-y", "-ss", str(seek), "-i", str(raw), "-t", str(length), "-an",
        "-vf", "scale=-2:720", "-c:v", "libx264", "-preset", "veryfast", "-threads", "1", str(out),
        stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    raw.unlink(missing_ok=True)
    if proc.returncode:
        raise RuntimeError(f"ffmpeg: {err.decode(errors='ignore')[-300:]}")
    return out


RUTUBE_SEARCH = "https://rutube.ru/api/search/video/"
TRAILER_WORDS = ("трейлер", "тизер", "trailer", "teaser")


def _norm(text: object) -> str:
    import re
    text = str(text or "").lower().replace("ё", "е")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text)).strip()


async def rutube_trailer(titles: list[str], year: int | None = None) -> dict | None:
    """Лучший трейлер на Rutube: название фильма + «трейлер», не платный, не 18+, 30–400 с.

    Возвращает {"url", "duration", "title"} или None. Поиск работает не из всех стран:
    с серверов в США Rutube отдаёт пустой список, из Европы — нормально.
    """
    import aiohttp

    names = [_norm(t) for t in titles if t]
    if not names:
        return None
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(RUTUBE_SEARCH, params={"query": f"{titles[0]} трейлер", "format": "json"},
                               headers={"accept": "application/json"}) as resp:
            data = await resp.json(content_type=None)

    best, best_score = None, 0.0
    for v in (data.get("results") or [])[:30]:
        if not isinstance(v, dict) or v.get("is_paid") or v.get("is_adult") or v.get("is_hidden"):
            continue
        title, dur = _norm(v.get("title")), float(v.get("duration") or 0)
        if not 30 <= dur <= 400 or not any(n in title for n in names):
            continue
        if not any(w in title for w in TRAILER_WORDS):
            continue
        score = 1.0
        score += 3 if v.get("is_official") else 0
        score += 1 if "русск" in title or "дубл" in title else 0
        score += 1 if year and str(year) in title else 0
        score -= 2 if any(w in title for w in ("обзор", "реакция", "разбор", "фанатск", "fan")) else 0
        if score > best_score:
            best, best_score = v, score
    if not best:
        return None
    url = best.get("video_url") or f"https://rutube.ru/video/{best.get('id')}/"
    return {"url": str(url), "duration": float(best.get("duration") or 0), "title": str(best.get("title") or "")}


def clip_start(duration: float, length: float) -> float:
    """Откуда резать: ~30% трейлера — после логотипов студий и возрастных плашек."""
    if duration <= 0:
        return 20.0
    return max(8.0, min(duration * 0.3, duration - length - 8))

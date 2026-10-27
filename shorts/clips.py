"""Короткие фрагменты трейлеров (YouTube, Rutube) через yt-dlp — только нужный кусок, без звука."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

log = logging.getLogger("shorts")

CLIP_FORMAT = "bv*[height<=720][ext=mp4]/bv*[height<=720]/b[height<=720]/b"


async def _yt_dlp(url: str, outtmpl: str, extra: list[str], timeout: float, fmt: str = CLIP_FORMAT) -> Path:
    """yt-dlp отдельным процессом: по таймауту его можно убить, а память освобождается сразу.

    В потоке бота он оставался висеть после таймаута и держал память — Railway падал по нехватке.
    """
    cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-warnings", "--no-progress",
           "-f", fmt, "-o", outtmpl, "--socket-timeout", "20", "--retries", "1",
           "--print", "after_move:filepath", *extra, url]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        proc.kill()
        await proc.wait()
        raise
    lines = [ln for ln in out.decode(errors="ignore").splitlines() if ln.strip()]
    if proc.returncode or not lines:
        raise RuntimeError(f"yt-dlp: {err.decode(errors='ignore').strip()[-300:] or 'нет файла'}")
    return Path(lines[-1].strip())


async def download_clip(url: str, out: Path, start: float = 20.0, length: float = 4.0,
                        timeout: float = 120) -> Path:
    """Скачать отрезок [start, start+length] видео в out (mp4, 720p, без звука)."""
    tmpl = str(out.with_name(out.stem + "_raw.%(ext)s"))
    seek = 0.0
    try:
        raw = await _yt_dlp(url, tmpl, ["--download-sections", f"*{start:.2f}-{start + length:.2f}",
                                        "--force-keyframes-at-cuts"], timeout)
    except RuntimeError as exc:
        if "partially downloaded" not in str(exc):
            raise
        # источник не умеет отдавать кусок — качаем целиком и режем сами
        log.info("shorts: partial download unsupported for %s, downloading whole", url)
        raw = await _yt_dlp(url, tmpl, ["--max-filesize", "80M"], timeout)
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
NOT_TRAILER = ("интервью", "о съемках", "о съёмках", "за кадром", "со съемок", "со съёмок", "фильм о фильме",
               "как снимали", "пресс конференц", "премьера фильма", "behind the scenes", "featurette",
               "making of", "interview", "подкаст", "стрим")


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
        if any(w in title for w in NOT_TRAILER):
            continue  # интервью, рассказ о съёмках и т.п. — не трейлер, хоть слово «трейлер» и есть
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


async def download_full(url: str, out_dir: Path, timeout: float = 150) -> Path:
    """Скачать трейлер целиком (видео без звука, до 720p, до 80 МБ) в out_dir; путь к файлу."""
    return await _yt_dlp(url, str(out_dir / "trailer_full.%(ext)s"), ["--max-filesize", "80M"], timeout)


async def sample_frames(src: Path, out_dir: Path, every: float = 4.0, duration: float = 0.0,
                        limit: int = 40) -> list[tuple[float, Path]]:
    """Кадры трейлера каждые every секунд (без начала с логотипами и финальных титров) для выбора ИИ."""
    head = max(6.0, duration * 0.08) if duration else 6.0
    tail = duration * 0.9 if duration else 0.0
    if duration and (tail - head) / every > limit:
        every = (tail - head) / limit
    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{head:.2f}"]
    if tail:
        cmd += ["-to", f"{tail:.2f}"]
    cmd += ["-i", str(src), "-vf", f"fps=1/{every:.3f},scale=448:-2", "-q:v", "4",
            str(out_dir / "frame_%03d.jpg")]
    proc = await asyncio.create_subprocess_exec(*cmd, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode:
        raise RuntimeError(f"ffmpeg frames: {err.decode(errors='ignore')[-200:]}")
    frames = sorted(out_dir.glob("frame_*.jpg"))[:limit]
    # кадр k снят примерно на head + k*every (+ полшага от фильтра fps)
    return [(head + k * every + every / 2, f) for k, f in enumerate(frames)]


async def cut_clip(src: Path, out: Path, start: float, length: float) -> Path:
    """Вырезать кусок из уже скачанного видео (mp4 h264, без звука, 720p)."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-y", "-ss", f"{max(start, 0):.2f}", "-i", str(src), "-t", str(length), "-an",
        "-vf", "scale=-2:720", "-c:v", "libx264", "-preset", "veryfast", "-threads", "1", str(out),
        stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode:
        raise RuntimeError(f"ffmpeg cut: {err.decode(errors='ignore')[-200:]}")
    return out


POST_VIDEO_MAX = 48 * 1024 * 1024  # бот Telegram отправляет файлы до 50 МБ


async def download_trailer(url: str, out: Path, max_seconds: int = 180, timeout: float = 240) -> Path:
    """Трейлер со звуком для поста в Telegram: до max_seconds, mp4 h264/aac, не больше POST_VIDEO_MAX."""
    raw = await _yt_dlp(url, str(out.with_name(out.stem + "_src.%(ext)s")), ["--max-filesize", "150M"], timeout,
                        fmt="b[height<=720][ext=mp4]/bv*[height<=720]+ba/b[height<=720]/b")
    try:
        for height, crf in ((720, 28), (480, 30)):
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-v", "error", "-y", "-i", str(raw), "-t", str(max_seconds), "-vf", f"scale=-2:{height}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-threads", "2",
                "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out),
                stderr=asyncio.subprocess.PIPE)
            _, err = await proc.communicate()
            if proc.returncode:
                raise RuntimeError(f"ffmpeg: {err.decode(errors='ignore')[-300:]}")
            if out.stat().st_size <= POST_VIDEO_MAX:
                return out
        raise RuntimeError("трейлер больше 50 МБ даже в 480p")
    finally:
        raw.unlink(missing_ok=True)

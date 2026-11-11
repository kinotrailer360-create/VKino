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


async def download_video(url: str, out_dir: Path, timeout: float = 1200) -> Path:
    """Видео со звуком целиком (до 720p, mp4) — для кнопки «⬇️ Скачать видео»."""
    return await _yt_dlp(url, str(out_dir / "video.%(ext)s"),
                         ["--merge-output-format", "mp4", "--max-filesize", "1500M"], timeout,
                         fmt="bv*[height<=720]+ba/b[height<=720]/bv*+ba/b")


async def split_for_telegram(src: Path, limit: int = 48 * 1024 * 1024) -> list[Path]:
    """Видео больше лимита Telegram (50 МБ у ботов) — на части без перекодирования (быстро, без потери
    качества); каждая часть меньше лимита."""
    size = src.stat().st_size
    if size <= limit:
        return [src]
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(src),
        stdout=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    duration = float(out.decode().strip() or 0)
    if duration <= 0:
        raise RuntimeError("не удалось узнать длину видео")
    parts = int(size / (limit * 0.85)) + 1  # запас: части режутся по ключевым кадрам и выходят неровными
    pattern = src.with_name(src.stem + "_part%02d.mp4")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-y", "-i", str(src), "-c", "copy", "-map", "0", "-f", "segment",
        "-segment_time", f"{duration / parts:.1f}", "-reset_timestamps", "1", str(pattern),
        stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode:
        raise RuntimeError(f"ffmpeg: {err.decode(errors='ignore')[-300:]}")
    files = sorted(src.parent.glob(src.stem + "_part*.mp4"))
    if any(f.stat().st_size > limit for f in files):
        raise RuntimeError("видео слишком плотное — части всё равно больше 50 МБ")
    return files


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


def _season_in(title: str, n: int) -> bool:
    """В названии ролика есть именно этот сезон: «3 сезон», «3-й сезон», «сезон 3», «season 3», «S03»."""
    import re
    return bool(re.search(rf"(?<!\d){n}\s*(?:-?(?:й|го|ий))?\s*сезон|сезон\w*\s*{n}(?!\d)|season\s*{n}(?!\d)|"
                          rf"\bs0?{n}(?!\d)", title))


def _age_days(v: dict) -> float | None:
    """Сколько дней назад выложен ролик Rutube (дата бывает строкой ISO или числом секунд); None — не знаем."""
    from datetime import datetime, timezone
    for key in ("publication_ts", "created_ts", "published_at", "created_at"):
        raw = v.get(key)
        if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.strip().isdigit()):
            sec = float(raw) / (1000 if float(raw) > 1e11 else 1)  # бывает и в миллисекундах
            return (datetime.now(timezone.utc).timestamp() - sec) / 86400
        raw = str(raw or "")
        if raw:
            try:
                ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
                return (datetime.now(timezone.utc) - ts).total_seconds() / 86400
            except ValueError:
                continue
    return None


async def rutube_trailer(titles: list[str], year: int | None = None, season: int | None = None,
                         fresh_days: float | None = None) -> dict | None:
    """Лучший трейлер на Rutube: название фильма + «трейлер», не платный, не 18+, 30–400 с.
    season — нужен трейлер именно этого сезона (в названии ролика есть номер сезона; со 2-го сезона без
    номера не берём — это может быть трейлер первого). fresh_days — ролик выложен не раньше (новость о
    только что вышедшем трейлере: старый трейлер не подходит).

    Возвращает {"url", "duration", "title"} или None. Поиск работает не из всех стран:
    с серверов в США Rutube отдаёт пустой список, из Европы — нормально.
    """
    import aiohttp

    names = [_norm(t) for t in titles if t]
    if not names:
        return None
    timeout = aiohttp.ClientTimeout(total=15)
    queries = [f"{t} {season} сезон трейлер" if season and season > 1 else f"{t} трейлер"
               for t in dict.fromkeys(t for t in titles[:2] if t)]
    for query in queries:  # по-русски не нашлось — пробуем оригинальное название
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(RUTUBE_SEARCH, params={"query": query, "format": "json"},
                                   headers={"accept": "application/json"}) as resp:
                data = await resp.json(content_type=None)
        found = _best_trailer(data, names, year, season, fresh_days)
        if found:
            return found
    return None


def _best_trailer(data: dict, names: list[str], year: int | None, season: int | None,
                  fresh_days: float | None) -> dict | None:
    best, best_score = None, 0.0
    for v in (data.get("results") or [])[:30]:
        if not isinstance(v, dict) or v.get("is_paid") or v.get("is_adult") or v.get("is_hidden"):
            continue
        title, dur = _norm(v.get("title")), float(v.get("duration") or 0)
        if not 30 <= dur <= 400 or not any(n in title for n in names):
            continue
        # короткое название («Кошелёк или жизнь», «Хэллоуин») встречается и в чужих роликах («Скуби-Ду: Кошелёк
        # или жизнь») — тогда в названии ролика должен быть год или второе (оригинальное) название фильма
        hits = [n for n in names if n in title]
        if all(len(n.split()) <= 3 for n in hits) and not (year and str(year) in title) and len(set(hits)) < 2:
            before = title.split(hits[0], 1)[0].strip()
            if before and not any(w in before for w in TRAILER_WORDS):
                continue  # перед названием что-то ещё — скорее всего это другой фильм или мультфильм
        if not any(w in title for w in TRAILER_WORDS):
            continue
        if season and season > 1 and not _season_in(title, season):
            continue  # трейлер другого сезона (или непонятно какого)
        age = _age_days(v)
        if fresh_days is not None and age is not None and age > fresh_days:
            continue  # старый ролик, а новость — о новом трейлере
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
                        limit: int = 40, whole: bool = False) -> list[tuple[float, Path]]:
    """Кадры трейлера каждые every секунд (без начала с логотипами и финальных титров) для выбора ИИ.
    whole — видео целиком (своя сцена автора: в ней нет логотипов, важное может быть с первой секунды)."""
    head = 0.0 if whole else max(6.0, duration * 0.08) if duration else 6.0
    tail = (duration if whole else duration * 0.9) if duration else 0.0
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

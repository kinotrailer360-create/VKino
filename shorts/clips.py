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

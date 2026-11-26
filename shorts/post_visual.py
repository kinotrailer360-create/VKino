"""Strict visual review for news attachments. Never send an unverified asset as approved."""
from __future__ import annotations

import asyncio
import hashlib
import math
import tempfile
from pathlib import Path

from PIL import Image

from . import ai, process

MAX_ASSETS = 10
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv"}
LOCAL_VIDEO = ("-protocol_whitelist", "file,pipe", "-format_whitelist", "mov,matroska,webm")
MAX_REVIEW_SECONDS = 150


def sample_times(duration):
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Некорректная длительность видео")
    return tuple(duration * f for f in (.1, .5, .9))


async def samples(path: Path, work: Path) -> list[Image.Image]:
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        with Image.open(path) as im:
            if im.width * im.height > 30_000_000:
                raise ValueError("Слишком большое изображение")
            out = im.convert("RGB")
            out.thumbnail((1024, 1024))
            return [out]
    raw = await process.run("ffprobe", "-v", "error", *LOCAL_VIDEO, "-show_entries", "format=duration",
                            "-of", "default=noprint_wrappers=1:nokey=1", str(path), timeout=15)
    points = sample_times(float(raw.strip()))
    result = []
    try:
        with tempfile.TemporaryDirectory(prefix="post-review-", dir=work) as folder:
            for i, second in enumerate(points):
                frame = Path(folder) / f"{i}.jpg"
                await process.run("ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", str(second),
                                  *LOCAL_VIDEO, "-threads", "1", "-i", str(path),
                                  "-frames:v", "1", "-vf", "scale=768:-2", str(frame), timeout=15)
                with Image.open(frame) as im:
                    result.append(im.convert("RGB"))
        return result
    except BaseException:
        for image in result:
            image.close()
        raise


async def review_asset(path: Path, text: str, work: Path, key: str):
    images = []
    try:
        images = await samples(path, work)
        if len(images) == 1 and path.suffix.lower() not in VIDEO_EXTENSIONS:
            return await ai.media_fits(key, images[0], text)
        if len(images) == 3 and path.suffix.lower() in VIDEO_EXTENSIONS:
            return await ai.media_sequence_fits(key, images, text)
        return None, "Не удалось получить все кадры"
    except (OSError, ValueError, RuntimeError, TimeoutError):
        return None, "Не удалось прочитать или проверить материал"
    finally:
        for image in images:
            image.close()


async def review_files(files, text: str, work: Path, key: str, notes=None) -> list[Path]:
    """Check every attached candidate; reject unknown, malformed, duplicate and timed-out assets."""
    keep, seen = [], set()
    stop = False
    deadline = asyncio.get_running_loop().time() + MAX_REVIEW_SECONDS
    for i, path in enumerate(files[:MAX_ASSETS], 1):
        try:
            with path.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
        except OSError:
            digest = None
        if digest is not None and digest in seen:
            if notes is not None:
                notes.append(f"№{i}: повтор файла — исключён")
            continue
        if digest is not None:
            seen.add(digest)
        fits, reason = None, "Проверка ИИ недоступна или превышен лимит времени"
        remaining = deadline - asyncio.get_running_loop().time()
        if digest is None:
            reason = "Не удалось прочитать файл"
        elif not stop and remaining > 0:
            try:
                async with asyncio.timeout(remaining):
                    fits, reason = await review_asset(path, text, work, key)
            except TimeoutError:
                stop = True
        if fits is True:
            keep.append(path)
        # A provider outage should not fan out into ten futile API requests.
        elif fits is None and reason.startswith("Проверка ИИ недоступна"):
            stop = True
        if notes is not None:
            status = "✅" if fits is True else "❌ исключён" if fits is False else "❔ не проверен, исключён"
            notes.append(f"№{i}: {status} — {reason[:180]}")
    if len(files) > MAX_ASSETS and notes is not None:
        notes.append(f"Сверх лимита {MAX_ASSETS} файлов материал не добавлен.")
    return keep

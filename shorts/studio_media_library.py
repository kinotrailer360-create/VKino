"""Bounded scene media selection across user uploads and approved source URLs.

Inputs are references already authorized to the Studio owner. The model only
proposes candidate shots; neither relevance nor publishing rights are approved.
"""
from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from PIL import Image

from . import ai, clips, process

log = logging.getLogger("shorts.media_library")

# Explicit, public platforms: do not let yt-dlp follow arbitrary user URLs.
PLATFORMS = (
    "youtube.com", "youtu.be", "rutube.ru", "vk.com",
    "vkvideo.ru", "vk.ru", "vimeo.com", "instagram.com",
    "tiktok.com", "x.com", "twitter.com",
)
MAX_MEDIA = 20
MAX_LINKS = 5
MAX_FRAMES = 4


def allowed_source(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        return (parsed.scheme == "https" and parsed.username is None
                and parsed.password is None
                and (not parsed.port or parsed.port == 443)
                and any(host == domain or host.endswith("." + domain)
                        for domain in PLATFORMS))
    except ValueError:
        return False


def required_length(scene: dict[str, Any]) -> float:
    duration = float(scene.get("duration") or 0)
    if not math.isfinite(duration) or not 2 <= duration <= 30:
        raise ValueError("Длительность сцены должна быть от 2 до 30 секунд")
    # TTS sometimes outlasts the planned narration by up to a few seconds.
    return min(30.0, duration + 3.0)


async def video_duration(path: Path) -> float:
    raw = await process.run("ffprobe", "-v", "error",
                            "-show_entries", "format=duration",
                            "-of", "default=noprint_wrappers=1:nokey=1",
                            str(path), timeout=20)
    try:
        value = float(raw.strip())
        if not math.isfinite(value) or value <= 0:
            raise ValueError("Некорректная длительность файла")
        return value
    except (TypeError, ValueError) as exc:
        raise ValueError("Невозможно прочитать длительность видео") from exc


async def matches(path: Path, scene_text: str, key: str) -> bool:
    if not ai.available(key):
        return False
    try:
        with Image.open(path) as im:
            if im.width * im.height > 30_000_000:
                return False
            fit, _ = await ai.media_fits(key, im.convert("RGB"), scene_text)
            return fit is True
    except (OSError, ValueError) as exc:
        log.info("Candidate rejected: %s", type(exc).__name__)
        return False


async def frame_at(source: Path, out: Path, second: float) -> Path:
    await process.run("ffmpeg", "-v", "error", "-y",
                      "-ss", str(max(0.0, second)), "-i", str(source),
                      "-frames:v", "1", "-vf", "scale=448:-2",
                      str(out), timeout=25)
    return out


async def choose_video(source: Path, scene: dict[str, Any],
                       folder: Path, key: str) -> tuple[float, float] | None:
    """Find a continuous shot by checking two frames in each eligible window."""
    length = required_length(scene)
    total = await video_duration(source)
    if total < length:
        return None
    samples_dir = folder / "sample"
    samples_dir.mkdir(parents=True, exist_ok=True)
    samples = await clips.sample_frames(
        source, samples_dir, every=max(2.0, total / MAX_FRAMES),
        duration=total, limit=MAX_FRAMES, whole=True,
    )
    for i, (timestamp, preview) in enumerate(samples):
        start = min(max(0.0, timestamp - 1.0), total - length)
        if not await matches(preview, scene["text"], key):
            continue
        second = await frame_at(source, folder / f"check_{i}.jpg",
                                start + min(length - 0.1, max(1.0, length * 0.7)))
        if await matches(second, scene["text"], key):
            return round(start, 3), round(start + length, 3)
    return None


async def select(bridge: Any, admin: int, scene: dict[str, Any],
                 project: dict[str, Any], work: Path, key: str) -> dict[str, Any] | None:
    """Scan all provided videos first, then animated/still images.

    Each asset is resolved through the owner's authenticated Studio storage.
    Results are suggestions; callers must keep scene approval gates intact.
    """
    ids = project.get("mediaLibrary") or []
    urls = project.get("sourceUrls") or []
    if not ids and not urls:
        return None
    if not ai.available(key):
        raise ValueError("Для проверки видеоряда настройте ИИ в /ai")
    failures: list[str] = []
    sources: list[tuple[Path, dict[str, Any], str, str | None]] = []
    for identifier in ids[:MAX_MEDIA]:
        try:
            path, meta = await bridge.resolve_file(admin, identifier)
            sources.append((path, meta, str(meta.get("name") or identifier), identifier))
        except (ValueError, OSError) as exc:
            failures.append(f"Файл недоступен: {type(exc).__name__}")

    # Inspect *all* uploaded video files before choosing, rather than returning
    # the first matching shot. A shortlist lets the Site offer alternatives.
    videos = [entry for entry in sources if entry[1].get("mime", "").startswith("video/")]
    images = [entry for entry in sources if entry not in videos]
    suggestions: list[dict[str, Any]] = []
    for index, (path, meta, label, source_id) in enumerate(videos):
        try:
            location = work / f"video_{index}"
            location.mkdir(parents=True, exist_ok=True)
            window = await choose_video(path, scene, location, key)
            if window is None:
                failures.append(f"Нет подходящего фрагмента: {label[:45]}")
                continue
            start, end = window
            suggestions.append({"asset": {"id": source_id, "mime": meta["mime"], "name": label},
                                "start": start, "end": end, "source": label,
                                "rights": "author_uploaded"})
        except Exception as exc:  # noqa: BLE001
            failures.append(f"Ошибка анализа видео: {type(exc).__name__}")
            log.warning("Media candidate error: %s", type(exc).__name__)
    if suggestions:
        chosen = dict(suggestions[0])
        chosen["alternatives"] = suggestions[1:4]
        chosen["note"] = (
            f"Проверено {len(videos)} видео; найдено подходящих вариантов: {len(suggestions)}. "
            "ИИ сверил два кадра каждого предложенного фрагмента. "
            "Подтвердите актёра, сцену и права перед монтажом; ничего не утверждено автоматически.")
        chosen["errors"] = failures[:8]
        return chosen

    # External source links are fetched sequentially *only if* author videos
    # had no match. This avoids caching five 80 MB videos needlessly.
    for index, url in enumerate(urls[:MAX_LINKS]):
        if not allowed_source(url):
            failures.append("Ссылка на неподдерживаемую площадку")
            continue
        directory = work / f"external_{index}"
        directory.mkdir(parents=True, exist_ok=True)
        downloaded = None
        try:
            downloaded = await clips.download_full(url, directory, timeout=90)
            window = await choose_video(downloaded, scene, directory, key)
            if not window:
                failures.append(f"Нет подходящего фрагмента по ссылке: {url[:40]}")
                continue
            start, end = window
            cut = work / f"selected_{index}.mp4"
            await clips.cut_clip(downloaded, cut, start, end - start)
            asset = await bridge.store_file(admin, cut, "video/mp4", "source-clip.mp4")
            return {"asset": asset, "start": 0.0, "end": end - start,
                    "source": url, "sourceStart": start, "rights": "unconfirmed",
                    "alternatives": [],
                    "note": "Фрагмент найден по предоставленной ссылке. "
                            "Подтвердите его содержание и право использования; "
                            "публикация автоматически не разрешена.",
                    "errors": failures[:8]}
        except Exception as exc:  # noqa: BLE001
            failures.append(f"Видео по ссылке недоступно: {type(exc).__name__}")
            log.warning("External source could not be used: %s", type(exc).__name__)
        finally:
            if downloaded is not None:
                downloaded.unlink(missing_ok=True)
    for path, meta, label, source_id in images:
        try:
            if source_id and await matches(path, scene["text"], key):
                return {"asset": {"id": source_id, "mime": meta["mime"], "name": label},
                        "source": label, "rights": "author_uploaded",
                        "note": "Кадр подобран из файлов автора; проверьте смысл перед утверждением.",
                        "errors": failures[:8]}
        except Exception as exc:  # noqa: BLE001
            failures.append(f"Ошибка фото: {type(exc).__name__}")
    return None

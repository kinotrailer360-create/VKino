"""«Сделать по образцу»: из чужого ролика-подборки — тема и список фильмов для нашей версии.

Берём только идею и список: картинки, текст, голос и монтаж у нас свои (daily.make_custom_short).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PIL import Image

from . import ai
from .daily import Fetch, _duration, _run, _title, find_film

log = logging.getLogger("shorts")

MAX_SECONDS = 5 * 60


async def extract(src: Path, work: Path, key: str) -> dict[str, Any]:
    """{"title", "films": [{"ru", "en", "year"}]} из видео-файла."""
    dur = await _duration(src)
    if dur > MAX_SECONDS:
        raise RuntimeError(f"ролик {dur / 60:.0f} мин — нужен короткий, до {MAX_SECONDS // 60} мин")
    small = work / "look.mp4"  # лёгкая копия для ИИ: 360p, 2 кадра/с, моно-звук
    await _run("ffmpeg", "-v", "error", "-y", "-i", str(src), "-vf", "scale=-2:360,fps=2", "-c:v", "libx264",
               "-preset", "veryfast", "-crf", "32", "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "32k",
               str(small))
    audio = work / "look.mp3"
    has_audio = True
    try:
        await _run("ffmpeg", "-v", "error", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
                   "-c:a", "libmp3lame", "-b:a", "48k", str(audio))
    except RuntimeError:
        has_audio = False
    frames_dir = work / "look_frames"
    frames_dir.mkdir(exist_ok=True)
    step = max(1.0, dur / 24)
    await _run("ffmpeg", "-v", "error", "-y", "-i", str(src), "-vf", f"fps=1/{step:.2f},scale=-2:360",
               str(frames_dir / "f%03d.jpg"))
    frames = [Image.open(f).convert("RGB") for f in sorted(frames_dir.glob("f*.jpg"))[:24]]
    return await ai.extract_list(key, small.read_bytes(), frames, audio.read_bytes() if has_audio else b"")


async def resolve(fetch: Fetch, films: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Найти фильмы на Кинопоиске: [{"query", "id" | None, "label"}] в том же порядке."""
    out = []
    for f in films:
        queries = []
        for name in (f.get("ru"), f.get("en")):
            if name:
                queries.append(f"{name} {f['year']}" if f.get("year") else str(name))
        found = None
        for q in queries:
            try:
                found = await find_film(fetch, q)
            except Exception as exc:  # noqa: BLE001
                log.warning("shorts: film lookup %r failed: %s", q, exc)
            if found:
                break
        label = f"{_title(found)} ({found.get('year')})" if found and found.get("year") else \
            (_title(found) if found else "")
        out.append({"query": queries[0] if queries else "?", "id": int(found["id"]) if found else None,
                    "label": label})
    return out


def parse_plan(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Текст автора: первая строка — тема, дальше фильм на строку («Сияние 1980», номера можно оставить)."""
    import re
    rows = [re.sub(r"^\s*\d+[.)]\s*", "", ln).strip(" -—•\t") for ln in text.splitlines()]
    rows = [r for r in rows if r]
    if len(rows) < 2:
        raise ValueError("нужна тема и хотя бы один фильм")
    films = []
    for r in rows[1:]:
        r = re.sub(r"\s*[—–-]\s*(✅|❌).*$", "", r)  # строка из нашего же списка с пометкой «— ✅ найден …»
        m = re.search(r"[\s(]+(19\d\d|20\d\d)\)?$", r)
        films.append({"ru": r[: m.start()].strip() if m else r, "en": "", "year": int(m.group(1)) if m else None})
    return rows[0], films

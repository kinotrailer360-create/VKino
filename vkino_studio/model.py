"""Validated, local-only project contract for approved VKino Studio renders.

Paths are always relative to the manifest directory, never arbitrary URLs or
filesystem locations. Remote workers must receive media into this directory
before creating a manifest. This is intentionally separate from VKino's bot API.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path, PureWindowsPath

IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_TYPES = {".mp4", ".mov", ".mkv", ".webm"}
AUDIO_TYPES = {".wav", ".mp3", ".m4a", ".aac", ".ogg"}


@dataclass(frozen=True)
class Scene:
    source: Path
    voice: Path
    text: str
    headline: str
    duration: float
    start: float
    rights: str
    approved: bool

    @property
    def is_image(self) -> bool:
        return self.source.suffix.lower() in IMAGE_TYPES


@dataclass(frozen=True)
class Project:
    title: str
    width: int
    height: int
    fps: int
    captions: bool
    scenes: tuple[Scene, ...]


def _local_file(base: Path, value: object, extensions: set[str]) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("Путь к файлу должен быть непустой строкой")
    p = Path(value)
    # Reject Windows absolute paths even while executing tests on Linux.
    if p.is_absolute() or PureWindowsPath(value).drive or "\\" in value:
        raise ValueError("Разрешены только относительные пути внутри папки проекта")
    actual = (base / p).resolve()
    if not actual.is_relative_to(base) or not actual.is_file():
        raise ValueError(f"Файл не найден в папке проекта: {value}")
    if actual.suffix.lower() not in extensions:
        raise ValueError(f"Неподдерживаемый формат файла: {value}")
    if actual.stat().st_size > 4 * 1024**3:
        raise ValueError("Размер исходника превышает 4 ГБ")
    return actual


def _finite_number(value: object, name: str, lower: float, upper: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name}: требуется число")
    result = float(value)
    if not math.isfinite(result) or not lower <= result <= upper:
        raise ValueError(f"{name}: значение должно быть между {lower} и {upper}")
    return result


def load_project(manifest: Path) -> Project:
    manifest = manifest.resolve()
    if manifest.suffix.lower() != ".json" or manifest.stat().st_size > 200_000:
        raise ValueError("Нужен JSON-проект размером до 200 КБ")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
        raise ValueError("Неподдерживаемая версия проекта, ожидается version=1")
    title = data.get("title", "")
    if not isinstance(title, str) or not 1 <= len(title) <= 180:
        raise ValueError("Название проекта должно быть от 1 до 180 символов")
    width = data.get("width", 1080)
    height = data.get("height", 1920)
    fps = data.get("fps", 30)
    if type(width) is not int or width not in {360, 720, 1080} or height != width * 16 // 9:
        raise ValueError("Формат должен быть 360x640, 720x1280 или 1080x1920")
    if type(fps) is not int or fps not in {24, 25, 30}:
        raise ValueError("Поддерживаемая частота кадров: 24, 25 или 30")
    captions = data.get("captions", True)
    if type(captions) is not bool:
        raise ValueError("captions должен быть true или false")
    raw_scenes = data.get("scenes")
    if not isinstance(raw_scenes, list) or not 1 <= len(raw_scenes) <= 30:
        raise ValueError("Нужно от 1 до 30 утверждённых сцен")
    base = manifest.parent
    scenes = []
    for index, raw in enumerate(raw_scenes, 1):
        if not isinstance(raw, dict) or raw.get("approved") is not True:
            raise ValueError(f"Сцена {index} не утверждена автором")
        text = raw.get("text", "")
        headline = raw.get("headline", title)
        if not isinstance(text, str) or not 1 <= len(text) <= 1000:
            raise ValueError(f"Сцена {index}: текст должен содержать от 1 до 1000 символов")
        if not isinstance(headline, str) or len(headline) > 100:
            raise ValueError(f"Сцена {index}: слишком длинная плашка")
        rights = raw.get("rights", "unconfirmed")
        if rights not in {"author", "licensed", "unconfirmed"}:
            raise ValueError(f"Сцена {index}: неизвестный статус прав")
        source = _local_file(base, raw.get("source"), IMAGE_TYPES | VIDEO_TYPES)
        voice = _local_file(base, raw.get("voice"), AUDIO_TYPES)
        duration = _finite_number(raw.get("duration"), "duration", 1, 30)
        start = _finite_number(raw.get("start", 0), "start", 0, 10**6)
        if source.suffix.lower() in IMAGE_TYPES and start != 0:
            raise ValueError("Для изображения start должен быть 0")
        scenes.append(Scene(source, voice, text, headline, duration, start, rights, True))
    if sum(s.duration for s in scenes) > 180:
        raise ValueError("Первая версия поддерживает ролики до 180 секунд")
    return Project(title, width, height, fps, captions, tuple(scenes))

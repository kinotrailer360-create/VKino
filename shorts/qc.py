"""Автоматическая проверка готового ролика перед показом автору (ТЗ §9): формат, длина, чёрные и застывшие
кадры, тишина, перегруз звука. Один проход ffmpeg. Только предупреждения — решение за автором; проверка
не заменяет просмотр."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger("shorts")

BLACK_MIN = 0.5  # чёрный экран дольше — ошибка монтажа (пропал кадр, пустая концовка)
FREEZE_MIN = 6.0  # картинка не меняется дольше — застыло (сцены в роликах короче)
SILENCE_MIN = 2.0  # тишина дольше — пропала фраза или музыка
CLIP_DB = -0.3  # пик громче — звук перегружен (хрипит)
SHORT_MAX = 180.0  # вертикальный Shorts на YouTube — до 3 минут


async def _run(*args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="ignore"), err.decode(errors="ignore")


def parse(probe: dict[str, Any], log_text: str, vertical: bool | None = None) -> list[str]:
    """Предупреждения по ffprobe (JSON) и журналу фильтров ffmpeg. Пустой список — всё в порядке."""
    issues: list[str] = []
    streams = probe.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = float((probe.get("format") or {}).get("duration") or 0)
    if not video:
        return ["❌ в файле нет видео"]
    w, h = int(video.get("width") or 0), int(video.get("height") or 0)
    vertical = h > w if vertical is None else vertical
    if vertical and (w, h) != (1080, 1920):
        issues.append(f"формат {w}×{h}, а для Shorts нужен 1080×1920")
    if not audio:
        issues.append("❌ в ролике нет звука")
    if duration < 5:
        issues.append(f"❌ ролик слишком короткий: {duration:.1f} с")
    elif vertical and duration > SHORT_MAX:
        issues.append(f"длина {duration:.0f} с — больше 3 минут, YouTube не примет как Shorts")
    for start, dur in re.findall(r"black_start:([\d.]+) black_end:[\d.]+ black_duration:([\d.]+)", log_text):
        if float(dur) >= BLACK_MIN:
            issues.append(f"чёрный экран {float(dur):.1f} с на {_tc(float(start))}")
    for start, dur in re.findall(r"freeze_start: ([\d.]+)[\s\S]*?freeze_duration: ([\d.]+)", log_text):
        if float(dur) >= FREEZE_MIN:
            issues.append(f"картинка застыла на {float(dur):.1f} с с {_tc(float(start))}")
    for start, dur in re.findall(r"silence_start: ([\d.]+)[\s\S]*?silence_duration: ([\d.]+)", log_text):
        if float(dur) >= SILENCE_MIN and float(start) < duration - 1.5:  # тишина в самом конце — норма
            issues.append(f"тишина {float(dur):.1f} с на {_tc(float(start))}")
    peaks = re.findall(r"Peak level dB: (-?[\d.]+|inf|-inf)", log_text)
    if peaks:
        try:
            peak = max(float(p) for p in peaks if p not in ("inf", "-inf"))
            if peak > CLIP_DB:
                issues.append(f"звук на пределе (пик {peak:.1f} дБ) — может хрипеть")
        except ValueError:
            pass
    return issues


def _tc(sec: float) -> str:
    return f"{int(sec) // 60}:{int(sec) % 60:02d}"


async def check(video: Path, vertical: bool | None = None) -> list[str]:
    """Проверить ролик. Не получилось проверить — одно предупреждение об этом, а не «всё хорошо»."""
    try:
        code, out, err = await _run("ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
                                    "-show_format", str(video))
        if code:
            return [f"проверка не запустилась: {err.strip()[:120]}"]
        probe = json.loads(out or "{}")
        has_audio = any(s.get("codec_type") == "audio" for s in probe.get("streams") or [])
        args = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(video),
                "-vf", "blackdetect=d=0.3:pix_th=0.08,freezedetect=n=-60dB:d=3"]
        if has_audio:
            args += ["-af", "silencedetect=n=-50dB:d=1.5,astats=metadata=0"]
        code, _, err = await _run(*args, "-f", "null", "-")
        if code:
            return [f"проверка не запустилась: {err.strip()[-120:]}"]
        return parse(probe, err, vertical)
    except Exception as exc:  # noqa: BLE001 — проверка не должна ломать отправку ролика
        log.warning("shorts: qc failed for %s: %s", video, exc)
        return [f"проверка не запустилась: {type(exc).__name__}"]


def report(issues: list[str]) -> str:
    if not issues:
        return "🧪 Автопроверка: формат, звук, чёрные и застывшие кадры — в порядке. Посмотри ролик целиком перед публикацией."
    return "🧪 <b>Автопроверка нашла:</b>\n" + "\n".join(f"• {html.escape(i)}" for i in issues) + \
        "\nПосмотри эти места перед публикацией."

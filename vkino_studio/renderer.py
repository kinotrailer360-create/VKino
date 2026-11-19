"""Deterministic offline 9:16 render, with per-scene voice and author approval.

Only approved local inputs; no clip downloading, AI guesses, or publishing.
Captions are *estimated* from sentence duration until word-level alignment lands.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Callable

from PIL import Image, ImageDraw, ImageFont

from .model import Project, Scene, load_project


def _command(argv: list[str], *, cwd: Path | None = None, timeout: int = 600) -> str:
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              check=False, shell=False)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Программа не найдена: {argv[0]}. Установите FFmpeg и добавьте в PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Превышено время выполнения: {argv[0]}") from exc
    if proc.returncode:
        raise RuntimeError(f"Ошибка {argv[0]}: {proc.stderr[-1300:]}")
    return proc.stdout


def _duration(path: Path) -> float:
    raw = _command(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "json", str(path)], timeout=45)
    value = float(json.loads(raw)["format"]["duration"])
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"Невозможно определить длительность файла: {path.name}")
    return value


def _ass_time(seconds: float) -> str:
    cs = max(0, round(seconds * 100))
    return f"{cs // 360000}:{(cs // 6000) % 60:02d}:{(cs // 100) % 60:02d}.{cs % 100:02d}"


def _ass_escape(text: str) -> str:
    # ASS has its own override syntax: never trust source text as style commands.
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


def write_captions(file: Path, scene: Scene, duration: float, width: int, height: int) -> None:
    """Approximate 1-2 word timing. Not forced alignment; never claim precision."""
    words = re.findall(r"\S+", scene.text)
    chunks = [" ".join(words[i:i+2]) for i in range(0, len(words), 2)]
    weights = [max(1, len(c)) for c in chunks]
    total = sum(weights)
    cursor = 0.0
    events = []
    for c, weight in zip(chunks, weights):
        end = cursor + duration * weight / total
        events.append(f"Dialogue: 0,{_ass_time(cursor)},{_ass_time(end)},Caption,,0,0,0,,{_ass_escape(c)}")
        cursor = end
    size = max(28, round(width * 0.064))
    margin = round(height * .16)
    content = ("[Script Info]\nScriptType: v4.00+\nWrapStyle: 2\n"
               f"PlayResX: {width}\nPlayResY: {height}\n"
               "[V4+ Styles]\n"
               "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
               f"Style: Caption,Arial,{size},&H0000FFFF,&H0000FFFF,&H00000000,&H55000000,-1,0,0,0,100,100,0,0,1,4,1,2,40,40,{margin},1\n"
               "[Events]\n"
               "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
               + "\n".join(events) + "\n")
    file.write_text(content, encoding="utf-8-sig")


def _font(size: int):
    for path in ["C:/Windows/Fonts/arialbd.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        proposal = f"{current} {word}".strip()
        if current and draw.textbbox((0, 0), proposal, font=font)[2] > max_width:
            lines.append(current)
            current = word
        else:
            current = proposal
    if current:
        lines.append(current)
    return lines[:3]


def banner_png(path: Path, scene: Scene, width: int, height: int) -> None:
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    pad = round(width * .05)
    top = round(height * .065)
    font = _font(round(width * .049))
    lines = _wrap(draw, scene.headline, font, width - pad * 4)
    if lines:
        line_height = round(width * .062)
        rect_h = pad + line_height * len(lines)
        draw.rounded_rectangle((pad, top, width-pad, top+rect_h),
                               radius=round(width * .025), fill=(0,0,0,205))
        for ix, line in enumerate(lines):
            bbox = draw.textbbox((0,0), line, font=font)
            draw.text(((width - (bbox[2]-bbox[0])) / 2, top+round(pad*.40)+ix*line_height),
                      line, font=font, fill=(255,255,255,255))
    image.save(path)


def _render_scene(scene: Scene, index: int, project: Project, work: Path,
                  encoder: str, callback: Callable[[str], None]) -> Path:
    voice_length = _duration(scene.voice)
    if voice_length > scene.duration + .10:
        raise ValueError(f"Сцена {index}: озвучка ({voice_length:.2f} с) длиннее сцены ({scene.duration:.2f} с). Исправьте длительность")
    if not scene.is_image and scene.start + scene.duration > _duration(scene.source) + .05:
        raise ValueError(f"Сцена {index}: исходное видео короче выбранного участка; зацикливание запрещено")
    out = work / f"scene_{index:03}.mp4"
    overlay = work / f"banner_{index:03}.png"
    captions = work / f"caption_{index:03}.ass"
    banner_png(overlay, scene, project.width, project.height)
    if project.captions:
        write_captions(captions, scene, scene.duration, project.width, project.height)
    inputs = (["-loop", "1", "-framerate", str(project.fps), "-i", str(scene.source)] if scene.is_image
              else ["-ss", str(scene.start), "-i", str(scene.source)])
    scale = (f"scale={project.width}:{project.height}:force_original_aspect_ratio=increase,"
             f"crop={project.width}:{project.height},setsar=1,fps={project.fps}")
    if project.captions:
        # Relative ASS path avoids Windows drive-colon parsing in FFmpeg filters.
        scale += f",ass=filename={captions.name}"
    graph = f"[0:v]{scale}[base];[base][2:v]overlay=0:0:shortest=1,format=yuv420p[v]"
    argv = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs,
            "-i", str(scene.voice), "-loop", "1", "-framerate", str(project.fps), "-i", str(overlay),
            "-filter_complex", graph, "-map", "[v]", "-map", "1:a:0",
            "-af", "apad", "-t", str(scene.duration), "-r", str(project.fps),
            "-c:v", encoder, "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000",
            "-b:a", "160k", "-movflags", "+faststart"]
    if encoder == "libx264":
        argv.extend(["-preset", "veryfast", "-crf", "22"])
    else:
        argv.extend(["-preset", "p4", "-cq", "24"])
    argv.append(str(out))
    callback(f"Монтаж сцены {index}")
    _command(argv, cwd=work, timeout=max(180, int(scene.duration*35)))
    if not out.is_file() or out.stat().st_size < 500:
        raise RuntimeError(f"Сцена {index} не создана")
    return out


def render(manifest: Path, destination: Path, encoder: str = "libx264",
           callback: Callable[[str], None] | None = None) -> Path:
    """Return final MP4 path; do not publish or mark author approval automatically."""
    if encoder not in {"libx264", "h264_nvenc"}:
        raise ValueError("Поддерживаемые кодеки: libx264, h264_nvenc")
    callback = callback or (lambda _: None)
    project = load_project(manifest)
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / "vkino_studio_preview.mp4"
    with tempfile.TemporaryDirectory(prefix="vkino_studio_") as temp:
        work = Path(temp)
        parts = [_render_scene(s, i, project, work, encoder, callback)
                 for i, s in enumerate(project.scenes, 1)]
        listing = work / "concat.txt"
        listing.write_text("".join(f"file '{item.name}'\n" for item in parts), encoding="utf-8")
        staged = work / "result.mp4"
        callback("Сборка итогового видео")
        _command(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                  "-f", "concat", "-safe", "1", "-i", str(listing),
                  "-c", "copy", "-movflags", "+faststart", str(staged)], cwd=work,
                 timeout=180)
        if not staged.is_file() or staged.stat().st_size < 1000:
            raise RuntimeError("Итоговое видео не создано")
        # Preserve older preview on failure; replace only on success.
        os.replace(staged, target)
    status = {"title": project.title, "scenes": len(project.scenes),
              "duration_planned": round(sum(s.duration for s in project.scenes), 3),
              "output": target.name, "captions_timing": "estimated" if project.captions else "off",
              "rights_unconfirmed": sum(s.rights == "unconfirmed" for s in project.scenes),
              "published": False, "author_final_approval": False}
    (destination / "render_report.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    callback(f"Готов черновик: {target}")
    return target

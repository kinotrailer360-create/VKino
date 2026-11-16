"""Rebuild a user short while keeping the uploaded source untouched."""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from typing import Any

from PIL import Image

from . import ai, daily


async def run(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode:
        raise RuntimeError(err.decode("utf-8", "replace")[-500:])
    return out.decode("utf-8", "replace")


async def duration(path: Path) -> float:
    raw = await run("ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "json", str(path))
    return float(json.loads(raw)["format"]["duration"])


async def remux(source: Path, audio: Path, output: Path) -> Path:
    """Keep original pictures; replace only the sound track."""
    await run("ffmpeg", "-v", "error", "-y", "-i", str(source), "-i", str(audio),
              "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
              "-shortest", "-movflags", "+faststart", str(output))
    return output


async def replace_audio(source: Path, text: str, work: Path) -> Path:
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    if not lines:
        raise ValueError("Пришли текст для озвучки.")
    voice, _, ok, _ = await daily.synthesize(lines, work, careful=True)
    if not ok:
        raise RuntimeError("Не удалось создать озвучку.")
    out = work / "перемонтированный_черновик.mp4"
    return await remux(source, voice, out)


async def transcribe(source: Path, work: Path, key: str) -> list[dict[str, Any]]:
    audio = work / "source.mp3"
    await run("ffmpeg", "-v", "error", "-y", "-i", str(source), "-vn", "-ac", "1",
              "-ar", "22050", "-b:a", "96k", str(audio))
    return await ai.transcribe_dub(key, audio.read_bytes(), await duration(source))


async def replace_visuals(source: Path, assets: list[Path], output: Path, work: Path,
                          key: str, narration: str | None = None,
                          assets_mode: str = "user") -> Path:
    """Match a unique visual to each phrase; keep source audio unless new narration is requested."""
    if assets_mode not in {"user", "ai", "mix"}:
        raise ValueError("Неизвестный источник кадров.")
    if not assets and assets_mode in {"user", "mix"}:
        raise ValueError("Добавь хотя бы одно фото или видео для нового видеоряда.")

    voice: Path | None = None
    if narration:
        lines = [line.strip() for line in narration.splitlines() if line.strip()]
        if not lines:
            raise ValueError("Не найден текст новой озвучки.")
        voice, _, ok, _ = await daily.synthesize(lines, work, careful=True)
        if not ok:
            raise RuntimeError("Не удалось создать новую озвучку.")
        total = await duration(voice)
        weight = sum(max(1, len(line)) for line in lines)
        at = 0.0
        phrases = []
        for line in lines:
            end = at + total * max(1, len(line)) / weight
            phrases.append({"start": at, "end": end, "text": line})
            at = end
    else:
        phrases = await transcribe(source, work, key)
        if not phrases:
            raise ValueError("Не удалось распознать речь. Проверь аудио или выбери замену только озвучки.")

    stills: list[Image.Image] = []
    for path in assets:
        try:
            with Image.open(path) as im:
                stills.append(im.convert("RGB").copy())
        except Exception:
            folder = work / f"frames_{len(stills)}"
            folder.mkdir(exist_ok=True)
            await run("ffmpeg", "-v", "error", "-y", "-i", str(path), "-vf",
                      "fps=1/3,scale=640:-2", "-frames:v", "8", str(folder / "frame_%02d.jpg"))
            for frame in sorted(folder.glob("*.jpg")):
                with Image.open(frame) as im:
                    stills.append(im.convert("RGB").copy())

    texts = [str(p.get("text") or p.get("ru") or "") for p in phrases]
    if not texts:
        raise ValueError("В ролике не найдены сцены для видеоряда.")
    generated: list[Image.Image] = []
    if assets_mode in {"ai", "mix"}:
        if len(texts) > 8:
            raise ValueError("Для режима ИИ ролик должен содержать не больше 8 сцен. Разбей его или выбери свои кадры.")
        for number, phrase in enumerate(texts, 1):
            raw = await ai.generate_illustration(key, phrase)
            if not raw:
                raise RuntimeError(f"ИИ не создал кадр для сцены {number}. Попробуй ещё раз или выбери свои кадры.")
            with Image.open(io.BytesIO(raw)) as im:
                generated.append(im.convert("RGB").copy())

    if assets_mode == "ai":
        stills = generated
        choices = list(range(len(texts)))
    else:
        stills.extend(generated)
        choices = await ai.match_images(key, texts, stills)
    if not choices or len(choices) < len(texts):
        raise ValueError("ИИ не смог сопоставить все реплики с кадрами. Добавь материалы или повтори проверку.")

    used: set[int] = set()
    selected: list[tuple[Path, float]] = []
    for i, phrase in enumerate(phrases):
        c = int(choices[i])
        if c < 0 or c >= len(stills) or c in used:
            free = [n for n in range(len(stills)) if n not in used]
            if not free:
                raise ValueError(f"Для сцены {i + 1} нет отдельного кадра. Добавь материалы.")
            c = free[0]
        used.add(c)
        frame = work / f"scene_{i:03d}.jpg"
        stills[c].save(frame, quality=92)
        seconds = max(.5, float(phrase.get("end") or 0) - float(phrase.get("start") or 0))
        selected.append((frame, seconds))

    listing = work / "scenes.txt"
    listing.write_text("\n".join(f"file '{p.name}'\nduration {seconds:.3f}"
                                   for p, seconds in selected)
                       + f"\nfile '{selected[-1][0].name}'\n", encoding="utf-8")
    silent = work / "visual_draft.mp4"
    await run("ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "1", "-i", str(listing),
              "-vf", "scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=25",
              "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(silent))
    audio = voice or work / "source_audio.m4a"
    if not voice:
        await run("ffmpeg", "-v", "error", "-y", "-i", str(source), "-vn", "-c:a", "aac", str(audio))
    await run("ffmpeg", "-v", "error", "-y", "-i", str(silent), "-i", str(audio),
              "-map", "0:v:0", "-map", "1:a:0", "-shortest", "-c:v", "copy", "-c:a", "aac",
              "-movflags", "+faststart", str(output))
    return output

"""Rebuild a user's short video while keeping the uploaded source untouched."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from PIL import Image

from . import ai, daily, project


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
    total = await duration(source)
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    if not lines:
        raise ValueError("Пришли текст для озвучки.")
    voice, _, ok, _ = await daily.synthesize(lines, work, careful=True)
    if not ok:
        raise RuntimeError("Не удалось создать озвучку.")
    # Preserve the source duration: the caller can review a longer/shorter audio draft.
    out = work / "перемонтированный_черновик.mp4"
    await remux(source, voice, out)
    return out


async def transcribe(source: Path, work: Path, key: str) -> list[dict[str, Any]]:
    audio = work / "source.mp3"
    await run("ffmpeg", "-v", "error", "-y", "-i", str(source), "-vn", "-ac", "1",
              "-ar", "22050", "-b:a", "96k", str(audio))
    raw = audio.read_bytes()
    return await ai.transcribe_dub(key, raw, await duration(source))


async def replace_visuals(source: Path, assets: list[Path], output: Path, work: Path,
                          key: str, narration: str | None = None) -> Path:
    """Assign unique uploaded stills to transcribed phrases and keep original audio."""
    if not assets:
        raise ValueError("Добавь хотя бы одно фото или видео для нового видеоряда.")
    if narration:
        lines=[x.strip() for x in narration.splitlines() if x.strip()]
        if not lines: raise ValueError("Не найден текст новой озвучки.")
        total=await duration(source);weight=sum(max(1,len(x)) for x in lines);at=0.0;phrases=[]
        for line in lines:
            end=at+total*max(1,len(line))/weight
            phrases.append({"start":at,"end":end,"text":line});at=end
    else:
        phrases = await transcribe(source, work, key)
        if not phrases:
            raise ValueError("Не удалось распознать речь. Пришли текст с таймкодами либо выбери замену только озвучки.")
    stills: list[Image.Image] = []
    for path in assets:
        try:
            with Image.open(path) as im:
                stills.append(im.convert("RGB").copy())
        except Exception:
            # User videos are sampled at distinct times; no source segment is reused.
            folder = work / f"frames_{len(stills)}"
            folder.mkdir(exist_ok=True)
            raw = await run("ffmpeg", "-v", "error", "-i", str(path), "-vf",
                            "fps=1/3,scale=640:-2", "-frames:v", "8",
                            str(folder / "frame_%02d.jpg"))
            for frame in sorted(folder.glob("*.jpg")):
                with Image.open(frame) as im:
                    stills.append(im.convert("RGB").copy())
    texts = [str(p.get("text") or p.get("ru") or "") for p in phrases]
    choices = await ai.match_images(key, texts, stills)
    if not choices or len(choices) < len(texts):
        raise ValueError("ИИ не смог сопоставить все реплики с кадрами. Добавь материалы или попробуй позже.")
    used: set[int] = set()
    selected: list[tuple[Path, float]] = []
    for i, phrase in enumerate(phrases):
        c = int(choices[i])
        if c < 0 or c >= len(stills) or c in used:
            free = [n for n in range(len(stills)) if n not in used]
            if not free:
                raise ValueError(f"Для фразы {i+1} не осталось отдельного кадра. Добавь ещё материалов.")
            c = free[0]
        used.add(c)
        frame = work / f"scene_{i:03d}.jpg"
        stills[c].save(frame, quality=92)
        end = float(phrase.get("end") or 0)
        start = float(phrase.get("start") or 0)
        selected.append((frame, max(.5, end-start)))
    listing = work / "scenes.txt"
    listing.write_text("\n".join(f"file '{p.name}'\nduration {d:.3f}" for p,d in selected)
                       + f"\nfile '{selected[-1][0].name}'\n", encoding="utf-8")
    silent = work / "visual_draft.mp4"
    await run("ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "1", "-i", str(listing),
              "-vf", "scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=25",
              "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(silent))
    audio = work / "source_audio.m4a"
    if narration:
        voice,_,ok,_=await daily.synthesize([x.strip() for x in narration.splitlines() if x.strip()],work,careful=True)
        if not ok: raise RuntimeError("Не удалось создать новую озвучку.")
        audio=voice
    else:
        await run("ffmpeg", "-v", "error", "-y", "-i", str(source), "-vn", "-c:a", "aac", str(audio))
    await run("ffmpeg", "-v", "error", "-y", "-i", str(silent), "-i", str(audio),
              "-map", "0:v:0", "-map", "1:a:0", "-shortest", "-c:v", "copy", "-c:a", "aac",
              "-movflags", "+faststart", str(output))
    return output

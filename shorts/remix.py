"""Rebuild a user short while keeping the uploaded source untouched."""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from typing import Any

from PIL import Image

from . import ai, daily, visual_guard


MAX_AI_REMIX_SCENES = 8


def limit_phrases(phrases: list[dict[str, Any]], limit: int = MAX_AI_REMIX_SCENES) -> list[dict[str, Any]]:
    """Merge adjacent transcript phrases into at most limit scenes, preserving timing and text."""
    if limit < 1:
        raise ValueError("Лимит сцен должен быть больше нуля.")
    if len(phrases) <= limit:
        return [dict(phrase) for phrase in phrases]

    scene_count = min(limit, len(phrases))
    grouped: list[dict[str, Any]] = []
    for scene_index in range(scene_count):
        start_index = scene_index * len(phrases) // scene_count
        end_index = (scene_index + 1) * len(phrases) // scene_count
        chunk = phrases[start_index:end_index]
        text = " ".join(str(p.get("text") or p.get("ru") or "").strip() for p in chunk).strip()
        grouped.append({
            "start": float(chunk[0].get("start") or 0),
            "end": float(chunk[-1].get("end") or chunk[-1].get("start") or 0),
            "text": text,
        })
    return grouped


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
    """Build a semantic, non-repeating video edit from supplied clips and photos."""
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

    if assets_mode in {"ai", "mix"}:
        phrases = limit_phrases(phrases)
    texts = [str(p.get("text") or p.get("ru") or "").strip() for p in phrases]
    if not texts:
        raise ValueError("В ролике не найдены сцены для видеоряда.")

    # Each candidate keeps the sampled frame and, for video, its source timestamp.
    # The timestamp lets the renderer cut an actual moving shot instead of holding a still.
    candidates: list[dict[str, Any]] = []
    video_durations: dict[Path, float] = {}
    for asset_number, path in enumerate(assets):
        try:
            with Image.open(path) as im:
                candidates.append({"kind": "image", "image": im.convert("RGB").copy()})
            continue
        except Exception:
            pass

        try:
            video_length = await duration(path)
        except Exception:
            continue
        if video_length <= 0.5:
            continue
        video_durations[path] = video_length
        folder = work / f"video_candidates_{asset_number}"
        folder.mkdir(exist_ok=True)
        # Sample across the whole clip, not only its first few seconds.
        fps = min(1.0, 8.0 / video_length)
        await run("ffmpeg", "-v", "error", "-y", "-i", str(path), "-vf",
                  f"fps={fps:.8f},scale=640:-2", "-frames:v", "8",
                  str(folder / "frame_%02d.jpg"))
        frame_paths = sorted(folder.glob("frame_*.jpg"))
        for frame_number, frame_path in enumerate(frame_paths):
            try:
                with Image.open(frame_path) as im:
                    picture = im.convert("RGB").copy()
            except Exception:
                continue
            timestamp = min(video_length - 0.1, (frame_number + 0.5) * video_length / max(1, len(frame_paths)))
            candidates.append({
                "kind": "video", "image": picture, "path": path,
                "timestamp": max(0.0, timestamp), "duration": video_length,
            })

    # If the user supplied videos, use their moving scenes and any supplied photos.
    # Only generate stills when there is no user video to cut from.
    has_video = any(item["kind"] == "video" for item in candidates)
    if assets_mode in {"ai", "mix"} and not has_video:
        for number, phrase in enumerate(texts, 1):
            raw = await ai.generate_illustration(key, phrase)
            if not raw:
                raise RuntimeError(
                    f"ИИ не создал кадр для сцены {number}. Попробуй ещё раз или добавь свои материалы."
                )
            with Image.open(io.BytesIO(raw)) as im:
                candidates.append({"kind": "image", "image": im.convert("RGB").copy()})

    if not candidates:
        raise ValueError("Не удалось прочитать загруженные видео или фото.")

    # Resolve each scene independently against only the candidates that remain
    # available. This preserves semantic matching while forbidding repeated frames
    # and overlapping portions of the same source clip.
    used_candidates: set[int] = set()
    used_ranges: dict[Path, list[tuple[float, float]]] = {}
    selected: list[dict[str, Any]] = []
    for scene_number, (phrase, text) in enumerate(zip(phrases, texts), 1):
        scene_seconds = max(
            0.5,
            float(phrase.get("end") or 0) - float(phrase.get("start") or 0),
        )
        available: list[tuple[int, dict[str, Any], float]] = []
        for candidate_index, candidate in enumerate(candidates):
            if candidate_index in used_candidates:
                continue
            start_at = 0.0
            end_at = scene_seconds
            if candidate["kind"] == "video":
                length = float(candidate["duration"])
                if length + 0.05 < scene_seconds:
                    continue
                start_at = max(0.0, min(
                    float(candidate["timestamp"]) - scene_seconds / 2,
                    length - scene_seconds,
                ))
                end_at = start_at + scene_seconds
                intervals = used_ranges.get(candidate["path"], [])
                if any(start_at < used_end + 1.0 and end_at > used_start - 1.0
                       for used_start, used_end in intervals):
                    continue
            available.append((candidate_index, candidate, start_at))

        if not available:
            raise ValueError(
                f"Для сцены {scene_number} не нашлось подходящего свободного фрагмента. "
                "Добавь еще видео или фото."
            )
        matches = await ai.match_images(
            key, [text], [candidate["image"] for _, candidate, _ in available]
        )
        if not matches or int(matches[0]) < 0 or int(matches[0]) >= len(available):
            raise ValueError(
                f"ИИ не нашел подходящий кадр по смыслу для сцены {scene_number}. "
                "Добавь другое видео или фото."
            )
        candidate_index, candidate, start_at = available[int(matches[0])]
        used_candidates.add(candidate_index)
        if candidate["kind"] == "video":
            end_at = start_at + scene_seconds
            used_ranges.setdefault(candidate["path"], []).append((start_at, end_at))
        else:
            end_at = float(phrase.get("end") or scene_seconds)

        review_image = work / f"scene_{scene_number - 1:03d}_review.jpg"
        candidate["image"].save(review_image, quality=92)
        selected.append({
            "kind": candidate["kind"], "path": candidate.get("path"),
            "start": start_at, "end": end_at, "seconds": scene_seconds,
            "image": review_image, "text": text,
            "phrase_start": float(phrase.get("start") or 0),
            "phrase_end": float(phrase.get("end") or scene_seconds),
        })

    review_scenes = [
        {"image": scene["image"].name, "text": scene["text"],
         "start": scene["phrase_start"], "end": scene["phrase_end"]}
        for scene in selected
    ]
    await visual_guard.audit(work, review_scenes, key, name="remix_visual_review")

    # Normalize each selected shot separately, then concatenate. Video shots remain
    # moving clips; photos are held for the scene duration without reusing an asset.
    selected_paths: list[Path] = []
    video_filter = (
        "scale=720:1280:force_original_aspect_ratio=decrease,"
        "pad=720:1280:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=25"
    )
    for index, scene in enumerate(selected):
        clip = work / f"scene_{index:03d}.mp4"
        if scene["kind"] == "video":
            await run(
                "ffmpeg", "-v", "error", "-y", "-ss", f"{scene['start']:.3f}",
                "-i", str(scene["path"]), "-t", f"{scene['seconds']:.3f}",
                "-vf", video_filter, "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-r", "25", str(clip),
            )
        else:
            await run(
                "ffmpeg", "-v", "error", "-y", "-loop", "1", "-framerate", "25",
                "-i", str(scene["image"]), "-t", f"{scene['seconds']:.3f}",
                "-vf", video_filter, "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-r", "25", str(clip),
            )
        selected_paths.append(clip)

    listing = work / "scenes.txt"
    listing.write_text(
        "\n".join(f"file '{clip.name}'" for clip in selected_paths) + "\n",
        encoding="utf-8",
    )
    silent = work / "visual_draft.mp4"
    await run(
        "ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "1", "-i", str(listing),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(silent),
    )
    audio = voice or work / "source_audio.m4a"
    if not voice:
        await run("ffmpeg", "-v", "error", "-y", "-i", str(source), "-vn", "-c:a", "aac", str(audio))
    await run(
        "ffmpeg", "-v", "error", "-y", "-i", str(silent), "-i", str(audio),
        "-map", "0:v:0", "-map", "1:a:0", "-shortest", "-c:v", "copy", "-c:a", "aac",
        "-movflags", "+faststart", str(output),
    )
    return output

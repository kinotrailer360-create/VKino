"""Rebuild a user short while keeping the uploaded source untouched."""
from __future__ import annotations

import asyncio
import io
import json
import logging
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from . import ai, daily, visual_guard


MAX_AI_REMIX_SCENES = 8
log = logging.getLogger(__name__)


def limit_phrases(
    phrases: list[dict[str, Any]],
    limit: int = MAX_AI_REMIX_SCENES,
    total_duration: float | None = None,
) -> list[dict[str, Any]]:
    """Group narration into scenes and cover the complete audio timeline."""
    if limit < 1:
        raise ValueError("Лимит сцен должен быть больше нуля.")
    if not phrases:
        return []

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

    if total_duration is None:
        return grouped

    full_duration = max(0.0, float(total_duration))
    boundaries = [0.0]
    for left, right in zip(grouped, grouped[1:]):
        left_end = float(left.get("end") or left.get("start") or 0)
        right_start = float(right.get("start") or left_end)
        midpoint = (left_end + right_start) / 2
        boundaries.append(max(boundaries[-1], min(full_duration, midpoint)))
    boundaries.append(full_duration)

    for index, scene in enumerate(grouped):
        scene["start"] = boundaries[index]
        scene["end"] = boundaries[index + 1]
    return grouped


def split_caption_chunks(text: str, max_chars: int = 52) -> list[str]:
    """Split a scene narration into short readable subtitle cards."""
    words = str(text or "").split()
    chunks: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join(current + [word])
        if current and len(candidate) > max_chars:
            chunks.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        chunks.append(" ".join(current))
    return chunks or [""]


def render_caption_card(text: str, output: Path) -> None:
    """Draw a clean, high-contrast vertical-video subtitle card with bundled fonts."""
    canvas = Image.new("RGBA", (720, 1280), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    font_path = Path(__file__).parent / "assets" / "Montserrat.ttf"
    size = 42
    lines: list[str] = []
    while size >= 28:
        font = ImageFont.truetype(str(font_path), size=size)
        lines = []
        for word in str(text or "").split():
            candidate = f"{lines[-1]} {word}".strip() if lines else word
            if lines and draw.textbbox((0, 0), candidate, font=font)[2] > 600:
                lines.append(word)
            elif lines:
                lines[-1] = candidate
            else:
                lines.append(candidate)
        if len(lines) <= 4:
            break
        size -= 2

    line_height = int(size * 1.28)
    panel_height = max(126, 52 + line_height * len(lines))
    top = 1110 - panel_height
    draw.rounded_rectangle(
        (30, top, 690, 1110), radius=28,
        fill=(7, 12, 24, 218), outline=(70, 211, 255, 205), width=3,
    )
    draw.rounded_rectangle((48, top + 24, 56, 1110 - 24), radius=4, fill=(70, 211, 255, 255))
    font = ImageFont.truetype(str(font_path), size=size)
    y = top + (panel_height - line_height * len(lines)) // 2
    for line_index, line in enumerate(lines):
        color = (255, 211, 92, 255) if line_index == 0 else (255, 255, 255, 255)
        box = draw.textbbox((0, 0), line, font=font, stroke_width=1)
        x = (720 - (box[2] - box[0])) // 2
        draw.text((x, y), line, font=font, fill=color, stroke_width=2, stroke_fill=(0, 0, 0, 210))
        y += line_height
    canvas.save(output)


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
    # For AI-assisted modes, the source video itself is the first footage source.
    # Additional uploads replace this fallback and are preferred for matching.
    if not assets and assets_mode in {"ai", "mix"}:
        assets = [source]
    if not assets and assets_mode == "user":
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
        total = await duration(work / "source.mp3")

    if assets_mode in {"ai", "mix"}:
        phrases = limit_phrases(phrases, total_duration=total)
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

    if not candidates:
        raise ValueError("Не удалось прочитать загруженные видео или фото.")

    # Match the complete edit in one request to conserve provider quota. If the
    # model cannot answer, keep the draft moving with unique footage and mark it
    # for the author's review instead of failing the whole render.
    batch_matches = await ai.match_images(key, texts, [item["image"] for item in candidates])
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
                if any(start_at < used_end and end_at > used_start
                       for used_start, used_end in intervals):
                    continue
            available.append((candidate_index, candidate, start_at))

        async def generate_fallback() -> dict[str, Any] | None:
            if assets_mode not in {"ai", "mix"}:
                return None
            try:
                raw = await ai.generate_illustration(key, text)
                if not raw:
                    return None
                with Image.open(io.BytesIO(raw)) as im:
                    return {"kind": "image", "image": im.convert("RGB").copy()}
            except Exception as exc:  # generator/provider failures must not discard supplied footage
                log.warning("shorts remix: illustration fallback failed for scene %s: %s", scene_number, exc)
                return None

        candidate_index = -1
        candidate: dict[str, Any] | None = None
        start_at = 0.0
        matched_by_ai = False

        preferred_index = -1
        if batch_matches and scene_number <= len(batch_matches):
            try:
                preferred_index = int(batch_matches[scene_number - 1])
            except (TypeError, ValueError):
                preferred_index = -1

        available_by_id = {entry[0]: entry for entry in available}
        if preferred_index in available_by_id:
            candidate_index, candidate, start_at = available_by_id[preferred_index]
            matched_by_ai = True
        elif preferred_index >= 0 and available:
            # The batch choice may collide with a previous clip range. Recheck only
            # this affected scene against the remaining candidates.
            retry = await ai.match_images(
                key, [text], [item["image"] for _, item, _ in available]
            )
            retry_index = -1
            if retry:
                try:
                    retry_index = int(retry[0])
                except (TypeError, ValueError):
                    retry_index = -1
            if 0 <= retry_index < len(available):
                candidate_index, candidate, start_at = available[retry_index]
                matched_by_ai = True

        if candidate is None and preferred_index < 0 and batch_matches is not None:
            # The model answered but found no suitable supplied scene. Generate
            # only as the last resort; if generation is down, use a unique clip
            # as an explicitly unverified draft scene.
            generated = await generate_fallback()
            if generated is not None:
                candidates.append(generated)
                candidate_index = len(candidates) - 1
                candidate = generated

        if candidate is None and available:
            candidate_index, candidate, start_at = available[0]

        if candidate is None:
            generated = await generate_fallback()
            if generated is not None:
                candidates.append(generated)
                candidate_index = len(candidates) - 1
                candidate = generated
            else:
                raise ValueError(
                    f"Для сцены {scene_number} не осталось свободного фрагмента, "
                    "а запасной кадр ИИ сейчас недоступен."
                )

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
            "matched_by_ai": matched_by_ai,
        })

    review_scenes = [
        {
            "image": scene["image"].name if scene["kind"] != "video" else None,
            "clip": str(scene["path"]) if scene["kind"] == "video" else None,
            "offset": scene["start"],
            "text": scene["text"],
            "start": scene["phrase_start"],
            "end": scene["phrase_end"],
            "match_source": "semantic" if scene["matched_by_ai"] else "fallback",
        }
        for scene in selected
    ]
    # Keep the audit report, but still return a reviewable draft when a provider
    # cannot verify one or more scenes. The bot will name those scenes to the user.
    await visual_guard.audit(
        work, review_scenes, key, name="remix_visual_review", strict=False
    )

    # Render each scene with timed, high-contrast subtitles. Photos receive a
    # subtle Ken Burns push-in so the finished short keeps moving between clips.
    selected_paths: list[Path] = []
    video_filter = (
        "scale=720:1280:force_original_aspect_ratio=decrease,"
        "pad=720:1280:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=25,"
        "eq=contrast=1.035:saturation=1.06"
    )
    photo_filter = (
        "scale=820:1458:force_original_aspect_ratio=increase,"
        "crop=720:1280,zoompan=z='min(zoom+0.00018,1.06)':"
        "d=1:s=720x1280:fps=25,setsar=1,eq=contrast=1.035:saturation=1.06"
    )
    for index, scene in enumerate(selected):
        clip = work / f"scene_{index:03d}.mp4"
        seconds = float(scene["seconds"])
        chunks = split_caption_chunks(scene["text"])
        max_cards = max(1, min(5, int(seconds / 1.4)))
        if len(chunks) > max_cards:
            words = " ".join(chunks).split()
            chunks = []
            for start in range(0, len(words), (len(words) + max_cards - 1) // max_cards):
                chunks.append(" ".join(words[start:start + (len(words) + max_cards - 1) // max_cards]))
        caption_paths: list[Path] = []
        weights = [max(1, len(chunk)) for chunk in chunks]
        total_weight = sum(weights)
        card_ranges: list[tuple[float, float]] = []
        at = 0.0
        for card_index, (chunk, weight) in enumerate(zip(chunks, weights)):
            end_at = seconds if card_index == len(chunks) - 1 else at + seconds * weight / total_weight
            caption = work / f"caption_{index:03d}_{card_index:02d}.png"
            render_caption_card(chunk, caption)
            caption_paths.append(caption)
            card_ranges.append((at, end_at))
            at = end_at

        command = ["ffmpeg", "-v", "error", "-y"]
        if scene["kind"] == "video":
            command += [
                "-ss", f"{scene['start']:.3f}", "-i", str(scene["path"]),
            ]
            base_filter = video_filter
        else:
            command += [
                "-loop", "1", "-framerate", "25", "-i", str(scene["image"]),
            ]
            base_filter = photo_filter
        for caption in caption_paths:
            command += ["-loop", "1", "-framerate", "25", "-t", f"{seconds:.3f}", "-i", str(caption)]

        filters = [f"[0:v]{base_filter}[base]"]
        current = "base"
        for card_index, (caption, (start_at, end_at)) in enumerate(zip(caption_paths, card_ranges), 1):
            target = f"cap{card_index}"
            filters.append(
                f"[{current}][{card_index}:v]overlay=0:0:"
                f"enable='between(t,{start_at:.3f},{end_at:.3f})'[{target}]"
            )
            current = target
        filters.append(f"[{current}]format=yuv420p[outv]")
        command += [
            "-filter_complex", ";".join(filters), "-map", "[outv]",
            "-t", f"{seconds:.3f}", "-an", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "21", "-pix_fmt", "yuv420p", "-r", "25", str(clip),
        ]
        await run(*command)
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

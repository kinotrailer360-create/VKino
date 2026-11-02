"""Еженедельный выпуск «Новости кино» для YouTube (16:9, 6–9 минут).

Сценарий пишет ИИ (shorts.ai.write_weekly_episode) по новостям недели; здесь — картинки к каждой новости
(фото из постов-источников, кадры и постеры TMDB, портреты людей), озвучка по новости, монтаж и SEO.

Монтаж — сразу через ffmpeg (а не покадрово на Python, как у шортсов): длинный ролик иначе собирался бы часами.
Каждая картинка — свой кусок с медленным движением камеры и плашкой заголовка поверх, куски склеиваются без
перекодирования, в конце подмешивается музыка. Для Telegram — лёгкое превью до 50 МБ; полный файл — на YouTube.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

from . import daily, tmdb
from . import music as musiclib
from .cards import ASSETS, _cover, _font

log = logging.getLogger("shorts")

W, H = 1920, 1080
FPS = 25
PAN = 1.1  # картинка на 10% больше кадра — камера медленно едет
ACCENT = (255, 184, 28)
INK = (14, 14, 18)
CARD_SEC = 2.4  # заставка «Новость 3» между новостями
PREVIEW_MB = 46  # превью для Telegram (бот может отправить до 50 МБ)
INTRO_CLIP = ASSETS / "weekly_intro.mp4"  # анимированные заставка и концовка (исходник — tools/intro, Remotion)
OUTRO_CLIP = ASSETS / "weekly_outro.mp4"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; KinoMir360-bot/1.0; +https://t.me/KinoMir360)"}


@dataclass
class Episode:
    video: Path
    preview: Path | None
    cover: Path
    seo: dict[str, str] = field(default_factory=dict)
    duration: float = 0.0


async def _run(*args: str) -> None:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode:
        raise RuntimeError(f"{args[0]}: {err.decode(errors='ignore')[-500:]}")


async def _duration(path: Path) -> float:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    try:
        return float(out.decode().strip())
    except ValueError:
        return 0.0


# ---------------------------------------------------------------- картинки

async def _download(session: aiohttp.ClientSession, url: str) -> Image.Image | None:
    try:
        async with session.get(url) as resp:
            resp.raise_for_status()
            img = Image.open(io.BytesIO(await resp.read()))
            img.load()
        img = img.convert("RGB")
        return img if min(img.size) >= 200 else None
    except Exception as exc:  # noqa: BLE001
        log.info("shorts: weekly image %s skipped: %s", url[:80], exc)
        return None


async def story_images(story: dict[str, Any], posts: list[dict[str, Any]], tmdb_key: str,
                       limit: int = 6) -> list[Image.Image]:
    """Картинки к новости: фото людей, кадры и постер фильма с TMDB, фото из постов-источников."""
    urls: list[str] = []
    person = story.get("person_en") or story.get("person")
    if person:
        try:
            info = await tmdb.person_by_name(tmdb_key, person)
            urls += tmdb.profile_urls(info)[:2] if info else []
        except Exception as exc:  # noqa: BLE001
            log.info("shorts: weekly person %s failed: %s", person, exc)
    name = story.get("film_en") or story.get("film")
    if name:
        try:
            found = await tmdb.search_movie(tmdb_key, name) or []
            kind = "movie"
            if not found:
                found, kind = await tmdb.search_tv(tmdb_key, name) or [], "tv"
            if found:
                urls += await tmdb.backdrops(tmdb_key, kind, int(found[0]["id"]), 3)
                if found[0].get("poster"):
                    urls.append(found[0]["poster"])
        except Exception as exc:  # noqa: BLE001
            log.info("shorts: weekly film %s failed: %s", name, exc)
    for i in story.get("media") or []:
        if 1 <= i <= len(posts):
            p = posts[i - 1]
            urls += [m["image"] for m in p.get("media") or [] if m.get("image")][:3] or (
                [p["image"]] if p.get("image") else [])
    out: list[Image.Image] = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), headers=HEADERS) as session:
        for url in dict.fromkeys(urls):
            if len(out) >= limit:
                break
            img = await _download(session, url)
            if img:
                out.append(ImageOps.grayscale(img).convert("RGB") if story.get("mourning") else img)
    return out


def slide_image(img: Image.Image) -> Image.Image:
    """Картинка в кадр 16:9 с запасом на движение камеры. Вертикальные (портреты, постеры) — целиком по
    центру на размытом фоне из неё же, иначе лицо обрезалось бы."""
    sw, sh = round(W * PAN), round(H * PAN)
    img = img.convert("RGB")
    if img.width / img.height >= 1.3:
        return _cover(img, sw, sh, 0.35)
    bg = _cover(img, sw, sh, 0.5).filter(ImageFilter.GaussianBlur(40))
    bg = ImageEnhance.Brightness(bg).enhance(0.55)
    fit = img.copy()
    fit.thumbnail((sw, sh - 40), Image.LANCZOS)
    if fit.height < sh - 40:  # маленькую картинку дотягиваем по высоте
        k = (sh - 40) / fit.height
        fit = img.resize((round(img.width * k), sh - 40), Image.LANCZOS)
    bg.paste(fit, ((sw - fit.width) // 2, 20))
    return bg


def _logo() -> Image.Image | None:
    try:
        logo = Image.open(ASSETS / "logo.png").convert("RGBA")
        logo.thumbnail((150, 150), Image.LANCZOS)
        return logo
    except Exception:  # noqa: BLE001
        return None


def overlay(headline: str, num: int, total: int, mourning: bool) -> Image.Image:
    """Прозрачный слой поверх картинок новости: плашка «НОВОСТЬ 3/7», заголовок внизу, знак канала."""
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    grad = Image.linear_gradient("L").resize((W, 360)).point(lambda v: round(v * 0.85))
    shade = Image.new("RGBA", (W, 360), (0, 0, 0, 255))
    shade.putalpha(grad)
    layer.alpha_composite(shade, (0, H - 360))
    draw = ImageDraw.Draw(layer)
    pill_font = _font("Montserrat.ttf", 30, "Bold")
    label = f"НОВОСТЬ {num}/{total}"
    lw = draw.textlength(label, font=pill_font)
    color = (200, 200, 200) if mourning else ACCENT
    draw.rounded_rectangle((70, H - 250, 70 + lw + 48, H - 196), radius=27, fill=color)
    draw.text((94, H - 223), label, font=pill_font, fill=INK, anchor="lm")
    size = 76
    font = _font("Oswald.ttf", size, "Bold")
    while draw.textlength(headline, font=font) > W - 160 and size > 44:
        size -= 4
        font = _font("Oswald.ttf", size, "Bold")
    draw.text((70, H - 170), headline, font=font, fill="white", stroke_width=2, stroke_fill=(0, 0, 0))
    logo = _logo()
    if logo:
        logo.putalpha(logo.getchannel("A").point(lambda a: int(a * 0.85)))
        layer.alpha_composite(logo, (W - logo.width - 40, 36))
    return layer


def title_card(bg: Image.Image | None, top: str, big: str, small: str = "", mourning: bool = False) -> Image.Image:
    """Заставка: размытый фон, сверху мелко («НОВОСТЬ 3»), крупно заголовок, снизу подпись."""
    base = (_cover(bg, W, H, 0.4).filter(ImageFilter.GaussianBlur(28)) if bg else Image.new("RGB", (W, H), INK))
    base = ImageEnhance.Brightness(base).enhance(0.38).convert("RGBA")
    draw = ImageDraw.Draw(base)
    color = (210, 210, 210) if mourning else ACCENT
    draw.text((W // 2, 300), top, font=_font("Montserrat.ttf", 48, "Bold"), fill=color, anchor="mm")
    words, rows, size = big.split(), [], 120
    while size >= 60:
        font = _font("Oswald.ttf", size, "Bold")
        rows = [""]
        for w in words:
            if draw.textlength(f"{rows[-1]} {w}".strip(), font=font) <= W - 240:
                rows[-1] = f"{rows[-1]} {w}".strip()
            else:
                rows.append(w)
        if len(rows) <= 3:
            break
        size -= 8
    y = H // 2 - (len(rows) - 1) * size * 0.6 + 30
    for row in rows:
        draw.text((W // 2, y), row, font=font, fill="white", anchor="mm", stroke_width=3, stroke_fill=(0, 0, 0))
        y += size * 1.15
    draw.rounded_rectangle((W // 2 - 90, y - 10, W // 2 + 90, y), radius=5, fill=color)
    if small:
        draw.text((W // 2, y + 60), small, font=_font("Montserrat.ttf", 40, "SemiBold"), fill=(230, 230, 230),
                  anchor="mm")
    logo = _logo()
    if logo:
        base.alpha_composite(logo, (W - logo.width - 40, 36))
    return base.convert("RGB")


def thumbnail(img: Image.Image | None, text: str, date_label: str) -> Image.Image:
    """Обложка YouTube 1280×720: картинка главной новости, крупный текст, плашка «НОВОСТИ КИНО»."""
    tw, th = 1280, 720
    base = _cover(img, tw, th, 0.35) if img else Image.new("RGB", (tw, th), INK)
    base = ImageEnhance.Contrast(base).enhance(1.15).convert("RGBA")
    grad = Image.linear_gradient("L").rotate(90, expand=True).resize((tw, th)).point(lambda v: round(v * 0.9))
    shade = Image.new("RGBA", (tw, th), (0, 0, 0, 255))
    shade.putalpha(grad.transpose(Image.FLIP_LEFT_RIGHT))
    base.alpha_composite(shade)
    draw = ImageDraw.Draw(base)
    badge = _font("Montserrat.ttf", 34, "Bold")
    draw.rounded_rectangle((40, 40, 40 + draw.textlength("НОВОСТИ КИНО", font=badge) + 48, 104), radius=32,
                           fill=ACCENT)
    draw.text((64, 72), "НОВОСТИ КИНО", font=badge, fill=INK, anchor="lm")
    words, size = text.upper().split(), 110
    while size >= 56:
        font = _font("Oswald.ttf", size, "Bold")
        rows = [""]
        for w in words:
            if draw.textlength(f"{rows[-1]} {w}".strip(), font=font) <= tw * 0.62:
                rows[-1] = f"{rows[-1]} {w}".strip()
            else:
                rows.append(w)
        if len(rows) <= 3:
            break
        size -= 8
    y = th - 70 - len(rows) * size * 1.08
    for k, row in enumerate(rows):
        draw.text((44, y), row, font=font, fill=ACCENT if k == 0 else "white", stroke_width=4,
                  stroke_fill=(0, 0, 0))
        y += size * 1.08
    draw.text((tw - 40, th - 40), date_label, font=_font("Montserrat.ttf", 30, "SemiBold"), fill="white",
              anchor="rs")
    return base.convert("RGB")


# ---------------------------------------------------------------- монтаж

async def _slide_clip(img_path: Path, over: Path | None, dur: float, out: Path, reverse: bool) -> None:
    """Кусок видео из картинки: камера медленно едет вбок, поверх — плашка заголовка."""
    x = f"(iw-{W})*(1-min(t/{dur:.3f}\\,1))" if reverse else f"(iw-{W})*min(t/{dur:.3f}\\,1)"
    chain = f"[0:v]crop={W}:{H}:x='{x}':y='(ih-{H})/2',fps={FPS}"
    args = ["ffmpeg", "-v", "error", "-y", "-loop", "1", "-framerate", str(FPS), "-t", f"{dur:.3f}",
            "-i", str(img_path)]
    if over:
        args += ["-loop", "1", "-framerate", str(FPS), "-t", f"{dur:.3f}", "-i", str(over)]
        chain += "[b];[b][1:v]overlay=0:0"
    await _run(*args, "-filter_complex", chain + ",format=yuv420p", "-t", f"{dur:.3f}", "-r", str(FPS),
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-an", str(out))


async def _concat(parts: list[Path], out: Path, work: Path) -> None:
    lst = work / f"{out.stem}.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in parts), encoding="utf-8")
    await _run("ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out))


async def _segment(work: Path, name: str, slides: list[tuple[Path, float]], over: Path | None,
                   audio: Path | None, total: float) -> Path:
    """Сегмент выпуска: куски-картинки подряд + звук (голос или «вжух»), AAC 48 кГц стерео — одинаковый у всех,
    чтобы потом склеить без перекодирования."""
    clips = []
    for k, (img, dur) in enumerate(slides):
        clip = work / f"{name}_s{k}.mp4"
        await _slide_clip(img, over, dur, clip, reverse=k % 2 == 1)
        clips.append(clip)
    video = work / f"{name}_v.mp4"
    await _concat(clips, video, work)
    out = work / f"{name}.mp4"
    audio_in = ["-i", str(audio)] if audio else ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    await _run("ffmpeg", "-v", "error", "-y", "-i", str(video), *audio_in, "-map", "0:v", "-map", "1:a",
               "-af", "apad", "-t", f"{total:.3f}", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
               "-ac", "2", str(out))
    for clip in clips:
        clip.unlink(missing_ok=True)
    video.unlink(missing_ok=True)
    return out


async def _brand_clip(src: Path, out: Path) -> Path:
    """Готовый ролик (заставка или концовка) в те же параметры, что у сегментов, — чтобы склеить без
    перекодирования всего выпуска."""
    await _run("ffmpeg", "-v", "error", "-y", "-i", str(src), "-vf", f"scale={W}:{H},fps={FPS},format=yuv420p",
               "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-c:a", "aac", "-b:a", "160k",
               "-ar", "48000", "-ac", "2", str(out))
    return out


def _even_slides(paths: list[Path], starts: list[float], total: float) -> list[tuple[Path, float]]:
    """Картинки по фразам: смена картинки на начале фразы, примерно поровну (каждая — не меньше 3 с)."""
    if not paths:
        return []
    n = max(1, min(len(paths), round(total / 3.5), len(starts) or 1))
    cuts = sorted({starts[round(i * len(starts) / n)] for i in range(n)} | {0.0}) if starts else [0.0]
    cuts = [c for k, c in enumerate(cuts) if k == 0 or c - cuts[k - 1] >= 3.0]
    ends = cuts[1:] + [total]
    return [(paths[k % len(paths)], max(e - s, 0.5)) for k, (s, e) in enumerate(zip(cuts, ends))]


def _clock(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


async def _voice(lines: list[str], work: Path) -> tuple[Path, list[float], float, bool]:
    work.mkdir(parents=True, exist_ok=True)
    voice, starts, voiced, _ = await daily.synthesize(lines, work)
    return voice, starts, await _duration(voice), voiced


async def render(work: Path, script: dict[str, Any], posts: list[dict[str, Any]], tmdb_key: str,
                 date_label: str) -> Episode:
    """Собрать выпуск: заставка, вступление, новости с заставками «Новость N», финал; музыка; превью; SEO."""
    stories = script["stories"]
    total_n = len(stories)
    segments: list[Path] = []
    chapters: list[tuple[float, str]] = []
    clock = 0.0
    notes: list[str] = []
    first_img: Image.Image | None = None

    await daily._progress(3, "Картинки к новостям")
    pictures: list[list[Image.Image]] = []
    for k, story in enumerate(stories):
        imgs = await story_images(story, posts, tmdb_key)
        pictures.append(imgs)
        first_img = first_img or (imgs[0] if imgs else None)
        await daily._progress(3 + round(12 * (k + 1) / total_n), "Картинки к новостям")

    def save_slides(imgs: list[Image.Image], prefix: str) -> list[Path]:
        out = []
        for i, img in enumerate(imgs):
            path = work / f"{prefix}_{i}.jpg"
            slide_image(img).save(path, quality=88)
            out.append(path)
        return out

    # вступление: заставка выпуска под голос приветствия
    await daily._progress(16, "Озвучка вступления")
    voice, starts, dur, voiced = await _voice(script["intro"], work / "v_intro")
    if not voiced:
        notes.append(daily.no_voice_note(voiced))
    intro_card = work / "intro_card.jpg"
    slide_image(title_card(first_img, "KINOMIR360", "НОВОСТИ КИНО ЗА НЕДЕЛЮ", date_label)).save(intro_card,
                                                                                            quality=90)
    chapters.append((0.0, "Вступление"))
    if INTRO_CLIP.exists():  # анимированная заставка с логотипом — самое начало выпуска
        try:
            segments.append(await _brand_clip(INTRO_CLIP, work / "brand_intro.mp4"))
            clock += await _duration(segments[-1])
        except Exception as exc:  # noqa: BLE001
            log.warning("shorts: weekly intro clip skipped: %s", exc)
    segments.append(await _segment(work, "intro", [(intro_card, dur + 0.4)], None, voice, dur + 0.4))
    clock += dur + 0.4

    whoosh = ASSETS / "whoosh.wav"
    for k, story in enumerate(stories):
        n = k + 1
        await daily._progress(18 + round(64 * k / total_n), f"Новость {n} из {total_n}: озвучка и монтаж")
        imgs = pictures[k]
        bg = imgs[0] if imgs else first_img
        card = work / f"card{n}.jpg"
        slide_image(title_card(bg, f"НОВОСТЬ {n}", story["headline"], mourning=story["mourning"])).save(
            card, quality=90)
        segments.append(await _segment(work, f"card{n}", [(card, CARD_SEC)], None,
                                       whoosh if whoosh.exists() else None, CARD_SEC))
        chapters.append((clock, story.get("chapter") or story["headline"].capitalize()))
        clock += CARD_SEC
        voice, starts, dur, voiced = await _voice(story["lines"], work / f"v{n}")
        if not voiced and not notes:
            notes.append(daily.no_voice_note(voiced))
        paths = save_slides(imgs, f"st{n}") or [card]
        over = work / f"over{n}.png"
        overlay(story["headline"], n, total_n, story["mourning"]).save(over)
        total = dur + 0.5
        segments.append(await _segment(work, f"story{n}", _even_slides(paths, starts, total), over, voice, total))
        clock += total

    await daily._progress(84, "Финал выпуска")
    voice, starts, dur, voiced = await _voice(script["outro"], work / "v_outro")
    outro_card = work / "outro_card.jpg"
    slide_image(title_card(first_img, "ПОДПИШИСЬ — НОВЫЙ ВЫПУСК КАЖДОЕ ВОСКРЕСЕНЬЕ", "СПАСИБО, ЧТО СМОТРИТЕ!",
                           "YouTube @kinomir360 · Telegram @KinoMir360")).save(outro_card, quality=90)
    tail = 0.6 if OUTRO_CLIP.exists() else 2.0  # с анимированной концовкой карточку долго не держим
    segments.append(await _segment(work, "outro", [(outro_card, dur + tail)], None, voice, dur + tail))
    chapters.append((clock, "Итоги и вопрос зрителям"))
    if OUTRO_CLIP.exists():  # «Спасибо, что смотрите!» и кнопка «Подписаться»
        try:
            segments.append(await _brand_clip(OUTRO_CLIP, work / "brand_outro.mp4"))
        except Exception as exc:  # noqa: BLE001
            log.warning("shorts: weekly outro clip skipped: %s", exc)

    await daily._progress(88, "Склейка и музыка")
    body = work / "body.mp4"
    await _concat(segments, body, work)
    for seg in segments:
        seg.unlink(missing_ok=True)
    music = musiclib.pick("новости кино", " ".join(s["headline"] for s in stories)) or ASSETS / "music.mp3"
    final = work / "episode.mp4"
    await _run("ffmpeg", "-v", "error", "-y", "-i", str(body), "-stream_loop", "-1", "-i", str(music),
               "-filter_complex", "[1:a]volume=-24dB[m];[0:a][m]amix=inputs=2:duration=first:dropout_transition=0,"
               "volume=2[a]", "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
               "-movflags", "+faststart", str(final))
    body.unlink(missing_ok=True)
    length = await _duration(final)

    await daily._progress(93, "Превью для Telegram")
    preview: Path | None = work / "preview.mp4"
    kbps = max(250, int(PREVIEW_MB * 8 * 1024 / max(length, 1)) - 72)
    if final.stat().st_size <= PREVIEW_MB * 1024 * 1024:  # сам выпуск влезает в Telegram — шлём его
        preview = final
    else:
        try:  # качество ограничено и размером (crf), и потолком битрейта — превью влезает в 50 МБ
            await _run("ffmpeg", "-v", "error", "-y", "-i", str(final), "-vf", "scale=854:480", "-c:v", "libx264",
                       "-preset", "veryfast", "-crf", "27", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k",
                       "-c:a", "aac", "-b:a", "64k", "-ac", "1", "-movflags", "+faststart", str(preview))
            if preview.stat().st_size > 49 * 1024 * 1024:
                preview = None
        except Exception as exc:  # noqa: BLE001
            log.warning("shorts: weekly preview failed: %s", exc)
            preview = None

    cover = work / "cover.jpg"
    thumbnail(first_img, stories[0]["headline"], date_label).save(cover, quality=92)
    seo = build_seo(script, chapters, date_label)
    if notes:
        seo["warning"] = "\n".join(notes)
    await daily._progress(98, "Готово")
    return Episode(final, preview, cover, seo, length)


def build_seo(script: dict[str, Any], chapters: list[tuple[float, str]], date_label: str) -> dict[str, str]:
    title = script.get("title") or (" / ".join(s["headline"] for s in script["stories"][:2]) + " / Новости кино")
    if len(title) > 100:
        title = title[:100].rsplit(" / ", 1)[0]
    lines = [script.get("description") or f"Главные новости кино за неделю ({date_label}).", "",
             "⏱ Таймкоды:"] + [f"{_clock(t)} {name}" for t, name in chapters]
    lines += ["", "Какая новость удивила вас больше всего? Пишите в комментариях 👇", "",
              f"📢 Telegram: {daily.TELEGRAM_URL}", f"▶️ YouTube: {daily.YOUTUBE_URL}", "",
              "#новостикино #кино #сериалы #kinomir360"]
    tags = script.get("tags") or ""
    base = "новости кино, кино новости, новости кино 2026, киноновости, фильмы, сериалы, KinoMir360"
    pinned = ("Какая новость недели удивила вас больше всего? 👇\n\nНовый выпуск «Новости кино» — каждое "
              "воскресенье. Подписывайтесь, чтобы не пропустить!")
    return {"title": title, "description": "\n".join(lines), "tags": f"{tags}, {base}".strip(", "),
            "pinned": pinned, "long": "1"}


def headlines(script: dict[str, Any]) -> str:
    """Темы выпуска одной строкой (для памяти «что было в прошлый раз»)."""
    return "; ".join(re.sub(r"\s+", " ", s["headline"]) for s in script.get("stories") or [])

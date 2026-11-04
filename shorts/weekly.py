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

from . import boxchart, daily, lumean, netsafe, tmdb
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
    try:
        _, err = await proc.communicate()
    except asyncio.CancelledError:  # таймаут сборки — ffmpeg не должен работать дальше и писать в удалённую папку
        proc.kill()
        try:  # дождаться, чтобы не остался зомби, даже если отмену повторят
            await asyncio.shield(proc.wait())
        except asyncio.CancelledError:
            pass
        raise
    if proc.returncode:
        raise RuntimeError(f"{args[0]}: {err.decode(errors='ignore')[-500:]}")


async def _duration(path: Path) -> float:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 60)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        proc.kill()
        try:
            await asyncio.shield(proc.wait())
        except asyncio.CancelledError:
            pass
        raise
    try:
        return float(out.decode().strip())
    except ValueError:
        return 0.0


# ---------------------------------------------------------------- картинки

def _decode(data: bytes, mourning: bool) -> Image.Image | None:
    """Байты → картинка RGB, сразу уменьшенная (кадру 1920×1080 больше не нужно — и память не держим)."""
    img = Image.open(io.BytesIO(data))
    if img.width * img.height > 50_000_000:  # 50 Мп — не картинка для кадра, а подвох или скан
        return None
    img.load()
    img = img.convert("RGB")
    if min(img.size) < 200:
        return None
    img.thumbnail((2400, 2400), Image.LANCZOS)
    return ImageOps.grayscale(img).convert("RGB") if mourning else img


async def _download(session: aiohttp.ClientSession, url: str, mourning: bool = False) -> Image.Image | None:
    try:
        data = await netsafe.fetch(session, url)  # только публичные адреса, до 15 МБ
        return await asyncio.to_thread(_decode, data, mourning)  # разбор картинки — не в цикле событий бота
    except Exception as exc:  # noqa: BLE001
        log.info("shorts: weekly image %s skipped: %s", url[:80], exc)
        return None


async def story_images(story: dict[str, Any], posts: list[dict[str, Any]], tmdb_key: str,
                       limit: int = 6) -> list[Image.Image]:
    """Картинки к новости: фото людей, кадры и постер фильма с TMDB, фото из постов-источников."""
    person = story.get("person_en") or story.get("person")

    async def person_urls() -> list[str]:
        if not person:
            return []
        try:
            info = await tmdb.person_by_name(tmdb_key, person)
            return tmdb.profile_urls(info)[:3 if story.get("mourning") else 2] if info else []
        except Exception as exc:  # noqa: BLE001
            log.info("shorts: weekly person %s failed: %s", person, exc)
            return []

    async def film_urls() -> list[str]:
        """Кадры фильма или сериала — только при точном совпадении названия: «Джеймс Бонд» не должен дать
        мультсериал «James Bond Jr.». Новинки, которых ещё нет на TMDB («Джуманджи: Великий побег»), ищем
        по названию франшизы (до двоеточия)."""
        names = [n for n in (story.get("film_en"), story.get("film")) if n]
        if not names:
            return []
        year = _int(story.get("year"))
        tries = list(dict.fromkeys(names + [re.split(r"[:.—–-]", n)[0].strip() for n in names]))
        for query in tries:
            if len(query) < 3:
                continue
            try:
                films, shows = await asyncio.gather(tmdb.search_movie(tmdb_key, query),
                                                    tmdb.search_tv(tmdb_key, query), return_exceptions=True)
            except Exception as exc:  # noqa: BLE001
                log.info("shorts: weekly film %s failed: %s", query, exc)
                continue
            cands = [(r, "movie") for r in (films if isinstance(films, list) else [])[:4]] + \
                    [(r, "tv") for r in (shows if isinstance(shows, list) else [])[:3]]

            def score(c: tuple[dict[str, Any], str]) -> float:
                return max(max(tmdb.similar(c[0].get("title") or "", q), tmdb.similar(c[0].get("original") or "", q))
                           for q in (query, *names))

            good = [c for c in cands if score(c) >= 0.93 and _year_ok(c[0], year)]
            if not good:
                continue
            # из точных совпадений — самый популярный, фильм раньше сериала
            best, kind = max(good, key=lambda c: (score(c), c[1] == "movie", c[0].get("popularity") or 0))
            try:
                out = await tmdb.backdrops(tmdb_key, kind, int(best["id"]), 3)
            except Exception as exc:  # noqa: BLE001
                log.info("shorts: weekly backdrops %s failed: %s", query, exc)
                out = []
            return out + ([best["poster"]] if best.get("poster") else [])
        return []

    people, films = await asyncio.gather(person_urls(), film_urls())
    urls: list[str] = people + films
    keys = [_words(x) for x in (story.get("film"), story.get("film_en"), story.get("person"),
                                 story.get("person_en")) if x]
    for i in story.get("media") or []:
        if 1 <= i <= len(posts):
            p = posts[i - 1]
            text = _words(p.get("text") or "")
            if keys and not any(k and all(w[:max(3, len(w) - 2)] in text for w in k.split()[:2]) for k in keys):
                continue  # ИИ дал номер поста не об этой новости — его фото не берём
            urls += [m["image"] for m in p.get("media") or [] if m.get("image")][:3] or (
                [p["image"]] if p.get("image") else [])
    if len(urls) < 2 and keys:  # мало картинок — фото из любых постов недели о том же (по имени или названию)
        for p in posts:
            text = _words(p.get("text") or "")
            if any(k and all(w[:max(3, len(w) - 2)] in text for w in k.split()[:2]) for k in keys):
                urls += [m["image"] for m in p.get("media") or [] if m.get("image")][:2] or (
                    [p["image"]] if p.get("image") else [])
            if len(urls) >= limit:
                break
    wanted = list(dict.fromkeys(urls))[:limit + 3]  # с запасом на битые
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), headers=HEADERS) as session:
        got = await asyncio.gather(*(_download(session, u, bool(story.get("mourning"))) for u in wanted))
    return [img for img in got if img][:limit]


def _int(value: Any) -> int:
    try:
        return int(str(value or "0")[:4])
    except ValueError:
        return 0


def _year_ok(row: dict[str, Any], year: int) -> bool:
    """Год найденного фильма совпадает с годом из новости (±1): новость о «Догфайте» Гая Ричи (2028) не должна
    брать кадры одноимённого фильма 1991 года. Год неизвестен — не проверяем."""
    got = _int(row.get("year") or row.get("date") or row.get("first_air_date"))
    return not year or not got or abs(got - year) <= 1


def _words(text: str) -> str:
    return " ".join(re.findall(r"[\wа-яё]+", str(text or "").lower().replace("ё", "е")))


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
    # камера едет по кадру, увеличенному на 10%: края обрезаются — текст держим в пределах W - 400
    safe = W - 400
    top_size = 48
    while draw.textlength(top, font=_font("Montserrat.ttf", top_size, "Bold")) > safe and top_size > 26:
        top_size -= 2
    draw.text((W // 2, 300), top, font=_font("Montserrat.ttf", top_size, "Bold"), fill=color, anchor="mm")
    words, rows, size = big.split(), [], 120
    while size >= 60:
        font = _font("Oswald.ttf", size, "Bold")
        rows = [""]
        for w in words:
            if draw.textlength(f"{rows[-1]} {w}".strip(), font=font) <= safe:
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
    """Кусок видео из картинки: камера медленно едет вбок, поверх — плашка заголовка. Готовый ролик (график
    сборов) — как есть, в те же параметры (последний кадр держим, если ролик короче)."""
    if img_path.suffix == ".mp4":
        await _run("ffmpeg", "-v", "error", "-y", "-i", str(img_path), "-vf",
                   f"scale={W}:{H},fps={FPS},tpad=stop_mode=clone:stop_duration=30,format=yuv420p",
                   "-t", f"{dur:.3f}", "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                   "-an", str(out))
        return
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
    # тишина вторым входом: если у ролика нет своего звука, берётся она — у всех кусков одинаковая дорожка
    await _run("ffmpeg", "-v", "error", "-y", "-i", str(src), "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
               "-filter_complex", f"[0:v]scale={W}:{H},fps={FPS},format=yuv420p[v]", "-map", "[v]",
               "-map", "0:a?" if await _has_audio(src) else "1:a", "-shortest", "-r", str(FPS), "-c:v", "libx264",
               "-preset", "veryfast", "-crf", "22", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", str(out))
    return out


async def _has_audio(path: Path) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0",
        str(path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await asyncio.wait_for(proc.communicate(), 60)
    return bool(out.strip())


def _even_slides(paths: list[Path], starts: list[float], total: float) -> list[tuple[Path, float]]:
    """Картинки по фразам: смена картинки на начале фразы, примерно поровну (каждая — не меньше 3 с)."""
    if not paths:
        return []
    n = max(1, min(len(paths), round(total / 3.5), len(starts) or 1))
    cuts = sorted({starts[round(i * len(starts) / n)] for i in range(n)} | {0.0}) if starts else [0.0]
    cuts = [c for k, c in enumerate(cuts) if k == 0 or c - cuts[k - 1] >= 3.0]
    ends = cuts[1:] + [total]
    return [(paths[k % len(paths)], max(e - s, 0.5)) for k, (s, e) in enumerate(zip(cuts, ends))]


def _insert_clip(slides: list[tuple[Path, float]], t0: float, clip: Path, dur: float,
                 total: float) -> list[tuple[Path, float]]:
    """Вставить ролик (график) в ряд картинок с момента t0: картинки на это время уступают место."""
    t1 = min(t0 + dur, total)
    spans, t = [], 0.0
    for path, d in slides:
        s, e = t, t + d
        t = e
        if e <= t0 or s >= t1:
            spans.append((s, e, path))
            continue
        if s < t0:
            spans.append((s, t0, path))
        if e > t1:
            spans.append((t1, e, path))
    spans.append((t0, t1, clip))
    spans.sort(key=lambda x: x[0])
    out: list[list[Any]] = []
    for s, e, path in spans:
        if out and e - s < 1.0 and out[-1][0].suffix != ".mp4" and path.suffix != ".mp4":
            out[-1][1] += e - s  # обрезок короче секунды — продлеваем прошлую картинку
        else:
            out.append([path, e - s])
    return [(p, d) for p, d in out if d > 0.05]


MONEY = re.compile(r"сбор|кассов|бюджет|окуп|уик-?энд", re.I)  # новость о деньгах, а не просто «в прокате с…»


async def _money_clips(work: Path, n: int, story: dict[str, Any], lines: list[str], starts: list[float],
                       total: float, bg: Image.Image | None, tmdb_key: str) -> list[tuple[float, Path, float]]:
    """Графики про деньги для новости о сборах: [(начало, ролик, длительность)] — с фразы, где речь о сборах."""
    if not (story.get("money") or any(MONEY.search(x) for x in lines)) or total < 18:
        return []
    names = [x for x in (story.get("film_en"), story.get("film")) if x]
    if not names:
        return []
    try:
        info = await tmdb.box_office(tmdb_key, names, _int(story.get("year")))
    except Exception as exc:  # noqa: BLE001
        log.info("shorts: weekly box office %s failed: %s", names[0], exc)
        info = None
    charts = boxchart.plan(story, info)[: 2 if total >= 32 else 1]
    if not charts:
        return []
    each = 8.0
    first = next((i for i, x in enumerate(lines) if MONEY.search(x)), 0)
    t0 = starts[first] if first < len(starts) else total * 0.25
    t0 = max(0.0, min(t0, total - each * len(charts) - 1.0))
    out = []
    for k, (kind, data) in enumerate(charts):
        try:
            clip = await boxchart.render(kind, data, bg, each, work / f"money{n}_{k}.mp4")
        except Exception as exc:  # noqa: BLE001
            log.warning("shorts: weekly chart %s failed: %s", kind, exc)
            continue
        out.append((t0 + k * each, clip, each))
    return out


def _clock(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


class VoiceChanged(RuntimeError):
    """Основной голос (клон в Lumean) отказал посреди выпуска — дальше был бы другой голос."""


async def _voice(lines: list[str], work: Path) -> tuple[Path, list[float], float, bool]:
    """Озвучка куска выпуска. Весь выпуск — одним голосом: если клон в Lumean не ответил (кончился баланс,
    сбой), пробуем ещё раз, а потом останавливаем сборку — чтобы в середине не заговорил голос Microsoft."""
    work.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        voice, starts, voiced, _ = await daily.synthesize(lines, work, careful=True)
        note = daily.tts_note or ""
        if not (lumean.enabled() and "Lumean" in note and "не сработал" in note):
            return voice, starts, await _duration(voice), voiced
        log.warning("shorts: weekly voice fell back (attempt %d): %s", attempt + 1, note[:200])
        if attempt == 0:
            await asyncio.sleep(20)
    raise VoiceChanged("клон голоса в Lumean не ответил, выпуск остановлен, чтобы голос не сменился посреди "
                       f"ролика. Причина: {note[:300]}. Если это «Insufficient balance» — пополни баланс Lumean "
                       "и собери выпуск снова")


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

    daily.stressed.clear()
    await daily._progress(3, "Картинки к новостям")
    pictures: list[list[Image.Image]] = []
    for k, story in enumerate(stories):
        imgs = await story_images(story, posts, tmdb_key)
        pictures.append(imgs)
        first_img = first_img or (imgs[0] if imgs else None)
        await daily._progress(3 + round(12 * (k + 1) / total_n), "Картинки к новостям")

    def card_file(bg: Image.Image | None, top: str, big: str, small: str, path: Path,
                  mourning: bool = False) -> Path:
        slide_image(title_card(bg, top, big, small, mourning)).save(path, quality=90)
        return path

    def overlay_file(headline: str, n: int, total: int, mourning: bool, path: Path) -> Path:
        overlay(headline, n, total, mourning).save(path)
        return path

    # тяжёлые операции с картинками (размытие, текст) — в отдельном потоке, иначе бот на это время «замирает»
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
    await asyncio.to_thread(card_file, first_img, "KINOMIR360", "НОВОСТИ КИНО", date_label, intro_card)
    chapters.append((0.0, "Вступление"))
    if INTRO_CLIP.exists():  # анимированная заставка с логотипом — самое начало выпуска
        try:
            segments.append(await _brand_clip(INTRO_CLIP, work / "brand_intro.mp4"))
            clock += await _duration(segments[-1])
        except Exception as exc:  # noqa: BLE001
            log.warning("shorts: weekly intro clip skipped: %s", exc)
    # под приветствие и анонс — не одна заставка, а кадры главных новостей выпуска (что сегодня будет)
    teaser = [imgs[0] for imgs in pictures[:4] if imgs]
    teaser_paths = await asyncio.to_thread(save_slides, teaser, "teaser") if teaser else []
    slides = [(intro_card, min(3.0, dur + 0.4))]
    rest = dur + 0.4 - slides[0][1]
    if teaser_paths and rest > 1.5:
        each = rest / len(teaser_paths)
        slides += [(path, each) for path in teaser_paths]
    else:
        slides = [(intro_card, dur + 0.4)]
    segments.append(await _segment(work, "intro", slides, None, voice, dur + 0.4))
    clock += dur + 0.4

    whoosh = ASSETS / "whoosh.wav"
    for k, story in enumerate(stories):
        n = k + 1
        await daily._progress(18 + round(64 * k / total_n), f"Новость {n} из {total_n}: озвучка и монтаж")
        imgs = pictures[k]
        bg = imgs[0] if imgs else first_img
        card = work / f"card{n}.jpg"
        await asyncio.to_thread(card_file, bg, f"НОВОСТЬ {n}", story["headline"], "", card, story["mourning"])
        segments.append(await _segment(work, f"card{n}", [(card, CARD_SEC)], None,
                                       whoosh if whoosh.exists() else None, CARD_SEC))
        chapters.append((clock, story.get("chapter") or story["headline"].capitalize()))
        clock += CARD_SEC
        voice, starts, dur, voiced = await _voice(story["lines"], work / f"v{n}")
        if not voiced and not notes:
            notes.append(daily.no_voice_note(voiced))
        paths = await asyncio.to_thread(save_slides, imgs, f"st{n}") or [card]
        over = work / f"over{n}.png"
        await asyncio.to_thread(overlay_file, story["headline"], n, total_n, story["mourning"], over)
        total = dur + 0.5
        slides = _even_slides(paths, starts, total)
        for t0, clip, length in await _money_clips(work, n, story, story["lines"], starts, total, bg, tmdb_key):
            slides = _insert_clip(slides, t0, clip, length, total)  # сборы франшизы и окупаемость — графиком
        segments.append(await _segment(work, f"story{n}", slides, over, voice, total))
        clock += total

    await daily._progress(84, "Финал выпуска")
    voice, starts, dur, voiced = await _voice(script["outro"], work / "v_outro")
    outro_card = work / "outro_card.jpg"
    # «Спасибо, что смотрите!» говорит анимированная концовка — здесь вопрос зрителям, без повтора
    await asyncio.to_thread(card_file, first_img, "НОВЫЕ ВЫПУСКИ ПО СРЕДАМ И ВОСКРЕСЕНЬЯМ",
                            "КАКАЯ НОВОСТЬ УДИВИЛА ВАС БОЛЬШЕ ВСЕГО?", "Пишите в комментариях",
                            outro_card)
    tail = 0.6 if OUTRO_CLIP.exists() else 2.0  # с анимированной концовкой карточку долго не держим
    # под финальные фразы — не одна карточка 15 секунд, а быстрый повтор кадров всех новостей выпуска
    recap = await asyncio.to_thread(save_slides, [imgs[0] for imgs in pictures if imgs], "recap")
    head = min(4.0, dur + tail)
    slides = [(outro_card, dur + tail)]
    if recap and dur + tail - head >= 2.0 * min(len(recap), 3):
        each = (dur + tail - head) / len(recap)
        if each < 1.6:
            recap = recap[: max(1, int((dur + tail - head) / 1.6))]
            each = (dur + tail - head) / len(recap)
        slides = [(outro_card, head)] + [(path, each) for path in recap]
    segments.append(await _segment(work, "outro", slides, None, voice, dur + tail))
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
            for scale, crf, rate in (("854:480", "27", kbps), ("640:360", "30", int(kbps * 0.7))):
                await _run("ffmpeg", "-v", "error", "-y", "-i", str(final), "-vf", f"scale={scale}", "-c:v",
                           "libx264", "-preset", "veryfast", "-crf", crf, "-maxrate", f"{rate}k", "-bufsize",
                           f"{rate * 2}k", "-c:a", "aac", "-b:a", "64k", "-ac", "1", "-movflags", "+faststart",
                           str(preview))
                if preview.stat().st_size <= 49 * 1024 * 1024:
                    break  # не влезло в 480p — второй заход в 360p
            else:
                preview = None
        except Exception as exc:  # noqa: BLE001
            log.warning("shorts: weekly preview failed: %s", exc)
            preview = None

    cover = work / "cover.jpg"
    await asyncio.to_thread(lambda: thumbnail(first_img, stories[0]["headline"], date_label).save(cover, quality=92))
    seo = build_seo(script, chapters, date_label)
    if notes:
        seo["warning"] = "\n".join(notes)
    if daily.stressed:  # автор слушает и правит неверные через /say — следующая сборка прочтёт верно
        seo["stress"] = ", ".join(sorted(daily.stressed, key=str.lower))
    await daily._progress(98, "Готово")
    return Episode(final, preview, cover, seo, length)


def build_seo(script: dict[str, Any], chapters: list[tuple[float, str]], date_label: str) -> dict[str, str]:
    title = script.get("title") or (" / ".join(s["headline"] for s in script["stories"][:2]) + " / Новости кино")
    if len(title) > 100:
        title = title[:100].rsplit(" / ", 1)[0]
    lines = [script.get("description") or f"Главные новости кино ({date_label}).", "",
             "⏱ Таймкоды:"] + [f"{_clock(t)} {name}" for t, name in chapters]
    lines += ["", "Какая новость удивила вас больше всего? Пишите в комментариях 👇", "",
              f"📢 Telegram: {daily.TELEGRAM_URL}", f"▶️ YouTube: {daily.YOUTUBE_URL}", "",
              "#новостикино #кино #сериалы #kinomir360"]
    tags = script.get("tags") or ""
    base = "новости кино, кино новости, новости кино 2026, киноновости, фильмы, сериалы, KinoMir360"
    pinned = ("Какая новость удивила вас больше всего? 👇\n\nНовые выпуски «Новости кино» — по средам и "
              "воскресеньям. Подписывайтесь, чтобы не пропустить!")
    return {"title": title, "description": "\n".join(lines), "tags": f"{tags}, {base}".strip(", "),
            "pinned": pinned, "long": "1"}


def headlines(script: dict[str, Any]) -> str:
    """Темы выпуска одной строкой (для памяти «что было в прошлый раз»)."""
    return "; ".join(re.sub(r"\s+", " ", s["headline"]) for s in script.get("stories") or [])

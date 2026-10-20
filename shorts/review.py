"""Обзор фильма 16:9 для YouTube: сценарий (ИИ, текст автора или и то и другое), голос, монтаж.

Шаги (бот проводит по ним кнопками):
  1. find_film — фильм по названию или id Кинопоиска;
  2. write_script / parse_script — сценарий по разделам; автор проверяет и правит его в боте;
  3. make_review — озвучка, кадры TMDB и Кинопоиска, фрагменты трейлера, субтитры, рендер
     (shorts/make_review.py отдельным процессом), обложка 16:9 и SEO с таймкодами.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

import aiohttp
from PIL import Image, ImageDraw, ImageFilter

from . import ai, clips, daily, tmdb
from .daily import (
    ASSETS,
    ROOT,
    TELEGRAM_URL,
    YOUTUBE_URL,
    Fetch,
    ShortResult,
    Theme,
    _download,
    _download_first,
    _genres,
    _mentioned_person,
    _money,
    _poster_url,
    _progress,
    _stills,
    _title,
)

log = logging.getLogger("shorts")

SHOT = 5.5  # сек на один кадр
CLIP_SECONDS = 5.0
MAX_CLIPS = 14
SECTION_PAUSE = 2.5  # тишина между разделами в заказе Lumean — по ней режем дорожку
TITLE_SECONDS = 5.0  # заставка с названием в начале


# ---------------------------------------------------------------- фильм и сценарий

async def find_film(fetch: Fetch, query: str) -> dict[str, Any]:
    """Фильм по запросу «Дюна 2021» или «id:409424»: самый известный из найденных."""
    query = query.strip()
    if query.startswith("id:") and query[3:].isdigit():
        return await fetch(f"/movie/{int(query[3:])}")
    year_m = re.search(r"\s(19\d\d|20\d\d)$", query)
    year = int(year_m.group(1)) if year_m else None
    name = query[: year_m.start()].strip() if year_m else query
    data = await fetch("/movie/search", query=name, page=1, limit=10)
    cands = [x for x in data.get("docs") or [] if isinstance(x, dict) and x.get("id")]
    if year:
        cands = [c for c in cands if c.get("year") == year] or cands
    if not cands:
        raise RuntimeError(f"Кинопоиск не нашёл «{query}»")
    cands.sort(key=lambda c: -int((c.get("votes") or {}).get("kp") or 0))
    return await fetch(f"/movie/{cands[0]['id']}")


def _people(film: dict[str, Any], prof: str, n: int) -> list[dict[str, Any]]:
    return [p for p in film.get("persons") or []
            if isinstance(p, dict) and p.get("name") and str(p.get("enProfession") or "") == prof][:n]


def _clean(text: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", str(text or "")).replace("\xa0", " ")).strip()


def brief(film: dict[str, Any]) -> str:
    """Данные о фильме для ИИ: только то, что есть на Кинопоиске (выдумывать ИИ запрещено)."""
    rows = [f"Название: «{_title(film)}»" + (f" (оригинал: {film['alternativeName']})"
                                              if film.get("alternativeName") else "")]
    for label, value in (("Год", film.get("year")), ("Тип", "сериал" if film.get("isSeries") else "фильм"),
                         ("Жанры", ", ".join(_genres(film))),
                         ("Страны", ", ".join(c.get("name", "") for c in film.get("countries") or []
                                              if isinstance(c, dict))),
                         ("Длительность", f"{film['movieLength']} мин" if film.get("movieLength") else ""),
                         ("Слоган", film.get("slogan")),
                         ("Бюджет", _money(film.get("budget"))),
                         ("Сборы в мире", _money((film.get("fees") or {}).get("world")))):
        if value:
            rows.append(f"{label}: {value}")
    r = film.get("rating") or {}
    ratings = [f"{name} {float(r[key]):.1f}" for key, name in (("kp", "Кинопоиск"), ("imdb", "IMDb"))
               if r.get(key)]
    if ratings:
        rows.append("Рейтинги: " + ", ".join(ratings))
    directors = [p["name"] for p in _people(film, "director", 2)]
    if directors:
        rows.append("Режиссёр: " + ", ".join(directors))
    actors = [p["name"] + (f" — {p['description']}" if p.get("description") else "")
              for p in _people(film, "actor", 8)]
    if actors:
        rows.append("В ролях: " + "; ".join(actors))
    if film.get("description"):
        rows.append("Сюжет: " + _clean(film["description"])[:1500])
    facts = [_clean(f.get("value")) for f in film.get("facts") or []
             if isinstance(f, dict) and not f.get("spoiler") and f.get("value")]
    facts = [f for f in facts if 30 <= len(f) <= 450][:15]
    if facts:
        rows.append("Факты Кинопоиска:\n" + "\n".join(f"- {f}" for f in facts))
    return "\n".join(rows)


async def write_script(film: dict[str, Any], notes: str = "", key: str = "") -> dict[str, Any]:
    script = await ai.write_review(key, brief(film), notes)
    script["film_id"] = int(film["id"])
    return script


def parse_script(text: str, film_id: int, title: str = "") -> dict[str, Any]:
    """Сценарий из текста автора: разделы со строки «## Заголовок»; без них — по абзацам."""
    text = text.strip()
    sections: list[dict[str, str]] = []
    if re.search(r"(?m)^\s*##", text):
        for block in re.split(r"(?m)^\s*##\s*", text):
            head, _, body = block.partition("\n")
            if body.strip():
                sections.append({"heading": head.strip()[:40], "text": re.sub(r"\s+", " ", body).strip()})
            elif head.strip() and not sections:  # текст до первого заголовка
                sections.append({"heading": "", "text": head.strip()})
    else:
        paras = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        if len(paras) == 1:  # один абзац — делим по предложениям, примерно по 400 символов
            paras, cur = [], ""
            for sent in re.split(r"(?<=[.!?…])\s+", text):
                cur = f"{cur} {sent}".strip()
                if len(cur) >= 400:
                    paras.append(cur)
                    cur = ""
            if cur:
                paras.append(cur)
        sections = [{"heading": "", "text": p} for p in paras]
    if not sections:
        raise ValueError("пустой текст")
    return {"title": title, "sections": sections, "film_id": film_id}


def script_text(script: dict[str, Any]) -> str:
    """Сценарий в виде текста «## Заголовок» + абзац — так же его можно поправить и прислать обратно."""
    return "\n\n".join((f"## {s['heading']}\n" if s.get("heading") else "") + s["text"]
                       for s in script["sections"])


def script_chars(script: dict[str, Any]) -> int:
    return sum(len(s["text"]) for s in script["sections"])


# ---------------------------------------------------------------- картинки

def _frame_image(img: Image.Image, w: int = 1408, h: int = 792) -> Image.Image:
    """Кадр 16:9 с запасом под наезд; вертикальная картинка — целиком на размытом фоне."""
    if img.width / img.height >= 1.5:
        s = max(w / img.width, h / img.height)
        img = img.resize((round(img.width * s), round(img.height * s)), Image.LANCZOS)
        x, y = (img.width - w) // 2, (img.height - h) // 2
        return img.crop((x, y, x + w, y + h))
    s = max(w / img.width, h / img.height)
    bg = img.resize((round(img.width * s), round(img.height * s)))
    bg = bg.crop(((bg.width - w) // 2, (bg.height - h) // 2, (bg.width - w) // 2 + w, (bg.height - h) // 2 + h))
    bg = Image.blend(bg.filter(ImageFilter.GaussianBlur(30)), Image.new("RGB", (w, h)), 0.45)
    fg = img.copy()
    fg.thumbnail((w, h - 60), Image.LANCZOS)
    bg.paste(fg, ((w - fg.width) // 2, (h - fg.height) // 2))
    return bg


def make_thumbnail(bg: Image.Image, title: str, rating: str) -> Image.Image:
    """Обложка 1280×720 для YouTube: кадр, «ОБЗОР», крупное название, рейтинг."""
    from .make_short import font, wrap
    cfg = {"fonts": {"head": ASSETS / "Oswald.ttf", "sans": ASSETS / "Montserrat.ttf"}}
    w, h = 1280, 720
    img = _frame_image(bg, w, h).convert("RGBA")
    shade = Image.new("L", (w, 1))
    for x in range(w):
        shade.putpixel((x, 0), round(230 * max(0.0, 1 - x / (w * 0.75))))
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    layer.putalpha(shade.resize((w, h)))
    img.alpha_composite(layer)
    d = ImageDraw.Draw(img)
    tag_f = font(cfg, "sans", 44)
    tw = d.textlength("ОБЗОР", font=tag_f)
    d.rectangle((60, 80, 60 + tw + 50, 154), fill=(229, 9, 20))
    d.text((85, 88), "ОБЗОР", font=tag_f, fill=(255, 255, 255))
    room = h - 190 - (130 if rating else 30)  # высота под название, чтобы рейтинг не уехал за край
    for size in (130, 110, 92, 78, 64, 54):
        head_f = font(cfg, "head", size)
        lines = wrap(d, title.upper(), head_f, 760)
        if len(lines) <= 3 and len(lines) * size * 1.1 <= room:
            break
    y = 190
    for line in lines[:3]:
        d.text((60, y), line, font=head_f, fill=(255, 255, 255), stroke_width=4, stroke_fill=(0, 0, 0))
        y += round(size * 1.1)
    if rating:
        r_f = font(cfg, "sans", 46)
        rw = d.textlength(rating, font=r_f)
        d.rounded_rectangle((60, y + 20, 60 + rw + 44, y + 96), 14, fill=(255, 196, 0))
        d.text((82, y + 30), rating, font=r_f, fill=(0, 0, 0))
    return img.convert("RGB")


# ---------------------------------------------------------------- время: субтитры и кадры

def _sentences(text: str) -> list[str]:
    out: list[str] = []
    for sent in re.split(r"(?<=[.!?…])\s+", text.strip()):
        if len(sent) > 120 and "," in sent:  # длинное — по запятым, чтобы влезало в две строки
            cur = ""
            for piece in re.split(r"(?<=,)\s+", sent):
                if cur and len(cur) + len(piece) > 110:
                    out.append(cur)
                    cur = piece
                else:
                    cur = f"{cur} {piece}".strip()
            if cur:
                out.append(cur)
        elif sent.strip():
            out.append(sent.strip())
    return out


def subtitles(text: str, start: float, speech: float) -> list[dict[str, Any]]:
    """Субтитры раздела: время каждой фразы — пропорционально её длине."""
    sents = _sentences(text)
    total = sum(len(s) for s in sents) or 1
    out, t = [], start
    for s in sents:
        d = speech * len(s) / total
        out.append({"start": round(t, 2), "end": round(t + d, 2), "text": s})
        t += d
    return out


def _clock(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"


# ---------------------------------------------------------------- SEO

def build_review_seo(film: dict[str, Any], script: dict[str, Any], chapters: list[tuple[float, str]]) -> dict[str, str]:
    t, year = _title(film), film.get("year")
    title = script.get("title") or f"«{t}» — обзор: стоит ли смотреть?"
    if len(title) > 100:
        title = title[:97].rstrip() + "…"
    first = script["sections"][0]["text"]
    intro = first if len(first) <= 300 else first[:297].rsplit(" ", 1)[0] + "…"
    lines = [intro, ""]
    # таймкоды → главы YouTube: первая с 0:00, минимум три, каждая от 10 секунд
    chapters = [(s, h) for s, h in chapters if h]
    if len(chapters) >= 3:
        chapters[0] = (0.0, chapters[0][1])
        lines += ["⏱ Содержание:"] + [f"{_clock(s)} {h}" for s, h in chapters] + [""]
    directors = [p["name"] for p in _people(film, "director", 1)]
    actors = [p["name"] for p in _people(film, "actor", 4)]
    about = f"🎬 «{t}»" + (f" ({year})" if year else "")
    if directors:
        about += f", режиссёр {directors[0]}"
    lines += [about]
    if actors:
        lines.append("В ролях: " + ", ".join(actors))
    lines += ["", "А тебе как фильм? Пиши в комментариях 👇", "",
              f"📺 YouTube: {YOUTUBE_URL}",
              f"✈️ Telegram: {TELEGRAM_URL} — новинки, трейлеры и подборки каждый день", ""]
    name_tag = re.sub(r"[^\wё]", "", t.lower().replace(" ", ""))
    lines.append(" ".join(f"#{h}" for h in ["обзор", "обзорфильма", name_tag, "кино", "KinoMir360"] if h))
    tags: list[str] = []
    en = str(film.get("alternativeName") or film.get("enName") or "")
    for tag in [f"{t.lower()} обзор", f"обзор фильма {t.lower()}", t.lower(),
                f"{t.lower()} {year}" if year else "", f"{en.lower()} review" if en else "",
                "обзор фильма", "рецензия", "стоит ли смотреть", "пасхалки", "факты о фильме",
                *[a.lower() for a in actors[:3]], *[d.lower() for d in directors],
                "movie review", "KinoMir360", "киномир360"]:
        if tag and tag not in tags and len(", ".join(tags + [tag])) <= 480:
            tags.append(tag)
    pinned = (f"А как тебе «{t}»? Ставь оценку от 1 до 10 в комментариях 👇\n"
              f"Новинки и трейлеры каждый день — в Telegram {TELEGRAM_URL}")
    return {"title": title, "description": "\n".join(lines), "tags": ", ".join(tags), "pinned": pinned}


# ---------------------------------------------------------------- сборка

async def _trailer_clips(film: dict[str, Any], work: Path, n: int) -> tuple[list[str], str]:
    """n фрагментов по CLIP_SECONDS, равномерно по трейлеру (без логотипов в начале и титров в конце)."""
    if n <= 0:
        return [], ""
    titles = [str(film.get(k) or "") for k in ("name", "alternativeName", "enName")]
    tdir = work / "trailer"
    try:
        found = await clips.rutube_trailer([t for t in titles if t], film.get("year"))
        if not found:
            return [], "трейлер на Rutube не нашёлся"
        tdir.mkdir(exist_ok=True)
        src = await clips.download_full(found["url"], tdir)
        dur = float(found.get("duration") or 0) or await daily._duration(src)
        lo, hi = max(6.0, dur * 0.08), dur * 0.88 - CLIP_SECONDS
        if hi <= lo:
            return [], "трейлер слишком короткий"
        n = min(n, max(1, int((hi - lo) / CLIP_SECONDS)))
        out = []
        for k in range(n):
            clip = work / f"rclip{k}.mp4"
            await clips.cut_clip(src, clip, lo + (hi - lo) * k / max(n - 1, 1), CLIP_SECONDS)
            if clip.exists():
                out.append(clip.name)
        return out, ""
    except Exception as exc:  # noqa: BLE001 — без трейлера будут кадры
        log.warning("shorts: review trailer for %s failed: %s", _title(film), exc)
        return [], f"{type(exc).__name__}: {str(exc)[:150]}"
    finally:
        import shutil
        shutil.rmtree(tdir, ignore_errors=True)


async def make_review(fetch: Fetch, work: Path, script: dict[str, Any], music: Path | None = None,
                      tmdb_key: str = "", trailer_clips: bool = True) -> ShortResult:
    await _progress(3, "Загружаю фильм")
    film = await fetch(f"/movie/{script['film_id']}")
    t = _title(film)
    sections = script["sections"]

    await _progress(8, f"«{t}»: озвучка (ваш текст ≈{sum(len(s['text']) for s in sections)} символов)")
    voice, starts, voiced, spans = await daily.synthesize([s["text"] for s in sections], work, SECTION_PAUSE)
    speech_end = await daily._duration(voice)
    ends = starts[1:] + [speech_end]

    # кадры: на каждый раздел — по одному на SHOT секунд
    plan: list[dict[str, Any]] = []
    for i, s in enumerate(sections):
        n = max(1, round((ends[i] - starts[i]) / SHOT))
        for k in range(n):
            a = starts[i] + (ends[i] - starts[i]) * k / n
            plan.append({"start": round(a, 2), "section": i})
    subs: list[dict[str, Any]] = []
    for i, s in enumerate(sections):
        subs += subtitles(s["text"], starts[i], spans[i]["speech"])

    await _progress(30, "Качаю кадры из фильма")
    backdrops, _ = await tmdb.images(tmdb_key, film, limit=30)
    urls = backdrops + [u for u in await _stills(fetch, film) if u not in backdrops]
    photos = await tmdb.people(tmdb_key, film)
    images: list[str] = []
    portraits: dict[str, str] = {}
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        need = min(len(plan) + 2, 30)
        for url in urls:
            if len(images) >= need:
                break
            try:
                img = await _download(session, url)
            except Exception:  # noqa: BLE001
                continue
            path = work / f"rimg{len(images)}.jpg"
            await asyncio.to_thread(lambda im=img, pt=path: _frame_image(im).save(pt, quality=90))
            images.append(path.name)
        # кто упомянут в субтитрах — его фото (TMDB, запасной — Кинопоиск)
        for sub in subs:
            person = _mentioned_person(sub["text"], film)
            if not person or str(person.get("name")) in portraits:
                continue
            try:
                img = await _download_first(session, [photos.get(str(person.get("enName") or "").lower()),
                                                      person.get("photo")])
            except Exception:  # noqa: BLE001
                continue
            path = work / f"rperson{len(portraits)}.jpg"
            await asyncio.to_thread(lambda im=img, pt=path: _frame_image(im).save(pt, quality=90))
            portraits[str(person["name"])] = path.name
        try:
            poster = await _download(session, _poster_url(film))
        except Exception:  # noqa: BLE001
            poster = None
    if not images and poster is not None:
        path = work / "rimg0.jpg"
        _frame_image(poster).save(path, quality=90)
        images.append(path.name)
    if not images:
        raise RuntimeError(f"Не скачались кадры для «{t}»")

    video_note = ""
    clip_files: list[str] = []
    if trailer_clips:
        await _progress(40, "Режу фрагменты трейлера")
        clip_files, video_note = await _trailer_clips(film, work, min(MAX_CLIPS, len(plan) // 2))

    # раскладка: портрет, если в эту секунду говорят о человеке; иначе через раз трейлер и кадр
    shots: list[dict[str, Any]] = []
    img_k = clip_k = 0
    recent: list[str] = []
    for k, sh in enumerate(plan):
        mid = sh["start"] + SHOT / 2
        sub = next((s for s in subs if s["start"] <= mid < s["end"]), None)
        person = _mentioned_person(sub["text"], film) if sub else None
        shot = {"start": sh["start"], "end": plan[k + 1]["start"] if k + 1 < len(plan) else speech_end}
        portrait = portraits.get(str(person.get("name"))) if person else None
        if portrait and portrait not in recent:
            shot["image"] = portrait
        else:
            shot["image"] = images[img_k % len(images)]
            img_k += 1
            if clip_files and k > 0 and k % 2 == 1:
                shot["clip"] = clip_files[clip_k % len(clip_files)]
                clip_k += 1
        recent = (recent + [shot["image"]])[-3:]
        shots.append(shot)

    headings = [{"start": starts[i] + 0.3, "end": starts[i] + 4.0, "text": s["heading"].upper()}
                for i, s in enumerate(sections) if s.get("heading") and i > 0]
    r = film.get("rating") or {}
    ratings = []
    if r.get("kp"):
        ratings.append(["КП", f"{float(r['kp']):.1f}", [255, 102, 0]])
    if r.get("imdb"):
        ratings.append(["IMDb", f"{float(r['imdb']):.1f}", [245, 197, 24]])
    directors = [p["name"] for p in _people(film, "director", 1)]
    line = " • ".join(x for x in [str(film.get("year") or ""), ", ".join(_genres(film)[:2]),
                                  f"реж. {directors[0]}" if directors else ""] if x)
    cfg = {
        "output": "review.mp4",
        "voice": voice.name,
        "music": str(music or ASSETS / "music.mp3"),
        "music_db": -20 if voiced else -8,
        "logo": str(ASSETS / "logo.png"),
        "fonts": {"head": str(ASSETS / "Oswald.ttf"), "sans": str(ASSETS / "Montserrat.ttf")},
        "shots": shots,
        "subs": subs,
        "headings": headings,
        "title": {"end": min(TITLE_SECONDS, ends[0]), "name": t, "line": line, "ratings": ratings},
        "outro_bg": images[0],
        "sfx": [[max(starts[i] - 0.15, 0), str(ASSETS / "whoosh.wav"), -10] for i in range(1, len(sections))],
    }
    cfg_path = work / "review.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")

    await _progress(45, "Монтаж видео")
    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "shorts.make_review", str(cfg_path),
                                                stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, cwd=ROOT.parent)
    errors = asyncio.create_task(proc.stderr.read())
    try:
        async for raw in proc.stdout:
            m = re.match(rb"PROGRESS ([\d.]+)", raw)
            if m:
                await _progress(45 + round(53 * float(m.group(1))), "Монтаж видео")
        await proc.wait()
    except asyncio.CancelledError:
        proc.kill()
        raise
    err = await errors
    if proc.returncode:
        raise RuntimeError(f"монтаж обзора упал: {err.decode(errors='ignore')[-800:]}")

    best = max((float(r.get(k) or 0), name) for k, name in (("kp", "КП"), ("imdb", "IMDb")))
    thumb_src = Image.open(work / images[0]).convert("RGB")
    thumb = await asyncio.to_thread(make_thumbnail, thumb_src, t, f"{best[1]} {best[0]:.1f}" if best[0] else "")
    cover_path = work / "cover.jpg"
    thumb.save(cover_path, quality=92)

    chapters = [(starts[i], s.get("heading") or "") for i, s in enumerate(sections)]
    theme = Theme("review", "ОБЗОР", t.upper(), f"Обзор «{t}»", f"Обзор «{t}»", [])
    result = ShortResult(work / "review.mp4", cover_path, theme, [film], build_review_seo(film, script, chapters))
    notes = []
    if video_note:
        notes.append(f"🎞 Фрагменты трейлера не вставлены: {video_note}.")
    if not voiced or daily.tts_note:
        notes.append(daily.no_voice_note(voiced))
    if notes:
        result.seo["warning"] = "\n".join(notes)
    return result

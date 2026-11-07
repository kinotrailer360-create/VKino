"""Шортс «история персонажа» (лор): кто такой герой и откуда он взялся — по статье Википедии.

Шаги (бот проводит по ним кнопками):
  1. find_article — статья о персонаже в русской Википедии (текст и картинки);
  2. write_script — ИИ пишет текст на ~60 с, только по фактам статьи; автор проверяет и правит;
  3. make_lore — голос, свои картинки автора (страницы комиксов, кадры) и картинки статьи,
     субтитры по слову, рендер (shorts/make_lore.py), обложка и SEO.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

import aiohttp
from PIL import Image, ImageEnhance, ImageOps

from . import ai, clips, daily, pronounce, tmdb
from . import music as musiclib
from .cards import make_cover
from .daily import (
    ASSETS,
    ROOT,
    TELEGRAM_URL,
    ShortResult,
    Theme,
    _progress,
)
from .wiki import USER_AGENT

log = logging.getLogger("shorts")

WIKI_API = "https://ru.wikipedia.org/w/api.php"
MAX_IMAGES = 24
SKIP_IMAGE = re.compile(r"(logo|icon|flag|symbol|signature|wiki|commons|edit|question|stub|\.svg$)", re.I)


async def _wiki(session: aiohttp.ClientSession, **params: Any) -> dict[str, Any]:
    async with session.get(WIKI_API, params={"format": "json", "formatversion": 2, **params}) as resp:
        resp.raise_for_status()
        return await resp.json(content_type=None)


async def find_article(name: str) -> dict[str, Any]:
    """Статья о персонаже: {"title", "url", "text", "images": [url]}. Ищем «имя персонаж», затем просто имя."""
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as session:
        hits: list[dict[str, Any]] = []
        for query in (f"{name} персонаж", name):
            data = await _wiki(session, action="query", list="search", srsearch=query, srlimit=5)
            hits = (data.get("query") or {}).get("search") or []
            if hits:
                break
        if not hits:
            raise RuntimeError(f"в Википедии не нашлось статьи про «{name}»")
        title = hits[0]["title"]
        data = await _wiki(session, action="query", prop="extracts|images", explaintext=1, titles=title,
                           redirects=1, imlimit=50)
        page = ((data.get("query") or {}).get("pages") or [{}])[0]
        text = str(page.get("extract") or "")
        files = [i["title"] for i in page.get("images") or [] if not SKIP_IMAGE.search(i.get("title", ""))]
        images: list[str] = []
        if files:
            info = await _wiki(session, action="query", prop="imageinfo", iiprop="url|size", iiurlwidth=1280,
                               titles="|".join(files[:40]))
            for pg in (info.get("query") or {}).get("pages") or []:
                ii = (pg.get("imageinfo") or [{}])[0]
                if (ii.get("width") or 0) >= 300 and (ii.get("thumburl") or ii.get("url")):
                    images.append(str(ii.get("thumburl") or ii["url"]))
    if len(text) < 300:
        raise RuntimeError(f"статья «{title}» слишком короткая — уточни имя (например «Доктор Дум (Marvel)»)")
    return {"title": title, "url": f"https://ru.wikipedia.org/wiki/{title.replace(' ', '_')}", "text": text,
            "images": images}


async def write_script(name: str, hook: str, key: str) -> dict[str, Any]:
    article = await find_article(name)
    script = await ai.write_lore(key, name, article["text"], hook)
    script.update({"name": name, "hook": hook, "article": article["title"], "url": article["url"],
                   "images": article["images"]})
    return script


async def news_images(tmdb_key: str, person: str, person_en: str, films: list[str]) -> tuple[list[str], list[str]]:
    """Фото к новости: портреты человека и кадры фильмов из текста (TMDB), иначе — картинки Википедии.
    Возвращает (все картинки, портреты главного человека)."""
    urls: list[str] = []
    portraits: list[str] = []
    try:
        info = await tmdb.person_by_name(tmdb_key, person_en or person)
    except Exception as exc:  # noqa: BLE001
        log.info("shorts: TMDB person %s failed: %s", person_en or person, exc)
        info = None
    if info:
        portraits = tmdb.profile_urls(info)
        urls += portraits
        works = [("movie", c) for c in (info.get("movie_credits") or {}).get("cast") or []] + \
                [("tv", c) for c in (info.get("tv_credits") or {}).get("cast") or []]
        wanted = [tmdb._norm(f) for f in films if tmdb._norm(f)]

        def mentioned(c: dict[str, Any]) -> bool:
            names = {tmdb._norm(c.get(k)) for k in ("title", "original_title", "name", "original_name")}
            return any(w and any(w in n or (n and n in w) for n in names if n) for w in wanted)

        picked = [(k, c) for k, c in works if mentioned(c)]
        picked += [("movie", c) for c in tmdb.best_movies(info, 6) if all(c.get("id") != p.get("id") for _, p in picked)]
        seen: set[tuple[str, int]] = set()
        for kind, c in picked:
            if not c.get("id") or (kind, int(c["id"])) in seen or len(seen) >= 8:
                continue
            seen.add((kind, int(c["id"])))
            try:
                urls += await tmdb.backdrops(tmdb_key, kind, int(c["id"]), 2)
            except Exception as exc:  # noqa: BLE001
                log.info("shorts: TMDB backdrops %s failed: %s", c.get("id"), exc)
    if len(urls) < 8 and person:
        try:
            urls += (await find_article(person))["images"]
        except Exception as exc:  # noqa: BLE001
            log.info("shorts: wiki images for %s failed: %s", person, exc)
    out: list[str] = []
    for u in urls:
        if u not in out:
            out.append(u)
    return out[:MAX_IMAGES], portraits


async def news_fixed(tmdb_key: str, story: dict[str, Any], portraits: list[str]) -> list[dict[str, Any]]:
    """Картинки, привязанные к фразам: фото того, кого цитируют; логотип студии к её заявлению;
    в прощальном ролике — чёрно-белый портрет на «Светлая память»."""
    fixed: list[dict[str, Any]] = []
    for v in story.get("visuals") or []:
        try:
            if v.get("brand"):
                url = await tmdb.company_logo(tmdb_key, v["brand"])
                first = v["brand"].split()[0]
                keys = [v["brand"].lower(), first.lower(), pronounce.DEFAULT.get(first, "").lower()]
                mode = "logo"
            else:
                info = await tmdb.person_by_name(tmdb_key, v.get("person_en") or v.get("person_ru") or "")
                url = (tmdb.profile_urls(info) or [None])[0] if info else None
                keys = [w.lower() for w in (v.get("person_ru") or "").split()[-1:] + (v.get("person_en") or "").split()[-1:]]
                mode = "photo"
        except Exception as exc:  # noqa: BLE001
            log.info("shorts: news visual %s failed: %s", v, exc)
            continue
        if url:
            fixed.append({"line": v["line"], "keys": [k for k in keys if len(k) >= 3], "url": url, "mode": mode,
                          "who": v.get("brand") or v.get("person_ru") or v.get("person_en")})
    if portraits and is_mourning({"title": story.get("title"), "lines": story["lines"][-1:]}):
        fixed.append({"line": len(story["lines"]), "keys": ["светлая память"], "url": portraits[min(1, len(portraits) - 1)],
                      "mode": "bw", "who": f"{story.get('person') or 'актёр'}, чёрно-белое фото"})
    return fixed


async def write_news(text: str, key: str, tmdb_key: str) -> dict[str, Any]:
    """Шортс «своя новость»: текст автора → закадровый текст, фото человека и кадры фильмов из новости."""
    story = await ai.write_news_story(key, text)
    name = story["person"] or story["title"] or "Новость"
    images, portraits = await news_images(tmdb_key, story["person"], story["person_en"], story["films"])
    fixed = await news_fixed(tmdb_key, story, portraits)
    return {"kind": "news", "fixed": fixed, "name": name, "en": story["person_en"], "title": story["title"],
            "lines": story["lines"], "hashtags": story["hashtags"], "films": story["films"],
            "article": "текст автора", "url": "", "images": images}


def is_mourning(script: dict[str, Any]) -> bool:
    from .music import MOURNING
    text = " ".join([script.get("title") or ""] + list(script.get("lines") or [])).lower()
    return any(w in text for w in MOURNING)


def build_news_seo(script: dict[str, Any]) -> dict[str, str]:
    title = script.get("title") or script["name"]
    title = (title if len(title) <= 88 else title[:85] + "…") + " #shorts"
    mourning = is_mourning(script)
    tags_line = " ".join(script.get("hashtags") or [])
    desc = [" ".join(script["lines"][:3])[:400], ""]
    if tags_line:
        desc += [tags_line, ""]
    desc += ["Светлая память. Какая роль запомнилась вам больше всего? Пишите в комментариях 👇" if mourning
             else "Что думаешь? Пиши в комментариях 👇", "",
             f"✈️ Telegram: {TELEGRAM_URL} — новости кино каждый день"]
    tags: list[str] = []
    name, en = script["name"], script.get("en") or ""
    for t in [name.lower(), en.lower(), *[t.lstrip("#").lower() for t in script.get("hashtags") or []],
              *[f.lower() for f in script.get("films") or []], "новости кино", "кино новости",
              "KinoMir360", "киномир360"]:
        if t and t not in tags and len(", ".join(tags + [t])) <= 480:
            tags.append(t)
    pinned = (f"Светлая память 🕯 Какая роль для вас главная?\nВсе новости — в Telegram {TELEGRAM_URL}"
              if mourning else f"Что думаешь об этой новости? 👇\nВсе новости — в Telegram {TELEGRAM_URL}")
    return {"title": title, "description": "\n".join(desc), "tags": ", ".join(tags), "pinned": pinned}


def script_text(script: dict[str, Any]) -> str:
    return "\n".join(f"{i}. {line}" for i, line in enumerate(script["lines"], 1))


def parse_lines(text: str) -> list[str]:
    """Текст автора: фраза на строку (номера «1.» можно оставить); пустые строки пропускаем."""
    lines = [re.sub(r"^\s*\d+[.)]\s*", "", ln).strip() for ln in text.splitlines()]
    return [ln for ln in lines if ln]


def build_lore_seo(script: dict[str, Any]) -> dict[str, str]:
    name = script["name"]
    title = script.get("title") or f"Кто такой {name}? История персонажа"
    title = (title if len(title) <= 88 else title[:85] + "…") + " #shorts"
    desc = [f"{name} — история персонажа за минуту 🎬", ""]
    desc += [" ".join(script["lines"][:2])[:300], "",
             "А ты знал его историю? Пиши в комментариях 👇", "",
             f"✈️ Telegram: {TELEGRAM_URL} — новинки, трейлеры и подборки каждый день", "",
             f"Источник: {script.get('url', 'Википедия')}"]
    tags: list[str] = []
    en = script.get("en") or ""
    for t in [name.lower(), f"{name.lower()} история", f"кто такой {name.lower()}", "история персонажа",
              "комиксы", "лор", en.lower(), f"{en.lower()} origin" if en else "", "marvel", "dc",
              "KinoMir360", "киномир360"]:
        if t and t not in tags and len(", ".join(tags + [t])) <= 480:
            tags.append(t)
    pinned = f"Про кого из героев рассказать дальше? Пиши в комментариях 👇\nВсе ролики — в Telegram {TELEGRAM_URL}"
    return {"title": title, "description": "\n".join(desc), "tags": ", ".join(tags), "pinned": pinned}


def _logo_card(logo: Image.Image) -> Image.Image:
    """Логотип (PNG с прозрачностью) по центру тёмного кадра."""
    card = Image.new("RGB", (1080, 1350), (16, 16, 20))
    logo = logo.convert("RGBA")
    logo.thumbnail((820, 520))
    card.paste(logo, ((1080 - logo.width) // 2, (1350 - logo.height) // 2), logo)
    return card


def _black_white(img: Image.Image) -> Image.Image:
    return ImageEnhance.Contrast(ImageOps.grayscale(img).convert("RGB")).enhance(1.15)


def fixed_lines(script: dict[str, Any], lines: list[str]) -> dict[int, dict[str, Any]]:
    """К какой фразе какая привязанная картинка: по имени/названию в тексте фразы, иначе по номеру
    (автор мог поправить текст — номера тогда сдвигаются, а имена остаются)."""
    out: dict[int, dict[str, Any]] = {}
    for item in script.get("fixed") or []:
        low = [ln.lower() for ln in lines]
        hit = next((i for i, ln in enumerate(low) if i not in out and any(k in ln for k in item.get("keys") or [])), None)
        if hit is None:
            n = int(item.get("line") or 0) - 1
            hit = n if 0 <= n < len(lines) and n not in out else None
        if hit is not None:
            out[hit] = item
    return out


def _frame_ready(img: Image.Image, limit: int = 2400) -> Image.Image:
    """Уменьшить огромные сканы (рендеру хватит ~2400 px по длинной стороне)."""
    img = img.convert("RGB")
    if max(img.size) > limit:
        img.thumbnail((limit, limit), Image.LANCZOS)
    return img


CLIP_MAX = 3.5  # сек: отрезок трейлера на фразу — коротко (меньше риска Content ID), дальше кадр


async def _trailer_url(film: dict[str, Any]) -> str | None:
    """Официальный трейлер с YouTube по карточке TMDB (точно этот фильм), иначе — Rutube по названию и году."""
    try:
        url = await tmdb.youtube_trailer(os.getenv("TMDB_API_KEY", "").strip(), film.get("kind") or "movie",
                                         int(film["id"]))
        if url:
            return url
    except Exception as exc:  # noqa: BLE001
        log.info("shorts: tmdb trailer failed: %s", exc)
    try:
        found = await clips.rutube_trailer([n for n in [film.get("title"), *(film.get("names") or [])] if n],
                                           int(film["year"]) if str(film.get("year") or "").isdigit() else None)
        return found["url"] if found else None
    except Exception as exc:  # noqa: BLE001
        log.info("shorts: rutube trailer failed: %s", exc)
        return None


def _bright(path: Path) -> bool:
    """Кадр не тёмный: не затемнение и не белые титры на чёрном (почти весь кадр — тёмные точки)."""
    try:
        px = list(Image.open(path).convert("L").resize((64, 36)).getdata())
    except Exception:  # noqa: BLE001
        return True
    return sum(px) / len(px) > 28 and sum(v < 25 for v in px) / len(px) < 0.75


async def _cut_trailer(work: Path, at: float, src: Path, stem: str, length: float,
                       full: bool = False) -> tuple[str, float] | None:
    """Отрезок трейлера с секунды at, 1080×1920 без звука. full — на весь экран (динамичный монтаж), иначе кадр
    почти целиком по центру, сверху и снизу его размытая копия."""
    length = round(length, 2)
    name = f"{stem}.mp4"
    vf = ("[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30" if full else
          "[0:v]split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
          "boxblur=20:2,eq=brightness=-0.18[bg];[b]scale=1080:860:force_original_aspect_ratio=increase,"
          "crop=1080:860[fg];[bg][fg]overlay=0:(H-h)/2,fps=30")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-y", "-ss", f"{max(0.0, at):.2f}", "-i", str(src),
        "-t", f"{length:.2f}", "-an", "-filter_complex", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-threads", "2", str(work / name),
        stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode:
        log.info("shorts: trailer cut %s failed: %s", stem, err.decode(errors="ignore")[-200:])
        return None
    return name, length


SEG = 1.8  # динамичный монтаж: новый момент трейлера каждые ~1,8 с


async def _trailer_clips(work: Path, films: list[dict[str, Any]], lines: list[str], durs: list[float], skip: set[int],
                         gemini_key: str, repeats: set[int] | None = None,
                         stills_out: dict[int, str] | None = None,
                         more_out: dict[int, list[tuple[str, float]]] | None = None,
                         dynamic: bool = False) -> tuple[dict[int, tuple[str, float]], str]:
    """Трейлер фильма → отрезки до CLIP_MAX под фразы: ИИ смотрит кадры трейлера и выбирает момент к фразе;
    фразы без подходящего момента остаются с фото — ролик комбинированный (видео + фото).
    dynamic — как в динамичных шортсах: видео на весь экран, у каждой фразы (кроме своих картинок автора)
    отрезки, новый момент каждые ~1,8 с. ({фраза: (файл, длина)}, пояснение автору)."""
    tdir = work / "trailer"
    tdir.mkdir(exist_ok=True)
    want = [i for i in range(len(lines)) if i not in skip]
    seg = SEG if dynamic else CLIP_MAX
    try:
        # несколько фильмов (пересказ франшизы) — трейлер каждого, кадры для ИИ из всех вместе
        frames: list[tuple[float, Path, Path]] = []  # (секунда, миниатюра, файл трейлера)
        per = max(10, (60 if dynamic else 30) // max(1, len(films)))
        for n, film in enumerate(films[:3]):
            url = await _trailer_url(film)
            if not url:
                continue
            fdir = tdir / f"t{n}"
            fdir.mkdir(exist_ok=True)
            try:
                src_n = await clips.download_full(url, fdir)
                total = await daily._duration(src_n)
                frames += [(t, path, src_n) for t, path in
                           await clips.sample_frames(src_n, fdir, duration=total, limit=per,
                                                     every=2.0 if dynamic else 4.0)]
            except Exception as exc:  # noqa: BLE001 — один трейлер не скачался — берём остальные
                log.info("shorts: trailer of %s failed: %s", film.get("title"), exc)
        if not frames:
            return {}, "🎞 Трейлер фильма не нашёлся или не скачался — в ролике кадры и постеры."
        # тёмные кадры (затемнение, титры «14 октября» на чёрном) ИИ не показываем — в ролике это чёрный экран
        frames = [f for f in frames if _bright(f[1])] or frames
        thumbs = [Image.open(path).convert("RGB") for _, path, _ in frames]
        choices = await ai.match_images(gemini_key, [lines[i] for i in want], thumbs)
        used: set[int] = set()
        if choices:
            # комбинированный ролик: видео — там, где в трейлере есть момент под фразу, остальное — фото
            order = [c if 0 <= c < len(frames) else -1 for c in choices]
        else:
            # ИИ не ответил — чередуем: фраза с отрезком, фраза с фото (моменты — по ходу трейлера)
            step = max(1, len(frames) // max(1, (len(want) + 1) // 2))
            order = [(k // 2) * step % len(frames) if k % 2 == 0 else -1 for k in range(len(want))]
        for k, c in enumerate(order):
            if c in used:
                order[k] = -1
            elif c >= 0:
                used.add(c)
        if dynamic:  # фраза без своего момента — всё равно видео: свободный момент трейлера по порядку
            for k, c in enumerate(order):
                spare = [j for j in range(len(frames)) if j not in used]
                if c < 0 and spare:
                    order[k] = spare[(k * len(spare)) // max(1, len(order))]
                    used.add(order[k])

        def spare_near(c: int) -> int | None:
            spare = [k for k in range(len(frames)) if k not in used]
            if not spare:
                return None
            return min(spare, key=lambda x: (frames[x][2] != frames[c][2], abs(frames[x][0] - frames[c][0])))

        out: dict[int, tuple[str, float]] = {}
        more: dict[int, list[tuple[str, float]]] = {}  # длинная фраза: следующие отрезки после первого
        for i, c in zip(want, order):
            if c < 0:
                continue  # у этой фразы остаётся фото
            # кадр ИИ — в середине отрезка; начало чуть раньше, но не больше чем на 0,4 с
            at, src = frames[c][0] - 0.4, frames[c][2]
            got = await _cut_trailer(work, at, src, f"tclip{i}", min(seg, max(1.2, durs[i])), dynamic)
            if not got:
                continue
            out[i] = got
            # фраза длиннее отрезка — следующие моменты трейлера, а не стоп-кадр
            left, last_at, last_len = durs[i] - got[1], at, got[1]
            while left > (0.6 if dynamic else 1.5) and len(more.get(i, [])) < (8 if dynamic else 3):
                k = spare_near(c)
                if k is not None:
                    used.add(k)
                    at2, src2 = frames[k][0] - 0.4, frames[k][2]
                else:  # моменты кончились — продолжаем тот же кадр трейлера
                    at2, src2 = last_at + last_len, src
                nxt = await _cut_trailer(work, at2, src2, f"tclip{i}_{len(more.get(i, [])) + 1}",
                                         min(seg, max(1.0, left)), dynamic)
                if not nxt:
                    break
                more.setdefault(i, []).append(nxt)
                left, last_at, last_len = left - nxt[1], at2, nxt[1]
        if more_out is not None:
            more_out.update(more)
        # фразы, где фото повторилось бы (кадров у фильма мало), — стоп-кадр из ещё не взятого момента трейлера
        if stills_out is not None:
            spare = [c for c in range(len(frames)) if c not in used]
            for i in sorted((repeats or set()) - set(out)):
                if not spare:
                    break
                c = spare.pop(len(spare) // 2)
                name = f"tstill{i}.jpg"
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-v", "error", "-y", "-ss", f"{frames[c][0]:.2f}", "-i", str(frames[c][2]), "-frames:v", "1",
                    "-vf", "scale=1600:-2", "-q:v", "3",  # широкий стоп-кадр — рендер покажет его целиком
                    str(work / name), stderr=asyncio.subprocess.PIPE)
                await proc.communicate()
                if proc.returncode == 0 and (work / name).exists():
                    stills_out[i] = name
        return out, "" if out else "🎞 Трейлер скачался, но отрезки не вырезались — в ролике кадры."
    except Exception as exc:  # noqa: BLE001 — без трейлера ролик всё равно собирается из кадров
        log.warning("shorts: own script trailer failed: %s", exc)
        return {}, f"🎞 Трейлер не скачался ({type(exc).__name__}) — в ролике кадры и постеры."
    finally:
        import shutil
        shutil.rmtree(tdir, ignore_errors=True)


async def _hero_clips(work: Path, heroes: list[dict[str, Any]], lines: list[str], durs: list[float],
                      gemini_key: str) -> dict[int, list[tuple[str, float]]]:
    """Названный во фразе герой из другого фильма (Дум, Циклоп, Баки) → отрезки трейлера ЭТОГО фильма к фразе:
    кто назван, тот и на экране. {фраза: [(файл, длина), ...]}; не нашёлся трейлер — фраза остаётся как была."""
    key = os.getenv("TMDB_API_KEY", "").strip()
    out: dict[int, list[tuple[str, float]]] = {}
    for n, hero in enumerate(heroes[:4]):
        i = int(hero["line"])
        hdir = work / f"hero{n}"
        hdir.mkdir(exist_ok=True)
        try:
            found = []
            for q in [hero.get("ru"), hero.get("en")]:
                if q and not found:
                    found = await tmdb.search_movie(key, str(q))
            if hero.get("year"):
                found = sorted(found, key=lambda m: abs(int(m["year"] or 0) - int(hero["year"])))
            if not found:
                continue
            meta = {**found[0], "kind": "movie"}
            url = await _trailer_url(meta)
            if not url:
                continue
            src = await clips.download_full(url, hdir)
            total = await daily._duration(src)
            frames = [f for f in await clips.sample_frames(src, hdir, duration=total, limit=16, every=3.0)
                      if _bright(f[1])]
            if not frames:
                continue
            choice = await ai.match_images(gemini_key, [lines[i]], [Image.open(f[1]).convert("RGB") for f in frames])
            c = choice[0] if choice and 0 <= choice[0] < len(frames) else len(frames) // 2
            order = sorted(range(len(frames)), key=lambda k: abs(frames[k][0] - frames[c][0]))
            left, segs = durs[i], []
            for k in order:
                if left <= 0.6 or len(segs) >= 5:
                    break
                got = await _cut_trailer(work, frames[k][0] - 0.4, src, f"hclip{i}_{len(segs)}",
                                         min(SEG, max(1.0, left)), True)
                if got:
                    segs.append(got)
                    left -= got[1]
            if segs:
                out[i] = segs
                log.info("shorts: hero line %d → %s", i + 1, meta.get("title"))
        except Exception as exc:  # noqa: BLE001 — без кадров героя фраза остаётся с кадрами главного фильма
            log.info("shorts: hero clip %s failed: %s", hero, exc)
        finally:
            import shutil
            shutil.rmtree(hdir, ignore_errors=True)
    return out


def recap_title(titles: list[str], lines: list[str]) -> str:
    """Пересказ франшизы («Вспоминаем, чем закончились „Дюна“ и „Дюна: Часть вторая“») — название про пересказ,
    а не «„Дюна“ (2021) — что нужно знать»: «„Дюна“: что было в прошлых частях — перед третьей частью»."""
    text = " ".join(lines).lower().replace("ё", "е")
    titles = [t for t in titles if t]
    if len(titles) < 2 and not re.search(r"вспомина|пересказ|чем законч|что было в", text):
        return ""
    if not titles:
        return ""
    base = titles[0]
    for t in titles[1:]:  # общее начало названий: «Дюна» и «Дюна: Часть вторая» → «Дюна»
        while base and not t.lower().startswith(base.lower()):
            base = base[:-1]
    base = re.sub(r"[\s:.,—–-]+$", "", base)
    if not base or (len(base) < len(titles[0]) and titles[0][len(base)].isalnum()):
        base = titles[0]  # «Чужой» и «Чужие» → не «Чуж», а название первого фильма
    m = re.search(r"\b(втор|трет|четверт|пят|шест|седьм)ь?(?:ой|ей)\s+част", text)
    nxt = f" — перед {m.group(1)}{'ьей' if m.group(1) == 'трет' else 'ой'} частью" if m else ""
    head = "что было в прошлых частях" if len(titles) > 1 or m else "что было раньше"
    return f"«{base}»: {head}{nxt}"


async def _score_card(poster_url: str, value: str, out: Path) -> Path:
    """Финальная карточка рецензии: постер на размытом фоне и «МОЯ ОЦЕНКА 8/10»."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), headers={"User-Agent": USER_AGENT}) as s:
        async with s.get(poster_url) as resp:
            resp.raise_for_status()
            raw = await resp.read()

    def draw() -> None:
        from PIL import ImageDraw, ImageFilter

        from .cards import _font
        poster = Image.open(io.BytesIO(raw)).convert("RGB")
        w, h = 1080, 1920
        bg = ImageOps.fit(poster, (w, h)).filter(ImageFilter.GaussianBlur(40))
        bg = ImageEnhance.Brightness(bg).enhance(0.45)
        pw = 640
        p = poster.resize((pw, round(poster.height * pw / poster.width)))
        if p.height > 960:
            p = p.crop((0, 0, pw, 960))
        bg.paste(p, ((w - pw) // 2, 330))
        d = ImageDraw.Draw(bg)
        y = 330 + p.height + 70
        d.text((w // 2, y), "МОЯ ОЦЕНКА", font=_font("Montserrat.ttf", 56, "Bold"), fill="white", anchor="mm")
        d.text((w // 2, y + 150), f"{value}/10", font=_font("Oswald.ttf", 190, "Bold"), fill=(255, 196, 61),
               anchor="mm", stroke_width=4, stroke_fill=(0, 0, 0))
        bg.save(out, quality=93)

    await asyncio.to_thread(draw)
    return out


async def make_lore(work: Path, script: dict[str, Any], music: Path | None = None,
                    user_media: list[Path] | None = None, gemini_key: str = "",
                    user_pins: dict[int, int] | None = None) -> ShortResult:
    """user_pins — своя картинка (номер в user_media) -> фраза (с 1), к которой автор её подписал."""
    lines = script["lines"]
    news = script.get("kind") == "news"
    # динамичный монтаж: видео на весь экран, новый кадр каждые ~1,8 с, переходы, субтитры по слову, без заставки
    dynamic = script.get("style") == "dynamic"
    mourning = news and is_mourning(script)
    dynamic = dynamic and not mourning  # прощальный ролик — всегда спокойный
    await _progress(5, "Озвучка")
    # в прощальном ролике «подписывайся» в конце неуместно
    # в ролике уже есть призыв в телеграм (рубрика «Что мы знаем») — в конце не дописываем «подпишись»:
    # он обрывается на самой сильной фразе
    has_cta = dynamic and any("телеграм" in ln.lower() for ln in lines)
    spoken = lines if mourning or has_cta else lines[:-1] + [daily.with_outro(lines[-1])]
    voice, starts, voiced, spans = await daily.synthesize(spoken, work)
    await _progress(30, "Субтитры по слову")
    words = await daily.word_timeline(voice, starts, spans) if voiced else []

    await _progress(40, "Картинки")
    pool: list[tuple[str, Image.Image]] = []  # (файл, миниатюра для ИИ)
    user_at: dict[int, int] = {}  # номер своей картинки -> её место в pool
    for n, path in enumerate(user_media or []):
        try:
            img = _frame_ready(Image.open(path))
        except Exception as exc:  # noqa: BLE001
            log.warning("shorts: lore user image %s skipped: %s", path.name, exc)
            continue
        out = work / f"luser{n}.jpg"
        img.save(out, quality=90)
        img.thumbnail((480, 480))
        user_at[n] = len(pool)
        pool.append((out.name, img))
    own = len(pool)
    if len(pool) < len(lines):  # своих не хватает — добираем картинками из статьи
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as session:
            for url in script.get("images") or []:
                if len(pool) >= max(len(lines), own) or len(pool) >= MAX_IMAGES:
                    break
                try:
                    async with session.get(url) as resp:
                        resp.raise_for_status()
                        img = _frame_ready(Image.open(io.BytesIO(await resp.read())))
                except Exception:  # noqa: BLE001, S112 — битую картинку пропускаем
                    continue
                out = work / f"lwiki{len(pool)}.jpg"
                img.save(out, quality=90)
                img.thumbnail((480, 480))
                pool.append((out.name, img))
    general = len(pool)
    pinned: dict[int, int] = {}  # фраза -> картинка, привязанная к ней (кто говорит, логотип, памятный портрет)
    fixed = fixed_lines(script, lines)
    if fixed:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as session:
            for i, item in fixed.items():
                try:
                    async with session.get(item["url"]) as resp:
                        resp.raise_for_status()
                        raw = Image.open(io.BytesIO(await resp.read()))
                        raw.load()
                except Exception as exc:  # noqa: BLE001
                    log.info("shorts: fixed image %s failed: %s", item["url"], exc)
                    continue
                img = _logo_card(raw) if item["mode"] == "logo" else _frame_ready(raw)
                if item["mode"] == "bw":
                    img = _black_white(img)
                out = work / f"lfix{i}.jpg"
                img.save(out, quality=92)
                img.thumbnail((480, 480))
                pinned[i] = len(pool)
                pool.append((out.name, img))
    for n, line in (user_pins or {}).items():  # подпись автора важнее кадра, найденного ботом
        if n in user_at and 1 <= line <= len(lines):
            pinned[line - 1] = user_at[n]
    if not pool:
        raise RuntimeError("нет ни одной картинки: пришли свои (страницы комиксов, кадры) кнопкой «📎»")

    # какую картинку на какую фразу: привязанные — как есть; остальные ИИ по смыслу, иначе по порядку
    free_lines = [i for i in range(len(lines)) if i not in pinned]
    picked = await ai.match_images(gemini_key, [lines[i] for i in free_lines],
                                   [im for _, im in pool[:general]]) if gemini_key and general else None
    choices = [-1] * len(lines)
    for k, i in enumerate(free_lines):
        if picked and k < len(picked):
            choices[i] = picked[k]
    for i, c in pinned.items():
        choices[i] = c
    used: set[int] = set(pinned.values())
    order: list[int] = []
    nxt = 0
    for i in range(len(lines)):
        if i in pinned:
            order.append(pinned[i])
            continue
        c = choices[i] if choices and i < len(choices) else -1
        if c < 0 or c in used:
            free = [j for j in range(len(pool)) if j not in used]
            c = free[0] if free else nxt % (general or len(pool))
            nxt += 1
        used.add(c)
        order.append(c)
    slides = [{"start": starts[i], "image": pool[order[i]][0]} for i in range(len(lines))]
    durs = [((starts[i + 1] if i + 1 < len(starts) else starts[i] + float(spans[i]["speech"]) + 0.3) - starts[i])
            for i in range(len(lines))]
    clip_notes: list[str] = []
    score = script.get("score") if news else None
    tfilm = script.get("trailer_film") if news else None
    if score and tfilm and tfilm.get("poster"):  # рецензия: карточка «МОЯ ОЦЕНКА 8/10» на фразе с оценкой
        try:
            card = await _score_card(tfilm["poster"], str(score["value"]), work / "score.jpg")
            slides[int(score["line"])]["image"] = card.name
        except Exception as exc:  # noqa: BLE001
            log.info("shorts: score card failed: %s", exc)
    if tfilm and gemini_key:
        # отрезки трейлера под фразы: кроме фраз со своими картинками автора и с карточкой оценки
        skip = {line - 1 for line in (user_pins or {}).values()} | ({int(score["line"])} if score else set())
        await _progress(44, "Трейлер: подбираю отрезки")
        seen: set[str] = set()
        repeats: set[int] = set()
        for i, sl in enumerate(slides):  # фото, которое уже было выше по ролику, — повтор
            if sl["image"] in seen and i not in skip:
                repeats.add(i)
            seen.add(sl["image"])
        score_line = int(score["line"]) if score else -1
        if score_line >= 0 and durs[score_line] > 6:
            repeats.add(score_line)  # длинная финальная фраза: после карточки оценки — ещё кадр фильма
        heroes: dict[int, list[tuple[str, float]]] = {}
        if dynamic and script.get("kind_own"):
            # «Доктор Дум», «Циклоп», «Баки» — кадры из фильмов, где эти герои есть
            await _progress(43, "Трейлеры героев")
            found = await ai.hero_films(gemini_key, lines, str(tfilm.get("title") or ""))
            heroes = await _hero_clips(work, [h for h in found if h["line"] not in skip], lines, durs, gemini_key)
            skip |= set(heroes)
        stills: dict[int, str] = {}
        more: dict[int, list[tuple[str, float]]] = {}
        got, note = await _trailer_clips(work, script.get("trailer_films") or [tfilm], lines, durs, skip, gemini_key,
                                         repeats, stills, more, dynamic)
        if note:
            clip_notes.append(note)
        extra = []
        for i, name in stills.items():
            if i == score_line:  # карточка оценки — первые 4 с фразы, дальше кадр фильма
                extra.append({"start": round(starts[i] + 4.0, 3), "image": name})
            else:
                slides[i]["image"] = name
        for i, segs in heroes.items():
            got[i] = segs[0]
            more[i] = segs[1:]
        for i, (name, length) in got.items():
            image = slides[i]["image"]
            slides[i] = {"start": starts[i], "clip": name}
            at = starts[i] + length
            for name2, length2 in more.get(i, []):  # длинная фраза: следующие моменты трейлера
                extra.append({"start": round(at, 3), "clip": name2, "hard": True})  # склейка внутри фразы
                at += length2
            if starts[i] + durs[i] > at + 0.8:  # и всё ещё не хватило — кадр, а не стоп-кадр
                extra.append({"start": round(at, 3), "image": image, "hard": True})
        slides = sorted(slides + extra, key=lambda sl: sl["start"])
    labels: list[dict[str, Any]] = []
    if news and gemini_key and script.get("kind_own"):
        texts = await ai.screen_labels(gemini_key, lines, script.get("title") or "")
        labels = [{"start": starts[i], "end": starts[i] + durs[i], "text": t} for i, t in enumerate(texts)
                  if t and not (score and i == int(score["line"]))]  # на карточке оценки надпись уже есть

    cfg = {
        "output": "short.mp4",
        "voice": voice.name,
        "music": str(music or musiclib.pick(script.get("title") or "", " ".join(script.get("lines") or []))
                     or ASSETS / "music.mp3"),
        "music_db": -17 if voiced else -6,
        "logo": str(ASSETS / "logo.png"),
        "fonts": {"head": str(ASSETS / "Oswald.ttf"), "sans": str(ASSETS / "Montserrat.ttf")},
        "slides": slides,
        "labels": labels,
        "outro_image": pool[order[0]][0],
        # свой сценарий: короткая концовка — долгий статичный финал роняет досмотр шортса;
        # динамичный — без заставки вовсе: ролик обрывается на последней фразе и идёт по кругу
        "outro_len": 0 if dynamic else 2.0 if script.get("kind_own") else 3.5,
        "style": "dynamic" if dynamic else "calm",
        "words": words,
        "sfx": [[max(st - 0.1, 0), str(ASSETS / "whoosh.wav"), -12] for st in starts[1:]],
        "outro": {"question": "СВЕТЛАЯ\nПАМЯТЬ" if mourning else "ЧТО\nДУМАЕШЬ?" if news
                  else "ПРО КОГО\nРАССКАЗАТЬ\nДАЛЬШЕ?", "channel": "KinoMir360", "youtube": "@kinomir360",
                  "telegram": "@KinoMir360", "note": "ссылки в описании"},
    }
    cfg_path = work / "lore.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    await _progress(50, "Монтаж видео")
    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "shorts.make_lore", str(cfg_path),
                                                stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, cwd=ROOT.parent)
    errors = asyncio.create_task(proc.stderr.read())
    try:
        async for raw in proc.stdout:
            m = re.match(rb"PROGRESS ([\d.]+)", raw)
            if m:
                await _progress(50 + round(48 * float(m.group(1))), "Монтаж видео")
        await proc.wait()
    except asyncio.CancelledError:
        proc.kill()
        raise
    err = await errors
    if proc.returncode:
        raise RuntimeError(f"монтаж упал: {err.decode(errors='ignore')[-800:]}")

    cover_src = Image.open(work / pool[order[0]][0]).convert("RGB")
    if news:
        tag = "ПАМЯТИ" if mourning else "НОВОСТИ"
        cover = await asyncio.to_thread(make_cover, cover_src, tag, script.get("title") or script["name"])
        theme = Theme("news_own", tag, script["name"].upper(), script.get("title") or script["name"],
                      script.get("title") or script["name"], [])
        seo = build_news_seo(script)
    else:
        cover = await asyncio.to_thread(make_cover, cover_src, "ИСТОРИЯ", f"Кто такой {script['name']}?")
        theme = Theme("lore", "ИСТОРИЯ", script["name"].upper(), f"История: {script['name']}",
                      f"История: {script['name']}", [])
        seo = build_lore_seo(script)
    cover_path = work / "cover.jpg"
    cover.save(cover_path, quality=93)
    film = {"name": script["name"], "en": script.get("en") or ""}
    result = ShortResult(work / "short.mp4", cover_path, theme, [film], seo)
    notes = []
    if own == 0 and news:
        notes.append("🖼 Фото подобраны автоматически (TMDB, Википедия). Хочешь свои — кнопка «📎 Свои картинки».")
    elif own == 0:
        notes.append("🖼 Картинки взяты из статьи Википедии. Со своими страницами комиксов и кадрами "
                     "ролик выйдет ярче — кнопка «📎 Свои картинки».")
    if not voiced or daily.tts_note:
        notes.append(daily.no_voice_note(voiced))
    notes += clip_notes
    if notes:
        result.seo["warning"] = "\n".join(notes)
    return result


# ---------------------------------------------------------------- свой сценарий (озвучка как есть)

TIME_CELL = re.compile(r"^\s*\d{1,3}\s*[–—-]\s*\d{1,3}\s*(?:с|сек)?\.?\s*$")


def split_scripts(text: str) -> list[tuple[str, str]]:
    """Файл с несколькими сценариями («## Shorts 1 — …», «## Shorts 2 — …») → [(название, текст)]. Один
    сценарий — один элемент."""
    parts = re.split(r"^#{1,3}\s+(.+)$", text, flags=re.M)
    if len(parts) < 3:
        return [("Свой сценарий", text)]
    out = [(head.strip()[:60], body) for head, body in zip(parts[1::2], parts[2::2])
           if parse_script(body)["screens"]]  # раздел со сценарием — с таблицей; вступление файла — мимо
    if not out:
        out = [(head.strip()[:60], body) for head, body in zip(parts[1::2], parts[2::2])
               if len(parse_script(body)["lines"]) >= 3]
    return out or [("Свой сценарий", text)]


def parse_script(text: str) -> dict[str, Any]:
    """Сценарий автора: таблица «Время | Голос | На экране» (из нее — колонка голоса, а подсказки к кадрам —
    из «на экране») или просто фраза на строку. Заголовок — строка «Заголовок: …». Текст не переписываем."""
    title = ""
    m = re.search(r"\*{0,2}Заголовок:?\*{0,2}:?\s*(.+)", text)
    if m:
        title = re.sub(r"\s*#\w+", "", m.group(1)).strip(" *")
    lines: list[str] = []
    screens: list[str] = []
    voice_col = 1  # таблица «Время | Голос | На экране»; если колонки «Голос» нет — это план монтажа, не текст
    plan_only = False
    for row in text.splitlines():
        if row.count("|") >= 3:
            cells = [c.strip() for c in row.strip().strip("|").split("|")]
            low = [c.lower() for c in cells]
            if low and "врем" in low[0]:  # строка заголовков таблицы
                found = next((k for k, c in enumerate(low) if "голос" in c or "текст диктора" in c or "voice" in c),
                             None)
                plan_only = found is None
                voice_col = found or 1
                continue
            if len(cells) > voice_col and TIME_CELL.match(cells[0]) and cells[voice_col] and not plan_only:
                lines.append(cells[voice_col])
                screens.append(" ".join(c for k, c in enumerate(cells[1:], 1) if k != voice_col))
    if not lines:  # без таблицы: каждая непустая строка без разметки — фраза
        for row in text.splitlines():
            row = row.strip()
            if not row or row.startswith(("#", "**", "---", "|", ">", "~", "- ")) or row.lower().startswith("формула"):
                continue
            lines.append(re.sub(r"^\s*\d+[.)]\s*", "", row))
    return {"title": title, "lines": [ln for ln in lines if len(ln) >= 3][:20], "screens": screens}


_SCORE_WORDS = {"единиц": 1, "двойк": 2, "тройк": 3, "четвёрк": 4, "четверк": 4, "пятёрк": 5, "пятерк": 5,
                "шестёрк": 6, "шестерк": 6, "семёрк": 7, "семерк": 7, "восьмёрк": 8, "восьмерк": 8,
                "девятк": 9, "десятк": 10}
_SCORE_NUM = {"одну": 1, "один": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7,
              "восемь": 8, "девять": 9, "десять": 10}


def review_score(lines: list[str]) -> dict[str, Any] | None:
    """Оценка автора в сценарии рецензии: «8/10», «8 из 10», «крепкая восьмёрка из десяти», «восемь из десяти».
    {"line": номер фразы (с 0), "value": 8} — для финальной карточки «МОЯ ОЦЕНКА 8/10»."""
    for i in range(len(lines) - 1, -1, -1):
        low = lines[i].lower()
        m = re.search(r"(?<!\d)(10|[1-9](?:[.,]5)?)\s*(?:/|из)\s*10\b", low)
        if m:
            return {"line": i, "value": m.group(1).replace(",", ".")}
        if "из десяти" in low or "из 10" in low or "оценк" in low:
            for stem, v in _SCORE_WORDS.items():
                if stem in low:
                    return {"line": i, "value": str(v)}
            m = re.search(r"\b(" + "|".join(_SCORE_NUM) + r")\s+из\s+десяти", low)
            if m:
                return {"line": i, "value": str(_SCORE_NUM[m.group(1)])}
    return None


def _quoted(text: str) -> list[str]:
    """Названия в «ёлочках» по порядку (вложенные кавычки «„…“» тоже)."""
    names = re.findall(r"[«„]([^«»„“]{2,60})[»“]", text)
    out: list[str] = []
    for n in names:
        n = n.strip(" .,!?")
        if (not n or len(n) < 3 or not any(ch.isalpha() for ch in n) or n.lower() in ("сериал", "фильм", "мультфильм")
                or (n.upper() == n and any(ch.isalpha() for ch in n))):
            continue  # «5 ФИЛЬМОВ ДО 18.12», «2019», «сериал» — надписи и плашки на экране, а не названия
        if n.lower() not in {x.lower() for x in out}:
            out.append(n)
    return out


async def _film_backdrops(tmdb_key: str, name: str, n: int = 3, tv: bool = False, year: int | None = None,
                          meta: dict[str, Any] | None = None) -> list[str]:
    """Кадры фильма или сериала по названию (русскому или оригинальному): самый похожий и популярный.
    tv — в сценарии сказано, что это сериал («второй сезон «Локи»», плашка «сериал»): сериал впереди."""
    films: Any = None
    shows: Any = None
    for attempt in range(2):  # TMDB изредка отвечает ошибкой на пачку запросов подряд — повторяем один раз
        if attempt:
            await asyncio.sleep(1.5)
        if not isinstance(films, list):
            films = await asyncio.gather(tmdb.search_movie(tmdb_key, name), return_exceptions=True)
            films = films[0]
        if not isinstance(shows, list):
            shows = await asyncio.gather(tmdb.search_tv(tmdb_key, name), return_exceptions=True)
            shows = shows[0]
        if isinstance(films, list) and isinstance(shows, list):
            break
    for what, res in (("movie", films), ("tv", shows)):
        if not isinstance(res, list):
            log.warning("shorts: own script %s search %r failed: %s", what, name, res)
    cands = [(r, "movie") for r in (films if isinstance(films, list) else [])[:4]] + \
            [(r, "tv") for r in (shows if isinstance(shows, list) else [])[:3]]

    def score(c: tuple[dict[str, Any], str]) -> float:
        r = c[0]
        titles = [r.get("title") or "", r.get("original") or ""]
        best = max(tmdb.similar(t, name) for t in titles)
        # «Философский камень» в «Гарри Поттер и философский камень» — тот фильм; но одно слово («Локи») в
        # чужом названии («Симпсоны: Добро, Барт и Локи») — не он
        if len(name.split()) >= 2 and any(name.lower() in t.lower() for t in titles if t):
            best = max(best, 0.9)
        bonus = 0.05 if tv and c[1] == "tv" else 0.0
        got = str(r.get("year") or "")[:4]
        if year and got.isdigit():  # «Обитель зла» (2026) — а не фильмы 2002–2016 с тем же названием
            bonus += 0.3 if abs(int(got) - year) <= 1 else -0.3
        return best + bonus

    good = [c for c in cands if score(c) >= 0.6]
    if not good:
        return []
    best, kind = max(good, key=lambda c: (round(score(c), 1), c[0].get("popularity") or 0))
    if meta is not None:  # какой фильм нашли — для трейлера и карточки оценки
        meta.update(kind=kind, id=int(best["id"]), title=best.get("title") or name, poster=best.get("poster"),
                    year=str(best.get("year") or "")[:4])
    try:
        urls = await tmdb.backdrops(tmdb_key, kind, int(best["id"]), n)
    except Exception as exc:  # noqa: BLE001
        log.info("shorts: own script backdrops %s failed: %s", name, exc)
        urls = []
    return urls + ([best["poster"]] if best.get("poster") else [])


def _stem_key(name: str) -> str:
    """Название без падежа: «Обители зла» и «Обитель зла» → «обите зла» — один фильм в разных формах."""
    words = re.findall(r"[0-9a-zа-яё]+", name.lower().replace("ё", "е"))
    return " ".join(w[:max(3, len(w) - 2)] for w in words)  # «Дюну», «Дюны» → «дюн»


def _nominative(name: str) -> str:
    """Грубо — именительный падеж первого слова названия, чтобы искать в базе: «Дюну»/«Дюны» → «Дюна»,
    «Обители зла» → «Обитель зла», «Мстителей» → «Мстители». Не угадали — ищем и по форме из текста."""
    words = name.split(" ", 1)
    w = words[0]
    for end, repl in (("ями", "и"), ("ами", "ы"), ("ей", "и"), ("ой", "а"), ("ую", "ая"), ("ом", ""), ("у", "а"),
                      ("ы", "а"), ("е", "а"), ("ю", "я"), ("и", "ь")):
        if len(w) > len(end) + 2 and w.lower().endswith(end):
            w = w[:-len(end)] + repl
            break
    return " ".join([w] + words[1:])


async def write_own(text: str, tmdb_key: str, heading: str = "") -> dict[str, Any]:
    """Шортс по готовому сценарию автора: фразы — как есть, кадры — фильмов и сериалов из «ёлочек» (по порядку
    сценария), к фразе с названием — кадр этого фильма."""
    parsed = parse_script(text)
    lines = parsed["lines"]
    if len(lines) < 2:
        raise RuntimeError("не нашёл фраз: пришли сценарий таблицей «Время | Голос | На экране» или фразу на строку")
    films = _quoted(" ".join(lines + parsed["screens"]))[:10]
    # франшиза из заголовка раздела («Shorts 1 — Мстители: Судный день», «Shorts 3 — Гарри Поттер (сериал HBO)»)
    topic = re.sub(r"\(.*?\)", "", heading.split("—", 1)[-1]).strip(" «»") if "—" in heading else ""
    said: dict[str, str] = {}  # название для поиска -> как оно звучит в тексте (по нему ищем фразу)
    if topic:
        # «Судный день» в тексте — это «Мстители: Судный день» из заголовка, а не одноимённый фильм 2008 года
        for f in films:
            # но «Дюна» в «Дюна: Часть третья» — сама франшиза (пересказ первых частей): её не меняем
            if f.lower() != topic.lower() and f.lower() in topic.lower() and not topic.lower().startswith(f.lower()):
                said.setdefault(topic, f)
        films = list(dict.fromkeys(topic if f in said.values() else f for f in films))
        if topic.lower() not in {f.lower() for f in films}:
            films.append(topic)

    def is_tv(film: str) -> bool:  # «второй сезон «Локи»», «Локи на троне, плашка «сериал»»
        low = film.lower()
        return any(low in (ln + " " + sc).lower() and re.search(r"сезон|сериал", (ln + " " + sc).lower())
                   for ln, sc in zip(lines, parsed["screens"] + [""] * len(lines)))

    gate = asyncio.Semaphore(3)  # не залпом: по 3 названия одновременно

    # год рядом с названием: «Обитель зла» (2026) или «Обитель зла» 2026 года — нужен именно этот фильм
    years = {m.group(1).strip().lower(): int(m.group(2)) for m in re.finditer(
        r"«([^«»]{2,60})»\s*\(?\s*((?:19|20)\d\d)", " ".join([heading, parsed["title"], text]))}
    # год — к фильму в любом падеже: «Обители зла» (2026) относится и к «Обитель зла» в конце сценария
    years_stem = {_stem_key(k): v for k, v in years.items()}

    def year_of(film: str) -> int | None:
        return years.get(said.get(film, film).lower()) or years_stem.get(_stem_key(said.get(film, film)))

    # одно название в разных падежах ищем один раз — лучше форму без падежа (её база знает)
    by_stem: dict[str, str] = {}
    for f in films:
        by_stem.setdefault(_stem_key(f), f)
    forms: dict[str, list[str]] = {}
    for f in films:
        forms.setdefault(by_stem[_stem_key(f)], []).append(f)
    for f, fs in forms.items():  # сначала форма в именительном падеже — по ней база находит сам фильм
        guess = [_nominative(x) for x in fs]
        forms[f] = list(dict.fromkeys(guess + fs))

    metas: dict[str, dict[str, Any]] = {}

    async def look(film: str) -> list[str]:
        async with gate:
            metas[film] = {}
            urls = []
            for form in forms.get(film, [film]):  # «Обители зла» не нашлась — пробуем «Обитель зла» из текста
                urls = await _film_backdrops(tmdb_key, form, tv=is_tv(film), year=year_of(film), meta=metas[film])
                if urls:
                    break
            return urls

    films = list(forms)
    found = await asyncio.gather(*(look(f) for f in films)) if tmdb_key else []
    images: list[str] = []
    fixed: list[dict[str, Any]] = []
    missing: list[str] = []
    for film, urls in zip(films, found):
        if not urls:
            missing.append(said.get(film, film))
            continue
        # кадры упомянутых мимоходом фильмов («режиссёр «Варвара»») — только к их фразе, а не по всему ролику
        main_like = not years or year_of(film) is not None
        if main_like:
            images += urls[1:]
        key = said.get(film, film).lower()
        keys = [k.lower() for k in forms.get(film, [film])] + [key]
        line = next((i for i, (ln, sc) in enumerate(zip(lines, parsed["screens"] + [""] * len(lines)))
                     if any(k in (ln + " " + sc).lower() for k in keys)), None)
        if line is not None and all(f["line"] != line + 1 for f in fixed):
            fixed.append({"line": line + 1, "keys": [key], "url": urls[0], "mode": "photo", "who": film})
        elif main_like:
            images.insert(0, urls[0])
    # год после названия нужен только для поиска фильма — вслух и в субтитрах его нет:
    # «Обители зла» (2026) → «Обители зла»
    lines = [re.sub(r"(»)\s*\(\s*(?:19|20)\d\d\s*(?:г\.?|года?)?\s*\)", r"\1", ln) for ln in lines]
    title = parsed["title"] or (films[0] if films else lines[0][:60])
    # главный фильм ролика (рецензия, новость): с годом рядом с названием, иначе первый найденный — из его
    # трейлера бот возьмёт отрезки под фразы, его постер — на карточку оценки
    # главный фильм: с годом рядом с названием; иначе — упомянутый чаще всех (в любом падеже), а не первый попавшийся
    # («режиссёр «Варвара»» мимоходом не должен стать главным фильмом рецензии на «Обитель зла»)
    def mentions(f: str) -> int:  # в скольких фразах назван фильм (в любом падеже)
        stems = _stem_key(said.get(f, f)).split()
        return sum(1 for ln in lines if stems and all(w in ln.lower().replace("ё", "е") for w in stems))

    with_meta = [f for f in films if metas.get(f)]
    main_name = next((f for f in with_meta if year_of(f) is not None), None) or (
        max(with_meta, key=lambda f: (mentions(f), -films.index(f))) if with_meta else None)
    main = metas[main_name] if main_name else None
    trailer_film = {**main, "names": [n for n in films if metas.get(n) is main]} if main else None
    # пересказ франшизы: «Дюна» (2021) и «Дюна: Часть вторая» (2024) — трейлеры всех фильмов с годом
    with_year = [metas[f] for f in with_meta if year_of(f) is not None]
    seen_ids: set[int] = set()
    trailer_films = [m for m in with_year if not (m["id"] in seen_ids or seen_ids.add(m["id"]))]
    if not trailer_films and main:
        trailer_films = [main]
    score = review_score(lines)
    if main and not parsed["title"]:
        # название ролика — официальное из базы, а не форма из фразы («Обители зла» → «Обитель зла»)
        canon = str(main.get("title") or title)
        year_tag = f" ({main['year']})" if str(main.get("year") or "").isdigit() else ""
        title = f"«{canon}»{year_tag}" + (f" — рецензия без спойлеров, {score['value']}/10" if score
                                          else " — что нужно знать")
        recap = recap_title([str(m.get("title") or "") for m in trailer_films], lines)
        if recap and not score:
            title = recap
    name = str(main.get("title")) if main and main.get("title") else title
    return {"kind": "news", "kind_own": True, "fixed": fixed, "trailer_film": trailer_film,
            "trailer_films": trailer_films[:3],
            "score": score, "name": name, "en": "", "title": title, "lines": lines,
            "hashtags": [], "films": films, "article": "сценарий автора", "url": "", "missing": missing,
            "images": list(dict.fromkeys(images))[:MAX_IMAGES]}

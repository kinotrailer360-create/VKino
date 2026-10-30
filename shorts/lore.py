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
import re
import sys
from pathlib import Path
from typing import Any

import aiohttp
from PIL import Image

from . import ai, daily, tmdb
from . import music as musiclib
from .cards import make_cover
from .daily import (
    ASSETS,
    ROOT,
    SAVE_AND_SUBSCRIBE,
    TELEGRAM_URL,
    YOUTUBE_URL,
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


async def news_images(tmdb_key: str, person: str, person_en: str, films: list[str]) -> list[str]:
    """Фото к новости: портреты человека и кадры фильмов из текста (TMDB), иначе — картинки Википедии."""
    urls: list[str] = []
    try:
        info = await tmdb.person_by_name(tmdb_key, person_en or person)
    except Exception as exc:  # noqa: BLE001
        log.info("shorts: TMDB person %s failed: %s", person_en or person, exc)
        info = None
    if info:
        urls += tmdb.profile_urls(info)
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
    return out[:MAX_IMAGES]


async def write_news(text: str, key: str, tmdb_key: str) -> dict[str, Any]:
    """Шортс «своя новость»: текст автора → закадровый текст, фото человека и кадры фильмов из новости."""
    story = await ai.write_news_story(key, text)
    name = story["person"] or story["title"] or "Новость"
    images = await news_images(tmdb_key, story["person"], story["person_en"], story["films"])
    return {"kind": "news", "name": name, "en": story["person_en"], "title": story["title"],
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
             f"📺 YouTube: {YOUTUBE_URL}",
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
             f"📺 YouTube: {YOUTUBE_URL}",
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


def _frame_ready(img: Image.Image, limit: int = 2400) -> Image.Image:
    """Уменьшить огромные сканы (рендеру хватит ~2400 px по длинной стороне)."""
    img = img.convert("RGB")
    if max(img.size) > limit:
        img.thumbnail((limit, limit), Image.LANCZOS)
    return img


async def make_lore(work: Path, script: dict[str, Any], music: Path | None = None,
                    user_media: list[Path] | None = None, gemini_key: str = "") -> ShortResult:
    lines = script["lines"]
    news = script.get("kind") == "news"
    mourning = news and is_mourning(script)
    await _progress(5, "Озвучка")
    # в прощальном ролике «подписывайся» в конце неуместно
    spoken = lines if mourning else lines[:-1] + [f"{lines[-1]} {SAVE_AND_SUBSCRIBE}"]
    voice, starts, voiced, spans = await daily.synthesize(spoken, work)
    await _progress(30, "Субтитры по слову")
    words = await daily.word_timeline(voice, starts, spans) if voiced else []

    await _progress(40, "Картинки")
    pool: list[tuple[str, Image.Image]] = []  # (файл, миниатюра для ИИ)
    for n, path in enumerate(user_media or []):
        try:
            img = _frame_ready(Image.open(path))
        except Exception as exc:  # noqa: BLE001
            log.warning("shorts: lore user image %s skipped: %s", path.name, exc)
            continue
        out = work / f"luser{n}.jpg"
        img.save(out, quality=90)
        img.thumbnail((480, 480))
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
    if not pool:
        raise RuntimeError("нет ни одной картинки: пришли свои (страницы комиксов, кадры) кнопкой «📎»")

    # какую картинку на какую фразу: ИИ по смыслу; остальное — по порядку, без повторов подряд
    choices = await ai.match_images(gemini_key, lines, [im for _, im in pool]) if gemini_key else None
    used: set[int] = set()
    order: list[int] = []
    nxt = 0
    for i in range(len(lines)):
        c = choices[i] if choices and i < len(choices) else -1
        if c < 0 or c in used:
            free = [j for j in range(len(pool)) if j not in used]
            c = free[0] if free else nxt % len(pool)
            nxt += 1
        used.add(c)
        order.append(c)
    slides = [{"start": starts[i], "image": pool[order[i]][0]} for i in range(len(lines))]

    cfg = {
        "output": "short.mp4",
        "voice": voice.name,
        "music": str(music or musiclib.pick(script.get("title") or "", " ".join(script.get("lines") or []))
                     or ASSETS / "music.mp3"),
        "music_db": -17 if voiced else -6,
        "logo": str(ASSETS / "logo.png"),
        "fonts": {"head": str(ASSETS / "Oswald.ttf"), "sans": str(ASSETS / "Montserrat.ttf")},
        "slides": slides,
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
    if notes:
        result.seo["warning"] = "\n".join(notes)
    return result

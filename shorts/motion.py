"""Моушн-вставки для выпуска «Новости кино»: оценки, таймлайн, цитата, дата премьеры, цифры, афиши фильмов.

Как и графики сборов (boxchart), кадры рисуются на Python и уходят в ffmpeg; монтаж ставит ролик в новость
с той фразы, где о нём речь. Что показывать — решает ИИ по фактам источников (поле graphics в сценарии);
афиши фильмов человека и совместные фильмы двух людей бот берёт с TMDB.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageOps

from . import boxchart
from .boxchart import ACCENT, FPS, GREEN, GREY, RED, H, W, _ease, _fit, _header
from .cards import _font

log = logging.getLogger("shorts")

SECONDS = 7.0
TYPES = ("rating", "timeline", "quote", "date", "facts", "duo", "films")


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    rows = [""]
    for word in text.split():
        if draw.textlength(f"{rows[-1]} {word}".strip(), font=font) <= width:
            rows[-1] = f"{rows[-1]} {word}".strip()
        else:
            rows.append(word)
    return [r for r in rows if r]


def _frames(seconds: float) -> range:
    return range(int(seconds * FPS))


# ---------------------------------------------------------------- оценки

def rating_frames(bg: Image.Image, title: str, scores: list[tuple[str, float, float]], sub: str,
                  seconds: float):
    """Круговые шкалы: [(подпись, значение, максимум)] — 91 из 100 у критиков, 8,1 из 10 на IMDb."""
    n = len(scores)
    radius = 210 if n <= 2 else 170
    gap = W / (n + 1)
    for f in _frames(seconds):
        t = f / FPS
        frame = bg.copy()
        draw = ImageDraw.Draw(frame)
        _header(draw, title, sub)
        for k, (label, value, top) in enumerate(scores):
            p = _ease((t - 0.3 - k * 0.4) / 2.2)
            cx, cy = gap * (k + 1), 640
            share = value / top
            color = GREEN if share >= 0.6 else RED
            box = (cx - radius, cy - radius, cx + radius, cy + radius)
            draw.arc(box, 0, 360, fill=(60, 64, 76), width=34)
            if p > 0:
                draw.arc(box, -90, -90 + 360 * share * p, fill=color, width=34)
            shown = value * p
            text = f"{shown:.0f}%" if top == 100 else f"{shown:.1f}".replace(".", ",")
            draw.text((cx, cy - 10), text, font=_font("Oswald.ttf", 130 if n <= 2 else 104, "Bold"), fill="white",
                      anchor="mm")
            if top != 100:
                draw.text((cx, cy + 85), f"из {top:g}", font=_font("Montserrat.ttf", 34, "SemiBold"), fill=GREY,
                          anchor="mm")
            draw.text((cx, cy + radius + 70), label, font=_font("Montserrat.ttf", 44, "Bold"),
                      fill=ACCENT, anchor="mm")
        yield frame


# ---------------------------------------------------------------- таймлайн

def timeline_frames(bg: Image.Image, title: str, items: list[dict[str, str]], seconds: float):
    """Линия времени слева направо: точки появляются по очереди — «начало 2027: старт съёмок»."""
    n = len(items)
    width = min((W - 220) / n - 30, 520)  # ширина подписи под точкой — крайние не вылезают за кадр
    x0, x1, y = 110 + width / 2, W - 110 - width / 2, 640
    step = (x1 - x0) / max(n - 1, 1)
    grow = seconds * 0.6
    for f in _frames(seconds):
        t = f / FPS
        frame = bg.copy()
        draw = ImageDraw.Draw(frame)
        _header(draw, title, "")
        p_line = _ease(t / grow)
        draw.line((x0, y, x0 + (x1 - x0) * p_line, y), fill=(200, 200, 210), width=8)
        for k, item in enumerate(items):
            at = (k / max(n - 1, 1)) * grow
            p = _ease((t - at) / 0.6)
            if p <= 0:
                continue
            cx = x0 + step * k if n > 1 else W / 2
            r = 26 * p
            draw.ellipse((cx - r, y - r, cx + r, y + r), fill=ACCENT)
            when_font = _fit(draw, item["when"], "Oswald.ttf", 64, int(width))
            draw.text((cx, y - 70 - 30 * (1 - p)), item["when"].upper(), font=when_font, fill="white", anchor="ms")
            what_font = _font("Montserrat.ttf", 36, "SemiBold")
            for i, row in enumerate(_wrap(draw, item["what"], what_font, int(width))[:3]):
                draw.text((cx, y + 80 + i * 48), row, font=what_font, fill=(225, 225, 225), anchor="mt")
        yield frame


# ---------------------------------------------------------------- цитата

def quote_frames(bg: Image.Image, who: str, text: str, seconds: float):
    """Цитата печатается по словам, внизу — кто сказал."""
    draw0 = ImageDraw.Draw(bg.copy())
    size = 64
    while size > 38:
        font = _font("Montserrat.ttf", size, "Bold")
        rows = _wrap(draw0, f"«{text}»", font, W - 420)
        if len(rows) <= 5:
            break
        size -= 4
    words_total = len(f"«{text}»".split())
    for f in _frames(seconds):
        t = f / FPS
        frame = bg.copy()
        draw = ImageDraw.Draw(frame)
        draw.text((150, 170), "“", font=_font("Oswald.ttf", 300, "Bold"), fill=ACCENT)
        shown = int(words_total * _ease(t / (seconds * 0.55)) + 0.999)
        left = shown
        y = H / 2 - len(rows) * size * 0.65
        for row in rows:
            words = row.split()
            part = " ".join(words[:max(0, left)])
            left -= len(words)
            if part:
                draw.text((240, y), part, font=font, fill="white")
            y += size * 1.3
        if t > seconds * 0.45:
            p = _ease((t - seconds * 0.45) / 0.8)
            draw.rounded_rectangle((240, y + 40, 240 + 140 * p, y + 48), radius=4, fill=ACCENT)
            draw.text((240, y + 80), f"— {who}", font=_font("Montserrat.ttf", 46, "SemiBold"), fill=GREY)
        yield frame


# ---------------------------------------------------------------- дата премьеры

def date_frames(bg: Image.Image, date_text: str, what: str, poster: Image.Image | None, seconds: float):
    """Афиша слева выезжает, справа крупно дата: «13 НОЯБРЯ / 2026 / премьера в кино»."""
    m = re.match(r"\s*(.*?)(?:\s+(\d{4})(?:\s*г(?:ода|\.)?)?)?\s*$", date_text)
    main, year = (m.group(1), m.group(2) or "") if m else (date_text, "")
    post = None
    if poster is not None:
        post = ImageOps.fit(poster.convert("RGB"), (520, 780), Image.LANCZOS)
    for f in _frames(seconds):
        t = f / FPS
        frame = bg.copy()
        draw = ImageDraw.Draw(frame)
        p = _ease(t / 1.0)
        if post is not None:
            x = int(-560 + (180 + 560) * p)
            frame.paste(post, (x, 150))
            draw.rectangle((x, 150, x + 520, 930), outline=ACCENT, width=6)
        cx = 1250 if post is not None else W / 2
        q = _ease((t - 0.5) / 0.9)
        if q > 0:
            size = int(150 * (0.7 + 0.3 * q))
            font = _fit(draw, main.upper(), "Oswald.ttf", size, 1100 if post is not None else W - 200)
            draw.text((cx, 470), main.upper(), font=font, fill="white", anchor="mm")
            if year:
                draw.text((cx, 610), year, font=_font("Oswald.ttf", 90, "Bold"), fill=ACCENT, anchor="mm")
            wf = _font("Montserrat.ttf", 46, "SemiBold")
            for i, row in enumerate(_wrap(draw, what, wf, 1000)[:2]):
                draw.text((cx, 730 + i * 60), row, font=wf, fill=(230, 230, 230), anchor="mm")
        yield frame


# ---------------------------------------------------------------- ключевые цифры

def facts_frames(bg: Image.Image, title: str, items: list[dict[str, str]], seconds: float):
    """2–4 плитки: крупная цифра (бежит от нуля, если это число) и подпись."""
    n = len(items)
    gap = 40
    tile_w = (W - 220 - gap * (n - 1)) / n
    for f in _frames(seconds):
        t = f / FPS
        frame = bg.copy()
        draw = ImageDraw.Draw(frame)
        _header(draw, title, "")
        for k, item in enumerate(items):
            p = _ease((t - 0.3 - k * 0.35) / 1.4)
            if p <= 0:
                continue
            x = 110 + k * (tile_w + gap)
            y = 380 + 40 * (1 - p)
            draw.rounded_rectangle((x, y, x + tile_w, y + 480), radius=28, fill=(26, 28, 36))
            draw.rounded_rectangle((x, y, x + tile_w, y + 10), radius=5, fill=ACCENT)
            value = item["value"]
            num = re.fullmatch(r"(\d+)", value)
            shown = str(round(int(num.group(1)) * p)) if num else value
            font = _fit(draw, value, "Oswald.ttf", 170, int(tile_w - 60))
            draw.text((x + tile_w / 2, y + 190), shown, font=font, fill="white", anchor="mm")
            lf = _font("Montserrat.ttf", 38, "SemiBold")
            for i, row in enumerate(_wrap(draw, item["label"], lf, int(tile_w - 60))[:3]):
                draw.text((x + tile_w / 2, y + 330 + i * 48), row, font=lf, fill=(220, 220, 220), anchor="mm")
        yield frame


# ---------------------------------------------------------------- афиши фильмов

def posters_frames(bg: Image.Image, title: str, films: list[dict[str, Any]], seconds: float, gray: bool = False):
    """Афиши по годам выскакивают по очереди, под каждой — год и название."""
    films = films[:7]
    n = len(films)
    pw = min(300, int((W - 220 - 30 * (n - 1)) / n))
    ph = int(pw * 1.5)
    gap = (W - 220 - pw * n) / max(n - 1, 1) if n > 1 else 0
    x_start = 110 if n > 1 else (W - pw) / 2
    pics = []
    for film in films:
        img = film.get("img")
        if img is not None:
            img = ImageOps.fit(img.convert("RGB"), (pw, ph), Image.LANCZOS)
            if gray:
                img = ImageOps.grayscale(img).convert("RGB")
        pics.append(img)
    for f in _frames(seconds):
        t = f / FPS
        frame = bg.copy()
        draw = ImageDraw.Draw(frame)
        _header(draw, title, "")
        for k, film in enumerate(films):
            p = _ease((t - 0.3 - k * (seconds * 0.5 / n)) / 0.7)
            if p <= 0:
                continue
            x = int(x_start + k * (pw + gap))
            y = int(330 + 60 * (1 - p))
            if pics[k] is not None:
                frame.paste(pics[k], (x, y))
            else:
                draw.rectangle((x, y, x + pw, y + ph), fill=(40, 42, 52))
            draw.rectangle((x, y, x + pw, y + ph), outline=ACCENT if not gray else (200, 200, 200), width=4)
            draw.text((x + pw / 2, y + ph + 50), film["year"], font=_font("Oswald.ttf", 52, "Bold"),
                      fill=ACCENT if not gray else (220, 220, 220), anchor="mm")
            tf = _font("Montserrat.ttf", 28, "SemiBold")
            for i, row in enumerate(_wrap(draw, film["title"], tf, pw)[:2]):
                draw.text((x + pw / 2, y + ph + 105 + i * 36), row, font=tf, fill="white", anchor="mm")
        yield frame


# ---------------------------------------------------------------- сборка

async def render(item: dict[str, Any], bg_img: Image.Image | None, out: Path, mourning: bool = False,
                 seconds: float = SECONDS) -> Path:
    """item — проверенная вставка (см. clean) с уже скачанными картинками (poster, films[].img)."""
    bg = await _bg(bg_img)
    kind = item["type"]
    if kind == "rating":
        frames = rating_frames(bg, item["title"], item["scores"], item.get("sub", ""), seconds)
    elif kind == "timeline":
        frames = timeline_frames(bg, item["title"], item["items"], seconds)
    elif kind == "quote":
        frames = quote_frames(bg, item["who"], item["text"], seconds)
    elif kind == "date":
        frames = date_frames(bg, item["date"], item["what"], item.get("poster_img"), seconds)
    elif kind == "facts":
        frames = facts_frames(bg, item["title"], item["items"], seconds)
    else:  # duo, films
        frames = posters_frames(bg, item["title"], item["films"], seconds, gray=mourning)
    return await boxchart._encode(frames, out)


async def _bg(img: Image.Image | None) -> Image.Image:
    return await asyncio.to_thread(boxchart._background, img)


def _text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _number(value: Any) -> float:
    try:
        return float(str(value).replace(",", ".").replace("%", "").strip() or 0)
    except ValueError:
        return 0.0


def clean(raw: Any, film: str = "") -> dict[str, Any] | None:
    """Вставка из сценария ИИ → проверенный вид для render (или None, если данных мало или они странные)."""
    if not isinstance(raw, dict) or raw.get("type") not in TYPES:
        return None
    kind = raw["type"]
    line = int(_number(raw.get("line")) or 1)
    out: dict[str, Any] = {"type": kind, "line": max(1, line)}
    name = f"«{film.upper()}»" if film else ""
    if kind == "rating":
        source = _text(raw.get("source"), 30) or "Rotten Tomatoes"
        scores = []
        for label, key in (("Критики", "critics"), ("Зрители", "audience")):
            v = _number(raw.get(key))
            if v <= 0:
                continue
            top = 10.0 if v <= 10 else 100.0
            if v > top:
                continue
            scores.append((label if top == 100 else source, v, top))
        if not scores:
            return None
        reviews = int(_number(raw.get("reviews")))
        out.update(scores=scores, title=f"{name} — ОЦЕНКИ".strip(" —") if name else "ОЦЕНКИ",
                   sub=source + (f" · {reviews} рецензий" if reviews else ""))
    elif kind == "timeline":
        items = [{"when": _text(i.get("when"), 30), "what": _text(i.get("what"), 70)}
                 for i in raw.get("items") or [] if isinstance(i, dict) and i.get("when") and i.get("what")][:5]
        if len(items) < 2:
            return None
        out.update(items=items, title=f"{name}: ЧТО ДАЛЬШЕ" if name else "ЧТО ДАЛЬШЕ")
    elif kind == "quote":
        text, who = _text(raw.get("text"), 220).strip("«»\""), _text(raw.get("who"), 60)
        if len(text) < 15 or not who:
            return None
        out.update(text=text, who=who)
    elif kind == "date":
        date_text, what = _text(raw.get("date"), 40), _text(raw.get("what"), 80)
        if not re.search(r"\d", date_text) or not what:
            return None
        out.update(date=date_text, what=what)
    elif kind == "facts":
        items = [{"value": _text(i.get("value"), 12), "label": _text(i.get("label"), 60)}
                 for i in raw.get("items") or [] if isinstance(i, dict) and i.get("value") and i.get("label")][:4]
        if len(items) < 2:
            return None
        out.update(items=items, title=_text(raw.get("title"), 60).upper() or "ГЛАВНОЕ В ЦИФРАХ")
    elif kind == "duo":
        people = [_text(p, 60) for p in raw.get("people") or [] if p][:2]
        if len(people) != 2:
            return None
        out.update(people=people)
    elif kind == "films":
        person = _text(raw.get("person"), 60)
        if not person:
            return None
        out.update(person=person)
    return out

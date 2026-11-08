"""Кинодневник подписчика: итоги и карточка «Мой год в кино» (картинка, которую хочется переслать).

Чистые функции без сети и базы: статистика по записям дневника и рисование карточки (Pillow)."""

from __future__ import annotations

import io
from collections import Counter
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ASSETS = Path(__file__).resolve().parent / "assets"
W, H = 1080, 1350
GOLD, WHITE, GREY = (255, 196, 61), (255, 255, 255), (170, 176, 190)


def _font(name: str, size: int, weight: str = "SemiBold") -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(str(ASSETS / name), size)
    try:  # Montserrat — переменный шрифт: по умолчанию тонкий, на телефоне плохо читается
        font.set_variation_by_name(weight)
    except Exception:  # noqa: BLE001 — не переменный шрифт
        pass
    return font


def _films_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "фильм"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "фильма"
    return "фильмов"


def stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """rows: {title, score (0 — без оценки), genres «драма, триллер», year_released, poster}."""
    scored = [r for r in rows if int(r.get("score") or 0) > 0]
    genres = Counter(g.strip() for r in rows for g in str(r.get("genres") or "").split(",") if g.strip())
    decades = Counter(f"{int(r['year_released']) // 10 * 10}-е" for r in rows if r.get("year_released"))
    best = sorted(scored, key=lambda r: (-int(r["score"]), str(r.get("title"))))[:3]
    return {"count": len(rows), "avg": round(sum(int(r["score"]) for r in scored) / len(scored), 1) if scored else None,
            "genre": genres.most_common(1)[0][0] if genres else "", "decade": decades.most_common(1)[0][0]
            if decades else "", "best": best, "tens": sum(1 for r in scored if int(r["score"]) == 10)}


def _fit(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> str:
    """Обрезать строку с «…», чтобы влезла в width пикселей."""
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text.rstrip(" :,—") + "…"


def year_card(name: str, year: int, st: dict[str, Any], posters: list[bytes | None], bot_link: str) -> bytes:
    """PNG 1080×1350: «Мой 2026 в кино» — сколько фильмов, средняя оценка, любимый жанр, топ-3 с постерами."""
    img = Image.new("RGB", (W, H), (14, 16, 24))
    draw = ImageDraw.Draw(img)
    for y in range(H):  # мягкий градиент сверху вниз
        k = y / H
        draw.line([(0, y), (W, y)], fill=(int(20 + 30 * k), int(18 + 8 * k), int(40 + 20 * (1 - k))))
    head, big, sans, small = _font("Oswald.ttf", 64), _font("Oswald.ttf", 190), _font("Montserrat.ttf", 40), \
        _font("Montserrat.ttf", 32)
    draw.text((W // 2, 90), f"МОЙ {year} В КИНО", font=head, fill=GOLD, anchor="mm")
    if name:
        draw.text((W // 2, 160), _fit(draw, name, small, W - 120), font=small, fill=GREY, anchor="mm")
    draw.text((W // 2, 330), str(st["count"]), font=big, fill=WHITE, anchor="mm")
    draw.text((W // 2, 450), _films_word(st["count"]) + " посмотрено", font=sans, fill=WHITE, anchor="mm")
    facts = []
    if st.get("avg"):
        facts.append(f"⭐ средняя оценка {st['avg']}")
    if st.get("genre"):
        facts.append(f"любимый жанр — {st['genre']}")
    if st.get("decade"):
        facts.append(f"чаще всего — фильмы {st['decade']}")
    for k, line in enumerate(facts[:3]):
        draw.text((W // 2, 530 + k * 52), line.replace("⭐ ", ""), font=small, fill=GREY, anchor="mm")
    if st.get("best"):
        draw.text((W // 2, 730), "ЛУЧШЕЕ ЗА ГОД", font=_font("Oswald.ttf", 44), fill=GOLD, anchor="mm")
        n = len(st["best"])
        pw, ph, gap = 260, 390, 40
        x0 = (W - (n * pw + (n - 1) * gap)) // 2
        for k, row in enumerate(st["best"]):
            x = x0 + k * (pw + gap)
            box = Image.new("RGB", (pw, ph), (40, 44, 60))
            raw = posters[k] if k < len(posters) else None
            if raw:
                try:
                    p = Image.open(io.BytesIO(raw)).convert("RGB")
                    scale = max(pw / p.width, ph / p.height)
                    p = p.resize((int(p.width * scale) + 1, int(p.height * scale) + 1))
                    box = p.crop(((p.width - pw) // 2, (p.height - ph) // 2, (p.width - pw) // 2 + pw,
                                  (p.height - ph) // 2 + ph))
                except Exception:  # noqa: BLE001 — битый постер: серая плашка
                    pass
            shadow = Image.new("RGBA", (pw + 30, ph + 30), (0, 0, 0, 0))
            ImageDraw.Draw(shadow).rectangle([15, 15, pw + 15, ph + 15], fill=(0, 0, 0, 160))
            img.paste(shadow.filter(ImageFilter.GaussianBlur(10)), (x - 15, 780 - 5), shadow.filter(
                ImageFilter.GaussianBlur(10)))
            img.paste(box, (x, 790))
            draw.text((x + pw // 2, 790 + ph + 34), _fit(draw, str(row.get("title") or ""), small, pw), font=small,
                      fill=WHITE, anchor="mm")
            draw.text((x + pw // 2, 790 + ph + 76), f"{int(row['score'])}/10", font=small, fill=GOLD, anchor="mm")
    draw.text((W // 2, H - 50), f"Веди свой кинодневник: {bot_link}", font=_font("Montserrat.ttf", 28),
              fill=GREY, anchor="mm")
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()

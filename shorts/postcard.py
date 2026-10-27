"""Карточки фильмов для постов-подборок в Telegram: фирменный стиль KinoMir360 и защита от копирования.

Формат 4:5 (1080×1350) — в ленте Telegram он крупнее квадрата. Сверху — рейтинг и место в подборке,
снизу — название, год и жанр, актёры и плашка KinoMir360; по центру — едва заметный знак канала.
"""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

from .cards import ASSETS, _cover, _font, _gradient

W, H = 1080, 1350
ACCENT = (255, 184, 28)  # жёлто-оранжевый, как субтитры в роликах
INK = (14, 14, 18)
BRAND = "KinoMir360"


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int, lines: int) -> list[str]:
    out: list[str] = []
    for word in text.split():
        if out and draw.textlength(f"{out[-1]} {word}", font=font) <= max_w:
            out[-1] += f" {word}"
        else:
            out.append(word)
    if len(out) > lines:
        out = out[:lines]
        out[-1] = out[-1].rstrip(",.:;") + "…"
    return out


def _title_font(draw: ImageDraw.ImageDraw, title: str) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    for size in range(124, 60, -6):
        font = _font("Oswald.ttf", size, "Bold")
        rows = _wrap(draw, title, font, W - 120, 2)
        if all(draw.textlength(r, font=font) <= W - 120 for r in rows) and "…" not in rows[-1]:
            return font, rows
    font = _font("Oswald.ttf", 60, "Bold")
    return font, _wrap(draw, title, font, W - 120, 2)


def make_post_card(img: Image.Image, title: str, place: int = 0, total: int = 0, rating: float | None = None,
                   rating_label: str = "", year: int | str | None = None, genres: list[str] | None = None,
                   actors: list[str] | None = None) -> Image.Image:
    """Карточка фильма для подборки; без данных (фильм не с Кинопоиска) — только название и знак канала."""
    card = _cover(img.convert("RGB"), W, H, y_bias=0.3).convert("RGBA")
    card.alpha_composite(_gradient(W, 300, 170, 0))
    card.alpha_composite(_gradient(W, 900, 0, 250, INK), (0, H - 900))
    draw = ImageDraw.Draw(card)

    # знак канала по центру — едва заметный, чтобы картинку не выдали за свою
    mark = Image.new("RGBA", (W, 200), (0, 0, 0, 0))
    ImageDraw.Draw(mark).text((W // 2, 100), BRAND, font=_font("Oswald.ttf", 150, "Bold"),
                              fill=(255, 255, 255, 34), anchor="mm")
    card.alpha_composite(mark.rotate(28, expand=False, resample=Image.BICUBIC), (0, H // 2 - 220))

    # рейтинг — плашка цвета канала
    if rating:
        label_font, value_font = _font("Montserrat.ttf", 30, "Bold"), _font("Oswald.ttf", 64, "Bold")
        label = (rating_label or "★").upper()
        value = f"{rating:.1f}"
        lw, vw = draw.textlength(label, font=label_font), draw.textlength(value, font=value_font)
        box = (48, 48, 48 + 36 + lw + 18 + vw + 36, 48 + 100)
        draw.rounded_rectangle(box, radius=50, fill=ACCENT)
        draw.text((box[0] + 36, 98), label, font=label_font, fill=INK, anchor="lm")
        draw.text((box[0] + 36 + lw + 18, 96), value, font=value_font, fill=INK, anchor="lm")
    # место в подборке
    if place and total:
        num_font, small = _font("Oswald.ttf", 120, "Bold"), _font("Oswald.ttf", 56, "Bold")
        tail = f"/{total}"
        tw = draw.textlength(tail, font=small)
        draw.text((W - 52 - tw, 150), str(place), font=num_font, fill="white", anchor="rs",
                  stroke_width=2, stroke_fill=(0, 0, 0))
        draw.text((W - 52, 150), tail, font=small, fill=ACCENT, anchor="rs")

    # низ: название, акцентная черта, год и жанр, актёры, плашка канала
    font, rows = _title_font(draw, title.upper())
    line_h = round(font.size * 1.12)
    y = H - 250 - line_h * len(rows)
    meta = " · ".join(x for x in [str(year or ""), ", ".join((genres or [])[:2])] if x)
    if actors:
        y -= 50
    if meta:
        y -= 50
    for row in rows:
        draw.text((W // 2, y), row, font=font, fill="white", anchor="mt", stroke_width=2, stroke_fill=(0, 0, 0))
        y += line_h
    y += 18
    draw.rounded_rectangle((W // 2 - 70, y, W // 2 + 70, y + 8), radius=4, fill=ACCENT)
    y += 34
    if meta:
        draw.text((W // 2, y), meta, font=_font("Montserrat.ttf", 34, "SemiBold"), fill=ACCENT, anchor="mt")
        y += 50
    if actors:
        text_font = _font("Montserrat.ttf", 32, "Medium")
        names = ", ".join(actors[:3])
        while draw.textlength(f"В ролях: {names}", font=text_font) > W - 100 and "," in names:
            names = names.rsplit(",", 1)[0]
        draw.text((W // 2, y), f"В ролях: {names}", font=text_font, fill=(235, 235, 235), anchor="mt")

    # плашка канала — внизу по центру, с логотипом
    brand_font = _font("Montserrat.ttf", 36, "Bold")
    bw = draw.textlength(BRAND, font=brand_font)
    logo = Image.open(ASSETS / "logo.png").convert("RGBA")
    logo.thumbnail((56, 56))
    pill_w = 28 + logo.width + 14 + bw + 30
    x0, y0 = (W - pill_w) // 2, H - 118
    draw.rounded_rectangle((x0, y0, x0 + pill_w, y0 + 74), radius=37, fill=(0, 0, 0, 170), outline=ACCENT, width=3)
    card.alpha_composite(logo, (int(x0 + 28), int(y0 + (74 - logo.height) // 2)))
    draw.text((x0 + 28 + logo.width + 14, y0 + 37), BRAND, font=brand_font, fill="white", anchor="lm")
    return card.convert("RGB")

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
                   actors: list[str] | None = None, pill: tuple[str, str] | None = None,
                   meta: str | None = None, extra: str | None = None) -> Image.Image:
    """Карточка фильма для подборки; без данных (фильм не с Кинопоиска) — только название и знак канала.
    pill — своя плашка вместо рейтинга (например («🎂», «51»)); meta и extra — свои строки вместо
    «год · жанр» и «В ролях: …» (например «Роль: Нео»)."""
    card = _cover(img.convert("RGB"), W, H, y_bias=0.3).convert("RGBA")
    card.alpha_composite(_gradient(W, 300, 170, 0))
    card.alpha_composite(_gradient(W, 900, 0, 250, INK), (0, H - 900))
    draw = ImageDraw.Draw(card)

    # знак канала по центру — едва заметный, чтобы картинку не выдали за свою
    _center_mark(card, 150, 28, H // 2 - 120)

    # рейтинг — плашка цвета канала
    if rating or pill:
        label_font, value_font = _font("Montserrat.ttf", 30, "Bold"), _font("Oswald.ttf", 64, "Bold")
        label, value = pill or ((rating_label or "★").upper(), f"{rating:.1f}")
        lw, vw = draw.textlength(label, font=label_font), draw.textlength(value, font=value_font)
        box = (48, 48, 48 + 36 + lw + (18 + vw if value else 0) + 36, 48 + 100)
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
    if meta is None:
        meta = " · ".join(x for x in [str(year or ""), ", ".join((genres or [])[:2])] if x)
    if extra is None and actors:
        extra = "В ролях: " + ", ".join(actors[:3])
    if extra:
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
    if extra:
        text_font = _font("Montserrat.ttf", 32, "Medium")
        while draw.textlength(extra, font=text_font) > W - 100 and "," in extra:
            extra = extra.rsplit(",", 1)[0]
        while draw.textlength(extra, font=text_font) > W - 100:
            extra = extra[:-2].rstrip() + "…"
        draw.text((W // 2, y), extra, font=text_font, fill=(235, 235, 235), anchor="mt")

    _brand_pill(card, H - 118)
    return card.convert("RGB")


def _center_mark(card: Image.Image, size: int, angle: float, cy: int) -> None:
    """Едва заметная надпись KinoMir360 наискосок с центром в (середина, cy)."""
    font = _font("Oswald.ttf", size, "Bold")
    tw = round(ImageDraw.Draw(card).textlength(BRAND, font=font)) + 40
    mark = Image.new("RGBA", (tw, size * 2), (0, 0, 0, 0))
    ImageDraw.Draw(mark).text((tw // 2, size), BRAND, font=font, fill=(255, 255, 255, 34), anchor="mm")
    mark = mark.rotate(angle, expand=True, resample=Image.BICUBIC)
    card.alpha_composite(mark, ((card.width - mark.width) // 2, cy - mark.height // 2))


def _brand_pill(card: Image.Image, y0: int, scale: float = 1.0) -> None:
    """Плашка канала с логотипом по центру по горизонтали."""
    draw = ImageDraw.Draw(card)
    W_ = card.width
    brand_font = _font("Montserrat.ttf", round(36 * scale), "Bold")
    bw = draw.textlength(BRAND, font=brand_font)
    logo = Image.open(ASSETS / "logo.png").convert("RGBA")
    h = round(74 * scale)
    logo.thumbnail((round(56 * scale), round(56 * scale)))
    pill_w = 28 * scale + logo.width + 14 * scale + bw + 30 * scale
    x0 = (W_ - pill_w) // 2
    draw.rounded_rectangle((x0, y0, x0 + pill_w, y0 + h), radius=h // 2, fill=(0, 0, 0, 170), outline=ACCENT,
                           width=3)
    card.alpha_composite(logo, (int(x0 + 28 * scale), int(y0 + (h - logo.height) // 2)))
    draw.text((x0 + 28 * scale + logo.width + 14 * scale, y0 + h // 2), BRAND, font=brand_font, fill="white",
              anchor="lm")


def watermark(img: Image.Image, max_w: int = 1280) -> Image.Image:
    """Кадр для поста со знаками KinoMir360: плашка канала внизу и едва заметная надпись по центру."""
    frame = img.convert("RGB")
    if frame.width > max_w:
        frame = frame.resize((max_w, round(frame.height * max_w / frame.width)), Image.LANCZOS)
    card = frame.convert("RGBA")
    scale = max(0.5, card.width / W)
    band = round(200 * scale)
    card.alpha_composite(_gradient(card.width, band, 0, 150), (0, card.height - band))
    _center_mark(card, round(130 * scale), 20, card.height // 2)
    _brand_pill(card, card.height - round(100 * scale), scale)
    return card.convert("RGB")

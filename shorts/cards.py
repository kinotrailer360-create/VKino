"""Карточки фильмов и обложка в стиле KinoMir360."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ASSETS = Path(__file__).resolve().parent / "assets"
# высота 780: под картинкой — субтитр-фраза и строки с датами, и всё это выше подписей и кнопок YouTube
CARD_W, CARD_H = 1080, 780
COVER_W, COVER_H = 1080, 1920
RED = (229, 9, 20)
IMDB_YELLOW = (245, 197, 24)
TMDB_GREEN = (1, 210, 119)
KP_ORANGE = (255, 102, 0)


def _font(name: str, size: int, variation: str | None = None) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(str(ASSETS / name), size)
    if variation:
        try:
            f.set_variation_by_name(variation)
        except (OSError, ValueError):
            pass
    return f


def _cover(img: Image.Image, w: int, h: int, y_bias: float = 0.5) -> Image.Image:
    s = max(w / img.width, h / img.height)
    img = img.resize((round(img.width * s), round(img.height * s)), Image.LANCZOS)
    x = (img.width - w) // 2
    y = round((img.height - h) * y_bias)
    return img.crop((x, y, x + w, y + h))


def _gradient(w: int, h: int, top_alpha: int, bottom_alpha: int, color=(0, 0, 0)) -> Image.Image:
    g = Image.linear_gradient("L").resize((w, h))
    g = g.point(lambda v: round(top_alpha + (bottom_alpha - top_alpha) * v / 255))
    layer = Image.new("RGBA", (w, h), color + (0,))
    layer.putalpha(g)
    return layer


def _fit(draw: ImageDraw.ImageDraw, text: str, name: str, size: int, max_w: int, min_size: int = 40):
    while size > min_size:
        f = _font(name, size)
        if draw.textlength(text, font=f) <= max_w:
            return f
        size -= 4
    return _font(name, min_size)


def _center(draw, y, text, fnt, fill="white", w=CARD_W, shadow=True):
    x = (w - draw.textlength(text, font=fnt)) / 2
    if shadow:
        draw.text((x + 3, y + 3), text, font=fnt, fill=(0, 0, 0, 160))
    draw.text((x, y), text, font=fnt, fill=fill)
    return x


def make_card(image: Image.Image, title: str, index: int, total: int,
              rating: float | None = None, rating_source: str = "IMDb",
              cast: list[str] | None = None, badge: str | None = None,
              height: int = CARD_H, place: int | None = None) -> Image.Image:
    """Карточка 1080xheight: рейтинг (или badge) слева сверху, n/N справа, название и актёры снизу."""
    card = _cover(image.convert("RGB"), CARD_W, height, 0.35).convert("RGBA")
    card.alpha_composite(card_overlay(title, index, total, rating, rating_source, cast, badge, height, place))
    return card.convert("RGB")


def card_overlay(title: str, index: int, total: int,
                 rating: float | None = None, rating_source: str = "IMDb",
                 cast: list[str] | None = None, badge: str | None = None,
                 height: int = CARD_H, place: int | None = None) -> Image.Image:
    """Всё оформление карточки на прозрачном фоне — кладётся поверх картинки или видео трейлера."""
    card = Image.new("RGBA", (CARD_W, height), (0, 0, 0, 0))
    # верхняя полоса почти непрозрачная: рейтинг, «НОВИНКА» и № читаются и поверх надписей трейлера
    card.alpha_composite(_gradient(CARD_W, 150, 225, 205), (0, 0))
    card.alpha_composite(_gradient(CARD_W, 150, 205, 0), (0, 150))
    card.alpha_composite(_gradient(CARD_W, 480, 0, 225), (0, height - 480))
    d = ImageDraw.Draw(card)

    # рейтинг или дата премьеры
    if rating:
        col = {"IMDb": IMDB_YELLOW, "TMDB": TMDB_GREEN}.get(rating_source, KP_ORANGE)
        d.ellipse((20, 30, 120, 130), fill=col)
        short = len(rating_source) <= 2  # «КП» крупнее, IMDb/TMDB — мельче, чтобы влезли в круг
        lab = _font("PTSerif-Bold.ttf", 38 if short else 27)
        lw = d.textlength(rating_source, font=lab)
        d.text((70 - lw / 2, 57 if short else 64), rating_source, font=lab, fill=(0, 0, 0))
        d.text((140, 38), f"{rating:.1f}", font=_font("PTSerif-Bold.ttf", 72), fill="white")
    elif badge:
        bf = _font("Montserrat.ttf", 40, "Bold")
        bw = d.textlength(badge, font=bf)
        d.rounded_rectangle((24, 40, 24 + bw + 50, 112), 10, fill=RED)
        d.text((49, 52), badge, font=bf, fill="white")

    big = _font("PTSerif-Bold.ttf", 150)
    if place is not None:
        # обратный отсчёт: «№5» … «№1»
        label = f"№{place}"
        lw = d.textlength(label, font=big)
        x = CARD_W - 30 - lw
        d.text((x + 3, 13), label, font=big, fill=(0, 0, 0, 140))
        d.text((x, 10), label, font=big, fill=(255, 214, 0) if place == 1 else "white")
        return _finish(card, d, title, cast, height)

    # n/N
    small = _font("PTSerif-Bold.ttf", 150)
    num, den = str(index), str(total)
    nw, dw = d.textlength(num, font=big), d.textlength(den, font=small)
    x = CARD_W - 30 - nw - 30 - dw
    d.text((x + 3, 10 + 3), num, font=big, fill=(0, 0, 0, 140))
    d.text((x, 10), num, font=big, fill="white")
    d.line((x + nw + 26, 40, x + nw + 6, 160), fill="white", width=4)
    d.text((x + nw + 30 + 3, 13), den, font=small, fill=(0, 0, 0, 140))
    d.text((x + nw + 30, 10), den, font=small, fill="white")

    return _finish(card, d, title, cast, height)


def _title_lines(d, title: str) -> list[tuple[str, ImageFont.FreeTypeFont]]:
    """Название в одну строку, а длинное («Романовы: преданность и предательство») — в две, без обрезки."""
    max_w = CARD_W - 60
    one = _fit(d, title, "PTSerif-Bold.ttf", 120, max_w, 54)
    if d.textlength(title, font=one) <= max_w or " " not in title:
        return [(title, _fit(d, title, "PTSerif-Bold.ttf", 120, max_w, 30))]
    words = title.split()
    # разрыв после двоеточия/тире, иначе — ближе к середине по длине
    cut = next((i + 1 for i, w in enumerate(words[:-1]) if w.endswith((":", "—", "-"))), None)
    if cut is None:
        cut = min(range(1, len(words)), key=lambda i: abs(len(" ".join(words[:i])) - len(" ".join(words[i:]))))
    lines = [" ".join(words[:cut]), " ".join(words[cut:])]
    size = min(_fit(d, ln, "PTSerif-Bold.ttf", 96, max_w, 30).size for ln in lines)
    return [(ln, _font("PTSerif-Bold.ttf", size)) for ln in lines]


def _finish(card, d, title, cast, height):
    """Название и «В ролях» внизу карточки."""
    # название и актёры
    cast_line = ("В ролях: " + ", ".join(cast[:3])) if cast else ""
    cast_f = _fit(d, cast_line, "PTSerif-Regular.ttf", 38, CARD_W - 80, 26) if cast_line else None
    bottom = height - (110 if cast_line else 60)
    if title:
        y = bottom
        for line, fnt in reversed(_title_lines(d, title.upper())):
            y -= d.textbbox((0, 0), line, font=fnt)[3]
            _center(d, y, line, fnt)
            y -= 6
    if cast_line:
        _center(d, bottom + 14, cast_line, cast_f)
        d.rounded_rectangle((110, bottom + 70, CARD_W - 110, bottom + 80), 5, fill=RED)
    return card


def make_cover(image: Image.Image, tag: str, headline: str, brand: str = "KinoMir360") -> Image.Image:
    """Вертикальная обложка 1080x1920: картинка сверху, тёмная панель с плашкой и заголовком снизу."""
    panel_y = 1080  # заголовок выше подписей YouTube — первые секунды ролика его не закрывают
    cover = Image.new("RGBA", (COVER_W, COVER_H), (28, 22, 18, 255))
    top = _cover(image.convert("RGB"), COVER_W, panel_y + 120, 0.25).convert("RGBA")
    cover.alpha_composite(top, (0, 0))
    cover.alpha_composite(_gradient(COVER_W, 420, 0, 255, (28, 22, 18)), (0, panel_y - 300))
    d = ImageDraw.Draw(cover)

    tf = _font("Montserrat.ttf", 46, "Medium")
    tw = d.textlength(tag, font=tf)
    d.rectangle(((COVER_W - tw) / 2 - 70, panel_y - 50, (COVER_W + tw) / 2 + 70, panel_y + 45), fill=(255, 51, 51))
    d.text(((COVER_W - tw) / 2, panel_y - 30), tag, font=tf, fill="white")

    # заголовок: не больше 3 строк, иначе уменьшаем шрифт
    for size in (78, 70, 62, 56):
        hf = _font("Montserrat.ttf", size, "Medium")
        lines: list[str] = []
        for para in headline.split("\n"):
            cur = ""
            for word in para.split():
                cand = f"{cur} {word}".strip()
                if d.textlength(cand, font=hf) <= 760 or not cur:  # правее x≈900 — кнопки YouTube
                    cur = cand
                else:
                    lines.append(cur)
                    cur = word
            if cur:
                lines.append(cur)
        if len(lines) <= 3:
            break
    max_lines = 3 if len(lines) <= 3 else 4  # очень длинное название — даём четвёртую строку
    step = round(size * 1.25)
    y = panel_y + 110 + (3 - min(len(lines), 3)) * step // 2
    for line in lines[:max_lines]:
        _center(d, y, line, hf, w=COVER_W)
        y += step

    bf = _font("Montserrat.ttf", 72, "Regular")
    kino, mir = brand[:4], brand[4:]
    kw = d.textlength(kino, font=bf)
    x = (COVER_W - kw - d.textlength(mir, font=bf)) / 2
    d.text((x, 1770), kino, font=bf, fill=(205, 205, 205))
    d.text((x + kw, 1770), mir, font=bf, fill=(222, 186, 120))
    return cover.convert("RGB")

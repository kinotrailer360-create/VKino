"""Тизер-ролик под инфоповод: мерцающий красный неон, «пепел», логотип KinoMir360 и призыв подписаться.
10 с, 1080×1080, без звука и без чужих кадров/логотипов. Используют бот (🔥 Инфоповод → 🎬 Тизер) и promo/rsya.
"""
from __future__ import annotations

import math
import random
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

A = Path(__file__).resolve().parent / "assets"
W = H = 1080
FPS, SECONDS = 30, 10
RED, WHITE, GREY = (235, 35, 45), (245, 245, 245), (170, 170, 180)
LOGO = Image.open(A / "logo.png").convert("RGBA")
CTA = {"tg": "Подписывайтесь в Telegram", "max": "Подписывайтесь в MAX"}
STRANGE = {"hook": "1955", "hook_sub": "Невада. Город Мэрион.", "line1": "ДО ХОУКИНСА", "line2": "БЫЛ МЭРИОН",
           "sub": "Приквел «Очень странных дел» · 2028", "outro": "Всё о новом сериале —"}


def font(name: str, size: int, weight: bytes | None = None) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(str(A / name), size)
    if weight:
        try:
            f.set_variation_by_name(weight)
        except (OSError, ValueError):
            pass
    return f


def fit(text: str, name: str, size: int, width: int, weight: bytes | None = None) -> ImageFont.FreeTypeFont:
    """Шрифт помельче, если строка не влезает по ширине."""
    f = font(name, size, weight)
    while size > 20 and text and f.getbbox(text)[2] > width:
        size -= 4
        f = font(name, size, weight)
    return f


def ease(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def flicker(t: float, seed: int) -> float:
    """Мерцание неона: в основном горит, иногда гаснет."""
    r = random.Random(int(t * 12) * 31 + seed)
    return 0.25 if r.random() < 0.12 else 1.0


def neon(im: Image.Image, xy, text: str, f, alpha: float, color=RED) -> None:
    if alpha <= 0:
        return
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.text(xy, text, font=f, fill=color + (int(255 * alpha),), anchor="mm")
    for r in (40, 18, 8):  # широкое и узкое свечение
        im.alpha_composite(layer.filter(ImageFilter.GaussianBlur(r)))
        im.alpha_composite(layer.filter(ImageFilter.GaussianBlur(r)))
    core = Image.new("RGBA", im.size, (0, 0, 0, 0))
    ImageDraw.Draw(core).text(xy, text, font=f, fill=(255, 150, 150, int(255 * alpha)), anchor="mm")
    im.alpha_composite(layer)
    im.alpha_composite(core.filter(ImageFilter.GaussianBlur(0.6)))


def plain(im: Image.Image, xy, text: str, f, alpha: float, color=WHITE) -> None:
    if alpha <= 0:
        return
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).text(xy, text, font=f, fill=color + (int(255 * alpha),), anchor="mm")
    im.alpha_composite(layer)


def background(t: float, grain_seed: int) -> Image.Image:
    im = Image.new("RGBA", (W, H), (6, 4, 8, 255))
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    g = ImageDraw.Draw(glow)
    pulse = 0.8 + 0.2 * math.sin(t * 2.2)
    g.ellipse([W * 0.1, H * 0.25, W * 0.9, H * 1.1], fill=(110, 10, 20, int(150 * pulse)))
    im.alpha_composite(glow.filter(ImageFilter.GaussianBlur(160)))
    rnd = random.Random(grain_seed)  # «пепел» в воздухе, медленно плывёт вверх
    d = ImageDraw.Draw(im)
    for _ in range(90):
        x0, y0, sp = rnd.uniform(0, W), rnd.uniform(0, H), rnd.uniform(15, 45)
        x = (x0 + 12 * math.sin(t + y0)) % W
        y = (y0 - sp * t) % H
        s = rnd.choice([1, 2, 2, 3])
        c = rnd.randint(120, 200)
        d.ellipse([x, y, x + s, y + s], fill=(c, c - 40, c - 40, 160))
    return im


def frame(i: int, cta: str, lines: dict[str, str]) -> Image.Image:
    t = i / FPS
    im = background(t, 11)
    hook = lines.get("hook") or ""
    huge = fit(hook, "Oswald.ttf", 300, W - 120)
    big = fit(max(lines.get("line1", ""), lines.get("line2", ""), key=len), "Oswald.ttf", 96, W - 120)
    mid = font("Montserrat.ttf", 54, b"SemiBold")
    # 0–3 c: «1955» мерцающим неоном
    a = ease(t / 0.8) * (1 - ease((t - 2.6) / 0.4))
    neon(im, (W / 2, H / 2 - 40), hook, huge, a * flicker(t, 1))
    plain(im, (W / 2, H / 2 + 185), lines.get("hook_sub", ""), fit(lines.get("hook_sub", ""), "Montserrat.ttf", 54, W - 100, b"SemiBold"), a * ease((t - 0.9) / 0.6), GREY)
    # 3–6 c: «До Хоукинса был Мэрион»
    b = ease((t - 3.0) / 0.6) * (1 - ease((t - 5.7) / 0.4))
    neon(im, (W / 2, H / 2 - 70), lines.get("line1", ""), big, b * flicker(t, 2))
    neon(im, (W / 2, H / 2 + 50), lines.get("line2", ""), big, b * flicker(t + 0.3, 3))
    plain(im, (W / 2, H / 2 + 190), lines.get("sub", ""), fit(lines.get("sub", ""), "Montserrat.ttf", 40, W - 100, b"Medium"), b * ease((t - 3.6) / 0.5), GREY)
    # 6–10 c: логотип канала и призыв
    c = ease((t - 6.1) / 0.6)
    if c > 0:
        logo = LOGO.copy()
        logo.thumbnail((420, 420))
        logo.putalpha(logo.getchannel("A").point(lambda v: int(v * c)))
        im.alpha_composite(logo, (int((W - logo.width) / 2), 150))
        plain(im, (W / 2, 640), lines.get("outro") or "Все подробности —", mid, c)
        plain(im, (W / 2, 710), "в канале KinoMir360", mid, c)
        d = ImageDraw.Draw(im)
        pulse = 1 + 0.03 * math.sin((t - 6.1) * 5)
        bf = font("Montserrat.ttf", int(46 * pulse), b"Bold")
        tw = bf.getbbox(cta)[2]
        bw, bh, y0 = tw + 100 * pulse, 100 * pulse, 850 + 25 * (1 - c)
        d.rounded_rectangle([W / 2 - bw / 2, y0 - bh / 2, W / 2 + bw / 2, y0 + bh / 2], radius=bh / 2,
                            fill=(int(RED[0] * c), int(RED[1] * c), int(RED[2] * c), 255))
        d.text((W / 2, y0), cta, font=bf, fill=WHITE, anchor="mm")
    return im.convert("RGB")


def render(lines: dict[str, str], out: Path, key: str = "tg") -> Path:
    """Тизер 10 с, 1080×1080 по строкам lines (hook, hook_sub, line1, line2, sub, outro); key — tg или max."""
    out.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
         "-i", "-", "-vf", "noise=alls=7:allf=t", "-c:v", "libx264", "-preset", "slow", "-crf", "23",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", str(out)], stdin=subprocess.PIPE)
    for i in range(FPS * SECONDS):
        proc.stdin.write(frame(i, CTA[key], lines).tobytes())
    proc.stdin.close()
    proc.wait()
    return out

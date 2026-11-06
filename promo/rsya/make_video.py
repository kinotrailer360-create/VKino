"""Промо-ролик KinoMir360 для рекламы в мессенджерах: 12 секунд, 1080×1080, без звука, без чужих кадров.

python3 promo/rsya/make_video.py   →  promo/rsya/video/kinomir360_{tg,max}_1x1.mp4
"""
from __future__ import annotations

import math
import random
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[2]
A, OUT = ROOT / "shorts/assets", ROOT / "promo/rsya/video"
W = H = 1080
FPS, SECONDS = 30, 12
ORANGE, WHITE, SOFT = (245, 130, 40), (255, 255, 255), (200, 228, 250)
LOGO = Image.open(A / "logo.png").convert("RGBA")
VARIANTS = {"tg": ("Подписывайтесь в Telegram", "t.me/KinoMir360"),
            "max": ("Подписывайтесь в MAX", "KinoMir360 в MAX")}


def font(name: str, size: int, weight: bytes = b"SemiBold") -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(str(A / name), size)
    try:  # Montserrat — вариативный шрифт, по умолчанию слишком тонкий
        f.set_variation_by_name(weight)
    except (OSError, ValueError):
        pass
    return f


def background() -> Image.Image:
    im = Image.new("RGB", (W, H), (8, 12, 28))
    d = ImageDraw.Draw(im)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=(int(8 + 10 * t), int(14 + 8 * t), int(34 + 12 * t)))
    glow = Image.new("RGB", (W, H))
    g = ImageDraw.Draw(glow)
    g.ellipse([-W * 0.3, -H * 0.4, W * 0.5, H * 0.4], fill=(20, 90, 150))
    g.ellipse([W * 0.55, H * 0.65, W * 1.35, H * 1.45], fill=(150, 70, 15))
    return Image.blend(im, glow.filter(ImageFilter.GaussianBlur(W / 6)), 0.55)


def ease(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def text_center(d: ImageDraw.ImageDraw, y: float, text: str, f, fill, alpha: float) -> None:
    if alpha <= 0:
        return
    col = tuple(int(c * alpha + 10 * (1 - alpha)) for c in fill)
    d.text((W / 2, y), text, font=f, fill=col, anchor="mm")


def frame(i: int, bg: Image.Image, stars: list, cta: str, sub: str) -> Image.Image:
    t = i / FPS
    im = bg.copy()
    d = ImageDraw.Draw(im)
    for x, y, ph in stars:  # мерцающие звёзды
        b = int(140 + 110 * math.sin(t * 3 + ph))
        d.ellipse([x, y, x + 2, y + 2], fill=(b, b, min(255, b + 30)))
    # логотип: появляется с увеличением, к 3-й секунде уезжает вверх
    grow, up = ease(t / 1.2), ease((t - 2.5) / 1.0)
    size = int((560 + 60 * grow) * (1 - 0.42 * up))
    logo = LOGO.resize((size, int(size * LOGO.height / LOGO.width)), Image.LANCZOS)
    if grow < 1:
        logo.putalpha(logo.getchannel("A").point(lambda a: int(a * grow)))
    cy = H / 2 - 40 - (H / 2 - 40 - 200) * up
    im.paste(logo, (int((W - logo.width) / 2), int(cy - logo.height / 2)), logo)
    d = ImageDraw.Draw(im)
    big = font("Oswald.ttf", 112)
    a1, a2 = ease((t - 3.0) / 0.6), ease((t - 3.5) / 0.6)
    text_center(d, 470 + 30 * (1 - a1), "НОВИНКИ КИНО", big, WHITE, a1)
    text_center(d, 595 + 30 * (1 - a2), "КАЖДЫЙ ДЕНЬ", big, ORANGE, a2)
    small = font("Montserrat.ttf", 46)
    for k, word in enumerate(("Трейлеры", "Премьеры", "Подборки")):
        a = ease((t - 5.0 - k * 0.6) / 0.5)
        text_center(d, 720, "", small, SOFT, 0)
        if a > 0:
            x = W / 2 + (k - 1) * 300
            col = tuple(int(c * a + 10 * (1 - a)) for c in SOFT)
            d.text((x, 715 + 20 * (1 - a)), word, font=small, fill=col, anchor="mm")
    a = ease((t - 7.8) / 0.6)
    if a > 0:  # кнопка-призыв, слегка «дышит»
        pulse = 1 + 0.035 * math.sin((t - 7.8) * 5) * a
        f = font("Montserrat.ttf", int(50 * pulse), b"Bold")
        tw = f.getbbox(cta)[2]
        bw, bh = tw + 110 * pulse, 110 * pulse
        y0 = 850 + 30 * (1 - a)
        fill = tuple(int(c * a + 10 * (1 - a)) for c in ORANGE)
        d.rounded_rectangle([W / 2 - bw / 2, y0 - bh / 2, W / 2 + bw / 2, y0 + bh / 2], radius=bh / 2, fill=fill)
        d.text((W / 2, y0), cta, font=f, fill=WHITE, anchor="mm")
        text_center(d, 960, sub, font("Montserrat.ttf", 38), SOFT, a)
    return im


def render(key: str) -> Path:
    cta, sub = VARIANTS[key]
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"kinomir360_{key}_1x1.mp4"
    bg, rnd = background(), random.Random(7)
    stars = [(rnd.randrange(W), rnd.randrange(H), rnd.random() * 6) for _ in range(140)]
    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
         "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "slow", "-crf", "23", "-pix_fmt", "yuv420p",
         "-movflags", "+faststart", "-an", str(out)], stdin=subprocess.PIPE)
    for i in range(FPS * SECONDS):
        proc.stdin.write(frame(i, bg, stars, cta, sub).tobytes())
    proc.stdin.close()
    proc.wait()
    return out


if __name__ == "__main__":
    for k in VARIANTS:
        p = render(k)
        print(p, f"{p.stat().st_size / 1e6:.1f} МБ")

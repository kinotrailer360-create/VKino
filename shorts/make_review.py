"""Сборка горизонтального обзора фильма KinoMir360 (16:9, 1280×720) из кадров, фрагментов трейлера,
голоса, субтитров и музыки.

Запуск:  python -m shorts.make_review work/review.json
Конфиг собирает shorts/review.py: кадры (shots) уже нарезаны по времени голоса, плашки разделов
(headings) и субтитры (subs) — с временем показа.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from .make_short import (
    FPS,
    RED,
    TG_BLUE,
    YT_RED,
    ClipReader,
    cover,
    draw_tg_icon,
    draw_yt_icon,
    duration,
    ease_out,
    extract_clip,
    font,
    load_logo,
    mix_audio,
    text_center,
    wrap,
    zoom,
)

W, H = 1280, 720
XFADE = 0.35  # смена кадра — мягкий переход, сек
MAX_MB = 46  # Telegram-бот отправляет файлы до 50 МБ — держим запас


# ---------------------------------------------------------------- кадры

class ImageShot:
    """Кадр с медленным наездом (или отъездом) — «эффект Кена Бёрнса»."""

    def __init__(self, path: Path, zoom_in: bool):
        self.base = cover(Image.open(path).convert("RGB"), int(W * 1.1), int(H * 1.1))
        self.zoom_in = zoom_in

    def frame(self, t: float, dur: float) -> Image.Image:
        k = min(max(t / max(dur, 0.1), 0.0), 1.0)
        return zoom(self.base, 1.0 + 0.08 * (k if self.zoom_in else 1 - k), W, H)

    def close(self):
        pass


class ClipShot:
    """Фрагмент трейлера; кадров не осталось (всё тёмное) — запасная картинка."""

    def __init__(self, frames: Path, fallback: ImageShot):
        self.reader = ClipReader(frames)
        self.fallback = fallback

    def frame(self, t: float, dur: float) -> Image.Image:
        img = self.reader.next()
        return img if img is not None else self.fallback.frame(t, dur)

    def close(self):
        self.reader.close()


# ---------------------------------------------------------------- оформление

def gradient(h: int, top_alpha: int, bottom_alpha: int) -> Image.Image:
    g = Image.new("L", (1, h))
    for y in range(h):
        g.putpixel((0, y), round(top_alpha + (bottom_alpha - top_alpha) * y / max(h - 1, 1)))
    layer = Image.new("RGBA", (W, h), (0, 0, 0, 255))
    layer.putalpha(g.resize((W, h)))
    return layer


def subtitle_layer(cfg: dict, text: str) -> Image.Image:
    """Субтитр внизу: до двух строк, белым с обводкой."""
    layer = Image.new("RGBA", (W, 130), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for size in (34, 31, 28, 25):
        f = font(cfg, "sans", size)
        lines = wrap(d, text, f, W - 160)
        if len(lines) <= 2:
            break
    lines = lines[:2]
    y = 130 - 14 - len(lines) * round(size * 1.3)
    for line in lines:
        text_center(d, W / 2, y, line, f, stroke=3)
        y += round(size * 1.3)
    return layer


def heading_layer(cfg: dict, text: str) -> Image.Image:
    """Плашка раздела слева сверху: красная полоса и название."""
    f = font(cfg, "head", 40)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    tw = probe.textlength(text, font=f)
    layer = Image.new("RGBA", (int(tw) + 70, 76), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rectangle((0, 0, layer.width, 76), fill=(0, 0, 0, 170))
    d.rectangle((0, 0, 10, 76), fill=RED)
    d.text((34, 10), text, font=f, fill=(255, 255, 255))
    return layer


def title_layer(cfg: dict, title: dict) -> Image.Image:
    """Заставка первых секунд: «ОБЗОР», название, год/жанр/режиссёр, рейтинги."""
    layer = gradient(H, 0, 235)
    d = ImageDraw.Draw(layer)
    x = 70
    tag_f = font(cfg, "sans", 30)
    tw = d.textlength("ОБЗОР", font=tag_f)
    d.rectangle((x, 300, x + tw + 36, 348), fill=RED)
    d.text((x + 18, 306), "ОБЗОР", font=tag_f, fill=(255, 255, 255))
    for size in (86, 74, 64, 54, 46):
        head_f = font(cfg, "head", size)
        lines = wrap(d, title["name"].upper(), head_f, W - 2 * x)
        if len(lines) <= 2:
            break
    y = 362
    for line in lines[:2]:
        d.text((x, y), line, font=head_f, fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0))
        y += round(size * 1.12)
    if title.get("line"):
        d.text((x, y + 22), title["line"], font=font(cfg, "sans", 28), fill=(225, 225, 225),
               stroke_width=2, stroke_fill=(0, 0, 0))
        y += 66
    bx = x
    badge_f = font(cfg, "sans", 30)
    for label, value, color in title.get("ratings") or []:
        text = f"{label} {value}"
        bw = d.textlength(text, font=badge_f) + 32
        d.rounded_rectangle((bx, y + 10, bx + bw, y + 58), 10, fill=tuple(color))
        d.text((bx + 16, y + 16), text, font=badge_f, fill=(0, 0, 0) if color[0] > 200 else (255, 255, 255))
        bx += bw + 14
    return layer


class Outro:
    def __init__(self, cfg: dict, bg_path: Path, logo: Image.Image):
        bg = cover(Image.open(bg_path).convert("RGB"), int(W * 1.1), int(H * 1.1)).filter(ImageFilter.GaussianBlur(30))
        self.bg = Image.blend(bg, Image.new("RGB", bg.size), 0.6)
        lw = 420
        self.logo = logo.resize((lw, round(logo.height * lw / logo.width)), Image.LANCZOS)
        ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(ov)
        text_center(d, W / 2, 330, "ПОДПИШИСЬ", font(cfg, "head", 72), fill=(255, 196, 0), stroke=3)
        btn_f = font(cfg, "sans", 34)
        for i, (label, icon, col, handle) in enumerate((("YouTube", draw_yt_icon, YT_RED, "@kinomir360"),
                                                         ("Telegram", draw_tg_icon, TG_BLUE, "@KinoMir360"))):
            bx, by = W / 2 - 520 + i * 540, 450
            d.rounded_rectangle((bx, by, bx + 500, by + 96), 48, fill=(255, 255, 255, 235))
            icon(d, bx + 28, by + 16, 64)
            d.text((bx + 110, by + 26), label, font=btn_f, fill=(20, 20, 20))
            d.text((bx + 120 + d.textlength(label + " ", font=btn_f), by + 26), handle, font=btn_f, fill=col)
        text_center(d, W / 2, 600, "ссылки в описании", font(cfg, "sans", 28), fill=(230, 230, 230), stroke=2)
        self.overlay = ov

    def frame(self, t: float, dur: float) -> Image.Image:
        img = zoom(self.bg, 1.0 + 0.04 * t / max(dur, 1), W, H)
        img.paste(self.overlay, (0, 0), self.overlay)
        s = 0.7 + 0.3 * ease_out(t / 0.6)
        lg = self.logo.resize((round(self.logo.width * s), round(self.logo.height * s)), Image.BILINEAR)
        img.paste(lg, ((W - lg.width) // 2, 190 - lg.height // 2), lg)
        return img


# ---------------------------------------------------------------- рендер

def _active(items: list[dict], t: float) -> int | None:
    for k, it in enumerate(items):
        if it["start"] <= t < it["end"]:
            return k
    return None


def render(cfg: dict, base: Path) -> Path:
    def p(x):
        return (base / x).resolve()

    cfg["fonts"] = {k: p(v) for k, v in cfg["fonts"].items()}
    voice, music, out = p(cfg["voice"]), p(cfg["music"]), p(cfg["output"])
    logo = load_logo(p(cfg["logo"]))
    shots = cfg["shots"]
    speech_end = duration(voice)
    outro_len = cfg.get("outro_len", 4.5)
    total = speech_end + outro_len
    shots[-1]["end"] = speech_end  # последний кадр — до конца речи, дальше концовка

    # кадры трейлеров — на диск заранее, пока кодировщик ещё не запущен (так меньше пик памяти)
    for k, s in enumerate(shots):
        if s.get("clip") and p(s["clip"]).exists():
            s["frames"] = str(extract_clip(p(s["clip"]), W, H, p(s["clip"]).with_suffix(f".frames{k}")))

    def make_shot(k: int):
        s = shots[k]
        still = ImageShot(p(s["image"]), zoom_in=k % 2 == 0)
        return ClipShot(Path(s["frames"]), still) if s.get("frames") else still

    loaded: dict[int, object] = {}

    def shot(k: int):
        if k not in loaded:
            for old in [j for j in loaded if j < k - 1]:
                loaded.pop(old).close()
            loaded[k] = make_shot(k)
        return loaded[k]

    subs, headings = cfg.get("subs") or [], cfg.get("headings") or []
    sub_cache: dict[int, Image.Image] = {}
    head_cache: dict[int, Image.Image] = {}
    bottom = gradient(200, 0, 190)
    title = cfg.get("title")
    title_img = title_layer(cfg, title) if title else None
    title_end = title["end"] if title else 0.0
    outro = Outro(cfg, p(cfg["outro_bg"]), logo)

    wm_w = 120
    wm = logo.resize((wm_w, round(logo.height * wm_w / logo.width)), Image.LANCZOS)
    wm.putalpha(wm.getchannel("A").point(lambda a: int(a * 0.8)))

    out.parent.mkdir(parents=True, exist_ok=True)
    audio = out.with_suffix(".m4a")
    sfx = [(float(t), p(f), float(db)) for t, f, db in cfg.get("sfx") or []]
    mix_audio(voice, music, total, audio, cfg.get("music_db", -18), sfx)

    # битрейт под лимит Telegram: весь файл не больше MAX_MB
    kbps = int(min(2600, max(700, MAX_MB * 8 * 1024 / total - 200)))
    log_path = out.with_suffix(".ffmpeg.log")
    log = open(log_path, "wb")
    enc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
         "-i", "-", "-i", str(audio), "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "veryfast",
         "-threads", "1", "-x264-params", "rc-lookahead=10:sync-lookahead=0:bframes=0:ref=2",
         "-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.3)}k", "-bufsize", f"{kbps * 2}k",
         "-pix_fmt", "yuv420p", "-c:a", "copy", "-shortest", "-movflags", "+faststart", str(out)],
        stdin=subprocess.PIPE, stderr=log)

    def encoder_failed() -> RuntimeError:
        enc.wait()
        log.close()
        tail = log_path.read_text(errors="ignore")[-600:].strip()
        return RuntimeError(f"ffmpeg завершился с кодом {enc.returncode} "
                            f"(-9 — не хватило памяти): {tail or 'нет вывода'}")

    n_frames = round(total * FPS)
    for f in range(n_frames):
        t = f / FPS
        if t >= speech_end:
            img = outro.frame(t - speech_end, outro_len)
            if t - speech_end < XFADE:
                k = len(shots) - 1
                prev = shot(k).frame(t - shots[k]["start"], shots[k]["end"] - shots[k]["start"])
                img = Image.blend(prev, img, ease_out((t - speech_end) / XFADE))
        else:
            k = max(j for j, s in enumerate(shots) if s["start"] <= t)
            s = shots[k]
            img = shot(k).frame(t - s["start"], s["end"] - s["start"])
            if k > 0 and t - s["start"] < XFADE:
                ps = shots[k - 1]
                prev = shot(k - 1).frame(t - ps["start"], ps["end"] - ps["start"])
                img = Image.blend(prev, img, ease_out((t - s["start"]) / XFADE))
            if title_img is not None and t < title_end:
                fade = min(1.0, (title_end - t) / 0.5, t / 0.4 + 0.3)
                layer = title_img if fade >= 1 else title_img.copy()
                if fade < 1:
                    layer.putalpha(layer.getchannel("A").point(lambda a, k=fade: int(a * k)))
                img.paste(layer, (0, 0), layer)
            h = _active(headings, t)
            if h is not None:
                if h not in head_cache:
                    head_cache[h] = heading_layer(cfg, headings[h]["text"])
                hl = head_cache[h]
                slide = ease_out((t - headings[h]["start"]) / 0.4)
                img.paste(hl, (round(-hl.width + (40 + hl.width) * slide), 40), hl)
            j = _active(subs, t)
            if j is not None and t >= title_end:
                if j not in sub_cache:
                    sub_cache.clear()  # держим в памяти только текущий субтитр
                    sub_cache[j] = subtitle_layer(cfg, subs[j]["text"])
                img.paste(bottom, (0, H - bottom.height), bottom)
                img.paste(sub_cache[j], (0, H - 140), sub_cache[j])
            img.paste(wm, (W - wm_w - 28, 24), wm)
        try:
            enc.stdin.write(img.tobytes())
        except BrokenPipeError:
            raise encoder_failed() from None
        if f % (FPS * 2) == 0:  # бот читает эти строки и двигает полоску загрузки
            print(f"PROGRESS {f / n_frames:.3f}  {t:5.1f}/{total:.1f} c", flush=True)
    for sh in loaded.values():
        sh.close()
    enc.stdin.close()
    if enc.wait():
        raise encoder_failed()
    log.close()
    log_path.unlink(missing_ok=True)
    audio.unlink(missing_ok=True)
    print("Готово:", out)
    return out


def main(cfg_path: str):
    path = Path(cfg_path).resolve()
    render(json.loads(path.read_text(encoding="utf-8")), path.parent)


if __name__ == "__main__":
    main(sys.argv[1])

"""Сборка вертикального шортса KinoMir360 из обложки, карточек, голоса и музыки.

Запуск:  python -m shorts.make_short shorts/examples/premieres.json
Зависимости: Pillow, ffmpeg/ffprobe в PATH.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageStat

W, H = 1080, 1920
FPS = 30
XFADE = 0.2  # длительность перехода между слайдами, сек
RED = (229, 9, 20)
TG_BLUE = (42, 171, 238)
YT_RED = (255, 0, 0)

HERE = Path(__file__).resolve().parent


# ---------------------------------------------------------------- utils

def ease_out(t: float) -> float:
    t = min(max(t, 0.0), 1.0)
    return 1 - (1 - t) ** 3


def font(cfg: dict, kind: str, size: int) -> ImageFont.FreeTypeFont:
    path = cfg["fonts"][kind]
    f = ImageFont.truetype(str(path), size)
    try:
        f.set_variation_by_name("Bold")
    except (OSError, ValueError):
        pass
    return f


def cover(img: Image.Image, w: int, h: int) -> Image.Image:
    """Масштабировать с заполнением и обрезкой по центру."""
    s = max(w / img.width, h / img.height)
    img = img.resize((math.ceil(img.width * s), math.ceil(img.height * s)), Image.LANCZOS)
    x, y = (img.width - w) // 2, (img.height - h) // 2
    return img.crop((x, y, x + w, y + h))


def zoom(img: Image.Image, scale: float, out_w: int, out_h: int) -> Image.Image:
    """Кроп по центру с увеличением scale (>=1) и приведение к out размеру."""
    cw, ch = img.width / scale, img.height / scale
    x, y = (img.width - cw) / 2, (img.height - ch) / 2
    return img.resize((out_w, out_h), Image.BILINEAR, box=(x, y, x + cw, y + ch))


def text_center(draw, cx, y, text, fnt, fill=(255, 255, 255), stroke=0, stroke_fill=(0, 0, 0)):
    box = draw.textbbox((0, 0), text, font=fnt, stroke_width=stroke)
    draw.text((cx - (box[2] - box[0]) / 2 - box[0], y), text, font=fnt, fill=fill,
              stroke_width=stroke, stroke_fill=stroke_fill)
    return box[3] - box[1]


def wrap(draw, text, fnt, max_w):
    """Перенос по словам; "\n" в тексте — принудительный перенос."""
    lines = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split():
            cand = f"{cur} {word}".strip()
            if draw.textlength(cand, font=fnt) <= max_w or not cur:
                cur = cand
            else:
                lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
    return lines


def load_logo(path: Path) -> Image.Image:
    """Логотип с прозрачностью. Если фон чёрный (JPG) — убираем его заливкой от краёв."""
    img = Image.open(path)
    if img.mode in ("RGBA", "LA") or "transparency" in img.info:
        img = img.convert("RGBA")
        return img.crop(img.getbbox())
    img = img.convert("RGB")
    mask = img.convert("L").point(lambda v: 255 if v < 28 else 0)
    for xy in ((0, 0), (img.width - 1, 0), (0, img.height - 1), (img.width - 1, img.height - 1)):
        if mask.getpixel(xy) == 255:
            ImageDraw.floodfill(mask, xy, 128)
    alpha = mask.point(lambda v: 0 if v == 128 else 255).filter(ImageFilter.GaussianBlur(1.2))
    img = img.convert("RGBA")
    img.putalpha(alpha)
    return img.crop(img.getbbox())


# ---------------------------------------------------------------- timing

def speech_gaps(audio: Path) -> list[tuple[float, float]]:
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(audio), "-af", "silencedetect=n=-40dB:d=0.25", "-f", "null", "-"],
        capture_output=True, text=True).stderr
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", out)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", out)]
    return list(zip(starts, ends))


def duration(path: Path) -> float:
    return float(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]).strip())


def slide_starts(cfg: dict, voice: Path, n_slides: int) -> list[float]:
    """Начало каждого слайда: паузы в голосе, ближайшие к ожидаемым по длине текста."""
    if cfg.get("starts"):
        return cfg["starts"]
    texts = cfg["voice_segments"]
    assert len(texts) == n_slides, "voice_segments должно совпадать с числом слайдов"
    total = duration(voice)
    gaps = speech_gaps(voice)
    speech_end = gaps[-1][0] if gaps and gaps[-1][1] >= total - 0.05 else total
    chars = [len(t) for t in texts]
    starts, used, acc = [0.0], -1, 0
    for i in range(1, n_slides):
        acc += chars[i - 1]
        expected = speech_end * acc / sum(chars)
        cands = [(abs(g[1] - expected), j) for j, g in enumerate(gaps) if j > used and g[1] - g[0] >= 0.3]
        _, used = min(cands)
        starts.append(max(gaps[used][1] - 0.12, starts[-1] + 0.5))
    return starts


# ---------------------------------------------------------------- slides

class Slide:
    def frame(self, t: float, dur: float) -> Image.Image:  # t — время от начала слайда
        raise NotImplementedError


class IntroSlide(Slide):
    def __init__(self, cfg, path):
        self.base = cover(Image.open(path).convert("RGB"), int(W * 1.08), int(H * 1.08))

    def frame(self, t, dur):
        return zoom(self.base, 1.0 + 0.07 * t / max(dur, 1), W, H)


DARK = 22  # средняя яркость кадра (0–255), ниже — считаем чёрным/затемнением


def _dark_or_titles(gray: Image.Image) -> bool:
    """Чёрный кадр/затемнение или титры — светлые буквы на почти чёрном фоне («ОШЕЛОМЛЯЮЩИЙ…», «скоро»)."""
    if ImageStat.Stat(gray).mean[0] < DARK:
        return True
    px = list(gray.getdata())
    black = sum(v < 35 for v in px) / len(px)
    bright = sum(v > 190 for v in px) / len(px)
    return black > 0.6 and bright > 0.02


def extract_clip(path: Path, w: int, h: int, out_dir: Path) -> Path:
    """Кадры трейлера в JPEG на диск заранее, до запуска кодировщика.

    Раньше кадры шли из ffmpeg прямо во время монтажа: два живых декодера (текущий и прошлый слайд)
    держали ~150 МБ, и вместе с ботом сервер на 0,5 ГБ падал по памяти.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-threads", "1", "-filter_threads", "1", "-i", str(path), "-an",
         "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps={FPS}",
         "-q:v", "3", str(out_dir / "f%04d.jpg")],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # чёрные кадры и затемнения между сценами трейлера выкидываем; почти всё тёмное — без видео
    frames = sorted(out_dir.glob("f*.jpg"))
    dark = []
    for f in frames:
        with Image.open(f) as im:
            if _dark_or_titles(im.convert("L").resize((96, 54))):
                dark.append(f)
    for f in (frames if len(frames) - len(dark) < FPS else dark):
        f.unlink(missing_ok=True)
    return out_dir


class ClipReader:
    """Кадры трейлера по одному из заранее нарезанных JPEG, по кругу."""

    def __init__(self, frames_dir: Path):
        self.frames = sorted(frames_dir.glob("f*.jpg"))
        self.k = 0
        self.last: Image.Image | None = None

    def next(self) -> Image.Image | None:
        if self.frames:
            with Image.open(self.frames[self.k % len(self.frames)]) as im:
                self.last = im.convert("RGB")
            self.k += 1
        return self.last  # кадров нет — держим последний (или None — тогда статичная карточка)

    def close(self):
        self.frames = []


class CardSlide(Slide):
    def __init__(self, cfg, path, idx, total, caption, info=None, karaoke=None, clip=None, overlay=None):
        card = Image.open(path).convert("RGB")
        card = card.resize((W, round(card.height * W / card.width)), Image.LANCZOS)
        self.card = card
        # фрагмент трейлера под оформлением карточки (рейтинг, №, название)
        self.clip = self.card_overlay = None
        if clip and overlay and Path(clip).is_dir() and Path(overlay).exists():
            self.card_overlay = Image.open(overlay).convert("RGBA").resize(card.size)
            self.clip = ClipReader(Path(clip))
        bg = cover(card, int(W * 1.1), int(H * 1.1)).filter(ImageFilter.GaussianBlur(40))
        self.bg = Image.blend(bg, Image.new("RGB", bg.size), 0.55)
        self.card_y = 380

        # статичный слой с заголовком, подписью и прогрессом
        ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(ov)
        tag_f = font(cfg, "sans", 40)
        tag = cfg.get("tag", "НОВИНКИ")
        tw = d.textlength(tag, font=tag_f)
        d.rectangle((W / 2 - tw / 2 - 30, 165, W / 2 + tw / 2 + 30, 235), fill=RED)
        text_center(d, W / 2, 173, tag, tag_f)
        header = cfg.get("header", "")
        for size in (84, 72, 62, 54):  # длинное название фильма — шрифт поменьше
            head_f = font(cfg, "head", size)
            if d.textlength(header, font=head_f) <= W - 80:
                break
        text_center(d, W / 2, 250 + (84 - size) // 2, header, head_f, stroke=3)

        y = self.card_y + card.height + 26
        self.karaoke = None
        if caption:
            # факт/новость (караоке) — крупно, чтобы читалось с телефона; длинный — шрифт поменьше
            sizes, max_lines = ((60, 54, 48, 42), 4) if karaoke else ((46, 42, 38), 5)
            for size in sizes:
                cap_f = font(cfg, "sans", size)
                lines = wrap(d, caption, cap_f, W - 100)
                if len(lines) <= max_lines:
                    break
            if karaoke:
                # караоке: слова рисуются каждый кадр, сказанные — жёлтым
                words = []
                for line in lines:
                    x = (W - d.textlength(line, font=cap_f)) / 2
                    for word in line.split(" "):
                        words.append((x, y, word))
                        x += d.textlength(word + " ", font=cap_f)
                    y += round(size * 1.3)
                self.karaoke = (words, cap_f, karaoke.get("words") or [], karaoke.get("speech") or 1.0)
            else:
                for line in lines:
                    text_center(d, W / 2, y, line, cap_f, stroke=3)
                    y += round(size * 1.3)
        # строки «подпись: значение • подпись: значение» — даты выхода, бюджет, сборы
        for row in info or []:
            pieces = []
            for k, (label, value) in enumerate(row):
                if k:
                    pieces.append(("  •  ", (170, 170, 170)))
                pieces += [(f"{label}: ", (255, 196, 0)), (value, (255, 255, 255))]
            for size in (38, 34, 30, 27):  # длинная строка — шрифт поменьше
                info_f = font(cfg, "sans", size)
                width = sum(d.textlength(t, font=info_f) for t, _ in pieces)
                if width <= W - 50:
                    break
            x = (W - width) / 2
            for t, col in pieces:
                d.text((x, y), t, font=info_f, fill=col, stroke_width=2, stroke_fill=(0, 0, 0))
                x += d.textlength(t, font=info_f)
            y += 54

        # прогресс: total сегментов, заполнены до idx
        seg_w, gap, y = 70, 12, 110
        x0 = (W - (total * seg_w + (total - 1) * gap)) / 2
        for k in range(total):
            x = x0 + k * (seg_w + gap)
            d.rounded_rectangle((x, y, x + seg_w, y + 10), 5,
                                fill=RED if k <= idx else (255, 255, 255, 90))
        self.overlay = ov

    def frame(self, t, dur):
        img = zoom(self.bg, 1.0 + 0.06 * t / max(dur, 1), W, H)
        s = 1.12 - 0.12 * ease_out(t / 0.45) + 0.03 * t / max(dur, 1)
        cw, ch = round(W * s), round(self.card.height * s)
        source = self.card
        if self.clip:
            video = self.clip.next()
            if video is not None:
                source = video.convert("RGBA")
                source.alpha_composite(self.card_overlay)
                source = source.convert("RGB")
        card = source.resize((cw, ch), Image.BILINEAR)
        cx, cy = (W - cw) // 2, self.card_y + (self.card.height - ch) // 2
        img.paste(card, (cx, cy))
        img.paste(self.overlay, (0, 0), self.overlay)
        if self.karaoke:
            self._draw_karaoke(img, t)
        return img

    def close(self):
        if self.clip:
            self.clip.close()
            self.clip = None

    def _draw_karaoke(self, img, t):
        words, fnt, spoken, speech = self.karaoke
        if spoken:
            # время слов из синтеза; слов на экране может быть чуть больше/меньше (IMAX -> Аймакс)
            said = sum(1 for w in spoken if w <= t)
            done = round(said * len(words) / len(spoken))
        else:
            done = int(len(words) * min(max(t / speech, 0), 1) + 0.5)
        d = ImageDraw.Draw(img)
        for k, (x, y, word) in enumerate(words):
            col = (255, 214, 0) if k < done else (255, 255, 255)
            d.text((x, y), word, font=fnt, fill=col, stroke_width=3, stroke_fill=(0, 0, 0))


def draw_yt_icon(d, x, y, s):
    d.rounded_rectangle((x, y + s * 0.15, x + s, y + s * 0.85), s * 0.18, fill=YT_RED)
    d.polygon([(x + s * 0.4, y + s * 0.33), (x + s * 0.4, y + s * 0.67), (x + s * 0.7, y + s * 0.5)], fill="white")


def draw_tg_icon(d, x, y, s):
    d.ellipse((x, y, x + s, y + s), fill=TG_BLUE)
    d.polygon([(x + s * 0.22, y + s * 0.5), (x + s * 0.76, y + s * 0.28),
               (x + s * 0.66, y + s * 0.74), (x + s * 0.45, y + s * 0.6)], fill="white")
    d.polygon([(x + s * 0.45, y + s * 0.6), (x + s * 0.42, y + s * 0.75), (x + s * 0.53, y + s * 0.66)],
              fill=(200, 228, 245))


class OutroSlide(Slide):
    def __init__(self, cfg, bg_path, logo):
        bg = cover(Image.open(bg_path).convert("RGB"), int(W * 1.1), int(H * 1.1))
        bg = bg.filter(ImageFilter.GaussianBlur(45))
        self.bg = Image.blend(bg, Image.new("RGB", bg.size), 0.65)
        lw = 760
        self.logo = logo.resize((lw, round(logo.height * lw / logo.width)), Image.LANCZOS)

        ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(ov)
        q_f = font(cfg, "head", 70)
        y = 210
        for line in wrap(d, cfg["outro"]["question"], q_f, W - 120):
            text_center(d, W / 2, y, line, q_f, stroke=3)
            y += 92

        y = 1180
        text_center(d, W / 2, y, "ПОДПИШИСЬ", font(cfg, "head", 96), fill=(255, 196, 0), stroke=3)
        btn_f = font(cfg, "sans", 52)
        name = cfg["outro"].get("channel", "KinoMir360")
        self.buttons = []
        for i, (label, icon, col, key) in enumerate((("YouTube", draw_yt_icon, YT_RED, "youtube"),
                                                     ("Telegram", draw_tg_icon, TG_BLUE, "telegram"))):
            handle = cfg["outro"].get(key, name)
            btn = Image.new("RGBA", (860, 140), (0, 0, 0, 0))
            bd = ImageDraw.Draw(btn)
            bd.rounded_rectangle((0, 0, 859, 139), 70, fill=(255, 255, 255, 235))
            icon(bd, 40, 25, 90)
            bd.text((160, 36), f"{label}", font=btn_f, fill=(20, 20, 20))
            lx = 160 + bd.textlength(label + "  ", font=btn_f)
            bd.text((lx, 36), handle, font=btn_f, fill=col)
            self.buttons.append(btn)
        sub_f = font(cfg, "sans", 40)
        text_center(d, W / 2, 1690, cfg["outro"].get("note", "ссылки в описании"), sub_f,
                    fill=(230, 230, 230), stroke=2)
        self.overlay = ov

    def frame(self, t, dur):
        img = zoom(self.bg, 1.0 + 0.05 * t / max(dur, 1), W, H)
        img.paste(self.overlay, (0, 0), self.overlay)
        s = (0.6 + 0.4 * ease_out(t / 0.6)) * (1 + 0.025 * math.sin(t * 3))
        lg = self.logo.resize((round(self.logo.width * s), round(self.logo.height * s)), Image.BILINEAR)
        img.paste(lg, ((W - lg.width) // 2, 760 - lg.height // 2), lg)
        for i, btn in enumerate(self.buttons):
            k = ease_out((t - 0.4 - 0.25 * i) / 0.5)
            if k <= 0:
                continue
            x = round((W - btn.width) / 2 + (1 - k) * (W if i == 0 else -W))
            img.paste(btn, (x, 1330 + i * 170), btn)
        return img


# ---------------------------------------------------------------- субтитры по одному слову

class WordCaptions:
    """Крупное слово по центру — то, что голос говорит прямо сейчас (как в TikTok/Shorts)."""

    def __init__(self, cfg: dict, words: list[dict], y: int, width: int = W):
        self.cfg, self.words, self.y, self.width = cfg, words, y, width
        self.k = 0
        self.cache: dict[int, Image.Image] = {}

    def _render(self, text: str) -> Image.Image:
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        for size in (76, 68, 60, 52, 46):  # не крупно — картинку под словом видно
            f = font(self.cfg, "sans", size)
            if probe.textlength(text, font=f) <= self.width - 100:
                break
        box = probe.textbbox((0, 0), text, font=f, stroke_width=5)
        w, h = box[2] - box[0] + 30, box[3] - box[1] + 30
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).text((15 - box[0] + 5, 15 - box[1] + 7), text, font=f, fill=(0, 0, 0, 170),
                                    stroke_width=5, stroke_fill=(0, 0, 0, 170))
        layer.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(5)))
        ImageDraw.Draw(layer).text((15 - box[0], 15 - box[1]), text, font=f, fill=(255, 184, 28),  # жёлто-оранжевый
                                   stroke_width=5, stroke_fill=(0, 0, 0))
        return layer

    def draw(self, img: Image.Image, t: float) -> None:
        words = self.words
        while self.k + 1 < len(words) and words[self.k + 1]["s"] <= t:
            self.k += 1
        if not words or t < words[self.k]["s"]:
            return
        cur = words[self.k]
        nxt = words[self.k + 1]["s"] if self.k + 1 < len(words) else cur["e"] + 0.4
        if t > max(cur["e"] + 0.35, cur["s"] + 0.25) and t < nxt:  # пауза в речи — слово гаснет
            return
        if self.k not in self.cache:
            self.cache = {self.k: self._render(cur["w"].strip(" ,.!?:;«»\"—"))}
        layer = self.cache[self.k]
        pop = 0.86 + 0.14 * ease_out((t - cur["s"]) / 0.09)  # слово чуть «выпрыгивает»
        if pop < 0.999:
            layer = layer.resize((max(1, round(layer.width * pop)), max(1, round(layer.height * pop))),
                                 Image.BILINEAR)
        img.paste(layer, ((img.width - layer.width) // 2, self.y - layer.height // 2), layer)


# ---------------------------------------------------------------- render

# голос: без гула снизу, меньше «бубнения» на 250 Гц, разборчивость на 3 кГц, ровная громкость
VOICE_CHAIN = ("highpass=f=90,equalizer=f=250:t=q:w=1:g=-2,equalizer=f=3200:t=q:w=1.2:g=4,"
               "acompressor=threshold=0.1:ratio=3:attack=5:release=150:makeup=2,loudnorm=I=-16:TP=-2:LRA=7")


def mix_audio(voice: Path, music: Path, total: float, out: Path, music_db: float,
              sfx: list[tuple[float, Path, float]] | None = None):
    """Голос (с обработкой для разборчивости) + музыка (заметно тише голоса и приглушается под него)
    + звуковые эффекты (время, файл, громкость дБ)."""
    fade = max(total - 2.0, 0)
    sfx = [(t, f, db) for t, f, db in sfx or [] if 0 <= t < total and f.exists()]
    inputs = ["-i", str(voice), "-i", str(music)]
    filt = (
        f"[1:a]volume={music_db}dB,atrim=0:{total},afade=t=in:d=0.5,afade=t=out:st={fade}:d=2[m];"
        f"[0:a]aresample=48000,{VOICE_CHAIN},apad=whole_dur={total},asplit=2[v][sc];"
        f"[m][sc]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=350[md];"
    )
    for k, (t, f, db) in enumerate(sfx):
        inputs += ["-i", str(f)]
        ms = round(t * 1000)
        filt += f"[{k + 2}:a]aresample=48000,volume={db}dB,adelay={ms}|{ms}[s{k}];"
    mix = "[v][md]" + "".join(f"[s{k}]" for k in range(len(sfx)))
    filt += f"{mix}amix=inputs={2 + len(sfx)}:normalize=0:duration=first,loudnorm=I=-14:TP=-1.5:LRA=11[a]"
    subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs,
                    "-filter_complex", filt, "-map", "[a]", "-ar", "48000", "-c:a", "aac", "-b:a", "192k",
                    str(out)], check=True)


def render(cfg: dict, base: Path) -> Path:
    """Собрать видео по конфигу; относительные пути считаются от base."""
    def p(x):
        return (base / x).resolve()

    cfg["fonts"] = {k: p(v) for k, v in cfg["fonts"].items()}
    voice, music, out = p(cfg["voice"]), p(cfg["music"]), p(cfg["output"])
    logo = load_logo(p(cfg["logo"]))

    cards = cfg["cards"]
    # кадры трейлеров — на диск заранее, пока кодировщик ещё не запущен (так меньше пик памяти)
    clip_dirs: dict[int, Path] = {}
    for i, c in enumerate(cards):
        if c.get("clip") and p(c["clip"]).exists():
            with Image.open(p(c["image"])) as im:
                ch = round(im.height * W / im.width)
            clip_dirs[i] = extract_clip(p(c["clip"]), W, ch, p(c["clip"]).with_suffix(".frames"))
    # слайды создаются по мере надобности и выгружаются после показа — экономим память
    makers = [lambda: IntroSlide(cfg, p(cfg["intro"]))]
    makers += [lambda i=i, c=c: CardSlide(cfg, p(c["image"]), i, len(cards), c.get("caption", ""), c.get("info"),
                                          c.get("karaoke"),
                                          clip_dirs.get(i),
                                          p(c["overlay"]) if c.get("overlay") else None)
               for i, c in enumerate(cards)]
    makers.append(lambda: OutroSlide(cfg, p(cfg["intro"]), logo))
    loaded: dict[int, Slide] = {}

    def slide(k: int) -> Slide:
        if k not in loaded:
            for old in [j for j in loaded if j < k - 1]:
                getattr(loaded.pop(old), "close", lambda: None)()
            loaded[k] = makers[k]()
        return loaded[k]

    starts = slide_starts(cfg, voice, len(makers))
    total = duration(voice) + cfg.get("tail", 1.5)
    ends = starts[1:] + [total]
    print("Слайды (сек):", ", ".join(f"{s:.2f}" for s in starts), f"| всего {total:.2f}")

    # водяной знак
    wm_w = cfg.get("watermark_width", 150)
    wm = logo.resize((wm_w, round(logo.height * wm_w / logo.width)), Image.LANCZOS)
    wm.putalpha(wm.getchannel("A").point(lambda a: int(a * cfg.get("watermark_opacity", 0.85))))
    wm_pos = (W - wm_w - 36, 40)
    # середина картинки на карточке: ниже — название, актёры, даты, а низ экрана закрывают подписи YouTube/TikTok
    captions = WordCaptions(cfg, cfg["words"], cfg.get("word_y", 880)) if cfg.get("words") else None
    word_y = captions.y if captions else 0

    out.parent.mkdir(parents=True, exist_ok=True)
    audio = out.with_suffix(".m4a")
    sfx = [(float(t), p(f), float(db)) for t, f, db in cfg.get("sfx") or []]
    mix_audio(voice, music, total, audio, cfg.get("music_db", -14), sfx)

    # настройки x264 под маленький сервер: один поток и короткий lookahead — мало памяти
    log_path = out.with_suffix(".ffmpeg.log")
    log = open(log_path, "wb")
    enc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
         "-i", "-", "-i", str(audio), "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
         "-threads", "1", "-x264-params", "rc-lookahead=0:sync-lookahead=0:bframes=0:ref=2",
         "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "copy", "-shortest", "-movflags", "+faststart", str(out)],
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
        i = max(k for k, s in enumerate(starts) if s <= t)
        dur = ends[i] - starts[i]
        img = slide(i).frame(t - starts[i], dur)
        # переход: предыдущий слайд гаснет поверх нового
        if i > 0 and t - starts[i] < XFADE:
            prev = slide(i - 1).frame(t - starts[i - 1], ends[i - 1] - starts[i - 1])
            img = Image.blend(prev, img, ease_out((t - starts[i]) / XFADE))
        if i < len(makers) - 1:
            img.paste(wm, wm_pos, wm)
            if captions:  # на концовке с кнопками подписки слова не нужны
                # на обложке внизу текст обложки — слово выше; на карточках — в полосе под картинкой
                captions.y = 1120 if i == 0 else word_y
                captions.draw(img, t)
        try:
            enc.stdin.write(img.tobytes())
        except BrokenPipeError:
            raise encoder_failed() from None
        if f % (FPS * 2) == 0:  # бот читает эти строки и двигает полоску загрузки
            print(f"PROGRESS {f / n_frames:.3f}  {t:5.1f}/{total:.1f} c", flush=True)
    for sl in loaded.values():
        getattr(sl, "close", lambda: None)()
    enc.stdin.close()
    if enc.wait():
        raise encoder_failed()
    log.close()
    log_path.unlink(missing_ok=True)
    audio.unlink(missing_ok=True)
    print("Готово:", out)
    return out


def main(cfg_path: str):
    cfg_path = Path(cfg_path).resolve()
    render(json.loads(cfg_path.read_text(encoding="utf-8")), cfg_path.parent)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else str(HERE / "examples" / "premieres.json"))

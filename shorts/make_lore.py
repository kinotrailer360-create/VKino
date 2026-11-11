"""Сборка вертикального шортса «история персонажа»: картинки (страницы комиксов, кадры), по которым
медленно едет камера, крупные субтитры по одному слову, голос и музыка.

Запуск:  python -m shorts.make_lore work/lore.json  (конфиг собирает shorts/lore.py)
"""

from __future__ import annotations

import json
import random
import re
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from .make_short import (
    FPS,
    H,
    OutroSlide,
    W,
    WordCaptions,
    duration,
    ease_out,
    font,
    load_logo,
    mix_audio,
)

XFADE = 0.25
MARGIN = 1.12  # картинка чуть больше кадра — есть куда ехать камере


class PanSlide:
    """Картинка на весь кадр; камера едет вдоль длинной стороны (страница комикса — сверху вниз)."""

    def __init__(self, path: Path, reverse: bool):
        img = Image.open(path).convert("RGB")
        s = max(W / img.width, H / img.height) * MARGIN
        self.img = img.resize((max(W, round(img.width * s)), max(H, round(img.height * s))), Image.LANCZOS)
        self.dx, self.dy = self.img.width - W, self.img.height - H
        self.reverse = reverse

    def frame(self, t: float, dur: float) -> Image.Image:
        k = min(max(t / max(dur, 0.1), 0.0), 1.0)
        k = k * k * (3 - 2 * k)  # плавно разгоняется и тормозит
        if self.reverse:
            k = 1 - k
        if self.dy >= self.dx:  # высокая картинка — вниз, по центру по горизонтали
            x, y = self.dx // 2, round(self.dy * k)
        else:  # широкая — вбок
            x, y = round(self.dx * k), self.dy // 2
        return self.img.crop((x, y, x + W, y + H))


FIT_H = 860  # высота широкого кадра в вертикальном ролике: почти весь кадр 16:9 виден, по бокам срезано немного


class FitSlide:
    """Широкий кадр (16:9) — целиком по центру, сверху и снизу его же размытая копия; камера медленно наезжает
    (до +6 %). Раньше широкий кадр растягивался на весь экран 9:16: от него оставалась узкая середина, а камера
    быстро ехала вбок — «фото бегают, главного в кадре мало»."""

    ZOOM = 0.06

    def __init__(self, path: Path, reverse: bool):
        img = Image.open(path).convert("RGB")
        bg = img.copy()
        s = max(W / bg.width, H / bg.height)
        bg = bg.resize((round(bg.width * s) + 1, round(bg.height * s) + 1), Image.LANCZOS)
        bg = bg.crop(((bg.width - W) // 2, (bg.height - H) // 2, (bg.width - W) // 2 + W, (bg.height - H) // 2 + H))
        self.bg = ImageEnhance.Brightness(bg.filter(ImageFilter.GaussianBlur(28))).enhance(0.55)
        top = max(W / img.width, FIT_H / img.height) * (1 + self.ZOOM)
        self.fg = img.resize((round(img.width * top) + 1, round(img.height * top) + 1), Image.LANCZOS)
        self.reverse = reverse

    def frame(self, t: float, dur: float) -> Image.Image:
        k = min(max(t / max(dur, 0.1), 0.0), 1.0)
        k = k * k * (3 - 2 * k)
        z = 1 + self.ZOOM * ((1 - k) if self.reverse else k)  # плавный наезд (или отъезд)
        scale = z / (1 + self.ZOOM)
        fw, fh = round(self.fg.width * scale), round(self.fg.height * scale)
        fg = self.fg.resize((fw, fh), Image.BILINEAR)
        cw, ch = min(W, fw), min(FIT_H, fh)
        fg = fg.crop(((fw - cw) // 2, (fh - ch) // 2, (fw - cw) // 2 + cw, (fh - ch) // 2 + ch))
        img = self.bg.copy()
        img.paste(fg, ((W - cw) // 2, (H - ch) // 2))
        return img


class FillSlide:
    """Кадр на весь экран 9:16: чёрные полосы кино-кадра срезаны, середина по центру, камера медленно наезжает
    (как в роликах, где картинка не «бегает» и не стоит в узкой полосе)."""

    ZOOM = 0.08

    def __init__(self, path: Path, reverse: bool):
        img = Image.open(path).convert("RGB")
        gray = img.convert("L").resize((32, img.height))
        rows = [sum(gray.crop((0, y, 32, y + 1)).getdata()) / 32 for y in range(gray.height)]
        top = next((y for y, v in enumerate(rows) if v > 14), 0)
        bottom = next((y for y, v in enumerate(reversed(rows)) if v > 14), 0)
        if top + bottom < img.height * 0.5:
            img = img.crop((0, top, img.width, img.height - bottom))
        s = max(W / img.width, H / img.height) * (1 + self.ZOOM)
        self.img = img.resize((round(img.width * s) + 1, round(img.height * s) + 1), Image.LANCZOS)
        self.reverse = reverse

    def frame(self, t: float, dur: float) -> Image.Image:
        k = min(max(t / max(dur, 0.1), 0.0), 1.0)
        k = k * k * (3 - 2 * k)
        z = (1 + self.ZOOM * ((1 - k) if self.reverse else k)) / (1 + self.ZOOM)
        cw, ch = round(W / z), round(H / z)
        x, y = (self.img.width - cw) // 2, (self.img.height - ch) // 2
        return self.img.crop((x, y, x + cw, y + ch)).resize((W, H), Image.BILINEAR)


class ClipSlide:
    """Отрезок трейлера (уже 1080×1920, 30 к/с, без звука): кадры читаются из ffmpeg по ходу монтажа, в памяти —
    только текущий. Кончился отрезок — держим последний кадр (на время перехода к следующей картинке)."""

    def __init__(self, path: Path):
        self.proc = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"fps={FPS},scale={W}:{H}",
                                      "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.idx = -1
        self.last = Image.new("RGB", (W, H))

    def frame(self, t: float, dur: float) -> Image.Image:
        want = int(max(t, 0.0) * FPS)
        size = W * H * 3
        while self.idx < want and self.proc.stdout:
            buf = self.proc.stdout.read(size)
            if len(buf) < size:
                self.proc.stdout = None  # конец отрезка
                break
            self.last = Image.frombytes("RGB", (W, H), buf)
            self.idx += 1
        return self.last

    def close(self) -> None:
        self.proc.kill()
        self.proc.wait()


TR = 0.3  # динамичный монтаж: переход между фразами
PUNCH = 0.14  # и «толчок» камеры на резкой склейке внутри фразы
TRANSITIONS = ["whip", "zoom", "tear", "flash", "whip", "zoom"]


class Transitions:
    """Переходы динамичного монтажа: «рывок» вбок со смазом, наезд, рваная бумага, вспышка."""

    def __init__(self, seed: int = 7):
        rnd = random.Random(seed)
        step = 12
        # рваный край: случайное блуждание по вертикали (один на ролик — так дешевле)
        x, edge = 0.0, []
        for _ in range(H // step + 2):
            x = max(-34.0, min(34.0, x + rnd.uniform(-14, 14)))
            edge.append(x)
        self.edge = edge
        self.paper = [16 + rnd.uniform(0, 14) for _ in edge]
        self.step = step

    def apply(self, kind: str, a: Image.Image, b: Image.Image, p: float) -> Image.Image:
        e = ease_out(p)
        if kind == "whip":
            off = round(W * e)
            img = Image.new("RGB", (W, H))
            img.paste(a, (-off, 0))
            img.paste(b, (W - off, 0))
            smear = img.resize((W // 12, H), Image.BILINEAR).resize((W, H), Image.BILINEAR)  # смаз по горизонтали
            return Image.blend(img, smear, 0.85 * (1 - abs(2 * p - 1)))
        if kind == "zoom":
            z = 1 + 0.25 * (1 - e)
            bw, bh = round(W * z), round(H * z)
            big = b.resize((bw, bh), Image.BILINEAR).crop(((bw - W) // 2, (bh - H) // 2, (bw - W) // 2 + W,
                                                            (bh - H) // 2 + H))
            return Image.blend(a, big, min(1.0, p * 2.5))
        if kind == "flash":
            white = Image.new("RGB", (W, H), (255, 255, 255))
            if p < 0.4:
                return Image.blend(a, white, 0.85 * p / 0.4)
            return Image.blend(b, white, 0.85 * (1 - (p - 0.4) / 0.6))
        # tear — рваная бумага: новый кадр «отрывается» слева направо, по краю белая полоса бумаги
        base = -80 + (W + 160) * e
        pts = [(base + self.edge[k], k * self.step) for k in range(len(self.edge))]
        mask = Image.new("L", (W, H), 0)
        md = ImageDraw.Draw(mask)
        md.polygon([(0, 0)] + pts + [(0, H)], fill=255)
        img = Image.composite(b, a, mask)
        d = ImageDraw.Draw(img)
        shadow = [(x + self.paper[k] + 10, y) for k, (x, y) in enumerate(pts)]
        d.polygon(pts + shadow[::-1], fill=(40, 40, 40))
        paper = [(x + self.paper[k], y) for k, (x, y) in enumerate(pts)]
        d.polygon(pts + paper[::-1], fill=(242, 240, 234))
        return img


def punch(b: Image.Image, t: float) -> Image.Image:
    """Резкая склейка внутри фразы: кадр «толкается» — за PUNCH секунды с +5 % к обычному размеру."""
    z = 1 + 0.05 * (1 - ease_out(t / PUNCH))
    bw, bh = round(W * z), round(H * z)
    return b.resize((bw, bh), Image.BILINEAR).crop(((bw - W) // 2, (bh - H) // 2, (bw - W) // 2 + W,
                                                    (bh - H) // 2 + H))


class OneWordCaptions(WordCaptions):
    """Субтитры по одному слову — крупно по центру, заглавными (как в динамичных шортсах); имена — жёлтым."""

    SIZE = 96

    def __init__(self, cfg: dict, words: list[dict], y: int):
        self.big = font(cfg, "head", self.SIZE)
        super().__init__(cfg, words, y)

    def _group(self) -> list[list[int]]:
        return [[k] for k in range(len(self.words))]

    def _render(self, g: int, hot: int) -> Image.Image:
        raw = self._clean(self.words[self.groups[g][0]]["w"])
        word = re.sub(r"^[^\w]+|[^\w!?]+$", "", raw) or raw
        name = word[:1].isupper() and g > 0 and not re.search(r"[.!?…]\W*$", self._clean(
            self.words[self.groups[g - 1][0]]["w"]))
        text = word.upper()
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        fnt = self.big
        while probe.textlength(text, font=fnt) > W - 120 and fnt.size > 48:
            fnt = fnt.font_variant(size=fnt.size - 6)
        w, h = round(probe.textlength(text, font=fnt)) + 60, round(fnt.size * 1.5)
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).text((34, 14), text, font=fnt, fill=(0, 0, 0, 190), stroke_width=7,
                                    stroke_fill=(0, 0, 0, 190))
        ImageDraw.Draw(layer).text((30, 8), text, font=fnt, fill=self.HOT if name else (255, 255, 255),
                                   stroke_width=6, stroke_fill=(0, 0, 0))
        out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        out.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(6)))
        out.alpha_composite(layer)
        return out


def _label_image(text: str, font_path: Path) -> Image.Image:
    """Крупная надпись на экране: жёлтая плашка, тёмный текст, до двух строк."""
    size = 74
    font = ImageFont.truetype(str(font_path), size)
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    words, lines = text.split(), []
    while True:
        lines, cur = [], ""
        for w in words:
            test = f"{cur} {w}".strip()
            if probe.textlength(test, font=font) <= W - 160:
                cur = test
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
        if len(lines) <= 2 or size <= 50:
            break
        size -= 6
        font = ImageFont.truetype(str(font_path), size)
    lines = [ln for ln in lines[:2] if ln]
    widths = [probe.textlength(ln, font=font) for ln in lines]
    line_h = int(size * 1.18)
    box_w, box_h = int(max(widths)) + 70, line_h * len(lines) + 34
    img = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, box_w - 1, box_h - 1), radius=22, fill=(255, 196, 61, 240))
    for k, ln in enumerate(lines):
        d.text((box_w // 2, 17 + k * line_h + line_h // 2), ln, font=font, fill=(14, 14, 18), anchor="mm")
    return img


def render(cfg: dict, base: Path) -> Path:
    def p(x):
        return (base / x).resolve()

    cfg["fonts"] = {k: p(v) for k, v in cfg["fonts"].items()}
    voice, music, out = p(cfg["voice"]), p(cfg["music"]), p(cfg["output"])
    logo = load_logo(p(cfg["logo"]))
    slides = cfg["slides"]
    speech_end = duration(voice)
    outro_len = cfg.get("outro_len", 3.5)
    dynamic = cfg.get("style") == "dynamic"
    # динамичный монтаж: без заставки в конце — последний кадр держится полсекунды, ролик идёт по кругу
    total = speech_end + (outro_len if outro_len > 0 else 0.5)
    trans = Transitions()
    kinds: dict[int, str] = {}  # переход к слайду k (только на новых фразах)
    for k, sl in enumerate(slides):
        if k and not sl.get("hard"):
            kinds[k] = TRANSITIONS[len(kinds) % len(TRANSITIONS)]
    ends = [s["start"] for s in slides[1:]] + [speech_end]

    loaded: dict[int, PanSlide | FitSlide | FillSlide | ClipSlide] = {}

    def slide(k: int) -> PanSlide | FitSlide | FillSlide | ClipSlide:
        if k not in loaded:
            for old in [j for j in loaded if j < k - 1]:
                gone = loaded.pop(old)
                if isinstance(gone, ClipSlide):
                    gone.close()
            if slides[k].get("clip"):  # отрезок трейлера
                loaded[k] = ClipSlide(p(slides[k]["clip"]))
            else:
                path = p(slides[k]["image"])
                with Image.open(path) as probe:
                    wide = probe.width > probe.height * 1.05
                # широкий кадр — целиком с размытым фоном; высокий (постер, страница комикса) — едет камера
                kind = FillSlide if cfg.get("fill") and wide else FitSlide if wide else PanSlide
                loaded[k] = kind(path, reverse=k % 3 == 2)
        return loaded[k]

    outro = OutroSlide(cfg, p(cfg.get("outro_image") or slides[0]["image"]), logo)
    labels = [(float(lb["start"]), float(lb["end"]), _label_image(lb["text"], cfg["fonts"]["head"]))
              for lb in cfg.get("labels") or [] if str(lb.get("text") or "").strip()]
    captions = (OneWordCaptions(cfg, cfg.get("words") or [], cfg.get("word_y", H // 2 + 160)) if dynamic
                else WordCaptions(cfg, cfg.get("words") or [], cfg.get("word_y", H // 2 + 120)))
    wm_w = 150
    wm = logo.resize((wm_w, round(logo.height * wm_w / logo.width)), Image.LANCZOS)
    wm.putalpha(wm.getchannel("A").point(lambda a: int(a * 0.85)))

    out.parent.mkdir(parents=True, exist_ok=True)
    audio = out.with_suffix(".m4a")
    sfx = [(float(t), p(f), float(db)) for t, f, db in cfg.get("sfx") or []]
    mix_audio(voice, music, total, audio, cfg.get("music_db", -16), sfx)

    log_path = out.with_suffix(".ffmpeg.log")
    log = open(log_path, "wb")  # noqa: SIM115 — закрываем вручную после кодировщика
    enc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
         "-i", "-", "-i", str(audio), "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "veryfast",
         "-tune", "zerolatency", "-threads", "1", "-x264-params", "rc-lookahead=0:sync-lookahead=0:bframes=0:ref=2",
         "-crf", "21", "-pix_fmt", "yuv420p", "-c:a", "copy", "-shortest", "-movflags", "+faststart", str(out)],
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
        if t >= speech_end and outro_len > 0:
            img = outro.frame(t - speech_end, outro_len)
            if t - speech_end < XFADE:
                k = len(slides) - 1
                prev = slide(k).frame(t - slides[k]["start"], ends[k] - slides[k]["start"])
                img = Image.blend(prev, img, ease_out((t - speech_end) / XFADE))
        else:
            k = max(j for j, s in enumerate(slides) if s["start"] <= t)
            img = slide(k).frame(t - slides[k]["start"], ends[k] - slides[k]["start"])
            into = t - slides[k]["start"]
            if dynamic and k > 0 and slides[k].get("hard") and into < PUNCH:
                img = punch(img, into)  # склейка внутри фразы — резкая, с толчком
            elif dynamic and k in kinds and into < TR:
                prev = slide(k - 1).frame(t - slides[k - 1]["start"], ends[k - 1] - slides[k - 1]["start"])
                img = trans.apply(kinds[k], prev, img, into / TR)
            elif not dynamic and k > 0 and into < XFADE:
                prev = slide(k - 1).frame(t - slides[k - 1]["start"], ends[k - 1] - slides[k - 1]["start"])
                img = Image.blend(prev, img, ease_out(into / XFADE))
            img.paste(wm, (W - wm_w - 36, 40), wm)
            for start, end, lab in labels:  # надпись на экране: появляется с фразой, держится до её конца
                if start <= t < end:
                    pop = min(1.0, (t - start) / 0.18)
                    y = 230 - round(40 * (1 - ease_out(pop)))
                    img.paste(lab, ((W - lab.width) // 2, y), lab)
                    break
            captions.draw(img, t)
        try:
            enc.stdin.write(img.tobytes())
        except BrokenPipeError:
            raise encoder_failed() from None
        if f % (FPS * 2) == 0:  # бот читает эти строки и двигает полоску загрузки
            print(f"PROGRESS {f / n_frames:.3f}  {t:5.1f}/{total:.1f} c", flush=True)
    enc.stdin.close()
    for sl in loaded.values():
        if isinstance(sl, ClipSlide):
            sl.close()
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

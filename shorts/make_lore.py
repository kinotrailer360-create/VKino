"""Сборка вертикального шортса «история персонажа»: картинки (страницы комиксов, кадры), по которым
медленно едет камера, крупные субтитры по одному слову, голос и музыка.

Запуск:  python -m shorts.make_lore work/lore.json  (конфиг собирает shorts/lore.py)
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .make_short import (
    FPS,
    H,
    OutroSlide,
    W,
    WordCaptions,
    duration,
    ease_out,
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
    total = speech_end + outro_len
    ends = [s["start"] for s in slides[1:]] + [speech_end]

    loaded: dict[int, PanSlide | ClipSlide] = {}

    def slide(k: int) -> PanSlide | ClipSlide:
        if k not in loaded:
            for old in [j for j in loaded if j < k - 1]:
                gone = loaded.pop(old)
                if isinstance(gone, ClipSlide):
                    gone.close()
            if slides[k].get("clip"):  # отрезок трейлера
                loaded[k] = ClipSlide(p(slides[k]["clip"]))
            else:  # чередуем направление: вниз, вверх — чтобы монтаж не казался однообразным
                loaded[k] = PanSlide(p(slides[k]["image"]), reverse=k % 3 == 2)
        return loaded[k]

    outro = OutroSlide(cfg, p(cfg.get("outro_image") or slides[0]["image"]), logo)
    labels = [(float(lb["start"]), float(lb["end"]), _label_image(lb["text"], cfg["fonts"]["head"]))
              for lb in cfg.get("labels") or [] if str(lb.get("text") or "").strip()]
    captions = WordCaptions(cfg, cfg.get("words") or [], cfg.get("word_y", H // 2 + 120))
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
        if t >= speech_end:
            img = outro.frame(t - speech_end, outro_len)
            if t - speech_end < XFADE:
                k = len(slides) - 1
                prev = slide(k).frame(t - slides[k]["start"], ends[k] - slides[k]["start"])
                img = Image.blend(prev, img, ease_out((t - speech_end) / XFADE))
        else:
            k = max(j for j, s in enumerate(slides) if s["start"] <= t)
            img = slide(k).frame(t - slides[k]["start"], ends[k] - slides[k]["start"])
            if k > 0 and t - slides[k]["start"] < XFADE:
                prev = slide(k - 1).frame(t - slides[k - 1]["start"], ends[k - 1] - slides[k - 1]["start"])
                img = Image.blend(prev, img, ease_out((t - slides[k]["start"]) / XFADE))
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

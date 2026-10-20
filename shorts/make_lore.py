"""Сборка вертикального шортса «история персонажа»: картинки (страницы комиксов, кадры), по которым
медленно едет камера, крупные субтитры по одному слову, голос и музыка.

Запуск:  python -m shorts.make_lore work/lore.json  (конфиг собирает shorts/lore.py)
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from PIL import Image

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

    loaded: dict[int, PanSlide] = {}

    def slide(k: int) -> PanSlide:
        if k not in loaded:
            for old in [j for j in loaded if j < k - 1]:
                loaded.pop(old)
            # чередуем направление: вниз, вверх — чтобы монтаж не казался однообразным
            loaded[k] = PanSlide(p(slides[k]["image"]), reverse=k % 3 == 2)
        return loaded[k]

    outro = OutroSlide(cfg, p(slides[0]["image"]), logo)
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
            captions.draw(img, t)
        try:
            enc.stdin.write(img.tobytes())
        except BrokenPipeError:
            raise encoder_failed() from None
        if f % (FPS * 2) == 0:  # бот читает эти строки и двигает полоску загрузки
            print(f"PROGRESS {f / n_frames:.3f}  {t:5.1f}/{total:.1f} c", flush=True)
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

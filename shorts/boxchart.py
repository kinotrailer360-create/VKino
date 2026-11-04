"""Моушн-графика про деньги для выпуска «Новости кино»: сборы фильмов франшизы и окупаемость.

Кадры рисуются на Python (PIL) и сразу уходят в ffmpeg — получается короткий ролик 16:9, который монтаж
вставляет в новость вместо пары картинок, пока ведущий говорит о сборах.

Окупаемость считается грубо, как её обычно объясняют в новостях: около половины сборов остаётся
кинотеатрам, плюс реклама, — фильм выходит в плюс примерно при сборах в 2,5 бюджета. Расходы на рекламу
студии не публикуют: на графике это помечено как оценка.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

from .cards import _cover, _font

log = logging.getLogger("shorts")

W, H = 1920, 1080
FPS = 25
ACCENT = (255, 184, 28)
GREEN = (72, 214, 120)
RED = (255, 92, 92)
GREY = (150, 150, 160)
BREAK_EVEN = 2.5  # сборы, при которых фильм примерно окупается (в бюджетах)
MARKETING = 0.7  # реклама — оценка в долях бюджета (студии её не раскрывают)


def money(mln: float) -> str:
    """$400 млн, $1,2 млрд."""
    if mln >= 1000:
        return f"${mln / 1000:.1f} млрд".replace(".", ",").replace(",0 ", " ")
    return f"${mln:.0f} млн" if mln >= 10 else f"${mln:.1f} млн".replace(".", ",")


def _ease(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def _background(img: Image.Image | None) -> Image.Image:
    if img is None:
        return Image.new("RGB", (W, H), (14, 14, 18))
    bg = _cover(img.convert("RGB"), W, H, 0.4).filter(ImageFilter.GaussianBlur(30))
    return ImageEnhance.Brightness(bg).enhance(0.28)


def _fit(draw: ImageDraw.ImageDraw, text: str, name: str, size: int, width: int, style: str = "Bold"):
    font = _font(name, size, style)
    while draw.textlength(text, font=font) > width and size > 22:
        size -= 2
        font = _font(name, size, style)
    return font


def _header(draw: ImageDraw.ImageDraw, title: str, sub: str) -> None:
    draw.text((110, 110), title, font=_fit(draw, title, "Oswald.ttf", 76, W - 220), fill="white")
    if sub:
        draw.text((112, 210), sub, font=_fit(draw, sub, "Montserrat.ttf", 34, W - 220, "SemiBold"), fill=GREY)
    draw.rounded_rectangle((112, 270, 292, 278), radius=4, fill=ACCENT)


def franchise_frames(bg: Image.Image, title: str, rows: list[dict[str, Any]], seconds: float):
    """Горизонтальные столбики: каждый фильм франшизы, нынешний — жёлтым. rows: {"name", "value", "now",
    "note"} (value — млн $). Столбики вырастают по очереди, цифры бегут вверх."""
    top = max(r["value"] for r in rows) or 1
    n = len(rows)
    area_top, area_bottom = 330, H - 120
    step = min(110, (area_bottom - area_top) / n)
    bar_h = step * 0.62
    label_w = 620
    max_w = W - 110 - label_w - 260
    total = int(seconds * FPS)
    grow = 0.55 * seconds  # первые ~55% — рост, потом держим кадр
    for f in range(total):
        t = f / FPS
        frame = bg.copy()
        draw = ImageDraw.Draw(frame)
        _header(draw, title, "Мировые сборы, данные TMDB и СМИ")
        for k, row in enumerate(rows):
            start = k * grow / (n + 1)
            p = _ease((t - start) / (grow / (n + 1) * 2))
            y = area_top + k * step
            name_font = _fit(draw, row["name"], "Montserrat.ttf", 36, label_w - 30, "SemiBold")
            alpha_col = ACCENT if row.get("now") else (235, 235, 235)
            draw.text((110, y + bar_h / 2), row["name"], font=name_font, fill=alpha_col, anchor="lm")
            x0 = 110 + label_w
            w = max(6, max_w * row["value"] / top * p)
            if row.get("forecast"):
                draw.rounded_rectangle((x0, y, x0 + w, y + bar_h), radius=10, outline=ACCENT, width=5)
            else:
                draw.rounded_rectangle((x0, y, x0 + w, y + bar_h), radius=10,
                                       fill=ACCENT if row.get("now") else (90, 96, 112))
            if p > 0.05:
                value = money(row["value"] * p) + (f" · {row['note']}" if row.get("note") else "")
                draw.text((x0 + w + 20, y + bar_h / 2), value, font=_font("Oswald.ttf", 44, "Bold"),
                          fill="white", anchor="lm")
        yield frame


def payback_frames(bg: Image.Image, title: str, budget: float, gross: float, marketing: float,
                   seconds: float):
    """Бюджет, реклама (оценка), порог окупаемости и сборы: столбик сборов дорастает до своей цифры, в конце —
    плашка «ОКУПИЛСЯ ×5» или «ДО ОКУПАЕМОСТИ ЕЩЁ $X»."""
    need = budget * BREAK_EVEN
    top = max(gross, need, budget + marketing) * 1.08
    rows = [("Бюджет", budget, (120, 128, 148)), ("Реклама (оценка)", marketing, (96, 104, 124)),
            ("Сборы", gross, ACCENT)]
    label_w = 520
    x0 = 110 + label_w
    max_w = W - x0 - 300
    ys = [380, 520, 700]
    bar_h = 96
    total = int(seconds * FPS)
    grow = 0.6 * seconds
    ok = gross >= need
    for f in range(total):
        t = f / FPS
        frame = bg.copy()
        draw = ImageDraw.Draw(frame)
        _header(draw, title, "Окупаемость ≈ 2,5 бюджета: половина сборов остаётся кинотеатрам, плюс реклама")
        for k, (name, value, color) in enumerate(rows):
            p = _ease((t - k * grow / 4) / (grow / 2))
            y = ys[k]
            draw.text((110, y + bar_h / 2), name, font=_font("Montserrat.ttf", 40, "SemiBold"),
                      fill=ACCENT if k == 2 else (235, 235, 235), anchor="lm")
            w = max(6, max_w * value / top * p)
            draw.rounded_rectangle((x0, y, x0 + w, y + bar_h), radius=12, fill=color)
            if p > 0.05:
                draw.text((x0 + w + 20, y + bar_h / 2), money(value * p), font=_font("Oswald.ttf", 50, "Bold"),
                          fill="white", anchor="lm")
        # порог окупаемости — пунктир через все столбики
        line_p = _ease((t - grow * 0.3) / (grow * 0.4))
        if line_p > 0:
            lx = x0 + max_w * need / top
            y_end = 330 + (ys[-1] + bar_h + 40 - 330) * line_p
            yy = 330
            while yy < y_end:
                draw.line((lx, yy, lx, min(yy + 18, y_end)), fill=(255, 255, 255), width=4)
                yy += 30
            draw.text((lx, ys[-1] + bar_h + 62), f"окупаемость ≈ {money(need)}",
                      font=_font("Montserrat.ttf", 34, "Bold"), fill="white", anchor="mm")
        if t > grow + 0.3:  # итог
            if ok:
                badge = f"ОКУПИЛСЯ: СБОРЫ В {gross / budget:.1f} РАЗА БОЛЬШЕ БЮДЖЕТА".replace(".", ",")
                color = GREEN
            else:
                badge = f"ДО ОКУПАЕМОСТИ ЕЩЁ ≈ {money(need - gross).upper()}"
                color = RED
            font = _font("Oswald.ttf", 58, "Bold")
            bw = draw.textlength(badge, font=font) + 80
            draw.rounded_rectangle((W / 2 - bw / 2, 935, W / 2 + bw / 2, 1025), radius=45, fill=color)
            draw.text((W / 2, 980), badge, font=font, fill=(14, 14, 18), anchor="mm")
        yield frame


async def _encode(frames, out: Path) -> Path:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
        "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-an", str(out),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    assert proc.stdin is not None
    try:
        for frame in frames:
            data = await asyncio.to_thread(lambda fr=frame: fr.convert("RGB").tobytes())
            proc.stdin.write(data)
            await proc.stdin.drain()
        proc.stdin.close()
        _, err = await proc.communicate()
    except BaseException:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode:
        raise RuntimeError(f"ffmpeg (график): {err.decode(errors='ignore')[-300:]}")
    return out


def plan(story: dict[str, Any], info: dict[str, Any] | None) -> list[tuple[str, dict[str, Any]]]:
    """Какие графики нужны новости: [("franchise", {...}), ("payback", {...})]. Цифры из новости (ИИ взял их
    из источников) важнее TMDB: там сборы свежего фильма обновляются с опозданием."""
    m = story.get("money") or {}
    gross = float(m.get("gross") or 0)
    budget = float(m.get("budget") or 0)
    forecast = float(m.get("forecast") or 0)
    if info:
        # сборы свежего фильма TMDB обновляет с опозданием — его цифру берём, только если фильм не этого года
        if not gross and not forecast and str(info.get("year") or "9999") < str(date.today().year):
            gross = info.get("revenue", 0) / 1e6
        budget = budget or info.get("budget", 0) / 1e6
    film = story.get("film") or (info or {}).get("title") or ""
    out: list[tuple[str, dict[str, Any]]] = []
    if info:
        others = [p for p in info.get("parts") or [] if p["id"] != info["id"] and p["revenue"] >= 1e6]
        others = others[-6:]  # последние фильмы франшизы — иначе у «Годзиллы» было бы 30 столбиков
        if len(others) >= 2:
            rows = [{"name": f"{p['title']} ({p['year']})", "value": p["revenue"] / 1e6} for p in others]
            if gross:
                rows.append({"name": f"{film} ({info.get('year') or ''})".replace(" ()", ""), "value": gross,
                             "now": True})
            elif forecast:
                rows.append({"name": f"{film}: старт", "value": forecast, "now": True, "forecast": True,
                             "note": "прогноз"})
            out.append(("franchise", {"title": f"СБОРЫ ФРАНШИЗЫ «{film.upper()}»", "rows": rows}))
    if budget >= 1 and gross >= 1 and not forecast:
        out.append(("payback", {"title": f"«{film.upper()}»: ОКУПИЛСЯ ЛИ ФИЛЬМ", "budget": budget,
                                "gross": gross, "marketing": round(budget * MARKETING)}))
    return out


async def render(kind: str, data: dict[str, Any], bg_img: Image.Image | None, seconds: float, out: Path) -> Path:
    bg = await asyncio.to_thread(_background, bg_img)
    if kind == "franchise":
        frames = franchise_frames(bg, data["title"], data["rows"], seconds)
    else:
        frames = payback_frames(bg, data["title"], data["budget"], data["gross"], data["marketing"], seconds)
    return await _encode(frames, out)

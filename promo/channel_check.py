"""Проверка Telegram-каналов перед закупкой рекламы: накрутка, живость, тематика, цена за просмотр.

Только стандартная библиотека Python 3.8+ — работает на Windows/Mac/Linux без установки пакетов.
Читает открытую страницу канала t.me/s/<канал> (последние ~20 постов): подписчики, просмотры, реакции, даты.

Запуск:
    python channel_check.py @Filmi @marvelgolik https://telega.in/tme/domfantasy
    python channel_check.py @Filmi:450 @POKDC:600        ← после двоеточия цена поста 1/24 в рублях (для CPM)
    python channel_check.py -f channels.txt               ← по одному каналу в строке (можно с :цена)
    python channel_check.py ... --csv result.csv          ← ещё и таблица для Excel

Понимает: @name, name, t.me/name, telega.in/tme/name, telega.in/channels/name/card.
Каналы MAX (max.ru/…) так не проверить — они помечаются «проверь вручную».
"""
from __future__ import annotations

import argparse
import csv
import html
import re
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timezone

PIRATE = re.compile(r"смотреть онлайн|смотреть бесплатно|бесплатно смотреть|в хорошем качестве|\bhd\b|1080p?|скачать|"
                    r"фильм целиком|все серии подряд", re.I)
OFFTOPIC = re.compile(r"стрим|twitch|мой канал на ютуб|моё творчество|мое творчество|крипт|ставк|казино|заработ", re.I)
CINEMA = re.compile(r"фильм|кино|сериал|трейлер|премьер|режисс|актёр|актер|сезон|marvel|netflix|оскар|прокат", re.I)


def username(raw: str) -> tuple[str, int]:
    raw = raw.strip()
    price = 0
    m = re.search(r":(\d{2,6})$", raw)
    if m:
        price, raw = int(m.group(1)), raw[: m.start()]
    if "max.ru" in raw:
        return "MAX:" + raw, price
    m = re.search(r"(?:t\.me/(?:s/)?|telega\.in/(?:tme|channels)/)([A-Za-z0-9_]{4,})", raw)
    name = m.group(1) if m else raw.lstrip("@").split("/")[0]
    return name, price


def num(s: str) -> int:
    s = s.replace(" ", "").replace(",", ".").upper()
    m = re.match(r"([\d.]+)([KM]?)", s)
    if not m:
        return 0
    return int(float(m.group(1)) * {"K": 1_000, "M": 1_000_000}.get(m.group(2), 1))


def fetch(name: str) -> str:
    req = urllib.request.Request(f"https://t.me/s/{name}", headers={"Accept-Language": "ru"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "ignore")


def check(name: str, price: int) -> dict:
    row = {"канал": "@" + name, "цена": price or "", "вердикт": "", "причины": ""}
    if name.startswith("MAX:"):
        row.update(канал=name[4:], вердикт="❓ MAX", причины="статистику MAX смотри в карточке telega.in")
        return row
    try:
        page = fetch(name)
    except Exception as exc:  # noqa: BLE001
        row.update(вердикт="❓", причины=f"не открылся: {exc}")
        return row
    if "tgme_channel_info" not in page:
        row.update(вердикт="🔴", причины="закрытый канал или не найден — проверить нельзя")
        return row
    title = html.unescape(re.sub(r"<[^>]+>", "", re.search(r"<title>(.*?)</title>", page, re.S).group(1)))
    title = title.replace(" – Telegram", "").strip()
    subs = next((num(v) for v, k in re.findall(
        r'counter_value">([^<]+)</span>\s*<span class="counter_type">([^<]+)', page) if "subscriber" in k), 0)
    msgs = page.split('class="tgme_widget_message_wrap')[1:]
    views = [num(v) for v in re.findall(r'tgme_widget_message_views">([^<]+)<', page)]
    texts = [html.unescape(re.sub(r"<[^>]+>", " ", m)) for m in msgs]
    reacts = [sum(num(x) for x in re.findall(r'class="tgme_reaction[^"]*"[^>]*>.*?</i>\s*([\d.,KM]+)', m, re.S))
              for m in msgs]
    dates = [datetime.fromisoformat(d) for d in re.findall(r'datetime="([^"]+)"', page)]
    med = statistics.median(views) if views else 0
    err = med / subs * 100 if subs else 0
    spread = (statistics.pstdev(views) / statistics.mean(views)) if len(views) > 3 and statistics.mean(views) else 0
    days_quiet = (datetime.now(timezone.utc) - dates[-1]).days if dates else 999
    span = max(1.0, (dates[-1] - dates[0]).total_seconds() / 86400) if len(dates) > 1 else 1.0
    per_day = len(msgs) / span
    pirate = sum(bool(PIRATE.search(t)) for t in texts)
    cinema = sum(bool(CINEMA.search(t)) for t in texts)
    off = bool(OFFTOPIC.search(title + " " + " ".join(texts[:5])))
    react_rate = (statistics.median(reacts) / med * 100) if reacts and med else 0
    cpm = round(price / med * 1000) if price and med else ""

    bad, warn = [], []
    if err < 8:
        bad.append(f"ERR {err:.0f}% — мёртвая или накрученная база")
    elif err < 12:
        warn.append(f"ERR {err:.0f}% низковат")
    if err > 80:
        bad.append(f"ERR {err:.0f}% — просмотров больше, чем похоже на правду (накрутка/репосты)")
    elif err > 60:
        warn.append(f"ERR {err:.0f}% высоковат")
    if spread > 1.2:
        warn.append("просмотры скачут (возможна точечная накрутка)")
    if days_quiet > 10:
        bad.append(f"не публикует {days_quiet} дн.")
    if pirate >= max(3, len(msgs) // 3):
        bad.append(f"пиратский контент ({pirate}/{len(msgs)} постов)")
    if cinema < max(3, len(msgs) // 3):
        warn.append(f"мало про кино ({cinema}/{len(msgs)} постов)")
    if off:
        warn.append("похоже на личный блог/не по теме")
    if per_day > 12:
        warn.append(f"{per_day:.0f} постов в день — реклама утонет")
    if react_rate < 0.3 and med > 500:
        warn.append("почти нет реакций")
    if cpm and cpm > 600:
        warn.append(f"дорого: CPM {cpm} ₽")
    verdict = "🔴 не брать" if bad else ("🟡 можно, но" if len(warn) >= 2 else "🟢 брать")
    row.update({"название": title[:40], "подписчики": subs, "охват поста": int(med), "ERR %": round(err),
                "постов/день": round(per_day, 1), "реакции % охвата": round(react_rate, 1),
                "дней без постов": days_quiet, "CPM ₽": cpm, "вердикт": verdict,
                "причины": "; ".join(bad + warn) or "живой кино-канал"})
    return row


def main() -> None:
    ap = argparse.ArgumentParser(description="Проверка Telegram-каналов на накрутку перед покупкой рекламы")
    ap.add_argument("channels", nargs="*")
    ap.add_argument("-f", "--file")
    ap.add_argument("--csv")
    a = ap.parse_args()
    items = list(a.channels)
    if a.file:
        with open(a.file, encoding="utf-8") as fh:
            items += [line.strip() for line in fh if line.strip() and not line.startswith("#")]
    if not items:
        ap.print_help()
        sys.exit(1)
    rows = []
    for raw in items:
        name, price = username(raw)
        rows.append(check(name, price))
        time.sleep(0.7)  # бережно к t.me
    order = {"🟢": 0, "🟡": 1, "❓": 2, "🔴": 3}
    rows.sort(key=lambda r: (order.get(r["вердикт"][:1], 9), -(r.get("ERR %") or 0)))
    cols = ["вердикт", "канал", "название", "подписчики", "охват поста", "ERR %", "постов/день",
            "реакции % охвата", "дней без постов", "цена", "CPM ₽", "причины"]
    for r in rows:
        print(" | ".join(str(r.get(c, "")) for c in cols))
    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, delimiter=";", extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print(f"\nТаблица для Excel: {a.csv}")


if __name__ == "__main__":
    main()

"""Ежедневный шортс: тема -> фильмы с Кинопоиска -> карточки, голос, видео, SEO."""

from __future__ import annotations

import asyncio
import html
import io
import json
import logging
import random
import re
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp
from PIL import Image

from .cards import make_card, make_cover

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
FILMS = 5
VOICE = "ru-RU-DmitryNeural"
VOICE_RATE = "+22%"
GAP = 0.2  # пауза между фразами, сек
MIN_RATING = 6.0  # ниже — рейтинг не показываем, он только отпугнёт
SKIP_RATING = 5.5  # ниже — фильм в подборку не берём

YOUTUBE_URL = "https://youtube.com/@kinomir360"
TELEGRAM_URL = "https://t.me/KinoMir360"

# По дням недели (пн..вс)
SCHEDULE = ["online", "cinema", "facts", "genre", "online", "upcoming", "facts"]

GENRES = {
    "фантастика": ("ТОП ФАНТАСТИКИ", "фантастических фильмов, которые взрывают мозг", "фантастика"),
    "ужасы": ("ТОП УЖАСОВ", "фильмов ужасов, после которых не уснёшь", "ужасы"),
    "триллер": ("ТОП ТРИЛЛЕРОВ", "триллеров, которые держат до последней минуты", "триллеры"),
    "комедия": ("ТОП КОМЕДИЙ", "комедий для отличного вечера", "комедии"),
    "боевик": ("ТОП БОЕВИКОВ", "боевиков, от которых захватывает дух", "боевики"),
    "детектив": ("ТОП ДЕТЕКТИВОВ", "детективов с неожиданной развязкой", "детективы"),
}

Fetch = Callable[..., Awaitable[dict[str, Any]]]
log = logging.getLogger("shorts")


@dataclass
class Theme:
    key: str
    tag: str
    header: str
    hook: str  # «5 ...» — начало фразы для обложки и голоса
    seo_title: str
    tags: list[str]
    upcoming: bool = False


@dataclass
class ShortResult:
    video: Path
    cover: Path
    theme: Theme
    films: list[dict[str, Any]]
    seo: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------- данные

def _range(start: date, end: date) -> str:
    return f"{start:%d.%m.%Y}-{end:%d.%m.%Y}"


def _title(item: dict[str, Any]) -> str:
    return str(item.get("name") or item.get("alternativeName") or item.get("enName") or "").strip()


def _image_url(item: dict[str, Any]) -> str | None:
    for key in ("backdrop", "poster"):
        val = item.get(key)
        if isinstance(val, dict) and val.get("url"):
            return str(val["url"])
    return None


def _poster_url(item: dict[str, Any]) -> str | None:
    val = item.get("poster")
    if isinstance(val, dict) and val.get("url"):
        return str(val["url"])
    return _image_url(item)


def _best_rating(item: dict[str, Any]) -> tuple[float, str]:
    """Больший из рейтингов IMDb и Кинопоиска (0 — рейтинга нет)."""
    r = item.get("rating") or {}
    best, label = 0.0, ""
    for key, name in (("imdb", "IMDb"), ("kp", "КП")):
        try:
            v = float(r.get(key) or 0)
        except (TypeError, ValueError):
            v = 0.0
        if v > best:
            best, label = v, name
    return best, label


def _rating(item: dict[str, Any]) -> tuple[float | None, str]:
    """Рейтинг для карточки и описания: только если он достаточно высокий."""
    v, label = _best_rating(item)
    return (v, label) if v >= MIN_RATING else (None, "")


def _actors(item: dict[str, Any], n: int = 3) -> list[str]:
    out = []
    for p in item.get("persons") or []:
        if not isinstance(p, dict):
            continue
        if str(p.get("enProfession") or p.get("profession") or "").lower() not in ("actor", "актеры", "актёры"):
            continue
        name = str(p.get("name") or p.get("enName") or "").strip()
        if name and name not in out:
            out.append(name)
        if len(out) >= n:
            break
    return out


def _genres(item: dict[str, Any]) -> list[str]:
    return [str(g.get("name")) for g in item.get("genres") or [] if isinstance(g, dict) and g.get("name")]


MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]


def _premiere(item: dict[str, Any]) -> date | None:
    prem = item.get("premiere") or {}
    for key in ("russia", "digital", "world"):
        raw = str(prem.get(key) or "")[:10]
        try:
            return date.fromisoformat(raw)
        except ValueError:
            continue
    return None


def _date_ru(d: date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]}"


def _pdate(item: dict[str, Any], key: str) -> date | None:
    raw = str((item.get("premiere") or {}).get(key) or "")[:10]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _date_full(d: date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def _money(obj: Any) -> str | None:
    """{"value": 150000000, "currency": "$"} -> "$150 млн"."""
    if not isinstance(obj, dict):
        return None
    try:
        v = float(obj.get("value") or 0)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    if v >= 1e9:
        num = f"{v / 1e9:.1f}".rstrip("0").rstrip(".").replace(".", ",") + " млрд"
    elif v >= 1e6:
        num = f"{v / 1e6:.0f} млн"
    else:
        num = f"{v / 1e3:.0f} тыс"
    cur = str(obj.get("currency") or "$").strip()
    if cur in ("$", "€"):
        return f"{cur}{num}"
    return f"{num} руб." if cur in ("₽", "руб", "руб.", "RUB") else f"{num} {cur}"


def info_rows(item: dict[str, Any]) -> list[list[tuple[str, str]]]:
    """Строки под карточкой: даты выхода, бюджет и сборы — что есть в данных."""
    dates: list[tuple[str, str]] = []
    if item.get("isSeries"):
        if d := _pdate(item, "world"):
            dates.append(("Премьера", _date_full(d)))
    else:
        if d := _pdate(item, "russia"):
            dates.append(("В кино", _date_full(d)))
        if d := _pdate(item, "digital"):
            dates.append(("Онлайн", _date_full(d)))
        if not dates and (d := _pdate(item, "world")):
            dates.append(("Премьера", _date_full(d)))
    money: list[tuple[str, str]] = []
    if b := _money(item.get("budget")):
        money.append(("Бюджет", b))
    if f := _money((item.get("fees") or {}).get("world")):
        money.append(("Сборы", f))
    return [row for row in (dates, money) if row]


async def _query(fetch: Fetch, **params: Any) -> list[dict[str, Any]]:
    params.setdefault("page", 1)
    params.setdefault("limit", 40)
    try:
        data = await fetch("/movie", **params)
    except Exception as exc:  # noqa: BLE001 — одна неудачная выборка не должна ронять сборку
        log.warning("shorts: query %s failed: %s", params, exc)
        return []
    return [x for x in data.get("docs") or [] if isinstance(x, dict)]


def theme_for(key: str | None, today: date) -> Theme:
    key = key or SCHEDULE[today.weekday()]
    n = FILMS
    if key == "online":
        return Theme("online", "НОВИНКИ", "ОНЛАЙН-ПРЕМЬЕРЫ",
                     f"{n} свежих онлайн-премьер, которые стоит посмотреть",
                     f"{n} свежих онлайн-премьер 🔥 Что посмотреть",
                     ["онлайн премьеры", "новинки онлайн", "что посмотреть", "новые фильмы", "новые сериалы"])
    if key == "cinema":
        return Theme("cinema", "В КИНО", "СЕЙЧАС В КИНО",
                     f"{n} фильмов, которые идут в кино прямо сейчас",
                     f"Что посмотреть в кино: {n} фильмов в прокате 🍿",
                     ["кинопрокат", "сейчас в кино", "новинки кино", "что посмотреть в кино", "афиша кино"])
    if key == "series":
        return Theme("series", "СЕРИАЛЫ", "НОВЫЕ СЕРИАЛЫ",
                     f"{n} новых сериалов, от которых сложно оторваться",
                     f"{n} новых сериалов, от которых сложно оторваться 📺",
                     ["новые сериалы", "сериалы новинки", "что посмотреть", "лучшие сериалы", "сериал на вечер"])
    if key == "upcoming":
        return Theme("upcoming", "СКОРО", "СКОРО В КИНО",
                     f"{n} самых ожидаемых премьер ближайших недель",
                     f"{n} самых ожидаемых премьер ⏳ Скоро в кино",
                     ["скоро в кино", "ожидаемые фильмы", "премьеры", "новинки кино", "афиша"],
                     upcoming=True)
    genre = key.split(":", 1)[1] if key.startswith("genre:") else random.choice(list(GENRES))
    header, hook, plural = GENRES.get(genre, GENRES["фантастика"])
    return Theme(f"genre:{genre}", "ПОДБОРКА", header, f"{n} {hook}",
                 f"{n} {hook} 🎬",
                 [f"лучшие {plural}", plural, "что посмотреть", "подборка фильмов", "топ фильмов"])


async def pick_films(fetch: Fetch, theme: Theme, today: date, exclude: set[int]) -> list[dict[str, Any]]:
    common = {"sortField": "votes.kp", "sortType": "-1", "notNullFields": "name"}
    if theme.key == "online":
        docs = await _query(fetch, isSeries="false",
                            **{"premiere.digital": _range(today - timedelta(days=30), today)}, **common)
        docs += await _query(fetch, isSeries="true",
                             **{"premiere.world": _range(today - timedelta(days=30), today)}, **common)
        docs.sort(key=lambda x: -int((x.get("votes") or {}).get("kp") or 0))
    elif theme.key == "cinema":
        docs = await _query(fetch, isSeries="false",
                            **{"premiere.russia": _range(today - timedelta(days=35), today)}, **common)
    elif theme.key == "series":
        docs = await _query(fetch, isSeries="true",
                            **{"premiere.world": _range(today - timedelta(days=60), today)}, **common)
    elif theme.key == "upcoming":
        docs = await _query(fetch, isSeries="false", sortField="votes.await", sortType="-1",
                            notNullFields="name",
                            **{"premiere.russia": _range(today + timedelta(days=1), today + timedelta(days=60))})
    else:
        genre = theme.key.split(":", 1)[1]
        docs = await _query(fetch, page=random.randint(1, 3), isSeries="false",
                            **{"genres.name": genre, "rating.kp": "7.2-10", "votes.kp": "30000-100000000"},
                            **common)
        random.shuffle(docs)

    picked, seen = [], set()
    for d in docs:
        mid = d.get("id")
        if not mid or mid in exclude or mid in seen or not _title(d) or not _image_url(d):
            continue
        if 0 < _best_rating(d)[0] < SKIP_RATING:
            continue
        seen.add(mid)
        picked.append(d)
        if len(picked) >= FILMS:
            break
    # подробности (актёры, жанры, рейтинг) — в списках они бывают неполными
    full = []
    for d in picked:
        try:
            full.append(await fetch(f"/movie/{d['id']}"))
        except Exception:  # noqa: BLE001
            full.append(d)
    return full


async def _download(session: aiohttp.ClientSession, url: str) -> Image.Image:
    async with session.get(url) as resp:
        resp.raise_for_status()
        return Image.open(io.BytesIO(await resp.read())).convert("RGB")


# ---------------------------------------------------------------- факты о фильме

FACTS = 5
FACT_MIN, FACT_MAX = 40, 170  # длиннее — не влезает на экран и затягивает ролик


def _clean_fact(raw: Any) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", "", str(raw or "")))
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def usable_facts(item: dict[str, Any]) -> list[str]:
    """Факты без спойлеров и подходящей длины (сначала FACT, потом ляпы)."""
    facts, bloopers = [], []
    for f in item.get("facts") or []:
        if not isinstance(f, dict) or f.get("spoiler"):
            continue
        text = _clean_fact(f.get("value"))
        if not FACT_MIN <= len(text) <= FACT_MAX:
            continue
        (bloopers if str(f.get("type") or "").upper() == "BLOOPER" else facts).append(text)
    return facts + bloopers


def _kind(item: dict[str, Any]) -> str:
    kind = str(item.get("type") or "")
    if item.get("isSeries") or "series" in kind:
        return "сериале"
    if kind in ("cartoon", "anime"):
        return "мультфильме"
    return "фильме"


def _facts_word(n: int) -> str:
    return "факта" if 2 <= n <= 4 else "фактов"


async def pick_fact_film(fetch: Fetch, query: str, exclude: set[int]) -> tuple[dict[str, Any], list[str]]:
    if query:
        try:
            data = await fetch("/movie/search", query=query, page=1, limit=5)
            cands = [x for x in data.get("docs") or [] if isinstance(x, dict)]
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Поиск «{query}» не удался: {exc}") from exc
        need = 3
    else:
        cands = await _query(fetch, page=random.randint(1, 5), sortField="votes.kp", sortType="-1",
                             notNullFields="name",
                             **{"rating.kp": "7-10", "votes.kp": "150000-100000000"})
        random.shuffle(cands)
        need = FACTS
    for d in cands[:8]:
        if not d.get("id") or (not query and d["id"] in exclude):
            continue
        try:
            full = await fetch(f"/movie/{d['id']}")
        except Exception:  # noqa: BLE001
            continue
        facts = usable_facts(full)
        if len(facts) >= need and _image_url(full):
            return full, random.sample(facts[:12], min(FACTS, len(facts[:12])))
    raise RuntimeError(f"Не нашёл фильм с {need}+ подходящими фактами" + (f" по запросу «{query}»" if query else ""))


async def _stills(fetch: Fetch, item: dict[str, Any]) -> list[str]:
    """Кадры из фильма; если API их не отдаёт — фон и постер."""
    urls: list[str] = []
    try:
        data = await fetch("/image", movieId=item["id"], type="still", limit=20)
        for img in data.get("docs") or []:
            w, h = img.get("width") or 0, img.get("height") or 0
            if img.get("url") and (not w or w >= 700) and (not h or w >= h):
                urls.append(str(img["url"]))
    except Exception as exc:  # noqa: BLE001
        log.info("shorts: no stills for %s: %s", item.get("id"), exc)
    random.shuffle(urls)
    for fallback in (_image_url(item), _poster_url(item)):
        if fallback and fallback not in urls:
            urls.append(fallback)
    return urls


# ---------------------------------------------------------------- голос

def _say_film(item: dict[str, Any], theme: Theme) -> str:
    parts = [f"«{_title(item)}»."]
    genres = _genres(item)
    if theme.upcoming and _premiere(item):
        parts.append(f"В кино с {_date_ru(_premiere(item))}.")
    elif genres:
        parts.append(f"{genres[0].capitalize()}.")
    return " ".join(parts)


def voice_lines(theme: Theme, films: list[dict[str, Any]]) -> list[str]:
    return ([f"{theme.hook}. Сохрани, чтобы не потерять!"]
            + [_say_film(f, theme) for f in films]
            + ["Какой посмотришь первым? Пиши в комментариях "
               "и подписывайся на КиноМир триста шестьдесят!"])


async def _run(*cmd: str) -> str:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, cwd=ROOT.parent)
    out, err = await proc.communicate()
    if proc.returncode:
        raise RuntimeError(f"{cmd[0]} failed: {err.decode(errors='ignore')[-800:]}")
    return out.decode(errors="ignore")


async def _duration(path: Path) -> float:
    out = await _run("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path))
    return float(out.strip())


async def synthesize(lines: list[str], work: Path) -> tuple[Path, list[float], bool]:
    """Озвучка по фразам; возвращает файл, начало каждой фразы и признак, что голос есть."""
    parts: list[Path] = []
    voiced = True
    try:
        import edge_tts
        for i, text in enumerate(lines):
            part = work / f"voice_{i}.mp3"
            await edge_tts.Communicate(text, VOICE, rate=VOICE_RATE).save(str(part))
            parts.append(part)
    except Exception as exc:  # noqa: BLE001 — без голоса ролик всё равно собираем
        log.warning("shorts: TTS failed, using silence: %s", exc)
        voiced = False
        parts = []
        for i in range(len(lines)):
            part = work / f"voice_{i}.mp3"
            sec = 3.5 if 0 < i < len(lines) - 1 else 4.5
            await _run("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                       "-t", str(sec), str(part))
            parts.append(part)

    starts, t = [], 0.0
    inputs, chains = [], []
    for i, part in enumerate(parts):
        starts.append(round(t, 3))
        t += await _duration(part) + GAP
        inputs += ["-i", str(part)]
        chains.append(f"[{i}:a]aresample=48000,apad=pad_dur={GAP}[a{i}]")
    filt = ";".join(chains) + ";" + "".join(f"[a{i}]" for i in range(len(parts))) + \
        f"concat=n={len(parts)}:v=0:a=1[out]"
    voice = work / "voice.wav"
    await _run("ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", filt, "-map", "[out]", str(voice))
    return voice, starts, voiced


# ---------------------------------------------------------------- SEO

def build_seo(theme: Theme, films: list[dict[str, Any]]) -> dict[str, str]:
    names = [_title(f) for f in films]
    title = f"{theme.seo_title} #shorts"
    if len(title) > 100:
        title = title[:90].rstrip() + "… #shorts"

    rows = []
    for i, f in enumerate(films, 1):
        year = f.get("year")
        line = f"{i}. {_title(f)}" + (f" ({year})" if year else "")
        rating, src = _rating(f)
        if theme.upcoming and _premiere(f):
            line += f" — в кино с {_date_ru(_premiere(f))}"
        elif rating:
            line += f" — {src} {rating:.1f}"
        rows.append(line)

    hashtags = ["#чтопосмотреть", "#новинкикино", "#KinoMir360"]
    if theme.key == "series":
        hashtags[1] = "#сериалы"
    elif theme.key == "upcoming":
        hashtags[1] = "#скоровкино"
    elif theme.key.startswith("genre:"):
        hashtags[1] = "#подборкафильмов"

    description = "\n".join([
        f"{theme.hook} — сохрани, чтобы не потерять! 🍿", "", *rows, "",
        "Какой посмотришь первым? Пиши в комментариях 👇", "",
        f"📺 YouTube: {YOUTUBE_URL}",
        f"✈️ Telegram: {TELEGRAM_URL} — новинки, трейлеры и подборки каждый день", "",
        " ".join(hashtags),
    ])

    tags: list[str] = []
    for t in theme.tags + [n.lower() for n in names] + \
            [a.lower() for f in films for a in _actors(f, 1)] + ["KinoMir360", "киномир360", "шортс кино"]:
        if t and t not in tags and len(", ".join(tags + [t])) <= 480:
            tags.append(t)

    pinned = (f"Все трейлеры и где смотреть — в нашем Telegram 👉 {TELEGRAM_URL}\n"
              "А какой фильм ты посмотришь первым? 👇")
    return {"title": title, "description": description, "tags": ", ".join(tags), "pinned": pinned}


def build_facts_seo(film: dict[str, Any], facts: list[str]) -> dict[str, str]:
    t, n, kind = _title(film), len(facts), _kind(film)
    title = f"{n} {_facts_word(n)} о {kind} «{t}», которые ты не знал 🤯 #shorts"
    if len(title) > 100:
        title = f"{n} {_facts_word(n)} о «{t}» 🤯 #shorts"[:100]
    year = film.get("year")
    description = "\n".join([
        f"{n} {_facts_word(n)} о {kind} «{t}»" + (f" ({year})" if year else "") + " — сохрани! 🍿", "",
        *[f"{i}. {f}" for i, f in enumerate(facts, 1)], "",
        "Какой факт удивил тебя больше всего? Пиши в комментариях 👇", "",
        f"📺 YouTube: {YOUTUBE_URL}",
        f"✈️ Telegram: {TELEGRAM_URL} — новинки, трейлеры и подборки каждый день", "",
        "#фактыокино #кино #KinoMir360",
    ])
    tags: list[str] = []
    for tag in ["факты о фильме", "интересные факты о кино", t.lower(), f"{t.lower()} факты",
                f"{t.lower()} интересные факты", "как снимали", "съемки фильма"] + \
            [a.lower() for a in _actors(film, 3)] + ["KinoMir360", "киномир360", "шортс кино"]:
        if tag and tag not in tags and len(", ".join(tags + [tag])) <= 480:
            tags.append(tag)
    pinned = (f"Какой факт удивил больше? 👇\n"
              f"Ещё больше фактов и подборок — в нашем Telegram 👉 {TELEGRAM_URL}")
    return {"title": title, "description": description, "tags": ", ".join(tags), "pinned": pinned}


# ---------------------------------------------------------------- сборка

async def _render(work: Path, cover_path: Path, cards: list[dict[str, Any]], lines: list[str],
                  tag: str, header: str, question: str, music: Path | None) -> bool:
    """Голос + рендер видео work/short.mp4; возвращает, удалось ли озвучить."""
    voice, starts, voiced = await synthesize(lines, work)
    cfg = {
        "output": "short.mp4",
        "intro": cover_path.name,
        "logo": str(ASSETS / "logo.png"),
        "voice": voice.name,
        "music": str(music or ASSETS / "music.mp3"),
        "music_db": -14 if voiced else -6,
        "tail": 1.2,
        "fonts": {"head": str(ASSETS / "Oswald.ttf"), "sans": str(ASSETS / "Montserrat.ttf")},
        "tag": tag,
        "header": header,
        "cards": cards,
        "starts": starts,
        "outro": {
            "question": question,
            "channel": "KinoMir360",
            "youtube": "@kinomir360",
            "telegram": "@KinoMir360",
            "note": "ссылки в описании",
        },
    }
    cfg_path = work / "short.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    # рендер — отдельным процессом, чтобы не блокировать бота
    await _run(sys.executable, "-m", "shorts.make_short", str(cfg_path))
    return voiced


async def make_facts_short(fetch: Fetch, work: Path, query: str = "", exclude: set[int] | None = None,
                           music: Path | None = None) -> ShortResult:
    film, facts = await pick_fact_film(fetch, query, exclude or set())
    t, n, kind = _title(film), len(facts), _kind(film)
    hook = f"{n} {_facts_word(n)} о {kind} «{t}»"
    theme = Theme("facts", "ФАКТЫ", t.upper(), hook, hook, [])

    cards = []
    urls = await _stills(fetch, film)
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        cover_src = await _download(session, _poster_url(film))
        images: list[Image.Image] = []
        for url in urls:
            if len(images) >= n:
                break
            try:
                images.append(await _download(session, url))
            except Exception:  # noqa: BLE001
                continue
    if not images:
        raise RuntimeError(f"Не скачались картинки для «{t}»")
    for i, fact in enumerate(facts, 1):
        # название уже в заголовке слайда — на карточке не повторяем
        card = await asyncio.to_thread(make_card, images[(i - 1) % len(images)], "", i, n,
                                       None, "IMDb", None, f"ФАКТ №{i}", 780)
        path = work / f"card{i}.jpg"
        card.save(path, quality=93)
        cards.append({"image": path.name, "caption": fact})

    cover = await asyncio.to_thread(make_cover, cover_src, "ФАКТЫ", f"{hook}, которые ты не знал")
    cover_path = work / "cover.jpg"
    cover.save(cover_path, quality=93)

    lines = ([f"{hook}, которые ты точно не знал!"] + facts
             + ["Какой факт удивил тебя больше всего? Пиши в комментариях "
                "и подписывайся на КиноМир триста шестьдесят!"])
    voiced = await _render(work, cover_path, cards, lines, "ФАКТЫ", t.upper(),
                           "КАКОЙ ФАКТ\nУДИВИЛ БОЛЬШЕ?\nПИШИ В КОММЕНТАРИЯХ", music)
    result = ShortResult(work / "short.mp4", cover_path, theme, [film], build_facts_seo(film, facts))
    if not voiced:
        result.seo["warning"] = "⚠️ Голос не синтезировался — ролик собран только с музыкой."
    return result


async def make_daily_short(fetch: Fetch, work: Path, theme_key: str | None = None,
                           exclude: set[int] | None = None, music: Path | None = None,
                           today: date | None = None) -> ShortResult:
    """fetch(path, **params) -> dict — запрос к API Кинопоиска (PoiskKino._get)."""
    today = today or date.today()
    work.mkdir(parents=True, exist_ok=True)
    theme_key = theme_key or SCHEDULE[today.weekday()]
    if theme_key.startswith("facts"):
        query = theme_key.partition(":")[2].strip()
        try:
            return await make_facts_short(fetch, work, query, exclude, music)
        except RuntimeError:
            if query:
                raise
            log.exception("shorts: facts failed, falling back to genre")
            theme_key = "genre"
    theme = theme_for(theme_key, today)
    # в текстах зашито «5 ...», поэтому фильмов должно быть ровно FILMS;
    # если тема их не набрала — берём жанровую подборку
    films = await pick_films(fetch, theme, today, exclude or set())
    for _ in range(3):
        if len(films) >= FILMS:
            break
        log.info("shorts: theme %s gave %d films, falling back to genre", theme.key, len(films))
        theme = theme_for("genre", today)
        films = await pick_films(fetch, theme, today, exclude or set())
    if len(films) < FILMS:
        raise RuntimeError(f"Не набралось {FILMS} фильмов для подборки (тема {theme.key})")
    n = len(films)

    cards = []
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        cover_src = await _download(session, _poster_url(films[0]))
        for i, f in enumerate(films, 1):
            img = await _download(session, _image_url(f))
            rating, src = _rating(f)
            badge = None if rating else "НОВИНКА"
            if theme.upcoming:
                prem = _premiere(f)
                rating, badge = None, (f"С {_date_ru(prem).upper()}" if prem else "СКОРО")
            card = await asyncio.to_thread(make_card, img, _title(f), i, n, rating, src or "IMDb",
                                           _actors(f), badge)
            path = work / f"card{i}.jpg"
            card.save(path, quality=93)
            caption = " • ".join(x for x in [", ".join(_genres(f)[:2]).capitalize(),
                                             str(f.get("year") or "")] if x)
            cards.append({"image": path.name, "caption": caption, "info": info_rows(f)})

    headline = theme.hook.replace(", ", ",\n", 1)
    cover = await asyncio.to_thread(make_cover, cover_src, theme.tag, headline)
    cover_path = work / "cover.jpg"
    cover.save(cover_path, quality=93)

    voiced = await _render(work, cover_path, cards, voice_lines(theme, films), theme.tag, theme.header,
                           "КАКОЙ ПОСМОТРИШЬ\nПЕРВЫМ?\nПИШИ В КОММЕНТАРИЯХ", music)

    result = ShortResult(work / "short.mp4", cover_path, theme, films, build_seo(theme, films))
    if not voiced:
        result.seo["warning"] = "⚠️ Голос не синтезировался — ролик собран только с музыкой."
    return result

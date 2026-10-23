"""Ежедневный шортс: тема -> фильмы с Кинопоиска -> карточки, голос, видео, SEO."""

from __future__ import annotations

import asyncio
import html
import io
import json
import logging
import os
import random
import re
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp
from PIL import Image

from . import ai, clips, lumean, news, pronounce, tgsearch, tmdb, wiki
from .cards import card_overlay, make_card, make_cover

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
FILMS = 5
VOICE = "ru-RU-DmitryNeural"
VOICE_RATE = "+22%"
# запасной голос — Piper, работает прямо на сервере без интернета (модель кладёт Dockerfile)
PIPER_MODEL = Path(os.getenv("PIPER_MODEL", "/opt/piper/voice.onnx"))
PIPER_SPEED = 0.8  # length_scale: меньше — быстрее, ~как +22% у основного голоса
GAP = 0.2  # пауза между фразами, сек
MIN_RATING = 6.0  # ниже — рейтинг не показываем, он только отпугнёт
SKIP_RATING = 5.5  # ниже — фильм в подборку не берём

YOUTUBE_URL = "https://youtube.com/@kinomir360"
TELEGRAM_URL = "https://t.me/KinoMir360"

# По дням недели (пн..вс)
SCHEDULE = ["online", "cinema", "facts", "genre", "news", "upcoming", "facts"]

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

ProgressFn = Callable[[int, str], Awaitable[None]]
on_progress: ProgressFn | None = None  # бот ставит перед сборкой — двигает полоску «▓▓░░ 35%»


last_stage = ""  # последний этап сборки — если она зависнет, бот скажет, на чём


async def _progress(pct: int, stage: str) -> None:
    global last_stage
    last_stage = f"{pct}% — {stage}"
    log.info("shorts: %s", last_stage)
    if on_progress is None:
        return
    try:
        await on_progress(max(0, min(pct, 100)), stage)
    except Exception as exc:  # noqa: BLE001 — полоска не должна ломать сборку
        log.debug("shorts: progress update failed: %s", exc)


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


async def _download_first(session: aiohttp.ClientSession, urls: list[str | None]) -> Image.Image:
    """Первая картинка, которая скачалась (TMDB, потом Кинопоиск)."""
    last: Exception | None = None
    for url in urls:
        if not url:
            continue
        try:
            return await _download(session, url)
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise RuntimeError(f"Не скачалась ни одна картинка: {last}")


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


async def pick_fact_film(fetch: Fetch, query: str, exclude: set[int],
                         web: bool = False) -> tuple[dict[str, Any], list[str]]:
    """Фильм и его факты с Кинопоиска (все подходящие). web=True — факты найдёт ИИ в интернете,
    поэтому названный автором фильм берём, даже если на Кинопоиске фактов мало."""
    if query.startswith("id:") and query[3:].isdigit():
        # выбран из подсказок поиска — точный фильм по id Кинопоиска
        cands = [{"id": int(query[3:])}]
        need = 0 if web else 3
    elif query:
        # «Мастер и Маргарита 2024» — год в конце уточняет, какой из одноимённых фильмов
        year_m = re.search(r"\s(19\d\d|20\d\d)$", query)
        year = int(year_m.group(1)) if year_m else None
        name = query[: year_m.start()].strip() if year_m else query
        try:
            data = await fetch("/movie/search", query=name, page=1, limit=10)
            cands = [x for x in data.get("docs") or [] if isinstance(x, dict)]
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Поиск «{query}» не удался: {exc}") from exc
        if year:
            cands = [c for c in cands if c.get("year") == year] or cands
        # одноимённые (фильм 2024 и сериал 2005) — берём самый известный, по числу оценок
        cands.sort(key=lambda c: -int((c.get("votes") or {}).get("kp") or 0))
        need = 0 if web else 3
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
            return full, facts
    raise RuntimeError(f"Не нашёл фильм с {need}+ подходящими фактами" + (f" по запросу «{query}»" if query else ""))


fact_channels: list[str] = list(tgsearch.DEFAULT_CHANNELS)  # бот подставляет список из настроек


async def web_facts(film: dict[str, Any], kp_facts: list[str], gemini_key: str,
                    n: int = FACTS, rumors: bool = False,
                    media_out: list[dict[str, Any]] | None = None,
                    progress: ProgressFn | None = None) -> tuple[list[str], list[str]]:
    """Факты из интернета (поиск Google через Gemini, Википедия, Telegram-каналы) и с Кинопоиска.
    rumors=True — фильм ещё не вышел: новости и слухи о нём.
    media_out — сюда добавляются посты каналов с фото или видео (материал для ролика).
    progress(процент, этап) — для полоски в боте."""
    en = str(film.get("alternativeName") or film.get("enName") or "")
    if progress:
        await progress(15, "Читаю Википедию и Telegram-каналы")
    article, posts = await asyncio.gather(
        wiki.film_article(_title(film), en, film.get("year")),
        tgsearch.search(fact_channels, [_title(film), en], per_channel=6 if rumors else tgsearch.POSTS_PER_CHANNEL))
    if progress:
        await progress(40, f"Нашёл постов в каналах: {len(posts)}. ИИ ищет в Google и выбирает лучшее")
    facts, sources = await ai.web_facts(gemini_key, _title(film), en, film.get("year"), _kind(film), kp_facts,
                                        article, n, tgsearch.as_text(posts, 14000 if rumors else 9000), rumors)
    if media_out is not None:
        media_out.extend(p for p in posts if p.get("image") or p.get("video"))
    channels = list(dict.fromkeys(f"t.me/{p['channel']}" for p in posts))
    return facts, [s for s in sources if s != "Telegram-каналы"] + channels


async def gather_facts(film: dict[str, Any], kp_facts: list[str], gemini_key: str,
                       media_out: list[dict[str, Any]] | None = None, rumors: bool = False) -> tuple[list[str], str]:
    """(факты для ролика, заметка автору). Без ИИ или при сбое — факты Кинопоиска, как раньше.
    media_out — посты Telegram-каналов с фото и видео о фильме."""
    if ai.available(gemini_key):
        await _progress(7, "ИИ ищет факты в интернете")
        try:
            facts, sources = await web_facts(film, kp_facts, gemini_key, media_out=media_out, rumors=rumors)
            for f in kp_facts:  # ИИ нашёл меньше пяти — добираем Кинопоиском
                if len(facts) >= FACTS:
                    break
                if f not in facts:
                    facts.append(f)
            src = ", ".join(sources[:6]) or "интернет"
            return facts, f"🔎 Факты нашёл ИИ в интернете ({src}) — проверь перед публикацией."
        except Exception as exc:  # noqa: BLE001
            log.warning("shorts: web facts for %s failed: %s", _title(film), exc)
            note = f"⚠️ Факты в интернете не нашлись ({str(exc)[:200]}) — взял с Кинопоиска."
    else:
        note = ""
    if len(kp_facts) < 3:
        raise RuntimeError(f"У «{_title(film)}» мало фактов на Кинопоиске, а ИИ их в интернете не нашёл")
    return random.sample(kp_facts[:12], min(FACTS, len(kp_facts[:12]))), note


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


# ---------------------------------------------------------------- фрагменты трейлеров

CLIP_SECONDS = 5.0


TRAILER_BUDGET = 75  # сек на трейлер одного фильма: не уложились — карточка с кадром


async def _trailer_clip(item: dict[str, Any], out: Path) -> bool:
    """Скачать 5 секунд трейлера с Rutube в out; False — не нашлось или не скачалось."""
    titles = [str(item.get(k) or "") for k in ("name", "alternativeName", "enName")]

    async def fetch() -> bool:
        found = await clips.rutube_trailer([t for t in titles if t], item.get("year"))
        if not found:
            log.info("shorts: no Rutube trailer for %s", _title(item))
            return False
        await clips.download_clip(found["url"], out, clips.clip_start(found["duration"], CLIP_SECONDS),
                                  CLIP_SECONDS, timeout=60)
        return out.exists()

    try:
        return await asyncio.wait_for(fetch(), timeout=TRAILER_BUDGET)
    except Exception as exc:  # noqa: BLE001 — без трейлера останется кадр
        log.warning("shorts: trailer clip for %s failed: %s", _title(item), exc)
        return False


async def _fact_clips(item: dict[str, Any], facts_by_idx: list[tuple[int, str]], work: Path,
                     gemini_key: str) -> tuple[dict[int, str], str]:
    """Для фактов — фрагменты трейлера, где видно то, о чём факт: ({номер факта: файл клипа}, пояснение).

    Трейлер с Rutube качается целиком, режется на кадры, Gemini выбирает кадр под каждый факт
    (или «ничего не подходит»), вокруг выбранного момента вырезается CLIP_SECONDS.
    """
    titles = [str(item.get(k) or "") for k in ("name", "alternativeName", "enName")]
    trailer_dir = work / "trailer"
    try:
        found = await clips.rutube_trailer([t for t in titles if t], item.get("year"))
        if not found:
            log.info("shorts: no Rutube trailer for facts about %s", _title(item))
            return {}, "трейлер на Rutube не нашёлся"
        trailer_dir.mkdir(exist_ok=True)
        src = await clips.download_full(found["url"], trailer_dir)
        frames = await clips.sample_frames(src, trailer_dir, duration=found["duration"])
        if not frames:
            return {}, "не удалось нарезать кадры из трейлера"
        thumbs = [Image.open(path).convert("RGB") for _, path in frames]
        choices = await ai.match_images(gemini_key, [text for _, text in facts_by_idx], thumbs)
        if not choices:
            return {}, f"ИИ не ответил ({ai.last_error or 'без подробностей'})"
        out: dict[int, str] = {}
        used: set[int] = set()
        for (i, _), c in zip(facts_by_idx, choices):
            if c < 0 or c in used:
                continue
            used.add(c)
            clip = work / f"fclip{i}.mp4"
            # момент чуть раньше выбранного кадра — чтобы действие успело начаться в кадре
            await clips.cut_clip(src, clip, frames[c][0] - 1.5, CLIP_SECONDS)
            out[i] = clip.name
        return out, "" if out else f"в трейлере «{found['title'][:60]}» нет моментов под эти факты"
    except Exception as exc:  # noqa: BLE001 — без трейлера будут фото и кадры
        log.warning("shorts: trailer clips for facts about %s failed: %s", _title(item), exc)
        return {}, f"ошибка: {type(exc).__name__}: {str(exc)[:150]}"
    finally:
        import shutil
        shutil.rmtree(trailer_dir, ignore_errors=True)


# ---------------------------------------------------------------- новости кино

NEWS = 5


async def _news_film(fetch: Fetch, item: news.NewsItem) -> dict[str, Any] | None:
    """Фильм из «кавычек» заголовка — через поиск Кинопоиска (poiskkino)."""
    if not item.film_query:
        return None
    try:
        data = await fetch("/movie/search", query=item.film_query, page=1, limit=3)
    except Exception:  # noqa: BLE001
        return None
    want = item.film_query.lower()
    for d in data.get("docs") or []:
        names = {str(d.get(k) or "").lower() for k in ("name", "alternativeName", "enName")}
        if want in names and _image_url(d):
            return d
    return None


def _placeholder() -> Image.Image:
    """Фон для новости без картинки: тёмный градиент с логотипом."""
    bg = Image.linear_gradient("L").resize((1280, 720)).point(lambda v: 20 + v // 6)
    img = Image.merge("RGB", (bg, bg.point(lambda v: v * 3 // 4), bg.point(lambda v: v // 2)))
    logo = Image.open(ASSETS / "logo.png").convert("RGBA")
    logo.thumbnail((520, 520))
    img.paste(logo, ((1280 - logo.width) // 2, (720 - logo.height) // 2), logo)
    return img


def _tg_news(posts: list[dict[str, Any]], exclude: set[int]) -> list[news.NewsItem]:
    """Посты каналов как новости: первая фраза — заголовок, дальше — подробности."""
    out = []
    for p in posts[:20]:
        first, _, rest = re.sub(r"\s+", " ", p["text"]).partition(". ")
        title = first[:140].rstrip(" .") if len(first) <= 160 else first[:140].rsplit(" ", 1)[0] + "…"
        item = news.NewsItem(title=title, lead=rest[:300], source=f"t.me/{p['channel']}", link=p["url"],
                             published=p["time"], image=p.get("image"), video=p.get("video"))
        quoted = re.search(r"«([^»]{2,60})»", p["text"])
        item.film_query = quoted.group(1) if quoted else None
        if item.uid not in exclude:
            out.append(item)
    return out


async def make_news_short(fetch: Fetch, work: Path, feeds: list[str], exclude: set[int] | None = None,
                          music: Path | None = None, tmdb_key: str = "",
                          today: date | None = None, gemini_key: str = "") -> ShortResult:
    today = today or date.today()
    await _progress(5, "Читаю ленты новостей")
    items = await news.latest_news(feeds, exclude or set())
    # ИИ-редактор: главные новости без местных событий и повторов, пересказ понятными словами
    ai.last_error = None
    retold: list[str] | None = None
    if ai.available(gemini_key):
        # свежие посты Telegram-каналов — ИИ сверяет их с новостями СМИ и берёт то, чего в лентах ещё нет
        await _progress(7, "Читаю Telegram-каналы")
        items = items[:25] + _tg_news(await tgsearch.latest(fact_channels, news.MAX_AGE_HOURS), exclude or set())
    if ai.available(gemini_key) and len(items) >= 3:
        await _progress(8, "ИИ выбирает главные новости")
        candidates = items[:45]
        picked = await ai.pick_news(gemini_key, [(it.title, it.lead, it.source) for it in candidates], NEWS)
        if picked:
            items = [candidates[p["i"]] for p in picked]
            retold = [p["text"] for p in picked]
            for it, p in zip(items, picked):
                it.film_query = p["film"] or it.film_query  # по-русски — картинку найдём на Кинопоиске
    await _progress(12, "Качаю картинки к новостям")
    # сначала новости с картинкой (кадр фильма или фото из ленты), потом остальные
    with_img: list[tuple[news.NewsItem, list[str]]] = []
    without: list[tuple[news.NewsItem, list[str]]] = []
    for item in items[:25]:
        urls: list[str] = []
        film = await _news_film(fetch, item)
        if film:
            item.film_name = _title(film)
            backdrops, _ = await tmdb.images(tmdb_key, film, limit=1)
            urls = backdrops + [u for u in [_image_url(film)] if u]
        if item.image:
            urls.append(item.image)
        (with_img if urls else without).append((item, urls))
        if len(with_img) >= NEWS and not retold:
            break
    if retold:  # порядок редактора: самое интересное первым; картинки нет — будет заставка
        chosen = [(it, next(u for i2, u in with_img + without if i2 is it)) for it in items]
    else:
        chosen = sorted((with_img + without)[:NEWS], key=lambda x: -x[0].published)
    if len(chosen) < 3:
        raise RuntimeError(f"Мало свежих новостей ({len(chosen)}). Проверь ленты: /feeds")
    n = len(chosen)

    cards = []
    images: list[Image.Image] = []
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for item, urls in chosen:
            try:
                images.append(await _download_first(session, urls))
            except Exception:  # noqa: BLE001
                images.append(_placeholder())
    for i, ((item, _), img) in enumerate(zip(chosen, images), 1):
        card = await asyncio.to_thread(make_card, img, "", i, n, None, "IMDb", None, f"НОВОСТЬ №{i}", FACT_CARD_H)
        path = work / f"card{i}.jpg"
        card.save(path, quality=93)
        video: dict[str, str] = {}
        if item.video:  # видео из поста канала — 5 секунд из середины
            try:
                files = await tgsearch.download_media([{"video": item.video}], work, 1, prefix=f"nv{i}_")
                if files:
                    dur = await _duration(files[0])
                    clip = work / f"newsclip{i}.mp4"
                    await clips.cut_clip(files[0], clip, max(0.0, dur / 2 - CLIP_SECONDS / 2), min(CLIP_SECONDS, dur))
                    card_overlay("", i, n, None, "IMDb", None, f"НОВОСТЬ №{i}", FACT_CARD_H).save(
                        work / f"overlay{i}.png")
                    video = {"clip": clip.name, "overlay": f"overlay{i}.png"}
            except Exception as exc:  # noqa: BLE001 — останется картинка
                log.warning("shorts: news video %s skipped: %s", item.video[:80], exc)
        cards.append({"image": path.name, "caption": item.title, "info": [[("Источник", item.source)]],
                      "karaoke": True, **video})

    day = _date_ru(today)
    cover = await asyncio.to_thread(make_cover, images[0], "НОВОСТИ", f"Главные новости кино\n{day}")
    cover_path = work / "cover.jpg"
    cover.save(cover_path, quality=93)

    def say(item: news.NewsItem) -> str:
        title = item.title if item.title[-1] in ".!?…" else item.title + "."
        return f"{title} {item.lead}".strip()

    # пересказ редактора; не вышло — Gemini пересказывает по заголовкам; без ИИ — заголовок и лид как есть
    if not retold:
        await _progress(30, "ИИ пересказывает новости")
        retold = await ai.retell_news(gemini_key, [(it.title, it.lead, it.source) for it, _ in chosen])
    spoken = retold or [say(it) for it, _ in chosen]
    # дата — только на обложке: голосом «на двадцать третье октября» звучит затянуто
    lines = (["Главные новости кино!"] + spoken
             + [f"Какая новость удивила? Пиши в комментариях! {SAVE_AND_SUBSCRIBE}"])
    voiced = await _render(work, cover_path, cards, lines, "НОВОСТИ", "НОВОСТИ КИНО",
                           "КАКАЯ НОВОСТЬ\nУДИВИЛА БОЛЬШЕ?\nПИШИ В КОММЕНТАРИЯХ", music)

    first = chosen[0][0].title
    title = f"Новости кино {day}: {first}"
    title = (title if len(title) <= 88 else title[:85].rstrip() + "…") + " #shorts"
    description = "\n".join([
        f"Главные новости кино на {day} 🎬", "",
        *[f"{i}. {it.title} ({it.source})" for i, (it, _) in enumerate(chosen, 1)], "",
        "Какая новость удивила больше всего? Пиши в комментариях 👇", "",
        f"📺 YouTube: {YOUTUBE_URL}",
        f"✈️ Telegram: {TELEGRAM_URL} — новинки, трейлеры и подборки каждый день", "",
        "#новостикино #кино #KinoMir360",
    ])
    tags: list[str] = []
    for tag in ["новости кино", "новости кино сегодня", "новости сериалов", "кино новости", "что нового в кино"] + \
            [it.film_name.lower() for it, _ in chosen if it.film_name] + ["KinoMir360", "киномир360", "шортс кино"]:
        if tag not in tags and len(", ".join(tags + [tag])) <= 480:
            tags.append(tag)
    seo = {"title": title, "description": description, "tags": ", ".join(tags),
           "pinned": f"Новости кино каждый день — в нашем Telegram 👉 {TELEGRAM_URL}\nКакая новость главная? 👇"}
    theme = Theme("news", "НОВОСТИ", "НОВОСТИ КИНО", f"Главные новости кино на {day}", title, [])
    result = ShortResult(work / "short.mp4", cover_path, theme,
                         [{"id": it.uid, "name": it.title} for it, _ in chosen], seo)
    if ai.last_error:
        result.seo["warning"] = f"⚠️ ИИ не пересказал новости: {ai.last_error}"
    if not voiced or tts_note:
        result.seo["warning"] = no_voice_note(voiced)
    return result


# ---------------------------------------------------------------- голос

NUMBERS = {1: "один", 2: "два", 3: "три", 4: "четыре", 5: "пять", 6: "шесть", 7: "семь", 8: "восемь",
           9: "девять", 10: "десять"}
SAVE_AND_SUBSCRIBE = "Сохрани и подпишись на КиноМир триста шестьдесят!"


def _say_film(item: dict[str, Any], theme: Theme, place: int) -> str:
    """Обратный отсчёт: «Номер пять — «X». Комедия.» … «И номер один — «X»!»."""
    title = f"«{_title(item)}»"
    if theme.key.startswith("genre:"):
        # в жанровой подборке жанр и так ясен («Ужасы.» на каждом фильме звучит глупо) — называем год
        year = f", {item['year']} год" if item.get("year") else ""
        return f"И номер один — {title}{year}!" if place == 1 else f"Номер {NUMBERS.get(place, place)} — {title}{year}."
    parts = [f"И номер один — {title}!" if place == 1 else f"Номер {NUMBERS.get(place, place)} — {title}."]
    genres = _genres(item)
    if theme.upcoming and _premiere(item):
        parts.append(f"В кино с {_date_ru(_premiere(item))}.")
    elif genres:
        parts.append(f"{genres[0].capitalize()}.")
    return " ".join(parts)


def voice_lines(theme: Theme, films: list[dict[str, Any]]) -> list[str]:
    # короткое начало — первые секунды решают, досмотрят ли ролик; «сохрани» — в конце
    n = len(films)
    return ([f"{theme.hook}!"]
            + [_say_film(f, theme, n - i) for i, f in enumerate(films)]
            + [f"Какой посмотришь первым? Пиши в комментариях! {SAVE_AND_SUBSCRIBE}"])


def _rank_key(item: dict[str, Any]) -> tuple[float, int]:
    """Чем выше, тем ближе к №1: лучший рейтинг, при равенстве — больше оценок."""
    return _best_rating(item)[0], int((item.get("votes") or {}).get("kp") or 0)


async def _run(*cmd: str) -> str:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, cwd=ROOT.parent)
    try:
        out, err = await proc.communicate()
    except asyncio.CancelledError:
        proc.kill()  # сборку прервали по таймауту — не оставляем рендер жрать память
        raise
    if proc.returncode:
        raise RuntimeError(f"{cmd[0]} failed: {err.decode(errors='ignore')[-800:]}")
    return out.decode(errors="ignore")


async def _duration(path: Path) -> float:
    out = await _run("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path))
    return float(out.strip())


def no_voice_note(voiced: bool = False) -> str:
    reason = f" Причина: {tts_error}." if tts_error else ""
    if voiced:
        return tts_note or ""
    return f"⚠️ Голос не синтезировался — ролик собран только с музыкой.{reason} Попробуй собрать ещё раз."


TTS_TIMEOUT = 20  # сек на фразу (обычно отвечает за 1–3 с): без лимита запрос к сервису голоса может висеть бесконечно


async def _tts(text: str, path: Path) -> list[float]:
    """Синтез в mp3; возвращает время начала каждого слова (сек) — для субтитров-караоке."""
    import edge_tts

    words: list[float] = []
    with open(path, "wb") as out:
        async for chunk in edge_tts.Communicate(text, VOICE, rate=VOICE_RATE, boundary="WordBoundary").stream():
            if chunk["type"] == "audio":
                out.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                words.append(chunk["offset"] / 10_000_000)  # единицы по 100 нс
    return words


TTS_RETRY_DELAYS = (4, 12)  # сервис голоса иногда сбоит/ограничивает частые запросы — повторяем, а не сдаёмся
TTS_PAUSE = 0.5  # пауза между фразами: частые запросы подряд сервис иногда отвергает «No audio»
TTS_COOLDOWN = 20  # фраза не прошла — ждём подольше и пробуем её целиком ещё раз
tts_error: str | None = None  # причина, если голос так и не получился — показываем админу
tts_note: str | None = None  # пометка, если ролик озвучен запасным голосом


async def _say_once(text: str, path: Path) -> list[float]:
    """Одна попытка; не принял знак ударения — сразу повторяем без него."""
    try:
        return await asyncio.wait_for(_tts(text, path), timeout=TTS_TIMEOUT)
    except Exception:
        if pronounce.STRESS not in text:
            raise
        log.warning("shorts: TTS failed with stress marks, retrying without them")
        return await asyncio.wait_for(_tts(text.replace(pronounce.STRESS, ""), path), timeout=TTS_TIMEOUT)


async def _edge_to_file(text: str, path: Path) -> list[float]:
    """Основной голос (Microsoft Edge) с повторами при сбое сервиса."""
    for delay in (*TTS_RETRY_DELAYS, None):
        try:
            return await _say_once(text, path)
        except Exception as exc:  # noqa: BLE001
            if delay is None:
                raise
            log.warning("shorts: TTS attempt failed (%r), retry in %ss", exc, delay)
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


def piper_ready() -> bool:
    import importlib.util

    return importlib.util.find_spec("piper") is not None and PIPER_MODEL.exists()


async def _piper_batch(items: list[tuple[str, Path]]) -> None:
    """Запасной голос Piper в mp3 для нескольких фраз — отдельным процессом (память освобождается сразу)."""
    wavs = [(text.replace(pronounce.STRESS, ""), str(path.with_suffix(".piper.wav"))) for text, path in items]
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "shorts.piper_tts", str(PIPER_MODEL), str(PIPER_SPEED),
        stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=ROOT.parent)
    try:
        _, err = await asyncio.wait_for(proc.communicate(json.dumps(wavs).encode()), timeout=180)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        proc.kill()
        raise
    if proc.returncode:
        raise RuntimeError(f"piper: {err.decode(errors='ignore')[-300:]}")
    for (_, wav), (_, path) in zip(wavs, items):
        await _run("ffmpeg", "-v", "error", "-y", "-i", wav, "-c:a", "libmp3lame", "-q:a", "3", str(path))
        Path(wav).unlink(missing_ok=True)


async def _piper_to_file(text: str, path: Path) -> list[float]:
    """Одна фраза запасным голосом; времени слов нет — субтитры идут равномерно по фразе."""
    await _piper_batch([(text, path)])
    return []


last_engine = "edge"  # каким голосом озвучена последняя фраза (для /voice)
lumean_error = ""  # почему не сработал голос ElevenLabs (Lumean) — для /voice


async def say_to_file(text: str, path: Path) -> list[float]:
    """Озвучить фразу в mp3 (время слов в ответе): основной голос, при сбое — запасной Piper."""
    global last_engine, lumean_error
    lumean_error = "" if lumean.enabled() else "нет переменной LUMEAN_API_KEY"
    if lumean.enabled():
        try:
            await lumean.synthesize([text.replace(pronounce.STRESS, "")], [path], path.parent)
            last_engine = "lumean"
            return []
        except Exception as exc:  # noqa: BLE001
            log.warning("shorts: Lumean TTS failed (%s), using Edge", exc)
            lumean_error = str(exc)[:300]
    try:
        words = await _edge_to_file(text, path)
        last_engine = "edge"
        return words
    except Exception as exc:  # noqa: BLE001
        if not piper_ready():
            raise
        log.warning("shorts: Edge TTS failed (%r), using Piper", exc)
        words = await _piper_to_file(text, path)
        last_engine = f"piper ({str(exc).rstrip('.') or type(exc).__name__})"
        return words


async def spoken_lines(lines: list[str]) -> list[str]:
    """Текст для голоса: словарь произношения, затем ИИ пишет числа словами в нужном падеже и латиницу
    русскими буквами; что ИИ не исправил (или он недоступен) — простая замена без ИИ."""
    texts = [pronounce.dates(pronounce.apply(text)) for text in lines]  # даты — сразу в нужном падеже
    todo = [i for i, text in enumerate(texts) if pronounce.needs_rewrite(text)]
    if todo:
        try:  # не задерживаем сборку, если ИИ тормозит
            fixed = await asyncio.wait_for(ai.spoken_form([texts[i] for i in todo]), timeout=60)
        except asyncio.TimeoutError:
            fixed = None
        for i, text in zip(todo, fixed or []):
            texts[i] = pronounce.apply(text)  # ИИ мог потерять знак ударения из словаря — ставим снова
    return [_speakable(pronounce.plain_russian(text)) for text in texts]


def _speakable(text: str) -> str:
    """Убираем эмодзи и редкие символы — на них сервис голоса может ответить «No audio»."""
    text = re.sub(rf"[^\w\s.,!?:;«»\"'()%+\-–—{pronounce.STRESS}]", " ", text)
    text = re.sub(r"\s+([.,!?:;])", r"\1", re.sub(r"\s+", " ", text))
    return text.strip() or "…"


def _pieces(text: str) -> list[str]:
    """Фраза по предложениям, длинные — ещё и по запятым."""
    out: list[str] = []
    for sent in re.split(r"(?<=[.!?…])\s+", text):
        if len(sent) > 90 and "," in sent:
            out += [p.strip() for p in re.split(r"(?<=,)\s+", sent) if p.strip()]
        elif sent.strip():
            out.append(sent.strip())
    return out


async def _edge_pieces(text: str, path: Path, stem: Path) -> tuple[list[float], list[str]]:
    """Озвучить фразу кусками основным голосом и склеить; куски, которые не вышли, пропускаем.

    Возвращает (время слов во всей фразе, тексты пропущенных кусков). Если не вышло ничего — тишина.
    """
    files: list[Path] = []
    words: list[float] = []
    lost: list[str] = []
    t = 0.0
    for k, piece in enumerate(_pieces(text)):
        out = stem.with_name(f"{stem.name}_p{k}.mp3")
        if k:
            await asyncio.sleep(TTS_PAUSE * 2)
        try:
            got = await _say_once(piece, out)
        except Exception as exc:  # noqa: BLE001
            log.warning("shorts: Edge TTS piece failed %r: %r", piece[:60], exc)
            lost.append(piece)
            continue
        words += [t + w for w in got]
        t += await _duration(out) + 0.1
        files.append(out)
    if not files:
        await _run("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                   "-t", f"{max(2.5, min(len(text) / 14, 8.0)):.2f}", str(path))
        return [], lost
    inputs = [arg for f in files for arg in ("-i", str(f))]
    chain = "".join(f"[{i}:a]aresample=24000,apad=pad_dur=0.1[a{i}];" for i in range(len(files)))
    chain += "".join(f"[a{i}]" for i in range(len(files))) + f"concat=n={len(files)}:v=0:a=1[out]"
    await _run("ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", chain, "-map", "[out]",
               "-c:a", "libmp3lame", "-q:a", "3", str(path))
    return words, lost


async def synthesize(lines: list[str], work: Path,
                     pause: float | None = None) -> tuple[Path, list[float], bool, list[dict[str, Any]]]:
    """Озвучка по фразам: файл, начало каждой фразы, есть ли голос, и по фразе {длительность, время слов}."""
    global tts_error, tts_note
    tts_error, tts_note = None, None
    speak = await spoken_lines(lines)
    parts: list[Path] = [work / f"voice_{i}.mp3" for i in range(len(lines))]
    words: list[list[float]] = []
    failed: list[int] = []
    dropped: list[str] = []
    lumean_note = ""
    if lumean.enabled():
        # основной голос — ElevenLabs через Lumean; не вышло — как раньше, голос Microsoft
        try:
            await lumean.synthesize([t.replace(pronounce.STRESS, "") for t in speak], parts, work,
                                    pause or lumean.PAUSE)
            words = [[] for _ in speak]
        except Exception as exc:  # noqa: BLE001
            log.warning("shorts: Lumean TTS failed: %s", exc)
            lumean_note = f"🗣 Голос ElevenLabs (Lumean) не сработал ({str(exc)[:200]}) — озвучено голосом Microsoft."
    try:
        for i, text in enumerate(speak if not words else []):
            if i:
                await asyncio.sleep(TTS_PAUSE)
            try:
                words.append(await _edge_to_file(text, parts[i]))
                continue
            except Exception as exc:  # noqa: BLE001
                if not words:
                    raise  # первая же фраза не озвучилась — сервис недоступен целиком
                log.warning("shorts: Edge TTS rejected phrase %d (%r), cooling down", i + 1, exc)
            await asyncio.sleep(TTS_COOLDOWN)  # чаще всего это временное ограничение частых запросов
            try:
                words.append(await _say_once(text, parts[i]))
                continue
            except Exception as exc:  # noqa: BLE001
                # сервис работает, но эта фраза ему не нравится — озвучиваем её по кусочкам
                log.warning("shorts: Edge TTS rejected phrase %d (%r), trying pieces", i + 1, exc)
                got, lost = await _edge_pieces(text, parts[i], work / f"voice_{i}")
                words.append(got)
                dropped += lost
    except Exception as exc:  # noqa: BLE001
        bad = len(words)
        reason = f"{str(exc).rstrip('.') or type(exc).__name__}; фраза №{bad + 1}: «{speak[bad][:60]}»"
        log.warning("shorts: Edge TTS failed (%s)", reason)
        words = []
        if piper_ready():
            # весь ролик одним голосом: переозвучиваем все фразы запасным
            tts_note = f"🗣 Основной голос не ответил ({reason[:220]}) — озвучено запасным голосом."
            try:
                await _piper_batch(list(zip(speak, parts)))
                words = [[] for _ in speak]
            except Exception as exc2:  # noqa: BLE001
                log.exception("shorts: Piper failed too")
                reason, words = f"{reason}; запасной голос: {exc2}", []
        if len(words) < len(speak):
            tts_error, failed, words = reason[:250], list(range(len(speak))), [[] for _ in speak]
            for i, text in enumerate(lines):
                sec = max(2.5, min(len(text) / 14, 8.0))  # примерно столько звучала бы фраза
                await _run("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                           "-t", f"{sec:.2f}", str(parts[i]))
    voiced = not failed
    if lumean_note and not tts_note:
        tts_note = lumean_note
    if dropped and not tts_note:
        tts_note = ("🗣 Основной голос не смог озвучить кусок текста — в ролике он без голоса (на экране есть): "
                    + "; ".join(f"«{d[:60]}»" for d in dropped[:3]))

    starts, spans, t = [], [], 0.0
    inputs, chains = [], []
    for i, part in enumerate(parts):
        starts.append(round(t, 3))
        dur = await _duration(part)
        spans.append({"speech": round(dur, 3), "words": [round(w, 3) for w in words[i]],
                      "text": speak[i].replace(pronounce.STRESS, "")})
        t += dur + GAP
        inputs += ["-i", str(part)]
        chains.append(f"[{i}:a]aresample=48000,apad=pad_dur={GAP}[a{i}]")
    filt = ";".join(chains) + ";" + "".join(f"[a{i}]" for i in range(len(parts))) + \
        f"concat=n={len(parts)}:v=0:a=1[out]"
    voice = work / "voice.wav"
    await _run("ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", filt, "-map", "[out]", str(voice))
    return voice, starts, voiced, spans


async def word_timeline(voice: Path, starts: list[float], spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Субтитры по одному слову: [{"w", "s", "e"}] на всю озвучку.

    Точно — распознаванием готовой дорожки (Whisper на Groq); нет ключа или сбой — по тексту фраз:
    время слова из синтеза (голос Microsoft его даёт) или пропорционально длине слова.
    """
    if ai.RESERVE_KEY and "groq.com" in ai.RESERVE_URL:
        mp3 = voice.with_suffix(".words.mp3")
        try:
            await _run("ffmpeg", "-v", "error", "-y", "-i", str(voice), "-ac", "1", "-ar", "16000",
                       "-c:a", "libmp3lame", "-b:a", "48k", str(mp3))
            words = await ai.transcribe_words(mp3.read_bytes())
            if words:
                return words
        except Exception as exc:  # noqa: BLE001 — тогда по тексту
            log.warning("shorts: word timestamps failed: %s", exc)
        finally:
            mp3.unlink(missing_ok=True)
    out: list[dict[str, Any]] = []
    for start, span in zip(starts, spans):
        tokens = [w for w in str(span.get("text") or "").split() if re.search(r"\w", w)]
        if not tokens:
            continue
        times = span.get("words") or []
        if len(times) >= len(tokens):
            marks = [start + t for t in times[:len(tokens)]]
        else:
            total = sum(len(w) + 1 for w in tokens)
            acc, marks = 0, []
            for w in tokens:
                marks.append(start + span["speech"] * acc / total)
                acc += len(w) + 1
        for k, (w, m) in enumerate(zip(tokens, marks)):
            end = marks[k + 1] if k + 1 < len(marks) else start + span["speech"]
            out.append({"w": w, "s": round(m, 2), "e": round(end, 2)})
    return out


# ---------------------------------------------------------------- SEO

def build_seo(theme: Theme, films: list[dict[str, Any]]) -> dict[str, str]:
    names = [_title(f) for f in films]
    title = f"{theme.seo_title} #shorts"
    if len(title) > 100:
        title = title[:90].rstrip() + "… #shorts"

    rows = []
    for i, f in enumerate(films):
        year = f.get("year")
        line = f"{len(films) - i}. {_title(f)}" + (f" ({year})" if year else "")
        rating, src = _rating(f)
        if theme.upcoming and _premiere(f):
            line += f" — в кино с {_date_ru(_premiere(f))}"
        elif rating:
            line += f" — {src} {rating:.1f}"
        if f.get("_awards"):
            line += f" 🏆 {f['_awards']}"
        rows.append(line)

    hashtags = ["#чтопосмотреть", "#новинкикино", "#KinoMir360"]
    if theme.key == "series":
        hashtags[1] = "#сериалы"
    elif theme.key == "upcoming":
        hashtags[1] = "#скоровкино"
    elif theme.key.startswith("genre:") or theme.key == "custom":
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


def build_rumors_seo(film: dict[str, Any], facts: list[str]) -> dict[str, str]:
    t, kind = _title(film), _kind(film)
    title = f"Что известно о {kind} «{t}»: новости и слухи 🔥 #shorts"
    if len(title) > 100:
        title = f"«{t}»: новости и слухи 🔥 #shorts"[:100]
    year = film.get("year")
    description = "\n".join([
        f"Что известно о {kind} «{t}»" + (f" ({year})" if year else "") + " — главные новости и слухи 🔥", "",
        *[f"{i}. {f}" for i, f in enumerate(facts, 1)], "",
        "Ждёшь премьеру? Пиши в комментариях 👇", "",
        f"📺 YouTube: {YOUTUBE_URL}",
        f"✈️ Telegram: {TELEGRAM_URL} — новинки, трейлеры и подборки каждый день", "",
        "#новостикино #слухи #KinoMir360",
    ])
    tags: list[str] = []
    for tag in [t.lower(), f"{t.lower()} дата выхода", f"{t.lower()} новости", f"{t.lower()} слухи",
                f"{t.lower()} трейлер", "новости кино", "скоро выйдет"] + \
            [a.lower() for a in _actors(film, 3)] + ["KinoMir360", "киномир360", "шортс кино"]:
        if tag and tag not in tags and len(", ".join(tags + [tag])) <= 480:
            tags.append(tag)
    pinned = (f"Ждёшь премьеру «{t}»? 👇\n"
              f"Все новости о новинках — в нашем Telegram 👉 {TELEGRAM_URL}")
    return {"title": title, "description": description, "tags": ", ".join(tags), "pinned": pinned}


# ---------------------------------------------------------------- TikTok

# в TikTok нет названия и кликабельных ссылок: короткая подпись с ключевыми словами + хэштеги
# на русском (для русскоязычной ленты) и на английском (их TikTok часто продвигает шире)
TIKTOK_BASE = ["#кино", "#фильмы", "#чтопосмотреть", "#киноманы", "#KinoMir360"]
TIKTOK_BASE_EN = ["#movie", "#movies", "#film", "#cinema", "#fyp"]
TIKTOK_THEME = {
    "online": ["#новинкикино", "#онлайнкинотеатр", "#newmovies", "#streaming"],
    "cinema": ["#кинопрокат", "#вкино", "#boxoffice", "#nowshowing"],
    "upcoming": ["#скоровкино", "#премьеры", "#comingsoon", "#trailer"],
    "series": ["#сериалы", "#новыесериалы", "#tvseries", "#series"],
    "genre": ["#топфильмов", "#подборкафильмов", "#movierecommendations", "#top5"],
    "facts": ["#фактыокино", "#интересныефакты", "#moviefacts", "#didyouknow"],
    "rumors": ["#новостикино", "#слухи", "#скоровыйдет", "#comingsoon"],
    "news": ["#новостикино", "#киноновости", "#movienews", "#filmnews"],
    "birthday": ["#деньрождения", "#актеры", "#happybirthday", "#actor"],
    "lore": ["#историяперсонажа", "#комиксы", "#lore", "#comics"],
    "custom": ["#подборкафильмов", "#чтопосмотреть", "#movierecommendations", "#top"],
}
TIKTOK_MAX_TAGS = 15


def _hashtag(text: str) -> str | None:
    """«Мастер и Маргарита» -> #МастерИМаргарита; слишком длинное — не берём."""
    words = re.findall(r"[0-9A-Za-zА-Яа-яЁё]+", text)
    tag = "".join(w[:1].upper() + w[1:] for w in words)
    return f"#{tag}" if 2 < len(tag) <= 24 else None


def _names(result: ShortResult) -> tuple[list[str], list[str]]:
    """Названия (или имя именинника) по-русски и по-английски — для хэштегов; №1 первым."""
    key, films = result.theme.key, result.films
    if key == "news":  # здесь «фильмы» — заголовки новостей
        return [], []
    if key in ("birthday", "lore"):
        return [str(f.get("name") or "") for f in films], [str(f.get("en") or "") for f in films]
    films = films[::-1]  # в подборке №1 — последний
    ru = [_title(f) for f in films]
    en = [str(f.get("enName") or f.get("alternativeName") or "") for f in films]
    return ru, [t for t in en if t and re.search(r"[A-Za-z]", t)]


def _hashtags(result: ShortResult) -> list[str]:
    """Хэштеги ролика, самые точные первыми: тема, названия/имя (рус и англ), затем общие."""
    ru, en = _names(result)
    theme = TIKTOK_THEME.get(result.theme.key, [])
    tags = theme[:2] + [t for t in map(_hashtag, ru) if t][:2] + [t for t in map(_hashtag, en) if t][:2] \
        + theme[2:] + TIKTOK_BASE + TIKTOK_BASE_EN
    return list(dict.fromkeys(tags))


def tiktok_caption(result: ShortResult) -> str:
    """Подпись для TikTok: заголовок, вопрос для комментариев, призыв подписаться и хэштеги."""
    seo = result.seo
    title = re.sub(r"\s*#shorts\s*", " ", seo.get("title", "")).strip()
    question = next((ln.strip() for ln in seo.get("pinned", "").splitlines() if ln.strip().endswith("👇")),
                    "Пиши в комментариях 👇")
    tags = _hashtags(result)[:TIKTOK_MAX_TAGS]
    return "\n".join([title, "", question, "Подпишись, чтобы не пропустить новые ролики 🔔",
                      "Ещё больше — в нашем Telegram KinoMir360 (ссылка в профиле)", "", " ".join(tags)])


# YouTube: больше 15 хэштегов в описании — он игнорирует все, поэтому берём 14 + #shorts
YOUTUBE_MAX_TAGS = 14
EN_KEYWORDS = {
    "online": ["new movies", "streaming premieres"],
    "cinema": ["box office", "movies in theaters"],
    "upcoming": ["coming soon movies", "movie trailers"],
    "series": ["new tv series", "tv shows"],
    "genre": ["movie recommendations", "best movies"],
    "facts": ["movie facts", "behind the scenes"],
    "rumors": ["upcoming movies", "movie news"],
    "news": ["movie news", "film news"],
    "birthday": ["happy birthday", "best movies"],
    "lore": ["character origin", "comics lore"],
    "custom": ["movie recommendations", "best movies"],
}


def add_english_seo(result: ShortResult) -> None:
    """YouTube: хэштеги на русском и английском в конце описания и английские ключевые слова в тегах."""
    seo = result.seo
    hashtags = ["#shorts"] + [t for t in _hashtags(result) if t != "#fyp"][:YOUTUBE_MAX_TAGS]
    lines = seo.get("description", "").rstrip().splitlines()
    if lines and lines[-1].startswith("#"):
        lines = lines[:-1]  # прежняя строка хэштегов (только русские) — заменяем
    seo["description"] = "\n".join(lines + [" ".join(hashtags)])
    _, en = _names(result)
    words = [w.strip() for w in seo.get("tags", "").split(",") if w.strip()]
    for w in [e.lower() for e in en[:3]] + EN_KEYWORDS.get(result.theme.key, []) + ["movies", "film", "shorts"]:
        if w and w not in words and len(", ".join(words + [w])) <= 480:
            words.append(w)
    seo["tags"] = ", ".join(words)


# ---------------------------------------------------------------- сборка

async def _render(work: Path, cover_path: Path, cards: list[dict[str, Any]], lines: list[str],
                  tag: str, header: str, question: str, music: Path | None,
                  boom_slide: int | None = None) -> bool:
    """Голос + рендер видео work/short.mp4; возвращает, удалось ли озвучить.

    Карточки с "karaoke": True получают время слов для подсветки текста.
    Звуки: «вжух» на каждом переходе, «бум» перед слайдом boom_slide (номер один в отсчёте).
    """
    await _progress(40, "Озвучка")
    voice, starts, voiced, spans = await synthesize(lines, work)
    # субтитры по слову; текст, который и так звучит (караоке), на карточке тогда не дублируем
    words = await word_timeline(voice, starts, spans) if voiced else []
    for i, card in enumerate(cards, 1):  # слайд 0 — обложка
        if card.pop("karaoke", False):
            if words:
                card["caption"] = ""
            else:
                card["karaoke"] = spans[i]
    sfx = [[max(st - 0.15, 0), str(ASSETS / "whoosh.wav"), -6] for st in starts[1:]]
    if boom_slide is not None and boom_slide < len(starts):
        sfx.append([max(starts[boom_slide] - 0.05, 0), str(ASSETS / "boom.wav"), -5])
    cfg = {
        "output": "short.mp4",
        "intro": cover_path.name,
        "logo": str(ASSETS / "logo.png"),
        "voice": voice.name,
        "music": str(music or ASSETS / "music.mp3"),
        "music_db": -16 if voiced else -6,
        "tail": 1.2,
        "fonts": {"head": str(ASSETS / "Oswald.ttf"), "sans": str(ASSETS / "Montserrat.ttf")},
        "tag": tag,
        "header": header,
        "cards": cards,
        "starts": starts,
        "sfx": sfx,
        "words": words,
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
    # рендер — отдельным процессом, чтобы не блокировать бота; его строки PROGRESS двигают полоску
    await _progress(50, "Монтаж видео")
    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "shorts.make_short", str(cfg_path),
                                                stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, cwd=ROOT.parent)
    errors = asyncio.create_task(proc.stderr.read())  # читаем параллельно, иначе процесс может встать
    try:
        async for line in proc.stdout:
            m = re.match(rb"PROGRESS ([\d.]+)", line)
            if m:
                await _progress(50 + round(48 * float(m.group(1))), "Монтаж видео")
        await proc.wait()
    except asyncio.CancelledError:
        proc.kill()  # сборку прервали по таймауту — не оставляем рендер жрать память
        raise
    err = await errors
    if proc.returncode:
        raise RuntimeError(f"{sys.executable} failed: {err.decode(errors='ignore')[-800:]}")
    await _progress(98, "Видео готово")
    return voiced


def _mentioned_person(text: str, item: dict[str, Any]) -> dict[str, Any] | None:
    """Актёр или режиссёр, чья фамилия есть в тексте (с учётом падежей: Хиддлстона, Нолану)."""
    for p in (item.get("persons") or [])[:25]:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        if str(p.get("enProfession") or "") not in ("actor", "director"):
            continue
        surname = str(p["name"]).split()[-1]
        if len(surname) < 4:
            continue
        stem = surname[:-1] if len(surname) >= 5 and surname[-1].lower() in "аеёиоуыэюяйь" else surname
        if re.search(rf"(?<!\w){re.escape(stem)}\w{{0,3}}(?!\w)", text, re.IGNORECASE):
            return p
    return None


FACT_CARD_H = 780  # высота картинки на слайдах фактов и новостей (как CARD_H)


async def _own_for_facts(files: list[Path], facts: list[str], work: Path, gemini_key: str,
                         strict: bool = False, prefix: str = "own") -> tuple[dict[int, Image.Image], dict[int, str]]:
    """Свои фото/видео автора к фактам: ИИ — по смыслу, остальные — по порядку; один файл — один факт.
    strict=True (материал из Telegram-каналов) — только то, что ИИ счёл подходящим, без раздачи по порядку.

    Возвращает ({факт: картинка}, {факт: файл клипа}); у видео картинка — его кадр (запасной).
    """
    items: list[tuple[Image.Image, str | None]] = []
    for n, path in enumerate(files):
        try:
            if path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                items.append((Image.open(path).convert("RGB"), None))
                continue
            dur = await _duration(path)
            clip = work / f"{prefix}{n}.mp4"
            await clips.cut_clip(path, clip, max(0.0, dur / 2 - CLIP_SECONDS / 2) if dur > CLIP_SECONDS else 0.0,
                                 min(CLIP_SECONDS, dur))
            still = work / f"{prefix}{n}.jpg"
            await _run("ffmpeg", "-v", "error", "-y", "-ss", "1", "-i", str(clip), "-frames:v", "1", str(still))
            items.append((Image.open(still).convert("RGB"), clip.name))
        except Exception as exc:  # noqa: BLE001 — битый файл пропускаем
            log.warning("shorts: own material %s skipped: %s", path.name, exc)
    if not items:
        return {}, {}
    choices = await ai.match_images(gemini_key, facts, [im for im, _ in items]) if gemini_key else None
    used: set[int] = set()
    at: dict[int, int] = {}
    for i, c in enumerate(choices or []):
        if c >= 0 and c not in used:
            at[i] = c
            used.add(c)
    free = [] if strict else [i for i in range(len(facts)) if i not in at]
    for c in range(len(items)):
        if c not in used and free:
            at[free.pop(0)] = c
            used.add(c)
    own: dict[int, Image.Image] = {}
    videos: dict[int, str] = {}
    for i, c in at.items():
        img, clip = items[c]
        own[i] = img if img.width >= img.height * 1.2 else _portrait(img)  # вертикальное — целиком на фоне
        if clip:
            videos[i] = clip
    return own, videos


def _portrait(img: Image.Image, w: int = 1080, h: int = FACT_CARD_H) -> Image.Image:
    """Портрет целиком по центру на размытом фоне из него же (иначе обрезка срежет лицо)."""
    from PIL import ImageFilter
    s = max(w / img.width, h / img.height)
    bg = img.resize((round(img.width * s), round(img.height * s)))
    bg = bg.crop(((bg.width - w) // 2, (bg.height - h) // 2, (bg.width - w) // 2 + w, (bg.height - h) // 2 + h))
    bg = Image.blend(bg.filter(ImageFilter.GaussianBlur(30)), Image.new("RGB", (w, h)), 0.45)
    fg = img.copy()
    fg.thumbnail((w, h - 40))
    bg.paste(fg, ((w - fg.width) // 2, (h - fg.height) // 2 + 20))
    return bg


async def make_facts_short(fetch: Fetch, work: Path, query: str = "", exclude: set[int] | None = None,
                           music: Path | None = None, tmdb_key: str = "",
                           gemini_key: str = "", trailer_clips: bool = True,
                           custom_facts: list[str] | None = None,
                           user_media: list[Path] | None = None, rumors: bool = False,
                           tg_media: list[dict[str, Any]] | None = None) -> ShortResult:
    """custom_facts — факты автора (кнопка «Факты: свой текст»): фильм ищем по query, факты не меняем.
    user_media — свои фото и видео автора: идут к фактам первыми.
    rumors=True — фильм ещё не вышел: ролик «Что известно о …» из новостей и слухов.
    tg_media — посты Telegram-каналов с фото и видео о фильме: ИИ ставит их к фактам, к которым они подходят."""
    await _progress(5, "Ищу фильм и факты")
    if custom_facts:
        film = await find_film(fetch, query, upcoming=rumors)
        if not film:
            raise RuntimeError(f"Кинопоиск не нашёл «{query}»")
        facts, web_note = list(custom_facts), ""
    else:
        film, kp_facts = await pick_fact_film(fetch, query, exclude or set(), web=ai.available(gemini_key))
        # ещё не вышел — вместо фактов «Что известно о …» из новостей и слухов (если есть ИИ)
        rumors = rumors or (ai.available(gemini_key) and is_upcoming(film))
        tg_media = []
        facts, web_note = await gather_facts(film, kp_facts, gemini_key, media_out=tg_media, rumors=rumors)
    await _progress(10, f"«{_title(film)}»: качаю кадры")
    # награды из Викиданных — первым фактом (в фактах автора — только его текст)
    award_fact = None if custom_facts else wiki.awards_fact(_title(film), await wiki.awards(film))
    if award_fact:
        facts = [award_fact] + facts[: FACTS - 1]
    t, n, kind = _title(film), len(facts), _kind(film)
    if rumors:
        label, card_label, hook = "СЛУХИ", "НОВОСТЬ", f"Что известно о {kind} «{t}»"
        intro, cover_text = f"{hook}? Главные новости и слухи!", f"{hook}: новости и слухи"
        outro_line, outro = "Ждёшь премьеру? Пиши в комментариях!", "ЖДЁШЬ\nПРЕМЬЕРУ?\nПИШИ В КОММЕНТАРИЯХ"
    else:
        label, card_label, hook = "ФАКТЫ", "ФАКТ", f"{n} {_facts_word(n)} о {kind} «{t}»"
        intro, cover_text = f"{hook}, которые ты точно не знал!", f"{hook}, которые ты не знал"
        outro_line, outro = "Какой факт удивил? Пиши в комментариях!", "КАКОЙ ФАКТ\nУДИВИЛ БОЛЬШЕ?\nПИШИ В КОММЕНТАРИЯХ"
    theme = Theme("rumors" if rumors else "facts", label, t.upper(), hook, hook, [])

    cards = []
    # пул кадров: без надписей с TMDB, затем кадры/фон/постер с Кинопоиска
    backdrops, _ = await tmdb.images(tmdb_key, film, limit=12)
    urls = tmdb.pick_varied(backdrops, 12) + await _stills(fetch, film)
    photos = await tmdb.people(tmdb_key, film)
    pool_size = 12 if ai.available(gemini_key) else n
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # у свежих фильмов на Кинопоиске может не быть постера — тогда кадр TMDB
        cover_src = await _download_first(session, [_poster_url(film)] + backdrops[:2])
        images: list[Image.Image] = []
        for url in urls:
            if len(images) >= pool_size:
                break
            try:
                images.append(await _download(session, url))
            except Exception:  # noqa: BLE001
                continue
        # свои фото и видео автора — в первую очередь, к фактам, о которых они
        own: dict[int, Image.Image] = {}
        own_videos: dict[int, str] = {}
        if user_media:
            await _progress(14, "Разбираю ваши фото и видео")
            own, own_videos = await _own_for_facts(user_media, facts, work, gemini_key)
        # фото и видео из постов Telegram-каналов — к фактам, которым они подходят по смыслу
        rest_tg = [i for i in range(len(facts)) if i not in own]
        if tg_media and rest_tg and ai.available(gemini_key):
            await _progress(16, "Фото и видео из Telegram-каналов")
            try:
                files = await tgsearch.download_media(tg_media, work)
                tg_own, tg_videos = await _own_for_facts(files, [facts[i] for i in rest_tg], work, gemini_key,
                                                         strict=True, prefix="tgc")
                own.update({rest_tg[k]: img for k, img in tg_own.items()})
                own_videos.update({rest_tg[k]: clip for k, clip in tg_videos.items()})
            except Exception as exc:  # noqa: BLE001 — без материала из каналов ролик всё равно соберём
                log.warning("shorts: tg media for facts failed: %s", exc)
        # факт про актёра/режиссёра — его фото (TMDB, запасной — Кинопоиск)
        portraits: dict[int, Image.Image] = {}
        for i, fact in enumerate(facts):
            if i in own:
                continue
            person = _mentioned_person(fact, film)
            if not person:
                continue
            urls_p = [photos.get(str(person.get("enName") or "").lower()), person.get("photo")]
            try:
                portraits[i] = _portrait(await _download_first(session, urls_p))
            except Exception:  # noqa: BLE001
                continue
    rest = [i for i in range(n) if i not in portraits and i not in own]
    if not images and rest:
        raise RuntimeError(f"Не скачались картинки для «{t}»")

    ai.last_error = None
    # факты без фото — сначала момент из трейлера, который Gemini счёл подходящим
    videos: dict[int, str] = dict(own_videos)
    video_note = ""
    if trailer_clips and rest:
        await _progress(18, "Ищу моменты в трейлере")
        if ai.available(gemini_key):
            found, video_note = await _fact_clips(film, [(i, facts[i]) for i in rest], work, gemini_key)
            videos.update(found)
        else:
            video_note = "нет ключа GEMINI_API_KEY или RESERVE_AI_KEY — без ИИ моменты не подобрать"
    # остальные — кадр, который Gemini выбрал по смыслу; без ИИ — по очереди
    stills = [i for i in rest if i not in videos]
    await _progress(32, "ИИ подбирает кадры")
    choices = await ai.match_images(gemini_key, [facts[i] for i in stills], images) if stills else None
    used: set[int] = set()
    picked: dict[int, Image.Image] = {**portraits, **own}
    for k, i in enumerate(stills + [v for v in videos if v not in own]):  # у фактов с видео кадр — запасной
        c = choices[k] if choices and k < len(stills) else -1
        if c < 0 or c in used:
            c = next((j for j in range(len(images)) if j not in used), k % len(images))
        used.add(c)
        picked[i] = images[c]

    for i, fact in enumerate(facts, 1):
        # название уже в заголовке слайда — на карточке не повторяем
        card = await asyncio.to_thread(make_card, picked[i - 1], "", i, n,
                                       None, "IMDb", None, f"{card_label} №{i}", FACT_CARD_H)
        path = work / f"card{i}.jpg"
        card.save(path, quality=93)
        video: dict[str, str] = {}
        if (i - 1) in videos:
            card_overlay("", i, n, None, "IMDb", None, f"{card_label} №{i}", FACT_CARD_H).save(work / f"overlay{i}.png")
            video = {"clip": videos[i - 1], "overlay": f"overlay{i}.png"}
        cards.append({"image": path.name, "caption": fact, "karaoke": True, **video})

    cover = await asyncio.to_thread(make_cover, cover_src, label, cover_text)
    cover_path = work / "cover.jpg"
    cover.save(cover_path, quality=93)

    lines = [intro] + facts + [f"{outro_line} {SAVE_AND_SUBSCRIBE}"]
    voiced = await _render(work, cover_path, cards, lines, label, t.upper(), outro, music)
    seo = build_rumors_seo(film, facts) if rumors else build_facts_seo(film, facts)
    result = ShortResult(work / "short.mp4", cover_path, theme, [film], seo)
    notes = [web_note] if web_note else []
    if video_note:
        notes.append(f"🎞 Видео из трейлера не вставлено: {video_note}.")
    if ai.last_error:
        notes.append(f"⚠️ ИИ не помог подобрать кадры: {ai.last_error}")
    if not voiced or tts_note:
        notes.append(no_voice_note(voiced))
    if notes:
        result.seo["warning"] = "\n".join(notes)
    return result


# ---------------------------------------------------------------- своя подборка (тема и список от автора)

CUSTOM_MIN, CUSTOM_MAX = 3, 10


UPCOMING_STATUS = ("announced", "pre-production", "filming", "post-production")


def is_upcoming(item: dict[str, Any]) -> bool:
    """Ещё не вышел: на Кинопоиске «в производстве», год впереди или премьера позже сегодняшнего дня."""
    if str(item.get("status") or "") in UPCOMING_STATUS:
        return True
    today = date.today()
    if int(item.get("year") or 0) > today.year:
        return True
    dates = [d for d in (_pdate(item, k) for k in ("world", "russia", "digital")) if d]
    return bool(dates) and min(dates) > today


async def find_film(fetch: Fetch, query: str, upcoming: bool = False) -> dict[str, Any] | None:
    """Фильм по «Название» или «Название 2018»: самый известный из найденных (полная карточка).
    upcoming=True — сначала те, что выходят в этом году или позже (новый сериал, а не старый фильм).
    Слово «сериал» или «фильм» в запросе — уточнение: ищем без него, но такой тип — первым."""
    query = query.strip()
    kind_m = re.search(r"(?<!\w)(мультсериал|сериал|мультфильм|фильм)(?!\w)", query, re.I)
    want_series = bool(kind_m) and "сериал" in kind_m.group(1).lower()
    if kind_m:
        query = re.sub(r"\s+", " ", query[: kind_m.start()] + query[kind_m.end():]).strip(" ,.-—")
    if query.startswith("id:") and query[3:].isdigit():
        return await fetch(f"/movie/{int(query[3:])}")
    year_m = re.search(r"[\s(]+(19\d\d|20\d\d)\)?$", query)
    year = int(year_m.group(1)) if year_m else None
    name = query[: year_m.start()].strip() if year_m else query
    data = await fetch("/movie/search", query=name, page=1, limit=10)
    cands = [x for x in data.get("docs") or [] if isinstance(x, dict) and x.get("id")]
    if year:
        cands = [c for c in cands if c.get("year") in (year - 1, year, year + 1)] or cands
    if not cands:
        return None
    this_year = date.today().year

    def series(c: dict[str, Any]) -> bool:
        return bool(c.get("isSeries")) or "series" in str(c.get("type") or "")

    cands.sort(key=lambda c: (c.get("year") != year if year else False,
                              bool(kind_m) and series(c) != want_series,
                              upcoming and int(c.get("year") or 0) < this_year,
                              -int((c.get("votes") or {}).get("kp") or 0)))
    return await fetch(f"/movie/{cands[0]['id']}")


def custom_theme(title: str, n: int) -> Theme:
    title = re.sub(r"\s+", " ", title).strip(" .!") or "Подборка фильмов"
    header = title.upper() if len(title) <= 34 else title.upper()[:33].rsplit(" ", 1)[0] + "…"
    return Theme("custom", "ПОДБОРКА", header, title, f"{title} 🎬",
                 [title.lower(), "подборка фильмов", "что посмотреть", "топ фильмов", "фильмы на вечер"])


async def make_custom_short(fetch: Fetch, work: Path, title: str, film_ids: list[int], music: Path | None = None,
                            tmdb_key: str = "", trailer_clips: bool = True) -> ShortResult:
    """Своя подборка: тема и фильмы — от автора (или из чужого ролика), порядок — как в списке."""
    work.mkdir(parents=True, exist_ok=True)
    if not CUSTOM_MIN <= len(film_ids) <= CUSTOM_MAX:
        raise RuntimeError(f"в подборке должно быть от {CUSTOM_MIN} до {CUSTOM_MAX} фильмов")
    await _progress(5, "Загружаю фильмы")
    films = [await fetch(f"/movie/{i}") for i in film_ids]
    result = await _list_short(work, custom_theme(title, len(films)), films, music, tmdb_key, trailer_clips)
    add_english_seo(result)
    return result


# ---------------------------------------------------------------- день рождения

BIRTHDAY_FILMS = 5


def _years(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "год"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "года"
    return "лет"


def person_uid(imdb: str) -> int:
    """Отрицательный id для истории роликов — не пересекается с фильмами Кинопоиска и новостями."""
    return -(10**12 + int(re.sub(r"\D", "", imdb) or 0))


async def pick_birthday(today: date, exclude: set[int], tmdb_key: str) -> tuple[dict[str, Any], dict[str, Any],
                                                                             list[dict[str, Any]]]:
    """(человек из Викиданных, профиль TMDB, лучшие фильмы) — самый известный именинник с фильмами."""
    people = await wiki.birthdays(today.month, today.day)
    if not people:
        raise RuntimeError("Сегодня в Викиданных нет известных именинников из кино")
    for who in people:
        if person_uid(who["imdb"]) in exclude:
            continue
        try:
            info = await tmdb.person(tmdb_key, who["imdb"])
        except Exception as exc:  # noqa: BLE001
            log.info("shorts: TMDB person %s failed: %s", who["imdb"], exc)
            continue
        if not info:
            continue
        films = tmdb.best_movies(info, BIRTHDAY_FILMS)
        if len(films) >= 3:
            return who, info, films
    raise RuntimeError("Не нашлось именинника с фильмами на TMDB (нужен TMDB_API_KEY)")


def build_birthday_seo(name: str, en: str, age: int, films: list[dict[str, Any]], female: bool) -> dict[str, str]:
    title = f"{name} — {age} {_years(age)}! Лучшие фильмы 🎂 #shorts"
    if len(title) > 100:
        title = f"{name}: лучшие фильмы 🎂 #shorts"[:100]
    names = [str(f.get("title") or f.get("original_title")) for f in films]
    description = "\n".join([
        f"🎂 Сегодня день рождения: {name}" + (f" ({en})" if en and en != name else "")
        + f" — {age} {_years(age)}!", "",
        "Лучшие фильмы:",
        *[f"{len(films) - i}. {t}" + (f" ({str(f.get('release_date'))[:4]})" if f.get("release_date") else "")
          for i, (t, f) in enumerate(zip(names, films))], "",
        "А какой фильм твой любимый? Пиши в комментариях 👇", "",
        f"📺 YouTube: {YOUTUBE_URL}",
        f"✈️ Telegram: {TELEGRAM_URL} — новинки, трейлеры и подборки каждый день", "",
        "#деньрождения #кино #KinoMir360",
    ])
    tags: list[str] = []
    for tag in [name.lower(), en.lower(), f"{name.lower()} фильмы", f"лучшие фильмы {name.lower()}",
                f"{name.lower()} день рождения", "день рождения звезды", "лучшие фильмы"] + \
            [t.lower() for t in names] + ["KinoMir360", "киномир360", "шортс кино"]:
        if tag and tag not in tags and len(", ".join(tags + [tag])) <= 480:
            tags.append(tag)
    pinned = (f"А какой фильм с {'ней' if female else 'ним'} твой любимый? 👇\n"
              f"Ещё больше подборок — в нашем Telegram 👉 {TELEGRAM_URL}")
    return {"title": title, "description": description, "tags": ", ".join(tags), "pinned": pinned}


def _latin(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", text))


async def _kp_role(fetch: Fetch | None, film: dict[str, Any], who: dict[str, Any]) -> str:
    """Роль по-русски с Кинопоиска (на TMDB роли по-английски, и голос читает их ужасно)."""
    if fetch is None:
        return ""
    title = str(film.get("title") or film.get("original_title") or "")
    year = int(str(film.get("release_date") or "0")[:4] or 0)
    try:
        data = await fetch("/movie/search", query=title, page=1, limit=5)
        doc = next((d for d in data.get("docs") or []
                    if d.get("id") and (not year or abs(int(d.get("year") or 0) - year) <= 1)), None)
        if not doc:
            return ""
        full = await fetch(f"/movie/{doc['id']}")
    except Exception as exc:  # noqa: BLE001 — без роли карточка всё равно собирается
        log.info("shorts: KP role for %s failed: %s", title, exc)
        return ""
    names = {who["name"].lower(), who["en"].lower()} - {""}
    for p in full.get("persons") or []:
        if str(p.get("name") or "").lower() in names or str(p.get("enName") or "").lower() in names:
            role = str(p.get("description") or "").strip()
            if role and not _latin(role):
                return role
    return ""


async def make_birthday_short(work: Path, today: date, exclude: set[int] | None = None,
                              music: Path | None = None, tmdb_key: str = "",
                              trailer_clips: bool = True, fetch: Fetch | None = None) -> ShortResult:
    await _progress(5, "Ищу именинника")
    who, info, top = await pick_birthday(today, exclude or set(), tmdb_key)
    await _progress(12, f"{who['name']}: качаю кадры и трейлеры")
    name, female = who["name"], who["female"]
    born = date.fromisoformat(who["born"])
    age = today.year - born.year
    he = "ей" if female else "ему"
    # обратный отсчёт: самый известный фильм — последним
    films = top[::-1]
    n = len(films)

    cards = []
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        photo = await _download_first(session, tmdb.profile_urls(info))
        for i, f in enumerate(films, 1):
            title = str(f.get("title") or f.get("original_title") or "")
            await _progress(12 + 25 * (i - 1) // n, f"Фильм {i} из {n}: {title}")
            year = str(f.get("release_date") or "")[:4]
            img = await _download_first(session, [tmdb.still_url(f)])
            rating = float(f.get("vote_average") or 0)
            rating_shown = rating if rating >= MIN_RATING else None
            card = await asyncio.to_thread(make_card, img, title, i, n, rating_shown, "TMDB", None, None,
                                           place=n - i + 1)
            path = work / f"card{i}.jpg"
            card.save(path, quality=93)
            video: dict[str, str] = {}
            item = {"name": title, "alternativeName": f.get("original_title"), "year": int(year) if year else None}
            if trailer_clips and await _trailer_clip(item, work / f"clip{i}.mp4"):
                card_overlay(title, i, n, rating_shown, "TMDB", None, None, place=n - i + 1).save(work / f"overlay{i}.png")
                video = {"clip": f"clip{i}.mp4", "overlay": f"overlay{i}.png"}
            # роль по-русски с Кинопоиска; английскую с TMDB не показываем и не озвучиваем
            role = await _kp_role(fetch, f, who) if f.get("job") != "Director" else ""
            if not role:
                tmdb_role = str(f.get("character") or "").strip()
                role = "" if _latin(tmdb_role) else tmdb_role
            f["_role"] = role
            rows = [[("Роль", role)]] if role else [[("Режиссёр", name)]] if f.get("job") == "Director" else []
            cards.append({"image": path.name, "caption": " • ".join(x for x in [name, year] if x),
                          "info": rows, **video})

    portrait = _portrait(photo, 1080, 1350)
    cover = await asyncio.to_thread(make_cover, portrait, "ДЕНЬ РОЖДЕНИЯ", f"{name}\n{age} {_years(age)}!")
    cover_path = work / "cover.jpg"
    cover.save(cover_path, quality=93)

    def say(f: dict[str, Any], place: int) -> str:
        title = f"«{f.get('title') or f.get('original_title')}»"
        head = f"И номер один — {title}!" if place == 1 else f"Номер {NUMBERS.get(place, place)} — {title}."
        role = f.get("_role") or ""
        return f"{head} Роль — {role}." if role and len(role) < 40 else head

    lines = ([f"Сегодня день рождения празднует {name}! {he.capitalize()} {age} {_years(age)}. Лучшие фильмы:"]
             + [say(f, n - i) for i, f in enumerate(films)]
             + [f"А какой фильм с {'ней' if female else 'ним'} твой любимый? Пиши в комментариях! {SAVE_AND_SUBSCRIBE}"])
    header = name.upper()
    voiced = await _render(work, cover_path, cards, lines, "ДЕНЬ РОЖДЕНИЯ", header,
                           "КАКОЙ ФИЛЬМ\nТВОЙ ЛЮБИМЫЙ?\nПИШИ В КОММЕНТАРИЯХ", music, boom_slide=n)

    hook = f"{name} — {age} {_years(age)}"
    theme = Theme("birthday", "ДЕНЬ РОЖДЕНИЯ", header, hook, hook, [])
    seo = build_birthday_seo(name, who["en"], age, films, female)
    result = ShortResult(work / "short.mp4", cover_path, theme,
                         [{"id": person_uid(who["imdb"]), "name": name, "en": who["en"]}], seo)
    if not voiced or tts_note:
        result.seo["warning"] = no_voice_note(voiced)
    return result


async def make_daily_short(fetch: Fetch, work: Path, theme_key: str | None = None,
                           exclude: set[int] | None = None, music: Path | None = None,
                           today: date | None = None, tmdb_key: str = "",
                           news_feeds: list[str] | None = None, manual: bool = False,
                           gemini_key: str = "", trailer_clips: bool = True) -> ShortResult:
    """fetch(path, **params) -> dict — запрос к API Кинопоиска (PoiskKino._get)."""
    result = await _make_short(fetch, work, theme_key, exclude, music, today, tmdb_key, news_feeds, manual,
                               gemini_key, trailer_clips)
    add_english_seo(result)
    return result


async def _make_short(fetch: Fetch, work: Path, theme_key: str | None = None,
                      exclude: set[int] | None = None, music: Path | None = None,
                      today: date | None = None, tmdb_key: str = "",
                      news_feeds: list[str] | None = None, manual: bool = False,
                      gemini_key: str = "", trailer_clips: bool = True) -> ShortResult:
    today = today or date.today()
    work.mkdir(parents=True, exist_ok=True)
    theme_key = theme_key or SCHEDULE[today.weekday()]
    if theme_key == "birthday":
        try:
            return await make_birthday_short(work, today, exclude, music, tmdb_key, trailer_clips, fetch)
        except Exception:
            if manual:
                raise
            log.exception("shorts: birthday failed, falling back to genre")
            theme_key = "genre"
    if theme_key == "news":
        try:
            return await make_news_short(fetch, work, news_feeds or news.DEFAULT_FEEDS, exclude, music,
                                         tmdb_key, today, gemini_key)
        except RuntimeError:
            if manual:
                raise
            log.exception("shorts: news failed, falling back to genre")
            theme_key = "genre"
    if theme_key.startswith("facts"):
        query = theme_key.partition(":")[2].strip()
        try:
            return await make_facts_short(fetch, work, query, exclude, music, tmdb_key, gemini_key,
                                          trailer_clips)
        except RuntimeError:
            if query:
                raise
            log.exception("shorts: facts failed, falling back to genre")
            theme_key = "genre"
    theme = theme_for(theme_key, today)
    # в текстах зашито «5 ...», поэтому фильмов должно быть ровно FILMS;
    # если тема их не набрала — берём жанровую подборку
    await _progress(5, "Подбираю фильмы")
    films = await pick_films(fetch, theme, today, exclude or set())
    for _ in range(3):
        if len(films) >= FILMS:
            break
        log.info("shorts: theme %s gave %d films, falling back to genre", theme.key, len(films))
        theme = theme_for("genre", today)
        films = await pick_films(fetch, theme, today, exclude or set())
    if len(films) < FILMS:
        raise RuntimeError(f"Не набралось {FILMS} фильмов для подборки (тема {theme.key})")
    # обратный отсчёт: показываем с №5 до №1, лучший — последним (ожидаемые — по порядку ожидания)
    ranked = films if theme.upcoming else sorted(films, key=_rank_key, reverse=True)
    return await _list_short(work, theme, ranked[::-1], music, tmdb_key, trailer_clips)


async def _list_short(work: Path, theme: Theme, films: list[dict[str, Any]], music: Path | None,
                      tmdb_key: str, trailer_clips: bool) -> ShortResult:
    """Подборка-отсчёт по готовому списку: films в порядке показа (последний — №1)."""
    n = len(films)
    cards = []
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        posters_tmdb: list[str] = []
        for i, f in enumerate(films, 1):
            await _progress(10 + 28 * (i - 1) // n, f"Фильм {i} из {n}: {_title(f)}")
            # кадр без надписей с TMDB, если нет — фон/постер Кинопоиска
            backdrops, posters = await tmdb.images(tmdb_key, f, limit=1)
            if i == n:
                posters_tmdb = posters
            img = await _download_first(session, backdrops + [_image_url(f)])
            rating, src = _rating(f)
            badge = None if rating else "НОВИНКА"
            if theme.upcoming:
                prem = _premiere(f)
                rating, badge = None, (f"С {_date_ru(prem).upper()}" if prem else "СКОРО")
            card = await asyncio.to_thread(make_card, img, _title(f), i, n, rating, src or "IMDb",
                                           _actors(f), badge, place=n - i + 1)
            path = work / f"card{i}.jpg"
            card.save(path, quality=93)
            video: dict[str, str] = {}
            if trailer_clips and await _trailer_clip(f, work / f"clip{i}.mp4"):
                overlay = card_overlay(_title(f), i, n, rating, src or "IMDb", _actors(f), badge, place=n - i + 1)
                overlay.save(work / f"overlay{i}.png")
                video = {"clip": f"clip{i}.mp4", "overlay": f"overlay{i}.png"}
            caption = " • ".join(x for x in [", ".join(_genres(f)[:2]).capitalize(),
                                             str(f.get("year") or "")] if x)
            rows = info_rows(f)
            f["_awards"] = wiki.awards_line(await wiki.awards(f))
            if f["_awards"]:
                rows.append([("Награды", f["_awards"])])
            cards.append({"image": path.name, "caption": caption, "info": rows, **video})
        # обложка — фильм №1: русский постер Кинопоиска, запасной — постер TMDB
        cover_src = await _download_first(session, [_poster_url(films[-1])] + posters_tmdb)

    headline = theme.hook.replace(", ", ",\n", 1)
    cover = await asyncio.to_thread(make_cover, cover_src, theme.tag, headline)
    cover_path = work / "cover.jpg"
    cover.save(cover_path, quality=93)

    voiced = await _render(work, cover_path, cards, voice_lines(theme, films), theme.tag, theme.header,
                           "КАКОЙ ПОСМОТРИШЬ\nПЕРВЫМ?\nПИШИ В КОММЕНТАРИЯХ", music, boom_slide=n)

    result = ShortResult(work / "short.mp4", cover_path, theme, films, build_seo(theme, films))
    if not voiced or tts_note:
        result.seo["warning"] = no_voice_note(voiced)
    return result

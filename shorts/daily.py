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

from . import ai, clips, news, pronounce, tmdb, wiki
from .cards import card_overlay, make_card, make_cover

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


# ---------------------------------------------------------------- фрагменты трейлеров

CLIP_SECONDS = 5.0


async def _trailer_clip(item: dict[str, Any], out: Path) -> bool:
    """Скачать 5 секунд трейлера с Rutube в out; False — не нашлось или не скачалось."""
    titles = [str(item.get(k) or "") for k in ("name", "alternativeName", "enName")]
    try:
        found = await clips.rutube_trailer([t for t in titles if t], item.get("year"))
        if not found:
            log.info("shorts: no Rutube trailer for %s", _title(item))
            return False
        await clips.download_clip(found["url"], out, clips.clip_start(found["duration"], CLIP_SECONDS),
                                  CLIP_SECONDS, timeout=90)
        return out.exists()
    except Exception as exc:  # noqa: BLE001 — без трейлера останется кадр
        log.warning("shorts: trailer clip for %s failed: %s", _title(item), exc)
        return False


async def _fact_clips(item: dict[str, Any], facts_by_idx: list[tuple[int, str]], work: Path,
                     gemini_key: str) -> dict[int, str]:
    """Для фактов — фрагменты трейлера, где видно то, о чём факт: {номер факта: файл клипа}.

    Трейлер с Rutube качается целиком, режется на кадры, Gemini выбирает кадр под каждый факт
    (или «ничего не подходит»), вокруг выбранного момента вырезается CLIP_SECONDS.
    """
    titles = [str(item.get(k) or "") for k in ("name", "alternativeName", "enName")]
    trailer_dir = work / "trailer"
    try:
        found = await clips.rutube_trailer([t for t in titles if t], item.get("year"))
        if not found:
            log.info("shorts: no Rutube trailer for facts about %s", _title(item))
            return {}
        trailer_dir.mkdir(exist_ok=True)
        src = await clips.download_full(found["url"], trailer_dir)
        frames = await clips.sample_frames(src, trailer_dir, duration=found["duration"])
        if not frames:
            return {}
        thumbs = [Image.open(path).convert("RGB") for _, path in frames]
        choices = await ai.match_images(gemini_key, [text for _, text in facts_by_idx], thumbs)
        if not choices:
            return {}
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
        return out
    except Exception as exc:  # noqa: BLE001 — без трейлера будут фото и кадры
        log.warning("shorts: trailer clips for facts about %s failed: %s", _title(item), exc)
        return {}
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


async def make_news_short(fetch: Fetch, work: Path, feeds: list[str], exclude: set[int] | None = None,
                          music: Path | None = None, tmdb_key: str = "",
                          today: date | None = None, gemini_key: str = "") -> ShortResult:
    today = today or date.today()
    items = await news.latest_news(feeds, exclude or set())
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
        if len(with_img) >= NEWS:
            break
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
        card = await asyncio.to_thread(make_card, img, "", i, n, None, "IMDb", None, f"НОВОСТЬ №{i}", 780)
        path = work / f"card{i}.jpg"
        card.save(path, quality=93)
        cards.append({"image": path.name, "caption": item.title, "info": [[("Источник", item.source)]],
                      "karaoke": True})

    day = _date_ru(today)
    cover = await asyncio.to_thread(make_cover, images[0], "НОВОСТИ", f"Главные новости кино\n{day}")
    cover_path = work / "cover.jpg"
    cover.save(cover_path, quality=93)

    def say(item: news.NewsItem) -> str:
        title = item.title if item.title[-1] in ".!?…" else item.title + "."
        return f"{title} {item.lead}".strip()

    # Gemini пересказывает своими словами; без ИИ — заголовок и лид как есть
    ai.last_error = None
    retold = await ai.retell_news(gemini_key, [(it.title, it.lead, it.source) for it, _ in chosen])
    spoken = retold or [say(it) for it, _ in chosen]
    lines = ([f"Главные новости кино на {day}!"] + spoken
             + [f"Какая новость удивила больше всего? Пиши в комментариях! {SAVE_AND_SUBSCRIBE}"])
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
    if gemini_key and ai.last_error:
        result.seo["warning"] = f"⚠️ Gemini не пересказал новости: {ai.last_error}"
    if not voiced:
        result.seo["warning"] = "⚠️ Голос не синтезировался — ролик собран только с музыкой."
    return result


# ---------------------------------------------------------------- голос

NUMBERS = {1: "один", 2: "два", 3: "три", 4: "четыре", 5: "пять", 6: "шесть", 7: "семь"}
SAVE_AND_SUBSCRIBE = "Сохрани, чтобы не потерять, и подписывайся на КиноМир триста шестьдесят!"


def _say_film(item: dict[str, Any], theme: Theme, place: int) -> str:
    """Обратный отсчёт: «Номер пять — «X». Комедия.» … «И номер один — «X»!»."""
    title = f"«{_title(item)}»"
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
    out, err = await proc.communicate()
    if proc.returncode:
        raise RuntimeError(f"{cmd[0]} failed: {err.decode(errors='ignore')[-800:]}")
    return out.decode(errors="ignore")


async def _duration(path: Path) -> float:
    out = await _run("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path))
    return float(out.strip())


TTS_TIMEOUT = 40  # сек на фразу: без лимита запрос к сервису голоса может висеть бесконечно


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


async def say_to_file(text: str, path: Path) -> list[float]:
    """Озвучить фразу в mp3 (время слов в ответе). Не принял знак ударения — повторяем без него."""
    try:
        return await asyncio.wait_for(_tts(text, path), timeout=TTS_TIMEOUT)
    except Exception:
        if pronounce.STRESS not in text:
            raise
        log.warning("shorts: TTS failed with stress marks, retrying without them")
        return await asyncio.wait_for(_tts(text.replace(pronounce.STRESS, ""), path), timeout=TTS_TIMEOUT)


async def synthesize(lines: list[str], work: Path) -> tuple[Path, list[float], bool, list[dict[str, Any]]]:
    """Озвучка по фразам: файл, начало каждой фразы, есть ли голос, и по фразе {длительность, время слов}."""
    parts: list[Path] = []
    words: list[list[float]] = []
    voiced = True
    try:
        for i, text in enumerate(lines):
            part = work / f"voice_{i}.mp3"
            words.append(await say_to_file(pronounce.apply(text), part))
            parts.append(part)
    except Exception as exc:  # noqa: BLE001 — без голоса ролик всё равно собираем
        log.warning("shorts: TTS failed, using silence: %s", exc)
        voiced = False
        parts, words = [], [[] for _ in lines]
        for i in range(len(lines)):
            part = work / f"voice_{i}.mp3"
            sec = 3.5 if 0 < i < len(lines) - 1 else 4.5
            await _run("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                       "-t", str(sec), str(part))
            parts.append(part)

    starts, spans, t = [], [], 0.0
    inputs, chains = [], []
    for i, part in enumerate(parts):
        starts.append(round(t, 3))
        dur = await _duration(part)
        spans.append({"speech": round(dur, 3), "words": [round(w, 3) for w in words[i]]})
        t += dur + GAP
        inputs += ["-i", str(part)]
        chains.append(f"[{i}:a]aresample=48000,apad=pad_dur={GAP}[a{i}]")
    filt = ";".join(chains) + ";" + "".join(f"[a{i}]" for i in range(len(parts))) + \
        f"concat=n={len(parts)}:v=0:a=1[out]"
    voice = work / "voice.wav"
    await _run("ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", filt, "-map", "[out]", str(voice))
    return voice, starts, voiced, spans


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
                  tag: str, header: str, question: str, music: Path | None,
                  boom_slide: int | None = None) -> bool:
    """Голос + рендер видео work/short.mp4; возвращает, удалось ли озвучить.

    Карточки с "karaoke": True получают время слов для подсветки текста.
    Звуки: «вжух» на каждом переходе, «бум» перед слайдом boom_slide (номер один в отсчёте).
    """
    voice, starts, voiced, spans = await synthesize(lines, work)
    for i, card in enumerate(cards, 1):  # слайд 0 — обложка
        if card.pop("karaoke", False):
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
        "music_db": -14 if voiced else -6,
        "tail": 1.2,
        "fonts": {"head": str(ASSETS / "Oswald.ttf"), "sans": str(ASSETS / "Montserrat.ttf")},
        "tag": tag,
        "header": header,
        "cards": cards,
        "starts": starts,
        "sfx": sfx,
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


def _portrait(img: Image.Image, w: int = 1080, h: int = 780) -> Image.Image:
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
                           gemini_key: str = "", trailer_clips: bool = True) -> ShortResult:
    film, facts = await pick_fact_film(fetch, query, exclude or set())
    # награды из Викиданных — первым фактом
    award_fact = wiki.awards_fact(_title(film), await wiki.awards(film))
    if award_fact:
        facts = [award_fact] + facts[: FACTS - 1]
    t, n, kind = _title(film), len(facts), _kind(film)
    hook = f"{n} {_facts_word(n)} о {kind} «{t}»"
    theme = Theme("facts", "ФАКТЫ", t.upper(), hook, hook, [])

    cards = []
    # пул кадров: без надписей с TMDB, затем кадры/фон/постер с Кинопоиска
    backdrops, _ = await tmdb.images(tmdb_key, film, limit=12)
    urls = tmdb.pick_varied(backdrops, 12) + await _stills(fetch, film)
    photos = await tmdb.people(tmdb_key, film)
    pool_size = 12 if gemini_key else n
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        cover_src = await _download(session, _poster_url(film))
        images: list[Image.Image] = []
        for url in urls:
            if len(images) >= pool_size:
                break
            try:
                images.append(await _download(session, url))
            except Exception:  # noqa: BLE001
                continue
        # факт про актёра/режиссёра — его фото (TMDB, запасной — Кинопоиск)
        portraits: dict[int, Image.Image] = {}
        for i, fact in enumerate(facts):
            person = _mentioned_person(fact, film)
            if not person:
                continue
            urls_p = [photos.get(str(person.get("enName") or "").lower()), person.get("photo")]
            try:
                portraits[i] = _portrait(await _download_first(session, urls_p))
            except Exception:  # noqa: BLE001
                continue
    if not images:
        raise RuntimeError(f"Не скачались картинки для «{t}»")

    ai.last_error = None
    rest = [i for i in range(n) if i not in portraits]
    # факты без фото — сначала момент из трейлера, который Gemini счёл подходящим
    videos: dict[int, str] = {}
    if trailer_clips and gemini_key and rest:
        videos = await _fact_clips(film, [(i, facts[i]) for i in rest], work, gemini_key)
    # остальные — кадр, который Gemini выбрал по смыслу; без ИИ — по очереди
    stills = [i for i in rest if i not in videos]
    choices = await ai.match_images(gemini_key, [facts[i] for i in stills], images) if stills else None
    used: set[int] = set()
    picked: dict[int, Image.Image] = dict(portraits)
    for k, i in enumerate(stills + list(videos)):  # у фактов с видео кадр — запасной
        c = choices[k] if choices and k < len(stills) else -1
        if c < 0 or c in used:
            c = next((j for j in range(len(images)) if j not in used), k % len(images))
        used.add(c)
        picked[i] = images[c]

    for i, fact in enumerate(facts, 1):
        # название уже в заголовке слайда — на карточке не повторяем
        card = await asyncio.to_thread(make_card, picked[i - 1], "", i, n,
                                       None, "IMDb", None, f"ФАКТ №{i}", 780)
        path = work / f"card{i}.jpg"
        card.save(path, quality=93)
        video: dict[str, str] = {}
        if (i - 1) in videos:
            card_overlay("", i, n, None, "IMDb", None, f"ФАКТ №{i}", 780).save(work / f"overlay{i}.png")
            video = {"clip": videos[i - 1], "overlay": f"overlay{i}.png"}
        cards.append({"image": path.name, "caption": fact, "karaoke": True, **video})

    cover = await asyncio.to_thread(make_cover, cover_src, "ФАКТЫ", f"{hook}, которые ты не знал")
    cover_path = work / "cover.jpg"
    cover.save(cover_path, quality=93)

    lines = ([f"{hook}, которые ты точно не знал!"] + facts
             + [f"Какой факт удивил больше всего? Пиши в комментариях! {SAVE_AND_SUBSCRIBE}"])
    voiced = await _render(work, cover_path, cards, lines, "ФАКТЫ", t.upper(),
                           "КАКОЙ ФАКТ\nУДИВИЛ БОЛЬШЕ?\nПИШИ В КОММЕНТАРИЯХ", music)
    result = ShortResult(work / "short.mp4", cover_path, theme, [film], build_facts_seo(film, facts))
    if gemini_key and ai.last_error:
        result.seo["warning"] = f"⚠️ Gemini не помог подобрать кадры: {ai.last_error}"
    if not voiced:
        result.seo["warning"] = "⚠️ Голос не синтезировался — ролик собран только с музыкой."
    return result


async def make_daily_short(fetch: Fetch, work: Path, theme_key: str | None = None,
                           exclude: set[int] | None = None, music: Path | None = None,
                           today: date | None = None, tmdb_key: str = "",
                           news_feeds: list[str] | None = None, manual: bool = False,
                           gemini_key: str = "", trailer_clips: bool = True) -> ShortResult:
    """fetch(path, **params) -> dict — запрос к API Кинопоиска (PoiskKino._get)."""
    today = today or date.today()
    work.mkdir(parents=True, exist_ok=True)
    theme_key = theme_key or SCHEDULE[today.weekday()]
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
    # обратный отсчёт: показываем с №5 до №1, лучший — последним (ожидаемые — по порядку ожидания)
    ranked = films if theme.upcoming else sorted(films, key=_rank_key, reverse=True)
    films = ranked[::-1]

    cards = []
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        posters_tmdb: list[str] = []
        for i, f in enumerate(films, 1):
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
    if not voiced:
        result.seo["warning"] = "⚠️ Голос не синтезировался — ролик собран только с музыкой."
    return result

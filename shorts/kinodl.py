"""«Кинодл»: один фильм в день, угадать за 6 подсказок (как Wordle, но про кино).

Чистые функции: подсказки из карточки Кинопоиска и проверка ответа (с опечатками и без артиклей)."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

MAX_TRIES = 6


def _norm(text: str) -> str:
    t = str(text or "").lower().replace("ё", "е")
    t = re.sub(r"^(the|a|an)\s+", "", t)
    return " ".join(re.findall(r"[0-9a-zа-я]+", t))


def titles(film: dict[str, Any]) -> list[str]:
    names = [film.get("name"), film.get("alternativeName"), film.get("enName")]
    names += [n.get("name") for n in film.get("names") or [] if isinstance(n, dict)]
    return [str(n) for n in dict.fromkeys(names) if n]


def is_correct(guess: str, film: dict[str, Any]) -> bool:
    """Ответ засчитан: совпало название (рус., ориг.) — допускаем опечатку и название без подзаголовка."""
    g = _norm(guess)
    if len(g) < 2:
        return False
    for t in titles(film):
        n = _norm(t)
        if not n:
            continue
        if g == n or (len(g) >= 4 and SequenceMatcher(None, g, n).ratio() >= 0.85):
            return True
        main = _norm(re.split(r"[:.(—–-]", t)[0])  # «Дюна: Часть вторая» — достаточно «Дюна часть вторая»/«Дюна 2»
        if main and len(main) >= 4 and g == main and len(titles(film)) and len(main) >= len(n) * 0.5:
            return True
    return False


def _mask(text: str, film: dict[str, Any]) -> str:
    """Убрать из описания название фильма и имена героев из названия."""
    for t in titles(film):
        for word in re.findall(r"[A-Za-zА-Яа-яЁё0-9]{3,}", t):
            text = re.sub(rf"(?i)\b{re.escape(word)}\w*", "▒▒▒", text)
    return text


def clues(film: dict[str, Any]) -> list[str]:
    """Пять текстовых подсказок (шестая — кадр из фильма, её добавляет бот): от общего к узнаваемому."""
    genres = ", ".join(str(g.get("name")) for g in film.get("genres") or [] if isinstance(g, dict))
    countries = ", ".join(str(c.get("name")) for c in film.get("countries") or [] if isinstance(c, dict))
    persons = [p for p in film.get("persons") or [] if isinstance(p, dict)]
    directors = [str(p.get("name")) for p in persons if p.get("enProfession") == "director" and p.get("name")]
    actors = [str(p.get("name")) for p in persons if p.get("enProfession") == "actor" and p.get("name")]
    rating = (film.get("rating") or {}).get("kp")
    length = film.get("movieLength") or film.get("seriesLength")
    about = str(film.get("shortDescription") or film.get("description") or film.get("slogan") or "").strip()
    out = [
        f"📅 {film.get('year') or '?'} год · 🎭 {genres or 'жанр неизвестен'}" + (f" · 🌍 {countries}" if countries else ""),
        (f"⭐ Кинопоиск {float(rating):.1f}" if rating else "⭐ рейтинга нет")
        + (f" · ⏱ {length} мин" if length else "")
        + (" · 📺 сериал" if film.get("isSeries") else ""),
        "🎬 Режиссёр: " + (", ".join(directors[:2]) or "неизвестен"),
        "👥 В ролях: " + (", ".join(actors[:3]) or "неизвестно"),
        "📝 " + (_mask(about[:260], film) or "описания нет"),
    ]
    return out


def share_line(day: str, tries: int, solved: bool) -> str:
    """Строка для пересылки без спойлера: 🟥🟥🟩⬜⬜⬜."""
    cells = ["🟥"] * (tries - 1 if solved else tries) + (["🟩"] if solved else [])
    cells += ["⬜"] * (MAX_TRIES - len(cells))
    return f"🎬 Кинодл {day}: {''.join(cells[:MAX_TRIES])} {tries if solved else 'X'}/{MAX_TRIES}"

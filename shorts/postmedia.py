"""Отбор постов-источников, из которых можно брать фото и видео к новостному посту.

Сборные посты каналов (дайджесты) и посты, где наш фильм упомянут мимоходом, дают чужие картинки: к новости
о «I Play Rocky» приходил постер «Мстителей». Здесь — чистые функции без сети, их проверяют тесты
(tests/test_core.py)."""

from __future__ import annotations

import re

DIGEST_MARKERS = ("дайджест", "главные новости", "главное за", "за сегодня", "за неделю", "за день",
                  "другие новости", "также стало известно", "коротко о", "новости дня", "что нового",
                  "итоги недели", "подборка новостей", "news roundup", "in case you missed", "this week in")


def _is_digest(text: str) -> bool:
    """Сборный пост канала: несколько новостей и одна общая картинка — она почти всегда от другой новости
    (к посту о «I Play Rocky» пришёл постер «Мстителей» из такого дайджеста). Признаки: слова «дайджест»,
    «главное за неделю»; 3+ пункта списка; 5+ разных названий в «ёлочках»."""
    low = (text or "").lower()
    if any(m in low for m in DIGEST_MARKERS):
        return True
    items = re.findall(r"(?m)^\s*(?:\d{1,2}[.)]|[•▪️➖➡️🔹🔸◾️—–-])\s*\S", text or "")
    if len(items) >= 3:
        return True
    titles = {t.strip().lower() for t in re.findall(r"«([^«»]{2,60})»", text or "")
              if not re.match(r"(?i)оскар|золот|эмми|сезар|ник[аи]\b", t.strip())}  # премии — не новости
    return len(titles) >= 5


def _focused(text: str, names: list[str]) -> bool:
    """Пост целиком об этой новости, а не сборный: наше название в начале поста (первые ~220 знаков), первое
    название в «ёлочках» — наше, чужих не больше трёх (упоминания вроде «Рокки», «Зелёной книги» допустимы).
    Только из таких постов берём фото и видео."""
    if _is_digest(text):
        return False
    if not names:
        return True
    if not any(_mentions_title(n, _title_words(text[:220])) for n in names):
        return False
    quoted = [_title_words(t) for t in re.findall(r"«([^«»\n]{2,60})»", text or "")
              if t.strip()[:1].isupper() and len(t.split()) <= 6]
    quoted = [t for t in quoted if t and not re.match(r"оскар|золот|эмми|сезар", t)]

    def ours(t: str) -> bool:
        return any(_mentions_title(n, t) or _mentions_title(t, n) for n in names)

    if quoted and not ours(quoted[0]):
        return False  # первым назван другой фильм — пост не о нашей новости («перенесли «Мстителей», а ещё…»)
    return len({t for t in quoted if not ours(t)}) <= 3


def _title_words(text: str) -> str:
    """Для сравнения названий: строчные буквы и цифры через пробел, ё → е, без кавычек и знаков."""
    return " ".join(re.findall(r"[\wа-яё]+", str(text or "").lower().replace("ё", "е")))


def _mentions_title(name: str, text: str) -> bool:
    """Название есть в тексте — с учётом падежей: каждое слово названия по основе («левиафана», «мстителей»)."""
    if name in text:
        return True
    words = [w for w in name.split() if len(w) >= 3]
    hits = [w[:max(3, len(w) - 2)] in text for w in words]
    return bool(words) and hits[0] and sum(hits) * 2 >= len(words)

"""Инфоповоды: громкие новости кино, о которых одновременно пишут несколько источников.

Бот раз в 20 минут собирает свежие посты Telegram-каналов и быстрых зарубежных источников, группирует посты об
одной истории и считает, сколько разных источников о ней пишут. Громкая история — от HOT_SOURCES источников
или от 2, если речь о большой франшизе. Автор получает «🔥 Инфоповод» с кнопками: пост сейчас, тизер-ролик,
текст для посева/рекламы.

Сверка «о той же ли истории» — функциями бота (_key_tokens, _shared): их передают в clusters().
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from . import ai

HOT_SOURCES = 3
FRANCHISES = {  # громкие вселенные (им хватает двух источников): название → как пишут по-русски и по-английски
    "Marvel": ["marvel", "мстител", "avengers", "человек-паук", "spider-man", "дэдпул", "deadpool"],
    "Звёздные войны": ["star wars", "звездные войны", "мандалорец", "mandalorian"],
    "Очень странные дела": ["stranger things", "очень странные дела"],
    "Гарри Поттер": ["гарри поттер", "harry potter", "хогвартс", "hogwarts"],
    "DC": ["batman", "бэтмен", "супермен", "superman", "dc studios", "джокер", "joker"],
    "Джеймс Бонд": ["джеймс бонд", "james bond", "агент 007"],
    "Игра престолов": ["игра престолов", "game of thrones", "дом дракона", "house of the dragon"],
    "Властелин колец": ["властелин колец", "lord of the rings", "хоббит", "hobbit"],
    "Аватар": ["аватар", "avatar"],
    "Дюна": ["дюна", "dune"],
    "Джон Уик": ["джон уик", "john wick"],
    "Форсаж": ["форсаж", "fast & furious", "fast and furious"],
    "Миссия невыполнима": ["миссия невыполнима", "mission: impossible", "mission impossible"],
    "Чужой": ["чужой", "alien:", "aliens"],
    "Терминатор": ["терминатор", "terminator"],
    "Матрица": ["матрица", "matrix"],
    "Одни из нас": ["the last of us", "одни из нас"],
    "Уэнсдей": ["уэнсдей", "wednesday"],
    "Пацаны": ["пацаны", "the boys"],
}
BIG_NAMES = ["netflix", "pixar", "disney", "нолан", "nolan", "тарантино", "tarantino", "вильнев", "villeneuve",
             "кэмерон", "cameron"]  # громкие студии и режиссёры: повод, но не одна и та же история


def franchise(text: str) -> str:
    """Название франшизы из текста; для студий и режиссёров — само имя с пометкой «*» (не склеивает истории)."""
    t = text.lower().replace("ё", "е")
    for name, aliases in FRANCHISES.items():
        if any(a.replace("ё", "е") in t for a in aliases):
            return name
    return next((f"*{n}" for n in BIG_NAMES if n in t), "")


def clusters(posts: list[dict[str, Any]], key_tokens: Callable[[str], set[str]],
             shared: Callable[[set[str], set[str]], set[str]]) -> list[dict[str, Any]]:
    """Группы постов об одной истории: [{"posts", "sources", "tokens", "franchise", "score"}], громкие первыми.
    Русские посты — по общим основам слов (≥4 и ≥35% меньшего), английские — по общим латинским словам (≥3);
    между языками — по латинским именам/названиям и числам (≥2)."""
    groups: list[dict[str, Any]] = []
    for p in posts:
        words = key_tokens(p.get("text") or "")
        if len(words) < 3:
            continue
        en = p.get("lang") == "en"
        latin = {w for w in words if w.isascii() and not w.isdigit()}
        best = None
        for g in groups:
            if en or g["en"]:
                same = len(latin & g["latin"]) >= (3 if en and g["en"] else 2)
            else:
                hits = shared(g["tokens"], words)
                same = len(hits) >= 4 and len(hits) / min(len(g["tokens"]), len(words)) >= 0.35
            if same:
                best = g
                break
        if best is None:
            groups.append({"posts": [p], "tokens": set(words), "latin": latin, "en": en})
        else:
            best["posts"].append(p)
            best["tokens"] |= words
            best["latin"] |= latin
            best["en"] = best["en"] and en
    merged: dict[str, dict[str, Any]] = {}
    for g in groups:  # «Очень странные дела» и Stranger Things — одна история, а не два уведомления
        fr = franchise(" ".join(p.get("text") or "" for p in g["posts"]))
        if fr and not fr.startswith("*") and fr in merged:
            m = merged[fr]
            m["posts"] += g["posts"]
            m["tokens"] |= g["tokens"]
            g["posts"] = []
        elif fr and not fr.startswith("*"):
            merged[fr] = g
    groups = [g for g in groups if g["posts"]]
    out = []
    for g in groups:
        sources = {str(p.get("channel") or p.get("label") or p.get("url")).lower() for p in g["posts"]}
        fr = franchise(" ".join(p.get("text") or "" for p in g["posts"]))
        score = len(sources) + (1 if fr else 0)
        out.append({"posts": g["posts"], "sources": len(sources), "tokens": sorted(g["tokens"]),
                    "franchise": fr.lstrip("*"), "score": score})
    return sorted(out, key=lambda g: g["score"], reverse=True)


def hot(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [g for g in groups if g["sources"] >= HOT_SOURCES or (g["sources"] >= 2 and g["franchise"])]


def material(group: dict[str, Any], limit: int = 6) -> str:
    return "\n\n".join(f"[{p.get('channel') or p.get('label')}] " + re.sub(r"\s+", " ", p.get("text") or "")[:700]
                       for p in group["posts"][:limit])


async def teaser_lines(key: str, text: str) -> dict[str, str]:
    """ИИ: строки для тизера по новости. Нет ИИ — простые строки из первой фразы."""
    prompt = (
        "Ты делаешь 10-секундный тизер-ролик для Telegram-канала о кино KinoMir360 по новости ниже. Нужны "
        "короткие интригующие строки по-русски, только факты из новости (не выдумывай), без спойлеров. Название "
        "фильма/сериала — прокатное русское, в «ёлочках».\n"
        "hook — 1–2 слова или число, самое цепляющее (год, «ТИЗЕР», «ОФИЦИАЛЬНО», имя), до 10 символов;\n"
        "hook_sub — пояснение к hook, до 34 символов;\n"
        "line1 и line2 — главная мысль КАПСОМ в две строки, каждая до 16 символов;\n"
        "sub — что это за проект и дата/год, до 40 символов;\n"
        "outro — «Все подробности —» или похожая фраза до 24 символов (продолжение: «в канале KinoMir360»).\n\n"
        "НОВОСТЬ:\n" + text[:4000] +
        '\n\nОтветь только JSON: {"hook": "", "hook_sub": "", "line1": "", "line2": "", "sub": "", "outro": ""}')
    data = None
    try:
        data = await ai._post_json(key, prompt, search=False, verify=False)
    except Exception:  # noqa: BLE001 — без ИИ: простые строки
        data = None
    if isinstance(data, dict) and data.get("line1"):
        lim = {"hook": 10, "hook_sub": 34, "line1": 18, "line2": 18, "sub": 44, "outro": 26}
        return {k: str(data.get(k) or "").strip()[:n] for k, n in lim.items()}
    first = re.split(r"(?<=[.!?])\s", re.sub(r"\s+", " ", text).strip())[0]
    words = re.sub(r"[^\w\s«»-]", "", first).upper().split()
    return {"hook": "НОВОСТЬ", "hook_sub": "", "line1": " ".join(words[:2])[:18],
            "line2": " ".join(words[2:4])[:18], "sub": "", "outro": "Все подробности —"}


async def promo_texts(key: str, text: str, channel: str) -> dict[str, str]:
    """Тексты под инфоповод: пост для посевов (с эмодзи) и вариант для Директа (без эмодзи, заголовок до 56)."""
    prompt = (
        f"По новости ниже напиши рекламу Telegram-канала о кино {channel}. Только факты из новости. "
        "1) seed — пост для посева в чужом кино-канале: 3–5 коротких строк с эмодзи, интрига, в конце призыв "
        f"подписаться на {channel}, до 450 символов. "
        "2) direct_title — заголовок для Яндекс Директа до 56 символов, без эмодзи. "
        f"3) direct_text — текст поста для Директа без эмодзи до 500 символов, с призывом подписаться на {channel}."
        "\n\nНОВОСТЬ:\n" + text[:4000] +
        '\n\nОтветь только JSON: {"seed": "", "direct_title": "", "direct_text": ""}')
    data = None
    try:
        data = await ai._post_json(key, prompt, search=False, verify=False)
    except Exception:  # noqa: BLE001
        data = None
    if not isinstance(data, dict):
        return {}
    emoji = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")
    return {"seed": str(data.get("seed") or "").strip()[:700],
            "direct_title": emoji.sub("", str(data.get("direct_title") or "")).strip()[:56],
            "direct_text": emoji.sub("", str(data.get("direct_text") or "")).strip()[:760]}

"""Инфоповоды: громкие новости кино, о которых одновременно пишут несколько источников.

Бот раз в 20 минут собирает свежие посты Telegram-каналов и быстрых зарубежных источников, группирует посты об
одной истории и считает, сколько разных источников о ней пишут. Громкая история — от HOT_SOURCES источников
или от 2, если речь о большой франшизе. Автор получает «🔥 Инфоповод» с кнопками: пост сейчас, тизер-ролик,
текст для посева/рекламы.

Сверка «о той же ли истории» — функциями бота (_key_tokens, _shared): их передают в clusters().
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

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

STUDIOS = ("marvel.com", "disney.com", "thewaltdisneycompany.com", "d23.com", "netflix.com",
           "dc.com", "warnerbros.com", "sonypictures.com", "universalpictures.com", "paramount.com")
TRADE_PRESS = ("deadline.com", "variety.com", "hollywoodreporter.com", "empireonline.com")
SOURCE_LABELS = ("Нужна сверка с первоисточником", "Сообщает профильное издание", "Есть публикация студии")
INSIDER_ACCOUNTS = {"trailertrack", "cryptic4kqual", "danielrpk", "mytimetoshineh", "alexfromcc"}
TRAILER_REPORT = re.compile(r"\b(?:trailer|teaser)\b", re.I)
TRAILER_DETAIL = re.compile(r"\b(?:will (?:drop|release|premiere)|(?:drops|lands|premieres)(?:\s+on)?|"
                            r"release date|runtime|\d+\s*(?:minutes?|seconds?)|scheduled|now online)\b", re.I)
CAST_REPORT = re.compile(r"\b(?:cast as|has been cast|in talks to|will play|reportedly joins)\b", re.I)


def public_link(value: Any) -> str:
    url = str(value or "").strip()
    if len(url) > 1800 or any(ch.isspace() or ord(ch) < 32 for ch in url):
        return ""
    try:
        parts = urlsplit(url)
        if parts.scheme in {"https", "http"} and parts.hostname and not parts.username and not parts.password:
            return url
    except ValueError:
        pass
    return ""


def source_rank(post: dict[str, Any]) -> int:
    host = urlsplit(public_link(post.get("url"))).hostname or ""
    for rank, domains in ((2, STUDIOS), (1, TRADE_PRESS)):
        if any(host == domain or host.endswith("." + domain) for domain in domains):
            return rank
    return 0


def card(group: dict[str, Any]) -> dict[str, Any]:
    posts = sorted(group["posts"], key=source_rank, reverse=True)
    lead = posts[0]
    links: dict[str, str] = {}
    media: dict[str, str] = {}
    for post in posts:
        if url := public_link(post.get("url")):
            links.setdefault(url, str(post.get("channel") or post.get("label") or urlsplit(url).hostname))
        if url := public_link(post.get("article_url")):
            links.setdefault(url, "Ссылка из обсуждения")
        for kind, label in (("video", "Видео"), ("image", "Фото")):
            if url := public_link(post.get(kind)):
                media.setdefault(url, label)
    headline = str(lead.get("title") or lead.get("text") or "Инфоповод").strip().split("\n", 1)[0]
    status = ("Инсайд из X · один источник, требует проверки" if group.get("single_insider")
              else SOURCE_LABELS[source_rank(lead)])
    return {"headline": headline[:140], "status": status,
            "links": list(links.items())[:5], "media": list(media.items())[:4]}


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
        link = public_link(p.get("article_url") or p.get("url"))
        best = None
        for g in groups:
            if link in g["links"]:
                best = g
                break
            if en or g["en"]:
                overlap = len(latin & g["latin"])
                same = overlap >= (3 if en and g["en"] else 2) and overlap >= 0.4 * min(len(latin), len(g["latin"]))
            else:
                hits = shared(g["tokens"], words)
                same = len(hits) >= 4 and len(hits) / min(len(g["tokens"]), len(words)) >= 0.35
            if same:
                best = g
                break
        if best is None:
            groups.append({"posts": [p], "tokens": set(words), "latin": latin, "en": en,
                           "links": {link} if link else set()})
        else:
            best["posts"].append(p)
            best["tokens"] |= words
            best["latin"] |= latin
            best["en"] = best["en"] and en
            if link:
                best["links"].add(link)
    out = []
    for g in groups:
        sources = {str(p.get("channel") or p.get("label") or p.get("url")).lower() for p in g["posts"]}
        fr = franchise(" ".join(p.get("text") or "" for p in g["posts"]))
        score = len(sources) + (1 if fr else 0)
        out.append({"posts": g["posts"], "sources": len(sources), "tokens": sorted(g["tokens"]),
                    "franchise": fr.lstrip("*"), "score": score})
    return sorted(out, key=lambda g: g["score"], reverse=True)


def _insider_report(post: dict[str, Any]) -> bool:
    name = str(post.get("channel") or "").casefold().removeprefix("x @")
    url = urlsplit(public_link(post.get("url")))
    if name not in INSIDER_ACCOUNTS or url.hostname not in {"x.com", "twitter.com", "www.x.com", "www.twitter.com"}:
        return False
    if not re.fullmatch(rf"/{name}/status/\d+/?", url.path, re.I):
        return False
    text = str(post.get("text") or "")
    return bool(CAST_REPORT.search(text) or (TRAILER_REPORT.search(text) and TRAILER_DETAIL.search(text)))


def hot(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    confirmed, insider = [], []
    for g in groups:
        if g["sources"] >= HOT_SOURCES or (g["sources"] >= 2 and g["franchise"]) or any(
            source_rank(post) == 2 for post in g["posts"]
        ):
            confirmed.append(g)
        elif g["sources"] == 1 and g["franchise"] and any(_insider_report(post) for post in g["posts"]):
            insider.append({**g, "single_insider": True})
    return confirmed + insider


def material(group: dict[str, Any], limit: int = 6) -> str:
    posts = sorted(group["posts"], key=source_rank, reverse=True)
    return "\n\n".join(
        f"[{p.get('channel') or p.get('label') or 'источник'}] {public_link(p.get('url'))}\n"
        + re.sub(r"\s+", " ", p.get("text") or "")[:700] for p in posts[:limit]
    )


async def topic_short_plan(key: str, topic: str, title: str = "") -> dict[str, Any]:
    results = await asyncio.wait_for(ai.websearch.search(f"{topic} кино фильмы сериалы", n=6), timeout=50)
    posts = [{"url": url, "title": item["title"], "text": f"{item['title']}\n{item['text']}",
              "channel": urlsplit(url).hostname}
             for item in results if (url := public_link(item.get("url"))) and item.get("text")]
    if not posts:
        raise ValueError("Нет материалов по теме. Уточни название фильма или события.")
    return await short_plan(key, {"posts": posts}, topic=topic, title=title)


async def short_plan(key: str, group: dict[str, Any], *, topic: str = "", title: str = "") -> dict[str, Any]:
    """One request for narration and scene searches, reviewed in the normal Shorts workflow."""
    subject = (f"Тема автора: «{topic}». Бери из материалов только относящееся к этой теме. "
               "Если фактов по теме недостаточно, верни пустой список сцен. " if topic else "")
    title_rule = f"Название от автора: «{title}». Сохрани его. " if title else ""
    prompt = (
        ai.UNTRUSTED
        + "Сделай русскоязычный киношортс KinoMir360 на 30–45 секунд об одном инфоповоде. "
        + subject + title_rule +
        "5–8 коротких реплик, по одной на сцену, вместе 420–690 знаков. В первой реплике конкретный факт "
        "или честный вопрос, во второй что зритель увидит или почему это важно. Сохрани оговорки и ссылки "
        "на издателя в формулировках. Обсуждение и пересказы сами по себе не подтверждают факт. "
        "По слуху прямо назови это слухом. Бери только данные ниже, не придумывай цитаты, кадры и ссылки. "
        "Для каждой реплики опиши, что показать, дай короткий поисковый запрос с названием проекта "
        "и короткую надпись. Если конкретный кадр не подтверждён, предложи нейтральное оформление. "
        'Ответь JSON: {"title":"", "description":"", "hashtags":"#тег", '
        '"scenes":[{"line":"", "visual":"", "query":"", "label":""}]}\n\n'
        f"Статус источника: {card(group)['status']}.\n\n{material(group, limit=4)}"
    )
    data = await ai._with_reserve(key, [{"text": prompt}], prompt, reserve_max_tokens=1700,
                                  paid_max_tokens=1700, allow_paid=False)
    if not isinstance(data, dict) or not isinstance(data.get("scenes"), list) or not 5 <= len(data["scenes"]) <= 8:
        raise ValueError("ИИ не вернул сценарий на 5–8 сцен. Карточка сохранена, можно повторить.")

    def field(row: dict, name: str, limit: int) -> str:
        value = row.get(name)
        return " ".join(value.split())[:limit] if isinstance(value, str) else ""

    title = title or field(data, "title", 95)
    lines, scenes = [], []
    for row in data["scenes"]:
        if not isinstance(row, dict):
            raise ValueError("ИИ вернул неполную сцену.")
        line, target, query = (field(row, name, 200) for name in ("line", "visual", "query"))
        if not all((line, target, query)) or any("http" in part.lower() for part in (line, target, query)):
            raise ValueError("У сцены нет реплики, описания кадра или поискового запроса.")
        lines.append(line)
        scenes.append({"heading": "Видеоряд", "targets": [target], "label": field(row, "label", 60),
                       "queries": [query], "sources": [], "source_hint": query, "fallback": "",
                       "control": "Сверь фильм, персонажа и действие с фразой."})
    if not title or not 390 <= len(" ".join(lines)) <= 720:
        raise ValueError("ИИ вернул сценарий без заголовка или за пределами короткого хронометража.")
    sources = "\n".join(url for url, _ in card(group)["links"][:3])
    description_text = re.sub(r"https?://\S+", "", field(data, "description", 600), flags=re.I).strip()
    description = "\n\n".join(filter(None, (description_text, sources)))
    hashtags = " ".join(dict.fromkeys(re.findall(r"#[\w]+", field(data, "hashtags", 180))))
    return {"title": title, "description": description, "hashtags": hashtags, "lines": lines, "scenes": scenes}


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

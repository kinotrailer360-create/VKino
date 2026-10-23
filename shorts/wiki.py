"""Награды и номинации фильма из Викиданных (по IMDb ID)."""

from __future__ import annotations

import logging
import re
from typing import Any

import aiohttp

SPARQL = "https://query.wikidata.org/sparql"
# Викиданные просят осмысленный User-Agent с контактом
USER_AGENT = "KinoMir360-shorts/1.0 (https://t.me/KinoMir360)"

# английское название награды -> как пишем по-русски; порядок = важность
AWARDS = [
    (("Academy Award",), "Оскар"),
    (("Primetime Emmy", "Emmy Award"), "Эмми"),
    (("Golden Globe",), "Золотой глобус"),
    (("BAFTA", "British Academy"), "BAFTA"),
    (("Palme d'Or",), "Золотая пальмовая ветвь"),
    (("Golden Lion",), "Золотой лев"),
    (("Golden Bear",), "Золотой медведь"),
    (("Nika Award",), "Ника"),
    (("Golden Eagle Award",), "Золотой орёл"),
    (("Saturn Award",), "Сатурн"),
]

log = logging.getLogger("shorts")

QUERY = """
SELECT ?kind ?award ?awardLabel WHERE {
  ?film wdt:P345 "%s" .
  { ?film wdt:P166 ?award . BIND("win" AS ?kind) }
  UNION
  { ?film wdt:P1411 ?award . BIND("nom" AS ?kind) }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""


def _group(label: str) -> str | None:
    for prefixes, name in AWARDS:
        if label.startswith(prefixes):
            return name
    return None


async def awards(item: dict[str, Any]) -> dict[str, dict[str, int]]:
    """{"wins": {"Оскар": 4}, "noms": {"Оскар": 7}} — пусто, если данных нет."""
    imdb = str((item.get("externalId") or {}).get("imdb") or "")
    if not re.fullmatch(r"tt\d+", imdb):
        return {"wins": {}, "noms": {}}
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as session:
            async with session.get(SPARQL, params={"query": QUERY % imdb, "format": "json"}) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001 — без наград ролик всё равно собираем
        log.info("shorts: Wikidata awards for %s failed: %s", imdb, exc)
        return {"wins": {}, "noms": {}}

    seen: dict[str, set[str]] = {"win": set(), "nom": set()}
    result: dict[str, dict[str, int]] = {"wins": {}, "noms": {}}
    for row in (data.get("results") or {}).get("bindings") or []:
        kind = (row.get("kind") or {}).get("value")
        award = (row.get("award") or {}).get("value", "")
        group = _group((row.get("awardLabel") or {}).get("value", ""))
        if kind not in seen or not group or award in seen[kind]:
            continue
        seen[kind].add(award)
        bucket = result["wins" if kind == "win" else "noms"]
        bucket[group] = bucket.get(group, 0) + 1
    return result


def _order(groups: dict[str, int]) -> list[tuple[str, int]]:
    rank = {name: i for i, (_, name) in enumerate(AWARDS)}
    return sorted(groups.items(), key=lambda kv: rank[kv[0]])


def awards_line(aw: dict[str, dict[str, int]], limit: int = 2) -> str | None:
    """Строка под карточкой: «Оскар» ×4 • «Золотой глобус»."""
    parts = [f"«{name}»" + (f" ×{n}" if n > 1 else "") for name, n in _order(aw["wins"])[:limit]]
    return " • ".join(parts) or None


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def awards_fact(title: str, aw: dict[str, dict[str, int]]) -> str | None:
    """Факт для озвучки: «X» получил 4 премии «Оскар» при 7 номинациях."""
    if aw["wins"]:
        name, wins = _order(aw["wins"])[0]
        noms = aw["noms"].get(name, 0)
        text = f"«{title}» получил {wins} {_plural(wins, 'премию', 'премии', 'премий')} «{name}»"
        if noms > wins:
            text += f" при {noms} {_plural(noms, 'номинации', 'номинациях', 'номинациях')}"
        return text + "."
    if aw["noms"]:
        name, noms = _order(aw["noms"])[0]
        return f"«{title}» {noms} {_plural(noms, 'раз', 'раза', 'раз')} номинировался на «{name}»."
    return None


# ---------------------------------------------------------------- дни рождения

# «Родились в этот день» из Википедии (лёгкий запрос), подробности — пачкой из Викиданных.
# Тяжёлый SPARQL по всем актёрам Викиданные не тянут (502 по таймауту).
ONTHISDAY = "https://{lang}.wikipedia.org/api/rest_v1/feed/onthisday/births/{month:02d}/{day:02d}"
ENTITIES = "https://www.wikidata.org/w/api.php"
FILM_JOBS = {
    "en": r"\b(actor|actress|film director|filmmaker|director)\b",
    "ru": r"(актёр|актер|актриса|режиссёр|режиссер)",
}
FEMALE = "Q6581072"


def _claim(entity: dict[str, Any], prop: str) -> Any:
    for claim in (entity.get("claims") or {}).get(prop) or []:
        value = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if value is not None:
            return value
    return None


async def _born_today(session: aiohttp.ClientSession, month: int, day: int) -> list[str]:
    """Q-идентификаторы людей кино, родившихся в этот день (англ. Википедия, запасная — русская)."""
    ids: list[str] = []
    errors = []
    for lang in ("en", "ru"):
        try:
            async with session.get(ONTHISDAY.format(lang=lang, month=month, day=day)) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{lang}: {exc}")
            continue
        for event in data.get("births") or []:
            for page in event.get("pages") or []:
                qid = page.get("wikibase_item")
                if qid and qid not in ids and re.search(FILM_JOBS[lang], str(page.get("description") or ""), re.I):
                    ids.append(qid)
        if ids:
            return ids
    if errors:
        raise RuntimeError("Википедия не ответила: " + "; ".join(errors))
    return ids


async def birthdays(month: int, day: int, limit: int = 10) -> list[dict[str, Any]]:
    """Живые звёзды кино, родившиеся в этот день, самые известные первыми.

    [{"name", "en", "born": "YYYY-MM-DD", "female": bool, "imdb": "nm…"}]; ошибка — исключение.
    Известность — по числу статей о человеке в Википедиях на разных языках.
    """
    timeout = aiohttp.ClientTimeout(total=40)
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as session:
        ids = await _born_today(session, month, day)
        entities: dict[str, Any] = {}
        for i in range(0, len(ids), 50):  # API отдаёт до 50 записей за раз
            params = {"action": "wbgetentities", "ids": "|".join(ids[i:i + 50]), "format": "json",
                      "props": "labels|claims|sitelinks", "languages": "ru|en"}
            async with session.get(ENTITIES, params=params) as resp:
                resp.raise_for_status()
                entities.update((await resp.json(content_type=None)).get("entities") or {})
    out: list[dict[str, Any]] = []
    for qid in ids:
        e = entities.get(qid) or {}
        labels = e.get("labels") or {}
        imdb = _claim(e, "P345")
        birth = _claim(e, "P569")
        born = str(birth.get("time") or "") if isinstance(birth, dict) else ""
        if _claim(e, "P570") is not None or not str(imdb or "").startswith("nm") or "ru" not in labels:
            continue  # умершие, без IMDb или без русского имени
        if len(born) < 11 or born[6:11] != f"{month:02d}-{day:02d}":
            continue  # дата в Викиданных не совпала (бывает старый/новый стиль)
        gender = _claim(e, "P21")
        out.append({
            "name": labels["ru"]["value"],
            "en": (labels.get("en") or {}).get("value", ""),
            "born": born[1:11],
            "female": isinstance(gender, dict) and gender.get("id") == FEMALE,
            "imdb": imdb,
            "_fame": len(e.get("sitelinks") or {}),
        })
    out.sort(key=lambda p: p["_fame"], reverse=True)
    return out[:limit]


ARTICLE_API = "https://{lang}.wikipedia.org/w/api.php"
# разделы статьи, где обычно самое интересное для фактов
FACT_SECTIONS = re.compile(r"(производств|съёмк|создани|кастинг|подбор актёр|интересн|факт|прокат|сборы|критик|"
                           r"production|filming|casting|development|trivia|box office|reception|legacy)", re.I)


async def _article(session: aiohttp.ClientSession, lang: str, query: str, limit: int) -> str:
    api = ARTICLE_API.format(lang=lang)
    base = {"format": "json", "formatversion": 2}
    async with session.get(api, params={**base, "action": "query", "list": "search", "srsearch": query,
                                        "srlimit": 1}) as resp:
        hits = ((await resp.json(content_type=None)).get("query") or {}).get("search") or []
    if not hits:
        return ""
    async with session.get(api, params={**base, "action": "query", "prop": "extracts", "explaintext": 1,
                                        "redirects": 1, "titles": hits[0]["title"]}) as resp:
        page = (((await resp.json(content_type=None)).get("query") or {}).get("pages") or [{}])[0]
    text = str(page.get("extract") or "")
    # вступление + разделы про съёмки, кастинг, сборы; пересказ сюжета пропускаем (там спойлеры)
    parts = re.split(r"\n(?===+ )", text)
    keep = [parts[0][:1500]] + [p for p in parts[1:] if FACT_SECTIONS.search(p.split("\n", 1)[0])]
    return f"[{lang}.wikipedia.org — {hits[0]['title']}]\n" + "\n".join(keep)[:limit]


async def film_article(title: str, en: str = "", year: int | None = None, limit: int = 9000) -> str:
    """Текст о фильме из русской и английской Википедии (без сюжета) — материал для фактов. Пусто — не нашли."""
    y = f" {year}" if year else ""
    jobs = [("ru", f"{title}{y} фильм")] + ([("en", f"{en}{y} film")] if en else [])
    out = []
    try:
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as session:
            for lang, query in jobs:
                try:
                    text = await _article(session, lang, query, limit)
                except Exception as exc:  # noqa: BLE001
                    log.warning("shorts: wiki %s article %r failed: %s", lang, query, exc)
                    continue
                if text:
                    out.append(text)
    except Exception as exc:  # noqa: BLE001
        log.warning("shorts: wiki article for %r failed: %s", title, exc)
    return "\n\n".join(out)

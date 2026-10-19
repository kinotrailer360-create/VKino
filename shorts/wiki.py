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

# актёр, киноактёр, телеактёр, кинорежиссёр; известность — по числу статей в Википедиях (sitelinks)
BIRTHDAYS = """
SELECT ?p ?ru ?en ?born ?gender ?imdb ?sl WHERE {
  VALUES ?occ { wd:Q33999 wd:Q10800557 wd:Q10798782 wd:Q2526255 }
  ?p wdt:P106 ?occ ; wikibase:sitelinks ?sl .
  FILTER(?sl > 40)
  ?p wdt:P569 ?born .
  FILTER(MONTH(?born) = %d && DAY(?born) = %d)
  FILTER NOT EXISTS { ?p wdt:P570 [] }
  ?p wdt:P345 ?imdb .
  FILTER(STRSTARTS(?imdb, "nm"))
  ?p rdfs:label ?ru . FILTER(LANG(?ru) = "ru")
  OPTIONAL { ?p rdfs:label ?en . FILTER(LANG(?en) = "en") }
  OPTIONAL { ?p wdt:P21 ?gender }
}
ORDER BY DESC(?sl)
LIMIT 40
"""
FEMALE = "http://www.wikidata.org/entity/Q6581072"


async def birthdays(month: int, day: int, limit: int = 10) -> list[dict[str, Any]]:
    """Живые звёзды кино, родившиеся в этот день, самые известные первыми.

    [{"name", "en", "born": "YYYY-MM-DD", "female": bool, "imdb": "nm…"}]; ошибка — исключение.
    """
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as session:
        async with session.get(SPARQL, params={"query": BIRTHDAYS % (month, day), "format": "json"}) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in data["results"]["bindings"]:
        imdb = row["imdb"]["value"]
        if imdb in seen:  # несколько профессий/дат — одна запись
            continue
        seen.add(imdb)
        out.append({
            "name": row["ru"]["value"],
            "en": (row.get("en") or {}).get("value", ""),
            "born": row["born"]["value"][:10],
            "female": (row.get("gender") or {}).get("value") == FEMALE,
            "imdb": imdb,
        })
        if len(out) >= limit:
            break
    return out

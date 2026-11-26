"""Structured news cards. Source IDs must resolve to retrieved articles."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

EDIT_PREFIX = "Редактирование новости · "
SIGNATURE = "🎬 KinoMir360 | YouTube | KinoMir360 в MAX"
MAX_POST = 1500
MAX_CARDS = 5
DISCOVER = re.compile(r"^\s*(?:(?:найди|поищи|подбери|покажи|пришли|дай|собери)\b.{0,65}\bновост\w*|(?:какие|есть ли)\b.{0,50}\bновост\w*|что нового\b|последние новости\b)", re.I | re.S)
OTHER_FORMAT = re.compile(r"\b(?:списком|список|фактчек|проверь|threads|shorts|шортс\w*)\b|без постов|только заголовки", re.I)


def is_discovery(question: str) -> bool:
    return bool(DISCOVER.search(question) and not OTHER_FORMAT.search(question))


def today() -> date:
    return datetime.now(ZoneInfo("Asia/Yekaterinburg")).date()


def prompt(question: str) -> str:
    return (
        question + "\n\nЗАДАНИЕ В РЕЖИМЕ КАРТОЧЕК НОВОСТЕЙ. Сегодня "
        + today().isoformat()
        + ". Пользователь просит не список, а до пяти отдельных готовых постов Telegram KinoMir360. "
          "Не добавляй Threads, Shorts или общий фактчек. Ищи по русскому и английскому названию, "
          "за последние сутки, если мало — до 7 дней; если период задан явно, соблюдай его. "
          "Каждая карточка — один реально найденный инфоповод, отдельная дата публикации, а не дата события. "
          "Сборы за уикенд, накопительные суммы и прогнозы не смешивай. Не используй Wikipedia, "
          "страницы тегов, общие подборки или главные страницы как источник статьи. "
          "Ссылайся на реальные числовые id из полученного веб-контекста <source id=...>. "
          "Если не нашёл конкретный материал с датой, пропусти инфоповод. Не выдумывай id. "
          "Для каждого source_id укажи evidence: точную непрерывную цитату на языке оригинала из этого источника, "
          "8–20 слов, которая подтверждает главный факт карточки, включая его важные ограничения. "
          "Не переводи цитату, не используй многоточие, не бери общий заголовок о франшизе вместо доказательства. "
          "Рекорд за праздничный период не означает рекорд за все периоды. Если точного подтверждения нет — убери факт или карточку. "
          "Каждый post — готовый русский текст Telegram, до 900 символов: заголовок, компактные абзацы, "
          "реакции и тематические хэштеги по смыслу. Без служебных ссылок, [N], Markdown, подписи канала "
          "(бот добавит сам) и без обещаний не подтверждённых фактов. Слухи явно маркируй прямо в тексте. "
          "Верни только корректный JSON, без пояснений вне JSON, структура: "
          '{"posts":[{"date":"YYYY-MM-DD","status":"СМИ","post":"ЗАГОЛОВОК\\n\\nТекст","source_ids":[2],"evidence":{"2":"точная цитата из источника"}}],'
          '"note":"краткое пояснение при нехватке материалов или расширении периода"}. '
          "status: Официально, СМИ, Слух или Аналитика. При отсутствии материалов верни пустой posts."
    )


def clean_post(text: str) -> str:
    if not isinstance(text, str):
        raise ValueError("Нужен текст поста.")
    text = re.sub(r"\s*🎬 KinoMir360 \| YouTube(?: \| KinoMir360 в MAX)?\s*$", "", text.strip())
    text = re.sub(r"\[\d{1,4}\]", "", text)
    text = text.replace("**", "").strip()
    if not text or len(text) > MAX_POST:
        raise ValueError("Текст поста должен содержать от 1 до 1500 символов.")
    return text


def _article_url(url):
    if not isinstance(url, str) or len(url) > 1800 or any(c.isspace() for c in url):
        return False
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        path = p.path.lower()
        return (p.scheme in {"http", "https"} and bool(host) and not p.username and not p.password
                and p.port in (None, 80, 443) and bool(path.strip("/"))
                and not (host == "wikipedia.org" or host.endswith(".wikipedia.org"))
                and not any(x in path for x in ("/tag/", "/tags/", "/category/", "/search/")))
    except ValueError:
        return False


def _normalize_evidence(text):
    return " ".join(text.translate(str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-"})).casefold().split())


def source_records(data):
    records = {}
    for item in data.get("sources", []) or []:
        if not isinstance(item, dict) or (item.get("source") or {}).get("type") != "web_search":
            continue
        ids = item.get("citation_ids") or []
        docs = item.get("document") or []
        for i, meta in enumerate(item.get("metadata") or []):
            if i >= len(ids) or not isinstance(meta, dict):
                continue
            url = meta.get("source") or meta.get("url")
            cid = str(ids[i])
            doc = docs[i] if isinstance(docs, list) and i < len(docs) else None
            if cid.isdigit() and _article_url(url) and isinstance(doc, str) and len(doc) >= 80:
                records[cid] = {"url": url, "title": " ".join(str(meta.get("title") or urlsplit(url).hostname).split())[:160],
                                "document": _normalize_evidence(doc[:100000])}
    return records


def parse_cards(answer, data, question, *, current=None):
    """Do not fabricate or silently substitute a source for an invalid citation."""
    current = current or today()
    raw = answer.strip()
    fence = chr(96) * 3
    if raw.startswith(fence):
        raw = re.sub("^" + fence + r"(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*" + fence + "$", "", raw)
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("posts"), list):
        raise ValueError("Неверный формат подборки.")
    records = source_records(data)
    note = " ".join(str(parsed.get("note") or "").split())[:400]
    cards = []
    used_titles, used_sources = set(), set()
    explicit = bool(re.search(r"\b(?:за|с|от)\s+(?:последн\w+\s+)?(?:\d+|сутки|недел\w*|месяц\w*|год\w*|вчера)|\bвчера\b|\bсегодня\b|\b\d{4}\s*год", question, re.I))
    for item in parsed["posts"][:15]:
        if not isinstance(item, dict):
            continue
        try:
            stamp = date.fromisoformat(item.get("date", ""))
            post = clean_post(item.get("post"))
        except (ValueError, TypeError):
            continue
        if stamp > current or (not explicit and stamp < current - timedelta(days=7)):
            continue
        status = item.get("status")
        if status not in {"Официально", "СМИ", "Слух", "Аналитика"}:
            continue
        ids = item.get("source_ids")
        evidence = item.get("evidence")
        if not isinstance(ids, list) or not isinstance(evidence, dict):
            continue
        selected = []
        seen = set()
        for cid in ids[:10]:
            rec = records.get(str(cid))
            quote = evidence.get(str(cid))
            if (rec and rec["url"] not in seen and isinstance(quote, str)
                    and 6 <= len(quote.split()) <= 20 and 30 <= len(quote) <= 300
                    and _normalize_evidence(quote) in rec["document"]):
                selected.append({"url": rec["url"], "title": rec["title"], "evidence": " ".join(quote.split())})
                seen.add(rec["url"])
            if len(selected) == 2:
                break
        title = re.sub(r"\W+", "", post.splitlines()[0].casefold())
        identity = frozenset(seen)
        if not selected or not title or title in used_titles or identity in used_sources:
            continue
        if re.search(r"https?://|\b(?:источники|источник)\s*:", post, re.I):
            continue
        if status == "Слух" and not re.search(r"\b(?:слух\w*|инсайд\w*|не подтвержд\w*|неофициальн\w*)\b", post, re.I):
            post = "⚠️ СЛУХ\n\n" + post
        used_titles.add(title)
        used_sources.add(identity)
        cards.append({"post": post, "date": stamp.isoformat(), "status": status, "sources": selected})
        if len(cards) >= MAX_CARDS:
            break
    cards.sort(key=lambda c: c["date"], reverse=True)
    return {"cards": cards, "note": note, "omitted": max(0, len(parsed["posts"]) - len(cards))}


def source_note(card):
    note = f"Дата материала: {card['date']} · {card['status']}"
    if card.get("edited"):
        note += "\nТекст изменён вручную. Источники относятся к исходной новости; проверь внесённые изменения."
    note += "\nИсточники для автора (не входят в пост):"
    for src in card["sources"]:
        note += "\n\n" + src["title"] + "\n" + src["url"] + "\n" + src.get("availability", "Доступность не проверена.")
        if src.get("evidence"):
            note += "\nФрагмент источника: «" + src["evidence"] + "»"
    return note + "\n\nФрагмент найден в тексте источника; соответствие всех утверждений и даты проверь перед публикацией."

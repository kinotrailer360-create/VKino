"""Scene Planner: deterministic mapping of narration phrases to visual scenes.

The first iteration deliberately does not guess semantic relevance. It records the
selected asset, exact narration interval, provenance when known, and review state.
Low-confidence or unknown provenance is surfaced for human review, never silently
treated as an approved semantic match.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

# права на материал сцены: материал автора — его; ИИ — не кадр из фильма; остальное — источник записан,
# но лицензия не подтверждена (публиковать можно, при жалобе по источнику ясно, что заменить)
RIGHTS = {"user_media": "author", "user_replacement": "author", "ai_generated_illustration": "ai"}
RIGHTS_LABEL = {"author": "✅ материал автора", "ai": "🎨 ИИ-иллюстрация, не кадр из фильма",
                "unconfirmed": "⚠️ права не подтверждены"}
SOURCE_LABEL = {"trailer_clip": "трейлер", "film_frame_pool": "кадр TMDB/Кинопоиска", "film_poster_fallback": "постер",
                "web_image_search": "поиск картинок", "person_portrait": "фото человека", "user_media": "ваш материал",
                "user_replacement": "ваша замена", "channel_media": "Telegram-канал",
                "ai_generated_illustration": "ИИ-иллюстрация"}


FACT_LABEL = {"confirmed": "✅ подтверждён", "single": "🟡 один источник", "conflict": "❌ источники расходятся",
              "unverified": "⚠️ не подтверждён источниками", "author": "✍️ текст автора"}


def fact_line(fact: dict[str, Any]) -> str:
    """Строка о проверке факта сцены для автора."""
    if not fact:
        return ""
    src = str(fact.get("source") or "")
    note = str(fact.get("note") or "")
    return (f"🔎 Факт: {FACT_LABEL.get(str(fact.get('status')), str(fact.get('status')))}"
            + (f" ({src})" if src and fact.get("status") != "author" else "")
            + (f" — {note}" if note else "") + (f" · проверено {fact['checked']}" if fact.get("checked") else ""))


def rights(source: str) -> str:
    return RIGHTS.get(source, "unconfirmed")


def _digest(path: Path) -> str:
    """Отпечаток файла: размер + начало и конец (видео бывают большими — целиком не читаем)."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            head = f.read(1 << 20)
            f.seek(max(0, size - (1 << 20)))
            tail = f.read(1 << 20)
    except OSError:
        return "missing"
    return hashlib.sha256(str(size).encode() + head + tail).hexdigest()[:24]


def scene_hash(scene: dict[str, Any], folder: Path) -> str:
    """Версия сцены: текст, время, материал (содержимое файла). Изменилось что-то — утверждение не действует."""
    asset = scene.get("asset") or {}
    path = Path(str(asset.get("path") or ""))
    path = path if path.is_absolute() else folder / path
    raw = "|".join([str(scene.get("text") or ""), f"{float(scene.get('start') or 0):.2f}",
                    f"{float(scene.get('end') or 0):.2f}", str(asset.get("type") or ""), _digest(path)])
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def sources_summary(plan: dict[str, Any]) -> str:
    """Под роликом: откуда кадры и у скольких права не подтверждены."""
    scenes = plan.get("scenes") or []
    counts: dict[str, int] = {}
    for s in scenes:
        name = SOURCE_LABEL.get(str((s.get("asset") or {}).get("source") or ""), "другое")
        counts[name] = counts.get(name, 0) + 1
    unconfirmed = sum(1 for s in scenes if (s.get("asset") or {}).get("rights", "unconfirmed") == "unconfirmed")
    parts = ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
    out = f"📎 Кадров: {len(scenes)} — {parts}." + (f" Права не подтверждены у {unconfirmed}." if unconfirmed else "")
    bad = [str(i + 1) for i, s in enumerate(scenes) if (s.get("fact") or {}).get("status") in ("conflict", "unverified")]
    if bad:
        out += f"\n⚠️ Факты без подтверждения или с расхождением: сцены {', '.join(bad)} — проверь до утверждения."
    return out


def build_scene_plan(
    lines: list[str],
    starts: list[float],
    spans: list[dict[str, Any]],
    cards: list[dict[str, Any]],
    *,
    first_card_line: int = 1,
    require_review: bool = True,
) -> dict[str, Any]:
    """Build a timed scene plan from synthesized phrase timings and rendered cards.

    `first_card_line` is the index in narration lines represented by cards[0].
    A scene occupies its phrase start through the next phrase start, so inter-phrase
    pauses remain attached to the current visual, matching the current renderer.
    """
    scenes: list[dict[str, Any]] = []
    for card_index, card in enumerate(cards):
        line_index = first_card_line + card_index
        if line_index >= len(lines) or line_index >= len(starts) or line_index >= len(spans):
            break
        start = max(0.0, float(starts[line_index]))
        speech = max(0.0, float(spans[line_index].get("speech") or 0.0))
        next_start = float(starts[line_index + 1]) if line_index + 1 < len(starts) else start + speech
        end = max(start, next_start, start + speech)
        clip = card.get("clip")
        image = card.get("image")
        asset_type = "clip" if clip else "still" if image else "missing"
        asset_path = clip or image
        source = card.get("source") or card.get("asset_source") or "unknown"
        visual_match = card.get("visual_match") or {}
        match_status = str(visual_match.get("status") or "unverified")
        match_score = visual_match.get("score")
        scenes.append({
            "id": f"scene-{card_index + 1:02d}",
            "line_index": line_index,
            "text": str(lines[line_index]),
            "start": round(start, 3),
            "end": round(end, 3),
            "duration": round(max(0.0, end - start), 3),
            "asset": {"type": asset_type, "path": asset_path, "source": source,
                      "ref": str(card.get("asset_ref") or "")[:300], "rights": rights(str(source))},
            "fact": card.get("fact_check") or {},
            "match": {"status": match_status, "score": match_score,
                      "reason": str(visual_match.get("reason") or "")[:160]},
            "review": {"required": bool(require_review), "approved": False, "replacement": None},
        })
    return {
        "schema_version": 1,
        "planner": "vkino-scene-planner",
        "timing_source": "tts_phrase_timestamps",
        "review_mode": "required" if require_review else "disabled",
        "scenes": scenes,
    }

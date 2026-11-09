"""Scene Planner: deterministic mapping of narration phrases to visual scenes.

The first iteration deliberately does not guess semantic relevance. It records the
selected asset, exact narration interval, provenance when known, and review state.
Low-confidence or unknown provenance is surfaced for human review, never silently
treated as an approved semantic match.
"""
from __future__ import annotations

from typing import Any


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
        scenes.append({
            "id": f"scene-{card_index + 1:02d}",
            "line_index": line_index,
            "text": str(lines[line_index]),
            "start": round(start, 3),
            "end": round(end, 3),
            "duration": round(max(0.0, end - start), 3),
            "asset": {"type": asset_type, "path": asset_path, "source": source},
            "match": {"status": "unverified", "score": None},
            "review": {"required": bool(require_review), "approved": False, "replacement": None},
        })
    return {
        "schema_version": 1,
        "planner": "vkino-scene-planner",
        "timing_source": "tts_phrase_timestamps",
        "review_mode": "required" if require_review else "disabled",
        "scenes": scenes,
    }

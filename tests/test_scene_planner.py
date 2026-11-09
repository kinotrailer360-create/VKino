"""Regression tests for Smart Visuals scene-plan timing and review metadata."""

from shorts.scene_planner import build_scene_plan


def test_scene_plan_uses_real_phrase_timing_and_asset_review_state() -> None:
    plan = build_scene_plan(
        ["Вступление", "Факт первый", "Факт второй"],
        [0.0, 2.4, 6.1],
        [{"speech": 1.8}, {"speech": 3.0}, {"speech": 2.2}],
        [
            {
                "image": "card1.jpg",
                "asset_source": "ai_generated_illustration",
                "visual_match": {
                    "status": "generated_illustration_review",
                    "score": None,
                    "reason": "Оригинальная иллюстрация, не кадр фильма",
                },
            },
            {
                "image": "card2.jpg",
                "asset_source": "film_frame_pool",
                "visual_match": {"status": "matched", "score": 84, "reason": "Подходит по сцене"},
            },
        ],
    )

    assert plan["timing_source"] == "tts_phrase_timestamps"
    assert len(plan["scenes"]) == 2
    first, second = plan["scenes"]
    assert (first["start"], first["end"], first["duration"]) == (2.4, 6.1, 3.7)
    assert first["asset"]["source"] == "ai_generated_illustration"
    assert first["match"]["status"] == "generated_illustration_review"
    assert first["review"]["required"] is True
    assert first["review"]["approved"] is False
    assert second["match"]["score"] == 84
    assert second["line_index"] == 2


def test_scene_plan_marks_unknown_asset_without_auto_approval() -> None:
    plan = build_scene_plan(
        ["Вступление", "Факт"],
        [0.0, 1.0],
        [{"speech": 0.5}, {"speech": 1.2}],
        [{"caption": "Факт"}],
    )

    scene = plan["scenes"][0]
    assert scene["asset"]["type"] == "missing"
    assert scene["asset"]["source"] == "unknown"
    assert scene["match"]["status"] == "unverified"
    assert scene["review"]["approved"] is False

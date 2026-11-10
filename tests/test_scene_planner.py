"""Regression tests for Smart Visuals scene-plan timing and review metadata."""

from PIL import Image

from shorts.make_short import ClipReader
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



def test_clip_reader_holds_last_frame_instead_of_looping(tmp_path) -> None:
    first = tmp_path / "f0001.jpg"
    second = tmp_path / "f0002.jpg"
    Image.new("RGB", (8, 8), (240, 10, 10)).save(first, quality=100)
    Image.new("RGB", (8, 8), (10, 10, 240)).save(second, quality=100)

    reader = ClipReader(tmp_path)
    frames = [reader.next() for _ in range(4)]

    assert all(frame is not None for frame in frames)
    colors = [frame.getpixel((4, 4)) for frame in frames]
    assert colors[0][0] > 180 and colors[0][2] < 80
    assert colors[1][2] > 180 and colors[1][0] < 80
    assert colors[2] == colors[1]
    assert colors[3] == colors[1]

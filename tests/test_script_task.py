from pathlib import Path
import asyncio
import json

import pytest

from shorts import script_task


TASK = """# Подборка
## 1. Заголовок, описание и хэштеги
**Заголовок:** Проверка фильма
**Описание:** Описание и ссылка
**Хэштеги:** #Shorts
## 2. Озвучка — только текст для TTS
> Первая реплика. И второе предложение одной сцены!
>
> Вторая реплика.

**Примечание TTS:** эту ремарку в голос не добавлять.
## 3. Покадровая карта для автоматической сборки
### Сцена 1 — Вступление (0:00–0:05)
- **Озвучка:** «Первая реплика. И второе предложение одной сцены!»
- **Целевой кадр A:** Логотип фильма
- **Целевой кадр B:** Герой на официальном постере
- **Поиск RU:** `Герой фильма`
- **Где искать:** https://www.marvel.com/movies
- **Контроль:** Не показывать другого героя.
### Сцена 2 — Финал
- **Озвучка:** «Вторая реплика.»
- **Целевой кадр:** Карточка
- **Надпись:** «ФИНАЛ»
- **Резерв:** Своя графика
## 4. Обязательные правила
Хранить отдельно от озвучки.
"""


def test_structured_script_import_preserves_voice_order_and_scene_fields():
    assert script_task.marked(TASK)
    result = script_task.parse(TASK)
    assert result["lines"] == ["Первая реплика. И второе предложение одной сцены!", "Вторая реплика."]
    assert result["scenes"][0]["queries"] == ["Герой фильма"]
    assert result["scenes"][0]["targets"] == ["Логотип фильма", "Герой на официальном постере"]
    assert result["scenes"][0]["sources"] == ["https://www.marvel.com/movies"]
    assert result["scenes"][1]["label"] == "ФИНАЛ"
    assert result["hashtags"] == "#Shorts"


def test_structured_script_rejects_different_voice_in_scene():
    with pytest.raises(ValueError, match="различаются"):
        script_task.parse(TASK.replace("«Вторая реплика.»", "«Другая реплика.»"))


def test_only_exact_approved_media_can_be_rendered(tmp_path):
    plan = script_task.parse(TASK)
    files = [tmp_path / "one.jpg", tmp_path / "two.jpg"]
    for i, file in enumerate(files, 1):
        script_task.fallback_card(file, plan["scenes"][i - 1], i)
    assets = {str(i): {"file": file.name, "approved": script_task.fingerprint(file)}
              for i, file in enumerate(files, 1)}
    assert script_task.require_approved(tmp_path, plan, assets) == files
    Path(files[-1]).write_bytes(files[0].read_bytes())
    with pytest.raises(ValueError, match="изменился"):
        script_task.require_approved(tmp_path, plan, assets)
    assets["2"]["approved"] = script_task.fingerprint(files[1])
    with pytest.raises(ValueError, match="тот же материал"):
        script_task.require_approved(tmp_path, plan, assets)


def test_edit_keeps_numbered_scene_mapping():
    assert script_task.edited_lines("2. Новая вторая реплика.", ["Первая", "Вторая"]) == [
        "Первая", "Новая вторая реплика."]
    with pytest.raises(ValueError, match="номер"):
        script_task.edited_lines("2. Правка\n2. Повтор", ["Первая", "Вторая"])


def test_only_official_video_uploaders_are_accepted(monkeypatch):
    async def run(*args, **kwargs):
        return json.dumps({"entries": [
            {"title": "Official trailer", "id": "abcdeFGHijk", "uploader_id": "@fan", "duration": 60},
            {"id": "testTrailer", "channel_id": script_task.MARVEL_CHANNEL, "duration": 90},
            {"id": "anotherClip", "uploader_id": "@waltdisneystudios", "duration": 600},
        ]})

    monkeypatch.setattr(script_task.process, "run", run)
    assert asyncio.run(script_task.official_videos("test trailer")) == ["testTrailer"]


def test_motion_keeps_split_and_ab_targets_and_proposed_length():
    scene = {"targets": ["сплит-экран: слева Первый герой, справа Второй герой. Если нет — графика."],
             "heading": "Вступление (0:00–0:06)"}
    assert script_task.motion_targets(scene) == ("split", ["Первый герой", "Второй герой"])
    assert script_task.video_duration(scene, "Реплика") == 8
    assert script_task.motion_targets({"targets": ["Кадр A", "Кадр B"]}) == ("sequence", ["Кадр A", "Кадр B"])


def test_verified_studio_trailer_is_tried_before_youtube_search(monkeypatch):
    async def unavailable(*_):
        pytest.fail("direct video references must be available without search")

    monkeypatch.setattr(script_task, "official_videos", unavailable)

    async def first():
        return await anext(script_task.studio_candidates(
            ["Kang trailer"], "Канг в костюме из «Квантомании»", []))

    assert asyncio.run(first()) == "ZlNFpri-Y40"
    assert not script_task.is_quantumania_kang("Канг из «Локи»")
    assert script_task.youtube_id("https://youtu.be/ZlNFpri-Y40") == "ZlNFpri-Y40"
    assert not script_task.youtube_id("https://youtube.com.example.net/watch?v=ZlNFpri-Y40")


def test_split_keeps_verified_image_when_video_is_unavailable(monkeypatch, tmp_path):
    async def no_video(*args):
        return None

    async def press(path, target, used):
        if "Канг" in target:
            path.write_bytes(b"press-image")
            return {"id": "studio-press", "url": "https://press.disney.co.uk/gallery/test", "kind": "still"}
        return None

    async def ffmpeg(*args, **kwargs):
        assert any(str(arg).endswith("_0.jpg") for arg in args)
        assert any("hstack=inputs=2" in str(arg) for arg in args)
        Path(args[-1]).write_bytes(b"split")

    monkeypatch.setattr(script_task, "matching_clip", no_video)
    monkeypatch.setattr(script_task, "verified_target_still", press)
    monkeypatch.setattr(script_task.process, "run", ffmpeg)
    scene = {"targets": ["сплит-экран: слева Канг, справа Доктор Дум."], "queries": ["Канг", "Дум"],
             "control": "", "heading": "Сцена", "label": "", "fallback": ""}
    result = asyncio.run(script_task.find_motion(tmp_path / "split.mp4", scene, "Реплика.", 1, "", []))
    assert result[0]["kind"] == "still"
    assert (tmp_path / "split.mp4").read_bytes() == b"split"

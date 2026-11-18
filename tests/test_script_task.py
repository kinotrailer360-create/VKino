from contextlib import aclosing
from pathlib import Path
import asyncio
import json

import pytest

from shorts import imgsearch, script_task


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

    async def press(path, scene, target, queries, key, used):
        if "Канг" in target:
            path.write_bytes(b"press-image")
            return {"id": "studio-press", "url": "https://press.disney.co.uk/gallery/test", "kind": "still"}
        return None

    async def ffmpeg(*args, **kwargs):
        assert any(str(arg).endswith("_0.jpg") for arg in args)
        assert any("hstack=inputs=2" in str(arg) for arg in args)
        Path(args[-1]).write_bytes(b"split")

    monkeypatch.setattr(script_task, "matching_clip", no_video)
    monkeypatch.setattr(script_task, "target_still", press)
    monkeypatch.setattr(script_task.process, "run", ffmpeg)
    scene = {"targets": ["сплит-экран: слева Канг, справа Доктор Дум."], "queries": ["Канг", "Дум"],
             "control": "", "heading": "Сцена", "label": "", "fallback": ""}
    result = asyncio.run(script_task.find_motion(tmp_path / "split.mp4", scene, "Реплика.", 1, "", []))
    assert result[0]["kind"] == "still"
    assert (tmp_path / "split.mp4").read_bytes() == b"split"


def test_yandex_image_result_preserves_original_and_source():
    import html

    result = {"origUrl": script_task.KANG_LOGO, "origWidth": 2000, "snippet": {"url": script_task.KANG_LOGO_PAGE}}
    state = {"initialState": {"serpList": {"items": {"keys": ["1"], "entities": {"1": result}}}}}
    page = f'<div data-state="{html.escape(json.dumps(state), quote=True)}"></div>'
    assert imgsearch.yandex_results(page, 2) == [{"image": script_task.KANG_LOGO, "source": script_task.KANG_LOGO_PAGE}]


def test_sequence_places_official_logo_before_illustrated_timeline(monkeypatch, tmp_path):
    async def no_video(*args):
        pytest.fail("the logo and original timeline should not trigger trailer searches")

    async def logo(*args):
        from PIL import Image
        assert args[1] == script_task.KANG_LOGO
        return Image.new("RGB", (600, 600))

    async def ffmpeg(*args, **kwargs):
        assert any("concat=n=2" in str(arg) for arg in args)
        assert Path(args[args.index("-i") + 1]).is_file()
        assert Path(args[args.index("-i", args.index("-i") + 1) + 1]).is_file()
        Path(args[-1]).write_bytes(b"sequence")

    monkeypatch.setattr(script_task, "matching_clip", no_video)
    monkeypatch.setattr(script_task, "opened_still", logo)
    monkeypatch.setattr(script_task.process, "run", ffmpeg)
    scene = {"targets": ["официальный логотип Avengers: The Kang Dynasty",
                         "авторская инфографика: `ПЛАН` → `СЕРЕДИНА` → `ДУМ`"],
             "queries": ["Avengers Kang Dynasty", "Династия Канга"], "control": "",
             "heading": "Сцена 3", "label": "", "fallback": ""}
    path = tmp_path / "sequence.mp4"
    result = asyncio.run(script_task.find_motion(path, scene, "Реплика.", 3, "", []))
    assert result == [{**script_task.still_ref(script_task.KANG_LOGO, script_task.KANG_LOGO_PAGE), "animated": True}]
    assert path.read_bytes() == b"sequence"


def test_known_doom_and_feige_photos_are_scoped_to_requested_subjects():
    feige = "официальное фото Кевина Файги на Comic-Con"
    doom = "официальный образ Доктора Дума из «Мстителей: Судный день»"
    assert script_task.wants_still(feige)
    assert script_task.studio_still(feige) == (script_task.FEIGE_PHOTO, script_task.FEIGE_PHOTO_PAGE)
    assert script_task.studio_still(doom) == (script_task.DOOM_PRESS_IMAGE, script_task.DOOM_PRESS)
    assert script_task.studio_still("драка Доктора Дума в «Мстителях: Судный день»") is None
    assert script_task.studio_still("официальное фото режиссёров Руссо") is None
    assert "Gage Skidmore" in script_task.still_ref(*script_task.studio_still(feige))["credit"]


def test_storyboard_keeps_planned_card_and_uses_distinct_confirmed_sources():
    kang = "официальный трейлер «Квантомании», где виден Канг, затем собственная карточка «Планы»"
    layout, parts = script_task.motion_targets({"targets": [kang]})
    assert layout == "sequence"
    assert script_task.studio_still(parts[0])[0] == script_task.KANG_THRONE
    assert script_task.author_graphic(parts[1])
    assert script_task.author_graphic("короткий переход к авторской карточке «Планы»")
    assert script_task.studio_still("архивный пресс-портрет Джонатана Мэйджорса")[0] == script_task.MAJORS_PORTRAIT
    assert script_task.studio_still("Роберт Дауни на презентации Marvel в роли Дума")[0] == script_task.DOWNEY_REVEAL_IMAGE
    assert script_task.studio_still("официальный логотип Avengers: Doomsday")[0] == script_task.DOOMSDAY_LOGO

    final = "чистая авторская финальная карточка: слева «Канг», справа «Дум», в центре «Кто круче?»"
    assert script_task.motion_targets({"targets": [final]}) == ("single", [final])
    assert script_task.card_text("КАНГ ИЛИ ДУМ? 👇") == "КАНГ ИЛИ ДУМ?"
    assert script_task.card_text("ROBERT DOWNEY JR. → DOCTOR DOOM") == "ROBERT DOWNEY JR. — DOCTOR DOOM"


def test_structured_trailer_uses_official_rutube_before_broad_search(monkeypatch):
    video_id = "abcdef0123456789" * 2
    url = f"https://rutube.ru/video/{video_id}/"

    async def rutube(titles, *, official_only=False):
        assert "Квантомания" in titles[0] and official_only
        return {"url": url}

    monkeypatch.setattr(script_task.clips, "rutube_trailer", rutube)

    async def first_two():
        async with aclosing(
            script_task.trailer_sources([], "Канг из «Квантомании»", [])
        ) as pool:
            return [await anext(pool), await anext(pool)]

    result = asyncio.run(first_two())
    assert result[0][0] == script_task.QUANTUMANIA_TRAILERS[0]
    assert result[1] == ("rutube_" + video_id, url)
    assert not script_task.wants_still("официальное видео/фото с презентации")
    assert not script_task.rutube_id(f"https://rutube.ru/video/private/{video_id}/")


def test_approved_still_camera_moves_without_changing_size(tmp_path):
    from PIL import Image, ImageDraw
    from shorts.make_lore import ApprovedStill

    path = tmp_path / "frame.jpg"
    with Image.new("RGB", (540, 960), "grey") as source:
        ImageDraw.Draw(source).rectangle((100, 100, 300, 300), fill="red")
        source.save(path)

    scene = ApprovedStill(path)
    with scene.frame(0, 4) as start, scene.frame(4, 4) as end:
        assert end.size == start.size
        assert start.tobytes() != end.tobytes()

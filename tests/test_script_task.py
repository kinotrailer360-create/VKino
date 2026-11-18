from pathlib import Path

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

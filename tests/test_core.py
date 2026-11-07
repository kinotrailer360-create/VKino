"""Регрессионные тесты чистых функций: то, что уже ломалось, не должно сломаться снова.
Запуск: python -m pytest -q"""

import random

from shorts import lore, music
from shorts.daily import _bad_part
from shorts.lumean import _pick_cuts
from shorts.youtube_upload import no_links


def _heard(text: str) -> list[dict]:
    return [{"w": w} for w in text.split()]


def test_music_multiverse_is_not_cartoon() -> None:
    # «Мультивселенная» включала детскую музыку с челестой в ролике о «Мстителях»
    assert music.mood_for("Локи держит на себе всю Мультивселенную") == "action"
    assert music.mood_for("Новый мультфильм Pixar") == "animation"
    assert music.mood_for("Вся «Дюна» за 60 секунд") == "scifi"


def test_voice_cuts_follow_text_length() -> None:
    # паузы внутри фраз (на тире) не должны становиться границами фраз
    ok = 0
    for seed in range(100):
        rnd = random.Random(seed)
        weights = [rnd.randint(30, 110) for _ in range(8)]
        t, silences, truth = 0.0, [], []
        for k, chars in enumerate(weights):
            dur = chars * 0.07 * rnd.uniform(0.85, 1.15)
            silences.append((t + dur * 0.3, t + dur * 0.3 + rnd.uniform(0.3, 0.7)))
            t += dur
            if k < len(weights) - 1:
                gap = rnd.uniform(0.9, 1.1)
                silences.append((t, t + gap))
                truth.append((t, t + gap))
                t += gap
        ok += _pick_cuts(sorted(silences), weights, t, 1.0) == truth
    assert ok >= 90


def test_phrase_check_catches_missing_words() -> None:
    text = "Третий — «Дэдпул и Росомаха». Он открыл дверь для героев из старых вселенных Fox."
    assert _bad_part(text, _heard("Третий Дэдпул и Росомаха он открыл дверь для героев из старых вселенных фокс")) < 2
    assert _bad_part(text, _heard("Громовержцы")) >= 2


def test_youtube_description_has_no_links() -> None:
    out = no_links("Текст\n📺 YouTube: https://youtube.com/@k\n✈️ Telegram: https://t.me/KinoMir360")
    assert "https://" not in out and "youtube.com" not in out and "t.me/KinoMir360" in out


def test_own_script_table_is_read_as_is() -> None:
    script = ("**Заголовок:** 5 фильмов #shorts\n\n| Время | Голос | [ON SCREEN] |\n|---|---|---|\n"
              "| 0–3 | Без этих пяти фильмов ты не поймёшь «Судный день». | Текст «5 ФИЛЬМОВ» |\n"
              "| 3–6 | Пятый — «Мстители: Финал». | плашка «2019» |\n")
    parsed = lore.parse_script(script)
    assert parsed["lines"][0].startswith("Без этих пяти")
    assert parsed["title"] == "5 фильмов"
    assert lore._quoted(" ".join(parsed["lines"] + parsed["screens"])) == ["Судный день", "Мстители: Финал"]


def test_numbers_for_voice() -> None:
    from shorts import pronounce as p

    def say(t: str) -> str:
        return p.plain_russian(p.dates(t))

    assert say("1-й сезон") == "первый сезон"
    assert say("в 90-х") == "в девяностых"
    assert say("в 3-м сезоне") == "в третьем сезоне"
    assert say("3-я часть") == "третья часть"
    assert say("12 млн долларов") == "двенадцать миллионов долларов"
    assert say("$75 млн") == "семьдесят пять миллионов долларов"
    assert say("1,5 млрд рублей") == "полтора миллиарда рублей"
    assert say("2 тыс зрителей") == "две тысячи зрителей"
    assert say("75%") == "семьдесят пять процентов"
    assert say("Оценка 8/10") == "Оценка восемь из десяти"
    assert say("7,5/10") == "семь с половиной из десяти"


def test_post_media_only_from_focused_posts() -> None:
    from shorts.postmedia import _focused, _is_digest, _title_words

    names = [_title_words("I Play Rocky")]
    digest = ("🎬 Главное за день:\n\n🔹 «Мстители: Судный день» перенесли.\n\n🔹 Вышел трейлер «I Play Rocky».\n\n"
              "🔹 «Дюна 3» — первые кадры.")
    single = "🥊 «I Play Rocky» выйдет в мае: фильм о том, как Сталлоне пробивал «Рокки». Режиссёр «Зелёной книги»."
    other_first = ("Marvel перенесли «Мстители: Судный день» на декабрь, а «Дюна 3» получила дату. Ещё стало "
                   "известно, что «I Play Rocky» выйдет в мае.")
    assert _is_digest(digest)
    assert not _focused(digest, names)
    assert _focused(single, names)
    assert not _focused(other_first, names)


def test_diary_year_stats_and_card() -> None:
    from shorts import diary

    rows = [{"title": "Интерстеллар", "score": 10, "genres": "фантастика, драма", "year_released": 2014},
            {"title": "Дюна: Часть вторая", "score": 9, "genres": "фантастика", "year_released": 2024},
            {"title": "Без оценки", "score": 0, "genres": "драма", "year_released": 2010}]
    st = diary.stats(rows)
    assert st["count"] == 3 and st["avg"] == 9.5 and st["genre"] in ("фантастика", "драма")
    assert [r["title"] for r in st["best"]] == ["Интерстеллар", "Дюна: Часть вторая"]
    png = diary.year_card("Зритель", 2026, st, [None, None], "t.me/VKino_360bot")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_kinodl_clues_and_answers() -> None:
    from shorts import kinodl

    film = {"name": "Интерстеллар", "enName": "Interstellar", "year": 2014, "genres": [{"name": "фантастика"}],
            "persons": [{"name": "Кристофер Нолан", "enProfession": "director"}],
            "shortDescription": "Интерстеллар — экспедиция через червоточину."}
    clues = kinodl.clues(film)
    assert len(clues) == 5 and "Интерстеллар" not in clues[4]
    assert kinodl.is_correct("интерстелар", film) and kinodl.is_correct("Interstellar", film)
    assert not kinodl.is_correct("Начало", film)
    assert kinodl.share_line("06.11", 3, True).endswith("3/6")


def test_review_score_and_montage_table() -> None:
    from shorts import lore

    assert lore.review_score(["Начало", "Мой вердикт: крепкая восьмёрка из десяти."]) == {"line": 1, "value": "8"}
    assert lore.review_score(["Оценка 7,5/10"]) == {"line": 0, "value": "7.5"}
    assert lore.review_score(["Без оценки"]) is None
    plan = ("| Время | Что показываем | Текст на экране |\n|---|---|---|\n"
            "| 0–4 сек. | Самый эффектный кадр | ЗАБУДЬТЕ ПРО ЙОВОВИЧ! |\n")
    assert lore.parse_script(plan)["screens"] == []  # план монтажа не читается вслух


def test_own_script_main_film_any_case() -> None:
    import asyncio

    from shorts import lore

    async def fake(key, name, n=3, tv=False, year=None, meta=None):
        db = {"Обитель зла": "2026", "Варвар": "2022"}
        if name in db and meta is not None:
            meta.update(kind="movie", id=len(name), title=name, poster="P", year=db[name])
            return ["B" + name]
        return []

    real = lore._film_backdrops
    lore._film_backdrops = fake
    try:
        text = ("Забудьте всё об «Обители зла» с Миллой.\nЗа камерой режиссёр «Варвар».\n"
                "Мой вердикт: 8/10. А вы посмотрели новую «Обитель зла»?")
        script = asyncio.run(lore.write_own(text, "k"))
    finally:
        lore._film_backdrops = real
    assert script["trailer_film"]["title"] == "Обитель зла"  # не «Варвар», упомянутый мимоходом
    assert lore._stem_key("Обители зла") == lore._stem_key("Обитель зла")


def test_fact_check_must_not_change_story() -> None:
    from shorts.ai import same_story

    draft = "🥋 БРЭДЛИ КУПЕР СНИМЕТ ФИЛЬМ ПО «КОБРЕ КАЙ»! Netflix и Sony готовят фильм, режиссёр — Брэдли Купер."
    other = "🎬 НАТАЛИ ДАЙЕР И ЭЛАЙДЖА ВУД СЫГРАЮТ В СИКВЕЛЕ «1408»! К Джону Кьюсаку присоединятся новые актёры."
    fixed = "🥋 БРЭДЛИ КУПЕР СНИМЕТ ФИЛЬМ ПО «КОБРЕ КАЙ»! По слухам, Netflix готовит фильм, режиссёр — Брэдли Купер."
    assert not same_story(draft, other)
    assert same_story(draft, fixed)


def test_outro_and_dune_stress():
    from shorts import daily, pronounce
    assert daily.with_outro("Сохраняй, чтобы освежить память.").count("охран") == 1
    assert "Сохрани и подпишись" in daily.with_outro("Премьера в декабре.")
    said = pronounce.apply("находят фременов, КиноМир")
    assert "фре́менов" in said and "Киноми́р" in said


def test_recap_title():
    from shorts.lore import recap_title
    text = ["Вспоминаем, чем закончились части, перед выходом третьей части."]
    assert recap_title(["Дюна", "Дюна: Часть вторая"], text) == "«Дюна»: что было в прошлых частях — перед третьей частью"
    assert recap_title(["Дюна"], ["Обычная рецензия"]) == ""

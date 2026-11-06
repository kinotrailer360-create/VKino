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

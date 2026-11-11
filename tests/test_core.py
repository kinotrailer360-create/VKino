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


def test_wwk_body_parses_title_and_lines():
    from shorts.lore import parse_script
    body = "**Заголовок:** Что мы знаем о Мстителях\nЧто мы знаем о «Мстители: Судный день» (2026)?\nДавайте разберём."
    got = parse_script(body)
    assert got["title"] == "Что мы знаем о Мстителях"
    assert got["lines"][0].startswith("Что мы знаем") and len(got["lines"]) == 2


def test_transitions_render():
    from PIL import Image

    from shorts.make_lore import TRANSITIONS, Transitions
    from shorts.make_short import H, W
    a, b = Image.new("RGB", (W, H), "red"), Image.new("RGB", (W, H), "blue")
    tr = Transitions()
    for kind in set(TRANSITIONS):
        for p in (0.0, 0.5, 1.0):
            assert tr.apply(kind, a, b, p).size == (W, H)


def test_whatweknow_drops_stale_news(monkeypatch):
    import asyncio

    from shorts import ai

    async def fake(*a, **k):
        return {"title": "Т", "about": "о «Мстителях: Судный день»", "premiere": "2026-12-18", "year": 2026,
                "good": [{"text": "Руссо снова режиссёры", "date": "2024-07-27"},
                         {"text": "Реакции с показа восторженные", "date": "2026-11-01"},
                         {"text": "Хронометраж почти три часа", "date": "2026-10-28"}],
                "bad": [{"text": "По слухам, погибнут два героя", "date": "2026-10-30"}],
                "shock": [{"text": "Инсайдер пишет о сцене после титров с Росомахой", "date": "2026-11-03"}]}
    monkeypatch.setattr(ai, "_post_json", fake)
    got = asyncio.run(ai.whatweknow_script("k", "Мстители: Судный день", "07.11.2026"))
    text = " ".join(got["lines"])
    assert got["stale"] == 1 and "Руссо" not in text
    assert got["lines"][0] == "Что мы знаем о «Мстителях: Судный день» (2026) за 41 день до премьеры?"
    assert "Но радоваться рано." in got["lines"] and got["lines"][-1].startswith("Инсайдер")
    assert any("телеграм" in ln for ln in got["lines"])


def test_compact_list_keeps_every_film():
    import os
    for name, value in {"BOT_TOKEN": "123456:TEST", "ADMIN_IDS": "1", "KINOPOISK_TOKEN": "test",
                        "DATABASE_URL": "postgresql://test@localhost/test"}.items():
        os.environ.setdefault(name, value)
    import bot
    films = [f"{k}️⃣ «Фильм номер {k}» (2026) ⭐ 7.{k}\n" + "Очень длинное описание фильма о героях и событиях " * 3
             for k in range(1, 9)]
    post = "🎬 НОВИНКИ НЕДЕЛИ\n🎬 Подборка от KinoMir360\n\nВводная фраза про вечер.\n\n" + "\n\n".join(films) \
        + "\n\n❤️ — смотрю 🔥 — в список\n#кино #новинки"
    out = bot._compact_list(post, 900)
    assert len(out) <= 900
    assert all(f"«Фильм номер {k}»" in out for k in range(1, 9))
    assert "#кино #новинки" in out


def test_same_name_rejects_other_films():
    from shorts.daily import _same_name
    assert not _same_name("Черные голуби", {"name": "Черные паруса", "enName": "Black Sails"})
    assert not _same_name("Что, если любовь", {"name": "Что, если...?", "enName": "What If...?"})
    assert _same_name("Black Doves", {"name": "Чёрные голуби", "enName": "Black Doves"})
    assert _same_name("Черные голуби", {"name": "Чёрные голуби"})


def test_tmdb_card_looks_like_kinopoisk():
    from shorts import daily, tmdb
    card = tmdb._kp_like("movie", {"id": 7, "title": "Что, если любовь", "original_title": "Possible Love",
                                   "release_date": "2025-11-05", "vote_average": 7.46, "vote_count": 120,
                                   "genres": [{"name": "драма"}], "poster_path": "/p.jpg",
                                   "credits": {"cast": [{"name": "Чо Ин-сон"}], "crew": []}})
    assert card["id"] == "tmdb:movie:7" and card["year"] == 2025
    assert daily._title(card) == "Что, если любовь" and daily._actors(card) == ["Чо Ин-сон"]
    assert daily._best_rating(card) == (7.5, "TMDB") and daily._genres(card) == ["драма"]


def test_tmdb_rating_preferred_when_enough_votes():
    from shorts import daily
    film = {"rating": {"kp": 8.4, "imdb": 8.1, "tmdb": 7.9}, "votes": {"kp": 900000, "tmdb": 5000}}
    assert daily._best_rating(film) == (7.9, "TMDB")
    few = {"rating": {"kp": 8.4, "imdb": 8.1, "tmdb": 9.5}, "votes": {"kp": 900000, "tmdb": 4}}
    assert daily._best_rating(few) == (8.4, "КП")  # мало голосов на TMDB — как раньше


def test_amazon_stress():
    from shorts import pronounce
    assert "Амазо́н" in pronounce.apply("премьера на Amazon")
    assert "Амазо́не" in pronounce.apply("вышел на Амазоне")


def test_netsafe_blocks_internal_addresses():
    import asyncio

    import pytest

    from shorts import netsafe
    for url in ("http://127.0.0.1/", "http://169.254.169.254/latest", "http://10.0.0.5/x", "file:///etc/passwd"):
        with pytest.raises(netsafe.UnsafeURL):
            asyncio.run(netsafe.check_url(url))


def test_budget_reserves_before_call_and_blocks_at_limit():
    import asyncio

    import pytest

    from shorts import budget

    class Conn:  # имитация PostgreSQL: строки ai_usage в памяти
        def __init__(self, rows):
            self.rows = rows

        async def execute(self, sql, *a):
            if sql.startswith("UPDATE ai_usage SET status = 'settled'"):
                self.rows[a[0]]["actual"], self.rows[a[0]]["status"] = a[1], "settled"
            elif sql.startswith("UPDATE ai_usage SET status = 'failed'"):
                self.rows[a[0]]["status"] = "failed"

        async def fetchval(self, sql, *a):
            if sql.startswith("SELECT COALESCE"):
                return sum((r["actual"] if r["actual"] is not None else r["est"]) for r in self.rows.values()
                           if r["pool"] == a[0] and r["status"] in ("reserved", "settled"))
            rid = len(self.rows) + 1
            self.rows[rid] = {"pool": a[0], "est": a[3], "actual": None, "status": "reserved"}
            return rid

        def transaction(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Pool(Conn):
        def acquire(self):
            return self

    rows = {}
    pool = Pool(rows)
    budget.attach(lambda: pool)
    budget.LIMITS["ai"], budget.LIMITS["tts"] = 1.0, 1.0
    budget.on_warning = None

    async def run():
        async with budget.guard("ai", "gemini", 0.5) as r:
            await r.settle(0.7)  # факт дороже оценки — учитывается факт
        with pytest.raises(budget.BudgetExceeded):
            async with budget.guard("ai", "image", 0.45):
                pass  # 0.7 + 0.45 > 1.0 — запрос не делается
        async with budget.guard("ai", "free", 0.0):
            pass  # бесплатное не блокируется
        async with budget.guard("tts", "tts", 0.9):
            pass  # отдельный кошелёк озвучки
        with pytest.raises(RuntimeError):
            async with budget.guard("ai", "gemini", 0.1):
                raise RuntimeError("сбой")  # резерв снят
        assert await budget.spent("ai") == pytest.approx(0.7)
    try:
        asyncio.run(run())
    finally:
        budget.attach(None)


def test_safe_resolver_blocks_rebinding():
    import asyncio

    import pytest

    from shorts import netsafe

    class Flip:  # DNS-перепривязка: первый ответ публичный, второй — внутренний
        def __init__(self):
            self.n = 0

        async def resolve(self, host, port=0, family=0):
            self.n += 1
            ip = "93.184.216.34" if self.n == 1 else "127.0.0.1"
            return [{"hostname": host, "host": ip, "port": port, "family": family, "proto": 0, "flags": 0}]

        async def close(self):
            pass

    async def run():
        r = netsafe.SafeResolver()
        r._inner = Flip()
        assert (await r.resolve("evil.example"))[0]["host"] == "93.184.216.34"
        with pytest.raises(OSError):
            await r.resolve("evil.example")  # соединение по второму ответу не состоится
    asyncio.run(run())


def test_fetch_checks_every_redirect(monkeypatch):
    import asyncio

    import pytest

    from shorts import netsafe

    class Resp:
        def __init__(self, status, location=None):
            self.status, self.headers = status, ({"Location": location} if location else {})
            self.url = __import__("yarl").URL("https://public.example/a")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Session:
        def __init__(self):
            self.calls = []

        def get(self, url, allow_redirects=True):
            assert allow_redirects is False
            self.calls.append(url)
            return Resp(302, "http://169.254.169.254/latest/meta-data")

    async def ok(url):
        if "169.254" in url:
            raise netsafe.UnsafeURL("внутренний адрес")
    monkeypatch.setattr(netsafe, "check_url", ok)
    s = Session()
    with pytest.raises(netsafe.UnsafeURL):
        asyncio.run(netsafe.fetch(s, "https://public.example/a"))
    assert s.calls == ["https://public.example/a"]  # во внутреннюю сеть запрос так и не ушёл


def test_no_builtin_admin():
    import pathlib
    import re
    src = pathlib.Path(__file__).resolve().parent.parent.joinpath("bot.py").read_text(encoding="utf-8")
    assert re.search(r'os\.getenv\("ADMIN_IDS", ""\)', src)  # пусто по умолчанию — админов нет


def test_migrations_numbered_and_checked():
    import pytest

    from shorts import migrations
    nums = [m[0] for m in migrations.MIGRATIONS]
    assert nums == sorted(set(nums)) and nums[0] == 1
    applied = {v: migrations.checksum(sql, step) for v, _, sql, step in migrations.MIGRATIONS[:3]}
    assert migrations.pending(applied) == nums[3:]  # применённые второй раз не идут
    applied[2] = "изменённая"
    with pytest.raises(migrations.MigrationError):
        migrations.pending(applied)


def test_fsm_storage_rejects_non_json():
    import asyncio

    import pytest
    from aiogram.fsm.storage.base import StorageKey

    from shorts import fsm_pg

    class Pool:
        async def execute(self, *a):
            pass
    s = fsm_pg.PgStorage(lambda: Pool())
    with pytest.raises(fsm_pg.FSMDataError):
        asyncio.run(s.set_data(StorageKey(bot_id=1, chat_id=1, user_id=1), {"img": b"\x89PNG"}))


SERVER_005 = "47c3808c9f7e2ad57b115535951869ce3cdd48caa9d5c3dc12eb135b5372c203"


def test_history_key_is_hash_not_long_value():
    from shorts import migrations
    sql = next(m[2] for m in migrations.MIGRATIONS if m[0] == 7)
    assert "PRIMARY KEY (kind, vhash)" in sql and "md5(value)" in sql  # длинный сюжет не влезал в индекс
    # 005 — ровно в том виде, в каком применена на сервере (её контрольная сумма записана в schema_version)
    v5 = next(m for m in migrations.MIGRATIONS if m[0] == 5)
    assert migrations.checksum(v5[2], v5[3]) == SERVER_005


def test_fact_check_gets_only_related_materials():
    from shorts import ai
    rules = "Правила. " * 200
    other = "\n\n".join(f"{i}. [t.me/x]\nНовость про «Бэтмен» и Паттинсона номер {i}. " * 3 for i in range(30))
    ours = "\n\n31. [t.me/y]\nВильнёв снимет «Дюна 3» с Зендеей, премьера в 2026."
    task = rules + "\n\n" + other + ours
    got = ai._relevant_task(task, "🏜 «ДЮНА 3»: Вильнёв и Зендея возвращаются")
    assert "Вильнёв снимет «Дюна 3»" in got and "Бэтмен" not in got
    assert len(got) < len(task) // 2
    assert ai._clip("слово " * 200, 50).endswith("…") and len(ai._clip("слово " * 200, 50)) <= 51


def test_season_number_is_ordinal():
    from shorts import pronounce
    assert pronounce.numbers("4 сезон на HBO") == "четвёртый сезон на HBO"
    assert pronounce.numbers("во 2 сезоне") == "во втором сезоне"
    assert pronounce.numbers("3 часть") == "третья часть"
    assert pronounce.numbers("2 сезона подряд") == "2 сезона подряд"  # «два сезона» — не трогаем


def test_title_numbers_and_wednesday_stress():
    from shorts import pronounce
    assert pronounce.numbers("сиквел фильма «1408»") == "сиквел фильма «четырнадцать ноль восемь»"
    assert pronounce.numbers("в 1408 году") == "в 1408 году"  # год — не название
    assert pronounce.numbers("14080 долларов") == "14080 долларов"
    assert pronounce.apply("выйдет в среду") == "выйдет в сре́ду"


def test_spoken_form_is_cached(tmp_path, monkeypatch):
    """Переозвучка: неизменная фраза даёт тот же текст для голоса без нового запроса к ИИ."""
    import asyncio
    from shorts import ai, daily
    monkeypatch.setattr(daily, "SPOKEN_CACHE", tmp_path / "spoken.json")
    calls = []

    async def fake(texts, key=None, premium=False):
        calls.append(list(texts))
        return [t + " (ИИ)" for t in texts]

    async def no_accent(texts):
        return texts

    monkeypatch.setattr(ai, "spoken_form", fake)
    monkeypatch.setattr(daily, "_accent", no_accent)
    first = asyncio.run(daily.spoken_lines(["Привет", "Пока"], careful=True))
    second = asyncio.run(daily.spoken_lines(["Привет", "Пока", "Новая"], careful=True))
    assert second[:2] == first
    assert calls == [["Привет", "Пока"], ["Новая"]]


def test_project_revoice_only_fixed_phrase(tmp_path, monkeypatch):
    """«🎙 Только звук»: исправленная фраза встаёт на своё место, остальные и длина ролика — как были."""
    import asyncio
    import json
    import shutil
    import subprocess

    import pytest
    if not shutil.which("ffmpeg"):
        pytest.skip("нет ffmpeg")
    from shorts import daily, project

    def ff(*a):
        subprocess.run(["ffmpeg", "-v", "error", "-y", *a], check=True)

    for i, f in enumerate([440, 550, 660]):
        ff("-f", "lavfi", "-i", f"sine=f={f}:d=2", "-ar", "48000", str(tmp_path / f"p{i}.wav"))
    ff("-i", str(tmp_path / "p0.wav"), "-i", str(tmp_path / "p1.wav"), "-i", str(tmp_path / "p2.wav"),
       "-filter_complex", "[0:a]apad=pad_dur=0.2[a];[1:a]apad=pad_dur=0.2[b];[2:a]apad=pad_dur=0.2[c];"
       "[a][b][c]concat=n=3:v=0:a=1", "-ar", "48000", str(tmp_path / "voice.wav"))
    ff("-f", "lavfi", "-i", "sine=f=100:d=10", "-ar", "48000", str(tmp_path / "music.mp3"))
    ff("-f", "lavfi", "-i", "color=c=red:s=160x120:d=8:r=25", "-i", str(tmp_path / "voice.wav"), "-shortest",
       "-c:v", "libx264", "-c:a", "aac", str(tmp_path / "short.mp4"))
    (tmp_path / "short.json").write_text(json.dumps({"music": "music.mp3", "music_db": -14, "sfx": []}))
    voice = {"file": "voice.wav", "lines": ["раз", "два", "три"], "prepared": False, "careful": False,
             "pause": None, "starts": [0, 2.2, 4.4], "speech": [2, 2, 2]}
    project.save(tmp_path, "short", "shorts.make_short", "short.json", "short.mp4", voice, {})

    async def spoken(lines, careful=False):
        return [x + "!" if x == "два" else x for x in lines]

    class Key:
        def __init__(self, text):
            self.text = text

        def exists(self):
            return self.text != "два!"

    async def synth(lines, work, pause=None, careful=False, prepared=False):
        work.mkdir(parents=True, exist_ok=True)
        for k, _ in enumerate(lines):
            ff("-f", "lavfi", "-i", "sine=f=1000:d=2.4", "-ar", "44100", str(work / f"voice_{k}.mp3"))

    monkeypatch.setattr(daily, "spoken_lines", spoken)
    monkeypatch.setattr(daily, "_voice_key", lambda t, p: Key(t))
    monkeypatch.setattr(daily, "synthesize", synth)
    before = asyncio.run(daily._duration(tmp_path / "short.mp4"))
    video, fixed, notes = asyncio.run(project.revoice(tmp_path))
    assert fixed == [(1, 2.2, "два")] and notes == []
    assert abs(asyncio.run(daily._duration(video)) - before) < 0.1  # длина ролика та же
    assert abs(asyncio.run(daily._duration(tmp_path / "voice.wav")) - 6.6) < 0.1  # фраза ужата в своё место


def test_oconnor_pronunciation():
    from shorts import pronounce
    assert pronounce.apply("Джоша О'Коннора") == "Джоша Око́ннора"
    assert pronounce.apply("О Коннор умеет") == "Око́ннор умеет"
    assert pronounce.apply("о Конноре думаю") == "о Конноре думаю"  # «про Коннора» — не трогаем
    assert pronounce.apply("Коннор Макгрегор") == "Коннор Макгрегор"


def test_weekly_blocks_build_fix_and_assemble(tmp_path, monkeypatch):
    """Выпуск по кускам: вступление, новость, финал собираются отдельно; «🎙 Звук» меняет только звук куска
    (картинка та же); склейка даёт выпуск с таймкодами и звуком по кускам."""
    import asyncio
    import shutil
    import subprocess

    import pytest
    if not shutil.which("ffmpeg"):
        pytest.skip("нет ffmpeg")
    from PIL import Image

    from shorts import daily, project, weekly

    async def synth(lines, work, pause=None, careful=False, prepared=False):
        work.mkdir(parents=True, exist_ok=True)
        starts, t = [], 0.0
        for _ in lines:
            starts.append(t)
            t += 1.2
        voice = work / "voice.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=f=440:d={t:.1f}", "-ar", "48000",
                        str(voice)], check=True)
        if prepared:
            for k, _ in enumerate(lines):
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=900:d=1", str(work / f"voice_{k}.mp3")],
                               check=True)
        daily.last_voice = {"file": "voice.wav", "lines": list(lines), "prepared": prepared, "careful": careful,
                            "pause": pause, "starts": starts, "speech": [1.0] * len(lines)}
        daily.voice_cache_hits = 0
        return voice, starts, True, [{"speech": 1.0, "words": [], "text": x} for x in lines]

    async def spoken(lines, careful=False):
        return [x + "!" if x == "Фраза два." else x for x in lines]

    class Key:
        def __init__(self, text):
            self.text = text

        def exists(self):
            return self.text != "Фраза два.!"

    monkeypatch.setattr(daily, "synthesize", synth)
    monkeypatch.setattr(daily, "spoken_lines", spoken)
    monkeypatch.setattr(daily, "_voice_key", lambda t, p: Key(t))
    monkeypatch.setattr(weekly, "W", 320)
    monkeypatch.setattr(weekly, "H", 180)
    monkeypatch.setattr(weekly, "INTRO_CLIP", tmp_path / "none.mp4")
    monkeypatch.setattr(weekly, "OUTRO_CLIP", tmp_path / "none.mp4")
    img = Image.new("RGB", (640, 360), (90, 30, 30))
    script = {"intro": ["Привет."], "outro": ["Пока."], "title": "T", "description": "D", "tags": "",
              "stories": [{"headline": "НОВОСТЬ", "lines": ["Фраза один.", "Фраза два."], "mourning": False,
                           "graphics": [], "money": {}}]}
    music = weekly.ASSETS / "music.mp3"

    async def run():
        blocks = [await weekly.build_intro(tmp_path, script, [img], img, "1–2 ноября", music),
                  await weekly.build_story(tmp_path, script["stories"][0], 1, 1, [img], img, "", music),
                  await weekly.build_outro(tmp_path, script, img, music)]
        story = blocks[1]
        before = (tmp_path / story["pieces"][1]["segment"]).stat().st_size
        fixed = await weekly.revoice_block(tmp_path, story)
        assert [x[1] for x in fixed] == ["Фраза два."] and fixed[0][0] > weekly.CARD_SEC  # время — внутри куска
        assert (tmp_path / story["pieces"][1]["segment"]).stat().st_size != before
        with pytest.raises(project.NothingToFix):
            monkeypatch.setattr(daily, "_voice_key", lambda t, p: type("K", (), {"exists": lambda s: True})())
            await weekly.revoice_block(tmp_path, story)
        ep = await weekly.assemble(tmp_path, script, blocks, "1–2 ноября", img)
        length = await weekly._duration(ep.video)
        assert abs(length - sum(b["length"] for b in blocks)) < 0.5
        assert "Вступление" in ep.seo["description"] and ep.seo["cuts"].count("НОВОСТЬ") == 1

    asyncio.run(run())


def test_weekly_episode_single_story_allowed():
    """«✍️ Переписать» просит у ИИ одну новость — одна новость в ответе не считается браком."""
    import inspect
    from shorts import ai
    assert "min(3, n)" in inspect.getsource(ai.write_weekly_episode)


def _fake_session(responses, calls):
    """Подмена aiohttp.ClientSession: отдаёт заранее заданные ответы (статус, json, заголовки) и пишет запросы."""
    class Resp:
        def __init__(self, status, data, headers):
            self.status, self._data, self.headers = status, data, headers

        async def json(self, content_type=None):
            return self._data

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            calls.append(json)
            status, data, hdrs = responses.pop(0)
            return Resp(status, data, hdrs)

    return Session


def test_reserve_429_too_big_prompt_is_shrunk_once(monkeypatch):
    """Qwen 429 «Limit 6000, Requested 8000» — задание ужимается под лимит и уходит один раз, без серии повторов."""
    import asyncio
    from shorts import ai
    calls = []
    ok = {"choices": [{"message": {"content": '{"a": 1}'}}], "usage": {"prompt_tokens": 5000, "completion_tokens": 10}}
    too_big = {"error": {"message": "Rate limit reached for model on tokens per minute (TPM): Limit 6000, "
                                    "Requested 8000, please try again in 1.2s"}}
    monkeypatch.setattr(ai.aiohttp, "ClientSession", _fake_session([(429, too_big, {}), (200, ok, {})], calls))
    ai._cool.clear()
    assert asyncio.run(ai._ask_reserve("x" * 14000)) == {"a": 1}
    assert len(calls) == 2
    assert len(calls[1]["messages"][0]["content"][0]["text"]) < len(calls[0]["messages"][0]["content"][0]["text"])


def test_reserve_429_long_limit_pauses_provider(monkeypatch):
    """Qwen 429 с долгим ожиданием — один запрос, провайдер на паузе, следующий вызов его пропускает."""
    import asyncio

    import pytest
    from shorts import ai
    calls = []
    busy = {"error": {"message": "Rate limit reached ... (TPD): Limit 500000, Used 499000, Requested 3000. "
                                 "Please try again in 14m3s"}}
    monkeypatch.setattr(ai.aiohttp, "ClientSession", _fake_session([(429, busy, {})], calls))
    ai._cool.clear()
    with pytest.raises(ai.AIError):
        asyncio.run(ai._ask_reserve("короткое задание"))
    assert len(calls) == 1
    assert ai.cooling("reserve") and 14 <= ai.retry_in() / 60 <= 15


def test_with_reserve_switches_skips_paused_and_reports_retry(monkeypatch):
    """Gemini 429 → Qwen 429 → Lumean тайм-аут: ошибка AllBusy со временем, когда освободится ближайший ИИ;
    второй вызов не дёргает провайдеров на паузе; после паузы провайдер снова используется."""
    import asyncio
    import time

    import pytest
    from shorts import ai
    asked = []

    async def gemini(key, parts, search=False):
        asked.append("gemini")
        ai._quota_until[(key, False)] = time.monotonic() + 3600
        raise ai.AIError("Gemini: ключ 1: лимит исчерпан")

    async def reserve(prompt, image=None):
        asked.append("reserve")
        ai.cooldown("reserve", 600, "лимит в минуту")
        raise ai.AIError("резервный ИИ HTTP 429")

    async def lumean(prompt, image=None, max_tokens=4000):
        asked.append("lumean")
        raise asyncio.TimeoutError()

    monkeypatch.setattr(ai, "_ask", gemini)
    monkeypatch.setattr(ai, "_ask_reserve", reserve)
    monkeypatch.setattr(ai, "_ask_lumean", lumean)
    monkeypatch.setattr(ai, "RESERVE_KEY", "r")
    monkeypatch.setattr(ai, "BEAT_KEY", "")
    monkeypatch.setattr(ai, "LUMEAN_LLM_KEY", "l")
    monkeypatch.setattr(ai, "LLM_PRIMARY", "lumean")
    monkeypatch.setattr(ai, "LUMEAN_MODE", "important")
    ai._cool.clear()
    ai._quota_until.clear()
    with pytest.raises(ai.AllBusy) as err:
        asyncio.run(ai._with_reserve("k", [{"text": "x"}], "x"))
    assert asked == ["gemini", "reserve", "lumean"]
    assert "не ответил за" in str(err.value) and 9 <= err.value.retry_in / 60 <= 10
    asked.clear()
    with pytest.raises(ai.AllBusy):
        asyncio.run(ai._with_reserve("k", [{"text": "x"}], "x"))
    assert "reserve" not in asked  # на паузе — не тратим запрос
    # пауза кончилась — резервный ИИ снова в деле
    ai._cool["reserve"] = (time.monotonic() - 1, "")

    async def reserve_ok(prompt, image=None):
        asked.append("reserve_ok")
        return {"ok": True}
    monkeypatch.setattr(ai, "_ask_reserve", reserve_ok)
    assert asyncio.run(ai._with_reserve("k", [{"text": "x"}], "x")) == {"ok": True}
    ai._cool.clear()
    ai._quota_until.clear()


def test_lumean_timeout_grows_for_big_tasks():
    from shorts import ai
    assert ai.lumean_timeout("x" * 1000) == 150
    assert ai.lumean_timeout("x" * 30000) == 300 and ai.lumean_timeout("x", 12000) == 300
    assert ai._retry_after({}, {}, "try again in 1m30.5s") == 90.5


def test_say_many_pairs_in_one_line():
    """/say несколькими парами в одну строку — каждая пара отдельно (раньше голос читал весь список)."""
    from shorts import pronounce
    pairs = pronounce.parse_pairs("Средиземье = средИземье Кольца власти = кОльца влАсти критики = крИтики\n"
                                  "Prime Video = прайм вИдео")
    assert pairs == [("Средиземье", "средИземье"), ("Кольца власти", "кОльца влАсти"), ("критики", "крИтики"),
                     ("Prime Video", "прайм вИдео")]
    assert pronounce.repair({"Средиземье": "средИземье критики = крИтики", "IMAX": "Аймакс"}) == \
        {"Средиземье": "средИземье", "критики": "крИтики", "IMAX": "Аймакс"}


def test_patch_voice_refuses_overlap(tmp_path):
    """Новая фраза намного длиннее старой — не накладываем на соседние, а просим пересобрать кусок заново."""
    import asyncio
    import shutil
    import subprocess

    import pytest
    if not shutil.which("ffmpeg"):
        pytest.skip("нет ффмпег")
    from shorts import project
    for name, d in (("old.wav", 4.4), ("new.mp3", 6.0)):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=f=440:d={d}", str(tmp_path / name)],
                       check=True)
    voice = {"starts": [0, 2.2], "speech": [2, 2]}
    with pytest.raises(project.TooLong):
        asyncio.run(project.patch_voice(tmp_path / "old.wav", voice, {0: tmp_path / "new.mp3"}, tmp_path / "o.wav"))

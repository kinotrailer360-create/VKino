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

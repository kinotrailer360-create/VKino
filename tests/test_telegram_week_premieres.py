import asyncio
from datetime import date
from unittest.mock import AsyncMock

from shorts import daily, tmdb


def test_week_premieres_keep_posterless_films_and_deduplicate_regional_fallback(monkeypatch):
    async def kinopoisk(path, **params):
        assert path == "/movie"
        assert params["premiere.russia"] == "16.11.2026-22.11.2026"
        return {"docs": [
            {"id": 1, "name": "Один", "premiere": {"russia": "2026-11-19"}, "externalId": {"tmdb": 22}},
            {"id": 2, "name": "Другая неделя", "premiere": {"russia": "2026-11-23"}},
            {"id": 3, "name": "Онлайн", "premiere": {"digital": "2026-11-19"}},
        ]}

    fallback = AsyncMock(return_value=[
        {"id": "tmdb:movie:22", "name": "One", "premiere": {"russia": "2026-11-19"}, "externalId": {"tmdb": 22}},
        {"id": "tmdb:movie:44", "name": "Второй", "premiere": {"russia": "2026-11-18"}},
    ])
    monkeypatch.setattr(tmdb, "russian_theatrical_releases", fallback)
    start, end, films = asyncio.run(daily.telegram_week_premieres(kinopoisk, "key", date(2026, 11, 18)))
    assert (start, end) == (date(2026, 11, 16), date(2026, 11, 22))
    assert [film["name"] for film in films] == ["Второй", "Один"]


def test_tmdb_discovers_russian_cinema_only(monkeypatch):
    async def discover(session, key, path, **params):
        assert path == "/discover/movie"
        assert params["region"] == "RU"
        assert params["with_release_type"] == "3|2"
        return {"results": [
            {"id": 1, "title": "Кино", "release_date": "2026-11-18"},
            {"id": 2, "title": "Позже", "release_date": "2026-11-23"},
        ]}

    monkeypatch.setattr(tmdb, "_get", discover)
    films = asyncio.run(tmdb.russian_theatrical_releases("key", date(2026, 11, 16), date(2026, 11, 22), 5))
    assert len(films) == 1
    assert films[0]["year"] is None
    assert films[0]["premiere"] == {"russia": "2026-11-18"}


def test_telegram_week_draft_works_with_one_film_without_poster_or_ai(monkeypatch, tmp_path):
    monkeypatch.setenv("BOT_TOKEN", "123456:TEST")
    import bot

    film = {"id": 1, "name": "Один фильм", "premiere": {"russia": "2026-11-19"}}
    monkeypatch.setattr(bot, "SHORTS_DIR", tmp_path)
    monkeypatch.setattr(daily, "telegram_week_premieres", AsyncMock(return_value=(
        date(2026, 11, 16), date(2026, 11, 22), [film])))
    ai = AsyncMock(side_effect=AssertionError("Weekly premieres don't require AI"))
    monkeypatch.setattr(bot.shorts_ai, "write_list_post", ai)
    messenger = AsyncMock()
    messenger.send_message.return_value.message_id = 1234
    offer = AsyncMock()
    monkeypatch.setattr(bot, "offer_post", offer)

    asyncio.run(bot.make_list_post(messenger, 1, "week"))

    assert "Один фильм" in messenger.send_message.await_args.args[1]
    assert "19 ноября" in messenger.send_message.await_args.args[1]
    messenger.send_photo.assert_not_awaited()
    ai.assert_not_awaited()
    assert offer.await_args.args[2:5] == ([1234], "text", "pt:l:week")

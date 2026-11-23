import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shorts import daily


@pytest.fixture
def daily_bot(monkeypatch, tmp_path):
    monkeypatch.setenv("BOT_TOKEN", "123456:TEST")
    monkeypatch.setenv("KINOPOISK_TOKEN", "test")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test@localhost/test")
    import bot

    monkeypatch.setattr(bot, "SHORTS_DIR", tmp_path)
    monkeypatch.setattr(bot, "SHORTS_TIMEOUT", 0.4)
    monkeypatch.setattr(bot, "SHORTS_RENDER_TIMEOUT", 0.4, raising=False)
    pool = SimpleNamespace(fetch=AsyncMock(return_value=[]), executemany=AsyncMock())
    monkeypatch.setattr(bot, "db", lambda: pool)
    for name in ("load_pronunciations", "load_voice", "load_fact_channels", "custom_music",
                 "set_shorts_setting", "news_feeds", "shorts_setting", "send_short"):
        monkeypatch.setattr(bot, name, AsyncMock(return_value=None))
    return bot


def test_daily_short_keeps_rendering_after_preparation_deadline(daily_bot, monkeypatch):
    async def build(*args, **kwargs):
        await asyncio.sleep(0.2)
        await daily._progress(50, "Монтаж видео")
        await daily._progress(75, "Монтаж видео")
        await asyncio.sleep(0.3)
        return SimpleNamespace(films=[], seo={})

    monkeypatch.setattr(daily_bot, "make_daily_short", build)

    asyncio.run(daily_bot.run_short(None, [1], "online"))

    daily_bot.send_short.assert_awaited_once()


@pytest.mark.parametrize("rendering", [False, True])
def test_daily_short_reports_phase_when_its_deadline_expires(daily_bot, monkeypatch, rendering):
    async def build(*args, **kwargs):
        if rendering:
            for pct in range(50, 98):
                await daily._progress(pct, "Монтаж видео")
                await asyncio.sleep(0.05)
        else:
            await daily._progress(40, "Озвучка")
        await asyncio.Event().wait()

    async def run():
        await asyncio.wait_for(daily_bot.run_short(None, [1], "online"), timeout=1.5)

    monkeypatch.setattr(daily_bot, "make_daily_short", build)

    phase = "Монтаж превысил" if rendering else "Подготовка превысила"
    with pytest.raises(RuntimeError, match=phase):
        asyncio.run(run())

    daily_bot.send_short.assert_not_awaited()


@pytest.mark.parametrize("approve", [True, False])
def test_scheduled_short_waits_for_one_decision(daily_bot, monkeypatch, approve):
    settings = {}
    scheduled = AsyncMock()
    telegram = SimpleNamespace(send_message=AsyncMock())
    message = SimpleNamespace(answer=AsyncMock())
    now = datetime.now(daily_bot.MSK).replace(hour=12)
    day = now.date().isoformat()
    theme = daily.SCHEDULE[now.weekday()]

    async def read(key):
        return settings.get(key)

    async def save(key, value):
        settings[key] = value

    async def claim(query, offer, replacement):
        assert "AND value = $1" in query
        if settings["daily_short_offer"] == offer:
            settings["daily_short_offer"] = replacement
            return replacement
        return None

    monkeypatch.setattr(daily_bot, "ADMIN_IDS", {1})
    monkeypatch.setattr(daily_bot, "SHORTS_HOUR", 8)
    monkeypatch.setattr(daily_bot, "shorts_setting", read)
    monkeypatch.setattr(daily_bot, "set_shorts_setting", save)
    monkeypatch.setattr(daily_bot, "db", lambda: SimpleNamespace(fetchval=claim))
    monkeypatch.setattr(daily_bot, "start_short", scheduled)

    async def run():
        await daily_bot.offer_daily_short(telegram, now)
        await daily_bot.offer_daily_short(telegram, now)
        scheduled.assert_not_awaited()
        assert settings["daily_short_offer"] == f"{day}|{theme}"
        await asyncio.gather(*(daily_bot.daily_short_choice(message, day, approve) for _ in range(2)))

    asyncio.run(run())
    telegram.send_message.assert_awaited_once()
    assert settings["daily_short_offer"] == f"{day}|"
    if approve:
        scheduled.assert_awaited_once_with(message, theme, strict_theme=True)
    else:
        scheduled.assert_not_awaited()


def test_daily_custom_topic_keeps_user_title_and_opens_scene_review(daily_bot, monkeypatch):
    from shorts import hype

    posted = {"title": "Почему все боятся Дума?"}
    prepare = AsyncMock(return_value=posted)
    review = AsyncMock()
    state = SimpleNamespace(get_data=AsyncMock(return_value={}))
    message = SimpleNamespace(text="Собери Shorts по теме:Кто такой Доктор Дум?\nНазвание: Почему все боятся Дума?",
                              reply_to_message=None, answer=AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock())))
    monkeypatch.setattr(hype, "topic_short_plan", prepare)
    monkeypatch.setattr(daily_bot, "show_structured_plan", review)

    asyncio.run(daily_bot.daily_short_topic(message, state))

    prepare.assert_awaited_once_with(daily_bot.GEMINI_API_KEY, "Кто такой Доктор Дум?", posted["title"])
    review.assert_awaited_once_with(message, state, posted)


def test_approved_theme_does_not_render_another_genre(tmp_path, monkeypatch):
    pick = AsyncMock(return_value=[])
    monkeypatch.setattr(daily, "pick_films", pick)
    with pytest.raises(RuntimeError, match="онлайн|online"):
        asyncio.run(daily.make_daily_short(AsyncMock(), tmp_path, "online", strict_theme=True))
    pick.assert_awaited_once()

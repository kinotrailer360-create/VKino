import asyncio
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

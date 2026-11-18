"""Social source discovery uses only configured official API adapters."""
import asyncio
from unittest.mock import AsyncMock, patch

from shorts import studio_social_sources as sources


def test_title_filter_requires_movie_and_trailer():
    assert sources.useful_title("Marvel: Avengers Official Trailer", ["Avengers"])
    assert sources.useful_title("Мстители: трейлер", ["Мстители"])
    assert not sources.useful_title("Fan interview about Avengers", ["Avengers"])
    assert not sources.useful_title("Other Movie Official Trailer", ["Avengers"])


def test_discovery_deduplicates_public_results():
    async def scenario():
        a = {"url": "https://www.youtube.com/watch?v=1",
             "platform": "YouTube", "title": "Trailer"}
        b = {"url": "https://rutube.ru/video/2/", "platform": "Rutube", "title": "Трейлер"}
        with patch.object(sources, "youtube", new=AsyncMock(return_value=[a])), patch.object(
            sources, "vk", new=AsyncMock(return_value=[a])
        ), patch.object(sources, "rutube", new=AsyncMock(return_value=[b])):
            found = await sources.discover(["Мстители"])
        assert [v["url"] for v in found] == [a["url"], b["url"]]
    asyncio.run(scenario())


def test_missing_credential_skips_provider():
    async def scenario():
        with patch.dict("os.environ", {"YOUTUBE_API_KEY": "", "VK_API_TOKEN": ""}):
            assert await sources.youtube(["Avengers"]) == []
            assert await sources.vk(["Avengers"]) == []
    asyncio.run(scenario())

import asyncio
import calendar
from unittest.mock import AsyncMock

import aiohttp
import pytest

from shorts import fastnews


def test_worldofreel_falls_back_once_and_preserves_publication_time(monkeypatch):
    source = fastnews.WORLD_OF_REEL_RSS
    published = calendar.timegm((2026, 11, 22, 23, 24, 51))
    feed = """<rss version="2.0"><channel><title>World of Reel</title><item>
      <title>Cinema news</title><link>https://www.worldofreel.com/blog/story</link>
      <pubDate>Sun, 22 Nov 2026 23:24:51 +0000</pubDate>
      </item></channel></rss>"""
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")
    monkeypatch.setattr(fastnews, "_firecrawl_next", 0)
    monkeypatch.setattr(fastnews, "_firecrawl_rss", None)
    monkeypatch.setattr(fastnews, "_firecrawl_lock", asyncio.Lock())
    download = AsyncMock(side_effect=[
        aiohttp.ClientResponseError(None, (), status=status)
        for status in (403, 429, 403, 429)
    ])
    monkeypatch.setattr(fastnews, "_download", download)

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self):
            return {"success": True, "data": {"rawHtml": feed, "metadata": {"statusCode": 200}}}

    class Session:
        requests = []

        def post(self, url, **kwargs):
            self.requests.append((url, kwargs))
            return Response()

    session = Session()

    async def check_feed():
        first = await fastnews._fetch(session, source, published - 1, 200)
        expired = await fastnews._fetch(session, source, published + 1, 200)
        return first, expired

    first, expired = asyncio.run(check_feed())
    assert expired == []
    assert first[0]["time"] == published
    assert first[0]["url"] == "https://www.worldofreel.com/blog/story"
    [(url, request)] = session.requests
    assert url == fastnews.FIRECRAWL_SCRAPE
    assert request["json"]["url"] == source
    assert request["json"]["maxAge"] == 0
    assert request["allow_redirects"] is False


@pytest.mark.parametrize("source, configured", [
    (fastnews.WORLD_OF_REEL_RSS, False),
    ("https://www.empireonline.com/", True),
])
def test_firecrawl_stays_off_without_key_or_for_other_feeds(monkeypatch, source, configured):
    if configured:
        monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")
    else:
        monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    download = AsyncMock(side_effect=aiohttp.ClientResponseError(None, (), status=403))
    fallback = AsyncMock()
    monkeypatch.setattr(fastnews, "_download", download)
    monkeypatch.setattr(fastnews, "_worldofreel_via_firecrawl", fallback)
    with pytest.raises(aiohttp.ClientResponseError):
        asyncio.run(fastnews._fetch(None, source, 0, 200))
    fallback.assert_not_awaited()

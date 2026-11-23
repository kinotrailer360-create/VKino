def test_reddit_429_cools_down_without_repeat_requests(monkeypatch):
    import asyncio
    import aiohttp
    from shorts import fastnews, health

    source = fastnews.REDDIT_PACK
    url = fastnews.feed_url(source)
    fastnews._reddit_cache.clear()
    fastnews._reddit_retry.clear()
    calls = []

    async def throttle(session):
        calls.append(url)
        raise aiohttp.ClientResponseError(None, (), status=429, message='Too Many Requests',
                                          headers={'Retry-After': '7200'})

    monkeypatch.setattr(fastnews, '_download_reddit', throttle)

    async def run():
        for _ in range(3):
            try:
                await fastnews._fetch(None, source, 0, 10)
            except fastnews.RedditRateLimited:
                pass
            else:
                raise AssertionError('429 should remain visible to source status')
    asyncio.run(run())
    assert calls == [url]
    assert 3600 < fastnews._reddit_retry[url] - fastnews.time.time() <= 7200
    state = {}
    for _ in range(3):
        state, alerts = health.update(state, [(url, fastnews.REDDIT_PACK_LABEL, 'Лимит Reddit: ждём следующей попытки')])
        assert not alerts
    assert health.status_line(state[url]).startswith('🟡')


def test_reddit_pack_is_read_once_and_retains_each_subreddit(monkeypatch):
    import asyncio
    from shorts import fastnews

    monkeypatch.setattr(fastnews, '_reddit_cache', {})
    monkeypatch.setattr(fastnews, '_reddit_retry', {})
    raw = b'''<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>New official trailer</title><updated>2026-11-23T12:00:00Z</updated>
        <link href="https://www.reddit.com/r/movies/comments/a/"/></entry>
      <entry><title>New series announcement</title><updated>2026-11-23T12:00:00Z</updated>
        <link href="https://www.reddit.com/r/television/comments/b/"/></entry>
      <entry><title>Off topic</title><updated>2026-11-23T12:00:00Z</updated>
        <link href="https://www.reddit.com/r/gaming/comments/c/"/></entry>
    </feed>'''
    calls = []

    async def download(session):
        calls.append(session)
        return raw

    monkeypatch.setattr(fastnews, '_download_reddit', download)

    async def run():
        first = await fastnews._fetch(None, fastnews.REDDIT_PACK, 0, 6)
        second = await fastnews._fetch(None, fastnews.REDDIT_PACK, 0, 25)
        return first, second

    first, second = asyncio.run(run())
    assert first == second
    assert [post['channel'] for post in first] == ['Reddit r/movies', 'Reddit r/television']
    assert len(calls) == 1


def test_pack_migrates_retired_defaults_preserving_custom_sources():
    from shorts import fastnews

    custom = 'https://example.org/film/rss.xml'
    boxoffice = 'https://www.reddit.com/r/boxoffice/search.rss?q=flair%3A%22Industry+News%22&restrict_sr=1&'
    assert boxoffice in fastnews.LEGACY_REDDIT_FEEDS
    feeds = [*fastnews.LEGACY_REDDIT_FEEDS[:2], custom, fastnews.REDDIT_PACK, boxoffice]
    assert fastnews.with_reddit_pack(feeds) == [fastnews.REDDIT_PACK, custom]
    assert fastnews.with_reddit_pack([custom, fastnews.REDDIT_PACK]) == [custom, fastnews.REDDIT_PACK]

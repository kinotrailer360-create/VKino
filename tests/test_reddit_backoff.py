def test_reddit_429_cools_down_without_repeat_requests(monkeypatch):
    import asyncio
    import aiohttp
    from shorts import fastnews, health

    url = fastnews.DEFAULT_FEEDS[1]
    fastnews._reddit_cache.clear()
    fastnews._reddit_retry.clear()
    calls = []

    async def throttle(session, requested):
        calls.append(requested)
        raise aiohttp.ClientResponseError(None, (), status=429, message='Too Many Requests',
                                          headers={'Retry-After': '7200'})

    async def no_wait(seconds):
        return None

    monkeypatch.setattr(fastnews, '_download', throttle)
    monkeypatch.setattr(fastnews.asyncio, 'sleep', no_wait)

    async def run():
        for _ in range(3):
            try:
                await fastnews._fetch(None, url, 0, 10)
            except fastnews.RedditRateLimited:
                pass
            else:
                raise AssertionError('429 should remain visible to source status')
    asyncio.run(run())
    assert calls == [url]
    assert 3600 < fastnews._reddit_retry[url] - fastnews.time.time() <= 7200
    state = {}
    for _ in range(3):
        state, alerts = health.update(state, [(url, 'Reddit r/boxoffice', 'Лимит Reddit: ждём следующей попытки')])
        assert not alerts
    assert health.status_line(state[url]).startswith('🟡')

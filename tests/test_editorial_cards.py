import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from shorts import hype


def test_card_distinguishes_discussion_links_from_read_sources():
    article = 'https://deadline.com/2026/film/article/'
    discussion = dict(url='https://www.reddit.com/r/movies/comments/abc', article_url=article,
                      channel='Reddit', text='Обсуждают новый кастинг', image='https://example.com/photo.jpg')
    assert hype.card({'posts': [discussion]})['status'] == 'Нужна сверка с первоисточником'
    source = dict(url=article, title='New casting report', channel='Deadline')
    result = hype.card({'posts': [discussion, source]})
    assert result['status'] == 'Сообщает профильное издание'
    assert result['headline'] == source['title']
    assert result['links'][0] == (article, 'Deadline')
    assert result['media'] == [(discussion['image'], 'Фото')]
    assert hype.source_rank({'url': 'https://marvel.com.example.org/announcement'}) == 0


def test_clustering_does_not_merge_two_stories_just_for_franchise():
    trailer = dict(text='Marvel releases first Avengers trailer online today', lang='en',
                   url='https://marvel.com/articles/movies/trailer', channel='Marvel')
    cast = dict(text='Avengers actor confirms casting return in interview', lang='en',
                url='https://deadline.com/cast/', channel='Deadline')
    discussion = dict(text='Новый ролик уже выложили', lang='en', channel='Reddit',
                      url='https://reddit.com/r/movies/abc', article_url=trailer['url'])
    groups = hype.clusters([trailer, cast, discussion], lambda text: set(text.lower().split()), set.intersection)
    assert [group['posts'] for group in groups] == [[trailer, discussion], [cast]]
    assert [group['posts'] for group in hype.hot(groups)] == [[trailer, discussion]]


def test_single_insider_trailer_report_requires_details_and_original_post():
    original = dict(url='https://x.com/trailertrack/status/123', channel='X @trailertrack',
                    text='The new Avengers trailer drops on Monday.')
    group = dict(posts=[original], sources=1, franchise='Marvel')
    selected, = hype.hot([group])
    assert 'один источник' in hype.card(selected)['status']
    assert not hype.hot([{**group, 'posts': [{**original, 'text': 'Ready for an epic Monday? #AvengersDoomsday'}]}])
    assert not hype.hot([{**group, 'posts': [{**original, 'url': 'https://x.com/someone/status/123'}]}])


def test_shorts_plan_keeps_sources_from_the_card(monkeypatch):
    row = dict(line='В интервью режиссёр рассказал, что к съёмкам команда приступит уже этой зимой.',
               visual='Режиссёр рассказывает о съёмках', query='film director interview', label='Съёмки зимой')
    model = AsyncMock(return_value=dict(title='Когда начнутся съёмки?', description='Подробнее https://invented.test',
                                       hashtags='#Кино', scenes=[row] * 6))
    monkeypatch.setattr(hype.ai, '_with_reserve', model)
    url = 'https://deadline.com/casting/'
    plan = asyncio.run(hype.short_plan('test', {'posts': [dict(url=url, text=row['line'])]}))
    assert len(plan['lines']) == len(plan['scenes']) == 6
    assert plan['scenes'][0]['queries'] == ['film director interview']
    assert plan['description'] == f'Подробнее\n\n{url}'
    assert model.await_args.kwargs['allow_paid'] is False


def test_card_delivery_shows_draft_buttons_without_generating(monkeypatch):
    monkeypatch.setenv('BOT_TOKEN', '123456:TEST')
    monkeypatch.setenv('KINOPOISK_TOKEN', 'test')
    monkeypatch.setenv('DATABASE_URL', 'postgresql://test@localhost/test')
    import bot

    monkeypatch.setattr(bot, 'hype_cache', {})
    monkeypatch.setattr(bot, '_posts_history', AsyncMock(return_value={'stories': []}))
    script = AsyncMock()
    monkeypatch.setattr(hype, 'short_plan', script)
    sender = SimpleNamespace(send_message=AsyncMock())
    group = dict(posts=[dict(url='https://marvel.com/articles/movies/news', text='Тизер уже вышел')],
                 tokens=['тизер'], franchise='Marvel', sources=1)
    asyncio.run(bot.hype_alert(sender, group, [10]))
    args, kwargs = sender.send_message.await_args
    assert 'Есть публикация студии' in args[1]
    row = kwargs['reply_markup'].inline_keyboard[0]
    assert [button.text for button in row] == ['📝 Черновик поста', '🎬 Сценарий Shorts']
    script.assert_not_awaited()
    sender.send_message.side_effect = RuntimeError('Telegram unavailable')
    assert asyncio.run(bot.hype_alert(sender, group, [10])) is False

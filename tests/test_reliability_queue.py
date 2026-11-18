import asyncio
import os
from types import SimpleNamespace

for key, value in {'BOT_TOKEN':'123456:TEST', 'ADMIN_IDS':'1', 'KINOPOISK_TOKEN':'test',
                   'DATABASE_URL':'postgresql://test@localhost/test'}.items():
    os.environ.setdefault(key, value)


def test_schedule_does_not_delete_job_before_failed_send(monkeypatch):
    import bot
    events = []
    class DB:
        async def fetch(self, sql, *args):
            if 'DELETE FROM ai_queue' in sql:
                events.append('deleted')
                return [dict(chat_id=1, first_id=2)]
            if "status='uncertain'" in sql:
                return []
            return [dict(chat_id=1, first_id=2, attempts=1)]
        async def execute(self, sql, *args):
            events.append(('update', args))
            return 'UPDATE 1'
    async def publish(*args):
        events.append('send')
        raise RuntimeError('connection lost; delivery unknown')
    async def no_legacy(*args):
        pass
    class Bot:
        async def send_message(self, *args, **kw):
            events.append('notice')
    monkeypatch.setattr(bot, 'db', lambda: DB())
    monkeypatch.setattr(bot, 'publish_post', publish)
    monkeypatch.setattr(bot, '_queue_json_remove', no_legacy)
    asyncio.run(bot.publish_due(Bot()))
    assert 'deleted' not in events
    assert 'send' in events
    assert any(isinstance(event, tuple) and 'uncertain' in event[1] for event in events)


def test_schedule_retries_explicit_rate_limit_without_losing_job(monkeypatch):
    import bot
    from aiogram.exceptions import TelegramRetryAfter
    from aiogram.methods import CopyMessages
    recorded = []
    class DB:
        async def fetch(self, sql, *args):
            if "status='uncertain'" in sql:
                return []
            return [dict(chat_id=1, first_id=2, attempts=1)]
        async def execute(self, sql, *args):
            recorded.append(args)
            return 'UPDATE 1'
    async def publish(*args):
        raise TelegramRetryAfter(method=CopyMessages(chat_id=1, from_chat_id=1, message_ids=[2]),
                                 message='rate limit', retry_after=30)
    async def send(*args, **kwargs):
        pass
    monkeypatch.setattr(bot, 'db', lambda: DB())
    monkeypatch.setattr(bot, 'publish_post', publish)
    asyncio.run(bot.publish_due(SimpleNamespace(send_message=send)))
    assert any('queued' in args for args in recorded)

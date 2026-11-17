import asyncio
import json
import os
from types import SimpleNamespace

from shorts import ai, remix


def test_remix_metadata_uses_actual_narration_when_ai_is_unavailable(tmp_path, monkeypatch):
    (tmp_path / 'remix_narration.json').write_text(json.dumps([
        dict(text='«Сердце зверя» — красивый и напряжённый фильм.', start=0, end=3),
        dict(text='Моя оценка — десять из десяти.', start=3, end=5),
    ]))
    monkeypatch.setattr(ai, 'available', lambda key: False)
    seo = asyncio.run(remix.publication_metadata(tmp_path, ''))
    assert 'Сердце зверя' in seo['title']
    assert 'десять из десяти' in seo['description']
    assert len(seo['title']) <= 100
    assert json.loads((tmp_path / 'remix_seo.json').read_text()) == seo


def test_finished_remix_has_copyable_metadata_and_existing_upload_button(tmp_path, monkeypatch):
    for name, value in {'BOT_TOKEN': '123456:TEST', 'ADMIN_IDS': '1', 'KINOPOISK_TOKEN': 'test',
                        'DATABASE_URL': 'postgresql://test@localhost/test'}.items():
        os.environ.setdefault(name, value)
    import bot

    ready = tmp_path / 'ready'
    video = tmp_path / 'output.mp4'
    video.write_bytes(b'original full quality video')
    monkeypatch.setattr(bot, 'READY_DIR', ready)
    async def metadata(*args):
        return dict(title='Название <фильма>', description='Описание & смысл', tags='кино, KinoMir360')
    monkeypatch.setattr(remix, 'publication_metadata', metadata, raising=False)
    class Message:
        chat = SimpleNamespace(id=123)
        def __init__(self):
            self.sent = []
        async def answer_video(self, file, **kwargs):
            self.sent.append(('video', kwargs))
            return SimpleNamespace(message_id=42)
        async def answer(self, text, **kwargs):
            self.sent.append((text, kwargs))
    message = Message()
    asyncio.run(bot.send_remix_ready(message, video, tmp_path, 'Готово'))
    assert message.sent[0][0] == 'video'
    assert '<pre>Название &lt;фильма&gt;</pre>' in message.sent[1][0]
    assert '<pre>Описание &amp; смысл</pre>' in message.sent[2][0]
    button = message.sent[2][1]['reply_markup'].inline_keyboard[0][0]
    assert button.callback_data == 'yt:42'
    assert (ready / '123_42.mp4').read_bytes() == b'original full quality video'
    stored = json.loads((ready / '123_42.json').read_text())
    assert stored['title'] == 'Название <фильма>'
    assert stored['description'] == 'Описание & смысл'

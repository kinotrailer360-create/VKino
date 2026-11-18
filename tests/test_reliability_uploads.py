import asyncio
import copy
import os
from types import SimpleNamespace

for key, value in {'BOT_TOKEN':'123456:TEST', 'ADMIN_IDS':'1', 'KINOPOISK_TOKEN':'test',
                   'DATABASE_URL':'postgresql://test@localhost/test'}.items():
    os.environ.setdefault(key, value)


def test_simultaneous_remix_uploads_preserve_both_files(tmp_path):
    import bot
    class State:
        def __init__(self):
            self.data = {'remix_work':str(tmp_path), 'remix_assets':[]}
        async def get_data(self):
            return copy.deepcopy(self.data)
        async def update_data(self, **kw):
            self.data.update(copy.deepcopy(kw))
    class Downloader:
        async def download(self, file_id, destination):
            await asyncio.sleep(.01)
            destination.write_text(file_id)
    class Message:
        from_user = SimpleNamespace(id=1)
        photo = document = None
        bot = Downloader()
        def __init__(self, file_id):
            self.video = SimpleNamespace(file_id=file_id, file_unique_id=file_id, file_size=10, mime_type='video/mp4')
        async def answer(self, *args, **kwargs):
            pass
    async def scenario():
        state = State()
        await asyncio.gather(bot.remix_asset(Message('first'), state), bot.remix_asset(Message('second'), state))
        return state.data['remix_assets']
    paths = asyncio.run(scenario())
    from pathlib import Path
    assert len(set(paths)) == 2
    assert {Path(path).read_text() for path in paths} == {'first', 'second'}

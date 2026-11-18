import asyncio
import os
import json
from types import SimpleNamespace
from pathlib import Path
import signal
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_legacy_copy_accepts_existing_alias(tmp_path):
    target = tmp_path / 'work'
    target.mkdir()
    (target / 'video.mp4').write_bytes(b'saved')
    legacy = tmp_path / 'old'
    legacy.symlink_to(target, target_is_directory=True)
    subprocess.run([sys.executable, str(ROOT / 'deploy/preserve-work.py'), str(legacy), str(target)], check=True)
    assert (target / 'video.mp4').read_bytes() == b'saved'
    legacy.unlink()
    legacy.mkdir()
    (legacy / 'old.mp4').write_bytes(b'legacy')
    subprocess.run([sys.executable, str(ROOT / 'deploy/preserve-work.py'), str(legacy), str(target)], check=True)
    assert (target / 'old.mp4').read_bytes() == b'legacy'


def test_install_preserves_before_replacing_container():
    install = (ROOT / 'deploy/install.sh').read_text()
    assert 'echo "SHORTS_DIR=/data/work"' in install
    assert install.index('preserve-work.py') < install.index('docker compose up -d --build')
    assert 'SHORTS_DIR=/tmp/kinomir_shorts' not in install
    assert 'kinomir_shorts' in install  # migrate existing legacy .env too
    assert 'copytree' not in (ROOT / 'deploy/update.sh').read_text()


@pytest.mark.parametrize('setting', ['/tmp/kinomir_shorts', '"/tmp/kinomir_shorts"', "'/tmp/kinomir_shorts'", '/data/custom'])
def test_install_migrates_only_legacy_setting(tmp_path, setting):
    env = tmp_path / '.env'
    env.write_text('SHORTS_DIR=' + setting + '\nBOT_TOKEN=placeholder\n')
    command = next(line for line in (ROOT / 'deploy/install.sh').read_text().splitlines()
                   if line.startswith('sed -i -E "s|^SHORTS_DIR='))
    subprocess.run(['bash', '-c', command], cwd=tmp_path, check=True)
    expected = '/data/custom' if setting == '/data/custom' else '/data/work'
    assert env.read_text() == 'SHORTS_DIR=' + expected + '\nBOT_TOKEN=placeholder\n'


def test_isolation_keeps_album_concurrency_but_serializes_remix():
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    from shorts.fsm_pg import RemixEventIsolation
    async def scenario():
        storage = MemoryStorage()
        key = StorageKey(bot_id=1, chat_id=1, user_id=1)
        isolation = RemixEventIsolation(storage)
        for name, simultaneous in [('CustomState:photos', True), ('RemixState:assets', False)]:
            await storage.set_state(key, name)
            entered = asyncio.Event()
            async with isolation.lock(key):
                async def sibling():
                    async with isolation.lock(key):
                        entered.set()
                task = asyncio.create_task(sibling())
                await asyncio.sleep(.02)
                assert entered.is_set() == simultaneous
            await task
        await isolation.close()
    asyncio.run(scenario())


def test_interrupted_telegram_claims_require_channel_review(monkeypatch):
    import bot
    calls = []
    class DB:
        async def execute(self, sql, *args):
            calls.append(sql)
    monkeypatch.setattr(bot, 'db', lambda: DB())
    asyncio.run(bot.recover_post_publications())
    assert any("status='uncertain'" in sql and "status='publishing'" in sql for sql in calls)
    async def denied(*args):
        return False
    async def state(*args):
        return {'status': 'uncertain'}
    monkeypatch.setattr(bot, '_claim_publish', denied)
    monkeypatch.setattr(bot, 'post_publication_state', state)
    with pytest.raises(bot.PublicationNeedsReview):
        asyncio.run(bot.publish_post(None, 1, 2))


def test_cancelled_youtube_claim_becomes_uncertain(tmp_path, monkeypatch):
    import bot
    (tmp_path / '1_2.mp4').write_bytes(b'video')
    (tmp_path / '1_2.json').write_text(json.dumps({'title': 'Film'}))
    events = []
    class Pool:
        async def fetchval(self, *args):
            return 1
        async def execute(self, sql, *args):
            events.append((sql, args))
    class Message:
        chat = SimpleNamespace(id=1)
        async def answer(self, *args, **kwargs):
            return self
    async def value(*args):
        return 'token'
    async def answer(*args):
        pass
    async def scenario():
        started = asyncio.Event()
        async def upload(*args):
            started.set()
            await asyncio.Event().wait()
        tasks = []
        monkeypatch.setattr(bot, '_run_job', lambda job: tasks.append(asyncio.create_task(job())))
        monkeypatch.setattr(bot, 'yt_lock', asyncio.Lock())
        monkeypatch.setattr(bot.shorts_yt, 'upload', upload)
        await bot.youtube_button(SimpleNamespace(from_user=SimpleNamespace(id=1),message=Message(),data='yt:2',answer=answer))
        await asyncio.wait_for(started.wait(), 2)
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        assert any("status='uncertain'" in sql for sql, args in events)
        assert not bot.yt_lock.locked()
    monkeypatch.setattr(bot, 'READY_DIR', tmp_path)
    monkeypatch.setattr(bot, 'ADMIN_IDS', {1})
    monkeypatch.setattr(bot, 'YT_CLIENT_ID', 'client')
    monkeypatch.setattr(bot, 'YT_CLIENT_SECRET', 'secret')
    monkeypatch.setattr(bot, 'db', lambda: Pool())
    monkeypatch.setattr(bot, 'shorts_setting', value)
    monkeypatch.setattr(bot.shorts_yt, 'access_token', value)
    asyncio.run(scenario())


def test_streaming_renderer_cancellation_stops_descendant(tmp_path, monkeypatch):
    from shorts import daily
    pidfile = tmp_path / 'child'
    fake = tmp_path / 'renderer.py'
    # Child writes its own pid and sleeps. Parent reaps it on group SIGTERM.
    fake.write_text('import subprocess,sys,signal,time\n'
                    'p=subprocess.Popen([sys.executable,"-c",' + repr('import os,time,pathlib;pathlib.Path(' + repr(str(pidfile)) + ').write_text(str(os.getpid()));time.sleep(30)') + '])\n'
                    'def stop(*args):\n p.wait();sys.exit(0)\n'
                    'signal.signal(signal.SIGTERM,stop)\nprint("PROGRESS 0.1",flush=True)\ntime.sleep(30)\n')
    original = asyncio.create_subprocess_exec
    async def create(*args, **kwargs):
        return await original(sys.executable, str(fake), **kwargs)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create)
    async def scenario():
        task = asyncio.create_task(daily._run_render(tmp_path / 'config'))
        try:
            for _ in range(200):
                if pidfile.exists():
                    break
                await asyncio.sleep(.01)
            assert pidfile.exists()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            pid = int(pidfile.read_text())
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            if not task.done():
                task.cancel()
            if pidfile.exists():
                try:
                    os.kill(int(pidfile.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
    asyncio.run(scenario())


def test_manual_uncertain_post_offers_explicit_checked_retry(monkeypatch):
    import bot
    sent = []
    async def meta(*args):
        return {}
    async def publish(*args, **kwargs):
        raise bot.PublicationNeedsReview()
    async def answer(*args, **kwargs):
        sent.append((args, kwargs))
    monkeypatch.setattr(bot, 'ADMIN_IDS', {1})
    monkeypatch.setattr(bot, '_post_meta', meta)
    monkeypatch.setattr(bot, 'publish_post', publish)
    message = SimpleNamespace(chat=SimpleNamespace(id=1), bot=None, answer=answer)
    asyncio.run(bot.post_buttons(SimpleNamespace(from_user=SimpleNamespace(id=1), message=message,
                                                 data='pt:p:2', answer=answer), None))
    buttons = [kw['reply_markup'].inline_keyboard for args, kw in sent if 'reply_markup' in kw]
    assert buttons[0][0][0].callback_data == 'pt:pf:2'

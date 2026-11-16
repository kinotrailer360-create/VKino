"""Bridge contracts. Local transport and synthetic voice; no paid provider calls."""
import asyncio
import json
import math
import shutil
import struct
import wave
from types import SimpleNamespace
from uuid import uuid4

import pytest
from aiohttp import web
from PIL import Image

from shorts.studio import Studio, digest, validate_job
from shorts import studio_adapter as adapter


def job(operation='script', stage=1):
    return {'requestId': str(uuid4()), 'operation': operation, 'sceneIndex': 0,
            'project': {'id': str(uuid4()), 'revision': 1, 'stage': stage,
                        'channel': 'youtube', 'title': 'Тест', 'input': 'Исходник',
                        'scenes': [{'text': 'Короткий абзац.', 'duration': 2, 'start': 0, 'end': 2,
                                    'asset': {'id': str(uuid4())}, 'match': True, 'checked': True,
                                    'rights': True, 'renderApproved': False}]}}


def test_approval_gates_and_bounds():
    validate_job(job())
    for operation, stage in [('script', 0), ('media', 1), ('render', 2), ('assemble', 3)]:
        with pytest.raises(ValueError):
            validate_job(job(operation, stage))
    p = job('render', 3)
    validate_job(p)
    p['project']['scenes'][0]['checked'] = False
    with pytest.raises(ValueError):
        validate_job(p)
    p = job()
    p['project']['scenes'][0]['duration'] = float('nan')
    with pytest.raises(ValueError):
        validate_job(p)


def test_video_is_never_looped():
    assert adapter.clip_window({'duration': 3, 'start': 1, 'end': 6}, 8, 2) == 3
    for scene, actual, voice in [({'duration': 3, 'start': 0, 'end': 2}, 10, 2),
                                  ({'duration': 3, 'start': 8, 'end': 12}, 10, 2),
                                  ({'duration': 3, 'start': 0, 'end': 10}, 10, 31)]:
        with pytest.raises(ValueError):
            adapter.clip_window(scene, actual, voice)


class Pool:
    """Minimal database double for token/job authorization and transaction contracts."""
    def __init__(self):
        self.tokens = {}
        self.jobs = {}

    def acquire(self):
        return self

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, sql, *args):
        if sql.startswith('INSERT INTO studio_tokens'):
            self.tokens[args[0]] = {'admin_id': args[1], 'kind': 'session' if "'session'" in sql else 'pair'}
        elif sql.startswith('DELETE FROM studio_tokens WHERE admin_id'):
            self.tokens = {k: v for k, v in self.tokens.items() if v['admin_id'] != args[0]}
        elif sql.startswith('INSERT INTO studio_jobs'):
            self.jobs[args[0]] = {'id': args[0], 'admin_id': args[1], 'payload': json.loads(args[2]),
                                 'status': 'queued', 'result': None, 'error': None}

    async def fetchrow(self, sql, *args):
        if sql.startswith('DELETE FROM studio_tokens'):
            row = self.tokens.get(args[0])
            return self.tokens.pop(args[0]) if row and row['kind'] == 'pair' else None
        if 'FROM studio_tokens' in sql:
            row = self.tokens.get(args[0])
            return row if row and row['kind'] == 'session' else None
        if 'FROM studio_jobs' in sql:
            row = self.jobs.get(args[0])
            return row if row and (len(args) == 1 or row['admin_id'] == args[1]) else None

    async def fetchval(self, sql, *args):
        return sum(j['admin_id'] == args[0] and j['status'] in ('queued', 'running') for j in self.jobs.values())


class Request:
    def __init__(self, data=None, token='', identifier=''):
        self.data = data
        self.headers = {'Authorization': 'Bearer ' + token} if token else {}
        self.match_info = {'id': identifier}

    async def json(self):
        return self.data


def test_pair_once_owner_isolation_revocation_and_idempotency(tmp_path):
    async def scenario():
        pool = Pool()
        studio = Studio(pool, {1, 2}, tmp_path, None, asyncio.Lock())
        code = await studio.issue_pair(1)
        assert code not in pool.tokens and digest(code) in pool.tokens
        paired = json.loads((await studio.pair(Request({'code': code}))).text)
        token = paired['token']
        with pytest.raises(ValueError):
            await studio.pair(Request({'code': code}))
        with pytest.raises(web.HTTPUnauthorized):
            await studio.authenticate(Request())
        assert await studio.authenticate(Request(token=token)) == 1
        p = job()
        first = await studio.submit(Request(p, token))
        again = await studio.submit(Request(p, token))
        assert json.loads(first.text)['id'] == json.loads(again.text)['id']
        assert len(pool.jobs) == 1
        p['project']['title'] = 'Changed'
        with pytest.raises(ValueError):
            await studio.submit(Request(p, token))
        other = json.loads((await studio.pair(Request({'code': await studio.issue_pair(2)}))).text)['token']
        with pytest.raises(web.HTTPNotFound):
            await studio.job(Request(token=other, identifier=p['requestId']))
        await studio.revoke(1)
        with pytest.raises(web.HTTPUnauthorized):
            await studio.authenticate(Request(token=token))
    asyncio.run(scenario())


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='ffmpeg required')
def test_render_and_assemble_with_audio(tmp_path, monkeypatch):
    async def scenario():
        image = tmp_path / 'source.jpg'
        Image.new('RGB', (320, 180), (80, 120, 180)).save(image)
        files = {'source': (image, {'mime': 'image/jpeg'})}

        async def resolve(admin, identifier):
            assert admin == 1
            return files[identifier]

        async def store(admin, path, mime, name):
            identifier = str(uuid4())
            files[identifier] = (path, {'mime': mime})
            return {'id': identifier, 'mime': mime, 'name': name, 'size': path.stat().st_size}

        async def synthesize(lines, work, careful):
            voice = work / 'voice.wav'
            with wave.open(str(voice), 'wb') as wav:
                wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                wav.writeframes(b''.join(struct.pack('<h', int(3000 * math.sin(2 * math.pi * 440 * i / 16000))) for i in range(16000)))
            return voice, [0], True, []

        monkeypatch.setattr(adapter.daily, 'synthesize', synthesize)
        bridge = SimpleNamespace(resolve_file=resolve, store_file=store)
        scene = {'text': 'Проверка русского текста и озвучки.', 'duration': 2, 'asset': {'id': 'source'}}
        work = tmp_path / 'render'
        work.mkdir()
        result = await adapter.render_scene(bridge, 1, scene, work)
        assert result['hasVoice'] and 1.9 <= result['duration'] <= 2.2
        probe = json.loads(await adapter.run('ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(work / 'scene.mp4')))
        assert {'audio', 'video'} <= {s['codec_type'] for s in probe['streams']}
        video = next(s for s in probe['streams'] if s['codec_type'] == 'video')
        assert (video['width'], video['height']) == (720, 1280)
        final = tmp_path / 'final'
        final.mkdir()
        assembled = await adapter.assemble(bridge, 1, [{'render': result['asset']}], final)
        assert assembled['hasVoice'] and assembled['asset']['size'] > 1000
        # An upload disguised as video cannot open a playlist or contact an HTTP URL.
        malicious = tmp_path / 'fake.mp4'
        malicious.write_text('#EXTM3U\n#EXTINF:10,\nhttp://127.0.0.1/private\n')
        with pytest.raises(ValueError):
            await adapter.duration(malicious)
    asyncio.run(scenario())

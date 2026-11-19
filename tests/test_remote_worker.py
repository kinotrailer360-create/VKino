"""Pure contract tests and a synthetic end-to-end local agent run."""
import hashlib
import shutil
import subprocess
from uuid import uuid4

import pytest
from PIL import Image

from shorts.studio_worker import validate_remote_spec, canonical_path, CHUNK
from vkino_studio.remote import origin, run_once


def make_spec(tmp_path):
    media = tmp_path / 'media'
    media.mkdir(exist_ok=True)
    image = media / 'scene.jpg'
    audio = media / 'voice.wav'
    Image.new('RGB', (400, 600), 'red').save(image)
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i',
                    'sine=frequency=440:duration=1.2', '-ar', '48000', str(audio)], check=True)
    def asset(path):
        raw = path.read_bytes()
        return {'path': 'media/' + path.name, 'fileId': str(uuid4()), 'size': len(raw),
                'sha256': hashlib.sha256(raw).hexdigest()}
    return {'requestId': str(uuid4()), 'revision': 1, 'authorApproved': True,
        'project': {'version': 1, 'title': 'Новости кино', 'width': 360, 'height': 640,
                    'fps': 25, 'captions': True, 'scenes': [
                        {'source': 'media/scene.jpg', 'voice': 'media/voice.wav',
                         'text': 'Новости кино сегодня', 'headline': 'ПРЕМЬЕРА',
                         'duration': 2.0, 'rights': 'author', 'approved': True}]},
        'assets': [asset(image), asset(audio)]}


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='ffmpeg not installed')
def test_validate_remote_spec(tmp_path):
    spec = make_spec(tmp_path)
    assert validate_remote_spec(spec)['authorApproved'] is True
    spec['authorApproved'] = False
    with pytest.raises(ValueError, match='утверждение'):
        validate_remote_spec(spec)
    spec['authorApproved'] = True
    spec['project']['scenes'][0]['approved'] = False
    with pytest.raises(ValueError, match='утверждена'):
        validate_remote_spec(spec)


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='ffmpeg not installed')
def test_validate_paths_and_assets(tmp_path):
    spec = make_spec(tmp_path)
    for invalid in ('../secret.jpg', 'media/../../secret.jpg', 'C:\\secret.jpg',
                    'https://evil.com/a.mp4', '/tmp/a.mp4', 'media/a.exe'):
        with pytest.raises(ValueError):
            canonical_path(invalid)
    spec['assets'][0]['sha256'] = 'wrong'
    with pytest.raises(ValueError, match='параметры'):
        validate_remote_spec(spec)
    spec = make_spec(tmp_path)
    spec['assets'].pop()
    with pytest.raises(ValueError, match='точно'):
        validate_remote_spec(spec)


def test_server_url_validation():
    assert origin('https://vkino.example/') == 'https://vkino.example'
    for url in ('http://vkino.example', 'https://vkino.example:8080',
                'https://user:pw@vkino.example', 'https://vkino.example/api',
                'https://vkino.example/?token=bad'):
        with pytest.raises(ValueError):
            origin(url)


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='ffmpeg missing')
def test_agent_renders_and_requests_review(tmp_path):
    spec = make_spec(tmp_path)
    spec['id'] = spec['requestId']
    spec['lease'] = 'secret-lease'
    spec['assets'].reverse()  # order of download must not matter

    class FakeClient:
        def __init__(self):
            self.completed = []
        def claim(self):
            return spec
        def heartbeat(self, id, lease):
            assert lease == 'secret-lease'
        def fetch_asset(self, id, lease, asset, folder):
            source = tmp_path / asset['path']
            target = folder / asset['path']
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        def upload(self, id, lease, result):
            assert result.stat().st_size > 1000
            assert result.name == 'vkino_studio_preview.mp4'
            self.completed.append(id)
            return {'status': 'awaiting_review'}
        def fail(self, id, lease):
            raise AssertionError('No failure expected')

    fake = FakeClient()
    assert run_once(fake, encoder='libx264', temp_root=tmp_path) is True
    assert fake.completed == [spec['id']]


def test_chunk_size_matches_transport():
    from vkino_studio.remote import CHUNK as CLIENT_CHUNK
    assert CHUNK == CLIENT_CHUNK == 4 * 1024 * 1024

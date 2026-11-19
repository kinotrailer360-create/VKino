"""Author-approval conversion and synthetic Windows final-assembly tests (offline)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

import pytest

from shorts.studio_pc import build_spec
from shorts.studio_worker import validate_remote_spec
from vkino_studio.assembly import assemble_approved_clips
from vkino_studio.remote import run_once


def make_source():
    j = str(uuid4())
    project_id = str(uuid4())
    ids = [str(uuid4()), str(uuid4())]
    project = {'id': project_id, 'revision': 3, 'stage': 3, 'channel': 'youtube',
               'title': 'Кино новости', 'scenes': [
                   {'text': f'Блок {i}', 'duration': 1.6, 'start': 0, 'end': 2,
                    'render': {'id': mid}, 'renderApproved': True}
                   for i, mid in enumerate(ids, 1)]}
    payload = {'requestId': str(uuid4()), 'operation': 'assemble', 'project': project}
    files = {mid: {'mime': 'video/mp4', 'size': 1024, 'hash': 'a' * 64}
             for mid in ids}
    return {'id': j, 'admin_id': 15, 'status': 'succeeded', 'payload': payload}, files


def test_only_explicitly_approved_scenes_can_be_queued():
    source, files = make_source()
    spec = build_spec(source, files, 15)
    assert spec['mode'] == 'assemble_clips'
    assert len(spec['project']['clips']) == 2
    assert all(x['rights'] == 'unconfirmed' for x in spec['project']['clips'])
    assert spec['requestId'] == build_spec(source, files, 15)['requestId']
    with pytest.raises(ValueError):
        build_spec(source, files, 16)
    source['payload']['project']['scenes'][0]['renderApproved'] = False
    with pytest.raises(ValueError):
        build_spec(source, files, 15)


def test_assembly_rejects_duplicate_or_missing_clips():
    source, files = make_source()
    spec = build_spec(source, files, 15)
    spec['project']['clips'][1]['source'] = spec['project']['clips'][0]['source']
    with pytest.raises(ValueError):
        validate_remote_spec(spec)
    source, files = make_source()
    spec = build_spec(source, files, 15)
    spec['project']['clips'][1]['approved'] = False
    with pytest.raises(ValueError):
        validate_remote_spec(spec)


def make_videos(folder: Path):
    media = folder / 'media'
    media.mkdir(parents=True, exist_ok=True)
    for i, color in enumerate(('red', 'blue'), 1):
        output = media / f'clip_{i:03d}.mp4'
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                        '-f', 'lavfi', '-i', f'color=c={color}:s=720x1280:r=30:d=1.6',
                        '-f', 'lavfi', '-i', 'sine=frequency=400:duration=1.6',
                        '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                        '-c:a', 'aac', '-ar', '48000', '-shortest', str(output)], check=True)
    return media


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='FFmpeg required')
def test_approved_two_clip_draft_with_audio(tmp_path):
    make_videos(tmp_path)
    manifest = tmp_path / 'project.json'
    manifest.write_text(json.dumps({'version': 1, 'title': 'Кино новости', 'width': 720,
                'height': 1280, 'fps': 30, 'clips': [
                    {'source': f'media/clip_{i:03d}.mp4', 'duration': 1.6,
                     'approved': True, 'rights': 'unconfirmed'} for i in (1, 2)]}), encoding='utf-8')
    video = assemble_approved_clips(manifest, tmp_path / 'output')
    assert video.stat().st_size > 5000
    info = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries',
             'stream=codec_type:format=duration', '-of', 'json', str(video)]))
    assert {'video', 'audio'}.issubset({s['codec_type'] for s in info['streams']})
    assert 3 <= float(info['format']['duration']) <= 4
    report = json.loads((video.parent / 'render_report.json').read_text(encoding='utf-8'))
    assert report['published'] is False
    assert report['author_final_approval'] is False


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='FFmpeg required')
def test_remote_worker_can_run_pc_assembly(tmp_path):
    media = make_videos(tmp_path)
    source, files = make_source()
    for i, key in enumerate(files, 1):
        raw = (media / f'clip_{i:03d}.mp4').read_bytes()
        files[key] = {'mime': 'video/mp4', 'size': len(raw), 'hash': hashlib.sha256(raw).hexdigest()}
    spec = build_spec(source, files, 15)
    spec['id'] = spec['requestId']
    spec['lease'] = 'test-lease'

    class FakeClient:
        def claim(self): return spec
        def heartbeat(self, job_id, lease): assert lease == 'test-lease'
        def fetch_asset(self, job_id, lease, asset, destination):
            dst = destination / asset['path']
            dst.parent.mkdir(exist_ok=True)
            shutil.copyfile(tmp_path / asset['path'], dst)
        def upload(self, job_id, lease, path):
            assert path.stat().st_size > 5000
            return {'status': 'awaiting_review'}
        def fail(self, job_id, lease): raise AssertionError('No worker failure expected')

    assert run_once(FakeClient(), encoder='libx264', temp_root=tmp_path) is True
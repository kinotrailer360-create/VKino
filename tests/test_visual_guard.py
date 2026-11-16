import asyncio
import json
from pathlib import Path
import pytest
from PIL import Image
from shorts import ai, visual_guard as guard


def test_narration_covers_change_inside_shot():
    assert guard.narration(2, 5, ['Зендая, Эйфория', 'Энистон, Утреннее шоу'], [0, 3], 6) == 'Зендая, Эйфория Энистон, Утреннее шоу'
    assert guard.narration(3, 5, ['Первая', 'Вторая'], [0, 3], 6) == 'Вторая'


@pytest.mark.parametrize('answer', [False, None])
def test_reject_mismatch_and_model_outage(tmp_path, monkeypatch, answer):
    Image.new('RGB', (40, 40)).save(tmp_path / 'morning.jpg')
    async def check(key, image, text):
        assert text == 'Зендая в Эйфории'
        return answer, 'На картинке The Morning Show'
    monkeypatch.setattr(ai, 'media_fits', check)
    scene = dict(image='morning.jpg', text='Зендая в Эйфории', start=0, end=3)
    with pytest.raises(guard.VisualReviewRequired, match='1:'):
        asyncio.run(guard.audit(tmp_path, [scene]))
    report = json.loads((tmp_path / 'visual_review.json').read_text())
    assert report[0]['status'] != 'matched'
    assert 'approved' not in scene


def test_cache_is_bound_to_text(tmp_path, monkeypatch):
    Image.new('RGB', (40, 40)).save(tmp_path / 'photo.jpg')
    calls = []
    async def check(key, image, text):
        calls.append(text)
        return True, 'OK'
    monkeypatch.setattr(ai, 'media_fits', check)
    scenes = [dict(image='photo.jpg', text=t, start=i, end=i+1) for i,t in enumerate(['Один', 'Один', 'Другой'])]
    asyncio.run(guard.audit(tmp_path, scenes))
    assert calls == ['Один', 'Другой']
    assert all('approved' not in s for s in scenes)


def test_short_clip_rejected_before_model(tmp_path, monkeypatch):
    (tmp_path / 'clip.mp4').write_bytes(b'test')
    async def command(*args):
        assert args[0] == 'ffprobe'
        return '{"format":{"duration":1.2}}'
    monkeypatch.setattr(guard, 'command', command)
    with pytest.raises(guard.VisualReviewRequired, match='короче'):
        asyncio.run(guard.audit(tmp_path, [dict(clip='clip.mp4', text='Текст', start=0, end=5)]))


def test_three_frames_and_duplicate_clip(tmp_path, monkeypatch):
    (tmp_path / 'clip.mp4').write_bytes(b'test')
    points = []
    async def command(*args):
        if args[0] == 'ffprobe':
            return '{"format":{"duration":10}}'
        points.append(float(args[args.index('-ss')+1]))
        Image.new('RGB', (40, 40)).save(args[-1])
        return ''
    async def check(*args):
        return True, 'OK'
    monkeypatch.setattr(guard, 'command', command)
    monkeypatch.setattr(ai, 'media_fits', check)
    scenes = [dict(clip='clip.mp4', text='Текст', start=0, end=3, offset=2),
              dict(clip='clip.mp4', text='Текст', start=3, end=6, offset=2)]
    with pytest.raises(guard.VisualReviewRequired, match='уже использован'):
        asyncio.run(guard.audit(tmp_path, scenes))
    assert points == [2, 3.5, 4.85]
    assert not list(tmp_path.glob('.visual_*.jpg'))


def test_all_assembly_pipelines_call_shared_guard():
    root = Path(__file__).resolve().parents[1] / 'shorts'
    for module in ['daily', 'lore', 'review', 'weekly', 'studio_adapter']:
        assert 'await visual_guard.audit(' in (root / (module+'.py')).read_text()

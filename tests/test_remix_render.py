"""Exercise the real ffmpeg renderer; only network AI calls are replaced."""
import asyncio
import json
import shutil

import pytest

from shorts import ai, remix


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='ffmpeg required')
def test_remix_renders_short_captions_and_ribbons_without_losing_audio(tmp_path, monkeypatch):
    source = tmp_path / 'source.mp4'
    asyncio.run(remix.run(
        'ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=size=180x320:rate=25',
        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=22050', '-t', '2',
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(source),
    ))
    async def transcribe(key, audio, duration):
        return [dict(start=0, end=1, text='Красивый кадр.'),
                dict(start=1, end=2, text='Сильный финал.')]
    async def words(audio):
        return [dict(w='Красивый', s=0, e=.4), dict(w='кадр.', s=.4, e=.85),
                dict(w='Сильный', s=1, e=1.4), dict(w='финал.', s=1.4, e=1.85)]
    async def match(key, texts, images, context=''):
        return [0, 1]
    async def headlines(key, texts):
        return ['КРАСИВЫЙ КАДР', 'СИЛЬНЫЙ ФИНАЛ']
    monkeypatch.setattr(ai, 'transcribe_dub', transcribe)
    monkeypatch.setattr(ai, 'transcribe_words', words)
    monkeypatch.setattr(ai, 'match_images', match)
    monkeypatch.setattr(ai, 'remix_headlines', headlines)
    result = asyncio.run(remix.replace_visuals(source, [source], tmp_path / 'result.mp4', tmp_path, 'test'))
    assert abs(asyncio.run(remix.duration(result)) - 2) < .15
    events = json.loads((tmp_path / 'remix_caption_timing.json').read_text())['events']
    assert events[0]['text'] == 'Красивый кадр.'
    assert all(len(event['text'].split()) <= 2 for event in events)
    assert all(event['end'] - event['start'] <= 1.200001 for event in events)
    report = json.loads((tmp_path / 'remix_visual_review.json').read_text())
    assert all(scene['status'] == 'matched' for scene in report)

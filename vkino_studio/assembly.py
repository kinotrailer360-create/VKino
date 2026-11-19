"""Compose author-approved MP4 scenes on Windows without replacing visuals.

No guesswork: only previously rendered/approved numbered clips from a VKino
Studio assemble snapshot. This transport milestone does not yet offload
per-scene rendering to Windows.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import shutil
import tempfile
from typing import Callable

from .renderer import _command


def _media_info(path: Path) -> tuple[tuple, float]:
    data = json.loads(_command(['ffprobe', '-v', 'error', '-show_entries',
         'stream=codec_type,codec_name,width,height,r_frame_rate,pix_fmt,sample_rate,channels:format=duration',
         '-of', 'json', str(path)], timeout=40))
    streams = data.get('streams') or []
    videos = [s for s in streams if s.get('codec_type') == 'video']
    audios = [s for s in streams if s.get('codec_type') == 'audio']
    if len(videos) != 1 or len(audios) != 1:
        raise ValueError('Каждый утверждённый MP4 должен содержать одну видео- и аудиодорожку')
    v, a = videos[0], audios[0]
    duration = float(data['format']['duration'])
    if not math.isfinite(duration) or not 0 < duration <= 31:
        raise ValueError('Недопустимая длительность исходного фрагмента')
    fmt = (v.get('codec_name'), v.get('width'), v.get('height'), v.get('r_frame_rate'),
           v.get('pix_fmt'), a.get('codec_name'), a.get('sample_rate'), a.get('channels'))
    if v.get('codec_name') != 'h264' or a.get('codec_name') != 'aac':
        raise ValueError('Для сборки без перекодирования нужны H.264 + AAC')
    return fmt, duration


def assemble_approved_clips(manifest: Path, destination: Path,
                            callback: Callable[[str], None] | None = None) -> Path:
    callback = callback or (lambda _: None)
    data = json.loads(manifest.read_text(encoding='utf-8'))
    if data.get('version') != 1 or not 1 <= len(data.get('clips', [])) <= 30:
        raise ValueError('Неправильный проект финальной сборки')
    clips = data['clips']
    expected_format = None
    total = 0.0
    input_paths = []
    base = manifest.parent.resolve()
    for i, clip in enumerate(clips, start=1):
        if not isinstance(clip, dict) or clip.get('approved') is not True:
            raise ValueError(f'Фрагмент {i} не утверждён')
        relative = clip.get('source')
        from pathlib import PurePosixPath
        if not isinstance(relative, str) or len(relative) > 160 or '\\' in relative:
            raise ValueError('Неверный путь фрагмента')
        pure = PurePosixPath(relative)
        if (len(pure.parts) != 2 or pure.parts[0] != 'media' or pure.suffix.lower() != '.mp4'
                or pure.is_absolute() or '..' in pure.parts):
            raise ValueError('Фрагмент должен быть локальным MP4 из media/')
        path = (base / relative).resolve()
        if not path.is_relative_to(base) or not path.is_file() or path.stat().st_size > 50 * 1024 * 1024:
            raise ValueError('Отсутствует локальный утверждённый фрагмент')
        fmt, actual = _media_info(path)
        duration = clip.get('duration')
        if (type(duration) not in (int, float) or not math.isfinite(duration)
                or not 1 <= duration <= 30 or abs(actual - duration) > max(1.0, .05 * duration)):
            raise ValueError(f'Фрагмент {i}: длительность не соответствует утверждённому сценарию')
        if expected_format is not None and fmt != expected_format:
            raise ValueError('Фрагменты имеют несовместимые параметры видео или звука')
        expected_format = fmt
        total += duration
        input_paths.append(path)
    if total > 180:
        raise ValueError('Длительность финальной сборки превышает 180 секунд')
    if expected_format[1:3] != (data.get('width'), data.get('height')):
        raise ValueError('Формат кадров не совпадает с настройками проекта')
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / 'vkino_studio_preview.mp4'
    with tempfile.TemporaryDirectory(prefix='vkino-assemble-') as temp:
        work = Path(temp)
        # FFmpeg concat file URLs are never accepted. All filenames come from a
        # fresh internal temp dir and use safe generated names only.
        for i, path in enumerate(input_paths):
            shutil.copyfile(path, work / f'clip_{i:03d}.mp4')
        listing = work / 'concat.txt'
        listing.write_text(''.join(f"file 'clip_{i:03d}.mp4'\n" for i in range(len(input_paths))), encoding='utf-8')
        staged = work / 'draft.mp4'
        callback('Объединение утверждённых фрагментов на ПК')
        _command(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                  '-f', 'concat', '-safe', '1', '-i', str(listing),
                  '-c', 'copy', '-movflags', '+faststart', str(staged)], cwd=work,
                 timeout=240)
        result_fmt, result_len = _media_info_result(staged)
        if result_fmt != expected_format or abs(result_len - total) > max(2.0, .05 * total):
            raise ValueError('Итоговый файл не прошёл проверку дорожек или длительности')
        partial = destination / 'vkino_studio_preview.partial.mp4'
        try:
            shutil.copyfile(staged, partial)
            os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)
    (destination / 'render_report.json').write_text(json.dumps({
        'mode': 'assemble_clips', 'scenes': len(clips), 'duration': result_len,
        'author_final_approval': False, 'published': False,
        'rights': 'require_final_review', 'source': 'approved_clips'
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    return target


def _media_info_result(path: Path) -> tuple[tuple, float]:
    # Final video may exceed 31 seconds; do not reuse clip-duration limit.
    data = json.loads(_command(['ffprobe', '-v', 'error', '-show_entries',
      'stream=codec_type,codec_name,width,height,r_frame_rate,pix_fmt,sample_rate,channels:format=duration',
      '-of', 'json', str(path)], timeout=40))
    v = [s for s in data.get('streams', []) if s.get('codec_type') == 'video']
    a = [s for s in data.get('streams', []) if s.get('codec_type') == 'audio']
    if len(v) != 1 or len(a) != 1:
        raise ValueError('Итоговый MP4 не содержит аудио и видео')
    x, y = v[0], a[0]
    fmt = (x.get('codec_name'), x.get('width'), x.get('height'), x.get('r_frame_rate'),
           x.get('pix_fmt'), y.get('codec_name'), y.get('sample_rate'), y.get('channels'))
    length = float(data['format']['duration'])
    if not math.isfinite(length) or length <= 0:
        raise ValueError('Недопустимая длительность финального файла')
    return fmt, length
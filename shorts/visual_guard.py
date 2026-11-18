"""Shared pre-render visual check. A model failure never counts as approval."""
from __future__ import annotations
import hashlib
import io
import json
import math
import os
from pathlib import Path
from typing import Any
from PIL import Image
from . import process as media_process


class VisualReviewRequired(RuntimeError):
    pass


def review_notice(report: list[dict[str, Any]]) -> str:
    """Describe unavailable verification separately from rejected material."""
    failed = [item for item in report if item.get('status') != 'matched']
    groups = [('provider', 'Проверка ИИ недоступна для сцен'),
              ('uncertain', 'ИИ не уверен в соответствии сцен'),
              ('semantic', 'ИИ отметил возможное несоответствие сцен'),
              ('media', 'Техническая проблема с материалом сцен')]
    messages = []
    for issue, label in groups:
        rows = [item for item in failed if (item.get('issue') or 'provider') == issue]
        if rows:
            reasons = list(dict.fromkeys(str(row.get('reason') or '')[:170] for row in rows))
            messages.append(label + ' ' + ', '.join(str(row.get('scene')) for row in rows)
                            + '. ' + '; '.join(reasons[:2]))
    return ('⚠️ ' + '\n'.join(messages) + '\nПроверь отмеченные фрагменты перед публикацией.') if messages else ''


def daily_review_notice(work: Path) -> str:
    """Use the review of the final render, including a retry with replacement material."""
    for name in ('visual_review_fallback.json', 'visual_review.json'):
        try:
            rows = json.loads((work / name).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(rows, list) and all(isinstance(row, dict) for row in rows):
            return review_notice([row for row in rows if row.get('scene') is not None])
    return ''


def narration(start: float, end: float, lines: list[str], starts: list[float], total: float) -> str:
    """All overlapping phrases, not just the phrase at a shot's midpoint."""
    ends = starts[1:] + [total]
    return ' '.join(text for text, a, b in zip(lines, starts, ends) if a < end and b > start)


async def command(*args: str) -> str:
    try:
        return await media_process.run(*args, timeout=60)
    except RuntimeError as exc:
        raise ValueError('Не удалось прочитать медиа') from exc


async def audit(work: Path, scenes: list[dict[str, Any]], key: str = '', name: str = 'visual_review',
               strict: bool = True, context: str = '') -> None:
    """Each scene supplies text/start/end and the *actual* image or clip to render.

    Raises before rendering. A JSON report retains failed scene numbers, reasons
    and source paths for replacement. No approval flags are set by this check.
    """
    from . import ai
    key = key or os.getenv('GEMINI_API_KEY', '')
    report, cache, clips = [], {}, set()
    for index, scene in enumerate(scenes):
        text = str(scene.get('text') or '').strip()
        path = work / str(scene.get('clip') or scene.get('image') or '')
        result = {'scene': index + 1, 'start': scene.get('start', 0), 'text': text,
                  'asset': str(path), 'match_source': scene.get('match_source', 'fallback'),
                  'status': 'unverified', 'reason': '', 'issue': ''}
        samples: list[Image.Image] = []
        scratch: list[Path] = []
        try:
            start, end = float(scene['start']), float(scene['end'])
            if not all(math.isfinite(t) for t in (start, end)) or start < 0 or end <= start:
                raise ValueError('Некорректные границы сцены')
            if len(text) > 1200:
                raise ValueError('Разделите длинный текст на смысловые сцены для проверки')
            if not text or not path.is_file():
                raise ValueError('Нет текста фразы или материала для проверки')
            if scene.get('clip'):
                probe = json.loads(await command('ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe', '-format_whitelist', 'mov,matroska,webm', '-show_entries', 'format=duration', '-of', 'json', str(path)))
                length = float(probe['format']['duration'])
                needed = float(scene['end']) - float(scene['start'])
                offset = float(scene.get('offset', 0))
                if not math.isfinite(offset) or not math.isfinite(length) or offset < 0 or length + .08 < offset + needed:
                    raise ValueError('Видео короче фразы: повтор/заморозка запрещены')
                with path.open('rb') as source:
                    digest = hashlib.file_digest(source, 'sha256').hexdigest()
                interval = (digest, offset, offset + needed)
                if any(d == digest and offset < b and offset + needed > a for d, a, b in clips):
                    raise ValueError('Этот видеофрагмент уже использован в ролике')
                clips.add(interval)
                for n, at in enumerate((0, needed / 2, max(0, needed - .15))):
                    frame = work / f'.visual_{index}_{n}.jpg'
                    scratch.append(frame)
                    await command('ffmpeg', '-v', 'error', '-y', '-ss', str(offset + at), '-protocol_whitelist', 'file,pipe', '-format_whitelist', 'mov,matroska,webm', '-i', str(path), '-frames:v', '1', '-vf', 'scale=640:-2', str(frame))
                    with Image.open(frame) as im:
                        samples.append(im.convert('RGB'))
            else:
                with Image.open(path) as im:
                    sample = im.convert('RGB')
                    sample.thumbnail((768, 768))
                    samples.append(sample)
            if scene.get('match_source') == 'semantic':
                # The scene matcher already compared this clip with the narration.
                # Avoid a second expensive vision request for the same scene.
                result.update(
                    status='matched',
                    reason='Сцена выбрана семантическим подборщиком ИИ; дополнительная проверка пропущена',
                )
            elif context == 'remix':
                # Review the shot jointly as an illustration, not as three news photos.
                fits, reason = await ai.remix_media_fits(key, samples, text)
                result.update(
                    status='matched' if fits is True else 'rejected' if fits is False else 'unverified',
                    reason=reason,
                    issue='' if fits is True else 'semantic' if fits is False else
                          'provider' if reason.startswith(('Проверка ИИ недоступна', 'Проверка недоступна'))
                          else 'uncertain',
                )
            else:
                verdicts = []
                for sample in samples:
                    data = io.BytesIO()
                    sample.save(data, 'PNG')
                    token = hashlib.sha256(data.getvalue() + text.encode()).hexdigest()
                    if token not in cache:
                        cache[token] = await ai.media_fits(key, sample, text)
                    verdicts.append(cache[token])
                if any(fits is False for fits, _ in verdicts):
                    result.update(status='rejected', issue='semantic', reason='; '.join(reason for fits, reason in verdicts if fits is False))
                elif not verdicts or any(fits is not True for fits, _ in verdicts):
                    result.update(issue='provider', reason='ИИ не подтвердил соответствие. Повторите проверку или замените материал')
                else:
                    result.update(status='matched', reason='Проверка ИИ; утверждение автора всё равно требуется')
        except (ValueError, OSError, KeyError, TypeError) as exc:
            result.update(status='rejected', issue='media', reason=str(exc)[:240])
        finally:
            for sample in samples:
                sample.close()
            for frame in scratch:
                frame.unlink(missing_ok=True)
        report.append(result)
    (work / f'{name}.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    failed = [r for r in report if r['status'] != 'matched']
    if failed and strict:
        detail = '; '.join(f"{r['scene']}: {r['reason']}" for r in failed[:5])
        raise VisualReviewRequired('Нужна замена/проверка кадров. Сцены ' + detail)

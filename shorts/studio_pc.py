"""Author-approved Studio → Windows final assembly bridge (no publication).

The existing Sites Studio already renders and requires approval of every scene.
This adapter queues only *approved scene MP4s* for a Windows final assembly,
without guessing media or regenerating narration. It is owner-scoped and
idempotent; the VPS's existing Studio remains unchanged until explicitly used.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, NAMESPACE_URL, uuid5

from .studio import obj, validate_job
from .studio_worker import validate_remote_spec


def build_spec(source: dict[str, Any], files: dict[str, dict[str, Any]], admin: int) -> dict[str, Any]:
    """Build a remote draft from an existing validated Studio assemble job.

    This doesn't itself approve a scene: it consumes existing renderApproved flags.
    """
    if source.get('admin_id') != admin or source.get('status') != 'succeeded':
        raise ValueError('Нужна завершённая утверждённая сборка этого автора')
    payload = obj(source['payload'])
    if payload.get('operation') != 'assemble':
        raise ValueError('Нужна утверждённая итоговая сборка со всеми сценами')
    validate_job(payload)  # stages, scene render IDs, explicit approvals and duration limits
    project = payload['project']
    clips = []
    assets = []
    seen = set()
    for index, scene in enumerate(project['scenes'], start=1):
        ref = str(UUID(str(scene['render']['id'])))
        if ref in seen:
            raise ValueError('Повторно использован результат одной сцены — проверьте монтаж')
        seen.add(ref)
        meta = files.get(ref)
        if not isinstance(meta, dict) or meta.get('mime') != 'video/mp4':
            raise ValueError(f'Сцена {index}: утверждённый MP4 отсутствует в медиатеке')
        name = f'media/clip_{index:03d}.mp4'
        clips.append({'source': name, 'duration': float(scene['duration']), 'approved': True,
                      'rights': 'unconfirmed'})
        assets.append({'path': name, 'fileId': ref, 'sha256': meta['hash'], 'size': meta['size']})
    ref = str(UUID(str(source['id'])))
    project_id = str(UUID(str(project['id'])))
    revision = project['revision']
    spec = {'mode': 'assemble_clips',
            'requestId': str(uuid5(NAMESPACE_URL, f'vkino-pc-assemble-v03:{admin}:{ref}:{project_id}:{revision}')),
            'revision': revision, 'authorApproved': True,
            'project': {'version': 1, 'title': project.get('title') or 'VKino Studio',
                        'width': 720, 'height': 1280, 'fps': 30,
                        'clips': clips}, 'assets': assets}
    return validate_remote_spec(spec)


async def list_ready(studio: Any, admin: int, limit: int = 5) -> list[dict[str, str]]:
    rows = await studio.pool.fetch(
        "SELECT id,payload FROM studio_jobs WHERE admin_id=$1 AND status='succeeded' "
        "AND payload->>'operation'='assemble' ORDER BY updated_at DESC LIMIT $2", admin, limit)
    result = []
    for row in rows:
        try:
            payload = obj(row['payload'])
            validate_job(payload)
            title = str(payload['project'].get('title') or 'Проект')[:65]
            result.append({'id': str(row['id']), 'title': title, 'revision': str(payload['project']['revision'])})
        except (ValueError, TypeError, KeyError):
            continue
    return result


async def list_remote(studio: Any, admin: int, limit: int = 5) -> list[dict[str, str]]:
    rows = await studio.pool.fetch(
        'SELECT id,status,payload FROM studio_worker_jobs WHERE admin_id=$1 '
        'ORDER BY updated_at DESC LIMIT $2', admin, limit)
    return [{'id': str(r['id']), 'status': r['status'],
             'title': str(obj(r['payload']).get('project', {}).get('title') or 'Проект')[:65]}
            for r in rows]


async def enqueue_from_studio(studio: Any, admin: int, source_id: str) -> tuple[str, str]:
    """Idempotent explicit user action; never starts paid TTS or publishes."""
    if admin not in studio.admins or studio.worker_api is None:
        raise ValueError('Подключение ПК-монтажа недоступно')
    source_id = str(UUID(str(source_id)))
    source = await studio.pool.fetchrow(
        'SELECT id,admin_id,status,payload FROM studio_jobs WHERE id=$1 AND admin_id=$2', source_id, admin)
    if not source:
        raise ValueError('Исходный проект не найден')
    payload = obj(source['payload'])
    if payload.get('operation') != 'assemble':
        raise ValueError('Отправлять на ПК можно только завершённую утверждённую сборку')
    validate_job(payload)
    refs = [str(UUID(str(s['render']['id']))) for s in payload['project']['scenes']]
    metadata = {}
    for ref in refs:
        value = await studio.pool.fetchval(
            'SELECT metadata FROM studio_files WHERE id=$1 AND admin_id=$2', ref, admin)
        if value is None:
            raise ValueError('Утверждённый фрагмент уже удалён с VPS. Соберите его заново.')
        metadata[ref] = obj(value)
    spec = build_spec(dict(source), metadata, admin)
    for a in spec['assets']:
        path, meta = await studio.resolve_file(admin, a['fileId'])
        if (not path.is_file() or meta.get('hash') != a['sha256']
                or path.stat().st_size != a['size']):
            raise ValueError('Материал был изменён или удалён после утверждения')
    async with studio.pool.acquire() as db, db.transaction():
        await db.execute('SELECT pg_advisory_xact_lock($1)', admin)
        old = await db.fetchrow('SELECT admin_id,status,payload FROM studio_worker_jobs WHERE id=$1', spec['requestId'])
        if old:
            if old['admin_id'] != admin or obj(old['payload']) != spec:
                raise ValueError('Конфликт существующего задания')
            return spec['requestId'], old['status']
        count = await db.fetchval("SELECT COUNT(*) FROM studio_worker_jobs WHERE admin_id=$1 "
                                   "AND status IN ('queued','running')", admin)
        if count >= 3:
            raise ValueError('Очередь ПК-монтажа заполнена: максимум три задания')
        await db.execute('INSERT INTO studio_worker_jobs(id,admin_id,payload,status) '
                         "VALUES($1,$2,$3::jsonb,'queued')", spec['requestId'], admin,
                         json.dumps(spec, ensure_ascii=False, sort_keys=True))
    return spec['requestId'], 'queued'


async def owner_result(studio: Any, admin: int, job_id: str) -> tuple[Path, str]:
    job_id = str(UUID(str(job_id)))
    row = await studio.pool.fetchrow(
        'SELECT status FROM studio_worker_jobs WHERE id=$1 AND admin_id=$2', job_id, admin)
    if not row or row['status'] != 'awaiting_review':
        raise ValueError('Черновик ещё не готов или не принадлежит вам')
    path = studio.worker_api._folder(admin, job_id) / 'render.mp4'
    if not path.is_file():
        raise ValueError('Файл черновика отсутствует, проверьте журнал')
    return path, row['status']
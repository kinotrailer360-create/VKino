"""Opt-in, outbound-only Windows render executor for Studio.

No auto-publication, no generic command execution, no filesystem paths from clients.
Uses Studio's existing pairing and owner-scoped file library.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
from pathlib import PurePosixPath
import re
import secrets
import shutil
from uuid import UUID

from aiohttp import web

PREFIX = '/api/studio/v1/worker'
CHUNK = 4 * 1024 * 1024
MAX_RESULT = 256 * 1024 * 1024
EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.mp4', '.mov', '.mkv', '.webm',
              '.wav', '.mp3', '.m4a', '.aac', '.ogg'}


def hashed(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def is_uuid(value: str) -> str:
    return str(UUID(str(value)))


def canonical_path(value: object) -> str:
    if not isinstance(value, str) or len(value) > 160 or '\\' in value or '\x00' in value:
        raise ValueError('Недопустимый путь')
    path = PurePosixPath(value)
    if (path.is_absolute() or len(path.parts) != 2 or path.parts[0] != 'media'
            or path.parts[1] in {'.', '..'} or path.suffix.lower() not in EXTENSIONS
            or not re.fullmatch(r'[A-Za-z0-9_.-]{1,120}', path.parts[1])):
        raise ValueError('Материалы должны находиться в media/ и иметь разрешённый тип')
    return str(path)


def validate_remote_spec(raw: object) -> dict:
    if not isinstance(raw, dict) or raw.get('authorApproved') is not True:
        raise ValueError('Требуется явное утверждение автора')
    identifier = is_uuid(raw.get('requestId'))
    revision = raw.get('revision')
    if type(revision) is not int or revision < 1:
        raise ValueError('Неверная версия проекта')
    p = raw.get('project')
    if not isinstance(p, dict) or p.get('version') != 1:
        raise ValueError('Неверная версия формата проекта')
    if type(p.get('width')) is not int or p['width'] not in (360, 720, 1080):
        raise ValueError('Неверная ширина')
    if p.get('height') != p['width'] * 16 // 9 or p.get('fps') not in (24, 25, 30):
        raise ValueError('Неверный формат видеоролика')
    if type(p.get('captions', True)) is not bool or not isinstance(p.get('title'), str) or not 1 <= len(p['title']) <= 180:
        raise ValueError('Неверные настройки проекта')
    scenes = p.get('scenes')
    if not isinstance(scenes, list) or not 1 <= len(scenes) <= 30:
        raise ValueError('Разрешено от одной до 30 сцен')
    total = 0.0
    needed = set()
    for scene in scenes:
        if not isinstance(scene, dict) or scene.get('approved') is not True:
            raise ValueError('Каждая сцена должна быть утверждена')
        if not isinstance(scene.get('text'), str) or not 1 <= len(scene['text']) <= 1000:
            raise ValueError('Неверный текст сцены')
        if not isinstance(scene.get('headline', ''), str) or len(scene.get('headline', '')) > 100:
            raise ValueError('Неверная плашка сцены')
        if scene.get('rights') not in ('author', 'licensed', 'unconfirmed'):
            raise ValueError('Не указан статус прав на материал')
        dur = scene.get('duration')
        if isinstance(dur, bool) or not isinstance(dur, (int, float)) or not math.isfinite(dur) or not 1 <= dur <= 30:
            raise ValueError('Неверная длительность сцены')
        total += float(dur)
        for key in ('source', 'voice'):
            needed.add(canonical_path(scene.get(key)))
    if total > 180:
        raise ValueError('Суммарная длительность превышает 180 секунд')
    assets = raw.get('assets')
    if not isinstance(assets, list) or not 1 <= len(assets) <= 60:
        raise ValueError('Неверный список файлов')
    paths = set()
    file_ids = set()
    for asset in assets:
        if not isinstance(asset, dict):
            raise ValueError('Неверный файл')
        path = canonical_path(asset.get('path'))
        aid = is_uuid(asset.get('fileId'))
        sha = asset.get('sha256')
        size = asset.get('size')
        if (not isinstance(sha, str) or not re.fullmatch('[a-f0-9]{64}', sha)
                or type(size) is not int or not 0 < size <= 50 * 1024 * 1024
                or path in paths or aid in file_ids):
            raise ValueError('Недопустимые параметры файла')
        paths.add(path)
        file_ids.add(aid)
    if paths != needed:
        raise ValueError('Набор файлов должен точно соответствовать утверждённым сценам')
    return {'requestId': identifier, 'revision': revision, 'project': p,
            'assets': assets, 'authorApproved': True}


class StudioWorker:
    def __init__(self, studio):
        self.studio = studio

    def register(self, app):
        endpoints = [
            ('POST', '/pair', self.pair),
            ('POST', '/jobs', self.submit),
            ('GET', '/jobs/{id}', self.status),
            ('POST', '/jobs/{id}/retry', self.retry),
            ('GET', '/jobs/{id}/result', self.result),
            ('POST', '/claim', self.claim),
            ('POST', '/jobs/{id}/heartbeat', self.heartbeat),
            ('GET', '/jobs/{id}/assets/{asset}', self.asset),
            ('PUT', '/jobs/{id}/chunks/{index}', self.chunk),
            ('POST', '/jobs/{id}/finish', self.finish),
            ('POST', '/jobs/{id}/fail', self.fail),
        ]
        for method, path, fn in endpoints:
            async def handler(request, callback=fn):
                try:
                    response = await callback(request)
                    response.headers['Cache-Control'] = 'private, no-store'
                    response.headers['X-Content-Type-Options'] = 'nosniff'
                    return response
                except (ValueError, TypeError, KeyError) as exc:
                    return web.json_response({'error': str(exc)[:250]}, status=400)
            app.router.add_route(method, PREFIX + path, handler)

    async def auth_worker(self, request):
        auth = request.headers.get('Authorization', '')
        if not auth.startswith('Bearer ') or not 45 <= len(auth) <= 190:
            raise web.HTTPUnauthorized()
        token_hash = hashed(auth[7:])
        row = await self.studio.pool.fetchrow(
            'SELECT admin_id FROM studio_worker_tokens WHERE token_hash=$1 AND expires_at>NOW()', token_hash)
        if not row or row['admin_id'] not in self.studio.admins:
            raise web.HTTPUnauthorized()
        return int(row['admin_id']), token_hash

    async def lease(self, request):
        admin, worker_hash = await self.auth_worker(request)
        job_id = is_uuid(request.match_info['id'])
        lease_value = request.headers.get('X-Worker-Lease', '')
        if not 40 <= len(lease_value) <= 160:
            raise web.HTTPForbidden()
        row = await self.studio.pool.fetchrow(
            "SELECT * FROM studio_worker_jobs WHERE id=$1 AND admin_id=$2 AND status='running' "
            'AND worker_hash=$3 AND lease_hash=$4 AND lease_expires>NOW()',
            job_id, admin, worker_hash, hashed(lease_value))
        if row is None:
            raise web.HTTPForbidden(text='Lease истёк; ручной перезапуск обязателен')
        return row

    async def pair(self, request):
        data = await request.json()
        code = str(data.get('code', ''))
        if not re.fullmatch(r'[A-Za-z0-9_-]{40,60}', code):
            raise ValueError('Недействительный код')
        async with self.studio.pool.acquire() as db, db.transaction():
            row = await db.fetchrow(
                "DELETE FROM studio_tokens WHERE token_hash=$1 AND kind='pair' "
                'AND expires_at>NOW() RETURNING admin_id', hashed(code))
            if not row or row['admin_id'] not in self.studio.admins:
                raise ValueError('Код истёк. Получите новый через /studio')
            token = secrets.token_urlsafe(40)
            await db.execute('INSERT INTO studio_worker_tokens (token_hash,admin_id,expires_at) '
                             "VALUES ($1,$2,NOW()+INTERVAL '30 days')", hashed(token), row['admin_id'])
        return web.json_response({'token': token, 'expiresDays': 30, 'scope': 'worker'})

    async def submit(self, request):
        admin = await self.studio.authenticate(request)
        raw = validate_remote_spec(await request.json())
        for a in raw['assets']:
            meta = await self.studio.pool.fetchval(
                'SELECT metadata FROM studio_files WHERE id=$1 AND admin_id=$2', a['fileId'], admin)
            if not meta:
                raise ValueError('Файл не найден или не принадлежит автору')
            meta = json.loads(meta) if isinstance(meta, str) else meta
            if meta.get('hash') != a['sha256'] or meta.get('size') != a['size']:
                raise ValueError('Исходник изменён после утверждения')
        payload = json.dumps(raw, ensure_ascii=False, sort_keys=True)
        async with self.studio.pool.acquire() as db, db.transaction():
            await db.execute('SELECT pg_advisory_xact_lock($1)', admin)
            old = await db.fetchrow('SELECT * FROM studio_worker_jobs WHERE id=$1', raw['requestId'])
            if old:
                previous = old['payload']
                previous = json.loads(previous) if isinstance(previous, str) else previous
                if old['admin_id'] != admin or previous != raw:
                    raise ValueError('requestId уже занят другим заданием')
                return web.json_response({'id': raw['requestId'], 'status': old['status']})
            active = await db.fetchval("SELECT COUNT(*) FROM studio_worker_jobs WHERE admin_id=$1 "
                                       "AND status IN ('queued','running')", admin)
            if active >= 3:
                return web.json_response({'error': 'Не более трёх заданий в очереди'}, status=429)
            await db.execute('INSERT INTO studio_worker_jobs(id,admin_id,payload,status) '
                             "VALUES($1,$2,$3::jsonb,'queued')", raw['requestId'], admin, payload)
        return web.json_response({'id': raw['requestId'], 'status': 'queued'}, status=202)

    async def status(self, request):
        admin = await self.studio.authenticate(request)
        job_id = is_uuid(request.match_info['id'])
        row = await self.studio.pool.fetchrow(
            'SELECT * FROM studio_worker_jobs WHERE id=$1 AND admin_id=$2', job_id, admin)
        if not row:
            raise web.HTTPNotFound()
        return web.json_response({'id': job_id, 'status': row['status'], 'error': row['error'],
            'resultAvailable': row['status'] == 'awaiting_review', 'published': False})

    async def retry(self, request):
        admin = await self.studio.authenticate(request)
        job_id = is_uuid(request.match_info['id'])
        result = await self.studio.pool.execute(
            "UPDATE studio_worker_jobs SET status='queued',worker_hash=NULL,lease_hash=NULL,lease_expires=NULL, "
            "error=NULL,updated_at=NOW() WHERE id=$1 AND admin_id=$2 AND status IN ('failed','interrupted')",
            job_id, admin)
        if result != 'UPDATE 1':
            raise ValueError('Задание нельзя перезапустить без ручной проверки')
        folder = self._folder(admin, job_id)
        if folder.exists():
            await asyncio.to_thread(shutil.rmtree, folder)
        return web.json_response({'id': job_id, 'status': 'queued'})

    async def claim(self, request):
        admin, worker_hash = await self.auth_worker(request)
        await self.studio.pool.execute(
            "UPDATE studio_worker_jobs SET status='interrupted',error='Истёк срок аренды: проверьте результат', "
            "updated_at=NOW() WHERE admin_id=$1 AND status='running' AND lease_expires<NOW()", admin)
        lease = secrets.token_urlsafe(40)
        row = await self.studio.pool.fetchrow(
            "UPDATE studio_worker_jobs SET status='running',worker_hash=$2,lease_hash=$3, "
            "lease_expires=NOW()+INTERVAL '15 minutes',updated_at=NOW() WHERE id=("
            "SELECT id FROM studio_worker_jobs WHERE admin_id=$1 AND status='queued' "
            "ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING id,payload",
            admin, worker_hash, hashed(lease))
        if not row:
            return web.json_response({'job': None})
        payload = row['payload']
        payload = json.loads(payload) if isinstance(payload, str) else payload
        return web.json_response({'job': {'id': row['id'], 'lease': lease, **payload}})

    async def heartbeat(self, request):
        row = await self.lease(request)
        await self.studio.pool.execute(
            "UPDATE studio_worker_jobs SET lease_expires=NOW()+INTERVAL '15 minutes',updated_at=NOW() "
            'WHERE id=$1 AND lease_hash=$2', row['id'], row['lease_hash'])
        return web.json_response({'ok': True})

    async def asset(self, request):
        row = await self.lease(request)
        asset_id = is_uuid(request.match_info['asset'])
        p = row['payload']
        p = json.loads(p) if isinstance(p, str) else p
        if asset_id not in {a['fileId'] for a in p['assets']}:
            raise web.HTTPForbidden()
        path, meta = await self.studio.resolve_file(row['admin_id'], asset_id)
        return web.FileResponse(path, headers={'Content-Type': meta['mime']})

    def _folder(self, admin, job_id):
        return self.studio.root / str(admin) / 'remote' / job_id

    async def chunk(self, request):
        row = await self.lease(request)
        index_text = request.match_info['index']
        if not index_text.isascii() or not index_text.isdecimal() or len(index_text) > 3:
            raise ValueError('Неверный номер части')
        index = int(index_text)
        if index * CHUNK >= MAX_RESULT:
            raise ValueError('Превышен лимит результата 256 МБ')
        expected = request.headers.get('X-Chunk-SHA256', '')
        if not re.fullmatch(r'[a-f0-9]{64}', expected):
            raise ValueError('Нужна контрольная сумма части')
        folder = self._folder(row['admin_id'], row['id'])
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f'{index:03d}.chunk'
        temp = folder / f'{index:03d}.{secrets.token_hex(4)}.tmp'
        total = 0
        sha = hashlib.sha256()
        try:
            with temp.open('wb') as f:
                async for piece in request.content.iter_chunked(65536):
                    total += len(piece)
                    if total > CHUNK:
                        raise ValueError('Часть больше 4 МБ')
                    sha.update(piece)
                    f.write(piece)
            if not total or sha.hexdigest() != expected:
                raise ValueError('Контрольная сумма не совпала')
            if path.exists():
                with path.open('rb') as f:
                    old_hash = hashlib.file_digest(f, 'sha256').hexdigest()
                if old_hash != expected:
                    raise ValueError('Часть уже загружена с другим содержимым')
            else:
                os.replace(temp, path)
            return web.json_response({'index': index, 'ok': True})
        finally:
            temp.unlink(missing_ok=True)

    async def finish(self, request):
        row = await self.lease(request)
        info = await request.json()
        expected = info.get('sha256')
        size, count = info.get('size'), info.get('partCount')
        if (not isinstance(expected, str) or not re.fullmatch(r'[a-f0-9]{64}', expected)
            or type(size) is not int or not 1000 <= size <= MAX_RESULT
            or type(count) is not int or count != math.ceil(size / CHUNK)):
            raise ValueError('Неверный размер или количество частей')
        folder = self._folder(row['admin_id'], row['id'])
        temp = folder / 'render.tmp'
        out = folder / 'render.mp4'
        sha = hashlib.sha256()
        copied = 0
        try:
            with temp.open('wb') as dst:
                for i in range(count):
                    part = folder / f'{i:03d}.chunk'
                    if not part.is_file():
                        raise ValueError(f'Отсутствует часть {i}')
                    with part.open('rb') as src:
                        while block := src.read(65536):
                            copied += len(block)
                            if copied > size:
                                raise ValueError('Некорректный размер результата')
                            dst.write(block)
                            sha.update(block)
            if copied != size or sha.hexdigest() != expected:
                raise ValueError('Контрольная сумма MP4 не совпала')
            # ffprobe accepts file paths, never URLs or arbitrary command arguments.
            proc = await asyncio.create_subprocess_exec('ffprobe', '-v', 'error',
                '-protocol_whitelist', 'file,pipe', '-show_entries', 'stream=codec_type',
                '-of', 'json', str(temp), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            stdout, _ = await asyncio.wait_for(proc.communicate(), 30)
            if proc.returncode or not any(s.get('codec_type') == 'video' for s in json.loads(stdout).get('streams', [])):
                raise ValueError('Результат не содержит видеопотока')
            os.replace(temp, out)
            await self.studio.pool.execute(
                "UPDATE studio_worker_jobs SET status='awaiting_review',result=$2::jsonb, "
                'worker_hash=NULL,lease_hash=NULL,lease_expires=NULL,updated_at=NOW() WHERE id=$1',
                row['id'], json.dumps({'sha256': expected, 'size': size, 'published': False}))
            for p in folder.glob('*.chunk'):
                p.unlink(missing_ok=True)
            return web.json_response({'id': row['id'], 'status': 'awaiting_review', 'published': False})
        finally:
            temp.unlink(missing_ok=True)

    async def fail(self, request):
        row = await self.lease(request)
        await self.studio.pool.execute(
            "UPDATE studio_worker_jobs SET status='failed',error=$2,updated_at=NOW() WHERE id=$1",
            row['id'], 'Монтаж не завершён. Проверьте журнал Windows-программы.')
        return web.json_response({'id': row['id'], 'status': 'failed'})

    async def result(self, request):
        admin = await self.studio.authenticate(request)
        job_id = is_uuid(request.match_info['id'])
        row = await self.studio.pool.fetchrow('SELECT status FROM studio_worker_jobs WHERE id=$1 AND admin_id=$2', job_id, admin)
        if not row or row['status'] != 'awaiting_review':
            raise web.HTTPNotFound()
        path = self._folder(admin, job_id) / 'render.mp4'
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={'Content-Type': 'video/mp4',
                   'Content-Disposition': 'attachment; filename="vkino-studio-draft.mp4"'})

    async def cleanup(self):
        rows = await self.studio.pool.fetch(
            "DELETE FROM studio_worker_jobs WHERE updated_at < NOW()-INTERVAL '7 days' "
            "AND status NOT IN ('queued','running') RETURNING id,admin_id")
        for row in rows:
            await asyncio.to_thread(shutil.rmtree, self._folder(row['admin_id'], row['id']), True)

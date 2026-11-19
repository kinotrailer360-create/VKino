"""Outgoing-only VKino Studio Windows agent.

The worker never listens on a port. All downloads are scoped to its paired VKino
server and validated against the approved manifest hashes before rendering.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import ssl
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
from uuid import UUID

from .model import load_project
from .renderer import render

CHUNK = 4 * 1024 * 1024
MAX_JSON = 1024 * 1024
PREFIX = '/api/studio/v1/worker'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Редирект от сервера запрещён')


def origin(url: str) -> str:
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ('', '/')
            or parsed.port not in (None, 443)):
        raise ValueError('Сервер VKino должен быть HTTPS-доменом без пути, редиректов и нестандартного порта')
    return f'https://{parsed.hostname}'


class Client:
    def __init__(self, server: str, token: str = ''):
        self.server = origin(server)
        self.token = token
        self.opener = build_opener(NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))

    def request(self, method: str, route: str, body=None, *, headers=None, binary=False):
        if not route.startswith(PREFIX + '/') or '?' in route or '#' in route:
            raise ValueError('Запрещённый адрес API')
        data = None
        req_headers = {'Accept': 'application/json'}
        if self.token:
            req_headers['Authorization'] = f'Bearer {self.token}'
        if isinstance(body, (dict, list)):
            data = json.dumps(body, ensure_ascii=False).encode('utf-8')
            req_headers['Content-Type'] = 'application/json'
        elif isinstance(body, bytes):
            data = body
            req_headers['Content-Type'] = 'application/octet-stream'
        if headers:
            req_headers.update(headers)
        req = Request(self.server + route, data=data, method=method, headers=req_headers)
        try:
            with self.opener.open(req, timeout=90) as response:
                if binary:
                    return response
                raw = response.read(MAX_JSON + 1)
                if len(raw) > MAX_JSON:
                    raise ValueError('Слишком большой ответ API')
                return json.loads(raw)
        except HTTPError as exc:
            raise RuntimeError(f'VKino HTTP {exc.code}') from exc

    def pair(self, code: str) -> str:
        result = self.request('POST', PREFIX + '/pair', {'code': code})
        if result.get('scope') != 'worker' or not result.get('token'):
            raise RuntimeError('VKino не выдал токен исполнителя')
        self.token = result['token']
        return self.token

    def claim(self):
        return self.request('POST', PREFIX + '/claim', {})['job']

    def heartbeat(self, job_id: str, lease: str):
        self.request('POST', f'{PREFIX}/jobs/{job_id}/heartbeat', {}, headers={'X-Worker-Lease': lease})

    def fail(self, job_id: str, lease: str):
        self.request('POST', f'{PREFIX}/jobs/{job_id}/fail', {}, headers={'X-Worker-Lease': lease})

    def fetch_asset(self, job_id: str, lease: str, asset: dict, base: Path):
        identifier = str(UUID(asset['fileId']))
        # filenames have been validated by the server; validate again locally.
        from .model import IMAGE_TYPES, VIDEO_TYPES, AUDIO_TYPES
        from pathlib import PurePosixPath
        relative = asset['path']
        p = PurePosixPath(relative)
        if (p.is_absolute() or len(p.parts) != 2 or p.parts[0] != 'media'
                or p.suffix.lower() not in IMAGE_TYPES | VIDEO_TYPES | AUDIO_TYPES
                or '..' in p.parts or '\\' in relative):
            raise ValueError('Неверный путь материала')
        path = base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        request = Request(self.server + f'{PREFIX}/jobs/{job_id}/assets/{identifier}',
                          headers={'Authorization': f'Bearer {self.token}', 'X-Worker-Lease': lease})
        size = 0
        sha = hashlib.sha256()
        try:
            with self.opener.open(request, timeout=120) as response, path.open('xb') as out:
                while data := response.read(65536):
                    size += len(data)
                    if size > asset['size']:
                        raise ValueError('Размер скачанного файла больше заявленного')
                    sha.update(data)
                    out.write(data)
            if size != asset['size'] or sha.hexdigest() != asset['sha256']:
                raise ValueError('Контрольная сумма исходника не совпадает')
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path

    def upload(self, job_id: str, lease: str, path: Path):
        size = path.stat().st_size
        if not 1000 <= size <= 256 * 1024 * 1024:
            raise ValueError('Результат должен быть от 1 КБ до 256 МБ')
        sha = hashlib.sha256()
        with path.open('rb') as f:
            for index in range(math.ceil(size / CHUNK)):
                part = f.read(CHUNK)
                sha.update(part)
                self.request('PUT', f'{PREFIX}/jobs/{job_id}/chunks/{index}', part,
                    headers={'X-Worker-Lease': lease, 'X-Chunk-SHA256': hashlib.sha256(part).hexdigest()})
                self.heartbeat(job_id, lease)
        return self.request('POST', f'{PREFIX}/jobs/{job_id}/finish',
                {'size': size, 'sha256': sha.hexdigest(), 'partCount': math.ceil(size / CHUNK)},
                headers={'X-Worker-Lease': lease})


def run_once(client: Client, *, encoder: str, temp_root: Path) -> bool:
    job = client.claim()
    if job is None:
        return False
    job_id = str(UUID(job['id']))
    lease = job['lease']
    stop = threading.Event()
    errors = []

    def keepalive():
        while not stop.wait(60):
            try:
                client.heartbeat(job_id, lease)
            except Exception as exc:
                errors.append(str(exc))
                break

    pulse = threading.Thread(target=keepalive, daemon=True)
    pulse.start()
    try:
        with tempfile.TemporaryDirectory(prefix='vkino-', dir=temp_root) as tmp:
            folder = Path(tmp)
            for item in job['assets']:
                client.fetch_asset(job_id, lease, item, folder)
            manifest = folder / 'project.json'
            manifest.write_text(json.dumps(job['project'], ensure_ascii=False), encoding='utf-8')
            load_project(manifest)  # enforce the same author approval, file and timeline rules
            result = render(manifest, folder / 'output', encoder, print)
            if errors:
                raise RuntimeError('Соединение с VKino прервалось во время рендера')
            uploaded = client.upload(job_id, lease, result)
            print('Готово, результат ожидает утверждения:', uploaded.get('status'))
        return True
    except Exception:
        try:
            client.fail(job_id, lease)
        except Exception:
            pass
        raise
    finally:
        stop.set()
        pulse.join(timeout=3)


def main(argv=None):
    parser = argparse.ArgumentParser(description='VKino Studio: защищённый исходящий Windows-исполнитель')
    parser.add_argument('--server', required=True, help='https://домен-VKino')
    parser.add_argument('--pair', action='store_true', help='Подключить ПК по одноразовому коду /studio')
    parser.add_argument('--once', action='store_true', help='Выполнить не более одного задания')
    parser.add_argument('--encoder', choices=('libx264', 'h264_nvenc'), default='libx264')
    args = parser.parse_args(argv)
    client = Client(args.server, os.environ.get('VKINO_STUDIO_WORKER_TOKEN', ''))
    if args.pair:
        import getpass
        token = client.pair(getpass.getpass('Код из команды /studio: '))
        print('Токен получен. Храните его в VKINO_STUDIO_WORKER_TOKEN, не публикуйте в GitHub.')
        print('VKINO_STUDIO_WORKER_TOKEN=' + token)
        return
    if not client.token:
        parser.error('Нужно задать VKINO_STUDIO_WORKER_TOKEN или сначала выполнить --pair')
    with tempfile.TemporaryDirectory(prefix='vkino-worker-root-') as tmp:
        root = Path(tmp)
        while True:
            ran = run_once(client, encoder=args.encoder, temp_root=root)
            if args.once:
                break
            if not ran:
                time.sleep(5)


if __name__ == '__main__':
    main()

"""Persistent render jobs. Publication is deliberately a separate user action."""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

log = logging.getLogger(__name__)


def obj(raw: Any) -> dict[str, Any]:
    return json.loads(raw) if isinstance(raw, str) else dict(raw or {})


class Jobs:
    def __init__(self, pool: Any) -> None:
        self.pool = pool
        self.active: asyncio.Task | None = None
        self.stopping = False
        self.pending_result: tuple[str, str, str] | None = None

    async def enqueue(self, kind: str, chat: int, data: dict[str, Any]) -> str:
        identifier = str(uuid5(NAMESPACE_URL, f"vkino:{chat}:{kind}:{data['remix_work']}:{data.get('remix_revision',0)}"))
        await self.pool.execute(
            "INSERT INTO render_jobs(id,kind,chat_id,payload,status) VALUES($1,$2,$3,$4::jsonb,'queued') "
            "ON CONFLICT(id) DO NOTHING", identifier, kind, chat, json.dumps(data, ensure_ascii=False))
        return identifier

    async def recover(self) -> list[Any]:
        return await self.pool.fetch(
            "UPDATE render_jobs SET status='interrupted',error='Перезапуск бота',updated_at=NOW() "
            "WHERE status='running' RETURNING id,chat_id")

    async def cleanup(self, root: Path, days: int = 7) -> None:
        rows = await self.pool.fetch("SELECT id,payload FROM render_jobs WHERE status='succeeded' "
                                     "AND updated_at < NOW()-make_interval(days => $1)", days)
        for row in rows:
            path = Path(obj(row['payload']).get('remix_work') or '').resolve()
            if path != root.resolve() and path.is_relative_to(root.resolve()) and path.name.startswith('remix_'):
                await asyncio.to_thread(shutil.rmtree, path, True)
                await self.pool.execute("UPDATE render_jobs SET status='expired',updated_at=NOW() WHERE id=$1", row['id'])

    async def resume(self, identifier: str, chat: int) -> bool:
        row = await self.pool.fetchval(
            "UPDATE render_jobs SET status='queued',error='',updated_at=NOW() "
            "WHERE id=$1 AND chat_id=$2 AND status IN ('failed','interrupted','cancelled') RETURNING 1",
            identifier, chat)
        return bool(row)

    async def checkpoint(self, identifier: str, result: dict[str, Any]) -> None:
        await self.pool.execute("UPDATE render_jobs SET result=$2::jsonb,updated_at=NOW() WHERE id=$1",
                                identifier, json.dumps(result))

    async def flush_result(self) -> None:
        if self.pending_result:
            await self.pool.execute("UPDATE render_jobs SET status=$2,error=$3,updated_at=NOW() WHERE id=$1",
                                    *self.pending_result)
            self.pending_result = None

    async def worker(self, execute: Any) -> None:
        while not self.stopping:
            try:
                await self.flush_result()
                await self.tick(execute)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('Render queue temporarily unavailable')
                await asyncio.sleep(5)

    async def tick(self, execute: Any) -> None:
        row = await self.pool.fetchrow(
            "UPDATE render_jobs SET status='running',updated_at=NOW() WHERE id=("
            "SELECT id FROM render_jobs WHERE status='queued' ORDER BY created_at "
            "FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *")
        if not row:
            await asyncio.sleep(2)
            return
        self.active = asyncio.create_task(execute(row['kind'], row['chat_id'], obj(row['payload']),
                                                   row['id'], obj(row['result'])))
        status, error = 'succeeded', ''
        try:
            await self.active
        except asyncio.CancelledError:
            status = 'interrupted' if self.stopping else 'cancelled'
            error = 'Работа прервана; исходники сохранены'
        except Exception as exc:
            status, error = 'failed', str(exc)[:500]
            log.exception('Render job failed: %s', row['id'])
        finally:
            self.active = None
            self.pending_result = (row['id'], status, error)
        await self.flush_result()

    async def stop(self) -> None:
        self.stopping = True
        if self.active:
            self.active.cancel()

"""Durable upload claims and exact content/metadata versions."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from pathlib import Path
from typing import Any

from .scene_planner import _digest


def version(video: Path, seo: dict[str, Any]) -> str:
    raw = _digest(video) + json.dumps(seo, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


class Uploads:
    def __init__(self, pool: Any) -> None:
        self.pool = pool

    async def claim(self, version: str, chat: int, first: int, seo: dict[str, Any], force: bool) -> bool:
        return bool(await self.pool.fetchval(
            "INSERT INTO youtube_publications(version,chat_id,first_id,metadata,status) "
            "VALUES($1,$2,$3,$4::jsonb,'uploading') ON CONFLICT(version) DO UPDATE "
            "SET status='uploading',updated_at=NOW(),last_error='' "
            "WHERE youtube_publications.status='failed' OR ($5 AND youtube_publications.status <> 'uploading') RETURNING 1",
            version, chat, first, json.dumps(seo, ensure_ascii=False), force))

    async def complete(self, version: str, video_id: str, privacy: str) -> None:
        await self.pool.execute("UPDATE youtube_publications SET video_id=$2,privacy=$3,status='uploaded',updated_at=NOW() "
                                "WHERE version=$1", version, video_id, privacy)

    async def fail(self, version: str, error: str) -> None:
        await self.pool.execute("UPDATE youtube_publications SET status='uncertain',last_error=$2,updated_at=NOW() "
                                "WHERE version=$1", version, error[:500])

    async def recover(self) -> None:
        await self.pool.execute("UPDATE youtube_publications SET status='uncertain',last_error='Перезапуск во время загрузки', "
                                "updated_at=NOW() WHERE status='uploading'")

    async def cancel(self, version: str) -> None:
        # Delivery may have completed remotely; only a human may retry it.
        pending = asyncio.create_task(self.fail(version, 'Загрузка прервана; проверь YouTube перед повтором'))
        try:
            await asyncio.wait_for(asyncio.shield(pending), 5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending

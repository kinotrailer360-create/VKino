"""Хранилище состояний диалогов (FSM aiogram 3) в PostgreSQL — вместо MemoryStorage.

Состояние («жду текст сценария», «жду картинки», присланные file_id…) переживает перезапуск и обновление бота.
Таблица fsm_states создаётся миграцией 002 (shorts/migrations.py). Данные — только JSON: картинки хранятся
как file_id Telegram, не байтами и не локальными путями; несериализуемое значение — ошибка, а не тихая потеря.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, DefaultKeyBuilder, StorageKey

log = logging.getLogger("vkino.fsm")

KEEP_DAYS = 7  # незаконченный диалог дольше недели — удаляем


class FSMDataError(TypeError):
    """В состояние положили то, что нельзя сохранить в JSON (байты, Path, объект)."""


class PgStorage(BaseStorage):
    def __init__(self, pool_getter: Any) -> None:
        """pool_getter — функция, возвращающая asyncpg.Pool (пул создаётся после запуска диспетчера)."""
        self._pool = pool_getter
        self._keys = DefaultKeyBuilder(with_bot_id=True, with_business_connection_id=True, with_destiny=True)

    def _key(self, key: StorageKey) -> str:
        return self._keys.build(key)

    async def set_state(self, key: StorageKey, state: str | State | None = None) -> None:
        value = state.state if isinstance(state, State) else state
        await self._pool().execute(
            "INSERT INTO fsm_states (key, state) VALUES ($1, $2) "
            "ON CONFLICT (key) DO UPDATE SET state = EXCLUDED.state, updated_at = NOW()", self._key(key), value)

    async def get_state(self, key: StorageKey) -> str | None:
        return await self._pool().fetchval("SELECT state FROM fsm_states WHERE key = $1", self._key(key))

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        try:
            raw = json.dumps(dict(data), ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise FSMDataError(f"состояние диалога не сохраняется в JSON: {exc}") from exc
        await self._pool().execute(
            "INSERT INTO fsm_states (key, data) VALUES ($1, $2::jsonb) "
            "ON CONFLICT (key) DO UPDATE SET data = EXCLUDED.data, updated_at = NOW()", self._key(key), raw)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        raw = await self._pool().fetchval("SELECT data FROM fsm_states WHERE key = $1", self._key(key))
        if raw is None:
            return {}
        return json.loads(raw) if isinstance(raw, str) else dict(raw)

    async def close(self) -> None:
        return None

    async def cleanup(self) -> int:
        """Удалить брошенные диалоги старше KEEP_DAYS; вернуть, сколько удалено."""
        done = await self._pool().execute(
            "DELETE FROM fsm_states WHERE updated_at < NOW() - make_interval(days => $1)", KEEP_DAYS)
        return int(str(done).split()[-1] or 0)

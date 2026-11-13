"""Нумерованные миграции базы: таблица schema_version, по порядку, один раз, с проверкой целостности.

Правила:
- миграция — (номер, имя, SQL[, перенос данных]); номера только растут, применённые не меняются;
- при старте бота: общая блокировка (pg_advisory_xact_lock) — две копии бота не применят миграции одновременно;
- применённая миграция записывается в schema_version с контрольной суммой; изменили её текст — бот не стартует
  миграции (MigrationError) и сообщает админу;
- все миграции только ДОБАВЛЯЮТ таблицы: старые таблицы и JSON-ключи в shorts_settings не трогаются — откат на
  прошлую версию бота работает (на переходный период бот пишет и в старое место);
- перенос данных проверяется: сколько записей было в JSON — столько в таблице, иначе вся миграция откатывается.
- копию базы перед обновлением делает deploy/update.sh (pg_dump), если есть неприменённые миграции.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Awaitable, Callable

log = logging.getLogger("vkino.migrations")

LOCK_ID = 7300001  # номер блокировки PostgreSQL для миграций VKino


class MigrationError(RuntimeError):
    pass


# ---------------------------------------------------------------- перенос данных из JSON в shorts_settings

async def _setting_json(conn: Any, key: str, default: Any) -> Any:
    raw = await conn.fetchval("SELECT value FROM shorts_settings WHERE key = $1", key)
    try:
        return json.loads(raw) if raw else default
    except ValueError:
        log.warning("migration: %s is not JSON — skipped", key)
        return default


async def _copy_drafts(conn: Any) -> None:
    meta = await _setting_json(conn, "posts_meta", {})
    n = 0
    for key, value in (meta.items() if isinstance(meta, dict) else []):
        chat, _, first = str(key).partition(":")
        if not (chat.lstrip("-").isdigit() and first.isdigit() and isinstance(value, dict)):
            continue
        published = value.pop("published", None)
        await conn.execute(
            "INSERT INTO ai_drafts (chat_id, first_id, meta, status, published_at) "
            "VALUES ($1, $2, $3::jsonb, $4, to_timestamp($5)) ON CONFLICT DO NOTHING",
            int(chat), int(first), json.dumps(value, ensure_ascii=False),
            "published" if published else "draft", float(published) if published else None)
        n += 1
    got = await conn.fetchval("SELECT COUNT(*) FROM ai_drafts")
    if got < n:
        raise MigrationError(f"черновики: в JSON {n}, в таблице {got}")


async def _copy_queue(conn: Any) -> None:
    queue = await _setting_json(conn, "posts_queue", [])
    n = 0
    for q in queue if isinstance(queue, list) else []:
        if not isinstance(q, dict) or not all(str(q.get(k, "")).lstrip("-").isdigit() for k in ("chat", "first", "at")):
            continue
        await conn.execute("INSERT INTO ai_queue (chat_id, first_id, at) VALUES ($1, $2, to_timestamp($3)) "
                           "ON CONFLICT (chat_id, first_id) DO UPDATE SET at = EXCLUDED.at",
                           int(q["chat"]), int(q["first"]), float(q["at"]))
        n += 1
    got = await conn.fetchval("SELECT COUNT(*) FROM ai_queue")
    if got < n:
        raise MigrationError(f"очередь: в JSON {n}, в таблице {got}")


async def _copy_history(conn: Any) -> None:
    hist = await _setting_json(conn, "posts_history", {})
    seen: set[tuple[str, str]] = set()  # одинаковые элементы в JSON бывают — в таблице они один раз
    for kind, items in (hist.items() if isinstance(hist, dict) else []):
        for pos, item in enumerate(items if isinstance(items, list) else []):
            value = history_value(item)
            await conn.execute(
                "INSERT INTO ai_post_history (kind, pos, value) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                str(kind), pos, value)
            seen.add((str(kind), value))
    n = len(seen)
    got = await conn.fetchval("SELECT COUNT(*) FROM ai_post_history")
    if got < n:
        raise MigrationError(f"история постов: в JSON {n}, в таблице {got}")


def history_value(item: Any) -> str:
    """Элемент истории постов как строка: ссылки и id — как есть, сюжеты (словари) — JSON с сортировкой ключей."""
    return item if isinstance(item, str) else json.dumps(item, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------- список миграций

Step = Callable[[Any], Awaitable[None]]

MIGRATIONS: list[tuple[int, str, str, Step | None]] = [
    # 001 — точка отсчёта: таблицы, которые создаёт bot.py:init_db (users, favorites, … shorts_settings, ideas),
    # уже существуют; миграция их не меняет, только фиксирует начало нумерации
    (1, "baseline (tables of init_db)", "", None),
    (2, "fsm_states — состояния диалогов бота", """
        CREATE TABLE IF NOT EXISTS fsm_states (
            key TEXT PRIMARY KEY,
            state TEXT,
            data JSONB NOT NULL DEFAULT '{}',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_fsm_states_time ON fsm_states(updated_at);
    """, None),
    (3, "ai_drafts + ai_audit_log — черновики постов и журнал", """
        CREATE TABLE IF NOT EXISTS ai_drafts (
            chat_id BIGINT NOT NULL,
            first_id BIGINT NOT NULL,
            meta JSONB NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            claimed_at TIMESTAMPTZ,
            published_at TIMESTAMPTZ,
            published_by BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (chat_id, first_id)
        );
        CREATE INDEX IF NOT EXISTS idx_ai_drafts_time ON ai_drafts(created_at);
        CREATE TABLE IF NOT EXISTS ai_audit_log (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            first_id BIGINT NOT NULL,
            actor BIGINT,
            action TEXT NOT NULL,
            details JSONB NOT NULL DEFAULT '{}',
            at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_ai_audit_post ON ai_audit_log(chat_id, first_id, at);
    """, _copy_drafts),
    (4, "ai_queue — отложенная публикация", """
        CREATE TABLE IF NOT EXISTS ai_queue (
            chat_id BIGINT NOT NULL,
            first_id BIGINT NOT NULL,
            at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (chat_id, first_id)
        );
        CREATE INDEX IF NOT EXISTS idx_ai_queue_at ON ai_queue(at);
    """, _copy_queue),
    (5, "ai_post_history — что уже было в постах (дедупликация)", """
        CREATE TABLE IF NOT EXISTS ai_post_history (
            kind TEXT NOT NULL,
            pos BIGINT NOT NULL,
            value TEXT NOT NULL,
            added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (kind, value)
        );
        CREATE INDEX IF NOT EXISTS idx_ai_post_history_kind ON ai_post_history(kind, pos);
    """, _copy_history),
    (6, "ai_usage — платные вызовы и резервы бюджета", """
        CREATE TABLE IF NOT EXISTS ai_usage (
            id BIGSERIAL PRIMARY KEY,
            pool TEXT NOT NULL,
            op TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            estimate_usd NUMERIC(12, 6) NOT NULL DEFAULT 0,
            actual_usd NUMERIC(12, 6),
            units JSONB NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            settled_at TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS idx_ai_usage_month ON ai_usage(pool, created_at);
    """, None),
    # 007 — ключ истории по отпечатку значения: «сюжет» (JSON со словами новости) бывает длиннее 2,7 КБ, а такое
    # значение не влезает в индекс PostgreSQL — запись истории падала бы. 005 уже применена на сервере, её не трогаем
    (7, "ai_post_history: ключ (kind, md5(value))", """
        ALTER TABLE ai_post_history ADD COLUMN IF NOT EXISTS vhash TEXT GENERATED ALWAYS AS (md5(value)) STORED;
        ALTER TABLE ai_post_history DROP CONSTRAINT IF EXISTS ai_post_history_pkey;
        ALTER TABLE ai_post_history ADD PRIMARY KEY (kind, vhash);
    """, None),
    # 008 — утверждение сцен ролика: по версии сцены (отпечаток текста, времени и файла), а не по сообщению —
    # правка сцены даёт новый отпечаток и снимает утверждение только с неё; неизменённые сцены после
    # перемонтажа остаются утверждёнными
    (8, "scene_approvals — утверждённые версии сцен", """
        CREATE TABLE IF NOT EXISTS scene_approvals (
            chat_id BIGINT NOT NULL,
            scene_hash TEXT NOT NULL,
            first_id BIGINT NOT NULL,
            scene_index INT NOT NULL,
            actor BIGINT,
            at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (chat_id, scene_hash)
        );
    """, None),
    # 009 — библиотека сцен (ТЗ, приложение B): видео с правами, их сцены с ключевыми кадрами и описаниями;
    # выпуски рубрик (заголовок, крючок, ролик на YouTube) и их показатели через 48 часов
    (9, "media_library + media_scenes + episodes", """
        CREATE TABLE IF NOT EXISTS media_library (
            id SERIAL PRIMARY KEY,
            work TEXT NOT NULL,
            season INT,
            episode INT,
            file TEXT NOT NULL DEFAULT '',
            sha TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL DEFAULT '',
            owner TEXT NOT NULL DEFAULT '',
            license TEXT NOT NULL DEFAULT '',
            publish TEXT NOT NULL DEFAULT 'unconfirmed',
            added_by BIGINT,
            added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            indexed_at TIMESTAMPTZ,
            scenes INT NOT NULL DEFAULT 0,
            model TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS media_scenes (
            lib_id INT NOT NULL REFERENCES media_library(id) ON DELETE CASCADE,
            n INT NOT NULL,
            t_start REAL NOT NULL,
            t_end REAL NOT NULL,
            keyframe TEXT NOT NULL,
            info JSONB NOT NULL DEFAULT '{}',
            words TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (lib_id, n)
        );
        CREATE TABLE IF NOT EXISTS episodes (
            id SERIAL PRIMARY KEY,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            hook TEXT NOT NULL DEFAULT '',
            script JSONB NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            video_id TEXT,
            published_at TIMESTAMPTZ,
            views_48h BIGINT,
            likes_48h BIGINT,
            comments_48h BIGINT,
            collected_at TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS idx_episodes_pub ON episodes(published_at);
    """, None),
    # 010 — приглашённый «активен», если вернулся в бота на следующий день и позже (а не только запустил его)
    (10, "referrals.active", """
        ALTER TABLE referrals ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT FALSE;
    """, None),
]


def checksum(sql: str, step: Step | None) -> str:
    """Контрольная сумма миграции: SQL + имя функции переноса данных."""
    return hashlib.sha256((sql.strip() + "|" + (step.__name__ if step else "")).encode()).hexdigest()


def pending(applied: dict[int, str]) -> list[int]:
    """Номера неприменённых миграций; изменённая применённая миграция — MigrationError."""
    for version, name, sql, step in MIGRATIONS:
        if version in applied and applied[version] != checksum(sql, step):
            raise MigrationError(f"миграция {version:03d} ({name}) изменена после применения — "
                                 "так нельзя: добавьте новую миграцию")
    return [v for v, *_ in MIGRATIONS if v not in applied]


async def migrate(pool: Any) -> list[int]:
    """Применить новые миграции по порядку. Возвращает номера применённых сейчас."""
    numbers = [v for v, *_ in MIGRATIONS]
    if numbers != sorted(set(numbers)):
        raise MigrationError("номера миграций должны идти по возрастанию без повторов")
    done: list[int] = []
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", LOCK_ID)  # вторая копия бота ждёт здесь
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INT PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )""")
        applied = {r["version"]: r["checksum"] for r in await conn.fetch("SELECT version, checksum FROM schema_version")}
        todo = set(pending(applied))
        for version, name, sql, step in MIGRATIONS:
            if version not in todo:
                continue
            async with conn.transaction():  # своя точка сохранения: ошибка — откат всего запуска
                if sql.strip():
                    await conn.execute(sql)
                if step:
                    await step(conn)
                await conn.execute("INSERT INTO schema_version (version, name, checksum) VALUES ($1, $2, $3)",
                                   version, name, checksum(sql, step))
            log.info("migration %03d applied: %s", version, name)
            done.append(version)
    return done

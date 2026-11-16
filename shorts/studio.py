"""Private Sites bridge. No publishing actions; every result remains an unapproved draft.

One-use pairing codes are issued only by Telegram admin commands. Durable jobs are
idempotent; interrupted paid work is never retried automatically after a restart.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from aiohttp import web

log = logging.getLogger("vkino.studio")
PREFIX = "/api/studio/v1"
MAX_FILE = 50 * 1024 * 1024
MIMES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
         "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov"}
OPERATIONS = {"source", "script", "media", "check", "render", "assemble", "notify"}


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def obj(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def uuid(value: Any) -> str:
    return str(UUID(str(value)))


def validate_job(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("operation") not in OPERATIONS:
        raise ValueError("Неизвестная операция студии")
    request_id = uuid(data.get("requestId"))
    p = data.get("project")
    if not isinstance(p, dict) or p.get("channel") not in {"youtube", "telegram"}:
        raise ValueError("Неверный проект")
    uuid(p.get("id"))
    if type(p.get("revision")) is not int or p["revision"] < 1:
        raise ValueError("Укажите версию проекта")
    stage = p.get("stage")
    if type(stage) is not int or not 0 <= stage <= 4:
        raise ValueError("Неверный этап")
    operation = data["operation"]
    allowed = {"source": {0}, "script": {1}, "media": {2}, "check": {2}, "render": {3},
               "assemble": {3}, "notify": {0, 1, 2, 3, 4}}
    if stage not in allowed[operation]:
        raise ValueError("Сначала утвердите предыдущий этап")
    for key, maximum in (("title", 180), ("input", 30000), ("script", 30000), ("sourceUrl", 2000)):
        if not isinstance(p.get(key, ""), str) or len(p.get(key, "")) > maximum:
            raise ValueError("Слишком длинный или неверный текст")
    scenes = p.get("scenes", [])
    if not isinstance(scenes, list) or len(scenes) > 100:
        raise ValueError("Не больше 100 абзацев")
    index = data.get("sceneIndex")
    if operation in {"media", "check", "render"} and (type(index) is not int or not 0 <= index < len(scenes)):
        raise ValueError("Сцена не найдена")
    for s in scenes:
        if not isinstance(s, dict) or not isinstance(s.get("text"), str) or len(s["text"]) > 6000:
            raise ValueError("Неверная сцена")
        for k in ("duration", "start", "end"):
            n = s.get(k, 0)
            if isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) or n < 0:
                raise ValueError("Неверный тайминг")
        for k in ("asset", "render"):
            if s.get(k):
                uuid(s[k].get("id"))
    if operation == "render":
        s = scenes[index]
        if not s.get("asset") or not all(s.get(k) is True for k in ("match", "checked", "rights")):
            raise ValueError("Сначала проверьте и утвердите кадры")
        if not 2 <= s["duration"] <= 30 or len(s["text"]) > 1000:
            raise ValueError("Абзац для озвучки: до 1000 знаков и 30 секунд")
    if operation == "assemble":
        if not scenes or len(scenes) > 30 or sum(s["duration"] for s in scenes) > 180:
            raise ValueError("Итоговый Shorts: до 30 сцен и 180 секунд")
        if not all(s.get("render") and s.get("renderApproved") is True for s in scenes):
            raise ValueError("Сначала утвердите монтаж всех абзацев")
    if operation in {"render", "assemble"} and p["channel"] != "youtube":
        raise ValueError("Монтаж доступен в разделе YouTube")
    return {"requestId": request_id, "operation": operation, "project": p, "sceneIndex": index}


class Studio:
    def __init__(self, pool: Any, admins: set[int], root: Path, execute: Any, lock: Any,
                 site_url: str = "") -> None:
        self.pool, self.admins, self.root = pool, admins, root
        self.execute, self.lock, self.site_url = execute, lock, site_url
        self.bot: Any = None
        self.task: asyncio.Task | None = None
        self.last_cleanup = 0.0
        self.root.mkdir(parents=True, exist_ok=True)

    async def issue_pair(self, admin: int) -> str:
        if admin not in self.admins:
            raise ValueError("Только для администратора")
        code = secrets.token_urlsafe(32)
        async with self.pool.acquire() as c, c.transaction():
            await c.execute("DELETE FROM studio_tokens WHERE admin_id=$1 AND kind='pair'", admin)
            await c.execute("INSERT INTO studio_tokens (token_hash,admin_id,kind,expires_at) "
                            "VALUES ($1,$2,'pair',NOW()+INTERVAL '10 minutes')", digest(code), admin)
        return code

    async def revoke(self, admin: int) -> None:
        await self.pool.execute("DELETE FROM studio_tokens WHERE admin_id=$1", admin)
        await self.pool.execute("UPDATE studio_jobs SET status='cancelled',error='Подключение отозвано' "
                                "WHERE admin_id=$1 AND status='queued'", admin)

    async def authenticate(self, request: web.Request) -> int:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or len(auth) > 200:
            raise web.HTTPUnauthorized(text='{"error":"Подключите бота заново"}', content_type="application/json")
        row = await self.pool.fetchrow("SELECT admin_id FROM studio_tokens WHERE token_hash=$1 "
                                       "AND kind='session' AND expires_at>NOW()", digest(auth[7:]))
        if not row or row["admin_id"] not in self.admins:
            raise web.HTTPUnauthorized(text='{"error":"Доступ отозван или истёк"}', content_type="application/json")
        return int(row["admin_id"])

    def register(self, app: web.Application) -> None:
        for method, path, fn in [("POST", "/pair", self.pair), ("GET", "/status", self.status),
                                  ("POST", "/disconnect", self.disconnect), ("POST", "/jobs", self.submit),
                                  ("GET", "/jobs/{id}", self.job), ("POST", "/files", self.upload),
                                  ("GET", "/files/{id}", self.file), ("GET", "/inbox", self.inbox)]:
            async def route(request: web.Request, handler=fn) -> web.StreamResponse:
                try:
                    response = await handler(request)
                    response.headers["Cache-Control"] = "private, no-store"
                    return response
                except (ValueError, TypeError, KeyError) as exc:
                    return web.json_response({"error": str(exc)[:300]}, status=400)
                except web.HTTPException:
                    raise
                except Exception:
                    log.exception("Studio API failed")
                    return web.json_response({"error": "Ошибка сервера VKino. Проверьте журнал бота."}, status=500)
            app.router.add_route(method, PREFIX + path, route)

    async def pair(self, request: web.Request) -> web.Response:
        data = await request.json()
        code = str(data.get("code", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]{40,60}", code):
            raise ValueError("Неверный код подключения")
        async with self.pool.acquire() as c, c.transaction():
            row = await c.fetchrow("DELETE FROM studio_tokens WHERE token_hash=$1 AND kind='pair' "
                                   "AND expires_at>NOW() RETURNING admin_id", digest(code))
            if not row or row["admin_id"] not in self.admins:
                raise ValueError("Код истёк или уже использован. Получите новый: /studio")
            token = secrets.token_urlsafe(40)
            await c.execute("INSERT INTO studio_tokens (token_hash,admin_id,kind,expires_at) "
                            "VALUES ($1,$2,'session',NOW()+INTERVAL '30 days')", digest(token), row["admin_id"])
        return web.json_response({"token": token, "adminId": str(row["admin_id"]), "expiresDays": 30,
                                  "protocol": 1})

    async def status(self, request: web.Request) -> web.Response:
        admin = await self.authenticate(request)
        return web.json_response({"connected": True, "adminId": str(admin), "protocol": 1,
                                  "operations": sorted(OPERATIONS), "automaticPublication": False})

    async def disconnect(self, request: web.Request) -> web.Response:
        await self.authenticate(request)
        await self.pool.execute("DELETE FROM studio_tokens WHERE token_hash=$1",
                                digest(request.headers["Authorization"][7:]))
        return web.json_response({"ok": True})

    async def submit(self, request: web.Request) -> web.Response:
        admin = await self.authenticate(request)
        data = validate_job(await request.json())
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        identifier = data["requestId"]
        async with self.pool.acquire() as c, c.transaction():
            # Serialize admissions per owner, also across worker instances.
            await c.execute("SELECT pg_advisory_xact_lock($1)", admin)
            old = await c.fetchrow("SELECT * FROM studio_jobs WHERE id=$1", identifier)
            if old:
                if old["admin_id"] != admin or obj(old["payload"]) != data:
                    raise ValueError("Идентификатор уже использован для другого задания")
                return web.json_response({"id": identifier, "status": old["status"]})
            count = await c.fetchval("SELECT COUNT(*) FROM studio_jobs WHERE admin_id=$1 "
                                     "AND status IN ('queued','running')", admin)
            if count >= 3:
                return web.json_response({"error": "В очереди уже три задания. Дождитесь результата."}, status=429)
            await c.execute("INSERT INTO studio_jobs (id,admin_id,payload,status) VALUES ($1,$2,$3::jsonb,'queued')",
                            identifier, admin, payload)
        return web.json_response({"id": identifier, "status": "queued"}, status=202)

    async def job(self, request: web.Request) -> web.Response:
        admin = await self.authenticate(request)
        row = await self.pool.fetchrow("SELECT * FROM studio_jobs WHERE id=$1 AND admin_id=$2",
                                       uuid(request.match_info["id"]), admin)
        if not row:
            raise web.HTTPNotFound(text='{"error":"Задание не найдено"}', content_type="application/json")
        return web.json_response({"id": row["id"], "status": row["status"], "result": obj(row["result"]),
                                  "error": row["error"], "operation": obj(row["payload"])["operation"]})

    async def store_file(self, admin: int, path: Path, mime: str, name: str = "") -> dict[str, Any]:
        if mime not in MIMES or not 0 < path.stat().st_size <= MAX_FILE:
            raise ValueError("Файл должен быть до 50 МБ и иметь поддерживаемый формат")
        identifier = str(uuid4())
        folder = self.root / str(admin) / "files"
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / (identifier + MIMES[mime])
        await asyncio.to_thread(shutil.copyfile, path, dest)
        def sha() -> str:
            with dest.open("rb") as f:
                return hashlib.file_digest(f, "sha256").hexdigest()
        hashed = await asyncio.to_thread(sha)
        result = {"id": identifier, "name": (name or path.name)[:180], "mime": mime,
                  "hash": hashed, "size": dest.stat().st_size}
        try:
            async with self.pool.acquire() as c, c.transaction():
                await c.execute("SELECT pg_advisory_xact_lock($1)", admin)
                used = await c.fetchval("SELECT COALESCE(SUM((metadata->>'size')::bigint),0) "
                                        "FROM studio_files WHERE admin_id=$1", admin)
                if used + result["size"] > 512 * 1024 * 1024:
                    raise ValueError("Лимит файлов студии — 512 МБ. Копии очищаются через 7 дней")
                await c.execute("INSERT INTO studio_files (id,admin_id,metadata) VALUES ($1,$2,$3::jsonb)",
                                identifier, admin, json.dumps(result))
        except BaseException:
            dest.unlink(missing_ok=True)
            raise
        return result

    async def resolve_file(self, admin: int, identifier: str) -> tuple[Path, dict[str, Any]]:
        meta = await self.pool.fetchval("SELECT metadata FROM studio_files WHERE id=$1 AND admin_id=$2",
                                        uuid(identifier), admin)
        if not meta:
            raise ValueError("Файл не найден или принадлежит другому пользователю")
        meta = obj(meta)
        path = self.root / str(admin) / "files" / (uuid(identifier) + MIMES[meta["mime"]])
        if not path.is_file():
            raise ValueError("Файл уже удалён. Загрузите его снова")
        return path, meta

    async def upload(self, request: web.Request) -> web.Response:
        admin = await self.authenticate(request)
        mime = request.content_type
        if mime not in MIMES or (request.content_length or 0) > MAX_FILE:
            raise ValueError("Поддерживаются фото и видео до 50 МБ")
        # Limit retained media per admin to avoid exhausting the small VPS.
        used = await self.pool.fetchval("SELECT COALESCE(SUM((metadata->>'size')::bigint),0) "
                                       "FROM studio_files WHERE admin_id=$1", admin)
        if used + (request.content_length or MAX_FILE) > 512 * 1024 * 1024:
            raise ValueError("Лимит файлов студии на сервере — 512 МБ. Старые копии очищаются через 7 дней")
        path = self.root / (str(uuid4()) + ".upload")
        size = 0
        try:
            with path.open("wb") as f:
                async for chunk in request.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > MAX_FILE:
                        raise ValueError("Файл больше 50 МБ")
                    f.write(chunk)
            return web.json_response(await self.store_file(admin, path, mime))
        finally:
            path.unlink(missing_ok=True)

    async def file(self, request: web.Request) -> web.Response:
        admin = await self.authenticate(request)
        path, meta = await self.resolve_file(admin, request.match_info["id"])
        return web.FileResponse(path, headers={"Content-Type": meta["mime"], "X-Content-Type-Options": "nosniff"})

    async def export_text(self, admin: int, text: str, title: str = "Из VKino") -> None:
        if admin not in self.admins or not text.strip():
            raise ValueError("Ответьте командой на сообщение с текстом")
        await self.pool.execute("INSERT INTO studio_inbox (id,admin_id,title,body) VALUES ($1,$2,$3,$4)",
                                str(uuid4()), admin, title[:180], text[:30000])

    async def inbox(self, request: web.Request) -> web.Response:
        admin = await self.authenticate(request)
        rows = await self.pool.fetch("SELECT id,title,body,created_at FROM studio_inbox WHERE admin_id=$1 "
                                     "ORDER BY created_at DESC LIMIT 50", admin)
        return web.json_response([{"id": r["id"], "title": r["title"], "input": r["body"],
                                   "created": r["created_at"].isoformat()} for r in rows])

    async def cleanup(self) -> None:
        # Seven days is a transfer window; accepted results persist in the Site's storage.
        rows = await self.pool.fetch("DELETE FROM studio_files WHERE created_at < NOW()-INTERVAL '7 days' "
                                     "RETURNING id,admin_id,metadata")
        for row in rows:
            meta = obj(row["metadata"])
            path = self.root / str(row["admin_id"]) / "files" / (uuid(row["id"]) + MIMES[meta["mime"]])
            path.unlink(missing_ok=True)
        await self.pool.execute("DELETE FROM studio_tokens WHERE expires_at<NOW()")
        # Remove partial uploads and abandoned renders after a crash.
        cutoff = time.time() - 86400
        for path in self.root.glob("*.upload"):
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        for path in self.root.glob("*/jobs/*"):
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        self.last_cleanup = time.monotonic()

    async def start(self, bot: Any) -> None:
        self.bot = bot
        await self.pool.execute("UPDATE studio_jobs SET status='interrupted',error='Бот перезапущен. "
                                "Проверьте результат перед новым запуском.' WHERE status='running'")
        self.task = asyncio.create_task(self.worker())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def worker(self) -> None:
        while True:
            try:
                # Claim only after taking the same lock used by Telegram render jobs.
                async with self.lock:
                    if time.monotonic() - self.last_cleanup > 3600:
                        await self.cleanup()
                    row = await self.pool.fetchrow("UPDATE studio_jobs SET status='running',updated_at=NOW() "
                        "WHERE id=(SELECT id FROM studio_jobs WHERE status='queued' ORDER BY created_at "
                        "FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *")
                    if row:
                        await self.run_job(row)
                if not row:
                    await asyncio.sleep(3)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Studio worker error")
                await asyncio.sleep(5)

    async def run_job(self, row: Any) -> None:
        identifier, admin = row["id"], row["admin_id"]
        work = self.root / str(admin) / "jobs" / identifier
        work.mkdir(parents=True, exist_ok=True)
        try:
            if admin not in self.admins:
                raise ValueError("Администратор больше не имеет доступа")
            result = await asyncio.wait_for(self.execute(self, admin, obj(row["payload"]), work), 900)
            await self.pool.execute("UPDATE studio_jobs SET status='succeeded',result=$2::jsonb,updated_at=NOW() "
                                    "WHERE id=$1", identifier, json.dumps(result, ensure_ascii=False))
            message = "✅ Задание из KinoMir360 Studio готово. Откройте сайт и проверьте результат."
        except asyncio.CancelledError:
            await self.pool.execute("UPDATE studio_jobs SET status='interrupted',error='Работа прервана',updated_at=NOW() "
                                    "WHERE id=$1", identifier)
            raise
        except Exception as exc:
            # Never expose upstream credentials/URLs through error text.
            log.warning("Studio job %s failed: %s", identifier, type(exc).__name__)
            error = str(exc)[:400] if isinstance(exc, ValueError) else "Ошибка сервиса, лимит ИИ или таймаут. Проверьте /ai и журнал VKino."
            await self.pool.execute("UPDATE studio_jobs SET status='failed',error=$2,updated_at=NOW() WHERE id=$1",
                                    identifier, error)
            message = "⚠️ Задание из KinoMir360 Studio не завершено. Результат и статус доступны на сайте."
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if self.bot:
            try:
                await self.bot.send_message(admin, message, parse_mode=None)
            except Exception:
                log.warning("Studio notification was not delivered")


def default_root() -> Path:
    return Path(os.getenv("VKINO_DATA", "/tmp/vkino")) / "studio"

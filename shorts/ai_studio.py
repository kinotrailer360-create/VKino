"""Owner-only Telegram interface for the existing Open WebUI assistants."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import os
import secrets
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import netsafe, studio_voice

log = logging.getLogger(__name__)
BUTTON = "🧠 ИИ-студия"
PREFIX = "ИИ-студия · "
URL = "http://open-webui:8080/api/chat/completions"
MODES = {
    "news": ("📰 KinoMir360 — киноновости", "kinomir360---", "kinomir360-groq", True, "km360-editorial", []),
    "shorts": ("🎬 KinoMir360 — Shorts", "kinomir360---", "kinomir360-groq", True, "km360-editorial", []),
    "vikon": ("🎥 VIKON — сценарист", "vikon-analyst", "vikon-groq", True, "vikon-longform", []),
    "vkino": ("💻 VKino — разработчик", "vkino-technical", "vkino-groq", False, "vkino-production", ["vkino_github_reader"]),
    "personal": ("💬 Личный помощник", "models/gemini-flash-lite-latest", "openai/gpt-oss-20b", False, "", []),
}
PROVIDERS = {"auto": "Авто", "gemini": "Gemini", "groq": "Groq"}
RETRYABLE = {404, 408, 429, 500, 502, 503, 504}
SHORTS_FORMAT = """
Подготовь сценарий в точном формате существующего импортера VKino. Только Markdown без ограждений и вступления.
## Озвучка
Полная озвучка, слово в слово совпадающая с последовательными репликами таблицы.
## Карта видеоряда
| Сцена | Реплика | Что показать | Запрос RU / EN | Экранный текст |
|---|---|---|---|---|
| 1 (0–4с) | Первая точная реплика | Конкретный кадр | Поисковый запрос | Короткая надпись |
Далее 6–8 пронумерованных сцен с таймкодами, обычно 30–45 секунд, максимум 60.
Не вставляй символ | внутри ячеек. Реплики и текст озвучки строго совпадают.
## Заголовок
Название YouTube.
## Описание
1–2 предложения, вопрос, t.me/KinoMir360 и 3–5 тематических хэштегов.
Не выдумывай новости и источники. При нехватке данных сообщи об этом вместо готового плана.
Не утверждай, что видео найдено или смонтировано.
"""



MEMORY_SCOPES = {"all": "Общие правила", "km360": "KinoMir360", "vikon": "VIKON", "vkino": "VKino", "personal": "Личный помощник"}
MODE_SCOPE = {"news": "km360", "shorts": "km360", "vikon": "vikon", "vkino": "vkino", "personal": "personal"}
MEMORY_PREFIX = "Память ИИ-студии · "
MEMORY_TASK = "\n\nТЕКУЩЕЕ ЗАДАНИЕ ВЛАДЕЛЬЦА:\n"
REMEMBER = re.compile(r"^\s*запомни\s*:\s*(.+)$", re.I | re.S)
MAX_RULES = 10
MAX_RULE_CHARS = 400


def normalize_rule(text):
    rule = " ".join(text.split())
    if not rule or len(rule) > MAX_RULE_CHARS:
        raise ValueError("Правило должно содержать от 1 до 400 символов.")
    # Do not intentionally store obvious credentials. This is not a full secret detector.
    if re.search(r"\b(?:sk-[A-Za-z0-9_-]{16,}|AIza[A-Za-z0-9_-]{20,}|gsk_[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_-]{16,})\b", rule):
        raise ValueError("Не сохраняй API-ключи, пароли и токены в памяти.")
    return rule


def memory_prompt(question, rules):
    if not rules:
        return question
    # Open WebUI may replace a caller-provided system prompt with the custom model's
    # system prompt. Keep preferences in a clearly delimited user message instead.
    # This never replaces the configured model instructions or production approval.
    return ("Постоянные рабочие предпочтения, явно сохранённые владельцем. "
            "Применяй только относящиеся к текущей задаче, не раскрывай их без запроса. "
            "Они не отменяют правила достоверности, безопасности и согласования. "
            "В поисковые запросы не включай сам список предпочтений. "
            "Текущее задание имеет приоритет над прежними предпочтениями.\n"
            + "\n".join("— " + text for text in rules) + MEMORY_TASK + question)


class StudioError(Exception):
    def __init__(self, text: str, retryable: bool = False):
        super().__init__(text)
        self.retryable = retryable


def sole_owner(admins: set[int], configured: str = "") -> int | None:
    if configured:
        return int(configured) if configured.isdecimal() and int(configured) in admins else None
    return next(iter(admins)) if len(admins) == 1 else None


def read_key(data_dir: Path) -> str:
    key = os.getenv("OPEN_WEBUI_API_KEY", "").strip()
    if not key:
        path = data_dir / "openwebui-api-key"
        try:
            if path.is_symlink() or path.stat().st_mode & 0o077:
                return ""
            key = path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return key if key and len(key) <= 2048 and not any(c.isspace() for c in key) else ""


def bounded(messages: list[dict]) -> list[dict]:
    result = []
    size = 0
    for item in reversed(messages[-12:]):
        content = item.get("content", "")
        if not isinstance(content, str) or item.get("role") not in {"user", "assistant"}:
            continue
        if size + len(content) > 24000:
            break
        result.append({"role": item["role"], "content": content})
        size += len(content)
    result.reverse()
    while result and result[0]["role"] != "user":
        result.pop(0)
    return result


def model_sequence(mode: str, provider: str):
    _, primary, reserve, *_ = MODES[mode]
    models = [("Gemini", primary), ("Groq", reserve)]
    return models[:1] if provider == "gemini" else models[1:] if provider == "groq" else models


class StudioStore:
    """Separate, bounded per-project conversations on the bot's persistent volume."""
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        with closing(sqlite3.connect(self.path, timeout=3)) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS threads(owner INTEGER, mode TEXT, messages TEXT NOT NULL, PRIMARY KEY(owner,mode))")
            conn.execute("CREATE TABLE IF NOT EXISTS preferences(owner INTEGER PRIMARY KEY, provider TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS drafts(owner INTEGER PRIMARY KEY, token TEXT NOT NULL, content TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS studio_rules(id INTEGER PRIMARY KEY AUTOINCREMENT, owner INTEGER NOT NULL, scope TEXT NOT NULL, content TEXT NOT NULL, UNIQUE(owner,scope,content))")
            yield conn


    def rules(self, owner, scope):
        if scope not in MEMORY_SCOPES:
            raise ValueError("Неизвестный раздел памяти.")
        with self.connect() as c:
            return c.execute("SELECT id, content FROM studio_rules WHERE owner=? AND scope=? ORDER BY id", (owner, scope)).fetchall()

    def remember(self, owner, scope, text):
        if scope not in MEMORY_SCOPES:
            raise ValueError("Неизвестный раздел памяти.")
        rule = normalize_rule(text)
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            existing = c.execute("SELECT id FROM studio_rules WHERE owner=? AND scope=? AND content=?", (owner, scope, rule)).fetchone()
            if existing:
                return existing[0], False
            count = c.execute("SELECT count(*) FROM studio_rules WHERE owner=? AND scope=?", (owner, scope)).fetchone()[0]
            if count >= MAX_RULES:
                raise ValueError("В этом разделе уже 10 правил. Удали устаревшее правило.")
            row = c.execute("INSERT INTO studio_rules(owner,scope,content) VALUES(?,?,?)", (owner, scope, rule))
            return row.lastrowid, True

    def forget(self, owner, scope, ident):
        if scope not in MEMORY_SCOPES:
            return False
        with self.connect() as c:
            return bool(c.execute("DELETE FROM studio_rules WHERE owner=? AND scope=? AND id=?", (owner, scope, ident)).rowcount)

    def preferences_for(self, owner, mode):
        scope = MODE_SCOPE[mode]
        return [content for name in ("all", scope) for _, content in self.rules(owner, name)]

    def history(self, owner, mode):
        with self.connect() as c:
            row = c.execute("SELECT messages FROM threads WHERE owner=? AND mode=?", (owner, mode)).fetchone()
        return bounded(json.loads(row[0])) if row else []

    def save(self, owner, mode, messages):
        with self.connect() as c:
            c.execute("INSERT OR REPLACE INTO threads VALUES(?,?,?)", (owner, mode, json.dumps(bounded(messages), ensure_ascii=False)))

    def reset(self, owner, mode):
        with self.connect() as c:
            c.execute("DELETE FROM threads WHERE owner=? AND mode=?", (owner, mode))
            if mode == "shorts":
                c.execute("DELETE FROM drafts WHERE owner=?", (owner,))

    def provider(self, owner):
        with self.connect() as c:
            row = c.execute("SELECT provider FROM preferences WHERE owner=?", (owner,)).fetchone()
        return row[0] if row and row[0] in PROVIDERS else "auto"

    def set_provider(self, owner, provider):
        if provider not in PROVIDERS:
            raise ValueError("Invalid provider")
        with self.connect() as c:
            c.execute("INSERT OR REPLACE INTO preferences VALUES(?,?)", (owner, provider))

    def draft(self, owner, text):
        token = secrets.token_hex(8)
        with self.connect() as c:
            c.execute("INSERT OR REPLACE INTO drafts VALUES(?,?,?)", (owner, token, text))
        return token

    def get_draft(self, owner, token):
        with self.connect() as c:
            row = c.execute("SELECT content FROM drafts WHERE owner=? AND token=?", (owner, token)).fetchone()
        return row[0] if row else None

    def consume_draft(self, owner, token):
        with self.connect() as c:
            c.execute("DELETE FROM drafts WHERE owner=? AND token=?", (owner, token))

    def invalidate_draft(self, owner):
        with self.connect() as c:
            c.execute("DELETE FROM drafts WHERE owner=?", (owner,))



# Source URLs are data returned by Open WebUI, not text invented by the model.
_SOURCE_HEADING = re.compile(r"(?im)^\s*(?:#{1,6}[ \t]+)?(?:\*\*)?(?:источники(?:\s+(?:информации|для проверки))?|ссылки на источники|проверенные источники|sources)\s*(?:\*\*)?\s*:?\s*(?:\*\*)?[ \t]*$")
_CITATION = re.compile(r"\[(\d{1,4})\](?!\()")
_MARKDOWN_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_BARE_URL = re.compile(r"https?://[^\s<>\]\[()]+")
_VERIFY = re.compile(r"\b(?:проверь|проверить|проверка|фактчек|достоверно|правда ли)\b", re.I)


def grounded_news_answer(answer: str, data: dict, question: str) -> str:
    """Use exact retrieved web URLs; never treat citation markers as URLs."""
    records = []
    for item in data.get("sources", []) or []:
        if not isinstance(item, dict) or (item.get("source") or {}).get("type") != "web_search":
            continue
        ids = item.get("citation_ids") or []
        for i, meta in enumerate(item.get("metadata") or []):
            if not isinstance(meta, dict):
                continue
            url = meta.get("source") or meta.get("url")
            if not isinstance(url, str) or len(url) > 2000 or any(ch.isspace() for ch in url):
                continue
            try:
                parsed = urlsplit(url)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                    continue
            except ValueError:
                continue
            title = str(meta.get("title") or parsed.hostname).replace("\n", " ")[:200]
            cid = str(ids[i]) if i < len(ids) else ""
            records.append((cid, title, url))
    match = _SOURCE_HEADING.search(answer)
    body = answer[:match.start()] if match else answer
    cited = set(_CITATION.findall(body)) or set(_CITATION.findall(answer))
    needs_sources = bool(match or cited or _VERIFY.search(question))
    allowed = {r[2] for r in records}
    # Keep exact retrieved links in prose, but do not expose made-up links.
    body = _MARKDOWN_LINK.sub(lambda m: m[0] if m[2] in allowed else m[1], body)
    body = _BARE_URL.sub(lambda m: m[0] if m[0].rstrip(".,;:!?") in allowed else "", body)
    body = _CITATION.sub("", body)
    body = re.sub(r"[ \t]+([.,;:!?])", r"\1", body).strip()
    if not needs_sources:
        return body
    selected = [r for r in records if r[0] in cited] if cited else []
    if not selected:
        selected = records
    seen = set()
    links = []
    for _, title, url in selected:
        if url not in seen:
            links.append(f"• {title}\n{url}")
            seen.add(url)
        if len(links) >= 4:
            break
    if links:
        return body + "\n\nИсточники из поиска (отдельная проверка доступности ссылок не выполнялась):\n" + "\n\n".join(links)
    body = re.sub(r"(?im)^(\s*(?:\*\*)?)(?:частично )?подтверждено\b", r"\1Не удалось подтвердить", body, count=1)
    return ("⚠️ Поиск не вернул ссылки на веб-источники; достоверность не подтверждена.\n\n" + body).strip()



_SOURCE_BLOCK = "\n\nИсточники из поиска (отдельная проверка доступности ссылок не выполнялась):\n"


async def _source_status(session: aiohttp.ClientSession, url: str) -> str:
    """Check only HTTP access, not whether an article supports the claim."""
    try:
        # Use the same DNS and redirect protections as existing public media fetches.
        async with asyncio.timeout(7):
            for _ in range(4):
                parts = urlsplit(url)
                if (parts.scheme not in {"http", "https"} or not parts.hostname
                        or parts.username or parts.password or parts.port not in (None, 80, 443)):
                    return "небезопасный адрес"
                await netsafe.check_url(url)
                async with session.get(url, allow_redirects=False) as response:
                    if response.status in (301, 302, 303, 307, 308) and response.headers.get("Location"):
                        url = urljoin(str(response.url), response.headers["Location"])
                        continue
                    if 200 <= response.status < 300:
                        content_type = response.headers.get("Content-Type", "").lower()
                        if "text/html" in content_type or "application/xhtml+xml" in content_type:
                            return f"открывается (HTTP {response.status})"
                        return f"доступен ответ HTTP {response.status}, не HTML-статья"
                    return f"не удалось открыть (HTTP {response.status})"
            return "не проверено: слишком много перенаправлений"
    except (aiohttp.ClientError, OSError, ValueError, TimeoutError, netsafe.UnsafeURL):
        return "не удалось проверить доступность"


async def verify_news_links(answer: str) -> str:
    """Verify at most four exact URLs in the generated metadata-only source block."""
    body, separator, block = answer.partition(_SOURCE_BLOCK)
    if not separator:
        return answer
    lines = block.splitlines()
    indices = [i for i, line in enumerate(lines) if line.startswith(("https://", "http://"))][:4]
    if not indices:
        return answer
    try:
        async with netsafe.safe_session(
            timeout=aiohttp.ClientTimeout(total=7, connect=3),
            trust_env=False,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as session:
            statuses = await asyncio.gather(*(_source_status(session, lines[i]) for i in indices))
    except (aiohttp.ClientError, OSError, ValueError, TimeoutError):
        statuses = ["не удалось проверить доступность"] * len(indices)
    for i, status in zip(indices, statuses):
        lines[i] += "\nДоступность: " + status
    return (body + "\n\nИсточники из поиска (HTTP-проверка с VPS; не доказательство содержания статьи):\n"
            + "\n".join(lines))


class StudioClient:
    def __init__(self, data_dir):
        self.data_dir = data_dir

    async def _request(self, model, messages, mode):
        key = read_key(self.data_dir)
        if not key:
            raise StudioError("Нужно подключить API-ключ Open WebUI на VPS.")
        _, _, _, search, skill, tools = MODES[mode]
        payload = {
            "model": model, "messages": messages, "stream": False,
            "features": {"web_search": search, "memory": True},
            "tool_ids": tools,
        }
        if skill:
            payload["skill_ids"] = [skill]
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=100, connect=8),
                                             trust_env=False) as session:
                async with session.post(URL, json=payload, headers={"Authorization": "Bearer " + key},
                                        allow_redirects=False) as response:
                    if response.status in (401, 403):
                        raise StudioError("Open WebUI отклонил доступ. Проверь ключ и разрешения.")
                    if response.status != 200:
                        raise StudioError(f"Open WebUI: HTTP {response.status}.", response.status in RETRYABLE)
                    data = await response.json(content_type=None)
        except StudioError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise StudioError("Open WebUI недоступен или не успел ответить.", True) from None
        except (ValueError, TypeError):
            raise StudioError("Некорректный ответ Open WebUI.") from None
        try:
            answer = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise StudioError("Open WebUI не вернул текст ответа.") from None
        if not isinstance(answer, str) or not answer.strip():
            raise StudioError("Модель вернула пустой ответ.", True)
        if len(answer) > 30000:
            raise StudioError("Слишком длинный ответ. Раздели задачу на части.")
        if mode == "news":
            question = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
            if MEMORY_TASK in question:
                question = question.rsplit(MEMORY_TASK, 1)[-1]
            return await verify_news_links(grounded_news_answer(answer, data, question))
        return answer.strip()

    async def ask(self, mode, provider, history, question, *, rules=()):
        messages = bounded(history + [{"role": "user", "content": question}])
        if not messages:
            raise StudioError("Запрос слишком длинный.")
        prepared = memory_prompt(question, rules)
        if mode == "shorts":
            prepared += "\n\n" + SHORTS_FORMAT
        messages[-1] = {"role": "user", "content": prepared}
        sequence = model_sequence(mode, provider)
        for i, (label, model) in enumerate(sequence):
            try:
                return await self._request(model, messages, mode), label
            except StudioError as exc:
                if not exc.retryable or i == len(sequence) - 1:
                    raise
                log.info("AI Studio: trying reserve provider")
        raise StudioError("Нет доступной модели.")


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row] for row in rows
    ])


def menu(provider):
    return keyboard([[(data[0], "ais:m:" + mode)] for mode, data in MODES.items()]
                    + [[("🧠 Память и правила", "ais:memory")], [("ИИ: " + PROVIDERS[provider], "ais:providers")]])


def chunks(text):
    while text:
        end = min(1700, len(text))
        if end < len(text):
            split = text.rfind("\n", 700, end)
            if split > 0:
                end = split
        yield text[:end]
        text = text[end:].lstrip("\n")


def build_router(admin_ids, data_dir, validate_script, handoff, busy):
    router = Router(name="openwebui_ai_studio")
    owner = sole_owner(admin_ids, os.getenv("OPEN_WEBUI_TELEGRAM_OWNER_ID", "").strip())
    store = StudioStore(data_dir / "ai_studio.sqlite3")
    client = StudioClient(data_dir)
    lock = asyncio.Lock()

    def allowed(message, sender):
        return owner is not None and sender == owner and message.chat.type == "private" and message.chat.id == owner

    async def show_menu(message):
        await message.answer("🧠 ИИ-студия\nВыбери помощника. Диалоги проектов хранятся отдельно.",
                             reply_markup=menu(store.provider(owner)), parse_mode=None)

    async def prompt(message, mode):
        await message.answer(PREFIX + MODES[mode][0] + "\nОтветь текстом или запиши голосовое сообщение (до 2 минут). Голос распознаётся через Groq или Gemini.",
                             reply_markup=ForceReply(selective=False, input_field_placeholder="Вопрос или задание"),
                             parse_mode=None)

    async def show_memory(message, scope):
        records = store.rules(owner, scope)
        await message.answer("🧠 " + MEMORY_SCOPES[scope] + "\n" +
                             ("\n\n".join(f"{ident}. {text}" for ident, text in records) if records else "Сохранённых правил пока нет."),
                             parse_mode=None, disable_web_page_preview=True)
        rows = [[("➕ Добавить правило", "ais:memadd:" + scope)]]
        rows.extend([[(f"Удалить №{ident}", f"ais:memdel:{scope}:{ident}")] for ident, _ in records])
        rows.append([("⬅️ Разделы памяти", "ais:memory")])
        await message.answer("Управление правилами", reply_markup=keyboard(rows), parse_mode=None)

    async def save_rule(message, scope, text):
        try:
            ident, created = store.remember(owner, scope, text)
        except ValueError as exc:
            await message.answer("⚠️ " + str(exc), parse_mode=None)
            return
        await message.answer(("✅ Правило сохранено" if created else "Это правило уже сохранено")
                             + f" · {MEMORY_SCOPES[scope]} · №{ident}.",
                             reply_markup=keyboard([[("🧠 Правила проекта", "ais:mem:" + scope)], [("⬅️ ИИ-студия", "ais:home")]]),
                             parse_mode=None)

    async def run_question(message, mode, question="", *, voice=False):
        question = question.strip()
        if not voice and (not question or len(question) > 10000):
            await message.answer("Пришли текст от 1 до 10 000 символов.", parse_mode=None)
            return
        if lock.locked():
            await message.answer("ИИ-студия уже обрабатывает запрос.", parse_mode=None)
            return
        if not voice and (match := REMEMBER.fullmatch(question)):
            await save_rule(message, MODE_SCOPE[mode], match[1])
            return
        async with lock:
            if not read_key(data_dir):
                await message.answer("ИИ-студия установлена, но API-ключ Open WebUI ещё не подключён на VPS.", parse_mode=None)
                return
            status = await message.answer("🎙 Распознаю голос…" if voice else "⏳ ИИ-студия обрабатывает запрос…", parse_mode=None)
            try:
                if voice:
                    raw = await studio_voice.download_voice(message)
                    question, stt_provider = await studio_voice.transcribe_voice(raw)
                    del raw
                    for part in chunks("🎙 Распознано (" + stt_provider + "):\n" + question):
                        await message.answer(part, parse_mode=None, disable_web_page_preview=True)
                    if REMEMBER.fullmatch(question):
                        await status.edit_text("Для постоянной памяти подтверди распознанное правило текстом через меню «Память и правила».", parse_mode=None)
                        return
                    await status.edit_text("⏳ ИИ-студия обрабатывает запрос…", parse_mode=None)
                history = store.history(owner, mode)
                if mode == "shorts":
                    store.invalidate_draft(owner)
                answer, label = await client.ask(mode, store.provider(owner), history, question, rules=store.preferences_for(owner, mode))
                store.save(owner, mode, history + [{"role": "user", "content": question}, {"role": "assistant", "content": answer}])
                token = None
                if mode == "shorts":
                    try:
                        validate_script(answer)
                    except (ValueError, TypeError):
                        pass
                    else:
                        token = store.draft(owner, answer)
                await status.edit_text("✅ Ответ · " + label, parse_mode=None)
                for part in chunks(answer):
                    await message.answer(part, parse_mode=None, disable_web_page_preview=True)
                rows = [[("💬 Продолжить", "ais:m:" + mode), ("🆕 Новый диалог", "ais:reset:" + mode)]]
                if token:
                    rows.insert(0, [("🎬 Передать сценарий на согласование", "ais:render:" + token)])
                rows.append([("🧠 Правила проекта", "ais:mem:" + MODE_SCOPE[mode]), ("⬅️ ИИ-студия", "ais:home")])
                await message.answer("Что дальше?", reply_markup=keyboard(rows), parse_mode=None)
            except (StudioError, studio_voice.VoiceError) as exc:
                await status.edit_text("⚠️ " + str(exc), parse_mode=None)
                await message.answer("Повтори запрос или выбери помощника.", reply_markup=menu(store.provider(owner)), parse_mode=None)
            except Exception:
                log.warning("AI Studio request failed")
                await status.edit_text("⚠️ Не удалось обработать запрос. Попробуй позже.", parse_mode=None)

    @router.message(Command("aistudio"))
    async def command(message: Message, command: CommandObject):
        if not allowed(message, message.from_user.id if message.from_user else None):
            return
        mode, _, question = (command.args or "").strip().partition(" ")
        if mode not in MODES:
            await show_menu(message)
        elif question.strip():
            await run_question(message, mode, question)
        else:
            await prompt(message, mode)

    @router.message(F.text == BUTTON)
    async def main_button(message: Message):
        if allowed(message, message.from_user.id if message.from_user else None):
            await show_menu(message)

    @router.message(F.text, ~F.text.startswith("/"), F.reply_to_message.text.startswith(PREFIX))
    async def reply(message: Message):
        if not allowed(message, message.from_user.id if message.from_user else None):
            return
        quoted = message.reply_to_message
        if not quoted or not quoted.from_user or quoted.from_user.id != message.bot.id:
            return
        first = (quoted.text or "").splitlines()[0]
        mode = next((k for k, v in MODES.items() if first == PREFIX + v[0]), None)
        if mode:
            await run_question(message, mode, message.text or "")

    @router.message(F.voice, F.reply_to_message.text.startswith(PREFIX))
    async def voice_reply(message: Message):
        if not allowed(message, message.from_user.id if message.from_user else None):
            return
        quoted = message.reply_to_message
        if not quoted or not quoted.from_user or quoted.from_user.id != message.bot.id:
            return
        first = (quoted.text or "").splitlines()[0]
        mode = next((k for k, v in MODES.items() if first == PREFIX + v[0]), None)
        if mode:
            await run_question(message, mode, voice=True)

    @router.message(F.text, ~F.text.startswith("/"), F.reply_to_message.text.startswith(MEMORY_PREFIX))
    async def memory_reply(message: Message):
        if not allowed(message, message.from_user.id if message.from_user else None):
            return
        quoted = message.reply_to_message
        if not quoted or not quoted.from_user or quoted.from_user.id != message.bot.id:
            return
        first = (quoted.text or "").splitlines()[0]
        scope = next((k for k, label in MEMORY_SCOPES.items() if first == MEMORY_PREFIX + label), None)
        if scope:
            if lock.locked():
                await message.answer("Дождись ответа ИИ.", parse_mode=None)
                return
            await save_rule(message, scope, message.text or "")

    @router.callback_query(F.data.startswith("ais:"))
    async def callback(query: CallbackQuery, state: FSMContext):
        message = query.message
        if not isinstance(message, Message) or not allowed(message, query.from_user.id):
            await query.answer("Нет доступа", show_alert=True)
            return
        data = (query.data or "").split(":")
        action = data[1] if len(data) > 1 else ""
        value = data[2] if len(data) > 2 else ""
        if lock.locked():
            await query.answer("Дождись ответа ИИ.", show_alert=True)
            return
        await query.answer()
        if action == "home":
            await show_menu(message)
        elif action == "m" and value in MODES:
            await prompt(message, value)
        elif action == "reset" and value in MODES:
            store.reset(owner, value)
            await prompt(message, value)
        elif action == "memory":
            await message.answer("🧠 Постоянные правила. Общие применяются ко всем помощникам. KinoMir360 — к новостям и Shorts. "
                                 "Храни только рабочие предпочтения, не пароли и личные секреты.",
                                 reply_markup=keyboard([[(label, "ais:mem:" + scope)] for scope, label in MEMORY_SCOPES.items()]
                                                       + [[("⬅️ ИИ-студия", "ais:home")]]), parse_mode=None)
        elif action == "mem" and value in MEMORY_SCOPES:
            await show_memory(message, value)
        elif action == "memadd" and value in MEMORY_SCOPES:
            await message.answer(MEMORY_PREFIX + MEMORY_SCOPES[value] + "\nОтветь текстом: одно правило до 400 символов. "
                                 "Не добавляй ключи, пароли и секреты. Правила передаются ИИ при запросах.",
                                 reply_markup=ForceReply(selective=False, input_field_placeholder="Постоянное правило"), parse_mode=None)
        elif action == "memdel" and value in MEMORY_SCOPES and len(data) == 4 and data[3].isdigit() and len(data[3]) < 18:
            removed = store.forget(owner, value, int(data[3]))
            await message.answer("Правило удалено из памяти ИИ-студии." if removed else "Правило уже отсутствует.", parse_mode=None)
            await show_memory(message, value)
        elif action == "providers":
            await message.answer("Авто: Gemini, при временном сбое — Groq.",
                                 reply_markup=keyboard([[(label, "ais:p:" + k)] for k, label in PROVIDERS.items()]), parse_mode=None)
        elif action == "p" and value in PROVIDERS:
            store.set_provider(owner, value)
            await show_menu(message)
        elif action in {"render", "replace"} and len(value) == 16:
            async with lock:
                draft = store.get_draft(owner, value)
                if not draft:
                    await message.answer("Черновик устарел или уже передан.", parse_mode=None)
                    return
                current = await state.get_data()
                if busy() or current.get("structured_rendering") or await state.get_state() is not None:
                    await message.answer("Сначала заверши текущий монтаж или активный диалог. Черновик сохранён.", parse_mode=None)
                    return
                if current.get("structured_id") and action != "replace":
                    await message.answer("Уже есть черновик монтажа. Заменить его? Монтаж автоматически не начнётся.",
                                         reply_markup=keyboard([[("Заменить черновик", "ais:replace:" + value)], [("Назад", "ais:home")]]), parse_mode=None)
                    return
                try:
                    plan = validate_script(draft)
                except (ValueError, TypeError):
                    await message.answer("Формат сценария не прошёл проверку.", parse_mode=None)
                    return
                await handoff(message, state, plan)
                store.consume_draft(owner, value)

    return router

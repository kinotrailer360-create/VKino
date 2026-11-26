"""Owner-only Telegram interface for the existing Open WebUI assistants."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

import aiohttp
from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Message

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
            yield conn

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
        return answer.strip()

    async def ask(self, mode, provider, history, question):
        messages = bounded(history + [{"role": "user", "content": question}])
        if not messages:
            raise StudioError("Запрос слишком длинный.")
        if mode == "shorts":
            messages[-1] = {"role": "user", "content": question + "\n\n" + SHORTS_FORMAT}
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
                    + [[("ИИ: " + PROVIDERS[provider], "ais:providers")]])


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
        await message.answer(PREFIX + MODES[mode][0] + "\nОтветь на это сообщение своим вопросом или заданием.",
                             reply_markup=ForceReply(selective=False, input_field_placeholder="Вопрос или задание"),
                             parse_mode=None)

    async def run_question(message, mode, question):
        question = question.strip()
        if not question or len(question) > 10000:
            await message.answer("Пришли текст от 1 до 10 000 символов.", parse_mode=None)
            return
        if lock.locked():
            await message.answer("ИИ-студия уже обрабатывает запрос.", parse_mode=None)
            return
        async with lock:
            if not read_key(data_dir):
                await message.answer("ИИ-студия установлена, но API-ключ Open WebUI ещё не подключён на VPS.", parse_mode=None)
                return
            status = await message.answer("⏳ ИИ-студия обрабатывает запрос…", parse_mode=None)
            try:
                history = store.history(owner, mode)
                if mode == "shorts":
                    store.invalidate_draft(owner)
                answer, label = await client.ask(mode, store.provider(owner), history, question)
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
                rows.append([("⬅️ ИИ-студия", "ais:home")])
                await message.answer("Что дальше?", reply_markup=keyboard(rows), parse_mode=None)
            except StudioError as exc:
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

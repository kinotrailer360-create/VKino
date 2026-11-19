"""Telegram UI for the opt-in VKino Windows assembly queue.

Call register(router, get_studio, admins) once, before the generic callback
handler. No service is started by importing this module.
"""
from __future__ import annotations

from html import escape
from uuid import UUID

from . import studio_pc

LABELS = {
    'queued': '⏳ В очереди', 'running': '🖥 Монтируется на ПК',
    'awaiting_review': '✅ Черновик готов, ждёт проверки',
    'failed': '⚠️ Ошибка монтажа', 'interrupted': '⏸ Прервано',
    'cancelled': '🚫 Отменено',
}


def register(router, get_studio, admins: set[int]) -> None:
    """Keep all buttons private; check owner on every callback, not only at menu."""
    from aiogram import F
    from aiogram.filters import Command
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, FSInputFile

    def authorized(user, chat) -> bool:
        return bool(user and user.id in admins and chat and chat.type == 'private')

    async def make_menu(studio, admin):
        sources = await studio_pc.list_ready(studio, admin)
        jobs = await studio_pc.list_remote(studio, admin)
        buttons = []
        for entry in sources:
            buttons.append([InlineKeyboardButton(
                text='🖥 Смонтировать: ' + entry['title'][:30],
                callback_data='studio_pc:go:' + entry['id'])])
        for entry in jobs:
            buttons.append([InlineKeyboardButton(
                text=f"{LABELS.get(entry['status'], entry['status'])}: {entry['title'][:22]}",
                callback_data='studio_pc:status:' + entry['id'])])
        txt = ('🖥 <b>VKino Studio — монтаж на ПК</b>\n\n'
               'Кнопка отправляет на Windows только готовые и утверждённые MP4-фрагменты '
               'из завершённого проекта KinoMir360 Studio.\n'
               'Сценарий, кадры и каждый блок должны быть утверждены заранее.\n\n'
               'Ни одна публикация не выполняется автоматически.')
        if not sources:
            txt += '\n\nНет завершённых утверждённых сборок. Сначала согласуйте все сцены и соберите ролик на сайте.'
        if not jobs:
            txt += '\n\nОчередь ПК-монтажа пока пуста.'
        return txt, InlineKeyboardMarkup(inline_keyboard=buttons or [[
            InlineKeyboardButton(text='🔄 Обновить', callback_data='studio_pc:menu')]])

    @router.message(Command('studio_pc'))
    async def pc_command(message):
        if not authorized(message.from_user, message.chat):
            return
        studio = get_studio()
        if studio is None or studio.worker_api is None:
            await message.answer('ПК-монтаж ещё не включён на сервере.')
            return
        text, markup = await make_menu(studio, message.from_user.id)
        await message.answer(text, reply_markup=markup)

    @router.callback_query(F.data.startswith('studio_pc:'))
    async def pc_callback(callback):
        message = callback.message
        if not message or not authorized(callback.from_user, message.chat):
            await callback.answer('Нет доступа', show_alert=True)
            return
        studio = get_studio()
        if studio is None or studio.worker_api is None:
            await callback.answer('ПК-монтаж недоступен', show_alert=True)
            return
        parts = (callback.data or '').split(':', 2)
        action = parts[1] if len(parts) > 1 else ''
        ident = parts[2] if len(parts) == 3 else ''
        await callback.answer()
        try:
            if action == 'menu':
                text, markup = await make_menu(studio, callback.from_user.id)
                await message.answer(text, reply_markup=markup)
                return
            if action not in {'go', 'status', 'result'}:
                return
            ident = str(UUID(ident))
            if action == 'go':
                job_id, status = await studio_pc.enqueue_from_studio(studio, callback.from_user.id, ident)
                await message.answer('🖥 Задание принято. ' + LABELS.get(status, status)
                      + '\nКомпьютер получит его, когда Windows-агент будет запущен.\n'
                      'После сборки нужен твой финальный просмотр.',
                      reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                          InlineKeyboardButton(text='📋 Проверить статус',
                                               callback_data='studio_pc:status:' + job_id)]]))
                return
            if action == 'result':
                path, _ = await studio_pc.owner_result(studio, callback.from_user.id, ident)
                if path.stat().st_size > 45 * 1024 * 1024:
                    await message.answer('Видео больше 45 МБ: через Telegram отправка ограничена. '
                                         'Для скачивания используй защищённый раздел Studio '
                                         'после подключения просмотра результатов ПК-монтажа.')
                    return
                await message.answer_video(FSInputFile(str(path)),
                    caption='🎬 Черновик ПК-монтажа. Проверь все сцены и права на материал. '
                            'Ролик НЕ опубликован.', supports_streaming=True)
                return
            row = await studio.pool.fetchrow(
                'SELECT status,error,payload FROM studio_worker_jobs WHERE id=$1 AND admin_id=$2',
                ident, callback.from_user.id)
            if not row:
                raise ValueError('Задание не найдено')
            title = str(studio_pc.obj(row['payload']).get('project', {}).get('title') or 'Проект')
            text = f"🎬 <b>{escape(title[:100])}</b>\n{LABELS.get(row['status'], row['status'])}"
            if row['error']:
                text += '\n' + escape(str(row['error'])[:180])
            if row['status'] == 'awaiting_review':
                text += '\nВидеофайл требует финального просмотра. Автопубликации нет.'
                keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text='🎞 Получить черновик',
                                         callback_data='studio_pc:result:' + ident)]])
            else:
                keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text='🔄 Обновить статус',
                                         callback_data='studio_pc:status:' + ident)]])
            await message.answer(text, reply_markup=keyboard)
        except (ValueError, TypeError, KeyError) as exc:
            await message.answer('⚠️ ' + escape(str(exc)[:180]))
        except Exception:
            # Never disclose credentials, absolute paths or API responses to Telegram.
            await message.answer('⚠️ Не удалось выполнить действие. Проверь журнал VKino.')
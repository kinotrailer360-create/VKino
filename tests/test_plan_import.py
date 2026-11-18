import asyncio
import importlib
import io
from datetime import date
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest

from shorts import plan_import


@pytest.fixture
def bot(monkeypatch):
    for key, value in {"BOT_TOKEN": "123456:TEST", "KINOPOISK_TOKEN": "test",
                       "DATABASE_URL": "postgresql://test", "ADMIN_IDS": "1"}.items():
        monkeypatch.setenv(key, value)
    return importlib.import_module("bot")


def make_docx(*lines: str) -> bytes:
    root = ElementTree.Element(plan_import.WORD + "document")
    for line in lines:
        paragraph = ElementTree.SubElement(root, plan_import.WORD + "p")
        run = ElementTree.SubElement(paragraph, plan_import.WORD + "r")
        ElementTree.SubElement(run, plan_import.WORD + "t").text = line
    buf = io.BytesIO()
    with ZipFile(buf, "w") as archive:
        archive.writestr("word/document.xml", ElementTree.tostring(root))
    return buf.getvalue()


def test_uploaded_week_appears_in_daily_reminder_and_skips_empty_days(bot):
    start, week = plan_import.parse(make_docx(
        "Неделя публикаций: 23–29 ноября 2026",
        "ПН • Короткое видео", "Озвучка (30 секунд)", "Текст для диктора.",
        "Описание: Короткое описание", "Хэштеги: #Shorts", "Карта видеоряда — отдельно от озвучки",
        "1. Кадр: Титульная карточка",
        "ВС • Длинный ролик: Разбор", "Заголовок: «Тема разбора»",
        "Вступление", "ОЗВУЧКА: Начало разбора.", "ВИДЕОРЯД: Вступительные титры",
    ))
    assert bot.plan_week_key(start) == "editorial_plan:2026-11-23"
    assert week["2026-11-23"]["narration"] == "Текст для диктора."
    assert week["2026-11-23"]["visuals"] == ["1. Кадр: Титульная карточка"]
    text, keyboard = bot.plan_text(start, week)
    assert "Короткое видео" in text
    assert keyboard.inline_keyboard[0][0].callback_data == "pl:go:2026-11-23"
    assert bot.plan_text(date(2026, 11, 28), week)[1] is None
    text, keyboard = bot.plan_text(date(2026, 11, 29), week)
    assert "Длинное видео" in text and "Тема разбора" in text
    assert keyboard.inline_keyboard[0][0].callback_data == "pl:txt:2026-11-29"


def test_invalid_upload_is_rejected_and_cross_month_works():
    with pytest.raises(ValueError, match="разделы"):
        plan_import.parse(make_docx("Неделя публикаций: 23–29 ноября 2026"))
    monday, week = plan_import.parse(make_docx(
        "Неделя публикаций: 30 ноября – 6 декабря 2026", "ВС • Тема", "Озвучка", "Первый абзац."
    ))
    assert monday == date(2026, 11, 30)
    assert "2026-12-06" in week


def test_long_plan_telegram_response_does_not_truncate(bot):
    class Message:
        chunks = []

        async def answer(self, text, **kwargs):
            self.chunks.append(text)

    message = Message()
    script = {"title": "Разбор", "sections": [
        {"heading": "Часть", "narration": "А" * 5000, "visuals": []},
        {"heading": "Финал", "narration": "Последняя строка.", "visuals": []},
    ]}
    asyncio.run(bot.send_plan_entry(message, script, visual=False))
    assert len(message.chunks) > 1
    assert all(len(chunk) <= 3900 for chunk in message.chunks)
    assert "Последняя строка." in message.chunks[-1]

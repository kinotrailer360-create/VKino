"""Каркас бота: модуль импортируется, у каждой кнопки есть обработчик, данные кнопок влезают в 64 байта.
Ловит ошибки правок, которых не видят ruff и py_compile (несуществующее имя на уровне модуля, кнопка без
обработчика после переноса кода)."""

import os
import pathlib
import re

for name, value in {"BOT_TOKEN": "123456:TEST", "ADMIN_IDS": "1", "KINOPOISK_TOKEN": "test",
                    "DATABASE_URL": "postgresql://test@localhost/test"}.items():
    os.environ.setdefault(name, value)

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = (ROOT / "bot.py").read_text(encoding="utf-8")


def test_bot_imports() -> None:
    import bot  # noqa: F401


def test_every_button_prefix_has_handler() -> None:
    used = set(re.findall(r'_btn\([^\n]*?,\s*f?"([a-z]+):', SRC))
    used |= set(re.findall(r'callback_data=f?"([a-z]+):', SRC))
    handled = set(re.findall(r'F\.data\.startswith\(\s*"([a-z]+):', SRC))
    handled |= set(re.findall(r'F\.data\s*==\s*"([a-z]+):', SRC))
    handled |= set(re.findall(r'F\.data\.in_\(\{?\[?\s*"([a-z]+):', SRC))
    assert not used - handled, f"кнопки без обработчика: {sorted(used - handled)}"


def test_static_button_data_fits_telegram() -> None:
    for data in re.findall(r'_btn\([^\n]*?,\s*"([^"{}]+)"\)', SRC):
        assert len(data.encode()) <= 64, data


def test_weekly_own_text_split() -> None:
    """«📝 Свой текст»: скопированное сообщение новости → фразы для голоса, без шапки-ссылок на источники."""
    import bot
    text = "Первая фраза. Вторая фраза!\nТретья фраза?\n\n📎 Источники: t.me/a, t.me/b"
    assert bot._wd_phrases(text) == ["Первая фраза.", "Вторая фраза!", "Третья фраза?"]


def test_lore_media_links_and_og_image(tmp_path, monkeypatch) -> None:
    """Ссылки вместо файлов: «3 https://…» — к фразе 3; страница — её og:image; не картинка — ошибка."""
    import asyncio
    import io

    import pytest
    from PIL import Image

    import bot
    assert bot.IMG_URL.findall("3 https://a.com/x.jpg?w=1\nhttps://b.com/y.png") == \
        [("3", "https://a.com/x.jpg?w=1"), ("", "https://b.com/y.png")]
    buf = io.BytesIO()
    Image.new("RGB", (40, 20), (200, 0, 0)).save(buf, "PNG")
    pages = {"https://site.com/page": (b'<meta property="og:image" content="/img.png">', "text/html"),
             "https://site.com/img.png": (buf.getvalue(), "image/png"),
             "https://site.com/none": (b"<html></html>", "text/html")}

    async def fake_get(session, url, limit, trusted):
        return pages[url]

    monkeypatch.setattr(bot.netsafe, "_get", fake_get)
    asyncio.run(bot.fetch_image_url("https://site.com/page", tmp_path / "a.jpg"))
    assert Image.open(tmp_path / "a.jpg").size == (40, 20)
    with pytest.raises(RuntimeError):
        asyncio.run(bot.fetch_image_url("https://site.com/none", tmp_path / "b.jpg"))


def test_task_mode_docx_and_parse(monkeypatch) -> None:
    """Задание целиком: .docx читается (со ссылками), распознаётся как задание, ИИ-разбор → фразы, ссылки, надписи."""
    import asyncio
    import io
    import zipfile

    import bot
    from shorts import ai
    doc = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
           'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>'
           '<w:p><w:r><w:t>Сценарий озвучки</w:t></w:r></w:p>'
           '<w:p><w:hyperlink r:id="rId5"><w:r><w:t>кадр</w:t></w:r></w:hyperlink></w:p></w:body></w:document>')
    rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId5" Target="https://a.com/x.jpg"/></Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", doc)
        z.writestr("word/_rels/document.xml.rels", rels)
    assert bot.docx_text(buf.getvalue()) == "Сценарий озвучки\nкадр https://a.com/x.jpg"
    assert bot.looks_like_task("Сценарий озвучки: " + "текст " * 150 + " кадр https://x")
    assert not bot.looks_like_task("Просто фраза.\nЕщё фраза.")

    async def fake(key, parts, prompt, image=None, premium=False):
        return {"title": "T", "lines": [{"text": "В список попали:", "url": "", "label": "11 ПРОЕКТОВ"},
                                        {"text": "«Локи».", "url": "https://x.com/loki.jpg"},
                                        {"text": "«Тор».", "url": "https://www.google.com/search?q=thor"}]}
    monkeypatch.setattr(ai, "_with_reserve", fake)
    plan = asyncio.run(ai.parse_task("k", "задание " * 20))
    assert [x["url"] for x in plan["lines"]] == ["", "https://x.com/loki.jpg", ""]  # поиск Google — не картинка
    assert plan["lines"][0]["label"] == "11 ПРОЕКТОВ"

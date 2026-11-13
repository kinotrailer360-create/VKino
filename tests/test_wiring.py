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
    assert [x["url"] for x in plan["lines"]] == ["", "https://x.com/loki.jpg", "https://www.google.com/search?q=thor"]
    assert bot.search_query("https://www.google.com/search?tbm=isch&q=Doctor+Doom+Latveria") == "Doctor Doom Latveria"
    assert bot.search_query("https://x.com/a.jpg") == ""
    assert bot.IMG_URL.findall("[арт](https://a.com/x.jpg) и https://b.com/y.png») ") == \
        [("", "https://a.com/x.jpg"), ("", "https://b.com/y.png")]
    assert plan["lines"][0]["label"] == "11 ПРОЕКТОВ"


def test_search_link_picks_image_from_ddg(tmp_path, monkeypatch) -> None:
    """Ссылка на поиск Google: бот ищет в DuckDuckGo, Gemini выбирает картинку по фразе, фотостоки пропускаются."""
    import asyncio
    import io
    import json

    from PIL import Image

    import bot

    def png(color):
        buf = io.BytesIO()
        Image.new("RGB", (60, 40), color).save(buf, "PNG")
        return buf.getvalue()

    results = {"results": [
        {"image": "https://stock.shutterstock.com/a.jpg", "thumbnail": "https://t/0", "width": 1200},
        {"image": "https://site/a.jpg", "thumbnail": "https://t/1", "width": 1200},
        {"image": "https://site/b.jpg", "thumbnail": "https://t/2", "width": 1200}]}
    pages = {"https://t/1": (png((10, 10, 10)), "image/png"), "https://t/2": (png((200, 0, 0)), "image/png"),
             "https://site/b.jpg": (png((200, 0, 0)), "image/png")}

    async def fake_get(session, url, limit, trusted):
        if url.startswith("https://duckduckgo.com/?"):
            return b"<script>vqd='4-123'</script>", "text/html"
        if url.startswith("https://duckduckgo.com/i.js"):
            return json.dumps(results).encode(), "application/json"
        return pages[url]

    async def pick(key, texts, images):
        assert len(images) == 2  # фотосток выброшен
        return [1]

    monkeypatch.setattr(bot.netsafe, "_get", fake_get)
    monkeypatch.setattr(bot.shorts_ai, "match_images", pick)
    asyncio.run(bot.fetch_image_url("https://www.google.com/search?tbm=isch&q=Doctor+Doom", tmp_path / "d.jpg",
                                    "Дум на троне"))
    assert Image.open(tmp_path / "d.jpg").getpixel((5, 5))[0] > 150  # взята картинка, которую выбрал ИИ


class _FakeDB:
    """База в памяти: только запросы утверждения сцен."""

    def __init__(self) -> None:
        self.approved: set[tuple[int, str]] = set()

    async def fetch(self, sql, chat_id, hashes):
        return [{"scene_hash": h} for h in hashes if (chat_id, h) in self.approved]

    async def execute(self, sql, *args):
        if "scene_approvals" in sql:
            self.approved.add((args[0], args[1]))


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)


def test_scene_approval_flow_end_to_end(tmp_path, monkeypatch) -> None:
    """Сквозной сценарий ТЗ §14 без Telegram и базы: публикация — только когда утверждены все сцены текущей
    версии; замена материала снимает утверждение только с этой сцены; публикация в канал не вызывается."""
    import asyncio
    import json
    import bot
    from shorts import scene_planner
    folder = tmp_path / "proj"
    folder.mkdir()
    for k in (1, 2):
        (folder / f"card{k}.jpg").write_bytes(f"img{k}".encode())
    cards = [{"image": "card1.jpg", "asset_source": "film_frame_pool"}, {"image": "card2.jpg", "asset_source": "user_media"}]
    plan = scene_planner.build_scene_plan(["обложка", "факт 1", "факт 2"], [0, 1, 4],
                                          [{"speech": 1}, {"speech": 3}, {"speech": 3}], cards)
    (folder / "scene_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    fake_db, fake_bot, offers, audit = _FakeDB(), _FakeBot(), [], []
    monkeypatch.setattr(bot, "db", lambda: fake_db)
    monkeypatch.setattr(bot, "proj_dir", lambda chat, first: folder)

    async def fake_offer(*a, **k):
        offers.append(a)

    async def fake_audit(*a, **k):
        audit.append(a)

    async def no_publish(*a, **k):
        raise AssertionError("публикация без отдельного решения автора")

    monkeypatch.setattr(bot, "offer_post", fake_offer)
    monkeypatch.setattr(bot, "audit", fake_audit)
    monkeypatch.setattr(bot, "publish_post", no_publish)

    asyncio.run(bot.approve_scene(fake_bot, 7, 100, 0, actor=1))
    assert not offers and "Осталось: 2" in fake_bot.sent[-1]  # одна сцена — публикации ещё нет
    asyncio.run(bot.approve_scene(fake_bot, 7, 100, 1, actor=1))
    assert len(offers) == 1 and audit  # все утверждены — предложение публикации (одно), действия в журнале
    asyncio.run(bot.approve_scene(fake_bot, 7, 100, 1, actor=1))
    assert len(offers) == 1  # повторное нажатие не дублирует

    (folder / "card2.jpg").write_bytes(b"replacement")  # автор заменил кадр сцены 2
    _, ok = asyncio.run(bot.scene_approvals(7, folder, plan))
    assert ok == [True, False]

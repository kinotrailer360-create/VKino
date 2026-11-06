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

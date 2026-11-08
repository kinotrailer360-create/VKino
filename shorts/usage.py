"""Учёт платных расходов по дням: запросы к ИИ Lumean, символы озвучки, заказы музыки.

Хранится в файле на томе данных (VKINO_DATA) — переживает обновления бота; держим 31 день.
Цены провайдеров здесь не зашиты: сколько стоит символ и запрос, видно в кабинете Lumean, а бот показывает,
НА ЧТО уходят деньги (какие задачи и сколько озвучки)."""

from __future__ import annotations

import json
import logging
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger("shorts")
FILE = Path(os.getenv("VKINO_DATA", "/tmp/kinomir_data")) / "usage.json"
KEEP_DAYS = 31
_cache: dict[str, dict[str, Any]] | None = None


def _load() -> dict[str, dict[str, Any]]:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _cache = {}
    return _cache


def record(key: str, amount: float = 1, task: str = "") -> None:
    """+amount к счётчику дня (llm_calls, llm_tokens, tts_chars, tts_orders, music_tracks); task — на что."""
    data = _load()
    today = date.today().isoformat()
    day = data.setdefault(today, {})
    day[key] = day.get(key, 0) + amount
    if task:
        tasks = day.setdefault("tasks", {})
        tasks[task] = tasks.get(task, 0) + 1
    for old in [d for d in data if d < (date.today() - timedelta(days=KEEP_DAYS)).isoformat()]:
        data.pop(old, None)
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(FILE)
    except OSError as exc:
        log.info("shorts: usage not saved: %s", exc)


def summary(days: int) -> dict[str, Any]:
    """Сумма за последние days дней (сегодня включительно)."""
    data = _load()
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    out: dict[str, Any] = {"tasks": {}}
    for d, day in data.items():
        if d < since:
            continue
        for k, v in day.items():
            if k == "tasks":
                for t, n in v.items():
                    out["tasks"][t] = out["tasks"].get(t, 0) + n
            else:
                out[k] = out.get(k, 0) + v
    return out

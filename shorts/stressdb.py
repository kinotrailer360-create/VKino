"""Словарь ударений русского языка на диске (без нейросети и почти без памяти).

Источник — открытый словарь проекта RUAccent (ruaccent/accentuator на Hugging Face, Apache-2.0): ~3,2 млн
словоформ с ударением. Сама нейросеть RUAccent требует ~2 ГБ памяти, а словарь в SQLite — это ~100 МБ на
диске и пара мегабайт памяти: бот скачивает его один раз (21 МБ) и строит базу построчно.

Омографы (за́мок/замо́к, му́ка/мука́) из словаря не берём — там ударение зависит от смысла, его ставит ИИ или
голос сам. Слова с «ё» тоже не трогаем: «ё» всегда ударная.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import re
import sqlite3
from pathlib import Path

import aiohttp

log = logging.getLogger("shorts")

BASE = "https://huggingface.co/ruaccent/accentuator/resolve/main/dictionary/"
VOWELS = "аеёиоуыэюя"
_lock = asyncio.Lock()
_db: sqlite3.Connection | None = None
status = ""  # что со словарём (для /voice)


def _build(gz_accents: Path, gz_homographs: Path, db_path: Path) -> None:
    tmp = db_path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.execute("CREATE TABLE s(w TEXT PRIMARY KEY, p INTEGER) WITHOUT ROWID")
    rx = re.compile(r'\s*"([^"]+)":\s*"([^"]+)"')
    batch: list[tuple[str, int]] = []
    with gzip.open(gz_accents, "rt", encoding="utf-8") as f:  # одна запись на строку — читаем построчно
        for line in f:
            m = rx.match(line)
            if not m or m.group(2).count("+") != 1:
                continue
            batch.append((m.group(1), m.group(2).index("+")))
            if len(batch) >= 50000:
                con.executemany("INSERT OR IGNORE INTO s VALUES(?, ?)", batch)
                batch.clear()
    con.executemany("INSERT OR IGNORE INTO s VALUES(?, ?)", batch)
    with gzip.open(gz_homographs, "rt", encoding="utf-8") as f:
        homographs = list(json.load(f))  # ~20 тыс. слов — маленький файл
    con.executemany("DELETE FROM s WHERE w = ?", [(w,) for w in homographs])
    con.commit()
    con.execute("VACUUM")
    con.close()
    tmp.replace(db_path)


async def _download(session: aiohttp.ClientSession, name: str, out: Path) -> None:
    async with session.get(BASE + name) as resp:
        resp.raise_for_status()
        size = 0
        with out.open("wb") as f:
            async for chunk in resp.content.iter_chunked(1 << 16):
                size += len(chunk)
                if size > 80 * 1024 * 1024:
                    raise RuntimeError("файл словаря слишком большой")
                f.write(chunk)


async def ready(data_dir: Path) -> bool:
    """Открыть словарь; при первом запуске — скачать и построить (пара минут). False — словаря нет."""
    global _db, status
    if _db is not None:
        return True
    async with _lock:
        if _db is not None:
            return True
        db_path = data_dir / "stress.db"
        try:
            if not db_path.exists():
                data_dir.mkdir(parents=True, exist_ok=True)
                status = "строю словарь ударений…"
                acc, hom = data_dir / "accents.json.gz", data_dir / "omographs.json.gz"
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600), trust_env=True) as session:
                    await _download(session, "accents.json.gz", acc)
                    await _download(session, "omographs.json.gz", hom)
                await asyncio.to_thread(_build, acc, hom, db_path)
                acc.unlink(missing_ok=True)
                hom.unlink(missing_ok=True)
            _db = sqlite3.connect(db_path, check_same_thread=False)
            status = "словарь ударений подключён"
            return True
        except Exception as exc:  # noqa: BLE001
            status = f"словарь ударений недоступен: {str(exc)[:150] or type(exc).__name__}"
            log.warning("shorts: stress dictionary failed: %s", exc)
            return False


def lookup(words: list[str]) -> dict[str, int]:
    """Позиция ударной гласной для слов (в нижнем регистре), которые есть в словаре."""
    if _db is None or not words:
        return {}
    out: dict[str, int] = {}
    uniq = list(dict.fromkeys(words))
    for k in range(0, len(uniq), 500):
        chunk = uniq[k:k + 500]
        rows = _db.execute(f"SELECT w, p FROM s WHERE w IN ({','.join('?' * len(chunk))})", chunk).fetchall()
        out.update({w: int(p) for w, p in rows})
    return out


def accented(text: str, found: dict[str, int]) -> str:
    """Текст со знаком «+» перед ударной гласной (формат RUAccent) — для pronounce.merge_stress."""
    def repl(m: re.Match[str]) -> str:
        word = m.group(0)
        pos = found.get(word.lower())
        if pos is None or pos >= len(word) or word[pos].lower() not in VOWELS:
            return word
        return word[:pos] + "+" + word[pos:]

    return re.sub(r"(?<![\w-])[А-Яа-яЁё]+(?![\w-])", repl, text)  # составные через дефис не трогаем

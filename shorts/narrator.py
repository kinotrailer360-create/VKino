"""«Диктор»: любой текст → проверка русского языка → версия для голоса → озвучка своим голосом.

1. Текст режется на фразы (по предложениям, до ~220 знаков).
2. Редактор-корректор (сильная модель): имена, склонение, грамматика, речевые ошибки — со списком правок.
3. Версия для голоса: числа словами, латиница по-русски, ударения (словарь /say, ИИ, словарь ударений).
4. Озвучка через Lumean с кэшем фраз (повтор после правки оплачивает только изменённое) и контролем:
   каждая фраза распознаётся, места, где услышано не то, что в тексте, — автору на прослушивание.
"""

from __future__ import annotations

import io
import re
import zipfile
from html import unescape
from pathlib import Path
from typing import Any

from . import ai, daily

MAX_CHARS = 20000
LINE = 220


def read_docx(data: bytes) -> str:
    """Текст из .docx без лишних библиотек: абзацы <w:p>, текст <w:t>."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
    paras = []
    for p in re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S):
        text = "".join(re.findall(r"<w:t(?: [^>]*)?>(.*?)</w:t>", p, flags=re.S))
        paras.append(unescape(text))
    return "\n\n".join(x for x in paras if x.strip())


def split(text: str) -> list[str]:
    """Фразы для озвучки: предложения подряд, пока фраза не длиннее ~220 знаков; абзац — всегда новая фраза."""
    out: list[str] = []
    for para in re.split(r"\n\s*\n|\n", text.strip()):
        para = re.sub(r"\s+", " ", para).strip()
        if not para:
            continue
        cur = ""
        for sent in re.split(r"(?<=[.!?…])\s+", para):
            if cur and len(cur) + len(sent) + 1 > LINE:
                out.append(cur)
                cur = sent
            else:
                cur = f"{cur} {sent}".strip()
        if cur:
            out.append(cur)
    return out


async def prepare(key: str, text: str) -> dict[str, Any]:
    """{"clean": исправленные фразы, "speech": фразы для голоса, "fixes": правки, "stressed": имена с ударением}."""
    lines = split(text[:MAX_CHARS])
    if not lines:
        raise ValueError("пустой текст")
    clean, fixes = list(lines), []
    for k in range(0, len(lines), 40):  # большой сценарий — частями, чтобы ИИ не сбился со счёта фраз
        res = await ai.edit_lines(key, lines[k:k + 40])
        if res:
            clean[k:k + 40], part = res
            fixes += part
    daily.stressed.clear()
    speech = await daily.spoken_lines(clean, careful=True)
    return {"clean": clean, "speech": speech, "fixes": fixes,
            "stressed": sorted(daily.stressed, key=str.lower)}


async def voice(speech: list[str], work: Path) -> dict[str, Any]:
    """Озвучка фраз своим голосом (Lumean) с кэшем и контролем. {"file", "hits", "total", "issues", "note"}."""
    work.mkdir(parents=True, exist_ok=True)
    daily.voice_issues.clear()
    daily.voice_cache_hits = 0
    path, _, voiced, _ = await daily.synthesize(speech, work, prepared=True)
    return {"file": path, "hits": daily.voice_cache_hits, "total": len(speech),
            "issues": list(daily.voice_issues), "note": daily.tts_note or "", "voiced": voiced}


def as_text(lines: list[str]) -> str:
    return "\n\n".join(lines) + "\n"

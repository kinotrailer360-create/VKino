"""Проект готового ролика: всё, чтобы потом поправить ролик по частям, а не собирать его целиком заново.

Кнопки под роликом:
- «🎙 Только звук» — фразы заново проходят словарь произношения (/say); те, что зазвучат иначе, озвучиваются и
  встают в старую дорожку голоса на своё место (подогнаны по длине), музыка и эффекты смешиваются заново,
  картинка ролика не пересчитывается (копируется как есть). Платим только за исправленные фразы;
- «🔁 Полностью» — тот же текст: заново озвучка (неизменные фразы — из кэша, бесплатно) и монтаж.

В папке проекта (рабочая папка сборки, без временных кадров): project.json, конфиг монтажа, голос voice.wav,
картинки, отрывки, обложка, сам ролик. Хранится столько же, сколько ролик для загрузки на YouTube.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Any

from . import daily, lumean

log = logging.getLogger("shorts")

FILE = "project.json"
FIT_MAX = 1.25  # новую фразу ускоряем не больше чем на 25 % — иначе слышно; длиннее — предупреждаем
ASK_OVER = 12  # больше стольких фраз заново — сначала спросить автора
SKIP = ("*.frames", "*.m4a", "*.log", "voice_*.mp3", "*_s[0-9]*.mp4", "*_v.mp4")


class NothingToFix(RuntimeError):
    """Все фразы звучат так же, как в ролике."""


class TooMany(RuntimeError):
    def __init__(self, n: int, total: int, chars: int) -> None:
        super().__init__(f"{n} из {total}")
        self.n, self.total, self.chars = n, total, chars


def _rel(path: Any, work: Path) -> str:
    """Путь файла внутри папки сборки — относительный (папка переедет), снаружи (музыка библиотеки) — как есть."""
    p = Path(str(path))
    try:
        return str(p.resolve().relative_to(work.resolve()))
    except ValueError:
        return str(p)


def save(work: Path, kind: str, renderer: str, cfg: str, video: str, voice: dict[str, Any] | None = None,
         rerun: dict[str, Any] | None = None) -> None:
    """Записать проект в папку сборки (после монтажа). voice — разметка озвучки (daily.last_voice)."""
    data = {"kind": kind, "renderer": renderer, "cfg": cfg, "video": video,
            "voice": voice if voice is not None else dict(daily.last_voice), "rerun": rerun or {}}
    (work / FILE).write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")


def load(folder: Path) -> dict[str, Any] | None:
    try:
        return json.loads((folder / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def keep(work: Path, dest: Path) -> bool:
    """Скопировать проект из папки сборки в хранилище (до того как папку сборки удалят)."""
    if not (work / FILE).exists():
        return False
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(work, dest, ignore=shutil.ignore_patterns(*SKIP))
    return True


# ---------------------------------------------------------------- голос: какие фразы изменились

async def changed(voice: dict[str, Any]) -> tuple[list[str], list[int]]:
    """Текст для голоса по текущему словарю и номера фраз, которые зазвучат иначе (их нет в кэше озвучки)."""
    lines = voice.get("lines") or []
    if voice.get("prepared"):
        speak = list(lines)
    else:
        speak = await daily.spoken_lines(lines, bool(voice.get("careful")))
    pause = voice.get("pause") or lumean.PAUSE
    todo = [i for i, t in enumerate(speak) if not daily._voice_key(daily._for_lumean(t), pause).exists()]
    return speak, todo


async def voice_parts(voice: dict[str, Any], work: Path, sure: bool) -> tuple[dict[int, Path], list[str]]:
    """Озвучить изменившиеся фразы: {номер фразы: файл}. Ничего не изменилось — NothingToFix; много — TooMany."""
    speak, todo = await changed(voice)
    if not todo:
        raise NothingToFix()
    if len(todo) > ASK_OVER and not sure:
        raise TooMany(len(todo), len(speak), sum(len(speak[i]) for i in todo))
    work.mkdir(parents=True, exist_ok=True)
    await daily.synthesize([speak[i] for i in todo], work, voice.get("pause"), bool(voice.get("careful")),
                           prepared=True)
    return {i: work / f"voice_{k}.mp3" for k, i in enumerate(todo)}, speak


async def patch_voice(old: Path, voice: dict[str, Any], parts: dict[int, Path], out: Path,
                      total: float | None = None) -> list[str]:
    """Новые фразы — на место старых в дорожке голоса: старая фраза заглушается, новая встаёт с той же секунды.
    Длиннее старой — ускоряется (не больше FIT_MAX), чтобы картинка и субтитры не разъехались. Возвращает
    предупреждения (фраза не влезла)."""
    starts = [float(x) for x in voice["starts"]]
    speech = [float(x) for x in voice["speech"]]
    end_all = total if total is not None else await daily._duration(old)
    mutes, chains, inputs, notes = [], [], ["-i", str(old)], []
    for k, (i, part) in enumerate(sorted(parts.items())):
        start, slot = starts[i], speech[i]
        nxt = starts[i + 1] if i + 1 < len(starts) else end_all
        room = max(slot, nxt - start - 0.05)  # до начала следующей фразы (пауза между ними тоже наша)
        new = await daily._duration(part)
        tempo = 1.0
        if new > room:
            tempo = min(new / room, FIT_MAX)
            if new / tempo > room + 0.05:
                notes.append(f"фраза {i + 1} длиннее старой на {new - room:.1f} с — немного зайдёт на следующую; "
                             "если слышно наложение — «🔁 Полностью»")
        mutes.append(f"between(t,{start:.3f},{min(nxt, start + room + 0.05):.3f})")
        inputs += ["-i", str(part)]
        ms = round(start * 1000)
        tempo_f = f"atempo={tempo:.4f}," if tempo > 1.001 else ""
        chains.append(f"[{k + 1}:a]aresample=48000,aformat=channel_layouts=mono,{tempo_f}adelay={ms}|{ms}[p{k}]")
    filt = (f"[0:a]aresample=48000,aformat=channel_layouts=mono,volume=0:enable='{'+'.join(mutes)}'[base];"
            + ";".join(chains) + ";[base]" + "".join(f"[p{k}]" for k in range(len(parts)))
            + f"amix=inputs={len(parts) + 1}:normalize=0:duration=first[out]")
    await daily._run("ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", filt, "-map", "[out]",
                     "-ar", "48000", str(out))
    return notes


async def mux(video: Path, audio: Path, out: Path) -> None:
    """Картинка старого ролика как есть + новый звук."""
    await daily._run("ffmpeg", "-v", "error", "-y", "-i", str(video), "-i", str(audio), "-map", "0:v",
                     "-map", "1:a", "-c", "copy", "-shortest", "-movflags", "+faststart", str(out))


# ---------------------------------------------------------------- шортс, лор, обзор: только звук

MUSIC_DB = {"shorts.make_short": -14, "shorts.make_lore": -16, "shorts.make_review": -18}


async def revoice(folder: Path, sure: bool = False) -> tuple[Path, list[tuple[int, float, str]], list[str]]:
    """Только звук: новый ролик (в папке проекта) — та же картинка, исправленные фразы. Проект обновляется:
    дорожка голоса и ролик — новые, следующая правка ляжет поверх этой."""
    from .make_short import mix_audio

    proj = load(folder)
    if not proj or not proj.get("voice", {}).get("starts"):
        raise RuntimeError("у ролика нет разметки озвучки")
    voice = proj["voice"]
    tmp = folder / "revoice_tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        parts, speak = await voice_parts(voice, tmp, sure)
        cfg = json.loads((folder / proj["cfg"]).read_text(encoding="utf-8"))
        video = folder / proj["video"]
        total = await daily._duration(video)
        old_voice = folder / voice["file"]
        new_voice = tmp / "voice_new.wav"
        notes = await patch_voice(old_voice, voice, parts, new_voice)

        def path(x: str) -> Path:
            p = Path(x)
            p = p if p.is_absolute() else (folder / p).resolve()
            # своя музыка автора лежала в папке сборки (её уже нет) — копия в папке проекта под тем же именем
            return p if p.exists() or not (folder / p.name).exists() else folder / p.name

        sfx = [(float(t), path(f), float(db)) for t, f, db in cfg.get("sfx") or []]
        music = path(cfg["music"])
        audio = tmp / "mix.m4a"
        await asyncio.to_thread(mix_audio, new_voice, music, total, audio,
                                cfg.get("music_db", MUSIC_DB.get(proj["renderer"], -14)), sfx)
        out = tmp / "new.mp4"
        await mux(video, audio, out)
        shutil.move(str(new_voice), old_voice)  # следующая правка — поверх этой
        shutil.move(str(out), video)
        fixed = [(i, float(voice["starts"][i]), voice["lines"][i]) for i in sorted(parts)]
        return video, fixed, notes
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

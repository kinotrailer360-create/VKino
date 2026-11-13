"""Библиотека сцен (ТЗ, приложение B): видео, на которые у автора есть основание (свои, лицензия, официальные
трейлеры), один раз режутся на сцены, у каждой сцены — ключевой кадр и описание (кто в кадре, действие, место,
предметы, цвета, годится ли для вертикали). Ролик ищет сцену под фразу в два этапа: отбор по словам и
произведению, затем ИИ сравнивает фразу с ключевыми кадрами; ниже порога — «нет подходящего», а не первый
попавшийся кадр. Права — жёсткий фильтр: запрещённое к публикации в поиск не попадает, «не подтверждено» —
попадает с пометкой (так решил владелец, вариант «а»).

Ничего само не скачивает из интернета, кроме ссылок, которые прислал автор; DRM и платные сервисы не трогает."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any

from PIL import Image

from . import ai, daily

log = logging.getLogger("shorts")

LIB_DIR = daily.DATA_DIR / "library"
INBOX = LIB_DIR / "inbox"  # большие файлы — сюда: docker compose cp файл.mp4 bot:/data/library/inbox/
SCENE_CUT = 0.3  # порог смены плана ffmpeg (0–1)
SCENE_MIN, SCENE_MAX = 0.8, 5.0  # короче — склеиваем с соседним, длиннее — делим
MAX_SCENES = int(os.getenv("LIB_MAX_SCENES", "300"))  # больше — равномерная выборка (цена описания ИИ)
BATCH = 12  # ключевых кадров в одном запросе к ИИ
MATCH_MIN = 75  # ниже — сцену не берём
CANDIDATES = 12  # после отбора по словам — столько кадров смотрит ИИ

RIGHTS = {  # подпись автора → права
    "свои": {"source": "материал автора", "owner": "автор", "license": "свой материал", "publish": "yes"},
    "лицензия": {"source": "лицензированный материал", "owner": "", "license": "", "publish": "yes"},
    "трейлер": {"source": "официальный трейлер", "owner": "студия", "license": "права не подтверждены",
                "publish": "unconfirmed"},
    "нельзя": {"source": "только для анализа", "owner": "", "license": "публикация запрещена", "publish": "no"},
}

pool: Any = None  # asyncpg pool (attach)


def attach(db_pool: Any) -> None:
    global pool
    pool = db_pool


def parse_caption(text: str) -> tuple[str, dict[str, str], int | None, int | None]:
    """«Мстители: Финал | права: трейлер | сезон 1 серия 3» → (произведение, права, сезон, серия)."""
    parts = [p.strip() for p in re.split(r"[|\n]", text or "") if p.strip()]
    work, rights, season, episode = "", dict(RIGHTS["трейлер"]), None, None
    for p in parts:
        low = p.lower()
        m = re.match(r"права?\s*[:\-—]?\s*(.+)", low)
        if m:
            key = next((k for k in RIGHTS if k in m.group(1)), "")
            rights = dict(RIGHTS[key]) if key else {**RIGHTS["лицензия"], "license": p.split(":", 1)[-1].strip()}
            if key == "лицензия":
                rights["license"] = p.split(":", 1)[-1].strip()
            continue
        s, e = re.search(r"сезон\s*(\d+)", low), re.search(r"(?:серия|эпизод)\s*(\d+)", low)
        if s or e:
            season, episode = (int(s.group(1)) if s else None), (int(e.group(1)) if e else None)
            continue
        work = work or p.strip(" «»\"")
    return work, rights, season, episode


async def _run(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    return proc.returncode or 0, (out + err).decode(errors="ignore")


def segments(cuts: list[float], total: float) -> list[tuple[float, float]]:
    """Границы планов → сцены от SCENE_MIN до SCENE_MAX секунд."""
    edges = [0.0] + sorted(c for c in cuts if 0 < c < total) + [total]
    raw: list[tuple[float, float]] = []
    start = 0.0
    for a, b in zip(edges, edges[1:]):
        if b - a < SCENE_MIN:  # мелкий план — к предыдущей сцене (первый — к следующей)
            if raw:
                raw[-1] = (raw[-1][0], b)
            continue
        raw.append((start if not raw else a, b))
    if not raw:
        raw = [(0.0, total)]
    raw[-1] = (raw[-1][0], total)
    out: list[tuple[float, float]] = []
    for a, b in raw:
        n = max(1, int((b - a) // SCENE_MAX) + (1 if (b - a) % SCENE_MAX > 0.01 else 0))
        step = (b - a) / n
        out += [(round(a + k * step, 2), round(a + (k + 1) * step, 2)) for k in range(n)]
    if len(out) > MAX_SCENES:  # длинный фильм — равномерная выборка, чтобы описание не стоило как полфильма
        k = len(out) / MAX_SCENES
        out = [out[int(i * k)] for i in range(MAX_SCENES)]
    return out


async def detect(src: Path) -> list[tuple[float, float]]:
    total = await daily._duration(src)
    _, log_text = await _run("ffmpeg", "-hide_banner", "-nostats", "-i", str(src), "-vf",
                             f"select='gt(scene,{SCENE_CUT})',showinfo", "-an", "-f", "null", "-")
    cuts = [float(x) for x in re.findall(r"pts_time:([\d.]+)", log_text)]
    return segments(cuts, total)


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def scene_text(info: dict[str, Any]) -> str:
    """Текст сцены для отбора по словам: кто (важнее), действие, место, предметы, цвета, надписи."""
    who = " ".join(map(str, info.get("who") or []))
    rest = " ".join(str(info.get(k) or "") for k in ("action", "place", "objects", "colors", "text"))
    return f"{who} {who} {who} {rest}".lower()


async def add(src: Path, work: str, rights: dict[str, str], gemini_key: str, actor: int,
              season: int | None = None, episode: int | None = None,
              progress: Any = None) -> dict[str, Any]:
    """Положить видео в библиотеку и проиндексировать сцены. Уже есть такой файл — вернуть его запись."""
    sha = await asyncio.to_thread(_sha, src)
    row = await pool.fetchrow("SELECT id, work, scenes FROM media_library WHERE sha = $1", sha)
    if row:
        return {"id": row["id"], "work": row["work"], "scenes": row["scenes"], "existing": True}
    lib_id = await pool.fetchval(
        "INSERT INTO media_library (work, season, episode, file, sha, source, owner, license, publish, added_by) "
        "VALUES ($1, $2, $3, '', $4, $5, $6, $7, $8, $9) RETURNING id",
        work, season, episode, sha, rights.get("source", ""), rights.get("owner", ""), rights.get("license", ""),
        rights.get("publish", "unconfirmed"), actor)
    folder = LIB_DIR / str(lib_id)
    (folder / "kf").mkdir(parents=True, exist_ok=True)
    dest = folder / ("video" + (src.suffix or ".mp4"))
    await asyncio.to_thread(shutil.move if src.parent == INBOX else shutil.copy2, str(src), str(dest))
    await pool.execute("UPDATE media_library SET file = $2 WHERE id = $1", lib_id, str(dest))
    if progress:
        await progress("Режу видео на сцены")
    scenes = await detect(dest)
    frames: list[Path] = []
    for n, (a, b) in enumerate(scenes):
        kf = folder / "kf" / f"{n:04d}.jpg"
        await _run("ffmpeg", "-v", "error", "-y", "-ss", f"{(a + b) / 2:.2f}", "-i", str(dest), "-frames:v", "1",
                   "-vf", "scale=480:-2", str(kf))
        frames.append(kf)
    infos: list[dict[str, Any]] = []
    for k in range(0, len(frames), BATCH):
        if progress:
            await progress(f"ИИ описывает сцены {k + 1}–{min(k + BATCH, len(frames))} из {len(frames)}")
        imgs = [Image.open(f).convert("RGB") for f in frames[k:k + BATCH] if f.exists()]
        infos += await ai.describe_scenes(gemini_key, work, imgs) if imgs else []
    async with pool.acquire() as conn, conn.transaction():
        for n, ((a, b), kf) in enumerate(zip(scenes, frames)):
            info = infos[n] if n < len(infos) else {}
            await conn.execute(
                "INSERT INTO media_scenes (lib_id, n, t_start, t_end, keyframe, info, words) "
                "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7) ON CONFLICT DO NOTHING",
                lib_id, n, a, b, str(kf), json.dumps(info, ensure_ascii=False), scene_text(info))
        await conn.execute("UPDATE media_library SET scenes = $2, indexed_at = NOW(), model = $3 WHERE id = $1",
                           lib_id, len(scenes), ai.last_model or "")
    return {"id": lib_id, "work": work, "scenes": len(scenes), "existing": False}


def _stems(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[a-zа-яё0-9]{4,}", text.lower().replace("ё", "е"))}


def preselect(phrase: str, scenes: list[dict[str, Any]], limit: int = CANDIDATES) -> list[dict[str, Any]]:
    """Этап 1: сцены, где есть слова фразы (герой — втрое важнее: он в тексте сцены трижды)."""
    want = _stems(phrase)
    scored = []
    for s in scenes:
        words = s["words"].replace("ё", "е")
        hit = sum(words.count(st) for st in want)
        if hit:
            scored.append((hit, s))
    scored.sort(key=lambda x: -x[0])
    return [s for _, s in scored[:limit]]


async def find(phrases: list[str], works: list[str], gemini_key: str) -> list[dict[str, Any]]:
    """Сцена под каждую фразу: {"lib_id", "work", "start", "end", "file", "score", "reason", "rights"} или
    {"none": причина}. Только произведения из works (если в библиотеке они есть), права «нельзя» — исключены."""
    if pool is None:
        return [{"none": "библиотека не подключена"} for _ in phrases]
    rows = await pool.fetch(
        "SELECT s.lib_id, s.n, s.t_start, s.t_end, s.keyframe, s.words, l.work, l.file, l.source, l.license, "
        "l.publish FROM media_scenes s JOIN media_library l ON l.id = s.lib_id WHERE l.publish <> 'no'")
    scenes = [dict(r) for r in rows]
    if works:
        wanted = [_stems(w) for w in works if w]
        own = [s for s in scenes if any(w and w <= _stems(s["work"]) for w in wanted)]
        scenes = own or scenes
    out: list[dict[str, Any]] = []
    used: set[tuple[int, int]] = set()
    for phrase in phrases:
        cands = [s for s in preselect(phrase, scenes) if (s["lib_id"], s["n"]) not in used]
        if not cands:
            out.append({"none": "в библиотеке нет сцен с этими героями/словами"})
            continue
        imgs = []
        for s in cands:
            try:
                imgs.append(Image.open(s["keyframe"]).convert("RGB"))
            except OSError:
                imgs.append(Image.new("RGB", (8, 8)))
        ranked = await ai.rank_image_candidates(gemini_key, [phrase], imgs)
        pick = (ranked or [{}])[0]
        idx, score = int(pick.get("index", -1)), int(pick.get("score", 0) or 0)
        if not 0 <= idx < len(cands) or score < MATCH_MIN:
            out.append({"none": f"точного совпадения нет (лучшее {score}/100)" if ranked else "ИИ не ответил"})
            continue
        s = cands[idx]
        used.add((s["lib_id"], s["n"]))
        out.append({"lib_id": s["lib_id"], "n": s["n"], "work": s["work"], "start": s["t_start"], "end": s["t_end"],
                    "file": s["file"], "score": score, "reason": str(pick.get("reason") or "")[:160],
                    "rights": "author" if s["source"] == RIGHTS["свои"]["source"] else
                    "unconfirmed" if s["publish"] != "yes" else "licensed",
                    "ref": f"библиотека: {s['work']}, {_tc(s['t_start'])}–{_tc(s['t_end'])} ({s['source']})"})
    return out


def _tc(sec: float) -> str:
    return f"{int(sec) // 60}:{int(sec) % 60:02d}"


async def listing() -> list[dict[str, Any]]:
    rows = await pool.fetch("SELECT id, work, season, episode, scenes, source, license, publish, added_at "
                            "FROM media_library ORDER BY id DESC LIMIT 40")
    return [dict(r) for r in rows]


async def remove(lib_id: int) -> bool:
    row = await pool.fetchrow("DELETE FROM media_library WHERE id = $1 RETURNING id", lib_id)
    shutil.rmtree(LIB_DIR / str(lib_id), ignore_errors=True)
    return bool(row)


def evaluate(cases: list[dict[str, Any]], results: list[dict[str, Any]]) -> dict[str, float]:
    """Точность подбора на размеченном наборе (ТЗ, приложение B): cases — {"want": (lib_id, n) или None};
    results — ответы find. top1 — верная сцена; refusal — верный отказ, когда сцены нет; wrong — не та сцена."""
    top1 = wrong = refusal = should_refuse = 0
    for case, res in zip(cases, results):
        want = case.get("want")
        got = (res.get("lib_id"), res.get("n")) if "lib_id" in res else None
        if want is None:
            should_refuse += 1
            refusal += got is None
        elif got == tuple(want):
            top1 += 1
        elif got is not None:
            wrong += 1
    positives = max(1, len(cases) - should_refuse)
    return {"top1": top1 / positives, "wrong": wrong / positives,
            "refusal": refusal / should_refuse if should_refuse else 1.0}

"""Import rehearsed Shorts scripts with numbered, author-reviewed visuals."""
from __future__ import annotations

import asyncio
import hashlib
import io
import re
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import ai, imgsearch, netsafe

SECTION = re.compile(r"(?m)^##\s+\d+\.\s+(.+)$")
SCENE = re.compile(r"(?m)^###\s+Сцена\s+(\d+)\s*[—–-]\s*(.+)$", re.I)
FIELD = re.compile(r"(?m)^(?:- )?\*\*([^*]+?):\*\*[ \t]*(.*)$")


def marked(text: str) -> bool:
    headings = [m[1].casefold() for m in SECTION.finditer(text)]
    return any(x.startswith("озвучка") for x in headings) and any("покадровая карта" in x for x in headings)


def parse(text: str) -> dict:
    sections = list(SECTION.finditer(text))
    bodies = {m[1].casefold(): text[m.end():sections[i + 1].start() if i + 1 < len(sections) else len(text)]
              for i, m in enumerate(sections)}
    voice = next((s for name, s in bodies.items() if name.startswith("озвучка")), "")
    storyboard = next((s for name, s in bodies.items() if "покадровая карта" in name), "")
    pack = next((s for name, s in bodies.items() if name.startswith("заголовок")), "")
    lines = [line.strip() for line in re.findall(r"(?m)^>[ \t]*(\S.*)$", voice)]
    headings = list(SCENE.finditer(storyboard))
    if not 2 <= len(lines) == len(headings) <= 12 or sum(map(len, lines)) > 2800:
        raise ValueError("Нужна одна цитата озвучки и одна пронумерованная сцена на каждый блок (от 2 до 12).")
    meta = dict(FIELD.findall(pack))
    scenes = []
    for i, heading in enumerate(headings):
        if int(heading[1]) != i + 1:
            raise ValueError("Пронумеруй сцены подряд, начиная с первой.")
        body = storyboard[heading.end():headings[i + 1].start() if i + 1 < len(headings) else len(storyboard)]
        fields = dict(FIELD.findall(body))
        if fields.get("Озвучка", "").strip().removeprefix("«").removesuffix("»") != lines[i]:
            raise ValueError(f"Текст диктора и реплика в сцене {i + 1} различаются.")
        targets = [value for name, value in fields.items() if name.startswith("Целевой кадр")]
        if not targets:
            raise ValueError(f"У сцены {i + 1} нет целевого кадра.")
        label = (fields.get("Надпись сверху") or fields.get("Надпись") or "").strip("«» ")
        queries = [q for language in ("Поиск EN", "Поиск RU") for q in
                   re.findall(r"`([^`]+)`", fields.get(language, ""))]
        urls = re.findall(r'https://[^\s<>»;]+', fields.get("Где искать", ""))
        scenes.append({"heading": heading[2].strip(), "targets": targets, "label": label,
                       "queries": queries, "sources": urls, "source_hint": fields.get("Где искать", ""),
                       "fallback": fields.get("Резерв", ""), "control": fields.get("Контроль", "")})
    if not meta.get("Заголовок"):
        raise ValueError("Добавь заголовок для YouTube в раздел оформления.")
    return {"title": meta["Заголовок"], "description": meta.get("Описание", ""),
            "hashtags": meta.get("Хэштеги", ""), "lines": lines, "scenes": scenes}


def fingerprint(path: Path) -> str:
    with path.open("rb") as src:
        return hashlib.file_digest(src, "sha256").hexdigest()


def require_approved(folder: Path, plan: dict, assets: dict) -> list[Path]:
    files = []
    hashes = set()
    for i in range(1, len(plan["scenes"]) + 1):
        asset = assets.get(str(i), {})
        path = folder / str(asset.get("file", ""))
        if path.parent != folder or not path.is_file() or not asset.get("approved"):
            raise ValueError(f"Сначала утверди сцену {i}.")
        digest = fingerprint(path)
        if digest != asset["approved"]:
            raise ValueError(f"Материал сцены {i} изменился после согласования.")
        if digest in hashes:
            raise ValueError(f"Для сцены {i} использован тот же материал. Выбери другой.")
        hashes.add(digest)
        files.append(path)
    return files


def official(url: str) -> bool:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in ("marvel.com", "disney.com"))


async def find_still(scene: dict, narration: str, key: str) -> tuple[Image.Image, str] | None:
    """Only accept a verified still whose landing page belongs to the studio."""
    if len(scene["targets"]) != 1 or not any(official(url) for url in scene["sources"]):
        return None
    target = scene["targets"][0].replace("**", "")
    if any(word in target.casefold() for word in ("сплит-экран", "затем", "авторская финальная")):
        return None
    async with netsafe.safe_session(timeout=aiohttp.ClientTimeout(total=12), headers=imgsearch.BROWSER) as session:
        for query in scene["queries"][:2]:
            hits = await imgsearch.search(query + " (site:marvel.com OR site:disney.com)", 8)
            official_hits = [hit for hit in hits if official(hit.get("source") or "")]
            for hit in official_hits[:2]:
                try:
                    raw, _ = await netsafe._get(session, hit["image"], 10 * 1024 * 1024, "")
                    with Image.open(io.BytesIO(raw)) as opened:
                        if opened.width < 480 or opened.height < 360 or opened.width * opened.height > 18_000_000:
                            continue
                        image = opened.convert("RGB")
                    verdict, _ = await ai.media_fits(key, image, f"{narration}\nЦелевой кадр: {target}\nКонтроль: {scene['control']}")
                    if verdict is True:
                        return image, hit["source"]
                    image.close()
                except (aiohttp.ClientError, OSError, ValueError, RuntimeError):
                    continue
    return None


def fallback_card(path: Path, scene: dict, index: int) -> None:
    from .daily import ASSETS

    palette = ("#112219", "#15252f", "#272035", "#263020")
    with Image.new("RGB", (1080, 1920), palette[(index - 1) % len(palette)]) as image:
        draw = ImageDraw.Draw(image)
        head = ASSETS / "Oswald.ttf"
        sans = ImageFont.truetype(str(ASSETS / "Montserrat.ttf"), 34)
        draw.rectangle((96, 532, 984, 544), fill="#e6ba63")
        draw.text((540, 440), "КИНОМИР 360", font=sans, anchor="mm", fill="#e6ba63")
        fallback = scene["fallback"]
        words = re.findall(r"«([^»]+)»", fallback)
        title = "  /  ".join(words[:3]) or scene["label"] or scene["heading"].split(" (")[0]
        for size in range(80, 29, -5):
            font = ImageFont.truetype(str(head), size)
            pieces = []
            row = ""
            for word in title.upper().split():
                trial = f"{row} {word}".strip()
                if row and draw.textlength(trial, font=font) > 850:
                    pieces.append(row)
                    row = word
                else:
                    row = trial
            pieces.append(row)
            if len(pieces) <= 4:
                break
        draw.multiline_text((540, 850), "\n".join(pieces), font=font, anchor="mm",
                            align="center", spacing=16, fill="#f8f1e3")
        draw.text((540, 1300), "АВТОРСКАЯ ГРАФИКА", font=sans, anchor="mm", fill="#e6ba63")
        image.save(path, quality=93)


async def prepare_still(path: Path, scene: dict, narration: str, index: int, key: str) -> str:
    try:
        candidate = await asyncio.wait_for(find_still(scene, narration, key), 35)
    except (TimeoutError, aiohttp.ClientError, OSError, ValueError, RuntimeError):
        candidate = None
    if candidate:
        image, source = candidate
        with image:
            image.thumbnail((1600, 1600))
            image.save(path, quality=93)
        return source
    await asyncio.to_thread(fallback_card, path, scene, index)
    return ""


def normalize_photo(path: Path) -> None:
    with Image.open(path) as opened:
        if opened.width * opened.height > 18_000_000:
            raise ValueError("Кадр слишком большой.")
        photo = ImageOps.exif_transpose(opened).convert("RGB")
    with photo:
        photo.thumbnail((1920, 1920))
        photo.save(path, format="JPEG", quality=94)

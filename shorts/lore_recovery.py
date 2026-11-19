"""Bounded, verified image recovery for an individual unfilled narration phrase.

Do not take arbitrary nearby search hits when the AI cannot verify what the
image depicts. A 404 on one host is a reason to try a different verified source,
not a reason to silently reuse an unrelated frame.
"""
from __future__ import annotations

import hashlib
import io
import logging
import re
from typing import Any

import aiohttp
from PIL import Image, UnidentifiedImageError

from . import ai, imgsearch, netsafe

log = logging.getLogger("shorts.lore_recovery")
MAX_PHRASE_RESULTS = 8
MAX_PHRASE_QUERIES = 2
MAX_TOTAL_ATTEMPTS = 12
MAX_IMAGE_BYTES = 12 * 1024 * 1024


def queries(phrase: str, title: str = "") -> list[str]:
    """Preserve named subjects, omit long speculative narration."""
    words = re.findall(r"[\w-]+", phrase or "", flags=re.UNICODE)
    focus = " ".join(words[:13]).strip()
    name = (title or "").strip()[:80]
    out = []
    if name:
        out.append(f"{name} {focus[:90]} official still")
    if focus:
        out.append(f"{focus[:110]} кадр фильм")
    return list(dict.fromkeys(q for q in out if q.strip()))[:MAX_PHRASE_QUERIES]


def fingerprint(image: Image.Image) -> str:
    """Prevent exact visual reuse even when a search engine changes URL."""
    thumb = image.convert("RGB").resize((48, 48))
    return hashlib.sha256(thumb.tobytes()).hexdigest()


async def recover(phrase: str, title: str, key: str,
                  used_hashes: set[str] | None = None) -> tuple[Image.Image, str] | None:
    """Download, decode and *then* ask Gemini to approve each visual candidate.

    Returns an owned RGB image and the source URL, or None. Inconclusive model
    results do not count as approval. This does not verify copyright ownership.
    """
    if not ai.available(key):
        return None
    seen = set()
    used = used_hashes if used_hashes is not None else set()
    attempted = 0
    async with netsafe.safe_session(
        timeout=aiohttp.ClientTimeout(total=38), headers=imgsearch.BROWSER,
    ) as session:
        for query in queries(phrase, title):
            try:
                results = await imgsearch.search(query, MAX_PHRASE_RESULTS)
            except Exception as exc:  # noqa: BLE001
                log.info("Image recovery search failed: %s", type(exc).__name__)
                continue
            for result in results:
                url = str(result.get("image") or "")
                if not url.startswith("https://") or url in seen:
                    continue
                seen.add(url)
                attempted += 1
                if attempted > MAX_TOTAL_ATTEMPTS:
                    return None
                try:
                    raw, mime = await netsafe._get(session, url, MAX_IMAGE_BYTES, "")
                    if not mime.startswith("image/"):
                        continue
                    with Image.open(io.BytesIO(raw)) as original:
                        original.verify()
                    with Image.open(io.BytesIO(raw)) as original:
                        if original.width < 400 or original.height < 240:
                            continue
                        if original.width * original.height > 30_000_000:
                            continue
                        image = original.convert("RGB")
                    image.thumbnail((1600, 1600))
                    identity = fingerprint(image)
                    if identity in used:
                        image.close()
                        continue
                    fit, reason = await ai.media_fits(key, image, phrase)
                    if fit is True:
                        used.add(identity)
                        return image, url
                    log.info("Recovery candidate rejected: %s", str(reason)[:100])
                    image.close()
                except (OSError, ValueError, UnidentifiedImageError,
                        aiohttp.ClientError, TimeoutError) as exc:
                    log.info("Image recovery URL unavailable: %s", type(exc).__name__)
                    continue
    return None

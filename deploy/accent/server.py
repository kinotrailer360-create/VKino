"""Ударения в русском тексте (RUAccent) — маленький HTTP-сервис для бота.

POST /accent {"texts": ["…"]} → {"texts": ["…"]}: ударная гласная помечена «+» перед ней («кобур+е»).
Модель (~2 ГБ в памяти) скачивается при первом запуске в том /models и грузится один раз.
"""

import asyncio
import logging
import os

from aiohttp import web
from ruaccent import RUAccent

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("accent")
accent = RUAccent()
lock = asyncio.Lock()


async def handle(request: web.Request) -> web.Response:
    data = await request.json()
    texts = [str(t)[:2000] for t in (data.get("texts") or [])][:300]
    async with lock:  # модель не потокобезопасна — по одному запросу
        out = await asyncio.to_thread(lambda: [accent.process_all(t) for t in texts])
    return web.json_response({"texts": out})


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def main() -> None:
    model = os.getenv("ACCENT_MODEL", "turbo3.1")
    log.info("loading RUAccent %s…", model)
    accent.load(omograph_model_size=model, use_dictionary=True, tiny_mode=False)
    log.info("ready")
    app = web.Application(client_max_size=4 * 1024 * 1024)
    app.add_routes([web.post("/accent", handle), web.get("/health", health)])
    web.run_app(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()

"""Preflight Lumean paid requests against the gateway's CURRENT LUMC catalog.

The internal USD budget is a *separate* approximate ledger. Real Lumean AI
pricing can change and may be different for each model. A missing price is not
permission to purchase; an admin can use the provider's per-key AI monthly cap.
"""
from __future__ import annotations

import asyncio
import math
import os
from urllib.parse import quote

import aiohttp

CATALOG_URL = "https://api.lumean.app/api/public/ai/models"


class PriceCheckUnavailable(RuntimeError):
    """A paid request is blocked because its actual price cannot be verified."""


def max_request_lumc() -> float:
    try:
        value = float(os.getenv("LUMEAN_MAX_REQUEST_LUMC", "0.50"))
        return value if math.isfinite(value) and 0 < value <= 20 else 0.50
    except ValueError:
        return 0.50


def estimate_lumc(payload: dict, input_tokens: int, output_tokens: int) -> float:
    """Conservative upper estimate from Lumean's current per-million-token rates."""
    detail = payload.get("data") if isinstance(payload, dict) else None
    rows = detail.get("models") if isinstance(detail, dict) else None
    asset = detail.get("asset") if isinstance(detail, dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(asset, dict):
        raise PriceCheckUnavailable("Lumean: тариф модели недоступен")
    model = rows[0]
    if not isinstance(model, dict) or model.get("kind") != "chat" or model.get("billing_available") is not True:
        raise PriceCheckUnavailable("Lumean: модель недоступна для платного API")
    precision = asset.get("precision")
    if type(precision) is not int or not 0 <= precision <= 8 or asset.get("code") not in ("LUMC", "LMC"):
        raise PriceCheckUnavailable("Lumean: неизвестная единица оплаты")
    rate = model.get("price")
    if not isinstance(rate, dict):
        raise PriceCheckUnavailable("Lumean: цена модели не опубликована")
    numbers = []
    for cls in ("input", "output"):
        row = rate.get(cls)
        n = row.get("per_mtok_minor") if isinstance(row, dict) else None
        if type(n) not in (int, float) or not math.isfinite(n) or n < 0:
            raise PriceCheckUnavailable("Lumean: нет тарифа входных или выходных токенов")
        numbers.append(float(n))
    factor = 10 ** precision
    # Reserve at least a 30% margin for input tokenization overhead.
    return (max(input_tokens, 1) * 1.3 * numbers[0]
            + max(output_tokens, 1) * numbers[1]) / (1_000_000 * factor)


async def preflight(session: aiohttp.ClientSession, key: str, model: str,
                    estimated_input_tokens: int, max_output_tokens: int) -> float:
    """Fail closed if catalog is down or estimated request exceeds author's cap.

    Read-only call: no paid generation. This preflight does not replace the
    Lumean provider-side per-key monthly AI spend limit.
    """
    url = CATALOG_URL + "/" + quote(model, safe="")
    try:
        async def get_price():
            async with session.get(url, headers={"X-API-KEY": key, "Accept": "application/json"}) as response:
                if response.status != 200:
                    raise PriceCheckUnavailable(f"Lumean: каталог цен HTTP {response.status}")
                return await response.json(content_type=None)
        payload = await asyncio.wait_for(get_price(), timeout=12)
    except PriceCheckUnavailable:
        raise
    except (aiohttp.ClientError, TimeoutError, ValueError, OSError) as exc:
        raise PriceCheckUnavailable("Lumean: каталог цен не ответил") from exc
    try:
        estimate = estimate_lumc(payload, estimated_input_tokens, max_output_tokens)
    except (TypeError, OverflowError, ZeroDivisionError) as exc:
        raise PriceCheckUnavailable("Lumean: не удалось рассчитать цену") from exc
    ceiling = max_request_lumc()
    if estimate > ceiling:
        raise PriceCheckUnavailable(
            f"Lumean: оценка {estimate:.3f} LUMC за запрос превышает лимит {ceiling:.2f} LUMC; "
            "используй бесплатную модель или уменьши задание")
    return estimate

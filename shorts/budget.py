"""Единый месячный бюджет платных вызовов: проверка ДО запроса с резервированием, пересчёт ПОСЛЕ ответа.

Два независимых лимита («кошелька»):
- "ai"  — всё, кроме озвучки: текст и Vision (Gemini, Lumean LLM), картинки, музыка, платный веб-поиск;
- "tts" — озвучка (Lumean / ElevenLabs): свой лимит, чтобы шортсы не вставали из-за постов и наоборот.

Как работает: guard(pool, op, estimate) → под блокировкой (asyncio + pg_advisory_xact_lock) считаем потрачено +
зарезервировано за месяц; не влезает оценка — BudgetExceeded, запрос не делается. Влезает — строка ai_usage
со статусом reserved. После ответа — settle(фактическая стоимость по токенам/символам) или отмена при ошибке.
Так параллельные запросы не перескочат лимит. Бесплатные вызовы (оценка 0) не блокируются никогда, но учитываются.

Цены — из .env (ничего непроверенного не зашито). Цена не задана → операция считается по 0 и в отчёте помечена
«цена не задана» (у Lumean цену сверяем по журналу расходов после 10–20 вызовов, см. status_lines).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

log = logging.getLogger("shorts")


def _env_float(name: str, default: float | None = None) -> float | None:
    raw = (os.getenv(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


# лимиты в месяц, $ — решение владельца канала (ноябрь 2026): посты/ИИ — 10$, озвучка — отдельный кошелёк
LIMITS = {"ai": _env_float("AI_MONTHLY_BUDGET_USD", 10.0) or 10.0,
          # озвучка: подписка Lumean Start — 99 ₽ за 100 000 токенов в месяц ≈ 1,2 $; лимит = объём подписки
          "tts": _env_float("TTS_MONTHLY_BUDGET_USD", 1.2) or 1.2}
POOL_NAMES = {"ai": "ИИ, картинки, музыка", "tts": "озвучка"}
WARN_SHARE = 0.8  # с 80 % — предупреждение автору (раз в месяц на кошелёк)

# цены. Gemini и картинка — со страницы цен Google (ai.google.dev/gemini-api/docs/pricing, ноябрь 2026);
# Gemini по умолчанию 0: у бота бесплатный ключ (квоты), платный включается переменными в .env
PRICES: dict[str, float | None] = {
    "gemini_in_per_m": _env_float("PRICE_GEMINI_IN_PER_M", 0.0),     # платный 2.5 Flash: 0.30
    "gemini_out_per_m": _env_float("PRICE_GEMINI_OUT_PER_M", 0.0),   # платный 2.5 Flash: 2.50
    "image": _env_float("AI_COST_IMAGE_USD", 0.045),                 # 0.039–0.045 за картинку до 1K
    # ИИ Lumean (gpt-5.6): подобрано по кабинету Lumean за 29.10–08.11.2026 — 31,66 LUMC за 5,98 млн входных и
    # 0,39 млн выходных токенов; 1 LUMC = 10 ₽, 1 $ ≈ 82 ₽ (курс пополнения). Это оценка, а не тариф Lumean
    "lumean_in_per_m": _env_float("PRICE_LUMEAN_IN_PER_M", 0.24),
    "lumean_out_per_m": _env_float("PRICE_LUMEAN_OUT_PER_M", 5.8),
    "lumean_llm_call": _env_float("PRICE_LUMEAN_LLM_CALL"),          # запасная цена за вызов, если нет токенов
    # озвучка: по кабинету Lumean 08.11.2026 заказ шортса стоит 433–988 токенов подписки — ≈1 токен на символ
    # (журнал бота: 5 854 симв. за день при ~3 400 токенах успешных заказов + компенсированные); 1 токен =
    # 99 ₽ / 100 000 = 0,001 ₽ → 1000 символов ≈ 1 ₽ ≈ 0,012 $ (1 $ ≈ 82 ₽). Оценка, уточняется в .env
    "lumean_tts_per_1k": _env_float("PRICE_LUMEAN_TTS_PER_1K_CHARS", 0.012),
    # музыка: 94 токена за заказ из 2 вариантов ≈ 0,1 ₽ → ≈ 0,0006 $ за трек
    "lumean_music_track": _env_float("PRICE_LUMEAN_MUSIC_TRACK", 0.0006),
    "search_call": _env_float("PRICE_SEARCH_CALL", 0.0),             # Tavily/Яндекс — в бесплатных лимитах
}
IMAGE_USD = PRICES["image"] or 0.0

_pool_getter: Callable[[], Any] | None = None  # bot.py передаёт db() после подключения к PostgreSQL
_lock = asyncio.Lock()
LOCK_ID = 7300002
_warned: set[str] = set()  # «кошелёк:месяц», о которых уже предупредили
on_warning: Callable[[str], Any] | None = None  # bot.py: отправить предупреждение автору


class BudgetExceeded(RuntimeError):
    pass


def attach(pool_getter: Callable[[], Any]) -> None:
    global _pool_getter
    _pool_getter = pool_getter


def price(name: str) -> float:
    return PRICES.get(name) or 0.0


def priced(name: str) -> bool:
    return PRICES.get(name) is not None


SPENT_SQL = ("SELECT COALESCE(SUM(COALESCE(actual_usd, estimate_usd)), 0) FROM ai_usage WHERE pool = $1 "
             "AND status IN ('reserved', 'settled') AND created_at >= date_trunc('month', NOW())")


async def spent(pool: str) -> float:
    if not _pool_getter:
        return 0.0
    return float(await _pool_getter().fetchval(SPENT_SQL, pool))


class Reservation:
    def __init__(self, rid: int | None, pool: str, estimate: float) -> None:
        self.id, self.pool, self.estimate, self.done = rid, pool, estimate, False

    async def settle(self, actual_usd: float | None = None, units: dict[str, Any] | None = None) -> None:
        """Фактическая стоимость после ответа (None — остаётся оценка)."""
        self.done = True
        if self.id is None or not _pool_getter:
            return
        await _pool_getter().execute(
            "UPDATE ai_usage SET status = 'settled', actual_usd = $2, units = $3::jsonb, settled_at = NOW() "
            "WHERE id = $1", self.id, actual_usd, json.dumps(units or {}))

    async def cancel(self, error: str = "") -> None:
        self.done = True
        if self.id is None or not _pool_getter:
            return
        await _pool_getter().execute("UPDATE ai_usage SET status = 'failed', units = $2::jsonb, settled_at = NOW() "
                                     "WHERE id = $1", self.id, json.dumps({"error": error[:200]}))


async def reserve(pool: str, op: str, estimate: float, provider: str = "") -> Reservation:
    """Зарезервировать оценку до запроса; не влезает в лимит — BudgetExceeded (бесплатное — всегда можно)."""
    if not _pool_getter:  # база не подключена (тесты, отдельный процесс монтажа) — без учёта
        return Reservation(None, pool, estimate)
    async with _lock:
        conn_pool = _pool_getter()
        async with conn_pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", LOCK_ID)  # и между копиями бота
            used = float(await conn.fetchval(SPENT_SQL, pool))
            limit = LIMITS[pool]
            if estimate > 0 and used + estimate > limit:
                raise BudgetExceeded(f"месячный лимит «{POOL_NAMES[pool]}» исчерпан: {used:.2f}$ из {limit:g}$")
            rid = await conn.fetchval(
                "INSERT INTO ai_usage (pool, op, provider, status, estimate_usd) VALUES ($1, $2, $3, 'reserved', $4) "
                "RETURNING id", pool, op, provider, estimate)
    month_key = f"{pool}:{__import__('datetime').date.today():%Y-%m}"
    if estimate > 0 and used + estimate >= limit * WARN_SHARE and month_key not in _warned:
        _warned.add(month_key)
        text = (f"⚠️ Бюджет «{POOL_NAMES[pool]}»: потрачено {used + estimate:.2f}$ из {limit:g}$ (80 %). "
                "На 100 % платные вызовы этого кошелька остановятся до следующего месяца.")
        if on_warning:
            try:
                await on_warning(text)
            except Exception:  # noqa: BLE001
                log.exception("budget warning failed")
    return Reservation(rid, pool, estimate)


@asynccontextmanager
async def guard(pool: str, op: str, estimate: float, provider: str = "") -> AsyncIterator[Reservation]:
    """async with guard("ai", "gemini", 0.002, model) as r: … r.settle(факт). Ошибка запроса — резерв снят."""
    r = await reserve(pool, op, estimate, provider)
    try:
        yield r
    except BaseException as exc:
        if not r.done:
            await r.cancel(f"{type(exc).__name__}: {exc}")
        raise
    if not r.done:
        await r.settle(None)


def lumean_cost(tokens_in: int, tokens_out: int) -> float:
    return tokens_in / 1e6 * price("lumean_in_per_m") + tokens_out / 1e6 * price("lumean_out_per_m")


def gemini_cost(tokens_in: int, tokens_out: int) -> float:
    return tokens_in / 1e6 * price("gemini_in_per_m") + tokens_out / 1e6 * price("gemini_out_per_m")


async def status_lines() -> list[str]:
    """Отчёт автору: потрачено по кошелькам за месяц и какие цены ещё не заданы."""
    out = []
    for pool, limit in LIMITS.items():
        used = await spent(pool)
        out.append(f"💵 {POOL_NAMES[pool]}: ≈{used:.2f}$ из {limit:g}$ за месяц"
                   + (" — лимит исчерпан" if used >= limit else ""))
    tts_limit = LIMITS["tts"]
    per_1k = price("lumean_tts_per_1k")
    if per_1k:
        left = max(0.0, tts_limit - await spent("tts"))
        out.append(f"🗣 Озвучки осталось ≈ {int(left / per_1k * 1000):,} символов в этом месяце".replace(",", " ")
                   + " (≈ столько же токенов подписки Lumean)")
    missing = [n for n in ("lumean_tts_per_1k", "lumean_music_track") if not priced(n)]
    if missing:
        out.append("ℹ️ Цены Lumean ещё не заданы (" + ", ".join(missing) + ") — эти вызовы считаются, но по 0$. "
                   "После 10–20 вызовов сверим с кабинетом Lumean и впишем цены в .env.")
    return out


async def lumean_check() -> list[str]:
    """Для сверки цен Lumean: сколько вызовов и символов озвучки за 30 дней по журналу ai_usage."""
    if not _pool_getter:
        return []
    rows = await _pool_getter().fetch(
        "SELECT op, COUNT(*) AS n, COALESCE(SUM((units->>'chars')::BIGINT), 0) AS chars FROM ai_usage "
        "WHERE provider LIKE 'lumean%' AND status = 'settled' AND created_at > NOW() - INTERVAL '30 days' "
        "GROUP BY op ORDER BY op")
    return [f"{r['op']}: {r['n']} выз." + (f", {r['chars']:,} симв.".replace(",", " ") if r["chars"] else "")
            for r in rows]

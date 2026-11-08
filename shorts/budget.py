"""Месячный лимит платных операций с известной ценой (сейчас — картинки, нарисованные ИИ).

Цены берём с официальных страниц провайдеров и держим в переменных .env, чтобы их можно было поправить без
обновления кода. Озвучка и ИИ Lumean сюда не входят: их цену бот не знает (её видно в кабинете Lumean) — они
только считаются в отчёте «Расходы». Сумма — оценка, а не счёт провайдера.
"""

from __future__ import annotations

import os
from datetime import date

from . import usage

# лимит в месяц, $ — по решению автора канала (ноябрь 2026)
MONTHLY_USD = float(os.getenv("AI_MONTHLY_BUDGET_USD") or 10)
# картинка 16:9 от модели Gemini Flash Image: на странице цен Google (ai.google.dev/gemini-api/docs/pricing,
# ноябрь 2026) — $0.039–0.045 за картинку до 1K; берём верхнюю оценку
IMAGE_USD = float(os.getenv("AI_COST_IMAGE_USD") or 0.045)
WARN_SHARE = 0.8  # с 80 % лимита предупреждаем автора


def spent_month() -> float:
    """Сколько $ (по оценке) ушло на платные операции с начала месяца."""
    month = date.today().isoformat()[:7]
    return round(sum(float(day.get("usd") or 0) for d, day in usage._load().items() if d.startswith(month)), 4)


def allow(cost: float) -> bool:
    """Хватит ли лимита на операцию: на 100 % платные операции блокируются."""
    return spent_month() + cost <= MONTHLY_USD + 1e-9


def charge(cost: float, task: str) -> str:
    """Записать расход; вернуть предупреждение, если перешли 80 % лимита (иначе "")."""
    before = spent_month()
    usage.record("usd", cost, task)
    after = before + cost
    if before < MONTHLY_USD * WARN_SHARE <= after:
        return (f"⚠️ Потрачено {after:.2f}$ из {MONTHLY_USD:.0f}$ месячного лимита на картинки ИИ — "
                "на 100 % бот перестанет их рисовать до следующего месяца.")
    return ""


def status_line() -> str:
    spent = spent_month()
    return (f"💵 Платные операции (картинки ИИ) за месяц: ≈{spent:.2f}$ из {MONTHLY_USD:.0f}$"
            + (" — лимит исчерпан, картинки не рисуются" if spent >= MONTHLY_USD else ""))

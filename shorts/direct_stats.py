"""Статистика Яндекс Директа (Reports API v5) для ежедневного отчёта админу в Telegram."""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
from collections.abc import Mapping
from datetime import date, timedelta
from html import escape
from time import monotonic

import aiohttp

API = os.getenv("DIRECT_REPORTS_URL", "https://api.direct.yandex.com/json/v5/reports")
REQUEST_TIMEOUT = 60
REPORT_TIMEOUT = 300
TRANSIENT_ATTEMPTS = 3
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class DirectReportError(RuntimeError):
    """A report failed; the message is safe to show in Telegram."""


def _retry_delay(headers: Mapping[str, str], default: int) -> int:
    # Do not poll earlier than retryIn: the report may still be queued by Direct.
    for name in ("retryIn", "Retry-After"):
        try:
            delay = int(headers.get(name, ""))
        except (ValueError, TypeError):
            continue
        if delay > 0:
            return delay
    return default


def token() -> str:
    return os.getenv("YANDEX_DIRECT_TOKEN", "")


def _campaign_filter() -> list[dict]:
    ids = [x.strip() for x in os.getenv("DIRECT_CAMPAIGN_IDS", "").split(",") if x.strip().isdigit()]
    return [{"Field": "CampaignId", "Operator": "IN", "Values": ids}] if ids else []


async def report(kind: str, fields: list[str], date_from: date, date_to: date) -> list[dict]:
    """Строки отчёта (TSV → словари). Отчёт строится в офлайне — ждём, пока Директ его соберёт."""
    body = {"params": {
        "SelectionCriteria": {"DateFrom": date_from.isoformat(), "DateTo": date_to.isoformat(),
                              **({"Filter": flt} if (flt := _campaign_filter()) else {})},
        "FieldNames": fields,
        "ReportName": f"vkino {kind} {date_from} {date_to} {','.join(fields)}",
        "ReportType": kind, "DateRangeType": "CUSTOM_DATE", "Format": "TSV",
        "IncludeVAT": "YES", "IncludeDiscount": "NO"}}
    headers = {"Authorization": f"Bearer {token()}", "Accept-Language": "ru", "processingMode": "auto",
               "returnMoneyInMicros": "false", "skipReportHeader": "true", "skipReportSummary": "true"}
    if login := os.getenv("DIRECT_CLIENT_LOGIN"):
        headers["Client-Login"] = login
    deadline = monotonic() + REPORT_TIMEOUT
    failures = 0
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as s:
        while (remaining := deadline - monotonic()) > 0:
            retry_headers = {}
            try:
                async with s.post(API, json=body, headers=headers,
                                  timeout=aiohttp.ClientTimeout(total=min(REQUEST_TIMEOUT, remaining))) as r:
                    text = await r.text()
                    if r.status == 200:
                        return list(csv.DictReader(io.StringIO(text), delimiter="\t"))
                    if r.status in (201, 202):
                        delay = _retry_delay(r.headers, 10)
                        transient = False
                    elif r.status in RETRYABLE_STATUS:
                        retry_headers = r.headers
                        transient = True
                    else:
                        raise DirectReportError(
                            f"Директ отклонил запрос (HTTP {r.status}). "
                            "Проверьте доступ к рекламному кабинету и настройки отчёта.")
            except (TimeoutError, aiohttp.ClientConnectionError, aiohttp.ClientPayloadError):
                transient = True
            if transient:
                failures += 1
                if failures >= TRANSIENT_ATTEMPTS:
                    raise DirectReportError(
                        f"Директ временно недоступен или отвечает слишком долго "
                        f"(неудачных попыток: {failures}). Повторите /direct позже.")
                delay = _retry_delay(retry_headers, 5 * 2 ** (failures - 1))
            # Release the response before waiting; retries reuse the identical report request.
            remaining = deadline - monotonic()
            if delay >= remaining:
                break
            await asyncio.sleep(delay)
    raise DirectReportError("Директ не успел подготовить отчёт за 5 минут. Повторите /direct позже.")


def _num(v: str | None) -> float:
    try:
        return float(v or 0)
    except ValueError:
        return 0.0


def _rub(v: float) -> str:
    return f"{v:,.0f}".replace(",", " ") + " ₽" if v >= 100 else f"{v:.2f} ₽".replace(".", ",")


async def daily_text(subs_now: int | None = None, subs_before: int | None = None) -> str:
    """Отчёт за вчера и за 7 дней + лучшие и подозрительные площадки."""
    today = date.today()
    y, week = today - timedelta(days=1), today - timedelta(days=7)
    f = ["CampaignName", "Impressions", "Clicks", "Cost"]
    results = await asyncio.gather(
        report("CAMPAIGN_PERFORMANCE_REPORT", f, y, y),
        report("CAMPAIGN_PERFORMANCE_REPORT", f, week, y),
        report("CUSTOM_REPORT", ["Placement", "Impressions", "Clicks", "Cost"], week, y),
        return_exceptions=True)
    # A failed section is unavailable, never an empty report or zero spending.
    for result in results:
        if isinstance(result, asyncio.CancelledError):
            raise result
    unavailable = [label for label, result in zip(("вчера", "7 дней", "площадки"), results)
                   if isinstance(result, Exception)]
    for label, result in zip(("day", "week", "placements"), results):
        if isinstance(result, Exception):
            logging.warning("Direct report section %s failed: %s", label, type(result).__name__)
    if len(unavailable) == len(results):
        for result in results:
            if isinstance(result, DirectReportError):
                raise result
        raise DirectReportError("Не удалось получить данные Директа. Повторите /direct позже.")
    day_rows, week_rows, places = [None if isinstance(r, Exception) else r for r in results]

    def total(rows: list[dict]) -> tuple[float, float, float]:
        return (sum(_num(r["Impressions"]) for r in rows), sum(_num(r["Clicks"]) for r in rows),
                sum(_num(r["Cost"]) for r in rows))

    lines = ["📊 <b>Реклама KinoMir360 в Яндекс Директе</b>", ""]
    if unavailable:
        lines.extend(["⚠️ <b>Неполный отчёт.</b> Не удалось получить: "
                      + ", ".join(unavailable) + ". Повторить запрос: /direct.", ""])
    for title, rows in ((f"Вчера ({y:%d.%m})", day_rows), ("За 7 дней", week_rows)):
        if rows is None:
            lines.append(f"<b>{title}:</b> данные недоступны")
            continue
        imp, clk, cost = total(rows)
        ctr = f"{clk / imp * 100:.2f}%" if imp else "—"
        cpc = _rub(cost / clk) if clk else "—"
        n = lambda v: f"{v:,.0f}".replace(",", " ")  # noqa: E731
        lines.append(f"<b>{title}:</b> показы {n(imp)} · клики {n(clk)} · CTR {ctr} · "
                     f"расход {_rub(cost)} · клик {cpc}")
    if week_rows and len({r["CampaignName"] for r in week_rows}) > 1:  # сравнение за 7 дней
        lines.append("\n<b>По кампаниям за 7 дней:</b>")
        for r in sorted(week_rows, key=lambda r: _num(r["Cost"]), reverse=True):
            clk, cost = _num(r["Clicks"]), _num(r["Cost"])
            cpc = _rub(cost / clk) if clk else "—"
            lines.append(f"• {escape(r['CampaignName'])}: {clk:.0f} кл. · {_rub(cost)} · клик {cpc}")
    if subs_now is not None:
        lines.append(f"\n👥 Подписчиков: <b>{subs_now}</b>")
        if subs_before is not None:
            diff = subs_now - subs_before
            _, _, cost_y = total(day_rows or [])
            per = f" · цена подписчика ≈ {_rub(cost_y / diff)}" if diff > 0 and cost_y else ""
            lines.append(f"За сутки: {diff:+d}{per} (прирост не только от рекламы)")
    places = sorted(places or [], key=lambda r: _num(r["Clicks"]), reverse=True)
    if places:
        lines.append("\n<b>Площадки за 7 дней (по кликам):</b>")
        for r in places[:5]:
            lines.append(f"• {escape(r['Placement'])} — {r['Clicks']} кл., {_rub(_num(r['Cost']))}")
        bad = [r for r in places if _num(r["Impressions"]) >= 200
               and _num(r["Clicks"]) / _num(r["Impressions"]) > 0.05]
        if bad:
            lines.append("\n⚠️ <b>Подозрительно высокий CTR (>5%) — кандидаты на запрет:</b>")
            lines += [f"• <code>{escape(r['Placement'])}</code>" for r in bad[:5]]
    if week_rows == []:
        lines.append("\nЗа последние 7 дней Директ не вернул строк по выбранным кампаниям.")
    return "\n".join(lines)


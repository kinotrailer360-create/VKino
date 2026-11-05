"""Статистика Яндекс Директа (Reports API v5) для ежедневного отчёта админу в Telegram."""
from __future__ import annotations

import asyncio
import csv
import io
import os
from datetime import date, timedelta
from html import escape

import aiohttp

API = os.getenv("DIRECT_REPORTS_URL", "https://api.direct.yandex.com/json/v5/reports")


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
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
        for _ in range(20):
            async with s.post(API, json=body, headers=headers) as r:
                text = await r.text()
                if r.status == 200:
                    return list(csv.DictReader(io.StringIO(text), delimiter="\t"))
                if r.status in (201, 202):
                    await asyncio.sleep(min(int(r.headers.get("retryIn", "10") or 10), 30))
                    continue
                raise RuntimeError(f"Директ ответил {r.status}: {text[:300]}")
    raise RuntimeError("Директ не успел собрать отчёт")


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
    day_rows, week_rows, places = await asyncio.gather(
        report("CAMPAIGN_PERFORMANCE_REPORT", f, y, y),
        report("CAMPAIGN_PERFORMANCE_REPORT", f, week, y),
        report("CUSTOM_REPORT", ["Placement", "Impressions", "Clicks", "Cost"], week, y))

    def total(rows: list[dict]) -> tuple[float, float, float]:
        return (sum(_num(r["Impressions"]) for r in rows), sum(_num(r["Clicks"]) for r in rows),
                sum(_num(r["Cost"]) for r in rows))

    lines = ["📊 <b>Реклама KinoMir360 в РСЯ</b>", ""]
    for title, rows in ((f"Вчера ({y:%d.%m})", day_rows), ("За 7 дней", week_rows)):
        imp, clk, cost = total(rows)
        ctr = f"{clk / imp * 100:.2f}%" if imp else "—"
        cpc = _rub(cost / clk) if clk else "—"
        n = lambda v: f"{v:,.0f}".replace(",", " ")  # noqa: E731
        lines.append(f"<b>{title}:</b> показы {n(imp)} · клики {n(clk)} · CTR {ctr} · "
                     f"расход {_rub(cost)} · клик {cpc}")
    if subs_now is not None:
        lines.append(f"\n👥 Подписчиков: <b>{subs_now}</b>")
        if subs_before is not None:
            diff = subs_now - subs_before
            _, _, cost_y = total(day_rows)
            per = f" · цена подписчика ≈ {_rub(cost_y / diff)}" if diff > 0 and cost_y else ""
            lines.append(f"За сутки: {diff:+d}{per} (прирост не только от рекламы)")
    places = sorted(places, key=lambda r: _num(r["Clicks"]), reverse=True)
    if places:
        lines.append("\n<b>Площадки за 7 дней (по кликам):</b>")
        for r in places[:5]:
            lines.append(f"• {escape(r['Placement'])} — {r['Clicks']} кл., {_rub(_num(r['Cost']))}")
        bad = [r for r in places if _num(r["Impressions"]) >= 200
               and _num(r["Clicks"]) / _num(r["Impressions"]) > 0.05]
        if bad:
            lines.append("\n⚠️ <b>Подозрительно высокий CTR (>5%) — кандидаты на запрет:</b>")
            lines += [f"• <code>{escape(r['Placement'])}</code>" for r in bad[:5]]
    if not week_rows:
        lines.append("\nПоказов пока нет — кампания на модерации или выключена.")
    return "\n".join(lines)

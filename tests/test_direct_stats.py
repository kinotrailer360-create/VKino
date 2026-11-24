import asyncio
import copy
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

import aiohttp

from shorts import direct_stats as direct


class Response:
    def __init__(self, status=200, text="Clicks\tCost\n2\t10.50\n", headers=None):
        self.status = status
        self.body = text
        self.headers = headers or {}
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def text(self):
        if isinstance(self.body, BaseException):
            raise self.body
        return self.body


class ReportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 0
        self.responses = []
        self.requests = []
        self.waits = []
        self.last_response = None
        self.session_closed = False
        self.entered = 0
        case = self

        class Session:
            async def __aenter__(self):
                case.entered += 1
                return self

            async def __aexit__(self, *args):
                case.session_closed = True

            def post(self, url, **kwargs):
                case.requests.append((url, copy.deepcopy(kwargs)))
                response = case.responses.pop(0)
                if isinstance(response, BaseException):
                    case.last_response = None
                    raise response
                case.last_response = response
                return response

        async def sleep(delay):
            if self.last_response is not None:
                self.assertTrue(self.last_response.closed)
            self.waits.append(delay)
            self.now += delay

        for p in (patch.object(direct.aiohttp, "ClientSession", side_effect=lambda **kw: Session()),
                  patch.object(direct, "monotonic", side_effect=lambda: self.now),
                  patch.object(direct.asyncio, "sleep", side_effect=sleep)):
            p.start()
            self.addCleanup(p.stop)

    async def fetch(self):
        return await direct.report("CUSTOM_REPORT", ["Clicks", "Cost"], date(2026, 11, 20), date(2026, 11, 23))

    async def test_timeout_retries_identical_report_and_parses_tsv(self):
        self.responses = [TimeoutError(), Response()]
        self.assertEqual(await self.fetch(), [{"Clicks": "2", "Cost": "10.50"}])
        self.assertEqual(self.waits, [5])
        self.assertEqual(self.requests[0], self.requests[1])
        self.assertEqual(self.entered, 1)
        self.assertTrue(self.session_closed)

    async def test_connection_and_body_failure_retry(self):
        broken = Response(text=aiohttp.ClientPayloadError("connection lost"))
        self.responses = [aiohttp.ServerDisconnectedError(), broken, Response()]
        await self.fetch()
        self.assertTrue(broken.closed)
        self.assertEqual(self.waits, [5, 10])

    async def test_queued_report_honors_retry_in_over_thirty_seconds(self):
        self.responses = [Response(201, headers={"retryIn": "45"}),
                          Response(202, headers={"retryIn": "2"}), Response()]
        await self.fetch()
        self.assertEqual(self.waits, [45, 2])

    async def test_transient_http_retries(self):
        self.responses = [Response(429, headers={"Retry-After": "40"}), Response(503), Response()]
        await self.fetch()
        self.assertEqual(self.waits, [40, 10])

    async def test_permanent_error_is_not_retried_or_echoed(self):
        self.responses = [Response(403, text="private provider error details")]
        with self.assertRaises(direct.DirectReportError) as error:
            await self.fetch()
        self.assertIn("403", str(error.exception))
        self.assertNotIn("private", str(error.exception))
        self.assertEqual(self.waits, [])
        self.assertTrue(self.session_closed)

    async def test_timeout_retries_are_bounded_and_explained(self):
        self.responses = [TimeoutError() for _ in range(3)]
        with self.assertRaisesRegex(direct.DirectReportError, "попыток: 3.* /direct"):
            await self.fetch()
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.waits, [5, 10])

    async def test_invalid_retry_headers_use_default_without_busy_loop(self):
        self.responses = [Response(202, headers={"retryIn": "invalid"}),
                          Response(202, headers={"retryIn": "-1"}),
                          Response(202, headers={"retryIn": "0"}), Response()]
        await self.fetch()
        self.assertEqual(self.waits, [10, 10, 10])

    async def test_offline_polling_has_total_deadline(self):
        self.responses = [Response(201, headers={"retryIn": "200"}),
                          Response(202, headers={"retryIn": "200"})]
        with self.assertRaisesRegex(direct.DirectReportError, "5 минут"):
            await self.fetch()
        self.assertEqual(self.waits, [200])
        self.assertEqual(len(self.requests), 2)

    async def test_request_timeout_is_limited_by_remaining_time(self):
        self.responses = [Response(201, headers={"retryIn": "295"}), Response()]
        await self.fetch()
        self.assertEqual(self.requests[0][1]["timeout"].total, 60)
        self.assertEqual(self.requests[1][1]["timeout"].total, 5)

    async def test_cancellation_is_not_retried(self):
        self.responses = [asyncio.CancelledError()]
        with self.assertRaises(asyncio.CancelledError):
            await self.fetch()
        self.assertEqual(self.waits, [])
        self.assertTrue(self.session_closed)


class DailyTextTests(unittest.IsolatedAsyncioTestCase):
    day = [{"CampaignName": "Cinema", "Impressions": "100", "Clicks": "10", "Cost": "25"}]
    week = [{"CampaignName": "Cinema", "Impressions": "700", "Clicks": "70", "Cost": "175"}]
    places = [{"Placement": "<example>", "Impressions": "100", "Clicks": "10", "Cost": "25"}]

    async def render(self, results):
        with patch.object(direct, "report", new=AsyncMock(side_effect=results)):
            return await direct.daily_text(110, 100)

    async def test_partial_report_retains_metrics_and_labels_missing_placements(self):
        text = await self.render([self.day, self.week, TimeoutError()])
        self.assertIn("Неполный отчёт", text)
        self.assertIn("Не удалось получить: площадки", text)
        self.assertIn("расход 25,00 ₽", text)
        self.assertIn("расход 175 ₽", text)
        self.assertIn("/direct", text)

    async def test_missing_day_does_not_invent_zero_cost_or_subscriber_price(self):
        text = await self.render([TimeoutError(), self.week, self.places])
        self.assertIn("данные недоступны", text)
        self.assertNotIn("цена подписчика", text)
        self.assertNotIn("расход 0", text)
        self.assertIn("&lt;example&gt;", text)

    async def test_missing_week_is_not_reported_as_inactive_campaign(self):
        text = await self.render([self.day, TimeoutError(), self.places])
        self.assertIn("За 7 дней:</b> данные недоступны", text)
        self.assertNotIn("не вернул строк", text)
        self.assertNotIn("Показов пока нет", text)

    async def test_empty_successful_reports_remain_distinct_from_failure(self):
        text = await self.render([[], [], []])
        self.assertNotIn("Неполный отчёт", text)
        self.assertIn("не вернул строк", text)

    async def test_complete_report_retains_subscriber_cost_and_html_escaping(self):
        text = await self.render([self.day, self.week, self.places])
        self.assertNotIn("Неполный отчёт", text)
        self.assertIn("цена подписчика ≈ 2,50 ₽", text)
        self.assertIn("&lt;example&gt;", text)

    async def test_total_failure_explains_retry_instead_of_showing_zeroes(self):
        with self.assertRaisesRegex(direct.DirectReportError, "/direct"):
            await self.render([TimeoutError(), TimeoutError(), TimeoutError()])

    async def test_total_failure_keeps_provider_diagnostic(self):
        with self.assertRaisesRegex(direct.DirectReportError, "HTTP 403"):
            await self.render([direct.DirectReportError("HTTP 403"), TimeoutError(), TimeoutError()])

    async def test_cancellation_is_not_hidden_as_partial_success(self):
        with self.assertRaises(asyncio.CancelledError):
            await self.render([self.day, asyncio.CancelledError(), self.places])


if __name__ == "__main__":
    unittest.main()

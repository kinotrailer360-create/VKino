import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from shorts.ai_studio import StudioClient, StudioError, StudioStore, bounded, build_router, model_sequence, sole_owner


class StudioTests(unittest.TestCase):
    def test_owner(self):
        self.assertEqual(sole_owner({123}), 123)
        self.assertIsNone(sole_owner({123, 456}))
        self.assertIsNone(sole_owner({123}, "456"))
        self.assertEqual(sole_owner({123, 456}, "456"), 456)

    def test_history_and_draft(self):
        with tempfile.TemporaryDirectory() as d:
            s = StudioStore(Path(d) / "history.db")
            m = [{"role": "user", "content": "one"}]
            s.save(1, "news", m)
            self.assertEqual(s.history(1, "news"), m)
            self.assertEqual(s.history(2, "news"), [])
            self.assertEqual(s.history(1, "shorts"), [])
            t = s.draft(1, "script")
            self.assertIsNone(s.get_draft(2, t))
            self.assertEqual(s.get_draft(1, t), "script")
            s.consume_draft(1, t)
            self.assertIsNone(s.get_draft(1, t))
            s.set_provider(1, "groq")
            self.assertEqual(s.provider(1), "groq")
            s.reset(1, "news")
            self.assertEqual(s.history(1, "news"), [])

    def test_bounds(self):
        items = [{"role": "user", "content": "a" * 2000}, {"role": "assistant", "content": "b" * 2000}] * 20
        result = bounded(items)
        self.assertLessEqual(len(result), 12)
        self.assertLessEqual(sum(len(x["content"]) for x in result), 24000)

    def test_registration(self):
        r = build_router({123}, Path("/tmp/unused-ai-studio"), lambda text: {}, AsyncMock(), lambda: False)
        self.assertEqual(len(r.message.handlers), 3)
        self.assertEqual(len(r.callback_query.handlers), 1)

    def test_models(self):
        self.assertEqual(model_sequence("vkino", "auto"), [("Gemini", "vkino-technical"), ("Groq", "vkino-groq")])
        self.assertEqual(model_sequence("news", "groq"), [("Groq", "kinomir360-groq")])


class StudioClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_fallback(self):
        c = StudioClient(Path("/tmp"))
        with patch.object(c, "_request", AsyncMock(side_effect=[StudioError("HTTP 503", True), "готово"])) as request:
            self.assertEqual(await c.ask("news", "auto", [], "Новости"), ("готово", "Groq"))
            self.assertEqual(request.await_count, 2)

    async def test_auth_no_fallback(self):
        c = StudioClient(Path("/tmp"))
        with patch.object(c, "_request", AsyncMock(side_effect=StudioError("HTTP 401"))) as request:
            with self.assertRaises(StudioError):
                await c.ask("news", "auto", [], "Новости")
            self.assertEqual(request.await_count, 1)

    async def test_short_format(self):
        c = StudioClient(Path("/tmp"))
        with patch.object(c, "_request", AsyncMock(return_value="script")) as request:
            await c.ask("shorts", "gemini", [], "Сценарий")
            self.assertIn("## Карта видеоряда", request.await_args.args[1][-1]["content"])
            self.assertIn("Сценарий", request.await_args.args[1][-1]["content"])


class GroundedNewsTests(unittest.TestCase):
    def test_real_metadata_not_generated_url(self):
        from shorts.ai_studio import grounded_news_answer
        url = "https://kino.mail.ru/news/145878-real-article/"
        data = {"sources": [{"source": {"type": "collection"}, "citation_ids": [1],
                             "metadata": [{"source": "KinoMir360.md"}]},
                            {"source": {"type": "web_search"}, "citation_ids": [2],
                             "metadata": [{"source": url, "title": "Кино Mail"}]}]}
        raw = "Подтверждено [2].\n\n**Источники:**\n[Кино Mail](https://kino.mail.ru/news/fake/) [2]"
        result = grounded_news_answer(raw, data, "Проверь")
        self.assertIn(url, result)
        self.assertNotIn("/fake/", result)
        self.assertNotIn("[2]", result)
        self.assertNotIn("KinoMir360.md", result)

    def test_no_sources_is_explicit(self):
        from shorts.ai_studio import grounded_news_answer
        result = grounded_news_answer("Подтверждено [1].", {}, "Проверь")
        self.assertIn("достоверность не подтверждена", result)
        self.assertNotIn("[1]", result)

    def test_adaptation_without_sources(self):
        from shorts.ai_studio import grounded_news_answer
        self.assertEqual(grounded_news_answer("Готовый пост.", {}, "Для Threads"), "Готовый пост.")

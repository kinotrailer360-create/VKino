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
        self.assertEqual(len(r.message.handlers), 7)
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


class SourceAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_unrelated_text_untouched(self):
        from shorts.ai_studio import verify_news_links
        self.assertEqual(await verify_news_links("Пост без источников"), "Пост без источников")

    async def test_statuses_follow_real_urls(self):
        from shorts.ai_studio import _SOURCE_BLOCK, verify_news_links
        answer = "Проверка" + _SOURCE_BLOCK + "• Один\nhttps://example.com/a\n\n• Два\nhttps://example.com/b"
        with patch("shorts.ai_studio._source_status", AsyncMock(side_effect=[
            "открывается (HTTP 200)", "не удалось открыть (HTTP 404)"
        ])) as check:
            result = await verify_news_links(answer)
            self.assertIn("https://example.com/a\nДоступность: открывается (HTTP 200)", result)
            self.assertIn("https://example.com/b\nДоступность: не удалось открыть (HTTP 404)", result)
            self.assertEqual(check.await_count, 2)

    async def test_reject_credential_and_internal_port(self):
        from shorts.ai_studio import _source_status
        self.assertEqual(await _source_status(None, "http://user:pass@example.com/"), "небезопасный адрес")
        self.assertEqual(await _source_status(None, "http://example.com:8080/"), "небезопасный адрес")


class VoiceCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_voice_limits(self):
        from types import SimpleNamespace
        from shorts.studio_voice import voice_limits, VoiceError
        voice_limits(SimpleNamespace(duration=10, file_size=500))
        with self.assertRaises(VoiceError):
            voice_limits(SimpleNamespace(duration=121, file_size=500))
        with self.assertRaises(VoiceError):
            voice_limits(SimpleNamespace(duration=10, file_size=5000000))

    def test_buffer_limit(self):
        from shorts.studio_voice import LimitedBuffer, VoiceError, MAX_VOICE_BYTES
        with LimitedBuffer() as buf:
            with self.assertRaises(VoiceError):
                buf.write(b"x" * (MAX_VOICE_BYTES + 1))

    async def test_groq_first(self):
        from shorts import studio_voice
        with patch.object(studio_voice, "convert_voice", AsyncMock(return_value=b"mp3")), \
                patch.object(studio_voice, "_groq_available", return_value=True), \
                patch.object(studio_voice.ai, "_groq_whisper", AsyncMock(return_value=[{"text": "Привет мир"}])):
            self.assertEqual(await studio_voice.transcribe_voice(b"OggS"), ("Привет мир", "Whisper / Groq"))

    async def test_gemini_fallback(self):
        from shorts import studio_voice
        with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}), \
                patch.object(studio_voice, "convert_voice", AsyncMock(return_value=b"mp3")), \
                patch.object(studio_voice, "_groq_available", return_value=True), \
                patch.object(studio_voice.ai, "_groq_whisper", AsyncMock(side_effect=RuntimeError())), \
                patch.object(studio_voice.ai, "_ask", AsyncMock(return_value={"text": "Найди новости"})):
            self.assertEqual(await studio_voice.transcribe_voice(b"OggS"), ("Найди новости", "Gemini"))

    async def test_unavailable_transcription(self):
        from shorts import studio_voice
        with patch.dict("os.environ", {"GEMINI_API_KEY": "", "GEMINI_API_KEY_2": ""}), \
                patch.object(studio_voice, "convert_voice", AsyncMock(return_value=b"mp3")), \
                patch.object(studio_voice, "_groq_available", return_value=False):
            with self.assertRaises(studio_voice.VoiceError):
                await studio_voice.transcribe_voice(b"OggS")

    async def test_reject_non_telegram_voice(self):
        from shorts import studio_voice
        with self.assertRaises(studio_voice.VoiceError):
            await studio_voice.convert_voice(b"not an ogg file")


class MemoryTests(unittest.TestCase):
    def test_scopes_and_reset(self):
        with tempfile.TemporaryDirectory() as d:
            s = StudioStore(Path(d) / "memory.db")
            s.remember(1, "all", "Краткие ответы")
            ident, new = s.remember(1, "km360", "Только фактчекинг по запросу")
            self.assertTrue(new)
            self.assertEqual(s.remember(1, "km360", "Только фактчекинг по запросу"), (ident, False))
            s.remember(1, "vikon", "Один вопрос")
            s.reset(1, "news")
            self.assertEqual(s.preferences_for(1, "news"), ["Краткие ответы", "Только фактчекинг по запросу"])
            self.assertEqual(s.preferences_for(1, "shorts"), s.preferences_for(1, "news"))
            self.assertEqual(s.preferences_for(1, "vikon"), ["Краткие ответы", "Один вопрос"])
            self.assertEqual(s.preferences_for(2, "news"), [])
            self.assertFalse(s.forget(2, "km360", ident))
            self.assertTrue(s.forget(1, "km360", ident))
            self.assertEqual(s.preferences_for(1, "news"), ["Краткие ответы"])

    def test_limits_and_secret_rejection(self):
        from shorts.ai_studio import normalize_rule, MAX_RULES
        with self.assertRaises(ValueError):
            normalize_rule("x" * 401)
        with self.assertRaises(ValueError):
            normalize_rule("sk-" + "x" * 30)
        with tempfile.TemporaryDirectory() as d:
            s = StudioStore(Path(d) / "memory.db")
            for i in range(MAX_RULES):
                s.remember(1, "all", f"Правило {i}")
            with self.assertRaises(ValueError):
                s.remember(1, "all", "Лишнее правило")

    def test_memory_prompt_preserves_task(self):
        from shorts.ai_studio import memory_prompt, MEMORY_TASK
        self.assertEqual(memory_prompt("Для Threads", []), "Для Threads")
        message = memory_prompt("Для Threads", ["Проверяй даты"])
        self.assertIn("Проверяй даты", message)
        self.assertEqual(message.rsplit(MEMORY_TASK, 1)[-1], "Для Threads")


class MemoryClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_memory_in_request_and_fallback(self):
        c = StudioClient(Path("/tmp"))
        with patch.object(c, "_request", AsyncMock(side_effect=[StudioError("HTTP 503", True), "готово"])) as request:
            self.assertEqual(await c.ask("news", "auto", [], "Для Threads", rules=["Кратко"]), ("готово", "Groq"))
            for call in request.await_args_list:
                self.assertIn("Кратко", call.args[1][-1]["content"])
                self.assertTrue(call.args[1][-1]["content"].endswith("Для Threads"))


class NewsCardTests(unittest.TestCase):
    def fixture(self):
        return {"sources": [{"source": {"type": "web_search"}, "citation_ids": [2,3,4], "document": ["News article. The studio announced a new film project for next year. More reporting follows here.", "Wikipedia article. The studio announced a new film project for next year. Additional details.", "Tag page. The studio announced a new film project for next year. More stories."], "metadata": [
            {"source": "https://deadline.com/news/article-123/", "title": "Article"},
            {"source": "https://en.wikipedia.org/wiki/Test", "title": "Wiki"},
            {"source": "https://deadline.com/tag/test/", "title": "Tag"}
        ]}]}

    def test_discovery_not_factcheck_or_list(self):
        from shorts.studio_news import is_discovery
        self.assertTrue(is_discovery("Найди последние новости про Голодные игры"))
        self.assertTrue(is_discovery("Что нового по Marvel?"))
        self.assertFalse(is_discovery("Проверь новость"))
        self.assertFalse(is_discovery("Найди новости списком"))
        self.assertFalse(is_discovery("Найди новости и сделай Shorts"))

    def test_card_source_identity(self):
        import json
        from datetime import date
        from shorts.studio_news import parse_cards
        raw = {"posts": [
            {"date": "2026-11-26", "status": "СМИ", "post": "Заголовок\n\nТекст [2]", "source_ids": [2], "evidence": {"2": "The studio announced a new film project for next year."}},
            {"date": "2026-11-26", "status": "СМИ", "post": "Выдумка", "source_ids": [99], "evidence": {"99": "The studio announced a new film project for next year."}},
            {"date": "2026-11-26", "status": "СМИ", "post": "Википедия", "source_ids": [3], "evidence": {"3": "The studio announced a new film project for next year."}},
            {"date": "2026-11-26", "status": "СМИ", "post": "Страница тегов", "source_ids": [4], "evidence": {"4": "The studio announced a new film project for next year."}},
            {"date": "2026-11-26", "status": "СМИ", "post": "Дубликат", "source_ids": [2], "evidence": {"2": "The studio announced a new film project for next year."}}
        ]}
        result = parse_cards(json.dumps(raw), self.fixture(), "Найди новости", current=date(2026,11,26))
        self.assertEqual(len(result["cards"]), 1)
        self.assertEqual(result["cards"][0]["sources"][0]["url"], "https://deadline.com/news/article-123/")
        self.assertNotIn("[2]", result["cards"][0]["post"])
        self.assertEqual(result["omitted"], 4)

    def test_fabricated_evidence_is_rejected(self):
        import json
        from datetime import date
        from shorts.studio_news import parse_cards
        raw = {"posts": [{"date": "2026-11-26", "status": "СМИ", "post": "Заголовок",
                          "source_ids": [2], "evidence": {"2": "The studio cancelled the film and announced an entirely different project."}}]}
        result = parse_cards(json.dumps(raw), self.fixture(), "Найди новости", current=date(2026,11,26))
        self.assertEqual(result["cards"], [])

    def test_old_and_future_omitted(self):
        import json
        from datetime import date
        from shorts.studio_news import parse_cards
        raw = {"posts": [{"date": d, "status": "СМИ", "post": d, "source_ids": [2], "evidence": {"2": "The studio announced a new film project for next year."}} for d in ("2026-10-25","2026-11-27")]}
        result = parse_cards(json.dumps(raw), self.fixture(), "Последние новости", current=date(2026,11,26))
        self.assertEqual(result["cards"], [])

    def test_card_ownership_and_revision(self):
        with tempfile.TemporaryDirectory() as d:
            s = StudioStore(Path(d) / "memory.db")
            token = s.news_card(1, 123, {"post":"Текст"})
            self.assertIsNone(s.get_news_card(2, token))
            self.assertEqual(s.get_news_card(1, token), (123, {"post":"Текст"}, False))
            s.mark_news_transferred(1, token)
            s.remove_news_card(1, token)
            self.assertTrue(s.get_news_card(1, token)[2])
            token2 = s.news_card(1, 124, {"post":"Новый"})
            s.remove_news_card(1, token2)
            self.assertIsNone(s.get_news_card(1, token2))


class NewsActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_is_separate_request(self):
        c = StudioClient(Path("/tmp"))
        with patch.object(c, "_request", AsyncMock(return_value={"cards": [], "note": "", "omitted": 0})) as request:
            await c.discover("gemini", "Найди новости про X")
            self.assertEqual(request.await_args.args[2], "news_cards")
            self.assertIn("source_ids", request.await_args.args[1][-1]["content"])

    async def test_threads_selected_text(self):
        c = StudioClient(Path("/tmp"))
        with patch.object(c, "_request", AsyncMock(return_value="Готово")) as request:
            self.assertEqual(await c.threads("groq", "Только выбранный пост"), ("Готово", "Groq"))
            self.assertEqual(request.await_args.args[2], "news_threads")
            self.assertIn("Только выбранный пост", request.await_args.args[1][-1]["content"])

    async def test_handoff_does_not_publish(self):
        import bot
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        message = SimpleNamespace(chat=SimpleNamespace(id=1), bot=MagicMock(), answer=AsyncMock())
        fake_db = SimpleNamespace(fetchval=AsyncMock(side_effect=[None, 1]))
        with patch.object(bot, "db", return_value=fake_db), patch.object(bot, "offer_post", AsyncMock()) as offer, \
             patch.object(bot, "publish_post", AsyncMock()) as publish:
            await bot.studio_news_handoff(message, 123)
            await bot.studio_news_handoff(message, 123)
            self.assertEqual(offer.await_count, 1)
            self.assertEqual(offer.await_args.args[2], [123])
            publish.assert_not_awaited()


    async def test_news_fallback_after_rejected_candidates(self):
        c = StudioClient(Path("/tmp"))
        first = {"cards": [], "note": "", "omitted": 2}
        second = {"cards": [{"post": "Пост"}], "note": "", "omitted": 0}
        with patch.object(c, "_request", AsyncMock(side_effect=[first, second])) as request:
            self.assertEqual(await c.discover("auto", "Найди новости"), (second, "Groq"))
            self.assertEqual(request.await_count, 2)


class FeedbackTests(unittest.TestCase):
    def test_owner_isolation_and_rating(self):
        with tempfile.TemporaryDirectory() as d:
            s = StudioStore(Path(d) / "feedback.db")
            token = s.feedback_item(1, "news", "Вопрос", "Ответ")
            self.assertIsNone(s.get_feedback(2, token))
            self.assertFalse(s.rate_feedback(2, token, 1))
            self.assertIsNone(s.note_feedback(2, token, "Чужая правка"))
            self.assertTrue(s.rate_feedback(1, token, 1))
            self.assertTrue(s.rate_feedback(1, token, 1))
            self.assertEqual(s.feedback_stats(1), (1, 1, 0, 0))
            first = s.note_feedback(1, token, "Уточни даты")
            second = s.note_feedback(1, token, "Уточни кассу")
            self.assertNotEqual(first, second)
            self.assertEqual(s.get_feedback(1, token)["note"], "Уточни кассу")
            self.assertEqual(s.feedback_stats(1), (1, 0, 1, 1))
            self.assertEqual(s.preferences_for(1, "news"), [])
            s.remember(1, "km360", s.get_feedback(1, token)["note"])
            self.assertEqual(s.preferences_for(1, "shorts"), ["Уточни кассу"])

    def test_retention_and_reset(self):
        from shorts.ai_studio import MAX_FEEDBACK
        with tempfile.TemporaryDirectory() as d:
            s = StudioStore(Path(d) / "feedback.db")
            first = s.feedback_item(1, "news", "first", "answer")
            for i in range(MAX_FEEDBACK):
                last = s.feedback_item(1, "news", str(i), "answer")
            self.assertIsNone(s.get_feedback(1, first))
            s.reset(1, "news")
            self.assertIsNotNone(s.get_feedback(1, last))
            self.assertEqual(s.feedback_stats(1)[0], MAX_FEEDBACK)

    def test_note_and_correction_limits(self):
        from shorts.ai_studio import correction_task
        with tempfile.TemporaryDirectory() as d:
            s = StudioStore(Path(d) / "feedback.db")
            token = s.feedback_item(1, "personal", "one", "two")
            for note in ("", "x" * 1001, "sk-" + "x" * 30):
                with self.assertRaises(ValueError):
                    s.note_feedback(1, token, note)
            task = correction_task("Проверь новость", "Было", "Неверная дата")
            for part in ("Проверь новость", "Было", "Неверная дата"):
                self.assertIn(part, task)
            with self.assertRaises(ValueError):
                correction_task("one", "x" * 10000, "two")

    def test_card_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            s = StudioStore(Path(d) / "feedback.db")
            card = s.news_card(1, 123, {"post": "Карточка"})
            token = s.feedback_item(1, "news", "Готовый Telegram-пост", "Карточка", card)
            s.note_feedback(1, token, "Исправь заголовок")
            self.assertEqual(s.get_feedback(1, token)["card_token"], card)
            self.assertEqual(s.get_news_card(1, card)[1]["post"], "Карточка")

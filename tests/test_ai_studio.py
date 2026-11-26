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
        self.assertEqual(len(r.message.handlers), 4)
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

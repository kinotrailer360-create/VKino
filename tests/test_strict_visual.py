import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from tempfile import TemporaryDirectory

from PIL import Image

from shorts import ai, post_visual, visual_guard
from shorts.ai_studio import deliver_news_card


class StrictVisualTests(unittest.IsolatedAsyncioTestCase):
    async def test_unverified_and_wrong_photos_not_attached(self):
        with TemporaryDirectory() as directory:
            work = Path(directory)
            paths = [work / f"{n}.jpg" for n in range(5)]
            for n, p in enumerate(paths):
                p.write_bytes(bytes([n]))
            notes = []
            verdicts = [(True, "yes"), (False, "wrong"), (None, "ИИ не уверен"), (True, "yes"), (False, "wrong")]
            with patch.object(post_visual, "review_asset", AsyncMock(side_effect=verdicts)) as check:
                self.assertEqual(await post_visual.review_files(paths, "news", work, "key", notes), [paths[0], paths[3]])
                self.assertEqual(check.await_count, 5)
            self.assertIn("не проверен, исключён", notes[2])

    async def test_provider_outage_never_passes_remaining_photos(self):
        with TemporaryDirectory() as directory:
            work = Path(directory)
            paths = [work / f"{n}.jpg" for n in range(4)]
            for n, p in enumerate(paths):
                p.write_bytes(bytes([n]))
            with patch.object(post_visual, "review_asset", AsyncMock(return_value=(None, "Проверка ИИ недоступна"))) as check:
                self.assertEqual(await post_visual.review_files(paths, "news", work, "key"), [])
                self.assertEqual(check.await_count, 1)

    async def test_duplicate_bytes_only_checked_once(self):
        with TemporaryDirectory() as directory:
            work = Path(directory)
            paths = [work / "a.jpg", work / "b.jpg"]
            for p in paths:
                p.write_bytes(b"same")
            with patch.object(post_visual, "review_asset", AsyncMock(return_value=(True, "yes"))) as check:
                self.assertEqual(await post_visual.review_files(paths, "news", work, "key"), paths[:1])
                self.assertEqual(check.await_count, 1)

    def test_video_samples_cover_full_duration(self):
        self.assertEqual(post_visual.sample_times(10), (1, 5, 9))
        with self.assertRaises(ValueError):
            post_visual.sample_times(float("nan"))

    async def test_three_frame_contract_and_no_paid(self):
        images = [Image.new("RGB", (40, 40)) for _ in range(3)]
        try:
            rows = [{"n": n, "fits": True, "reason": "yes"} for n in range(3)]
            with patch.object(ai, "_with_reserve", AsyncMock(return_value={"checks": rows})) as call:
                self.assertIs((await ai.media_sequence_fits("key", images, "news"))[0], True)
                self.assertTrue(call.await_args.kwargs["vision_only"])
                self.assertFalse(call.await_args.kwargs["allow_paid"])
            rows[2]["fits"] = False
            with patch.object(ai, "_with_reserve", AsyncMock(return_value={"checks": rows})):
                self.assertIs((await ai.media_sequence_fits("key", images, "news"))[0], False)
            rows[2]["n"] = 1
            with patch.object(ai, "_with_reserve", AsyncMock(return_value={"checks": rows})):
                self.assertIsNone((await ai.media_sequence_fits("key", images, "news"))[0])
        finally:
            for image in images:
                image.close()

    async def test_semantic_selection_is_not_final_visual_approval(self):
        with TemporaryDirectory() as directory:
            work = Path(directory)
            Image.new("RGB", (40, 40)).save(work / "photo.jpg")
            scene = {"image": "photo.jpg", "text": "news", "start": 0, "end": 3, "match_source": "semantic"}
            with patch.object(ai, "media_fits", AsyncMock(return_value=(None, "Проверка ИИ недоступна"))) as check:
                await visual_guard.audit(work, [scene], strict=False)
                check.assert_awaited_once()
            self.assertEqual(json.loads((work / "visual_review.json").read_text())[0]["status"], "unverified")

    async def test_unknown_vision_models_not_called(self):
        im = Image.new("RGB", (40, 40))
        try:
            with patch.object(ai, "RESERVE_KEY", "test"), patch.object(ai, "BEAT_KEY", "test"), \
                    patch.object(ai, "RESERVE_MODEL", "openai/gpt-oss-120b"), \
                    patch.object(ai, "RESERVE_URL", "https://api.groq.com/openai/v1/chat/completions"), \
                    patch.object(ai, "studio_vision_available", return_value=False), \
                    patch.object(ai, "_ask", AsyncMock(side_effect=ai.AIError("429"))), \
                    patch.object(ai, "_ask_reserve", AsyncMock()) as reserve, \
                    patch.object(ai, "_ask_beat", AsyncMock()) as beat:
                self.assertFalse(ai.vision_reserve_available())
                with self.assertRaises(ai.AllBusy):
                    await ai._with_reserve("key", [{"text": "check"}, ai._thumb(im)], "check", im, vision_only=True)
                reserve.assert_not_awaited()
                beat.assert_not_awaited()
        finally:
            im.close()

    def test_documented_groq_vision(self):
        with patch.object(ai, "RESERVE_KEY", "test"), patch.object(ai, "RESERVE_MODEL", "qwen/qwen3.8-27b"), \
                patch.object(ai, "RESERVE_URL", "https://api.groq.com/openai/v1/chat/completions"):
            self.assertTrue(ai.vision_reserve_available())

    async def test_studio_photo_button_is_not_publication(self):
        card = {"post": "Text", "date": "2026-11-26", "status": "СМИ", "sources": [{"url": "https://example.com/a", "title": "Article"}]}
        preview = AsyncMock(return_value=MagicMock(message_id=123))
        send = AsyncMock()
        store = MagicMock()
        store.news_card.return_value = "1" * 16
        store.feedback_item.return_value = "2" * 16
        await deliver_news_card(send, preview, store, 1, card, allow_photo=True)
        callbacks = [b.callback_data for row in send.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertIn("ais:nphoto:" + "1" * 16, callbacks)
        self.assertIn("ais:npub:" + "1" * 16, callbacks)


    async def test_openwebui_is_final_visual_reserve(self):
        im = Image.new("RGB", (40, 40))
        try:
            with patch.object(ai, "RESERVE_KEY", ""), patch.object(ai, "BEAT_KEY", "test"), \
                    patch.object(ai, "studio_vision_available", return_value=True), \
                    patch.object(ai, "cooling", return_value=None), \
                    patch.object(ai, "_ask", AsyncMock(side_effect=ai.AIError("429"))), \
                    patch.object(ai, "_ask_studio_vision", AsyncMock(return_value={"fits": True})) as reserve, \
                    patch.object(ai, "_ask_beat", AsyncMock()) as beat:
                self.assertEqual(await ai._with_reserve("key", [{"text": "check"}, ai._thumb(im)], "check", im,
                                                       vision_only=True), {"fits": True})
                reserve.assert_awaited_once()
                beat.assert_not_awaited()
        finally:
            im.close()

    async def test_studio_vision_routes_only_to_multimodal_alias(self):
        from shorts.ai_studio import StudioClient
        with TemporaryDirectory() as directory:
            client = StudioClient(Path(directory))
            content = [{"type": "text", "text": "check"}, {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,abc"}}]
            with patch.object(client, "_request", AsyncMock(return_value='{"fits":true}')) as request:
                self.assertEqual(await client.vision(content), ('{"fits":true}', "Gemini"))
                request.assert_awaited_once_with("models/gemini-flash-lite-latest", [{"role": "user", "content": content}], "vision")

import unittest
from unittest.mock import AsyncMock, patch

from shorts.hype import original_material


def group():
    return {"posts": [{"url": "https://deadline.com/article", "title": "New film announced",
                       "text": "The original source reports a new film project.",
                       "image": "https://deadline.com/image.jpg", "channel": "Deadline"},
                      {"url": "https://example.com/post", "title": "Other source",
                       "image": "https://example.com/unrelated.jpg"}]}


class RawNewsTests(unittest.IsolatedAsyncioTestCase):
    def test_source_and_photo_from_same_record(self):
        item = original_material(group())
        self.assertEqual(item["url"], "https://deadline.com/article")
        self.assertEqual(item["image"], "https://deadline.com/image.jpg")
        self.assertEqual(item["title"], "New film announced")
        self.assertLessEqual(len((item["title"] + " " + item["excerpt"]).split()), 25)

    def test_invalid_source_not_used(self):
        self.assertIsNone(original_material({"posts": [{"url": "file:///etc/passwd"}]}))

    async def test_no_send_for_other_chat(self):
        import bot
        mock = AsyncMock()
        with patch.object(bot, "ADMIN_IDS", {1}), patch.dict("os.environ", {"OPEN_WEBUI_TELEGRAM_OWNER_ID": ""}):
            self.assertFalse(await bot.hype_raw_material(mock, group(), 2))
        mock.send_message.assert_not_awaited()

    async def test_raw_text_warning_and_dedup_without_publication(self):
        import bot
        g = group()
        g["posts"][0]["image"] = ""
        mock = AsyncMock()
        with patch.object(bot, "ADMIN_IDS", {1}), patch.dict("os.environ", {"OPEN_WEBUI_TELEGRAM_OWNER_ID": ""}), \
                patch.object(bot, "offer_post", AsyncMock()) as offer:
            self.assertTrue(await bot.hype_raw_material(mock, g, 1))
            self.assertTrue(await bot.hype_raw_material(mock, g, 1))
            offer.assert_not_awaited()
        mock.send_message.assert_awaited_once()
        self.assertIn("НЕ ПРОВЕРЕНО ИИ", mock.send_message.await_args.args[1])
        self.assertIn("https://deadline.com/article", mock.send_message.await_args.args[1])
        self.assertNotIn("reply_markup", mock.send_message.await_args.kwargs)

    async def test_studio_failure_delivers_raw_packet(self):
        import tempfile
        import bot
        from shorts.ai_studio import StudioClient, StudioError
        with tempfile.TemporaryDirectory() as d, \
                patch.object(bot, "ADMIN_IDS", {1}), \
                patch.dict("os.environ", {"OPEN_WEBUI_TELEGRAM_OWNER_ID": "", "VKINO_DATA": d}), \
                patch("shorts.ai_studio.read_key", return_value="test"), \
                patch.object(StudioClient, "discover", AsyncMock(side_effect=StudioError("429", True))), \
                patch.object(bot, "hype_raw_material", AsyncMock(return_value=True)) as raw:
            g = group()
            self.assertFalse(await bot.hype_studio_draft(AsyncMock(), g, 1))
            raw.assert_awaited_once()
            self.assertNotIn("studio_draft_ready", g)

import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from shorts import hype
from shorts.ai_studio import deliver_news_card


def post(url, text, **extra):
    return {"url": url, "text": text, "title": text, "time": time.time() - 300, "lang": "en", **extra}


def group(posts, franchise="Marvel"):
    return {"posts": posts, "franchise": franchise, "tokens": ["marvel", "trailer", "news"], "sources": len(posts), "score": len(posts)}


class HypeEditorialTests(unittest.TestCase):
    def test_tracking_and_x_alias(self):
        self.assertEqual(hype.canonical_link("https://www.deadline.com/a/?utm_source=x&film=1"),
                         hype.canonical_link("https://deadline.com/a?film=1"))
        self.assertNotEqual(hype.canonical_link("https://deadline.com/a?film=2"), hype.canonical_link("https://deadline.com/a?film=1"))
        self.assertEqual(hype.canonical_link("https://twitter.com/foo/status/123?s=20"),
                         hype.canonical_link("https://x.com/bar/status/123"))

    def test_syndication(self):
        url = "https://deadline.com/news/a"
        p = [post("https://t.me/a/1", "Marvel trailer", article_url=url),
             post("https://t.me/b/2", "Marvel trailer", article_url=url + "?utm_campaign=x")]
        self.assertEqual(hype.source_key(p[0]), hype.source_key(p[1]))
        self.assertEqual(hype.editorial_rank(group(p))["independent"], 1)

    def test_franchise_and_priority(self):
        self.assertEqual(hype.franchise("The Hunger Games: Sunrise on the Reaping"), "Голодные игры")
        trade = group([post("https://deadline.com/article", "Marvel trailer first look")])
        rumor = dict(group([post("https://x.com/danielrpk/status/4", "Marvel trailer drops tomorrow", channel="X @danielrpk")]), single_insider=True)
        self.assertGreater(hype.editorial_rank(trade)["score"], hype.editorial_rank(rumor)["score"])
        self.assertTrue(hype.draft_eligible(trade))
        self.assertFalse(hype.draft_eligible(rumor))
        self.assertEqual(hype.prioritize([rumor, trade])[0]["posts"], trade["posts"])

    def test_single_trade_story(self):
        trade = group([post("https://deadline.com/article", "Marvel trailer first look")])
        self.assertIn(trade, hype.hot([trade]))

    def test_exact_article_match(self):
        g = group([post("https://deadline.com/article", "Marvel trailer first look")])
        self.assertTrue(hype.monitor_match({"sources": [{"url": "https://deadline.com/article?utm_source=x"}]}, g))
        self.assertFalse(hype.monitor_match({"sources": [{"url": "https://deadline.com/other"}]}, g))


class HypeDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_delivery_is_draft_only(self):
        card = {"post": "Текст", "date": "2026-11-26", "status": "СМИ", "sources": [{"url": "https://deadline.com/article", "title": "Article"}]}
        preview = AsyncMock(return_value=MagicMock(message_id=123))
        send = AsyncMock()
        store = MagicMock()
        store.news_card.return_value = "1" * 16
        store.feedback_item.return_value = "2" * 16
        result = await deliver_news_card(send, preview, store, 1, card)
        self.assertEqual(result, "1" * 16)
        preview.assert_awaited_once_with("Текст")
        store.news_card.assert_called_once_with(1, 123, card)
        buttons = send.await_args.kwargs["reply_markup"].inline_keyboard
        callbacks = [b.callback_data for row in buttons for b in row]
        self.assertIn("ais:npub:" + result, callbacks)
        self.assertFalse(any(c.startswith("pt:p:") for c in callbacks))

    async def test_draft_owner_and_busy(self):
        import bot
        with patch.object(bot, "ADMIN_IDS", {1}), patch.dict("os.environ", {"OPEN_WEBUI_TELEGRAM_OWNER_ID": ""}):
            self.assertFalse(await bot.hype_studio_draft(AsyncMock(), group([]), 2))

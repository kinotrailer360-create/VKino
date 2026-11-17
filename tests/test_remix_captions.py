import tempfile
import unittest
from pathlib import Path

from shorts import remix_captions as captions


class RemixCaptionTests(unittest.TestCase):
    def test_captions_follow_words_and_clear_during_pause(self):
        events = captions.caption_events([
            {"w": "Очень", "s": 0.1, "e": 0.3},
            {"w": "красивые", "s": 0.31, "e": 0.65},
            {"w": "виды.", "s": 0.7, "e": 0.95},
            {"w": "Финал", "s": 3.0, "e": 3.4},
        ], 4.0)
        self.assertEqual([e["text"] for e in events], ["Очень красивые", "виды.", "Финал"])
        self.assertLess(events[1]["end"], 1.2)
        self.assertEqual(events[2]["start"], 3.0)
        self.assertTrue(all(len(e["text"].split()) <= 2 for e in events))

    def test_slow_or_invalid_word_cannot_freeze_caption(self):
        events = captions.caption_events([
            {"w": "Слово", "s": 1, "e": 15},
            {"w": "ошибка", "s": float("nan"), "e": 20},
        ], 20)
        self.assertEqual(len(events), 1)
        self.assertLessEqual(events[0]["end"] - events[0]["start"], 1.200001)

    def test_fallback_keeps_original_phrase_timing(self):
        words = captions.estimated_words([
            {"text": "Первое слово", "start": 1, "end": 2},
            {"text": "Другая фраза", "start": 6, "end": 7},
        ], 9)
        self.assertEqual([w["w"] for w in words], ["Первое", "слово", "Другая", "фраза"])
        self.assertEqual(words[0]["s"], 1)
        self.assertEqual(words[2]["s"], 6)
        self.assertEqual(words[-1]["e"], 7)

    def test_ass_keeps_ribbon_separate_from_two_word_captions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "captions.ass"
            captions.write_ass(path, [{"text": "Очень красиво", "start": 0, "end": 0.7}],
                               [{"headline": "КРАСОТА С ПЕРВЫХ СЕКУНД", "phrase_start": 0, "phrase_end": 4}])
            content = path.read_text()
            self.assertIn("ОЧЕНЬ КРАСИВО", content)
            self.assertIn("КРАСОТА С ПЕРВЫХ СЕКУНД", content)
            self.assertIn("0:00:00.70", content)
            self.assertIn("Style: Words", content)


if __name__ == "__main__":
    unittest.main()

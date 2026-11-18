"""Guard rails for Studio trailer discovery."""
import unittest
from unittest.mock import patch

from shorts.studio_adapter import trailer_candidate, trailer_titles


class TrailerTitlesTests(unittest.TestCase):
    def test_explicit_movie_title(self):
        self.assertEqual(
            trailer_titles("Канг из «Человека-муравья и Осы: Квантомания» рядом с Думом"),
            ["Человека-муравья и Осы: Квантомания"],
        )

    def test_character_name_is_not_assumed_to_be_movie_title(self):
        self.assertEqual(trailer_titles("Доктор Дум заменил Канга"), [])

    def test_explicit_project_title_fallback(self):
        self.assertEqual(trailer_titles("Драка героев", "Фильм: Мстители: Судный день"),
                         ["Мстители: Судный день"])


class CandidateSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_explicit_title_never_searches_trailer(self):
        with patch("shorts.studio_adapter.clips.rutube_trailer") as search:
            result = await trailer_candidate(None, 1, {"text": "Доктор Дум заменил Канга"},
                                             "", None, "unused")
        self.assertIsNone(result)
        search.assert_not_called()


if __name__ == "__main__":
    unittest.main()

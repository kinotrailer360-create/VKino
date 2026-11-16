"""Regression checks for public Instagram metadata parsing."""
import unittest
from shorts.linkfetch import INSTAGRAM, _Article, _direct_video


class InstagramImportTests(unittest.TestCase):
    def test_supported_public_urls(self):
        for url in (
            "https://www.instagram.com/reel/Cx_123-Ab/",
            "https://instagram.com/p/Cx_123-Ab/?igsh=test",
            "https://www.instagram.com/tv/ABC123/",
        ):
            self.assertIsNotNone(INSTAGRAM.fullmatch(url))
        for url in (
            "http://127.0.0.1/p/abc",
            "https://instagram.com/accounts/login/",
            "https://instagram.com.evil.example/p/abc",
        ):
            self.assertIsNone(INSTAGRAM.fullmatch(url))

    def test_metadata_and_image(self):
        parser = _Article()
        parser.feed('<meta property="og:title" content="Films">'
                    '<meta property="og:description" content="Three films">'
                    '<meta property="og:image" content="https://example.org/post.jpg">')
        self.assertEqual(parser.meta["og:description"], "Three films")
        self.assertEqual(parser.images, ["https://example.org/post.jpg"])

    def test_embedded_video_is_not_downloadable(self):
        self.assertIsNone(_direct_video("https://www.instagram.com/reel/ABC123/"))
        self.assertEqual(_direct_video("https://cdn.example.org/clip.mp4"),
                         "https://cdn.example.org/clip.mp4")


if __name__ == "__main__":
    unittest.main()

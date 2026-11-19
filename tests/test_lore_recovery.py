"""Regression tests for replacing missing Lore images without guessing."""
import asyncio
import io
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from PIL import Image

from shorts import lore_recovery as recovery


def sample_bytes(color):
    stream = io.BytesIO()
    Image.new("RGB", (680, 380), color).save(stream, "JPEG")
    return stream.getvalue()


def test_phrase_queries_include_subject_and_limit_length():
    found = recovery.queries("Доктор Дум появляется перед героями Marvel", "Мстители: Судный день")
    assert len(found) == 2
    assert all("Дум" in value for value in found)
    assert "Мстители" in found[0]


def test_same_visual_content_has_same_fingerprint():
    image = Image.new("RGB", (800, 500), (10, 80, 120))
    assert recovery.fingerprint(image) == recovery.fingerprint(image.copy())


def test_url_404_and_wrong_visual_are_skipped_without_guessing():
    async def scenario():
        urls = ["https://example.com/404.jpg", "https://example.com/wrong.jpg",
                "https://example.com/correct.jpg"]

        @asynccontextmanager
        async def session(**kwargs):
            yield object()

        async def get(session_arg, url, size, accept):
            if "404" in url:
                raise OSError("HTTP 404")
            color = (200, 10, 10) if "wrong" in url else (10, 80, 120)
            return sample_bytes(color), "image/jpeg"

        async def fit(key, image, phrase):
            return (image.getpixel((0, 0))[0] < 50), "Checked"

        with patch.object(recovery.ai, "available", return_value=True), patch.object(
            recovery.imgsearch, "search",
            new=AsyncMock(return_value=[{"image": u} for u in urls])
        ), patch.object(recovery.netsafe, "safe_session", session), patch.object(
            recovery.netsafe, "_get", new=get
        ), patch.object(recovery.ai, "media_fits", new=fit):
            recovered = await recovery.recover("Доктор Дум", "Мстители", "key", set())

        assert recovered is not None
        image, url = recovered
        assert url.endswith("/correct.jpg")
        image.close()
    asyncio.run(scenario())


def test_unknown_model_verdict_does_not_approve_any_image():
    async def scenario():
        @asynccontextmanager
        async def session(**kwargs):
            yield object()
        async def fetch(*args):
            return sample_bytes((100, 100, 100)), "image/jpeg"
        with patch.object(recovery.ai, "available", return_value=True), patch.object(
            recovery.imgsearch, "search",
            new=AsyncMock(return_value=[{"image": "https://example.com/a.jpg"}])
        ), patch.object(recovery.netsafe, "safe_session", session), patch.object(
            recovery.netsafe, "_get", new=fetch
        ), patch.object(recovery.ai, "media_fits", new=AsyncMock(return_value=(None, "429"))):
            assert await recovery.recover("Дум", "Мстители", "key") is None
    asyncio.run(scenario())


def test_no_model_does_not_search():
    async def scenario():
        with patch.object(recovery.ai, "available", return_value=False), patch.object(
            recovery.imgsearch, "search", new=AsyncMock()
        ) as search:
            assert await recovery.recover("Дум", "Мстители", "") is None
            search.assert_not_awaited()
    asyncio.run(scenario())

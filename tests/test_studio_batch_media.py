"""Regression checks for Studio batch-media integration."""
from shorts.studio import MIMES, MAX_FILE, Studio
from shorts import studio_adapter


def test_new_media_formats_registered():
    for mime in ("image/gif", "image/bmp", "image/tiff", "video/x-matroska", "video/x-msvideo", "video/x-m4v"):
        assert mime in MIMES
    assert MAX_FILE == 50 * 1024 * 1024


def test_batch_endpoint_and_old_endpoint_both_available():
    # Both endpoints must remain registered; existing clients use /files.
    import inspect
    source = inspect.getsource(Studio.register)
    assert '"/files/batch"' in source
    assert '"/files"' in source


def test_trailer_candidate_preserved_through_merge():
    # Regression: animated GIF additions may not erase restored trailer lookup.
    assert callable(studio_adapter.trailer_candidate)
    assert studio_adapter.trailer_titles("Канг из «Человека-муравья и Осы: Квантомания»")


def test_animated_gif_render_loop_exists():
    import inspect
    source = inspect.getsource(studio_adapter.render_scene)
    assert 'image/gif' in source
    assert '-stream_loop' in source


def test_actual_batch_handler_accepts_multiple_files_and_reports_errors(tmp_path):
    import asyncio
    import json
    from unittest.mock import AsyncMock
    from shorts.studio import Studio

    class Part:
        def __init__(self, name, mime, content):
            self.filename = name
            self.headers = {"Content-Type": mime}
            self.chunks = [content]
        async def read_chunk(self, size):
            return self.chunks.pop(0) if self.chunks else b""

    class Reader:
        def __init__(self):
            self.parts = [
                Part("a.jpg", "image/jpeg", b"a"),
                Part("b.mp4", "video/mp4", b"b"),
                Part("bad.exe", "application/octet-stream", b"x"),
            ]
        async def next(self):
            return self.parts.pop(0) if self.parts else None

    class Request:
        content_type = "multipart/form-data"
        async def multipart(self):
            return Reader()

    async def scenario():
        studio = Studio(None, {1}, tmp_path, None, None)
        studio.authenticate = AsyncMock(return_value=1)
        async def stored(admin, path, mime, name=""):
            assert admin == 1 and path.stat().st_size == 1
            return {"id": name, "mime": mime, "name": name, "size": 1}
        studio.store_file = stored
        result = json.loads((await studio.upload_batch(Request())).text)
        assert result["count"] == 3
        assert [f["name"] for f in result["files"]] == ["a.jpg", "b.mp4"]
        assert result["errors"][0]["name"] == "bad.exe"
    asyncio.run(scenario())

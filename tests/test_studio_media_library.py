"""Tests for safe project-scoped media selection; no paid AI or network."""
import asyncio
import shutil
import subprocess
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from shorts.studio import validate_job
from shorts import studio_media_library as library


def valid_project():
    return {"id": str(uuid4()), "revision": 1, "stage": 2,
            "channel": "youtube", "title": "KinoMir360",
            "mediaLibrary": [str(uuid4()), str(uuid4())],
            "sourceUrls": ["https://www.youtube.com/watch?v=abc123"],
            "scenes": [{"text": "Доктор Дум и Канг", "duration": 6,
                        "start": 0, "end": 6}]}


def test_approved_platform_allowlist_rejects_private_urls():
    assert library.allowed_source("https://www.youtube.com/watch?v=abc123")
    assert library.allowed_source("https://rutube.ru/video/123/")
    assert library.allowed_source("https://vkvideo.ru/video-123")
    assert not library.allowed_source("http://youtube.com/watch?v=1")
    assert not library.allowed_source("https://youtube.com.evil.example/1")
    assert not library.allowed_source("https://127.0.0.1/private")
    assert not library.allowed_source("file:///etc/passwd")
    assert not library.allowed_source("https://user:pass@youtube.com/1")


def test_validate_project_library_and_urls():
    project = valid_project()
    payload = {"requestId": str(uuid4()), "operation": "media",
               "sceneIndex": 0, "project": project}
    assert validate_job(payload)["operation"] == "media"
    project["mediaLibrary"] = ["../../secret"]
    with pytest.raises(ValueError):
        validate_job(payload)
    project["mediaLibrary"] = [str(uuid4())] * 21
    with pytest.raises(ValueError):
        validate_job(payload)
    project["mediaLibrary"] = []
    project["sourceUrls"] = ["https://localhost/private"]
    with pytest.raises(ValueError):
        validate_job(payload)


def test_required_length_avoids_unrenderable_four_second_trailer():
    assert library.required_length({"duration": 9}) == 12
    assert library.required_length({"duration": 30}) == 30
    with pytest.raises(ValueError):
        library.required_length({"duration": 0})


def test_selects_second_matching_uploaded_video_not_first(tmp_path):
    async def scenario():
        first = str(uuid4())
        second = str(uuid4())
        files = {
            first: (tmp_path / "one.mp4", {"mime": "video/mp4", "name": "first"}),
            second: (tmp_path / "two.mov", {"mime": "video/quicktime", "name": "second"}),
        }

        class Bridge:
            async def resolve_file(self, admin, identifier):
                assert admin == 7
                return files[identifier]

        project = {"mediaLibrary": [first, second], "sourceUrls": []}
        scene = {"text": "Канг из фильма", "duration": 6}
        with patch.object(library.ai, "available", return_value=True), patch.object(
            library, "choose_video", new=AsyncMock(side_effect=[None, (12.0, 21.0)])
        ) as choose:
            result = await library.select(Bridge(), 7, scene, project, tmp_path, "key")
        assert choose.await_count == 2
        assert result["asset"]["id"] == second
        assert result["start"] == 12.0
        assert result["end"] == 21.0
        assert result["rights"] == "author_uploaded"
        assert "подтвердите" in result["note"]
    asyncio.run(scenario())


def test_no_library_is_safe_noop(tmp_path):
    result = asyncio.run(library.select(None, 1, {"duration": 6},
                                         {"mediaLibrary": [], "sourceUrls": []},
                                         tmp_path, ""))
    assert result is None


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                    reason="ffmpeg required")
def test_real_ffmpeg_video_sampling_and_cutting(tmp_path):
    """Exercise actual video files and FFmpeg, with model judgment mocked."""
    from shorts import clips
    source = tmp_path / "input.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "testsrc2=size=320x180:rate=15:duration=12",
         "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", str(source)],
        check=True, timeout=30)
    async def scenario():
        async def always_fit(path, text, key):
            return True
        with patch.object(library.ai, "available", return_value=True), patch.object(
            library, "matches", new=always_fit
        ):
            window = await library.choose_video(
                source, {"text": "Тест", "duration": 3},
                tmp_path / "analysis", "key")
        assert window is not None
        begin, end = window
        assert 0 <= begin < end <= 12
        assert end - begin == pytest.approx(6, abs=.1)
        result = tmp_path / "cut.mp4"
        await clips.cut_clip(source, result, begin, end - begin)
        assert result.is_file() and result.stat().st_size > 1000
        assert 4 <= await library.video_duration(result) <= 7
    asyncio.run(scenario())

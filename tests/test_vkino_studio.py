"""Local-only renderer tests; no API/network or paid models."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest
from PIL import Image

from vkino_studio.model import load_project
from vkino_studio.renderer import render


def _fixture(tmp_path: Path) -> Path:
    assets = tmp_path / "media"
    assets.mkdir()
    Image.new("RGB", (640, 360), (23, 45, 67)).save(assets / "image.jpg")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=1.5", "-ar", "48000",
                    str(assets / "voice.wav")], check=True)
    manifest = tmp_path / "project.json"
    manifest.write_text(json.dumps({"version": 1, "title": "Тест кино", "width": 360,
                  "height": 640, "fps": 25, "captions": True,
                  "scenes": [{"source": "media/image.jpg", "voice": "media/voice.wav",
                              "text": "Новости кино сегодня", "headline": "НОВОСТИ",
                              "duration": 2.0, "approved": True, "rights": "author"}]}, ensure_ascii=False),
                        encoding="utf-8")
    return manifest


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg missing")
def test_approved_local_scene_renders_video_and_report(tmp_path):
    manifest = _fixture(tmp_path)
    messages = []
    result = render(manifest, tmp_path / "output", callback=messages.append)
    assert result.is_file() and result.stat().st_size > 1000
    probe = subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(result)])
    streams = json.loads(probe)["streams"]
    assert any(s["codec_type"] == "video" and s["width"] == 360 and s["height"] == 640 for s in streams)
    assert any(s["codec_type"] == "audio" for s in streams)
    report = json.loads((tmp_path / "output" / "render_report.json").read_text(encoding="utf-8"))
    assert report["published"] is False
    assert report["author_final_approval"] is False
    assert report["captions_timing"] == "estimated"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg missing")
def test_unapproved_scene_rejected(tmp_path):
    manifest = _fixture(tmp_path)
    data = json.loads(manifest.read_text())
    data["scenes"][0]["approved"] = False
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="утверждена"):
        load_project(manifest)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg missing")
def test_parent_and_absolute_paths_rejected(tmp_path):
    manifest = _fixture(tmp_path)
    for path in ["../outside.jpg", "C:\\secret\\media.jpg", "/etc/passwd"]:
        data = json.loads(manifest.read_text())
        data["scenes"][0]["source"] = path
        manifest.write_text(json.dumps(data))
        with pytest.raises(ValueError):
            load_project(manifest)
        data["scenes"][0]["source"] = "media/image.jpg"
        manifest.write_text(json.dumps(data))


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg missing")
def test_voice_longer_than_scene_is_rejected(tmp_path):
    manifest = _fixture(tmp_path)
    data = json.loads(manifest.read_text())
    data["scenes"][0]["duration"] = 1.0
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="озвучка"):
        render(manifest, tmp_path / "output")

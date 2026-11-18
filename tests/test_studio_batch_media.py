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

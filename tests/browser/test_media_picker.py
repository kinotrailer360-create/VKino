"""Smoke test for the real Studio widget with an isolated browser and stubbed API."""
import os
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")


def test_media_picker_uploads_multiple_files_and_updates_selection():
    widget = Path(__file__).resolve().parents[2] / "shorts" / "studio_media_picker.js"
    origin = "https://studio.example.test"
    files = [{"id": "existing", "name": "existing.gif", "size": 120}]
    uploaded = [
        {"id": "photo", "name": "sample.png", "size": 100},
        {"id": "clip", "name": "sample.mp4", "size": 200},
    ]

    with playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE"))
        page = browser.new_page()
        page.route(origin + "/", lambda route: route.fulfill(
            content_type="text/html", body='<main id="media"></main>',
        ))
        page.route(origin + "/api/files", lambda route: route.fulfill(json={"files": files}))

        def upload(route):
            request = route.request
            assert request.method == "POST"
            assert request.headers["authorization"] == "Bearer smoke-test-token"
            assert 'filename="sample.png"' in request.post_data
            assert 'filename="sample.mp4"' in request.post_data
            files.extend(uploaded)
            route.fulfill(json={"files": uploaded, "errors": []})

        page.route(origin + "/api/files/batch", upload)
        page.goto(origin + "/")
        page.add_script_tag(path=widget)
        page.evaluate("""() => {
            window.selectedIds = [];
            VKinoMediaPicker.mount({
                element: document.querySelector('#media'),
                apiBase: location.origin + '/api',
                token: 'smoke-test-token',
                onChange: ids => { window.selectedIds = ids; }
            });
        }""")

        playwright_api.expect(page.get_by_label("existing.gif", exact=False)).not_to_be_checked()
        page.locator("input[type=file]").set_input_files([
            {"name": "sample.png", "mimeType": "image/png", "buffer": b"photo"},
            {"name": "sample.mp4", "mimeType": "video/mp4", "buffer": b"video"},
        ])
        page.get_by_role("button", name="Загрузить выбранные файлы").click()
        playwright_api.expect(page.get_by_role("status")).to_have_text("Загружено: 2. Ошибок: 0")
        playwright_api.expect(page.get_by_label("sample.mp4", exact=False)).to_be_checked()
        assert page.evaluate("window.selectedIds") == ["photo", "clip"]
        page.get_by_label("sample.mp4", exact=False).uncheck()
        assert page.evaluate("window.selectedIds") == ["photo"]

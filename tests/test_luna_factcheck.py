import asyncio
import json

from shorts import ai, luna


def test_evidence_must_come_from_correct_source_and_claim():
    quote = "The actor is in talks to star."
    rows = [
        {"n": 2, "status": "confirmed", "source": 2, "quote": quote, "note": ""},
        {"n": 1, "status": "conflict", "source": 1, "quote": quote, "note": "Идут переговоры."},
        {"n": 4, "status": "confirmed", "source": 1, "quote": quote, "note": ""},
        {"n": 4, "status": "confirmed", "source": 1, "quote": quote, "note": ""},
    ]
    facts = ["Актёр утверждён", "другой фильм", "пропущенный факт", "дубликат"]
    sources = [("https://t.me/example/12", "The actor is in talks\n to star."), ("другой источник", "Заметка.")]
    first, *others = luna.validate({"checks": rows}, facts, sources)
    assert first == {"status": "conflict", "source": "https://t.me/example/12",
                     "note": "Идут переговоры.", "evidence": quote}
    assert all(row["status"] == "unverified" and "evidence" not in row for row in others)


def test_active_check_uses_luna_without_legacy_credentials(monkeypatch):
    monkeypatch.setattr(ai, "LUMEAN_LLM_KEY", "test")
    monkeypatch.setattr(luna, "MODE", "active")
    monkeypatch.setattr(ai, "_cool", {})

    async def check(prompt, *, max_tokens, model_override):
        assert "Премьера в России в декабре" in prompt
        assert "В декабре состоится мировая премьера." in prompt
        assert model_override == "gpt-6-luna"
        return {"checks": [{"n": 1, "status": "unverified", "note": "Регион не подтверждён."}]}

    monkeypatch.setattr(ai, "_ask_lumean", check)
    result = asyncio.run(ai.check_facts("", ["Премьера в России в декабре"],
                                       {"издание": "В декабре состоится мировая премьера."}))
    assert result[0]["status"] == "unverified"
    assert result[0]["note"] == "Регион не подтверждён."


def test_active_outage_is_visible_and_skips_repeat(monkeypatch):
    monkeypatch.setattr(ai, "LUMEAN_LLM_KEY", "test")
    monkeypatch.setattr(luna, "MODE", "active")
    monkeypatch.setattr(ai, "_cool", {})
    attempts = []

    async def check(prompt, *, max_tokens, model_override):
        attempts.append(prompt)
        raise TimeoutError

    monkeypatch.setattr(ai, "_ask_lumean", check)
    for _ in range(2):
        [result] = asyncio.run(ai.check_facts("gemini", ["факт"], {"источник": "материал"}))
        assert result["status"] == "unverified"
        assert "временно недоступна" in result["note"]
    assert len(attempts) == 1


def test_shadow_compares_but_returns_current_verdict(monkeypatch, caplog):
    monkeypatch.setattr(ai, "LUMEAN_LLM_KEY", "test")
    monkeypatch.setattr(luna, "MODE", "shadow")
    monkeypatch.setattr(ai, "_cool", {})

    async def check(prompt, *, max_tokens, model_override):
        return {"checks": [{"n": 1, "status": "conflict", "source": 1, "quote": "цитата", "note": ""}]}

    async def current(*args):
        return {"checks": [{"n": 1, "status": "confirmed", "source": "издание", "note": ""}]}

    monkeypatch.setattr(ai, "_ask_lumean", check)
    monkeypatch.setattr(ai, "_with_reserve", current)
    with caplog.at_level("INFO"):
        [result] = asyncio.run(ai.check_facts("gemini", ["факт"], {"издание": "цитата"}))
    assert result["status"] == "confirmed"
    assert result["luna_status"] == "conflict" and result["luna_evidence"] == "цитата"
    assert "agreement=0 verifiable_quotes=1" in caplog.text


def test_luna_uses_existing_lumean_gateway_without_changing_author_model(monkeypatch):
    class Response:
        status = 200
        headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def json(self, content_type=None):
            text = json.dumps({"checks": [{"n": 1, "status": "single", "source": 1,
                                           "quote": "официальный текст", "note": ""}]})
            return {"usage": {"prompt_tokens": 20, "completion_tokens": 7},
                    "choices": [{"finish_reason": "stop", "message": {"content": text}}]}

    class Session(Response):
        def __init__(self, **kwargs):
            assert kwargs["timeout"].total == 40

        def post(self, url, *, json, headers):
            assert url == "https://api.lumean.app/v1/chat/completions"
            assert headers["Authorization"] == "Bearer test"
            assert json["model"] == "gpt-6-luna"
            assert "temperature" not in json
            return Response()

    monkeypatch.setattr(ai, "LUMEAN_LLM_KEY", "test")
    monkeypatch.setattr(ai, "LUMEAN_LLM_URL", "https://api.lumean.app/v1")
    monkeypatch.setattr(ai, "LUMEAN_LLM_MODEL", "claude-sonnet-5-5")
    monkeypatch.setattr(ai, "_count_lumean", lambda data: None)
    monkeypatch.setattr(ai.aiohttp, "ClientSession", Session)
    monkeypatch.setattr(luna, "MODE", "active")
    monkeypatch.setattr(ai, "_cool", {})
    [result] = asyncio.run(ai.check_facts("", ["факт"], {"студия": "официальный текст"}))
    assert result["evidence"] == "официальный текст"
    assert ai.LUMEAN_LLM_MODEL == "claude-sonnet-5-5"

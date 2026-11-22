"""No costly calls for routine scene checks or bot AI status displays."""
import asyncio

import pytest
from PIL import Image

from shorts import ai, budget


def test_ai_budget_has_small_configurable_default():
    assert budget.LIMITS.get("ai", 0) > 0


def test_media_fits_never_calls_paid_model_for_visual_verdict(monkeypatch):
    monkeypatch.setattr(ai, "LUMEAN_LLM_KEY", "lumean-secret")
    monkeypatch.setattr(ai, "LUMEAN_MODE", "all")
    monkeypatch.setattr(ai, "LLM_PRIMARY", "lumean")
    monkeypatch.setattr(ai, "RESERVE_KEY", "")
    monkeypatch.setattr(ai, "BEAT_KEY", "")

    async def cheap(key, parts):
        return {"fits": True, "seen": "Доктор Дум"}
    async def expensive(*args, **kwargs):
        raise AssertionError("Visual matching must not bill Lumean")

    monkeypatch.setattr(ai, "_ask", cheap)
    monkeypatch.setattr(ai, "_ask_lumean", expensive)

    fit, reason = asyncio.run(ai.media_fits(
        "gemini-key", Image.new("RGB", (500, 500)), "Доктор Дум в кадре"
    ))
    assert fit is True
    assert "Дум" in reason


def test_no_paid_fallback_if_free_visual_model_failed(monkeypatch):
    monkeypatch.setattr(ai, "LUMEAN_LLM_KEY", "lumean-secret")
    monkeypatch.setattr(ai, "LUMEAN_MODE", "important")
    monkeypatch.setattr(ai, "LLM_PRIMARY", "lumean")
    monkeypatch.setattr(ai, "RESERVE_KEY", "")
    monkeypatch.setattr(ai, "BEAT_KEY", "")

    async def fail_free(*args):
        raise RuntimeError("Gemini HTTP 429")
    async def expensive(*args, **kwargs):
        raise AssertionError("Do not buy visual confirmation after a free outage")

    monkeypatch.setattr(ai, "_ask", fail_free)
    monkeypatch.setattr(ai, "_ask_lumean", expensive)
    with pytest.raises(ai.AllBusy):
        asyncio.run(ai._with_reserve("gemini", [{"text": "test"}], "test",
                                     allow_paid=False))


def test_ai_status_never_pays_for_probes(monkeypatch):
    monkeypatch.setattr(ai, "LUMEAN_LLM_KEY", "secret")
    monkeypatch.setattr(ai, "LUMEAN_MODE", "important")
    monkeypatch.setattr(ai, "LLM_PRIMARY", "lumean")
    monkeypatch.setattr(ai, "RESERVE_KEY", "")
    monkeypatch.setattr(ai, "BEAT_KEY", "")
    monkeypatch.setattr(ai, "gemini_keys", lambda key: [])
    async def fail_paid(*args, **kwargs):
        raise AssertionError("/ai should be read-only for paid models")
    async def fake_search():
        return []
    monkeypatch.setattr(ai, "_ask_lumean", fail_paid)
    monkeypatch.setattr(ai.websearch, "probe", fake_search)

    rows = asyncio.run(ai.probe(""))
    assert any("платная проба не запускалась" in reason for _, _, reason in rows)


def test_truncated_premium_response_is_not_retried_automatically(monkeypatch):
    monkeypatch.setattr(ai, "LUMEAN_LLM_KEY", "test")
    monkeypatch.setattr(ai, "LUMEAN_MODE", "important")
    monkeypatch.setattr(ai, "LLM_PRIMARY", "lumean")
    monkeypatch.setattr(ai, "LUMEAN_ALLOW_PAID_RETRY", False)
    monkeypatch.setattr(ai, "RESERVE_KEY", "")
    monkeypatch.setattr(ai, "BEAT_KEY", "")
    calls = []

    async def truncated(*args, **kwargs):
        calls.append(kwargs.get("max_tokens", args[2] if len(args) > 2 else None))
        raise ai.OutputTruncated("paid output truncated")

    monkeypatch.setattr(ai, "_ask_lumean", truncated)
    with pytest.raises(ai.AllBusy):
        asyncio.run(ai._with_reserve("", [{"text": "script"}], "script", premium=True))
    assert len(calls) == 1

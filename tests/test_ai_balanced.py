"""Smart-balance routing must never buy trivial visual/text work."""
import asyncio

import pytest

from shorts import ai


def setup(monkeypatch):
    monkeypatch.setattr(ai, "LUMEAN_MODE", "balanced")
    monkeypatch.setattr(ai, "LLM_PRIMARY", "lumean")
    monkeypatch.setattr(ai, "LUMEAN_LLM_KEY", "paid-key")
    monkeypatch.setattr(ai, "RESERVE_KEY", "")
    monkeypatch.setattr(ai, "BEAT_KEY", "")
    monkeypatch.setattr(ai, "_cool", {})


def test_balanced_success_free_model_never_bills(monkeypatch):
    setup(monkeypatch)
    async def gemini(*args):
        return {"ok": True}
    async def paid(*args, **kwargs):
        raise AssertionError("Premium must not run when free Gemini succeeded")
    monkeypatch.setattr(ai, "_ask", gemini)
    monkeypatch.setattr(ai, "_ask_lumean", paid)
    assert asyncio.run(ai._with_reserve("test", [{"text": "script"}], "script", premium=True)) == {"ok": True}


def test_balanced_simple_tasks_never_pay_even_on_free_outage(monkeypatch):
    setup(monkeypatch)
    async def failed(*args):
        raise ai.AIError("Gemini quota")
    async def paid(*args, **kwargs):
        raise AssertionError("Non-premium should not buy a paid fallback")
    monkeypatch.setattr(ai, "_ask", failed)
    monkeypatch.setattr(ai, "_ask_lumean", paid)
    with pytest.raises(ai.AllBusy):
        asyncio.run(ai._with_reserve("test", [{"text": "caption"}], "caption"))


def test_balanced_tries_free_beat_before_paid_premium(monkeypatch):
    setup(monkeypatch)
    monkeypatch.setattr(ai, "BEAT_KEY", "beat")
    calls = []
    async def failed(*args):
        calls.append("gemini")
        raise ai.AIError("Gemini quota")
    async def beat(*args, **kwargs):
        calls.append("beat")
        return {"ok": True}
    async def paid(*args, **kwargs):
        raise AssertionError("Don't buy after free BeatAPI answered")
    monkeypatch.setattr(ai, "_ask", failed)
    monkeypatch.setattr(ai, "_ask_beat", beat)
    monkeypatch.setattr(ai, "_ask_lumean", paid)
    assert asyncio.run(ai._with_reserve("key", [{"text": "script"}], "script", premium=True)) == {"ok": True}
    assert calls == ["gemini", "beat"]


def test_balanced_premium_fallback_after_free_outages(monkeypatch):
    setup(monkeypatch)
    calls = []
    async def failed(*args):
        calls.append("gemini")
        raise ai.AIError("Gemini quota")
    async def paid(*args, **kwargs):
        calls.append("lumean")
        assert args[2] == 4000
        return {"ok": True}
    monkeypatch.setattr(ai, "_ask", failed)
    monkeypatch.setattr(ai, "_ask_lumean", paid)
    assert asyncio.run(ai._with_reserve("key", [{"text": "script"}], "script", premium=True)) == {"ok": True}
    assert calls == ["gemini", "lumean"]

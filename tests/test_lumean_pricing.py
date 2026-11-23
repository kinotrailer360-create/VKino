"""Lumean per-call price guard: never approve unknown or expensive paid calls."""
import asyncio
import shutil

import pytest

from shorts import lumean_pricing


def test_elevenlabs_template_updates_when_delivery_settings_change(monkeypatch):
    from shorts import lumean

    created = []

    async def call(session, method, route, **kwargs):
        if method == 'GET':
            return {'items': [{'name': 'KinoMir360 TTS', 'id': 'old-template'}]}
        created.append(kwargs['json'])
        return {'id': str(len(created))}

    monkeypatch.setattr(lumean, '_call', call)
    monkeypatch.setattr(lumean, '_templates', {})
    monkeypatch.setattr(lumean, 'SPEED', 1.15)
    assert asyncio.run(lumean._template_id(None, 'elevenlabs', 'voice')) == '1'
    assert asyncio.run(lumean._template_id(None, 'elevenlabs', 'voice')) == '1'
    monkeypatch.setattr(lumean, 'SPEED', 1.1)
    assert asyncio.run(lumean._template_id(None, 'elevenlabs', 'voice')) == '2'
    assert created[0]['name'] != created[1]['name']
    assert created[1]['config']['tts_settings']['voice_settings']['speed'] == 1.1


def test_narration_gaps_follow_punctuation():
    from shorts.daily import narration_gap

    assert narration_gap('Он открывает дверь') < narration_gap('и вдруг,') < narration_gap('Всё стихло.')
    assert narration_gap('«И кто там?»') > narration_gap('Всё стихло.')


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='FFmpeg is required')
def test_narration_uses_decoded_timing(tmp_path, monkeypatch):
    from shorts import daily

    async def fake_voice(speak, parts, work, pause):
        for part in parts:
            await daily._run('ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                             'sine=frequency=440:duration=0.5', str(part))

    monkeypatch.setattr(daily, '_lumean_cached', fake_voice)
    monkeypatch.setattr(daily.lumean, 'enabled', lambda: True)

    async def check():
        path, starts, _, spans = await daily.synthesize(['Он входит', 'Что он увидел?', 'Всё ясно.'],
                                                        tmp_path, prepared=True)
        duration = await daily._duration(path)
        assert starts == pytest.approx([0, 0.59, 1.35], abs=0.003)
        assert [entry['speech'] for entry in spans] == pytest.approx([0.5, 0.5, 0.5], abs=0.003)
        assert duration == pytest.approx(1.85, abs=0.003)

    asyncio.run(check())


def catalog(input_rate=2000000, output_rate=3000000, *, model="gpt-4.1-mini"):
    # Precision 6, so 2_000_000 minor / 10**6 = 2 LUMC / 1m input tokens.
    return {
        "data": {
            "asset": {"code": "LUMC", "precision": 6},
            "models": [{
                "key": model, "kind": "chat", "billing_available": True,
                "price": {
                    "input": {"per_mtok_minor": input_rate},
                    "output": {"per_mtok_minor": output_rate},
                },
            }],
        },
    }


def test_catalog_estimate_in_lumc_not_usd():
    cost = lumean_pricing.estimate_lumc(catalog(), 1000, 4000)
    assert cost == pytest.approx(0.0146)


@pytest.mark.parametrize("corruption", [
    {},
    {"data": {"models": [], "asset": {"code": "LUMC", "precision": 6}}},
    {"data": {"models": [{
        "kind": "chat", "billing_available": False, "price": {}
    }], "asset": {"code": "LUMC", "precision": 6}}},
    {"data": {"models": [{
        "kind": "chat", "billing_available": True,
        "price": {"input": {"per_mtok_minor": 100}}
    }], "asset": {"code": "LUMC", "precision": 6}}},
])
def test_incomplete_or_unavailable_price_fails_closed(corruption):
    with pytest.raises(lumean_pricing.PriceCheckUnavailable):
        lumean_pricing.estimate_lumc(corruption, 1000, 4000)


def test_expensive_model_blocked_before_chat_purchase(monkeypatch):
    monkeypatch.setenv("LUMEAN_MAX_REQUEST_LUMC", "0.02")
    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def json(self, content_type=None):
            return catalog()

    class Session:
        def get(self, url, headers):
            assert url.endswith("/gpt-4.1-mini")
            assert headers["X-API-KEY"] == "test"
            return Response()

    async def scenario():
        with pytest.raises(lumean_pricing.PriceCheckUnavailable, match="превышает лимит"):
            await lumean_pricing.preflight(Session(), "test", "gpt-4.1-mini", 1500, 7000)
    asyncio.run(scenario())


def test_affordable_model_passes_price_gate(monkeypatch):
    monkeypatch.setenv("LUMEAN_MAX_REQUEST_LUMC", "0.02")
    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def json(self, content_type=None):
            return catalog()

    class Session:
        def get(self, url, headers):
            return Response()

    cost = asyncio.run(lumean_pricing.preflight(
        Session(), "test", "gpt-4.1-mini", 1000, 4000))
    assert cost == pytest.approx(0.0146)


def test_user_invalid_limit_cannot_disable_safety(monkeypatch):
    for text in ("0", "-1", "nan", "NaN", "500000", "abc"):
        monkeypatch.setenv("LUMEAN_MAX_REQUEST_LUMC", text)
        assert lumean_pricing.max_request_lumc() == 0.5


def test_balanced_request_has_stricter_per_call_cap(monkeypatch):
    monkeypatch.setenv("LUMEAN_MAX_REQUEST_LUMC", "0.50")

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def json(self, content_type=None):
            return catalog(input_rate=2_000_000, output_rate=3_000_000)

    class Session:
        def get(self, url, headers):
            return Response()

    # 0.30 LUMC is within the global 0.50 cap but above balanced's 0.20.
    async def run():
        with pytest.raises(lumean_pricing.PriceCheckUnavailable, match="0.20 LUMC"):
            await lumean_pricing.preflight(
                Session(), "key", "gpt-4.1-mini", 0, 100_000, limit_lumc=0.20
            )
    asyncio.run(run())

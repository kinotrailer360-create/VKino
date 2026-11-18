import asyncio
from datetime import date

from PIL import Image
from shorts import ai, budget, plan


def test_plan_continues_after_last_hardcoded_day():
    assert plan.today(date(2027, 1, 5)) is not None


def test_paid_vision_gets_images_even_without_reserve_key(monkeypatch):
    monkeypatch.setattr(ai, 'lumean_llm_on', lambda: True)
    monkeypatch.setattr(ai, 'LUMEAN_MODE', 'all')
    monkeypatch.setattr(ai, 'cooling', lambda name: '')
    monkeypatch.setattr(ai, 'RESERVE_KEY', '')
    monkeypatch.setattr(ai, 'BEAT_KEY', '')
    async def paid(prompt, image, tokens):
        return {'seen': image is not None}
    monkeypatch.setattr(ai, '_ask_lumean', paid)
    async def scenario():
        parts = [{'text':'what is shown?'}, ai._thumb(Image.new('RGB',(30,30),'red'))]
        return await ai._with_reserve('', parts, 'what is shown?', paid_first=True)
    assert asyncio.run(scenario()) == {'seen':True}


def test_interrupted_budget_reservations_are_reconciled_conservatively(monkeypatch):
    class Pool:
        async def execute(self, sql):
            assert "status = 'settled'" in sql
            assert 'estimate_usd' not in sql or 'actual_usd' in sql
            return 'UPDATE 2'
    monkeypatch.setattr(budget, '_pool_getter', lambda: Pool())
    assert asyncio.run(budget.reconcile_interrupted()) == 2

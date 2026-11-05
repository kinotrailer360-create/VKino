"""Создаёт тестовую кампанию KinoMir360 в РСЯ через API Яндекс Директа v5.

Запуск:  YANDEX_DIRECT_TOKEN=... python3 promo/rsya/direct_campaign.py [--check]
--check  только проверить токен и доступ (ничего не создаёт).

Кампания создаётся только в сетях (поиск выключен), недельный бюджет 1000 ₽,
стратегия «максимум кликов». Объявления отправляются на модерацию, а сама кампания
ставится на паузу — включает её владелец в интерфейсе Директа.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request
from datetime import date
from pathlib import Path

API = os.getenv("DIRECT_API_URL", "https://api.direct.yandex.com/json/v5/")
TOKEN = os.environ.get("YANDEX_DIRECT_TOKEN", "")
LOGIN = os.getenv("DIRECT_CLIENT_LOGIN", "tco09")
HERE = Path(__file__).resolve().parent
NAME = "KinoMir360 — РСЯ тест 1000/нед"
MICRO = 1_000_000
WEEKLY = int(os.getenv("WEEKLY_BUDGET", "1000"))
BID_CEILING = int(os.getenv("BID_CEILING", "12"))
HREF = ("https://t.me/KinoMir360?utm_source=yandex&utm_medium=rsya"
        "&utm_campaign=kinomir360_test&utm_content={ad_id}")
KEYWORDS = ["---autotargeting", "новинки кино", "что посмотреть", "трейлеры фильмов",
            "премьеры фильмов 2026", "новые сериалы", "фильмы 2026", "афиша кино", "лучшие сериалы"]
NEGATIVE = ["скачать", "торрент", "бесплатно", "hd", "rezka", "lordfilm", "порно", "работа", "вакансии"]
ADS = [  # (заголовок, доп. заголовок, текст, картинка)
    ("Новинки кино и сериалов каждый день", "Канал KinoMir360",
     "Трейлеры, премьеры и подборки — что посмотреть вечером. Подпишитесь в Telegram!", "1x1"),
    ("Новинки кино и сериалов каждый день", "Канал KinoMir360",
     "Трейлеры, премьеры и подборки — что посмотреть вечером. Подпишитесь в Telegram!", "16x9"),
    ("Что посмотреть сегодня вечером?", "Подборки каждый день",
     "Свежие подборки фильмов и сериалов в Telegram-канале KinoMir360. Подписывайтесь!", "1x1"),
    ("Трейлеры новых фильмов раньше всех", "Telegram-канал о кино",
     "Новые трейлеры, даты премьер и новости кино каждый день. Подпишитесь в Telegram", "16x9"),
]
IMAGES = {"1x1": HERE / "tgo/kinomir360_1x1_1080x1080.jpg", "16x9": HERE / "tgo/kinomir360_16x9_1080x607.jpg"}


def call(service: str, method: str, params: dict) -> dict:
    body = json.dumps({"method": method, "params": params}).encode()
    req = urllib.request.Request(API + service, data=body, headers={
        "Authorization": f"Bearer {TOKEN}", "Client-Login": LOGIN,
        "Accept-Language": "ru", "Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    if "error" in data:
        e = data["error"]
        raise SystemExit(f"❌ {service}.{method}: {e.get('error_string')} — {e.get('error_detail')} (код {e.get('error_code')})")
    return data["result"]


def ids(result: dict, key: str) -> list:
    out = []
    for item in result[key]:
        if item.get("Errors"):
            raise SystemExit(f"❌ {key}: {json.dumps(item['Errors'], ensure_ascii=False)}")
        for w in item.get("Warnings", []):
            print("⚠️", w.get("Message"), w.get("Details", ""))
        out.append(item.get("Id") or item.get("AdImageHash"))
    return out


def main() -> None:
    if not TOKEN:
        raise SystemExit("Нет YANDEX_DIRECT_TOKEN в окружении")
    existing = call("campaigns", "get", {"SelectionCriteria": {}, "FieldNames": ["Id", "Name", "State", "Status"]})
    print(f"✅ Доступ есть, кампаний в аккаунте: {len(existing.get('Campaigns', []))}")
    if "--check" in sys.argv:
        return
    for c in existing.get("Campaigns", []):
        if c["Name"] == NAME:
            raise SystemExit(f"Кампания «{NAME}» уже есть: №{c['Id']} ({c['State']}/{c['Status']})")

    hashes = {}
    for key, path in IMAGES.items():
        res = call("adimages", "add", {"AdImages": [{
            "ImageData": base64.b64encode(path.read_bytes()).decode(), "Name": path.name}]})
        hashes[key] = ids(res, "AddResults")[0]
    print("🖼 Картинки загружены:", hashes)

    camp = call("campaigns", "add", {"Campaigns": [{
        "Name": NAME, "StartDate": date.today().isoformat(),
        "NegativeKeywords": {"Items": NEGATIVE},
        "TextCampaign": {"BiddingStrategy": {
            "Search": {"BiddingStrategyType": "SERVING_OFF"},
            "Network": {"BiddingStrategyType": "WB_MAXIMUM_CLICKS", "WbMaximumClicks": {
                "WeeklySpendLimit": WEEKLY * MICRO, "BidCeiling": BID_CEILING * MICRO}}}}}]})
    cid = ids(camp, "AddResults")[0]
    print("📣 Кампания:", cid)

    grp = call("adgroups", "add", {"AdGroups": [{"Name": "Кино — РФ", "CampaignId": cid, "RegionIds": [225]}]})
    gid = ids(grp, "AddResults")[0]
    ids(call("keywords", "add", {"Keywords": [{"Keyword": k, "AdGroupId": gid} for k in KEYWORDS]}), "AddResults")
    print("🔑 Ключевые фразы и автотаргетинг добавлены")

    ads = call("ads", "add", {"Ads": [{"AdGroupId": gid, "TextAd": {
        "Title": t, "Title2": t2, "Text": txt, "Href": HREF, "Mobile": "NO", "AdImageHash": hashes[img]}}
        for t, t2, txt, img in ADS]})
    ad_ids = ids(ads, "AddResults")
    print("🧾 Объявления:", ad_ids)
    call("ads", "moderate", {"SelectionCriteria": {"Ids": ad_ids}})
    print("🛂 Отправлены на модерацию")
    try:
        call("campaigns", "suspend", {"SelectionCriteria": {"Ids": [cid]}})
        print("⏸ Кампания на паузе")
    except SystemExit as e:  # черновик до модерации иногда нельзя остановить — денег всё равно не тратит
        print("ℹ️ Паузу поставить не удалось:", e)
    print(f"Готово: https://direct.yandex.ru/dna/grid/campaigns?ulogin={LOGIN}")


if __name__ == "__main__":
    main()

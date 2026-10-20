"""Произношение для синтеза речи: латиница и аббревиатуры -> как читать по-русски, ударения.

Встроенный словарь + свои замены из бота (/say). Ударение в замене — заглавной гласной
внутри слова: «замОк» -> «замо́к» (знак ударения U+0301).
"""

from __future__ import annotations

import re

DEFAULT: dict[str, str] = {
    # кинотеатры и форматы
    "IMAX": "Аймакс",
    "Dolby": "Долби",
    "3D": "три дэ",
    "4K": "четыре кей",
    "CGI": "си джи ай",
    "VFX": "ви эф экс",
    "Blu-ray": "блю-рэй",
    # студии
    "Warner Bros.": "Уорнер Бразерс",
    "Warner Bros": "Уорнер Бразерс",
    "Disney": "Дисней",
    "Pixar": "Пиксар",
    "Marvel": "Марвел",
    "DC": "Ди Си",
    "Universal": "Юниверсал",
    "Paramount": "Парамаунт",
    "Lionsgate": "Лайонсгейт",
    "Sony": "Сони",
    "A24": "Эй двадцать четыре",
    "DreamWorks": "Дримворкс",
    "Legendary": "Леджендари",
    # сервисы
    "Netflix": "Нетфликс",
    "HBO Max": "Эйч Би О Макс",
    "HBO": "Эйч Би О",
    "Apple TV+": "Эппл Ти Ви плюс",
    "Apple TV": "Эппл Ти Ви",
    "Amazon": "Амазон",
    "Prime Video": "Прайм Видео",
    "Hulu": "Хулу",
    "Okko": "Окко",
    "Kion": "Кион",
    "Wink": "Винк",
    "Start": "Старт",
    "YouTube": "Ютуб",
    "TikTok": "Тик Ток",
    # премии и прочее
    "IMDb": "Ай Эм Ди Би",
    "BAFTA": "БАФТА",
    "Emmy": "Эмми",
    "Oscar": "Оскар",
    "Comic-Con": "Комик-Кон",
    "CinemaCon": "СинемаКон",
    "D23": "Ди двадцать три",
}

STRESS = "́"
VOWELS = "АЕЁИОУЫЭЮЯ"

CUSTOM: dict[str, str] = {}  # свои замены, бот подгружает их из базы перед сборкой


def mark_stress(text: str) -> str:
    """«замОк» -> «замо́к», «ТЕнцинг» -> «Те́нцинг»: заглавная гласная внутри слова — ударная.

    Аббревиатуры (БАФТА) не трогаем: там за заглавной идёт заглавная.
    """
    def repl(m: re.Match[str]) -> str:
        before = text[m.start() - 1] if m.start() else ""
        after = text[m.end()] if m.end() < len(text) else ""
        stressed = before.islower() or (before.isalpha() and after.islower())
        return m.group(1).lower() + STRESS if stressed else m.group(1)

    return re.sub(rf"(?<=[а-яёА-ЯЁ])([{VOWELS}])", repl, text)


def _keep_case(original: str, spoken: str) -> str:
    """Слово с заглавной в начале предложения остаётся с заглавной."""
    if original[:1].isupper() and spoken[:1].islower():
        return spoken[:1].upper() + spoken[1:]
    return spoken


def apply(text: str, extra: dict[str, str] | None = None) -> str:
    """Заменяет слова на их произношение; длинные варианты — первыми (HBO Max раньше HBO)."""
    table = {**DEFAULT, **CUSTOM, **(extra or {})}
    for word in sorted(table, key=len, reverse=True):
        pattern = rf"(?<![\w-]){re.escape(word)}(?!\w)"  # «IMAX-камера» тоже заменяем
        flags = 0 if word.isupper() else re.IGNORECASE  # аббревиатуры (DC, IMAX) — строго заглавными
        text = re.sub(pattern, lambda m, w=word: _keep_case(m.group(0), mark_stress(table[w])), text, flags=flags)
    return text

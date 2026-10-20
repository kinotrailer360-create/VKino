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


# ---------------------------------------------------------------- запасной вариант без ИИ: цифры и латиница

_ONES = ["ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять", "десять",
         "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать", "шестнадцать",
         "семнадцать", "восемнадцать", "девятнадцать"]
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят",
         "девяносто"]
_HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот",
             "девятьсот"]
_SCALES = [("тысяча", "тысячи", "тысяч", True), ("миллион", "миллиона", "миллионов", False),
           ("миллиард", "миллиарда", "миллиардов", False)]


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 10 < n < 20:
        return many
    return one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many


def _triple(n: int, feminine: bool) -> list[str]:
    words = [_HUNDREDS[n // 100]] if n >= 100 else []
    n %= 100
    if n >= 20:
        words.append(_TENS[n // 10])
        n %= 10
    if n or not words:
        word = _ONES[n]
        if feminine and n in (1, 2):
            word = "одна" if n == 1 else "две"
        words.append(word)
    return [w for w in words if w]


def number_words(n: int) -> str:
    """1984 -> «тысяча девятьсот восемьдесят четыре» (именительный падеж)."""
    if n == 0:
        return "ноль"
    if n < 0:
        return "минус " + number_words(-n)
    words: list[str] = []
    for power in range(len(_SCALES), 0, -1):
        chunk = n // 1000 ** power % 1000
        if chunk:
            one, few, many, fem = _SCALES[power - 1]
            if not (power == 1 and chunk == 1):  # «тысяча», а не «одна тысяча»
                words += _triple(chunk, fem)
            words.append(plural(chunk, one, few, many))
    if n % 1000 or not words:
        words += _triple(n % 1000, False) if n % 1000 else []
    return " ".join(words)


def _number(m: re.Match[str]) -> str:
    whole, frac, percent = m.group(1), m.group(2), m.group(3)
    n = int(whole.replace(" ", "").replace(" ", ""))
    text = number_words(n)
    if frac:  # 7.8 -> «семь и восемь» (рейтинги так и читают)
        text += " и " + " ".join(number_words(int(d)) for d in frac) if len(frac) == 1 else \
            " и " + number_words(int(frac))
    if percent:
        text += " " + ("процента" if frac else plural(n, "процент", "процента", "процентов"))
    return text


_LATIN = [  # грубая транскрипция английского: сначала сочетания, потом буквы
    ("tion", "шн"), ("sch", "ш"), ("sh", "ш"), ("ch", "ч"), ("th", "з"), ("ph", "ф"), ("ck", "к"),
    ("qu", "кв"), ("ee", "и"), ("ea", "и"), ("oo", "у"), ("ou", "ау"), ("ow", "оу"), ("ay", "эй"),
    ("ai", "эй"), ("oa", "оу"), ("igh", "ай"), ("kn", "н"), ("wr", "р"), ("wh", "у"), ("ce", "се"),
    ("ci", "си"), ("cy", "си"), ("ge", "дже"), ("gi", "джи"), ("ya", "я"), ("yo", "йо"), ("yu", "ю"),
    ("ye", "е"),
]
_LETTERS = dict(zip("abcdefghijklmnopqrstuvwxyz", ["а", "б", "к", "д", "е", "ф", "г", "х", "и", "дж", "к", "л",
                                                    "м", "н", "о", "п", "к", "р", "с", "т", "у", "в", "у", "кс",
                                                    "и", "з"]))
_SPELL = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", ["эй", "би", "си", "ди", "и", "эф", "джи", "эйч", "ай", "джей",
                                                  "кей", "эл", "эм", "эн", "оу", "пи", "кью", "ар", "эс", "ти",
                                                  "ю", "ви", "дабл ю", "экс", "уай", "зед"]))


def _latin_word(m: re.Match[str]) -> str:
    word = m.group(0)
    if word.isupper() and len(word) <= 4:  # аббревиатура: FBI -> «эф би ай»
        return " ".join(_SPELL[c] for c in word)
    low = word.lower()
    if len(low) > 3 and low.endswith("e") and low[-2] not in "aeiouy":
        low = low[:-1]  # немая e на конце: Blade -> Блейд-ish
    out, i = "", 0
    while i < len(low):
        for src, dst in _LATIN:
            if low.startswith(src, i):
                out, i = out + dst, i + len(src)
                break
        else:
            out, i = out + _LETTERS.get(low[i], low[i]), i + 1
    return out[:1].upper() + out[1:] if word[:1].isupper() else out


def plain_russian(text: str) -> str:
    """Что осталось после ИИ: цифры — словами, латиница — русскими буквами (грубо, но читаемо)."""
    text = re.sub(r"(?<![\w.,])(\d{1,3}(?:[  ]\d{3})+|\d+)(?:[.,](\d{1,2})(?!\d))?(?:\s?(%))?", _number, text)
    return re.sub(r"[A-Za-z]+", _latin_word, text)


def needs_rewrite(text: str) -> bool:
    return bool(re.search(r"[0-9A-Za-z]", text))

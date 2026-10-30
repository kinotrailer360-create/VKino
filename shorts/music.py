"""Библиотека фоновой музыки под настроение ролика: треки делает Lumean (музыка по описанию, 30 с).

Бот заказывает треки один раз (кнопка «🎼 Музыка Lumean» в настройках), хранит их в Telegram (file_id в
настройках) и держит копию в DIR/<настроение>/. Для ролика настроение угадывается по заголовку и тексту:
хоррор-подборка — мрачная музыка, комедия — весёлая. Своя музыка («Сменить музыку») важнее библиотеки.
"""

from __future__ import annotations

import asyncio
import os
import random
from pathlib import Path

DIR = Path(os.getenv("MUSIC_DIR", "/tmp/kinomir_shorts/music"))
TAIL = ("instrumental background music for a voiceover, steady energy from start to end, no long silences, "
        "no vocals")
# настроение: (подпись, описание для Lumean, слова в заголовке/тексте ролика)
MOODS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "horror": ("😱 Хоррор", "Dark horror movie trailer underscore, eerie drones, low strings, tension pulses",
               ("ужас", "хоррор", "страш", "кошмар", "зомби", "демон", "призрак", "мистик", "проклят")),
    "thriller": ("🔪 Триллер", "Suspenseful thriller underscore, ticking pulse, dark synth bass, mysterious piano",
                 ("триллер", "детектив", "маньяк", "преступ", "убий", "расследов", "тайн")),
    "action": ("💥 Боевик", "Epic action movie trailer music, driving percussion, aggressive hybrid orchestra",
               ("боевик", "экшн", "погон", "войн", "супергеро", "марвел", "marvel", "драк")),
    "comedy": ("😂 Комедия", "Playful quirky comedy underscore, pizzicato strings, light percussion, upbeat and fun",
               ("комеди", "смешн", "юмор", "ржач", "угар")),
    "animation": ("🧸 Мультфильмы", "Magical whimsical animated movie music, bright orchestra, celesta, joyful",
                  ("мульт", "аниме", "анимац", "pixar", "пиксар", "дисней", "disney")),
    "scifi": ("🚀 Фантастика", "Futuristic sci-fi soundtrack, pulsing synths, spacey pads, wonder and mystery",
              ("фантаст", "космос", "косми", "будущ", "робот", "инопланет", "киберпанк")),
    "drama": ("💔 Драма", "Emotional cinematic drama underscore, soft piano and warm strings, touching, hopeful",
              ("драм", "мелодрам", "любов", "роман", "трогат", "слёз", "слез")),
    "nostalgia": ("📼 Ностальгия", "Nostalgic retro 80s synthwave, warm analog synths, dreamy, cinematic",
                  ("ностальг", "90-х", "80-х", "детств", "ретро", "классик", "старые")),
    "news": ("📰 Новости", "Modern upbeat news background music, energetic electronic beat, cinematic hits",
             ("новост", "премьер", "анонс", "скоро в кино", "выход")),
    "epic": ("🎬 Общая (эпик)", "Epic cinematic trailer background music, inspiring orchestra, steady build, drums",
             ()),
}
DEFAULT = "epic"
# прощальный ролик — всегда спокойная драма, даже если в тексте «Marvel» или «боевик»
MOURNING = ("умер", "скончал", "ушёл из жизни", "ушел из жизни", "не стало", "светлая память", "прощаемся",
            "памяти ", "соболезн", "некролог")
LOOP_COPIES = 4  # 30-секундный трек → ~1 мин 50 с с плавными стыками
CROSSFADE = 3.0


def prompt(mood: str) -> str:
    return f"{MOODS[mood][1]}, {TAIL}"


def mood_for(*texts: str) -> str:
    """Настроение по текстам ролика — от главного (заголовок) к второстепенному (реплики)."""
    if any(w in (t or "").lower() for t in texts for w in MOURNING):
        return "drama"
    for text in texts:
        low = (text or "").lower()
        for mood, (_, _, words) in MOODS.items():
            if any(w in low for w in words):
                return mood
    return DEFAULT


def tracks(mood: str) -> list[Path]:
    return sorted((DIR / mood).glob("*.mp3"))


def pick(*texts: str) -> Path | None:
    """Трек библиотеки под ролик: по настроению, иначе общий, иначе None (стандартная музыка)."""
    mood = mood_for(*texts)
    found = tracks(mood) or tracks(DEFAULT)
    return random.choice(found) if found else None


async def make_long(src: Path, dst: Path) -> Path:
    """Склеить трек с самим собой с плавными переходами — хватит на любой шортс."""
    inputs: list[str] = []
    for _ in range(LOOP_COPIES):
        inputs += ["-i", str(src)]
    chain, last = "", "[0:a]"
    for k in range(1, LOOP_COPIES):
        out = f"[x{k}]" if k < LOOP_COPIES - 1 else "[a]"
        chain += f"{last}[{k}:a]acrossfade=d={CROSSFADE}{out};"
        last = out
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", chain.rstrip(";"), "-map", "[a]",
        "-c:a", "libmp3lame", "-b:a", "160k", str(dst),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode:
        raise RuntimeError(f"ffmpeg: {err.decode(errors='ignore')[-200:]}")
    return dst

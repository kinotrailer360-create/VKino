"""Запасной голос Piper отдельным процессом: модель (~150 МБ) не остаётся в памяти бота.

python -m shorts.piper_tts МОДЕЛЬ СКОРОСТЬ < [["текст", "путь.wav"], ...]
"""

import json
import sys
import wave


def main(model: str, speed: str) -> None:
    from piper import PiperVoice, SynthesisConfig

    voice = PiperVoice.load(model)
    config = SynthesisConfig(length_scale=float(speed))
    for text, path in json.load(sys.stdin):
        with wave.open(path, "wb") as out:
            voice.synthesize_wav(text, out, syn_config=config)


if __name__ == "__main__":
    main(*sys.argv[1:3])

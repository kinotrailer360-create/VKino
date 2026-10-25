"""Озвучка харчо: Piper (русский голос dmitri), фразы ставятся на моменты из VOICE.

python3 kharcho_voice.py [путь_к_модели.onnx]  →  kharcho_voice.wav (48 кГц, моно, ровно 30 с)

Проверяет, что фразы не налезают друг на друга и укладываются в 30 секунд.
"""

import json
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
from piper import PiperVoice, SynthesisConfig

HERE = Path(__file__).resolve().parent
SR = 48000
PRE = int(0.08 * SR)
SPEED = 1.05  # length_scale Piper: чуть медленнее для разборчивости


def timeline() -> dict:
    out = subprocess.run(["node", "-e", "console.log(JSON.stringify(require('./kharcho_animation.js')))"],
                         cwd=HERE, check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def main(model: str = "/opt/piper/voice.onnx") -> None:
    tl = timeline()
    voice = PiperVoice.load(model)
    parts = HERE / "build" / "voice"
    parts.mkdir(parents=True, exist_ok=True)
    total = int(round(tl["DURATION"] * SR))
    track = np.zeros(total, dtype=np.float32)
    report, prev_end = [], 0.0
    for i, line in enumerate(tl["VOICE"]):
        raw, res = parts / f"{i:02d}.wav", parts / f"{i:02d}.f32"
        with wave.open(str(raw), "wb") as out:
            voice.synthesize_wav(line.get("tts", line["text"]), out, syn_config=SynthesisConfig(length_scale=SPEED))
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-ar", str(SR), "-ac", "1",
                        "-f", "f32le", str(res)], check=True)
        pcm = np.fromfile(res, dtype="<f4")
        # срезаем тишину по краям, оставляя 80 мс запаса перед первым звуком, чтобы
        # не потерять тихие взрывные согласные; звук начинается точно в line.at
        idx = np.nonzero(np.abs(pcm) > 0.01)[0]
        lead = min(PRE, idx[0]) if len(idx) else 0
        pcm = pcm[idx[0] - lead: idx[-1] + 2400] if len(idx) else pcm
        start = int(round(line["at"] * SR)) - lead
        end = start + len(pcm)
        if start / SR < prev_end + 0.12:
            raise SystemExit(f"фраза {i} «{line['text']}» начинается раньше чем через 0,12 с после предыдущей ({start / SR:.2f} / {prev_end:.2f})")
        if end > total:
            raise SystemExit(f"фраза {i} «{line['text']}» не помещается в {tl['DURATION']} с (конец {end / SR:.2f})")
        fade = np.ones(len(pcm), dtype=np.float32)
        fade[:120] = np.linspace(0, 1, 120)
        fade[-480:] = np.linspace(1, 0, 480)
        track[start:end] += pcm * fade
        prev_end = end / SR
        report.append(f"{line['at']:6.2f}–{prev_end:5.2f} с  {line['text']}")
    peak = float(np.max(np.abs(track)))
    track *= 0.89 / peak  # −1 дБFS
    pcm16 = (np.clip(track, -1, 1) * 32767).astype("<i2")
    with wave.open(str(HERE / "kharcho_voice.wav"), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SR)
        out.writeframes(pcm16.tobytes())
    (HERE / "build" / "voice_timing.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n".join(report))


if __name__ == "__main__":
    main(*sys.argv[1:])

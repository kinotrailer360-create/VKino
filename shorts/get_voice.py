"""Скачать запасной голос Piper для шортсов (запускается при сборке Docker-образа).

Мужской русский голос с Hugging Face; если не скачался — женский из релизов Piper на GitHub.
Ошибка не ломает сборку: без файла голоса бот просто работает без запасного голоса.
"""

import io
import sys
import tarfile
import urllib.request
from pathlib import Path

HF = "https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium.onnx"
GH = "https://github.com/rhasspy/piper/releases/download/v0.0.2/voice-ru-irinia-medium.tar.gz"


def main(out_dir: str = "/opt/piper") -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    model, config = out / "voice.onnx", out / "voice.onnx.json"
    try:
        urllib.request.urlretrieve(HF, model)
        urllib.request.urlretrieve(HF + ".json", config)
        print("piper voice: dmitri (HF)")
        return
    except Exception as exc:  # noqa: BLE001
        print("piper voice: HF failed:", exc)
    try:
        with urllib.request.urlopen(GH, timeout=120) as resp:
            tar = tarfile.open(fileobj=io.BytesIO(resp.read()))
        for member in tar.getmembers():
            if member.name.endswith(".onnx.json"):
                config.write_bytes(tar.extractfile(member).read())
            elif member.name.endswith(".onnx"):
                model.write_bytes(tar.extractfile(member).read())
        print("piper voice: irinia (GitHub)")
    except Exception as exc:  # noqa: BLE001
        model.unlink(missing_ok=True)
        print("piper voice: download failed, fallback voice disabled:", exc)


if __name__ == "__main__":
    main(*sys.argv[1:])

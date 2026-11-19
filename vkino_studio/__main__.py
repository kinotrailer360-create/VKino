"""Windows launcher: GUI by default; headless preview with --project."""
from __future__ import annotations

import argparse
from pathlib import Path

from .renderer import render


def main() -> None:
    parser = argparse.ArgumentParser(description="VKino Studio: локальный монтаж утверждённых сцен")
    parser.add_argument("--project", type=Path, help="Путь к project.json; без параметра откроется GUI")
    parser.add_argument("--output", type=Path, help="Папка результата")
    parser.add_argument("--encoder", choices=["libx264", "h264_nvenc"], default="libx264")
    args = parser.parse_args()
    if args.project is None:
        from .desktop import start
        start()
        return
    result = render(args.project, args.output or args.project.parent / "output", args.encoder, print)
    print(f"Результат: {result}")


if __name__ == "__main__":
    main()

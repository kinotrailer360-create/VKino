"""Тизер под инфоповод «приквел «Очень странных дел»» — рендер в shorts/teaser.py.

python3 promo/rsya/make_teaser.py  →  promo/rsya/video/teaser_strange_{tg,max}_1x1.mp4
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from shorts import teaser  # noqa: E402

if __name__ == "__main__":
    for k in teaser.CTA:
        p = teaser.render(teaser.STRANGE, ROOT / f"promo/rsya/video/teaser_strange_{k}_1x1.mp4", k)
        print(p.name, f"{p.stat().st_size / 1e6:.1f} МБ")

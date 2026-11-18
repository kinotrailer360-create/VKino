"""Copy legacy container work into its mounted data directory before replacement."""
from pathlib import Path
import shutil
import sys

source = Path(sys.argv[1] if len(sys.argv) > 1 else '/tmp/kinomir_shorts')
target = Path(sys.argv[2] if len(sys.argv) > 2 else '/data/work')
if source.exists() and source.resolve() != target.resolve():
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, dirs_exist_ok=True)

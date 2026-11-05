"""Raccourci à la racine du projet : ``python3 main.py <SRC_DIR> <DEST_DIR>``."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from pyxtidy.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
